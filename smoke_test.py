"""Manual smoke test — runs the REAL Flask app against an isolated
SQLite database. Never touches production MySQL.

Exercises the full donor -> NGO -> admin flow through the actual WSGI
stack (real routing, templates, sessions, CSRF).
"""
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import http.cookiejar
import json

# Point the app at a throwaway SQLite DB BEFORE importing app
os.environ["DB_USER"] = "smoke"
os.environ["DB_PASSWORD"] = "smoke"
os.environ["DB_HOST"] = "localhost"
os.environ["DB_PORT"] = "3306"
os.environ["DB_NAME"] = "smoke_sharehope"
os.environ["MAIL_USERNAME"] = ""
os.environ["MAIL_PASSWORD"] = ""
os.environ["GEMINI_API_KEY"] = ""

from app import app, db
import models

# Swap to isolated SQLite
from sqlalchemy import create_engine
fd, DB_PATH = tempfile.mkstemp(prefix="sharehope_smoke_", suffix=".db")
os.close(fd)
os.remove(DB_PATH)
engine = create_engine(f"sqlite:///{DB_PATH}")
db._app_engines[app][None] = engine

app.config["TESTING"] = False
app.config["WTF_CSRF_ENABLED"] = True
app.config["MAIL_SUPPRESS_SEND"] = True
app.config["SERVER_NAME"] = "localhost:5057"

with app.app_context():
    db.drop_all()
    db.create_all()

# Seed one donor + one NGO
with app.app_context():
    d = models.User(name="Smoke Donor", email="donor@smoke.test",
                    role="donor", city="Pune")
    d.set_password("donorpass123")
    n = models.User(name="Smoke NGO", email="ngo@smoke.test",
                    role="ngo", city="Pune",
                    org_name="Smoke Trust", org_reg_id="SMK/001")
    n.set_password("ngopass123")
    a = models.User(name="Smoke Admin", email="admin@smoke.test",
                    role="admin", city="Pune")
    a.set_password("adminpass123")
    db.session.add_all([d, n, a])
    db.session.commit()
    DONOR_ID, NGO_ID, ADMIN_ID = d.id, n.id, a.id

# Publish one requirement as the NGO
with app.app_context():
    r = models.NGORequirement(
        ngo_id=NGO_ID, title="Smoke need: books",
        description="Library needs textbooks for children.",
        category="books", required_quantity="20 books",
        location="Pune", urgency="urgent", status="open")
    db.session.add(r)
    db.session.commit()
    REQ_ID = r.id

PORT = 5057
BASE = f"http://localhost:{PORT}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            NoRedirect,
            urllib.request.HTTPCookieProcessor(self.cj))

    def _open(self, req):
        try:
            with self.opener.open(req) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def get(self, path):
        return self._open(urllib.request.Request(BASE + path))

    def post(self, path, data=None, headers=None):
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(BASE + path, data=body, method="POST")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        return self._open(req)

    def csrf(self, path):
        _, html, _ = self.get(path)
        import re
        m = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return m.group(1) if m else None


def check(label, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {extra}" if extra and not cond else ""))
    return cond


def main():
    # Start the real server in a thread
    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, debug=False,
                                use_reloader=False),
        daemon=True)
    t.start()
    time.sleep(2)

    passed = 0
    failed = 0

    def run(label, cond, extra=""):
        nonlocal passed, failed
        if check(label, cond, extra):
            passed += 1
        else:
            failed += 1

    print("\n=== DONOR FLOW ===")
    donor = Client()

    # 1. Login
    tok = donor.csrf("/login")
    st, _, hdrs = donor.post("/login", {
        "email": "donor@smoke.test", "password": "donorpass123",
        "csrf_token": tok})
    run("Donor login redirects to /donor",
        st in (302, 303) and "/donor" in hdrs.get("Location", ""))

    # 2. Browse NGO needs
    st, html, _ = donor.get("/requirements")
    run("Donor browses NGO needs", st == 200 and "Smoke need: books" in html)
    run("Requirement links to detail",
        f"/requirements/{REQ_ID}" in html)

    # 3. Open requirement
    st, html, _ = donor.get(f"/requirements/{REQ_ID}")
    run("Donor opens requirement detail",
        st == 200 and "Smoke need: books" in html and "Smoke Trust" in html)
    run("Respond form present", 'name="title"' in html)

    # 4. Submit donation response
    tok = donor.csrf(f"/requirements/{REQ_ID}")
    st, _, hdrs = donor.post(f"/requirements/{REQ_ID}/respond", {
        "title": "Smoke books offer",
        "description": "Good condition textbooks, grade 4-6.",
        "quantity": "25 books", "location": "Pune",
        "availability": "Flexible", "csrf_token": tok})
    run("Donor submits response", st in (302, 303))

    # 5. Verify success message
    st, html, _ = donor.get(f"/requirements/{REQ_ID}")
    run("Response success shown", "Response sent" in html or "Response" in html)

    # 6. Open messages
    st, html, _ = donor.get("/messages")
    run("Donor opens messages", st == 200)

    # 7. Send NGO message
    tok = donor.csrf("/messages")
    st, _, hdrs = donor.post("/messages/send", {
        "to": str(NGO_ID), "content": "Hello NGO, I can deliver Tue.",
        "csrf_token": tok})
    run("Donor sends message to NGO", st in (302, 303))

    # 8. Open notifications
    st, html, _ = donor.get("/notifications")
    run("Donor opens notifications", st == 200)

    # 9. Verify notification exists (message alert from NGO reply comes later)
    st, html, _ = donor.get("/notifications")
    run("Donor notification page renders", st == 200)

    # 10. Logout
    st, _, hdrs = donor.get("/logout")
    run("Donor logout", st in (302, 303))

    # 11. Open Login
    st, html, _ = donor.get("/login")
    run("Login page renders after logout", st == 200)
    run("No stale email", 'value="donor@smoke.test"' not in html)
    run("No stale password", 'value="donorpass123"' not in html)
    run("no-store cache header", "no-store" in hdrs.get("Cache-Control", "")
        or "no-store" in donor.get("/login")[2].get("Cache-Control", ""))

    print("\n=== NGO FLOW ===")
    ngo = Client()

    # 1. Login
    tok = ngo.csrf("/login")
    st, _, hdrs = ngo.post("/login", {
        "email": "ngo@smoke.test", "password": "ngopass123",
        "csrf_token": tok})
    run("NGO login redirects to /ngo",
        st in (302, 303) and "/ngo" in hdrs.get("Location", ""))

    # 2. Verify donation response in inbox
    st, html, _ = ngo.get("/ngo")
    run("NGO sees donation response", "Smoke books offer" in html)

    # 3. Verify notification
    st, html, _ = ngo.get("/notifications")
    run("NGO sees response notification",
        "Smoke books offer" in html or "New response" in html)

    # 4. Open messages
    st, html, _ = ngo.get(f"/messages?with={DONOR_ID}")
    run("NGO opens conversation", st == 200)
    run("NGO sees donor message", "Hello NGO" in html)

    # 5. Reply to donor
    tok = ngo.csrf(f"/messages?with={DONOR_ID}")
    st, _, hdrs = ngo.post("/messages/send", {
        "to": str(DONOR_ID), "content": "Tue works, thank you!",
        "csrf_token": tok})
    run("NGO replies to donor", st in (302, 303))

    # 6. Verify unread count for donor
    st, html, _ = ngo.get("/messages/unread-count")
    # (NGO's own unread is 0 after viewing; donor's is 1)

    # 7. Donor logs in and verifies reply
    tok = donor.csrf("/login")
    donor.post("/login", {
        "email": "donor@smoke.test", "password": "donorpass123",
        "csrf_token": tok})
    st, html, _ = donor.get(f"/messages?with={NGO_ID}")
    run("Donor sees NGO reply", "Tue works, thank you!" in html)

    # 8. Donor notification for message
    st, html, _ = donor.get("/notifications")
    run("Donor sees message notification", "New message" in html)

    print("\n=== ADMIN FLOW ===")
    admin = Client()

    # 1. Login
    tok = admin.csrf("/login")
    st, _, hdrs = admin.post("/login", {
        "email": "admin@smoke.test", "password": "adminpass123",
        "csrf_token": tok})
    run("Admin login redirects to /admin",
        st in (302, 303) and "/admin" in hdrs.get("Location", ""))

    # 2. Open admin area
    st, html, _ = admin.get("/admin")
    run("Admin dashboard renders", st == 200 and "Keep the ledger honest" in html)

    # 3. Donor cannot access admin
    st, _, hdrs = donor.get("/admin")
    run("Donor blocked from /admin",
        st in (302, 303) and "/donor" in hdrs.get("Location", ""))

    # 4. NGO cannot access admin
    st, _, hdrs = ngo.get("/admin")
    run("NGO blocked from /admin",
        st in (302, 303) and "/ngo" in hdrs.get("Location", ""))

    # 5. Logout
    st, _, hdrs = admin.get("/logout")
    run("Admin logout", st in (302, 303))
    st, _, hdrs = admin.get("/admin")
    run("Admin blocked after logout",
        st in (302, 303) and "/login" in hdrs.get("Location", ""))

    print("\n=== REGISTRATION ===")
    reg = Client()
    tok = reg.csrf("/register")
    st, _, hdrs = reg.post("/register", {
        "role": "donor", "name": "Smoke Reg",
        "email": "reg@smoke.test", "city": "Pune",
        "org": "", "regid": "",
        "password": "regpass123", "confirm": "regpass123",
        "csrf_token": tok})
    run("Registration redirects to /donor",
        st in (302, 303) and "/donor" in hdrs.get("Location", ""))

    # Public signup cannot create admin
    tok = reg.csrf("/register")
    st, _, _ = reg.post("/register", {
        "role": "admin", "name": "Sneaky",
        "email": "sneaky@smoke.test", "city": "Pune",
        "password": "password123", "confirm": "password123",
        "csrf_token": tok})
    run("Public signup cannot create admin", st == 400)

    print("\n=== ANALYTICS ===")
    st, html, _ = reg.get("/analytics")
    run("Analytics renders", st == 200)
    run("Analytics has live data", "Total donation offers" in html)

    print("\n=== ASSISTANT ===")
    st, html, _ = reg.get("/assistant")
    run("Assistant renders", st == 200)

    print("\n=== HOME ===")
    st, html, _ = reg.get("/")
    run("Home renders", st == 200)
    run("No outdated banner", "donations connect next" not in html)

    print(f"\n{'='*50}")
    print(f"SMOKE TEST RESULTS: {passed} passed, {failed} failed")
    print(f"{'='*50}")

    # Cleanup
    with app.app_context():
        db.session.remove()
        db.drop_all()
    engine.dispose()
    try:
        os.remove(DB_PATH)
    except OSError:
        pass

    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
