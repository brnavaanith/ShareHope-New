"""ShareHope reusable email service (Flask-Mail + Gmail SMTP).

Reads MAIL_SERVER, MAIL_PORT, MAIL_USE_TLS, MAIL_USERNAME,
MAIL_PASSWORD and MAIL_DEFAULT_SENDER from .env — never hardcoded.
Never logs passwords or secrets.

Design:
- Stateless signed tokens (itsdangerous) for verification (24h) and
  password reset (1h). Single-use without extra tables:
    * verify: valid only while user.email_verified is False;
      first use sets it True, reuse is rejected.
    * reset: token binds to the current password_hash; using it
      changes the hash, so the same token can never work twice.
- All sends are best-effort and non-blocking: failures return False
  and never raise, so successful DB transactions are never undone.
- Duplicate emails are prevented by the caller: email helpers are
  invoked only after a successful commit AND only when the
  corresponding notification was newly created (see _notify return).
"""

import logging
import os
import re
import smtplib
import socket
import threading

from flask import render_template, url_for
from flask_mail import Mail, Message as MailMessage
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger(__name__)

mail = Mail()

# One send at a time: the SMTP transport is chosen per attempt (see
# _send_with_fallback), so the shared Flask-Mail state must never be
# observed mid-swap by another thread.
_send_lock = threading.Lock()

# Failures that happen BEFORE anything is handed to the SMTP server, so
# retrying on another transport can never deliver a duplicate message.
# NB smtplib.SMTPException derives from OSError, so protocol-level
# errors (bad credentials, rejected sender) must be excluded explicitly
# below, otherwise they would be retried as if they were transport
# faults — SMTPResponseError is checked first in _is_connect_error.
_TRANSPORT_ERRORS = (
    TimeoutError, socket.timeout, ConnectionError, OSError,
    smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
    smtplib.SMTPHeloError, smtplib.SMTPNotSupportedError,
)

# A protocol-level rejection means the server did talk to us: the
# message was never delivered, but switching ports cannot help (bad
# credentials, blocked sender, policy rejection).
_PROTOCOL_ERRORS = (
    smtplib.SMTPAuthenticationError, smtplib.SMTPResponseException,
    smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused,
)


def _is_connect_error(exc):
    """True only for transport faults worth retrying on another port."""
    if isinstance(exc, _PROTOCOL_ERRORS):
        return False
    return isinstance(exc, _TRANSPORT_ERRORS)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

VERIFY_SALT = "sharehope-verify-v1"
RESET_SALT = "sharehope-reset-v1"
VERIFY_MAX_AGE = 24 * 3600  # 24 hours
RESET_MAX_AGE = 3600  # 1 hour


def _secret():
    from flask import current_app
    return current_app.config.get("SECRET_KEY", "dev-only-change-me")


def _env_flag(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def init_mail(app):
    """Configure Flask-Mail from .env (Gmail SMTP) and init the app.

    Honours MAIL_USE_SSL so a deployment that cannot reach the
    submission port can use implicit TLS on 465. Missing credentials
    are fine in dev/test: sends become logged no-ops returning False
    instead of raising.
    """
    server = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    # Implicit TLS (465) and STARTTLS (587) are the two Gmail submission
    # transports. Read SSL first so the port default matches it.
    use_ssl = _env_flag("MAIL_USE_SSL", False)
    default_port = "465" if use_ssl else "587"
    port_raw = os.getenv("MAIL_PORT", default_port)
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = int(default_port)
    # STARTTLS on 465 is invalid, and vice versa: keep the pair coherent.
    use_tls = _env_flag("MAIL_USE_TLS", not use_ssl) and not use_ssl
    username = os.getenv("MAIL_USERNAME") or None
    password = os.getenv("MAIL_PASSWORD") or None
    sender = os.getenv("MAIL_DEFAULT_SENDER", "ShareHope <no-reply@sharehope.local>")

    app.config.setdefault("MAIL_SERVER", server)
    app.config.setdefault("MAIL_PORT", port)
    app.config.setdefault("MAIL_USE_TLS", use_tls)
    app.config.setdefault("MAIL_USE_SSL", use_ssl)
    app.config.setdefault("MAIL_USERNAME", username)
    app.config.setdefault("MAIL_PASSWORD", password)
    app.config.setdefault("MAIL_DEFAULT_SENDER", sender)
    # Short timeouts so SMTP can never hang a request for long.
    app.config.setdefault("MAIL_TIMEOUT", 10)

    # init_app is idempotent-safe: only register once per app.
    if "mail" not in getattr(app, "extensions", {}):
        mail.init_app(app)
    return mail


def _transports():
    """Candidate SMTP transports, configured one first.

    Some networks (and some office/ISP firewalls) block the 587
    submission port while leaving 465 reachable, or the reverse. Trying
    the configured one first and the implicit-TLS port second makes
    delivery work on both without any .env edit.
    """
    from flask import current_app
    cfg = current_app.config
    server = cfg.get("MAIL_SERVER") or "smtp.gmail.com"
    port = cfg.get("MAIL_PORT") or 587
    use_ssl = bool(cfg.get("MAIL_USE_SSL"))
    use_tls = bool(cfg.get("MAIL_USE_TLS"))
    candidates = [(server, port, use_ssl, use_tls)]
    alt_port = 465 if int(port) != 465 else 587
    candidates.append((server, alt_port, alt_port == 465, alt_port != 465))
    # De-duplicate, preserving the configured order.
    seen, ordered = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def _send_with_fallback(msg, timeout):
    """Send via Flask-Mail, retrying the alternate SMTP transport.

    Only connection-level failures trigger a retry, so a message can
    never be delivered twice. Returns True when the message was
    accepted by the server. Raises the last error if every transport
    fails, so the caller can log it as before.
    """
    state = current_mail_state()
    if state is None:
        # Mail extension not initialised (defensive): plain send.
        mail.send(msg)
        return True
    last_exc, last_name, retryable = None, "unknown", False
    for server, port, use_ssl, use_tls in _transports():
        with _send_lock:
            # Swap the transport for this attempt only, then restore so
            # the app's configured values are never permanently altered.
            saved = (state.server, state.port, state.use_ssl, state.use_tls)
            state.server, state.port = server, port
            state.use_ssl, state.use_tls = use_ssl, use_tls
            try:
                old_timeout = socket.getdefaulttimeout()
                try:
                    socket.setdefaulttimeout(timeout)
                    mail.send(msg)
                finally:
                    socket.setdefaulttimeout(old_timeout)
                return True
            except Exception as exc:  # noqa: BLE001 - retried below
                # Keep the exception itself: the `as` name is unbound once
                # the block exits.
                last_exc, last_name = exc, type(exc).__name__
                retryable = _is_connect_error(exc)
            finally:
                (state.server, state.port,
                 state.use_ssl, state.use_tls) = saved
        if not retryable:
            raise last_exc
        log.warning(
            "SMTP transport %s:%s unavailable (%s); trying alternate port.",
            server, port, last_name)
    if last_exc is not None:
        raise last_exc
    return False


def current_mail_state():
    """The live Flask-Mail state object, or None if not initialised."""
    try:
        from flask import current_app
        return (getattr(current_app, "extensions", {}) or {}).get("mail")
    except Exception:
        return None


def is_configured():
    """True when SMTP credentials exist (else sends are no-ops)."""
    from flask import current_app
    try:
        cfg = current_app.config
    except RuntimeError:
        return False
    return bool(cfg.get("MAIL_USERNAME") and cfg.get("MAIL_PASSWORD"))


def valid_email(address):
    return bool(address and EMAIL_RE.match(address.strip()))


def send_email(to, subject, html_body, text_body):
    """Best-effort send. Returns True on success, False otherwise.

    Never raises and never logs secrets. Invalid addresses are
    rejected without contacting SMTP.
    """
    from flask import current_app
    if current_app.config.get("MAIL_SUPPRESS_SEND"):
        # Automated tests set this: never touch SMTP.
        log.info("Email suppressed (MAIL_SUPPRESS_SEND): to=%s subject=%s",
                 _mask(to), subject)
        return False
    if not valid_email(to):
        log.warning("Email not sent: invalid recipient address.")
        return False
    if not is_configured():
        # Dev/test without credentials: log without secrets, no raise.
        log.info("Email skipped (SMTP not configured): to=%s subject=%s",
                 _mask(to), subject)
        try:
            current_app.logger.info(
                "Email skipped (SMTP not configured) to=%s subject=%s",
                _mask(to), subject)
        except Exception:
            pass
        return False
    try:
        sender = current_app.config.get("MAIL_DEFAULT_SENDER")
        msg = MailMessage(subject=subject, recipients=[to.strip()],
                          body=text_body, html=html_body, sender=sender)
        # Flask-Mail 0.10 ignores MAIL_TIMEOUT, so enforce it via the
        # socket default: SMTP can never hang a request longer than
        # MAIL_TIMEOUT. Restored afterwards; never raises.
        try:
            _timeout = int(current_app.config.get("MAIL_TIMEOUT", 10) or 10)
        except (TypeError, ValueError):
            _timeout = 10
        return _send_with_fallback(msg, _timeout)
    except Exception as exc:
        # Log type only — never the address book, body, or secrets.
        log.warning("Email send failed: %s subject=%s (%s)",
                    _mask(to), subject, type(exc).__name__)
        try:
            current_app.logger.warning(
                "Email send failed to=%s subject=%s (%s)",
                _mask(to), subject, type(exc).__name__)
        except Exception:
            pass
        return False


def _mask(address):
    try:
        local, _, domain = address.partition("@")
        if not domain:
            return "***"
        return (local[:1] + "***@" + domain) if local else "***@" + domain
    except Exception:
        return "***"


# ---------- tokens (stateless, expiring, single-use) ----------

def generate_verify_token(user):
    s = URLSafeTimedSerializer(_secret(), salt=VERIFY_SALT)
    return s.dumps({"uid": user.id, "email": user.email})


def confirm_verify_token(token, max_age=VERIFY_MAX_AGE):
    """Return user if token valid AND not already used; else None.

    Single-use: users with email_verified=True are rejected, so a
    second click on the same link fails closed with a generic message.
    """
    from extensions import db
    import models
    from flask import current_app
    try:
        s = URLSafeTimedSerializer(_secret(), salt=VERIFY_SALT)
        data = s.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired, Exception):
        return None
    try:
        uid = int(data.get("uid"))
    except (TypeError, ValueError):
        return None
    try:
        user = db.session.get(models.User, uid)
    except Exception:
        return None
    if user is None:
        return None
    # Bind to the address the token was issued for.
    if (data.get("email") or "").strip().lower() != (user.email or "").lower():
        return None
    if getattr(user, "email_verified", False):
        return None
    return user


def generate_reset_token(user):
    s = URLSafeTimedSerializer(_secret(), salt=RESET_SALT)
    # Bind to current hash prefix: after a successful reset the hash
    # changes, so the same token can never validate again (single-use).
    return s.dumps({"uid": user.id,
                    "pw": (user.password_hash or "")[:32]})


def confirm_reset_token(token, max_age=RESET_MAX_AGE):
    """Return user if token valid, unexpired and unused; else None."""
    from extensions import db
    import models
    try:
        s = URLSafeTimedSerializer(_secret(), salt=RESET_SALT)
        data = s.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired, Exception):
        return None
    try:
        uid = int(data.get("uid"))
    except (TypeError, ValueError):
        return None
    try:
        user = db.session.get(models.User, uid)
    except Exception:
        return None
    if user is None:
        return None
    if (data.get("pw") or "") != (user.password_hash or "")[:32]:
        return None  # already used (password changed) or wrong user state
    return user


# ---------- per-type helpers (each best-effort, bool return) ----------

def send_welcome_email(user):
    try:
        html = render_template("emails/welcome.html", user=user)
        text = render_template("emails/welcome.txt", user=user)
    except Exception:
        html = (f"<p>Welcome to ShareHope, {user.name}!</p>"
                f"<p>Your {user.role} account is ready.</p>")
        text = f"Welcome to ShareHope, {user.name}! Your {user.role} account is ready."
    return send_email(user.email, "Welcome to ShareHope", html, text)


def send_verification_email(user):
    try:
        token = generate_verify_token(user)
        link = url_for("verify_email", token=token, _external=True)
    except Exception:
        return False
    try:
        html = render_template("emails/verify.html", user=user, link=link)
        text = render_template("emails/verify.txt", user=user, link=link)
    except Exception:
        html = f"<p>Please verify your email: <a href='{link}'>{link}</a></p>"
        text = f"Please verify your email: {link}"
    return send_email(user.email, "Verify your ShareHope email", html, text)


def send_password_reset_email(user):
    try:
        token = generate_reset_token(user)
        link = url_for("reset_password", token=token, _external=True)
    except Exception:
        return False
    try:
        html = render_template("emails/reset.html", user=user, link=link)
        text = render_template("emails/reset.txt", user=user, link=link)
    except Exception:
        html = f"<p>Reset your password: <a href='{link}'>{link}</a> (expires in 1 hour).</p>"
        text = f"Reset your password: {link} (expires in 1 hour)."
    return send_email(user.email, "Reset your ShareHope password", html, text)


def send_donation_status_email(user, title, status_line, detail=""):
    """Generic donation-progress email (accept/decline/handover/complete)."""
    try:
        html = render_template("emails/donation_update.html", user=user,
                               title=title, status_line=status_line,
                               detail=detail)
        text = render_template("emails/donation_update.txt", user=user,
                               title=title, status_line=status_line,
                               detail=detail)
    except Exception:
        html = f"<p>{status_line}</p><p>{detail}</p>"
        text = f"{status_line}\n{detail}"
    return send_email(user.email, f"ShareHope: {title}", html, text)


def send_message_alert_email(receiver, sender, snippet):
    try:
        html = render_template("emails/message_alert.html", user=receiver,
                               sender=sender, snippet=snippet)
        text = render_template("emails/message_alert.txt", user=receiver,
                               sender=sender, snippet=snippet)
    except Exception:
        html = (f"<p>New message from {sender.name}.</p>")
        text = f"New message from {sender.name}."
    return send_email(receiver.email,
                      f"New ShareHope message from {sender.name}", html, text)
