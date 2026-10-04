"""ShareHope assistant service — Google Gemini integration (server-side only).

No AI SDK dependency: REST via stdlib ``urllib`` so requirements stay
untouched. The API key lives exclusively in ``GEMINI_API_KEY`` (.env),
never in code, logs, or client payloads.

Behavior:
- Stateless provider calls: only the system instruction, the current
  session's own turns, and the viewer's role are sent. Never emails,
  passwords, credentials, or database records.
- Conversation context lives in the signed Flask session (no new
  tables). Capped_bytes so the cookie can never overflow; each
  browser's session is naturally isolated per user.
- No key → honest built-in guide (static ShareHope help, never
  invented listings/statuses/users).
- The assistant is read-only by construction: this module has no
  database access at all.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "You are the ShareHope assistant, a helper inside the ShareHope "
    "community ledger web app. ShareHope connects donors (people offering "
    "goods) with verified NGOs (which publish item requirements) across "
    "eight categories: Books & Education, Food & Groceries, Clothing, "
    "Electronics, Toys & Games, Home Essentials, Furniture, Shoes & "
    "Accessories.\n"
    "Key workflows: donors post donation offers (title, category, "
    "description, quantity, location); NGOs publish requirements and "
    "link/accept/decline offers; accepted donations move to handover "
    "(donor marks handed over, NGO confirms receipt) and completed "
    "handovers count in the impact ledger. Offer statuses: pending, "
    "accepted, handed_over, declined. Requirement statuses: open, "
    "fulfilled, closed. Users track progress on /tracking, coordinate "
    "in donor-NGO message threads on /messages, get ledger alerts on "
    "/notifications, view live counts on /analytics, and manage their "
    "account on /profile. Registration has donor/NGO doors; password "
    "reset is via email link.\n"
    "Rules: answer concisely about ShareHope only; explain navigation "
    "with real page paths above; never invent NGOs, offers, users, "
    "statistics, or features; never reveal personal data; if asked "
    "about anything outside ShareHope, say so briefly and redirect to "
    "how you can help with donations."
)

MAX_PROMPT = 2000
HISTORY_TURNS = 8
HISTORY_BYTES = 3000
RATE_WINDOW = 3600
DEFAULT_RATE_LIMIT = 30
DEFAULT_TIMEOUT = 20
DEFAULT_MODEL = "gemini-3.8-flash"


def is_configured():
    return bool((os.getenv("GEMINI_API_KEY") or "").strip())


def validate_prompt(raw):
    text = (raw or "").strip()
    if not text:
        return None, "Write a question before sending."
    if len(text) > MAX_PROMPT:
        return None, f"Keep questions under {MAX_PROMPT} characters."
    return text, None


def get_history():
    from flask import session
    items = session.get("assistant_history") or []
    clean = []
    for m in items:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            content = str(m.get("content") or "")[:1000]
            if content.strip():
                clean.append({"role": m["role"], "content": content})
    return clean[-HISTORY_TURNS:]


def push_history(role, content):
    from flask import session
    items = get_history()
    items.append({"role": role, "content": str(content)[:1000]})
    items = items[-HISTORY_TURNS:]
    total = sum(len(m["content"]) for m in items)
    while len(items) > 2 and total > HISTORY_BYTES:
        dropped = items.pop(0)
        total -= len(dropped["content"])
    session["assistant_history"] = items


def clear_history():
    from flask import session
    session.pop("assistant_history", None)
    session.pop("assistant_rl", None)


def check_rate_limit(limit=None):
    """Sliding-window session rate limit. Returns (allowed, retry_hint)."""
    from flask import current_app, session
    if limit is None:
        try:
            limit = int(current_app.config.get("ASSISTANT_RATE_LIMIT",
                                               DEFAULT_RATE_LIMIT))
        except (TypeError, ValueError):
            limit = DEFAULT_RATE_LIMIT
    now = time.time()
    stamps = [t for t in (session.get("assistant_rl") or [])
              if isinstance(t, (int, float)) and now - t < RATE_WINDOW]
    if len(stamps) >= limit:
        session["assistant_rl"] = stamps
        return False, int(RATE_WINDOW - (now - stamps[0])) + 1
    stamps.append(now)
    session["assistant_rl"] = stamps
    return True, 0


def ask(prompt, role="visitor"):
    """Answer one prompt. Returns (reply, meta).

    ``meta`` carries ``fallback`` (bool) and ``reason`` for the UI.
    Never raises; provider failures degrade to the built-in guide.
    """
    history = get_history()
    if not is_configured():
        return _offline_reply(prompt), {"fallback": True,
                                        "reason": "unconfigured"}
    try:
        reply = _call_gemini(prompt, history, role)
        return reply, {"fallback": False, "reason": "live"}
    except _RateLimited:
        return ("ShareHope is getting a lot of questions right now — "
                "please wait a minute and try again."), {
                    "fallback": True, "reason": "rate_limited"}
    except _ModelNotFound:
        log.error("Assistant model not found (404) — update GEMINI_MODEL "
                  "in .env to a current model.")
        return (_offline_reply(prompt), {"fallback": True,
                                         "reason": "model_not_found"})
    except _ServiceUnavailable:
        log.warning("Assistant provider unavailable (503)")
        return (_offline_reply(prompt), {"fallback": True,
                                         "reason": "unavailable"})
    except _TimedOut:
        log.warning("Assistant provider timeout")
        return (_offline_reply(prompt), {"fallback": True,
                                         "reason": "timeout"})
    except Exception as exc:
        log.warning("Assistant provider error (%s)", type(exc).__name__)
        return (_offline_reply(prompt), {"fallback": True,
                                         "reason": "error"})


class _RateLimited(Exception):
    pass


class _TimedOut(Exception):
    pass


class _ModelNotFound(Exception):
    """The configured model is retired/unavailable (HTTP 404).

    Distinct from transient errors: retrying will not help, and the
    fallback reason should say the model needs updating.
    """
    pass


class _ServiceUnavailable(Exception):
    """Transient provider failure (HTTP 503 / overload). Retryable."""
    pass


def _call_gemini(prompt, history, role="visitor"):
    """POST one chat turn to Gemini. Returns the reply text.

    Raises _RateLimited / _TimedOut / _ModelNotFound /
    _ServiceUnavailable / Exception. Sends only the system
    instruction, session turns, prompt, and viewer role.
    Transient 503s are retried with backoff before giving up.
    """
    from flask import current_app
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    model = (os.getenv("GEMINI_MODEL") or DEFAULT_MODEL).strip()
    try:
        timeout = float(current_app.config.get("ASSISTANT_TIMEOUT",
                                               DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    contents = []
    for m in history[-HISTORY_TURNS:]:
        contents.append({"role": "user" if m["role"] == "user" else "model",
                         "parts": [{"text": m["content"][:500]}]})
    contents.append({"role": "user", "parts": [{"text": prompt[:1000]}]})
    body = json.dumps({
        "system_instruction": {"parts": [
            {"text": SYSTEM_INSTRUCTION + f"\nViewer role: {role}."}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 512, "temperature": 0.4},
    }).encode("utf-8")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    last_exc = None
    for attempt in range(2):
        req = urllib.request.Request(
            url + "?key=" + key, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            parts = (((payload.get("candidates") or [{}])[0]
                      .get("content") or {}).get("parts") or [])
            text = "".join(p.get("text", "") for p in parts if
                           isinstance(p, dict)).strip()
            if not text:
                raise ValueError("Empty provider reply")
            return text[:2000]
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise _RateLimited()
            if exc.code == 404:
                raise _ModelNotFound()
            if exc.code == 503 and attempt == 0:
                last_exc = _ServiceUnavailable()
                time.sleep(1.5)
                continue
            if exc.code == 503:
                raise last_exc or _ServiceUnavailable()
            raise
        except TimeoutError:
            raise _TimedOut()
        except Exception as exc:
            if "timed out" in type(exc).__name__.lower() or "timeout" in str(
                    exc).lower():
                raise _TimedOut()
            raise
    raise last_exc


def _offline_reply(prompt):
    """Built-in ShareHope guide used when the AI service is unavailable.

    Static navigation help only — links to real pages, never invented
    records, statuses, or users.
    """
    p = (prompt or "").lower()

    def has(*words):
        return any(w in p for w in words)

    if has("hello", "hi", "hey", "namaste", "good morning", "good evening"):
        return ("Hello! I can explain donations, requirements, tracking, "
                "messages, notifications, and analytics — or point you to "
                "the right page. What would you like to do on ShareHope?")
    if has("categor"):
        return ("ShareHope has eight categories: Books & Education, Food & "
                "Groceries, Clothing, Electronics, Toys & Games, Home "
                "Essentials, Furniture, and Shoes & Accessories. Browse "
                "offers on /explore and needs on /requirements.")
    if has("track", "status", "where", "progress", "handover", "handed"):
        return ("Donations move posted → matched → accepted → handed over → "
                "completed. Donors mark handover; NGOs confirm receipt. "
                "Follow any offer on /tracking, where each step shows its "
                "timestamp from the ledger.")
    if has("requirement", "need", "ngo"):
        return ("NGOs publish requirements (title, category, quantity, "
                "location, urgency) from /requirements/new. Matching donor "
                "offers appear in the NGO inbox, where they can link, "
                "accept, or decline. Donors find needs on /requirements.")
    if has("offer", "donat", "give", "post"):
        return ("Donors post offers from /offers/new with a title, "
                "category, description, quantity, and pickup location. "
                "Matching NGOs are notified, and progress is visible on "
                "/donor and /tracking.")
    if has("message", "chat", "talk", "contact"):
        return ("Donors and NGOs coordinate in threads on /messages — one "
                "thread per donor–NGO pair, newest replies first. New "
                "messages also raise a notification.")
    if has("notif", "alert", "bell", "remind"):
        return ("Accepts, matches, and handover news land on "
                "/notifications with All / Unread / Read filters. Open a "
                "thread or mark items read to clear the badge.")
    if has("analytic", "stat", "chart", "report", "insight"):
        return ("Live totals, trends, category and status charts are on "
                "/analytics with date-range and daily / weekly / monthly "
                "views. Donors also see their own figures there.")
    if has("profile", "account", "password", "email", "verif"):
        return ("Manage your name, city, and organisation details on "
                "/profile, change your password there too, and resend "
                "email verification from /verify-email if needed.")
    if has("register", "sign up", "join", "create account"):
        return ("Join from /register — choose the Donor door to offer "
                "goods or the NGO door (organisation + registration ID) "
                "to publish needs. Then verify your email.")
    if has("login", "log in", "sign in", "forgot"):
        return ("Log in on /login. Locked out? Use /forgot-password for a "
                "single-use reset link valid for one hour.")
    if has("thank", "shukriya", "great", "nice"):
        return "You're welcome! Anything else about donating or requesting on ShareHope?"
    return ("I can help with donations, requirements, tracking, messages, "
            "notifications, analytics, accounts, and finding your way "
            "around — e.g. “how do I post an offer?” or “where is "
            "tracking?”. What do you need?")
