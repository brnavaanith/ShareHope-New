
"""ShareHope — MySQL connection + database-backed authentication."""

import os
import re
import threading
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

import click

from dotenv import load_dotenv
from flask import (
    Flask, copy_current_request_context, flash, redirect, render_template,
    request, session, url_for
)
from flask_login import (
    LoginManager, current_user, login_required,
    login_user, logout_user,
)
from extensions import db
from flask_wtf.csrf import CSRFProtect, CSRFError
from sqlalchemy import case, func, text

from catalog import CATEGORIES

CAT_SLUGS = {c["slug"] for c in CATEGORIES}
CAT_NAMES = {c["slug"]: c["name"] for c in CATEGORIES}

OFFER_EDITABLE = {"pending", "declined"}
REQ_TRANSITIONS = {"open": {"closed", "fulfilled"}, "closed": {"open"}, "fulfilled": set()}

load_dotenv()

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "FLASK_SECRET_KEY", "dev-only-change-me"
)

# ---------- Cookie security (local HTTP dev: Secure left off) ----------
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"

csrf = CSRFProtect(app)

# ---------- Email (Flask-Mail + Gmail SMTP, best-effort) ----------
import email_service  # noqa: E402  (needs app object for config)

email_service.init_mail(app)

# ---------- Assistant (Gemini, server-side only; key never leaves server) ----------
import assistant_service  # noqa: E402  (needs app object for session/config)

# ---------- MySQL configuration ----------

db_user = os.getenv("DB_USER")
db_password = os.getenv("DB_PASSWORD")
db_host = os.getenv("DB_HOST", "localhost")
db_port = os.getenv("DB_PORT", "3306")
db_name = os.getenv("DB_NAME", "sharehope")

if not db_user or not db_password:
    raise RuntimeError("Database credentials are missing from .env")

app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"mysql+pymysql://{db_user}:{quote_plus(db_password)}"
    f"@{db_host}:{db_port}/{db_name}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

import models  # noqa: E402,F401  (registers tables for init-db; no route changes)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Log in to open that page."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(models.User, int(user_id))
    except (TypeError, ValueError):
        return None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.context_processor
def inject_catalog():
    """Shared taxonomy for every page; no donation records are invented here."""
    return {"categories": CATEGORIES}


@app.context_processor
def inject_unread_count():
    """Per-user unread badge. Anonymous visitors get zero, never a count."""
    if not current_user.is_authenticated:
        return {"unread_count": 0}
    return {"unread_count": models.Notification.query.filter_by(
        recipient_id=current_user.id, is_read=False).count()}


@app.context_processor
def inject_message_counts():
    """Per-user unread-message badge. Anonymous visitors get zero.

    Kept separate from ``inject_unread_count`` (notifications) so the
    completed notification system is untouched. Counts only rows where
    the current user is the receiver and ``is_read`` is False.
    """
    if not current_user.is_authenticated:
        return {"unread_msg_count": 0}
    return {"unread_msg_count": models.Message.query.filter_by(
        receiver_id=current_user.id, is_read=False).count()}


def _safe_next(value):
    """Return ``value`` only if it is a safe local path, else None."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None


def _dashboard_for(role):
    return {
        "donor": "donor",
        "ngo": "ngo",
        "admin": "admin"
    }.get(role, "donor")


def _send_registration_emails(user_id):
    """Best-effort welcome + verification emails AFTER a committed signup.

    Synchronous when TESTING or MAIL_SUPPRESS_SEND (deterministic for
    automated tests, never touches SMTP). Otherwise runs in a daemon
    thread with the request context copied so ``url_for(_external=True)``
    still builds correct links — registration never waits for SMTP.
    SMTP failure never undoes the account; returns (welcome_ok, verify_ok)
    synchronously, or (None, None) when queued in background.
    """
    if app.config.get("TESTING") or app.config.get("MAIL_SUPPRESS_SEND"):
        try:
            with app.app_context():
                user = db.session.get(models.User, user_id)
                if user is None:
                    return False, False
                w = email_service.send_welcome_email(user)
                try:
                    v = email_service.send_verification_email(user)
                except Exception:
                    app.logger.exception("Verification email failed")
                    v = False
                return bool(w), bool(v)
        except Exception:
            app.logger.exception("Registration emails failed")
            return False, False

    @copy_current_request_context
    def _bg(uid):
        try:
            user = db.session.get(models.User, uid)
            if user is None:
                return
            try:
                email_service.send_welcome_email(user)
            except Exception:
                app.logger.exception("Welcome email failed")
            try:
                email_service.send_verification_email(user)
            except Exception:
                app.logger.exception("Verification email failed")
        except Exception:
            app.logger.exception("Registration emails failed")
        finally:
            try:
                db.session.remove()
            except Exception:
                pass

    try:
        t = threading.Thread(target=_bg, args=(user_id,), daemon=True)
        t.start()
    except Exception:
        app.logger.exception("Registration email thread failed")
    return None, None


# Auth pages hold credentials; the three pages below render the live
# donation status (slip + tracker). None may be served from the browser
# cache: a replayed document shows stale values (old credentials, or a
# slip whose handover/completion status has since changed). Static
# assets keep their normal caching.
NO_STORE_PREFIXES = (
    "/login", "/register", "/logout", "/forgot-password",
    "/reset-password", "/verify-email",
    "/donor", "/ngo", "/tracking",
)


@app.after_request
def _no_store_auth_pages(response):
    """Send `no-store` for auth pages and live-status dashboards.

    Without this a browser may reuse a previously rendered document on
    back/forward navigation, replaying stale login values or a donation
    slip whose handover/completion status has since changed. Flask's
    test client has no cache, so this is only observable in a browser.
    Password managers (autocomplete) are untouched.
    """
    try:
        p = request.path or ""
    except Exception:
        return response
    if p.startswith(NO_STORE_PREFIXES):
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, max-age=0")
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---------- Main pages ----------

@app.get("/")
def index():
    # The Home slip is whatever an admin chose — never auto-selected and
    # never random. Returns None until an admin features a donation, so
    # the page falls back to its clean empty state.
    slip = _home_slip(_current_featureed_offer())
    return render_template("index.html", slip=slip, urgent=[
        {"id": r.id, "title": r.title,
         "category": CAT_NAMES.get(r.category, r.category),
         "quantity": r.required_quantity or "—", "location": r.location or "—",
         "org": _req_org(r)} for r in models.NGORequirement.query.filter_by(
            status="open", urgency="urgent").order_by(
            models.NGORequirement.created_at.desc()).limit(2).all()
    ])


@app.get("/health")
def health():
    return {"ok": True}


# ---------- Database connection test ----------

@app.get("/db-check")
def db_check():
    try:
        db.session.execute(text("SELECT 1"))
        return {"ok": True, "database": "connected"}
    except Exception:
        app.logger.exception("Database connection failed")
        db.session.rollback()
        return {"ok": False, "database": "disconnected"}, 500


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    # Invalid/expired token: safe message, no internals, no stack trace.
    flash("Your session expired — please try again.", "error")
    return redirect(request.referrer or url_for("index")), 400


@app.cli.command("init-db")
def init_db():
    """Create missing tables (and profile columns) only. Never drops data."""
    with app.app_context():
        before = set(db.inspect(db.engine).get_table_names())
        db.create_all()
        added_columns = models.ensure_profile_columns()
        added_columns += models.ensure_email_columns()
        added_tables = []
        if models.ensure_featured_slip_table():
            added_tables.append("featured_slip")
        after = set(db.inspect(db.engine).get_table_names())
    print(f"tables before: {sorted(before)}")
    print(f"tables after: {sorted(after)}")
    print(f"created: {sorted(after - before) or 'none (already up to date)'}")
    print(f"columns added: {added_columns or 'none (already up to date)'}")
    print(f"tables ensured: {added_tables or 'none (already up to date)'}")


@app.cli.command("create-admin")
@click.argument("email", required=False)
@click.option("--name", prompt=True, help="Admin display name")
def create_admin(email, name):
    """Create an admin account from the server console.

    Secure by design: admins are NEVER created through public
    registration (the /register form only offers donor/ngo). This
    command must be run by someone with server/DB access. The
    password is prompted interactively (never stored in shell
    history) and must be at least 8 characters.
    """
    import click
    if not email:
        email = click.prompt("Admin email")
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        click.echo("Invalid email address.")
        raise SystemExit(1)
    with app.app_context():
        if models.User.query.filter_by(email=email).first() is not None:
            click.echo(f"An account with email {email} already exists.")
            raise SystemExit(1)
        password = click.prompt("Admin password", hide_input=True,
                                confirmation_prompt=True)
        if len(password) < 8:
            click.echo("Password must be at least 8 characters.")
            raise SystemExit(1)
        admin = models.User(name=(name or "").strip() or email.split("@")[0],
                             email=email, role="admin")
        admin.set_password(password)
        db.session.add(admin)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Admin creation failed")
            click.echo("Could not create the admin account.")
            raise SystemExit(1)
        click.echo(f"Admin account created for {email}.")


# ---------- Auth (database-backed) ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(_dashboard_for(current_user.role)))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not EMAIL_RE.match(email) or not password:
            flash("Enter a valid email address and your password.", "error")
            return render_template("login.html", email=email), 400

        user = models.User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            # Generic message: never reveal whether the email exists.
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email), 401

        login_user(user)
        flash(f"Welcome back, {user.name}.", "success")
        target = _safe_next(request.form.get("next"))
        return redirect(target or url_for(_dashboard_for(user.role)))

    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/register", methods=["GET", "POST"])
def register():
    # Logged-in users already have an account: send them to their desk
    # instead of showing a stale/empty signup form.
    if request.method == "GET" and current_user.is_authenticated:
        return redirect(url_for(_dashboard_for(current_user.role)))
    if request.method == "POST":
        role = (request.form.get("role") or "donor").strip()
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        city = (request.form.get("city") or "").strip()
        org = (request.form.get("org") or "").strip()
        regid = (request.form.get("regid") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if role not in {"donor", "ngo"}:
            flash("Choose Donor or NGO.", "error")
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city
            ), 400

        if len(name) < 2:
            flash("Please tell us your name.", "error")
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org
            ), 400

        if not EMAIL_RE.match(email):
            flash("Enter a valid email address.", "error")
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org
            ), 400

        if len(city) < 2:
            flash("City helps NGOs plan pickup — please add it.", "error")
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org
            ), 400

        if role == "ngo" and (len(org) < 2 or len(regid) < 3):
            flash(
                "NGOs need an organisation name and registration ID.",
                "error"
            )
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org, regid=regid
            ), 400

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org
            ), 400

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org
            ), 400

        if models.User.query.filter_by(email=email).first() is not None:
            flash(
                "An account with that email already exists. "
                "Try logging in instead.",
                "error",
            )
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org
            ), 409

        user = models.User(
            name=name, email=email, role=role, city=city,
            org_name=org or None, org_reg_id=regid or None,
        )
        user.set_password(password)
        db.session.add(user)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Registration failed")
            flash("Could not create the account. Please try again.", "error")
            return render_template(
                "register.html", role=role, name=name,
                email=email, city=city, org=org
            ), 500

        login_user(user)
        flash(f"Welcome to ShareHope, {name}.", "success")
        # Best-effort emails AFTER commit: SMTP failure must never undo
        # the successful registration, and production signups must not
        # wait for SMTP (background thread; sync only in tests).
        _welcome_ok, _verify_ok = _send_registration_emails(user.id)
        # Sync path (tests / suppressed SMTP): mirror old flashes.
        if _verify_ok is True:
            flash("Please check your inbox to verify your email.", "info")
        elif _verify_ok is None:
            # Background path (production): verification was queued.
            flash("Please check your inbox to verify your email.", "info")
        return redirect(url_for(_dashboard_for(role)))

    return render_template(
        "register.html",
        role=request.args.get("type", "donor")
    )


@app.get("/logout")
@login_required
def logout():
    logout_user()
    # Full session cleanup so back-button/login never replays stale
    # credentials or flashes. Flask-Login clears the user; we clear the
    # rest (assistant history/rate-limit, flashes are re-added below).
    try:
        session.clear()
    except Exception:
        pass
    flash("You have been signed out.", "info")
    resp = redirect(url_for("index"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


# ---------- Email verification & password reset (stateless tokens) ----------

@app.get("/verify-email/<token>")
def verify_email(token):
    """Verify an address via expiring single-use link. Works logged out."""
    user = email_service.confirm_verify_token(token)
    if user is None:
        flash("That verification link is invalid, expired, or already used.", "error")
        return redirect(url_for("index")), 400
    try:
        user.email_verified = True
        user.verified_at = datetime.utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Email verification failed")
        flash("Could not verify your email. Please try again.", "error")
        return redirect(url_for("index")), 500
    flash("Email verified — thank you.", "success")
    return redirect(url_for("login"))


@app.get("/verify-email")
@login_required
def verify_resend():
    """Resend the verification email (only while unverified)."""
    if getattr(current_user, "email_verified", False):
        flash("Your email is already verified.", "info")
        return redirect(url_for(_dashboard_for(current_user.role)))
    try:
        sent = email_service.send_verification_email(current_user)
    except Exception:
        app.logger.exception("Verification resend failed")
        sent = False
    if sent:
        flash("Verification email sent — check your inbox.", "success")
    else:
        flash("Could not send the verification email right now.", "error")
    return redirect(url_for(_dashboard_for(current_user.role)))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            # Same generic response for bad input: no enumeration.
            flash("If an account exists for that email, a reset link is on its way.", "info")
            return redirect(url_for("login"))
        user = models.User.query.filter_by(email=email).first()
        # Best-effort: never reveal whether the address is registered.
        if user is not None:
            try:
                email_service.send_password_reset_email(user)
            except Exception:
                app.logger.exception("Password-reset email failed")
        flash("If an account exists for that email, a reset link is on its way.", "info")
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = email_service.confirm_reset_token(token)
    if user is None:
        flash("That reset link is invalid, expired, or already used.", "error")
        return redirect(url_for("forgot_password")), 400
    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset.html", token=token), 400
        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("reset.html", token=token), 400
        try:
            user.set_password(password)
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Password reset failed")
            flash("Could not save the new password. Please try again.", "error")
            return render_template("reset.html", token=token), 500
        # Password hash changed → the same token can never validate again.
        flash("Password updated — please log in.", "success")
        return redirect(url_for("login"))
    return render_template("reset.html", token=token)


# ---------- Browse pages (no fabricated records) ----------

@app.get("/explore")
def explore():
    offers = models.DonationOffer.query.filter_by(status="pending").order_by(
        models.DonationOffer.created_at.desc()).all()
    return render_template("explore.html", donations=[
        {"title": o.title, "category": CAT_NAMES.get(o.category, o.category),
         "category_slug": o.category, "quantity": o.quantity or "—",
         "location": o.location or "—", "availability": o.availability,
         "donor": o.donor.name if o.donor else "A donor",
         "posted": _fmt_date(o.created_at)} for o in offers
    ], db_pending=False)


@app.get("/requirements")
def requirements():
    reqs = models.NGORequirement.query.filter_by(status="open").order_by(
        models.NGORequirement.urgency.desc(),
        models.NGORequirement.created_at.desc()).all()
    return render_template("requirements.html", requirements=[
        {"id": r.id, "title": r.title,
         "category": CAT_NAMES.get(r.category, r.category),
         "category_slug": r.category, "quantity_needed": r.required_quantity or "—",
         "location": r.location or "—", "urgency": r.urgency,
         "deadline": _fmt_date(r.deadline),
         "org": (_req_org(r) or "An NGO")} for r in reqs
    ], db_pending=False)


@app.get("/requirements/<int:req_id>")
def requirement_detail(req_id):
    """Public requirement details so donors can open a need and respond.

    Shows the full requirement (title, category, quantity, location,
    urgency, deadline, description, organisation). The respond form
    itself requires a donor login (POST is gated); anonymous visitors
    see the details plus a login prompt.
    """
    req = db.session.get(models.NGORequirement, req_id)
    if req is None:
        flash("Requirement not found.", "error")
        return redirect(url_for("requirements")), 404
    category_name = CAT_NAMES.get(req.category, req.category)
    org = _req_org(req) or "An NGO"
    already_responded = False
    try:
        if current_user.is_authenticated and current_user.role == "donor":
            already_responded = models.DonationTracking.query.join(
                models.DonationOffer,
                models.DonationTracking.offer_id == models.DonationOffer.id
            ).filter(
                models.DonationTracking.requirement_id == req.id,
                models.DonationOffer.donor_id == current_user.id
            ).first() is not None
    except Exception:
        already_responded = False
    return render_template(
        "requirement_detail.html", req=req, category_name=category_name,
        org=org, already_responded=already_responded)


@app.post("/requirements/<int:req_id>/respond")
@login_required
def requirement_respond(req_id):
    """Donor response to an open NGO requirement.

    Creates one DonationOffer (donor-owned, status pending) plus one
    DonationTracking link (pending/pending) to this requirement, logs
    history, and notifies the NGO. Idempotent on double submit:
    a recent identical offer+link is treated as a duplicate.
    """
    if current_user.role != "donor":
        flash("Only donor accounts can respond to NGO needs.", "error")
        return redirect(url_for(_dashboard_for(current_user.role)))
    req = db.session.get(models.NGORequirement, req_id)
    if req is None or req.status != "open":
        flash("That need is no longer open for responses.", "error")
        return redirect(url_for("requirements")), 404 if req is None else 400
    if req.ngo_id == current_user.id:
        flash("You cannot respond to your own requirement.", "error")
        return redirect(url_for("requirement_detail", req_id=req.id)), 400
    # Force the offer category to the requirement's category so the
    # link always matches; other fields come from the donor's form.
    form = dict(request.form)
    form["category"] = req.category
    cleaned, error = _validate_offer(form)
    if error:
        flash(error, "error")
        category_name = CAT_NAMES.get(req.category, req.category)
        return render_template(
            "requirement_detail.html", req=req,
            category_name=category_name, org=_req_org(req) or "An NGO",
            already_responded=False, form=request.form), 400
    # Duplicate-submit guard: same donor/title/requirement within 60s.
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=60)
        dup = (models.DonationOffer.query.filter(
            models.DonationOffer.donor_id == current_user.id,
            models.DonationOffer.title == cleaned["title"],
            models.DonationOffer.created_at >= cutoff).first())
        if dup is not None:
            existing_link = models.DonationTracking.query.filter_by(
                offer_id=dup.id, requirement_id=req.id).first()
            if existing_link is not None:
                flash("Already sent — your response is with the NGO.", "info")
                return redirect(url_for("requirement_detail", req_id=req.id))
    except Exception:
        pass
    try:
        offer = models.DonationOffer(
            donor_id=current_user.id, status="pending", **cleaned)
        db.session.add(offer)
        db.session.flush()
        _log_history(offer.id, req.id, None, "pending", current_user.id)
        # Link is pending/pending: NGO decides via existing accept/decline.
        existing = models.DonationTracking.query.filter_by(
            offer_id=offer.id, requirement_id=req.id).first()
        if existing is None:
            db.session.add(models.DonationTracking(
                offer_id=offer.id, requirement_id=req.id,
                acceptance_status="pending", handover_status="pending"))
        new_notif = _notify(req.ngo_id, "need",
                            f"New response to “{req.title}”: “{offer.title}” "
                            f"from {current_user.name} "
                            f"({CAT_NAMES.get(offer.category, offer.category)}, "
                            f"{offer.location or '—'}).")
        ngo_id, offer_title = req.ngo_id, offer.title
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Requirement response failed")
        flash("Could not send your response. Please try again.", "error")
        category_name = CAT_NAMES.get(req.category, req.category)
        return render_template(
            "requirement_detail.html", req=req,
            category_name=category_name, org=_req_org(req) or "An NGO",
            already_responded=False, form=request.form), 500
    if new_notif:
        try:
            ngo_user = db.session.get(models.User, ngo_id)
            if ngo_user is not None:
                email_service.send_donation_status_email(
                    ngo_user, "New donation response",
                    f"New response to “{req.title}”: “{offer_title}” "
                    f"from {current_user.name}.",
                    "Open ShareHope → NGO inbox to accept or decline.")
        except Exception:
            app.logger.exception("Response email failed")
        # Note: response email is best-effort AFTER commit; SMTP failure
        # never undoes the saved response (same pattern as offers).
    flash("Response sent — the NGO can now accept or decline it.", "success")
    return redirect(url_for("requirement_detail", req_id=req.id))


@app.get("/messages")
@login_required
def messages():
    """Real donor–NGO inbox backed by the ``messages`` table.

    Conversations are pairs of users (no separate thread row, so
    duplicates are impossible by construction). ``?with=<user_id>``
    selects one thread; viewing marks only the current user's incoming
    rows as read. Uses existing ``Message`` fields only.
    """
    raw = (request.args.get("with") or "").strip()
    active_id = None
    if raw:
        try:
            active_id = int(raw)
        except (TypeError, ValueError):
            flash("Conversation not found.", "error")
            return redirect(url_for("messages")), 404
    conversations = _conversation_list(current_user.id)
    contactables = _contactable_users(current_user)
    thread = []
    active_other = None
    if active_id is not None:
        if active_id == current_user.id:
            flash("Conversation not found.", "error")
            return redirect(url_for("messages")), 404
        other = db.session.get(models.User, active_id)
        if other is None:
            flash("Conversation not found.", "error")
            return redirect(url_for("messages")), 404
        if not _can_view_thread(current_user, other):
            flash("Conversation not found.", "error")
            return redirect(url_for("messages")), 404
        active_other = other
        thread = _thread_messages(current_user.id, active_id)
        try:
            _mark_thread_read(current_user.id, active_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Mark-thread-read failed")
        # re-read so is_read flags reflect the commit
        thread = _thread_messages(current_user.id, active_id)
    return render_template(
        "messages.html",
        conversations=conversations,
        contactables=contactables,
        active_other=active_other,
        thread=thread,
        with_id=active_id,
    )


@app.post("/messages/send")
@login_required
def message_send():
    """Send one message to a permitted recipient.

    Validation: recipient must exist, not self, and satisfy
    ``_can_message``; content 1–2000 chars. Duplicate submits
    (same sender/receiver/content within 60s) are ignored.
    """
    try:
        to_id = int((request.form.get("to") or "").strip())
    except (TypeError, ValueError):
        flash("Conversation not found.", "error")
        return redirect(url_for("messages")), 404
    if to_id == current_user.id:
        flash("Conversation not found.", "error")
        return redirect(url_for("messages")), 404
    other = db.session.get(models.User, to_id)
    if other is None:
        flash("Conversation not found.", "error")
        return redirect(url_for("messages")), 404
    if not _can_message(current_user, other):
        flash("You can only message NGOs/donors connected to your donations.", "error")
        return redirect(url_for("messages")), 403
    cleaned, error = _validate_message_content(request.form.get("content"))
    if error:
        flash(error, "error")
        target = url_for("messages") + f"?with={other.id}"
        return redirect(target), 400
    if _is_duplicate_message(current_user.id, other.id, cleaned):
        flash("Already sent.", "info")
        return redirect(url_for("messages") + f"?with={other.id}")
    try:
        db.session.add(models.Message(
            sender_id=current_user.id, receiver_id=other.id,
            content=cleaned, is_read=False))
        new_notif = _notify_new_message(other.id, current_user, cleaned)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Message send failed")
        flash("Could not send the message. Please try again.", "error")
        return redirect(url_for("messages") + f"?with={other.id}"), 500
    # Best-effort message alert AFTER commit; only for genuinely new
    # threads/messages (deduped above + new_notif) so retries never
    # send duplicate emails. SMTP failure never undoes the send.
    if new_notif:
        try:
            snippet = cleaned if len(cleaned) <= 120 else cleaned[:120].rstrip() + "…"
            email_service.send_message_alert_email(other, current_user, snippet)
        except Exception:
            app.logger.exception("Message alert email failed")
    return redirect(url_for("messages") + f"?with={other.id}")


@app.get("/messages/thread/<int:other_id>/json")
@login_required
def message_thread_json(other_id):
    """Polling endpoint: messages in one thread after ``after_id``.

    Polling only — not real-time delivery. Marks the viewer's incoming
    rows as read (never the other user's). Participant-only.
    """
    if other_id == current_user.id:
        return {"ok": False, "error": "Conversation not found."}, 404
    other = db.session.get(models.User, other_id)
    if other is None or not _can_view_thread(current_user, other):
        return {"ok": False, "error": "Conversation not found."}, 404
    try:
        after_id = int((request.args.get("after_id") or "0").strip() or "0")
    except (TypeError, ValueError):
        after_id = 0
    msgs = (models.Message.query.filter(
        (((models.Message.sender_id == current_user.id)
          & (models.Message.receiver_id == other_id))
         | ((models.Message.sender_id == other_id)
            & (models.Message.receiver_id == current_user.id))),
        models.Message.id > after_id)
        .order_by(models.Message.created_at.asc(), models.Message.id.asc())
        .limit(200).all())
    try:
        _mark_thread_read(current_user.id, other_id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Mark-thread-read (json) failed")
    unread = models.Message.query.filter_by(
        receiver_id=current_user.id, is_read=False).count()
    return {"ok": True, "messages": [{
        "id": m.id,
        "from_me": m.sender_id == current_user.id,
        "sender": m.sender.name if m.sender else "—",
        "content": m.content,
        "created_at": m.created_at.isoformat() if m.created_at else "",
        "is_read": m.is_read,
    } for m in msgs], "unread_msg_count": unread}


@app.get("/messages/unread-count")
@login_required
def message_unread_count():
    """Lightweight sidebar polling: current user's unread-message total."""
    n = models.Message.query.filter_by(
        receiver_id=current_user.id, is_read=False).count()
    return {"ok": True, "unread_msg_count": n}


# ---------- Messaging helpers (reuse existing Message fields only) ----------

MSG_MAX_LEN = 2000
MSG_DUP_WINDOW_SEC = 60


def _validate_message_content(raw):
    text = (raw or "").strip()
    if not text:
        return None, "Write a message before sending."
    if len(text) > MSG_MAX_LEN:
        return None, f"Keep messages under {MSG_MAX_LEN} characters."
    return text, None


def _has_prior_thread(a_id, b_id):
    return db.session.query(models.Message.id).filter(
        (((models.Message.sender_id == a_id)
          & (models.Message.receiver_id == b_id))
         | ((models.Message.sender_id == b_id)
            & (models.Message.receiver_id == a_id)))
    ).first() is not None


def _shares_donation_link(donor, ngo):
    """True if a tracking row already connects this donor and NGO."""
    try:
        links = (models.DonationTracking.query
                 .join(models.DonationOffer,
                       models.DonationTracking.offer_id == models.DonationOffer.id)
                 .filter(models.DonationOffer.donor_id == donor.id).all())
    except Exception:
        return False
    for link in links:
        try:
            req = link.requirement
        except Exception:
            req = None
        if req is not None and req.ngo_id == ngo.id:
            return True
    return False


def _has_matching_ledger(donor, ngo):
    """True if the donor has an offer matching an open NGO requirement."""
    try:
        offers = models.DonationOffer.query.filter_by(
            donor_id=donor.id).all()
        reqs = models.NGORequirement.query.filter_by(
            ngo_id=ngo.id, status="open").all()
    except Exception:
        return False
    for offer in offers:
        for req in reqs:
            try:
                if _offer_matches(offer, req):
                    return True
            except Exception:
                continue
    return False


def _can_message(sender, recipient):
    """Whether ``sender`` may start/continue a thread with ``recipient``."""
    if sender is None or recipient is None:
        return False
    if sender.id == recipient.id:
        return False
    # Admins may coordinate with anyone (support/moderation).
    if sender.role == "admin" or recipient.role == "admin":
        return True
    roles = {sender.role, recipient.role}
    if roles != {"donor", "ngo"}:
        return False
    donor = sender if sender.role == "donor" else recipient
    ngo = recipient if recipient.role == "ngo" else sender
    if _has_prior_thread(donor.id, ngo.id):
        return True
    if _shares_donation_link(donor, ngo):
        return True
    return _has_matching_ledger(donor, ngo)


def _can_view_thread(viewer, other):
    """Viewing is allowed for participants: prior thread or may-message."""
    if viewer.id == other.id:
        return False
    if _has_prior_thread(viewer.id, other.id):
        return True
    return _can_message(viewer, other)


def _contactable_users(user, limit=50):
    """Users ``user`` is allowed to message (for the start-chat form)."""
    if user.role == "admin":
        return (models.User.query.filter(models.User.id != user.id)
                .order_by(models.User.name.asc()).limit(limit).all())
    want = "ngo" if user.role == "donor" else "donor" if user.role == "ngo" else None
    if want is None:
        return []
    candidates = (models.User.query.filter_by(role=want)
                  .order_by(models.User.name.asc()).limit(200).all())
    out = [u for u in candidates if _can_message(user, u)]
    return out[:limit]


def _conversation_list(user_id, limit=100):
    """Distinct threads for ``user_id``: other user + latest + unread."""
    rows = (models.Message.query.filter(
        (models.Message.sender_id == user_id)
        | (models.Message.receiver_id == user_id))
        .order_by(models.Message.created_at.desc(),
                  models.Message.id.desc()).limit(2000).all())
    seen = {}
    for m in rows:
        other_id = m.receiver_id if m.sender_id == user_id else m.sender_id
        if other_id is None or other_id in seen:
            continue
        seen[other_id] = m
        if len(seen) >= limit:
            break
    convos = []
    for other_id, latest in seen.items():
        other = db.session.get(models.User, other_id)
        if other is None:
            continue
        unread = models.Message.query.filter_by(
            sender_id=other_id, receiver_id=user_id,
            is_read=False).count()
        convos.append({"other": other, "latest": latest,
                       "unread": unread, "last_at": latest.created_at})
    convos.sort(key=lambda c: (c["last_at"] is None, c["last_at"]),
                reverse=True)
    return convos


def _thread_messages(user_id, other_id, limit=500):
    return (models.Message.query.filter(
        (((models.Message.sender_id == user_id)
          & (models.Message.receiver_id == other_id))
         | ((models.Message.sender_id == other_id)
            & (models.Message.receiver_id == user_id))))
        .order_by(models.Message.created_at.asc(),
                  models.Message.id.asc()).limit(limit).all())


def _mark_thread_read(user_id, other_id):
    """Mark only ``user_id``'s incoming rows as read (never the other's)."""
    now = datetime.utcnow()
    (models.Message.query.filter_by(
        sender_id=other_id, receiver_id=user_id,
        is_read=False).update(
        {"is_read": True, "read_at": now},
        synchronize_session=False))


def _is_duplicate_message(sender_id, receiver_id, content):
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(seconds=MSG_DUP_WINDOW_SEC)
    return (models.Message.query.filter_by(
        sender_id=sender_id, receiver_id=receiver_id,
        content=content).filter(
        models.Message.created_at >= cutoff).first() is not None)


def _notify_new_message(receiver_id, sender, content):
    """One persistent notification per incoming message (deduped).

    Reuses the completed ``_notify`` helper (type ``info``), so exact
    unread duplicates never fan out twice. Snippet keeps the
    notification short; the full text stays in ``messages``.
    Returns True when a new notification was queued.
    """
    name = sender.name if sender and sender.name else "Someone"
    snippet = (content or "").strip().replace("\n", " ")
    if len(snippet) > 120:
        snippet = snippet[:120].rstrip() + "…"
    return _notify(receiver_id, "info", f"New message from {name}: “{snippet}”")


@app.get("/analytics")
def analytics():
    """Live ledger analytics — all figures from real database aggregates.

    Public page (as before): platform totals only, never private user
    details. Logged-in donors/NGOs additionally see a personal panel
    scoped to their own records; admins see platform metadata.
    Invalid filters fall back to defaults (same forgiving pattern as
    the notifications ``show`` filter); the JSON endpoint is strict.
    """
    filters, _error = _parse_analytics_filters(request.args, strict=False)
    try:
        stats = _analytics_stats(filters, _viewer())
    except Exception:
        app.logger.exception("Analytics aggregation failed")
        flash("Analytics are unavailable right now. Please try again later.",
              "error")
        stats = _empty_analytics_stats(filters)
        return render_template("analytics.html", stats=stats,
                               filters=filters, db_error=True), 500
    return render_template("analytics.html", stats=stats,
                           filters=filters, db_pending=False)


@app.get("/analytics/data")
def analytics_data():
    """Machine-readable analytics for charts and tests.

    Same scoping as the page. Strict validation: invalid parameters
    return 400 JSON. Never exposes names, emails, or message contents.
    """
    filters, error = _parse_analytics_filters(request.args, strict=True)
    if error:
        return {"ok": False, "error": error}, 400
    try:
        stats = _analytics_stats(filters, _viewer())
    except Exception:
        app.logger.exception("Analytics aggregation failed")
        return {"ok": False, "error": "Analytics unavailable."}, 500
    stats["ok"] = True
    return stats


# ---------- Analytics helpers (efficient aggregate queries only) ----------

ANALYTICS_GRANS = ("daily", "weekly", "monthly")
ANALYTICS_MAX_DAYS = 366
OFFER_STATUSES = ("pending", "accepted", "handed_over", "declined")


def _viewer():
    """Authenticated user or None (public page stays public)."""
    try:
        if current_user.is_authenticated:
            return current_user
    except Exception:
        pass
    return None


def _parse_analytics_filters(args, strict=False):
    """Validate ``from``/``to`` (YYYY-MM-DD) and ``gran``.

    Returns (filters, error). Forgiving mode falls back to defaults;
    strict mode returns an error string for invalid input.
    """
    today = date.today()
    default_to = today
    # Default: first day of the month, five months back (six buckets).
    m = default_to.month - 5
    y = default_to.year
    while m <= 0:
        m += 12
        y -= 1
    default_from = date(y, m, 1)

    gran = (args.get("gran") or "monthly").strip().lower()
    if gran not in ANALYTICS_GRANS:
        if strict:
            return None, "gran must be daily, weekly or monthly."
        gran = "monthly"

    def _parse_day(raw, fallback):
        raw = (raw or "").strip()
        if not raw:
            return fallback, None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date(), None
        except ValueError:
            return None, f"Date must be YYYY-MM-DD (got {raw!r})."

    from_day, err1 = _parse_day(args.get("from"), default_from)
    to_day, err2 = _parse_day(args.get("to"), default_to)
    if strict and (err1 or err2):
        return None, err1 or err2
    if from_day is None or to_day is None:
        from_day, to_day = default_from, default_to
    if from_day > to_day:
        if strict:
            return None, "from must not be after to."
        from_day, to_day = default_from, default_to
    if (to_day - from_day).days > ANALYTICS_MAX_DAYS:
        if strict:
            return None, f"Date range too large (max {ANALYTICS_MAX_DAYS} days)."
        from_day, to_day = default_from, default_to
    return {"from": from_day.isoformat(), "to": to_day.isoformat(),
            "gran": gran}, None


def _day_bounds(filters):
    start = datetime.strptime(filters["from"], "%Y-%m-%d")
    end = datetime.strptime(filters["to"], "%Y-%m-%d") + timedelta(days=1)
    return start, end


def _day_expr(column):
    """Dialect-aware day expression (identical semantics on both DBs)."""
    try:
        dialect = db.engine.dialect.name
    except Exception:
        dialect = "sqlite"
    if dialect == "sqlite":
        return func.strftime("%Y-%m-%d", column)
    return func.date_format(column, "%Y-%m-%d")


def _bucket_list(filters):
    """Every bucket in range with its member days (empty → zero).

    Returns [(bucket_id, label, [day_str, ...])]. Weekly buckets are
    Monday-start windows identified by the Monday date — dialect-free,
    so SQLite tests and MySQL agree exactly.
    """
    gran = filters["gran"]
    start = datetime.strptime(filters["from"], "%Y-%m-%d").date()
    end = datetime.strptime(filters["to"], "%Y-%m-%d").date()
    if gran == "daily":
        buckets, cur = [], start
        while cur <= end:
            buckets.append((cur.isoformat(), cur.strftime("%d %b"),
                            [cur.isoformat()]))
            cur += timedelta(days=1)
        return buckets
    if gran == "weekly":
        buckets, cur = [], start - timedelta(days=start.weekday())
        while cur <= end:
            days = [(cur + timedelta(days=i)).isoformat()
                    for i in range(7)]
            days = [d for d in days
                    if start.isoformat() <= d <= end.isoformat()]
            buckets.append((cur.isoformat(), "w/c " + cur.strftime("%d %b"),
                            days))
            cur += timedelta(days=7)
        return buckets
    buckets, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(
            cur.year, cur.month + 1, 1)
        day, days = cur, []
        while day < nxt and day <= end:
            if day >= start:
                days.append(day.isoformat())
            day += timedelta(days=1)
        buckets.append((f"{cur.year:04d}-{cur.month:02d}",
                        cur.strftime("%b %Y"), days))
        cur = nxt
    return buckets


def _empty_analytics_stats(filters):
    cats = [{"slug": c["slug"], "name": c["name"], "offers": 0}
            for c in CATEGORIES]
    statuses = [{"status": s, "count": 0} for s in OFFER_STATUSES]
    buckets = [{"bucket": b, "label": l, "offers": 0, "completed": 0}
               for b, l, _days in _bucket_list(filters)]
    regs = [{"bucket": b, "label": l, "donors": 0, "ngos": 0}
            for b, l, _days in _bucket_list(filters)]
    return {"filters": filters,
            "overview": {"total_offers": 0, "completed": 0, "pending": 0,
                         "active_ngos": 0, "donors": 0,
                         "completion_rate": 0.0},
            "trends": buckets,
            "by_category": cats,
            "by_status": statuses,
            "users": {"donors": 0, "ngos": 0, "registrations": regs},
            "personal": None}


def _daily_offer_map(from_day, to_day, extra=()):
    """{day_str: (offers, completed)} via one GROUP BY day query."""
    day = _day_expr(models.DonationOffer.created_at).label("day")
    rows = db.session.query(
        day,
        func.count(models.DonationOffer.id),
        func.sum(case((models.DonationOffer.status == "handed_over", 1),
                      else_=0)),
    ).filter(models.DonationOffer.created_at >= from_day,
             models.DonationOffer.created_at < to_day,
             *extra).group_by("day").all()
    return {r[0]: (int(r[1]), int(r[2] or 0)) for r in rows}


def _daily_user_map(from_day, to_day):
    """{day_str: {'donor': n, 'ngo': n}} via one GROUP BY query."""
    day = _day_expr(models.User.created_at).label("day")
    rows = db.session.query(
        day, models.User.role, func.count(models.User.id),
    ).filter(models.User.created_at >= from_day,
             models.User.created_at < to_day,
             models.User.role.in_(("donor", "ngo"))
             ).group_by("day", models.User.role).all()
    out = {}
    for d, role, n in rows:
        out.setdefault(d, {"donor": 0, "ngo": 0})
        if role in ("donor", "ngo"):
            out[d][role] = int(n)
    return out


def _analytics_stats(filters, viewer):
    """All platform aggregates with a bounded date range.

    Uses GROUP BY queries (never full-table Python scans). Each offer
    is counted exactly once per breakdown (grouped by its own status /
    category columns). No names, emails, or message contents included.
    """
    from_day, to_day = _day_bounds(filters)
    gran = filters["gran"]
    buckets = _bucket_list(filters)

    offer_q = models.DonationOffer.query.filter(
        models.DonationOffer.created_at >= from_day,
        models.DonationOffer.created_at < to_day)

    total = offer_q.count()
    completed = offer_q.filter_by(status="handed_over").count()
    pending = offer_q.filter_by(status="pending").count()
    rate = round(completed / total * 100, 1) if total else 0.0

    # Category counts (all eight slugs, zero-filled — zero is data).
    cat_rows = dict(db.session.query(
        models.DonationOffer.category, func.count(models.DonationOffer.id)
    ).filter(models.DonationOffer.created_at >= from_day,
             models.DonationOffer.created_at < to_day
             ).group_by(models.DonationOffer.category).all())
    by_category = [{"slug": c["slug"], "name": c["name"],
                    "offers": int(cat_rows.get(c["slug"], 0))}
                   for c in CATEGORIES]

    # Status counts (each offer counted exactly once).
    status_rows = dict(db.session.query(
        models.DonationOffer.status, func.count(models.DonationOffer.id)
    ).filter(models.DonationOffer.created_at >= from_day,
             models.DonationOffer.created_at < to_day
             ).group_by(models.DonationOffer.status).all())
    by_status = [{"status": s, "count": int(status_rows.get(s, 0))}
                 for s in OFFER_STATUSES]

    # Trends: one daily GROUP BY, rolled up to buckets (empty → zero).
    daily_offers = _daily_offer_map(from_day, to_day)
    trends = []
    for b, label, days in buckets:
        n = sum(daily_offers.get(d, (0, 0))[0] for d in days)
        c = sum(daily_offers.get(d, (0, 0))[1] for d in days)
        trends.append({"bucket": b, "label": label,
                       "offers": n, "completed": c})

    # User activity: aggregate counts only, never identities.
    donors = models.User.query.filter(
        models.User.role == "donor",
        models.User.created_at >= from_day,
        models.User.created_at < to_day).count()
    ngos = models.User.query.filter(
        models.User.role == "ngo",
        models.User.created_at >= from_day,
        models.User.created_at < to_day).count()
    daily_users = _daily_user_map(from_day, to_day)
    registrations = []
    for b, label, days in buckets:
        dn = sum(daily_users.get(d, {}).get("donor", 0) for d in days)
        gn = sum(daily_users.get(d, {}).get("ngo", 0) for d in days)
        registrations.append({"bucket": b, "label": label,
                              "donors": dn, "ngos": gn})

    # Active NGOs: distinct NGOs with a requirement in range.
    active_ngos = db.session.query(
        func.count(func.distinct(models.NGORequirement.ngo_id))
    ).filter(models.NGORequirement.created_at >= from_day,
             models.NGORequirement.created_at < to_day,
             models.NGORequirement.ngo_id.isnot(None)).scalar() or 0

    stats = {
        "filters": filters,
        "overview": {"total_offers": total, "completed": completed,
                     "pending": pending,
                     "active_ngos": int(active_ngos), "donors": donors,
                     "completion_rate": rate},
        "trends": trends,
        "by_category": by_category,
        "by_status": by_status,
        "users": {"donors": donors, "ngos": ngos,
                  "registrations": registrations},
        "personal": _personal_stats(viewer, from_day, to_day, gran, buckets),
    }
    # Admin-only platform metadata: omitted entirely for others.
    if viewer is not None and viewer.role == "admin":
        stats["admin"] = {
            "messages": models.Message.query.count(),
            "notifications": models.Notification.query.count(),
            "admins": models.User.query.filter_by(role="admin").count(),
        }
    return stats


def _personal_stats(viewer, from_day, to_day, gran, buckets):
    """Records belonging to the viewer only (None when anonymous)."""
    if viewer is None:
        return None
    if viewer.role == "donor":
        base = models.DonationOffer.query.filter(
            models.DonationOffer.donor_id == viewer.id,
            models.DonationOffer.created_at >= from_day,
            models.DonationOffer.created_at < to_day)
        total = base.count()
        completed = base.filter_by(status="handed_over").count()
        pending = base.filter_by(status="pending").count()
        srows = dict(db.session.query(
            models.DonationOffer.status, func.count(models.DonationOffer.id)
        ).filter(models.DonationOffer.donor_id == viewer.id,
                 models.DonationOffer.created_at >= from_day,
                 models.DonationOffer.created_at < to_day
                 ).group_by(models.DonationOffer.status).all())
        daily = _daily_offer_map(
            from_day, to_day,
            extra=(models.DonationOffer.donor_id == viewer.id,))
        return {"kind": "donor", "total_offers": total,
                "completed": completed, "pending": pending,
                "completion_rate": round(completed / total * 100, 1) if total else 0.0,
                "by_status": [{"status": s, "count": int(srows.get(s, 0))}
                              for s in OFFER_STATUSES],
                "trend": [{"bucket": b, "label": l,
                           "offers": sum(daily.get(d, (0, 0))[0]
                                         for d in days)}
                          for b, l, days in buckets]}
    if viewer.role == "ngo":
        req_statuses = ("open", "fulfilled", "closed")
        base = models.NGORequirement.query.filter(
            models.NGORequirement.ngo_id == viewer.id,
            models.NGORequirement.created_at >= from_day,
            models.NGORequirement.created_at < to_day)
        total = base.count()
        rrows = dict(db.session.query(
            models.NGORequirement.status, func.count(models.NGORequirement.id)
        ).filter(models.NGORequirement.ngo_id == viewer.id,
                 models.NGORequirement.created_at >= from_day,
                 models.NGORequirement.created_at < to_day
                 ).group_by(models.NGORequirement.status).all())
        linked = db.session.query(
            func.count(models.DonationTracking.id)
        ).join(models.NGORequirement, models.DonationTracking.requirement_id == models.NGORequirement.id
               ).filter(models.NGORequirement.ngo_id == viewer.id,
                        models.DonationTracking.acceptance_status == "accepted"
                        ).scalar() or 0
        return {"kind": "ngo", "total_requirements": total,
                "accepted_links": int(linked),
                "by_status": [{"status": s, "count": int(rrows.get(s, 0))}
                              for s in req_statuses]}
    return {"kind": viewer.role}


@app.get("/notifications")
@login_required
def notifications():
    show = (request.args.get("show") or "all").strip()
    if show not in {"all", "unread", "read"}:
        show = "all"
    query = models.Notification.query.filter_by(
        recipient_id=current_user.id).order_by(
        models.Notification.created_at.desc(),
        models.Notification.id.desc())
    if show == "unread":
        query = query.filter_by(is_read=False)
    elif show == "read":
        query = query.filter_by(is_read=True)
    items = query.limit(200).all()
    counts = {
        "all": models.Notification.query.filter_by(
            recipient_id=current_user.id).count(),
        "unread": models.Notification.query.filter_by(
            recipient_id=current_user.id, is_read=False).count(),
    }
    counts["read"] = counts["all"] - counts["unread"]
    return render_template("notifications.html", items=items, show=show,
                           counts=counts, titles=NOTIF_TITLES)


@app.post("/notifications/<int:notif_id>/read")
@login_required
def notification_read(notif_id):
    note = db.session.get(models.Notification, notif_id)
    # Unknown ids and other users' rows look identical: not found.
    if note is None or note.recipient_id != current_user.id:
        flash("Notification not found.", "error")
        return redirect(url_for("notifications")), 404
    if not note.is_read:
        note.is_read = True
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Mark-as-read failed")
            flash("Could not update the notification. Please try again.", "error")
            return redirect(url_for("notifications")), 500
    show = (request.form.get("show") or "all").strip()
    return redirect(url_for("notifications",
                            show=show if show in {"all", "unread", "read"} else "all"))


@app.post("/notifications/read-all")
@login_required
def notifications_read_all():
    try:
        models.Notification.query.filter_by(
            recipient_id=current_user.id, is_read=False).update(
            {"is_read": True}, synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Mark-all-read failed")
        flash("Could not update notifications. Please try again.", "error")
        return redirect(url_for("notifications")), 500
    flash("All notifications marked as read.", "success")
    return redirect(url_for("notifications"))


@app.get("/assistant")
def assistant():
    """ShareHope assistant — public page (as before), real answers.

    History lives in the viewer's own signed session (no tables);
    the AI key never leaves the server (see assistant_service).
    """
    history = assistant_service.get_history()
    return render_template("assistant.html", history=history,
                           ai_available=assistant_service.is_configured())


@app.post("/assistant/ask")
def assistant_ask():
    """Answer one chat turn. Fetch clients get JSON; plain forms get
    a redirect (PRG) so the page works without JavaScript.

    Read-only by construction: the service has no database access and
    these routes never write records — only the session history.
    """
    cleaned, error = assistant_service.validate_prompt(
        request.form.get("message") or request.form.get("prompt"))
    wants_json = ("application/json" in
                  (request.headers.get("Accept") or "")) or (
        request.headers.get("X-Requested-With") == "fetch")
    if error:
        if wants_json:
            return {"ok": False, "error": error}, 400
        flash(error, "error")
        return redirect(url_for("assistant")), 400
    allowed, _retry = assistant_service.check_rate_limit()
    if not allowed:
        msg = ("Too many questions in a short time — "
               "please wait a little and try again.")
        if wants_json:
            return {"ok": False, "error": msg}, 429
        flash(msg, "error")
        return redirect(url_for("assistant")), 429
    try:
        role = (current_user.role
                if current_user.is_authenticated else "visitor")
    except Exception:
        role = "visitor"
    reply, meta = assistant_service.ask(cleaned, role)
    assistant_service.push_history("user", cleaned)
    assistant_service.push_history("assistant", reply)
    if wants_json:
        return {"ok": True, "reply": reply,
                "fallback": bool(meta.get("fallback"))}
    return redirect(url_for("assistant"))


@app.post("/assistant/clear")
def assistant_clear():
    """Start a new chat (drops this browser's session history)."""
    assistant_service.clear_history()
    if ("application/json" in (request.headers.get("Accept") or "")) or (
            request.headers.get("X-Requested-With") == "fetch"):
        return {"ok": True}
    flash("Started a new chat.", "info")
    return redirect(url_for("assistant"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    """Own account only — there is no per-user profile URL by design.

    GET renders name/email/role plus role-specific fields and a small
    activity summary. POST updates an explicit allowlist (name, city,
    and organisation fields for NGOs only); ``email`` and ``role``
    keys are always ignored so roles can never be self-changed.
    One atomic commit per request; empty city clears the field, other
    fields are never blanked unintentionally (validated first).
    """
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        city = (request.form.get("city") or "").strip()
        if len(name) < 2 or len(name) > 120:
            flash("Display name must be 2–120 characters.", "error")
            return render_template("profile.html",
                                   stats=_profile_stats(current_user)), 400
        if len(city) > 160:
            flash("City must be under 160 characters.", "error")
            return render_template("profile.html",
                                   stats=_profile_stats(current_user)), 400
        org_name = org_reg = None
        if current_user.role == "ngo":
            org_name = (request.form.get("org_name")
                        or request.form.get("org") or "").strip()
            org_reg = (request.form.get("org_reg_id")
                       or request.form.get("regid") or "").strip()
            if len(org_name) < 2 or len(org_name) > 160:
                flash("Organisation name must be 2–160 characters.", "error")
                return render_template("profile.html",
                                       stats=_profile_stats(current_user)), 400
            if len(org_reg) < 3 or len(org_reg) > 80:
                flash("Registration ID must be 3–80 characters.", "error")
                return render_template("profile.html",
                                       stats=_profile_stats(current_user)), 400
        # NOTE: email/role are never read from the form (mass-assignment
        # protection); donors' org_* keys are likewise ignored.
        current_user.name = name
        current_user.city = city or None
        if current_user.role == "ngo":
            current_user.org_name = org_name
            current_user.org_reg_id = org_reg
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Profile update failed")
            flash("Could not save changes. Please try again.", "error")
            return render_template("profile.html",
                                   stats=_profile_stats(current_user)), 500
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))
    return render_template("profile.html", stats=_profile_stats(current_user))


@app.post("/profile/password")
@login_required
def profile_password():
    """Secure password change: current password required, hashed via
    the existing ``User.set_password`` (Werkzeug) method. Plaintext
    is never stored or displayed. Success keeps the session (same
    user id) so the user is not logged out."""
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or request.form.get("confirm") or ""
    if not current_user.check_password(current):
        flash("Current password is incorrect.", "error")
        return render_template("profile.html",
                               stats=_profile_stats(current_user)), 400
    if len(new) < 8 or len(new) > 256:
        flash("New password must be 8–256 characters.", "error")
        return render_template("profile.html",
                               stats=_profile_stats(current_user)), 400
    if new != confirm:
        flash("New passwords do not match.", "error")
        return render_template("profile.html",
                               stats=_profile_stats(current_user)), 400
    try:
        current_user.set_password(new)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Password change failed")
        flash("Could not change the password. Please try again.", "error")
        return render_template("profile.html",
                               stats=_profile_stats(current_user)), 500
    flash("Password changed.", "success")
    return redirect(url_for("profile"))


def _profile_stats(user):
    """Small role-scoped activity summary for the profile page.

    Counts only; failures degrade to zeros (never a 500 on GET).
    """
    stats = {"offers": 0, "handed": 0, "reqs": 0, "open_reqs": 0}
    try:
        if user.role == "donor":
            stats["offers"] = models.DonationOffer.query.filter_by(
                donor_id=user.id).count()
            stats["handed"] = models.DonationOffer.query.filter_by(
                donor_id=user.id, status="handed_over").count()
        elif user.role == "ngo":
            stats["reqs"] = models.NGORequirement.query.filter_by(
                ngo_id=user.id).count()
            stats["open_reqs"] = models.NGORequirement.query.filter_by(
                ngo_id=user.id, status="open").count()
    except Exception:
        app.logger.exception("Profile stats failed")
    return stats


# ---------- Donation workflow helpers ----------

def _log_history(offer_id, requirement_id, old_status, new_status, user_id):
    db.session.add(models.DonationHistory(
        offer_id=offer_id, requirement_id=requirement_id,
        old_status=old_status, new_status=new_status,
        changed_by=user_id,
    ))


NOTIF_TITLES = {
    "accept": "Offer accepted",
    "need": "New match on the ledger",
    "info": "Ledger update",
    "reminder": "Reminder",
    "handover": "Handover update",
}


def _notify(recipient_id, ntype, content):
    """Queue one notification inside the caller's transaction.

    Skips exact unread duplicates so retries and double submits never
    fan out twice. Nothing is committed here — the caller commits.
    Returns True when a new row was queued, False when skipped/dup.
    """
    if not recipient_id:
        return False
    content = (content or "").strip()[:500]
    if not content:
        return False
    dup = models.Notification.query.filter_by(
        recipient_id=recipient_id, type=ntype,
        content=content, is_read=False).first()
    if dup is None:
        db.session.add(models.Notification(
            recipient_id=recipient_id, type=ntype, content=content))
        return True
    return False


def _email_donation_update(user_id, title, status_line, detail=""):
    """Best-effort donation email AFTER a successful commit.

    Never raises and never affects the already-committed transaction.
    Called only when a new notification was queued, so retries and
    double submits cannot produce duplicate emails.
    """
    if not user_id:
        return
    try:
        user = db.session.get(models.User, user_id)
        if user is None:
            return
        email_service.send_donation_status_email(
            user, title, status_line, detail)
    except Exception:
        app.logger.exception("Donation status email failed")


def _ngo_ids_matching_offer(offer, limit=50):
    """NGO user ids with an open requirement matching this offer."""
    ids = []
    reqs = models.NGORequirement.query.filter_by(status="open").all()
    for req in reqs:
        if req.ngo_id and _offer_matches(offer, req):
            if req.ngo_id not in ids:
                ids.append(req.ngo_id)
            if len(ids) >= limit:
                break
    return ids


def _donor_ids_matching_requirement(req, limit=50):
    """Donor user ids with a past offer in this category and a nearby city."""
    ids = []
    offers = models.DonationOffer.query.filter_by(category=req.category).all()
    for offer in offers:
        donor = offer.donor
        if donor is None or donor.role != "donor":
            continue
        if not _locations_match(donor.city, req.location):
            continue
        if donor.id not in ids:
            ids.append(donor.id)
        if len(ids) >= limit:
            break
    return ids


def _loc_tokens(value):
    return set(re.findall(r"[a-z0-9]+", (value or "").lower()))


def _locations_match(a, b):
    ta, tb = _loc_tokens(a), _loc_tokens(b)
    if not ta or not tb:
        return True  # a missing place never blocks a match
    return bool(ta & tb)


def _offer_matches(offer, req):
    return offer.category == req.category and _locations_match(
        offer.location, req.location
    )


def _req_org(req):
    if req is None:
        return None
    ngo = req.ngo
    if ngo is None:
        return None
    return ngo.org_name or ngo.name


def _fmt_date(value):
    return value.strftime("%d %b %Y") if value else "—"


def _validate_offer(form):
    title = (form.get("title") or "").strip()
    category = (form.get("category") or "").strip()
    description = (form.get("description") or "").strip()
    quantity = (form.get("quantity") or "").strip()
    location = (form.get("location") or "").strip()
    availability = (form.get("availability") or "").strip() or "Flexible"
    if len(title) < 3:
        return None, "Give your offer a title of at least 3 characters."
    if category not in CAT_SLUGS:
        return None, "Choose a valid category."
    if len(description) < 10:
        return None, "Describe the items (at least 10 characters, including condition)."
    if not quantity:
        return None, "Tell NGOs the quantity (e.g. “40 books”, “25 kg”)."
    if len(location) < 2:
        return None, "Add a pickup location so NGOs can plan."
    return {
        "title": title, "category": category, "description": description,
        "quantity": quantity, "location": location,
        "availability": availability[:160],
    }, None


def _validate_requirement(form):
    title = (form.get("title") or "").strip()
    category = (form.get("category") or "").strip()
    description = (form.get("description") or "").strip()
    quantity = (form.get("required_quantity") or "").strip()
    location = (form.get("location") or "").strip()
    urgency = (form.get("urgency") or "open").strip()
    deadline_raw = (form.get("deadline") or "").strip()
    if len(title) < 3:
        return None, "Give your requirement a title of at least 3 characters."
    if category not in CAT_SLUGS:
        return None, "Choose a valid category."
    if len(description) < 10:
        return None, "Describe what you need (at least 10 characters)."
    if not quantity:
        return None, "Tell donors the required quantity."
    if len(location) < 2:
        return None, "Add the location where goods are needed."
    if urgency not in {"urgent", "soon", "open"}:
        return None, "Choose a valid urgency."
    deadline = None
    if deadline_raw:
        try:
            deadline = datetime.strptime(deadline_raw, "%Y-%m-%d").date()
        except ValueError:
            return None, "Deadline must be a valid date."
    return {
        "title": title, "category": category, "description": description,
        "required_quantity": quantity, "location": location,
        "urgency": urgency, "deadline": deadline,
    }, None


def _offer_progress(offer):
    """Derive stepper stage 0–4, a note and real timestamps for an offer."""
    tracks = sorted(offer.tracking_links, key=lambda t: t.id or 0)
    accepted = [t for t in tracks if t.acceptance_status == "accepted"]
    pending = [t for t in tracks if t.acceptance_status == "pending"]
    declined = [t for t in tracks if t.acceptance_status == "declined"]
    ts = {"posted": offer.created_at}
    if offer.status == "handed_over":
        done = models.DonationHistory.query.filter_by(
            offer_id=offer.id, new_status="handed_over"
        ).order_by(models.DonationHistory.id.desc()).first()
        if accepted:
            t = accepted[-1]
            ts.update(matched=t.created_at, accepted=t.accepted_at,
                      handover=t.handed_over_at)
        ts["completed"] = done.created_at if done else offer.updated_at
        return {"stage": 4, "note": "Completed and counted.", "ts": ts,
                "track": accepted[-1] if accepted else None}
    if accepted:
        t = accepted[-1]
        ts.update(matched=t.created_at, accepted=t.accepted_at,
                  handover=t.handed_over_at)
        if t.handover_status == "handed_over":
            return {"stage": 3, "note": "Handed over — awaiting NGO receipt confirmation.",
                    "ts": ts, "track": t}
        return {"stage": 2, "note": "Accepted — arrange the handover.",
                "ts": ts, "track": t}
    if pending:
        t = pending[-1]
        ts["matched"] = t.created_at
        org = _req_org(t.requirement)
        return {"stage": 1,
                "note": f"Matched with {org} — awaiting their decision." if org else "Matched — awaiting decision.",
                "ts": ts, "track": t}
    if declined:
        t = declined[-1]
        org = _req_org(t.requirement)
        return {"stage": 0,
                "note": f"Declined by {org} — edit and resubmit to try again." if org else "Declined — edit and resubmit to try again.",
                "ts": ts, "track": None}
    if offer.status == "declined":
        return {"stage": 0, "note": "Withdrawn by you — edit to put it back on the ledger.",
                "ts": ts, "track": None}
    return {"stage": 0, "note": "Posted — visible to matching NGOs.",
            "ts": ts, "track": None}


# ---------- Featured Home slip (admin-controlled) ----------

# Withdrawn/declined offers are not featureable: the Home slip should
# only ever show a real, meaningful record.
FEATUREABLE_STATUSES = ("pending", "accepted", "handed_over")

_SLIP_STATUS_LABEL = {
    "pending": "Posted",
    "accepted": "Accepted",
    "handed_over": "Completed",
}


def _featureable_offers(limit=200):
    """Existing offers an admin may feature, newest activity first.

    Only real records from ``donation_offers`` — nothing is invented.
    """
    return models.DonationOffer.query.filter(
        models.DonationOffer.status.in_(FEATUREABLE_STATUSES)
    ).order_by(
        models.DonationOffer.updated_at.desc(),
        models.DonationOffer.id.desc(),
    ).limit(limit).all()


def _current_featureed_offer():
    """The offer an admin featured, or None when none/unavailable.

    Defensive: a deleted or withdrawn offer falls back to None so the
    Home page shows its empty state instead of a stale record.
    """
    try:
        row = models.FeaturedSlip.query.first()
    except Exception:
        app.logger.exception("Featured slip lookup failed")
        return None
    if row is None or row.offer_id is None:
        return None
    offer = row.offer
    if offer is None or offer.status not in FEATUREABLE_STATUSES:
        return None
    return offer


def _linked_requirement(offer):
    """The requirement an offer is matched to (accepted link wins)."""
    links = sorted(offer.tracking_links, key=lambda t: t.id or 0)
    for t in links:
        if t.acceptance_status == "accepted" and t.requirement is not None:
            return t.requirement
    for t in links:
        if t.requirement is not None:
            return t.requirement
    return None


def _home_slip(offer):
    """Display data for the Home slip. Never exposes internal ids."""
    if offer is None:
        return None
    req = _linked_requirement(offer)
    donor = offer.donor
    return {
        "title": offer.title,
        "offer": " · ".join(
            p for p in (offer.quantity, offer.location) if p) or "Details on request",
        "requirement": (" · ".join(
            p for p in (_req_org(req), req.required_quantity) if p)
            if req is not None else "Awaiting NGO match"),
        "category": " · ".join(
            p for p in (CAT_NAMES.get(offer.category, offer.category),
                        offer.availability) if p),
        "status": _SLIP_STATUS_LABEL.get(offer.status,
                                         (offer.status or "").replace("_", " ").title()),
        "stamp": _SLIP_STATUS_LABEL.get(offer.status, "Posted"),
        "donor": donor.name if donor is not None else "A ShareHope donor",
        "partner": (_req_org(req) or "Awaiting NGO match"),
    }


def _set_featured_slip(offer_id, user_id):
    """Point the single FeaturedSlip row at ``offer_id`` (or clear it)."""
    row = models.FeaturedSlip.query.first()
    if row is None:
        row = models.FeaturedSlip(id=1)
        db.session.add(row)
    row.offer_id = offer_id or None
    row.updated_by = user_id or None
    row.updated_at = datetime.utcnow()
    db.session.add(row)
    return row


# ---------- Donor: donation offers ----------

@app.route("/offers/new", methods=["GET", "POST"])
@login_required
def offer_new():
    if current_user.role != "donor":
        flash("Only donor accounts can post donation offers.", "error")
        return redirect(url_for(_dashboard_for(current_user.role)))
    if request.method == "POST":
        cleaned, error = _validate_offer(request.form)
        if error:
            flash(error, "error")
            return render_template("offer_form.html", form=request.form), 400
        offer = models.DonationOffer(donor_id=current_user.id, status="pending", **cleaned)
        db.session.add(offer)
        try:
            db.session.flush()
            _log_history(offer.id, None, None, "pending", current_user.id)
            for ngo_id in _ngo_ids_matching_offer(offer):
                _notify(ngo_id, "need",
                        f"New matching offer: “{offer.title}” "
                        f"({CAT_NAMES.get(offer.category, offer.category)}, "
                        f"{offer.location or '—'}).")
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Offer creation failed")
            flash("Could not post the offer. Please try again.", "error")
            return render_template("offer_form.html", form=request.form), 500
        flash("Offer posted — matching NGOs can now see it.", "success")
        return redirect(url_for("donor"))
    return render_template("offer_form.html", form={})


@app.route("/offers/<int:offer_id>/edit", methods=["GET", "POST"])
@login_required
def offer_edit(offer_id):
    offer = db.session.get(models.DonationOffer, offer_id)
    if offer is None or offer.donor_id != current_user.id or current_user.role != "donor":
        flash("You can only edit your own donation offers.", "error")
        return redirect(url_for(_dashboard_for(current_user.role)) if current_user.is_authenticated else url_for("login"))
    if offer.status not in OFFER_EDITABLE:
        flash("Only pending or withdrawn offers can be edited.", "error")
        return redirect(url_for("donor"))
    if request.method == "POST":
        cleaned, error = _validate_offer(request.form)
        if error:
            flash(error, "error")
            return render_template("offer_form.html", form=request.form, offer=offer), 400
        old_status = offer.status
        for key, value in cleaned.items():
            setattr(offer, key, value)
        if old_status == "declined":
            offer.status = "pending"
            _log_history(offer.id, None, "declined", "pending", current_user.id)
            flash("Offer updated and put back on the ledger.", "success")
        else:
            flash("Offer updated.", "success")
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Offer update failed")
            flash("Could not save changes. Please try again.", "error")
            return render_template("offer_form.html", form=request.form, offer=offer), 500
        return redirect(url_for("donor"))
    return render_template("offer_form.html", form=offer, offer=offer)


@app.post("/offers/<int:offer_id>/cancel")
@login_required
def offer_cancel(offer_id):
    offer = db.session.get(models.DonationOffer, offer_id)
    if offer is None or offer.donor_id != current_user.id or current_user.role != "donor":
        flash("You can only withdraw your own donation offers.", "error")
        return redirect(url_for("donor"))
    if offer.status != "pending":
        flash("Only pending offers can be withdrawn.", "error")
        return redirect(url_for("donor"))
    try:
        offer.status = "declined"
        _log_history(offer.id, None, "pending", "declined", current_user.id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Offer withdrawal failed")
        flash("Could not withdraw the offer. Please try again.", "error")
        return redirect(url_for("donor")), 500
    flash("Offer withdrawn from the ledger.", "info")
    return redirect(url_for("donor"))


# ---------- NGO: requirements ----------

@app.route("/requirements/new", methods=["GET", "POST"])
@login_required
def requirement_new():
    if current_user.role != "ngo":
        flash("Only NGO accounts can publish requirements.", "error")
        return redirect(url_for(_dashboard_for(current_user.role)))
    if request.method == "POST":
        cleaned, error = _validate_requirement(request.form)
        if error:
            flash(error, "error")
            return render_template("requirement_form.html", form=request.form), 400
        req = models.NGORequirement(ngo_id=current_user.id, status="open", **cleaned)
        db.session.add(req)
        try:
            db.session.flush()
            for donor_id in _donor_ids_matching_requirement(req):
                _notify(donor_id, "need",
                        f"New need matches your giving: “{req.title}” "
                        f"({CAT_NAMES.get(req.category, req.category)}, "
                        f"{req.location or '—'}).")
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Requirement creation failed")
            flash("Could not publish the requirement. Please try again.", "error")
            return render_template("requirement_form.html", form=request.form), 500
        flash("Requirement published — matching offers are listed in your inbox.", "success")
        return redirect(url_for("ngo"))
    return render_template("requirement_form.html", form={})


@app.route("/requirements/<int:req_id>/edit", methods=["GET", "POST"])
@login_required
def requirement_edit(req_id):
    req = db.session.get(models.NGORequirement, req_id)
    if req is None or req.ngo_id != current_user.id or current_user.role != "ngo":
        flash("You can only edit your own requirements.", "error")
        return redirect(url_for(_dashboard_for(current_user.role)) if current_user.is_authenticated else url_for("login"))
    if req.status != "open":
        flash("Only open requirements can be edited — reopen it first.", "error")
        return redirect(url_for("ngo"))
    if request.method == "POST":
        cleaned, error = _validate_requirement(request.form)
        if error:
            flash(error, "error")
            return render_template("requirement_form.html", form=request.form, req=req), 400
        for key, value in cleaned.items():
            setattr(req, key, value)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Requirement update failed")
            flash("Could not save changes. Please try again.", "error")
            return render_template("requirement_form.html", form=request.form, req=req), 500
        flash("Requirement updated.", "success")
        return redirect(url_for("ngo"))
    return render_template("requirement_form.html", form=req, req=req)


@app.post("/requirements/<int:req_id>/status")
@login_required
def requirement_status(req_id):
    req = db.session.get(models.NGORequirement, req_id)
    if req is None or req.ngo_id != current_user.id or current_user.role != "ngo":
        flash("You can only manage your own requirements.", "error")
        return redirect(url_for("ngo"))
    target = (request.form.get("to") or "").strip()
    if target not in REQ_TRANSITIONS.get(req.status, set()):
        flash(f"Cannot move a {req.status} requirement to “{target or '—'}”.", "error")
        return redirect(url_for("ngo")), 400
    try:
        req.status = target
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Requirement status change failed")
        flash("Could not update the requirement. Please try again.", "error")
        return redirect(url_for("ngo")), 500
    flash(f"Requirement marked {target}.", "success")
    return redirect(url_for("ngo"))


# ---------- NGO: match / accept / decline ----------

def _ngo_open_requirement(req_id):
    req = db.session.get(models.NGORequirement, req_id)
    if req is None or req.ngo_id != current_user.id or current_user.role != "ngo":
        return None
    return req if req.status == "open" else None


@app.post("/offers/<int:offer_id>/link")
@login_required
def offer_link(offer_id):
    """Link a pending offer to one of the NGO's open requirements (Matched)."""
    if current_user.role != "ngo":
        flash("Only NGOs can link offers to requirements.", "error")
        return redirect(url_for(_dashboard_for(current_user.role)))
    offer = db.session.get(models.DonationOffer, offer_id)
    req = _ngo_open_requirement((request.form.get("requirement_id") or "").strip())
    if offer is None or offer.status != "pending" or req is None:
        flash("That offer can no longer be linked.", "error")
        return redirect(url_for("ngo")), 400
    existing = models.DonationTracking.query.filter_by(
        offer_id=offer.id, requirement_id=req.id).first()
    if existing:
        flash("Already linked to that requirement.", "info")
        return redirect(url_for("ngo"))
    try:
        db.session.add(models.DonationTracking(
            offer_id=offer.id, requirement_id=req.id,
            acceptance_status="pending", handover_status="pending"))
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Offer linking failed")
        flash("Could not link the offer. Please try again.", "error")
        return redirect(url_for("ngo")), 500
    flash(f"Matched with “{req.title}” — accept or decline it next.", "success")
    return redirect(url_for("ngo"))


@app.post("/tracking/<int:track_id>/accept")
@login_required
def tracking_accept(track_id):
    track = db.session.get(models.DonationTracking, track_id)
    if (track is None or track.requirement is None
            or track.requirement.ngo_id != current_user.id
            or current_user.role != "ngo"):
        flash("You can only accept offers linked to your requirements.", "error")
        return redirect(url_for("ngo")), 403
    if track.acceptance_status == "accepted":
        flash("Already accepted.", "info")
        return redirect(url_for("ngo"))
    if track.offer.status != "pending" or track.acceptance_status != "pending":
        flash("That offer can no longer be accepted.", "error")
        return redirect(url_for("ngo")), 400
    try:
        track.acceptance_status = "accepted"
        track.accepted_at = datetime.utcnow()
        track.offer.status = "accepted"
        _log_history(track.offer_id, track.requirement_id, "pending", "accepted", current_user.id)
        new_notif = _notify(track.offer.donor_id, "accept",
                f"“{_req_org(track.requirement) or 'An NGO'}” accepted your offer "
                f"“{track.offer.title}”. Arrange the handover.")
        donor_id, offer_title = track.offer.donor_id, track.offer.title
        org_name = _req_org(track.requirement)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Offer acceptance failed")
        flash("Could not accept the offer. Please try again.", "error")
        return redirect(url_for("ngo")), 500
    if new_notif:
        _email_donation_update(
            donor_id, "Offer accepted",
            f"“{org_name or 'An NGO'}” accepted your offer “{offer_title}”. Arrange the handover.",
            "Open ShareHope → Tracking to coordinate pickup.")
    flash("Offer accepted — arrange the handover with the donor.", "success")
    return redirect(url_for("ngo"))


@app.post("/tracking/<int:track_id>/decline")
@login_required
def tracking_decline(track_id):
    track = db.session.get(models.DonationTracking, track_id)
    if (track is None or track.requirement is None
            or track.requirement.ngo_id != current_user.id
            or current_user.role != "ngo"):
        flash("You can only decline offers linked to your requirements.", "error")
        return redirect(url_for("ngo")), 403
    if track.acceptance_status == "declined":
        flash("Already declined.", "info")
        return redirect(url_for("ngo"))
    if track.acceptance_status == "accepted" or track.offer.status != "pending":
        flash("An accepted offer cannot be declined — complete the handover instead.", "error")
        return redirect(url_for("ngo")), 400
    try:
        track.acceptance_status = "declined"
        _log_history(track.offer_id, track.requirement_id, "pending", "declined", current_user.id)
        new_notif = _notify(track.offer.donor_id, "info",
                f"“{_req_org(track.requirement) or 'An NGO'}” declined your offer "
                f"“{track.offer.title}”. It stays visible to other NGOs.")
        donor_id, offer_title = track.offer.donor_id, track.offer.title
        org_name = _req_org(track.requirement)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Offer decline failed")
        flash("Could not decline the offer. Please try again.", "error")
        return redirect(url_for("ngo")), 500
    if new_notif:
        _email_donation_update(
            donor_id, "Offer declined",
            f"“{org_name or 'An NGO'}” declined your offer “{offer_title}”. It stays visible to other NGOs.")
    flash("Offer declined — it stays visible to other NGOs.", "info")
    return redirect(url_for("ngo"))


@app.post("/offers/<int:offer_id>/decide")
@login_required
def offer_decide(offer_id):
    """Accept or decline a pool offer directly for one open requirement."""
    if current_user.role != "ngo":
        flash("Only NGOs can decide on offers.", "error")
        return redirect(url_for(_dashboard_for(current_user.role)))
    offer = db.session.get(models.DonationOffer, offer_id)
    req = _ngo_open_requirement((request.form.get("requirement_id") or "").strip())
    action = (request.form.get("action") or "").strip()
    if offer is None or offer.status != "pending" or req is None or action not in {"accept", "decline"}:
        flash("That decision is no longer possible.", "error")
        return redirect(url_for("ngo")), 400
    track = models.DonationTracking.query.filter_by(
        offer_id=offer.id, requirement_id=req.id).first()
    if track is not None and track.acceptance_status != "pending":
        flash("Already decided for that requirement.", "info")
        return redirect(url_for("ngo"))
    try:
        if track is None:
            track = models.DonationTracking(
                offer_id=offer.id, requirement_id=req.id,
                acceptance_status="pending", handover_status="pending")
            db.session.add(track)
            db.session.flush()
        if action == "accept":
            if models.DonationTracking.query.filter(
                    models.DonationTracking.offer_id == offer.id,
                    models.DonationTracking.acceptance_status == "accepted").first() is not None:
                db.session.rollback()
                flash("Another requirement already accepted this offer.", "error")
                return redirect(url_for("ngo")), 400
            track.acceptance_status = "accepted"
            track.accepted_at = datetime.utcnow()
            offer.status = "accepted"
            _log_history(offer.id, req.id, "pending", "accepted", current_user.id)
            new_notif = _notify(offer.donor_id, "accept",
                    f"“{_req_org(req) or 'An NGO'}” accepted your offer "
                    f"“{offer.title}”. Arrange the handover.")
            msg = "Offer accepted — arrange the handover with the donor."
        else:
            track.acceptance_status = "declined"
            _log_history(offer.id, req.id, "pending", "declined", current_user.id)
            new_notif = _notify(offer.donor_id, "info",
                    f"“{_req_org(req) or 'An NGO'}” declined your offer "
                    f"“{offer.title}”. It stays visible to other NGOs.")
            msg = "Offer declined — it stays visible to other NGOs."
        donor_id, offer_title, org_name, act = (
            offer.donor_id, offer.title, _req_org(req), action)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Offer decision failed")
        flash("Could not record the decision. Please try again.", "error")
        return redirect(url_for("ngo")), 500
    if new_notif:
        if act == "accept":
            _email_donation_update(
                donor_id, "Offer accepted",
                f"“{org_name or 'An NGO'}” accepted your offer “{offer_title}”. Arrange the handover.")
        else:
            _email_donation_update(
                donor_id, "Offer declined",
                f"“{org_name or 'An NGO'}” declined your offer “{offer_title}”. It stays visible to other NGOs.")
    flash(msg, "success" if action == "accept" else "info")
    return redirect(url_for("ngo"))


# ---------- Handover: donor marks, NGO confirms ----------

@app.post("/tracking/<int:track_id>/handover")
@login_required
def tracking_handover(track_id):
    track = db.session.get(models.DonationTracking, track_id)
    if (track is None or track.offer.donor_id != current_user.id
            or current_user.role != "donor"):
        flash("You can only mark handover for your own donations.", "error")
        return redirect(url_for("donor")), 403
    if track.acceptance_status != "accepted" or track.offer.status != "accepted":
        flash("Only accepted donations can be handed over.", "error")
        return redirect(url_for("donor")), 400
    if track.handover_status == "handed_over":
        flash("Handover already marked — waiting for NGO confirmation.", "info")
        return redirect(url_for("donor"))
    try:
        track.handover_status = "handed_over"
        track.handed_over_at = datetime.utcnow()
        new_notif = False
        ngo_id, offer_title, donor_name = None, track.offer.title, None
        try:
            donor_name = track.offer.donor.name if track.offer.donor else "The donor"
        except Exception:
            donor_name = "The donor"
        if track.requirement is not None:
            new_notif = _notify(track.requirement.ngo_id, "handover",
                    f"“{donor_name}” "
                    f"marked handover for “{track.offer.title}”. "
                    f"Confirm receipt to complete it.")
            ngo_id = track.requirement.ngo_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Handover marking failed")
        flash("Could not mark the handover. Please try again.", "error")
        return redirect(url_for("donor")), 500
    if new_notif:
        _email_donation_update(
            ngo_id, "Handover marked",
            f"“{donor_name}” marked handover for “{offer_title}”. Confirm receipt to complete it.")
    flash("Handover marked — the NGO confirms receipt to complete it.", "success")
    return redirect(url_for("donor"))


@app.post("/tracking/<int:track_id>/complete")
@login_required
def tracking_complete(track_id):
    track = db.session.get(models.DonationTracking, track_id)
    if (track is None or track.requirement is None
            or track.requirement.ngo_id != current_user.id
            or current_user.role != "ngo"):
        flash("You can only complete handovers for your requirements.", "error")
        return redirect(url_for("ngo")), 403
    if track.acceptance_status != "accepted" or track.handover_status != "handed_over" \
            or track.offer.status != "accepted":
        flash("Receipt can only be confirmed after the donor marks handover.", "error")
        return redirect(url_for("ngo")), 400
    try:
        track.offer.status = "handed_over"
        _log_history(track.offer_id, track.requirement_id, "accepted", "handed_over", current_user.id)
        new_donor = _notify(track.offer.donor_id, "handover",
                f"Handover completed for “{track.offer.title}”. "
                f"Counted in the impact ledger.")
        new_ngo = False
        if track.requirement is not None:
            new_ngo = _notify(track.requirement.ngo_id, "handover",
                    f"Handover completed for “{track.offer.title}”. "
                    f"Counted in the impact ledger.")
        donor_id = track.offer.donor_id
        ngo_id = track.requirement.ngo_id if track.requirement is not None else None
        offer_title = track.offer.title
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Handover completion failed")
        flash("Could not complete the handover. Please try again.", "error")
        return redirect(url_for("ngo")), 500
    if new_donor:
        _email_donation_update(
            donor_id, "Handover completed",
            f"Handover completed for “{offer_title}”. Counted in the impact ledger.")
    if new_ngo:
        _email_donation_update(
            ngo_id, "Handover completed",
            f"Handover completed for “{offer_title}”. Counted in the impact ledger.")
    flash("Handover completed and counted in the impact ledger.", "success")
    return redirect(url_for("ngo"))


# ---------- Dashboards (role-based access) ----------

def _require_role(*roles):
    if current_user.role not in roles:
        flash(
            f"That desk is for {', '.join(roles)} — "
            f"you’re signed in as {current_user.role}.",
            "error",
        )
        return redirect(url_for(_dashboard_for(current_user.role)))
    return None


@app.get("/dashboard")
@login_required
def dashboard():
    return redirect(url_for(_dashboard_for(current_user.role)))


@app.get("/donor")
@login_required
def donor():
    gate = _require_role("donor", "admin")
    if gate:
        return gate
    offers = models.DonationOffer.query.filter_by(
        donor_id=current_user.id).order_by(
        models.DonationOffer.created_at.desc()).all()
    slips = []
    for offer in offers:
        prog = _offer_progress(offer)
        history = models.DonationHistory.query.filter_by(
            offer_id=offer.id).order_by(
            models.DonationHistory.id.desc()).all()
        slips.append({"offer": offer, "progress": prog, "history": history,
                      "category_name": CAT_NAMES.get(offer.category, offer.category)})
    activity = models.DonationHistory.query.filter(
        models.DonationHistory.offer_id.in_(
            [o.id for o in offers]) if offers else False).order_by(
        models.DonationHistory.id.desc()).limit(8).all()
    actor_ids = {h.changed_by for h in activity if h.changed_by}
    actors = {u.id: u.name for u in models.User.query.filter(
        models.User.id.in_(actor_ids)).all()} if actor_ids else {}
    handed = sum(1 for o in offers if o.status == "handed_over")
    return render_template("donor.html", slips=slips, activity=activity,
                           actors=actors, n_offers=len(offers), n_handed=handed)


@app.get("/ngo")
@login_required
def ngo():
    gate = _require_role("ngo", "admin")
    if gate:
        return gate
    reqs = models.NGORequirement.query.filter_by(
        ngo_id=current_user.id).order_by(
        models.NGORequirement.created_at.desc()).all()
    open_reqs = [r for r in reqs if r.status == "open"]
    pool = models.DonationOffer.query.filter_by(
        status="pending").order_by(
        models.DonationOffer.created_at.desc()).all()
    matched, other = [], []
    for offer in pool:
        hits = [r for r in open_reqs if _offer_matches(offer, req := r)]
        (matched if hits else other).append({"offer": offer, "reqs": hits,
            "donor": offer.donor,
            "category_name": CAT_NAMES.get(offer.category, offer.category)})
    linked = models.DonationTracking.query.join(
        models.NGORequirement,
        models.DonationTracking.requirement_id == models.NGORequirement.id
    ).filter(models.NGORequirement.ngo_id == current_user.id).order_by(
        models.DonationTracking.id.desc()).all()
    awaiting = [t for t in linked if t.acceptance_status == "pending"]
    # Accepted links split by the offer's ledger status so completed
    # handovers (offer handed_over) never masquerade as in-progress:
    # active = accepted but not yet handed over; completed = handed over.
    # No new statuses, no duplicate rows, persists after refresh.
    active = [t for t in linked
              if t.acceptance_status == "accepted"
              and (t.offer is None or t.offer.status != "handed_over")]
    completed = [t for t in linked
                 if t.acceptance_status == "accepted"
                 and t.offer is not None and t.offer.status == "handed_over"]
    return render_template("ngo.html", reqs=reqs, open_reqs=open_reqs,
                           matched=matched, other=other, awaiting=awaiting,
                           active=active, completed=completed)


@app.get("/tracking")
@login_required
def tracking():
    if current_user.role == "donor":
        offers = models.DonationOffer.query.filter_by(
            donor_id=current_user.id).order_by(
            models.DonationOffer.created_at.desc()).all()
        slips = []
        for offer in offers:
            prog = _offer_progress(offer)
            history = models.DonationHistory.query.filter_by(
                offer_id=offer.id).order_by(
                models.DonationHistory.id).all()
            slips.append({"title": offer.title, "id": offer.id,
                          "category": CAT_NAMES.get(offer.category, offer.category),
                          "progress": prog, "history": history})
        return render_template("tracking.html", slips=slips, db_pending=False)
    if current_user.role == "ngo":
        linked = models.DonationTracking.query.join(
            models.NGORequirement,
            models.DonationTracking.requirement_id == models.NGORequirement.id
        ).filter(models.NGORequirement.ngo_id == current_user.id).order_by(
            models.DonationTracking.id.desc()).all()
        slips = [{
            "title": t.offer.title if t.offer else "Removed offer",
            "id": t.offer_id,
            "category": CAT_NAMES.get(t.offer.category, t.offer.category) if t.offer else "—",
            "progress": _offer_progress(t.offer) if t.offer else {"stage": 0, "note": "Offer removed.", "ts": {}},
            "history": models.DonationHistory.query.filter_by(
                offer_id=t.offer_id).order_by(models.DonationHistory.id).all(),
        } for t in linked]
        return render_template("tracking.html", slips=slips, db_pending=False)
    recent = models.DonationTracking.query.order_by(
        models.DonationTracking.id.desc()).limit(20).all()
    slips = [{
        "title": t.offer.title if t.offer else "Removed offer",
        "id": t.offer_id,
        "category": CAT_NAMES.get(t.offer.category, t.offer.category) if t.offer else "—",
        "progress": _offer_progress(t.offer) if t.offer else {"stage": 0, "note": "Offer removed.", "ts": {}},
        "history": [],
    } for t in recent]
    return render_template("tracking.html", slips=slips, db_pending=False)


@app.post("/admin/featured-slip")
@login_required
def admin_featured_slip():
    """Admin-only: choose, replace or clear the Home page slip.

    POST + the existing CSRF token, gated by ``_require_role("admin")``
    so donors and NGOs can never change it.
    """
    gate = _require_role("admin")
    if gate:
        return gate
    action = (request.form.get("action") or "").strip()
    try:
        if action == "clear":
            _set_featured_slip(None, current_user.id)
            db.session.commit()
            flash("Home slip removed — the page shows its empty state.",
                  "info")
            return redirect(url_for("admin"))
        offer_id = int((request.form.get("offer_id") or "").strip())
    except (TypeError, ValueError):
        flash("Choose a donation to feature.", "error")
        return redirect(url_for("admin")), 400
    offer = db.session.get(models.DonationOffer, offer_id)
    if offer is None or offer.status not in FEATUREABLE_STATUSES:
        flash("That donation cannot be featured.", "error")
        return redirect(url_for("admin")), 400
    try:
        _set_featured_slip(offer.id, current_user.id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("Featured slip update failed")
        flash("Could not update the Home slip. Please try again.", "error")
        return redirect(url_for("admin")), 500
    flash(f"“{offer.title}” now appears on the Home page.", "success")
    return redirect(url_for("admin"))


@app.get("/admin")
@login_required
def admin():
    gate = _require_role("admin")
    if gate:
        return gate
    counts = {
        "users": models.User.query.count(),
        "ngos": models.User.query.filter_by(role="ngo").count(),
        "pending_offers": models.DonationOffer.query.filter_by(status="pending").count(),
        "accepted": models.DonationTracking.query.filter_by(acceptance_status="accepted").count(),
        "handed_over": models.DonationOffer.query.filter_by(status="handed_over").count(),
        "open_reqs": models.NGORequirement.query.filter_by(status="open").count(),
    }
    users = models.User.query.order_by(models.User.created_at.desc()).limit(12).all()
    offers = models.DonationOffer.query.order_by(
        models.DonationOffer.created_at.desc()).limit(12).all()
    links = models.DonationTracking.query.order_by(
        models.DonationTracking.id.desc()).limit(12).all()
    featured_offer = _current_featureed_offer()
    return render_template("admin.html", counts=counts, users=users,
                           offers=offers, links=links,
                           featureable=_featureable_offers(),
                           featured_offer=featured_offer)


if __name__ == "__main__":
    app.run(debug=True, port=5000)