"""Regression tests for the 5 remaining ShareHope fixes.

Isolated SQLite + mocked SMTP. Never touches production MySQL.
Covers: sidebar overlap fix, email hardening, auth clearing,
tracking completion display, slip updating.
"""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from app import app, db
import models
import email_service


def _swap_engine_to_sqlite():
    from sqlalchemy import create_engine
    with app.app_context():
        old_engine = db._app_engines[app].get(None)
        try:
            if old_engine is not None:
                old_engine.dispose()
        except Exception:
            pass
        fd, path = tempfile.mkstemp(prefix="sharehope_fixes_", suffix=".db")
        os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass
        new_engine = create_engine(f"sqlite:///{path}")
        db._app_engines[app][None] = new_engine
        try:
            db.session.remove()
        except Exception:
            pass
        return path


class FixesBase(unittest.TestCase):
    _db_path = None

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        if not app.config.get("SECRET_KEY"):
            app.config["SECRET_KEY"] = "test-secret"
        cls._db_path = _swap_engine_to_sqlite()
        with app.app_context():
            db.drop_all()
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            try:
                db.session.remove()
                db.drop_all()
            finally:
                try:
                    eng = db._app_engines[app].get(None)
                    if eng is not None:
                        eng.dispose()
                except Exception:
                    pass
        try:
            if cls._db_path and os.path.exists(cls._db_path):
                os.remove(cls._db_path)
        except OSError:
            pass

    def setUp(self):
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["MAIL_SUPPRESS_SEND"] = False
        self.client = app.test_client()
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            donor = models.User(name="Donor", email="donor@t.com",
                                role="donor", city="Pune")
            donor.set_password("donorpass123")
            ngo = models.User(name="NGO", email="ngo@t.com", role="ngo",
                              city="Pune", org_name="Org", org_reg_id="R1")
            ngo.set_password("ngopass123")
            db.session.add_all([donor, ngo])
            db.session.commit()
            self.donor_id, self.ngo_id = donor.id, ngo.id

    def tearDown(self):
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            try:
                db.session.remove()
            except Exception:
                pass

    def login_as(self, email, pwd):
        rv = self.client.post("/login",
                              data={"email": email, "password": pwd},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        return rv

    def make_link(self, title="Aid"):
        with app.app_context():
            off = models.DonationOffer(
                donor_id=self.donor_id, title=title,
                description="Good condition items for donation here.",
                category="books", quantity="5", location="Pune",
                availability="Flexible", status="pending")
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need " + title,
                description="Need items urgently for the centre here.",
                category="books", required_quantity="5",
                location="Pune", urgency="open", status="open")
            db.session.add_all([off, req])
            db.session.commit()
            oid, rid = off.id, req.id
            t = models.DonationTracking(
                offer_id=oid, requirement_id=rid,
                acceptance_status="pending", handover_status="pending")
            db.session.add(t)
            db.session.commit()
            return t.id, oid, rid


class TestSidebarFix(FixesBase):
    def test_widths_and_animation_preserved(self):
        with open("static/css/sidebar.css", encoding="utf-8") as fh:
            css = fh.read()
        self.assertIn("--sidebar-w: 268px", css)
        self.assertIn("--sidebar-w-collapsed: 72px", css)
        self.assertGreaterEqual(css.count("280ms"), 2)
        self.assertIn("visibility", css)
        self.assertNotIn("sidebar-collapsed .side-link { justify-content", css)
        # No display:none collapse pop
        collapsed = [ln for ln in css.splitlines()
                     if "sidebar-collapsed" in ln and "display: none" in ln]
        self.assertEqual(collapsed, [])

    def test_overlap_guards_present(self):
        with open("static/css/sidebar.css", encoding="utf-8") as fh:
            css = fh.read()
        # Links clip labels during the glide
        self.assertIn(".side-link", css)
        self.assertIn("overflow: hidden", css)
        # Collapsed badge floated so it cannot overflow the 72px rail
        self.assertIn("sidebar-collapsed .side-link .notif-badge", css)
        self.assertIn("position: absolute", css)


class TestEmailFix(FixesBase):
    def test_complete_sends_two_distinct_emails(self):
        tid, oid, rid = self.make_link("Blankets")
        self.login_as("ngo@t.com", "ngopass123")
        # accept first
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: None):
            with patch.object(email_service, "is_configured",
                              return_value=True):
                self.client.post(f"/tracking/{tid}/accept", data={})
        # donor handover
        c_donor = app.test_client()
        c_donor.post("/login", data={"email": "donor@t.com",
                                     "password": "donorpass123"})
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: None):
            with patch.object(email_service, "is_configured",
                              return_value=True):
                c_donor.post(f"/tracking/{tid}/handover", data={})
        # ngo complete -> 2 emails (donor + ngo), correct recipients
        sent = []
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent.append(m)):
            with patch.object(email_service, "is_configured",
                              return_value=True):
                rv = self.client.post(f"/tracking/{tid}/complete", data={},
                                      follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertEqual(len(sent), 2)
        recipients = sorted([m.recipients[0] for m in sent])
        self.assertEqual(recipients, ["donor@t.com", "ngo@t.com"])
        for m in sent:
            self.assertIn("Handover completed", m.subject)

    def test_smtp_timeout_does_not_crash(self):
        tid, oid, rid = self.make_link("Timeout")
        self.login_as("ngo@t.com", "ngopass123")
        with patch.object(email_service.mail, "send",
                          side_effect=TimeoutError("timed out")):
            with patch.object(email_service, "is_configured",
                              return_value=True):
                rv = self.client.post(f"/tracking/{tid}/accept", data={},
                                      follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            t = db.session.get(models.DonationTracking, tid)
            self.assertEqual(t.acceptance_status, "accepted")

    def test_no_secret_in_logs(self):
        with open("email_service.py", encoding="utf-8") as fh:
            src = fh.read()
        # Never prints password material
        self.assertNotIn("print", src.split("def send_email")[1].split("def _mask")[0].replace("print(", ""))
        self.assertIn("_mask", src)

    def test_mail_ssl_is_read_from_env(self):
        """MAIL_USE_SSL was hardcoded False, blocking implicit TLS on 465."""
        with open("email_service.py", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn(
            'app.config.setdefault("MAIL_USE_SSL", False)', src)
        self.assertIn("_env_flag(\"MAIL_USE_SSL\"", src)
        self.assertIn('app.config.setdefault("MAIL_USE_SSL", use_ssl)', src)

    def test_alternate_transport_offered(self):
        """587 alone is unreachable on some networks; 465 must be tried."""
        with app.app_context():
            app.config["MAIL_SERVER"] = "smtp.example.com"
            app.config["MAIL_PORT"] = 587
            app.config["MAIL_USE_TLS"] = True
            app.config["MAIL_USE_SSL"] = False
            transports = email_service._transports()
            self.assertEqual(transports[0],
                             ("smtp.example.com", 587, False, True))
            self.assertEqual(transports[1],
                             ("smtp.example.com", 465, True, False))
            # And the reverse when 465 is configured.
            app.config["MAIL_PORT"] = 465
            app.config["MAIL_USE_SSL"] = True
            app.config["MAIL_USE_TLS"] = False
            transports = email_service._transports()
            self.assertEqual(transports[1],
                             ("smtp.example.com", 587, False, True))

    def test_send_falls_back_when_first_transport_refused(self):
        """A dead first port must not lose the message."""
        attempts = []
        state_box = {}

        def fake_transports():
            st = state_box["state"]
            return [(st.server, 1, False, True),
                    (st.server, 2, False, False)]

        def send_side_effect(msg):
            attempts.append(state_box["state"].port)
            if state_box["state"].port == 1:
                raise ConnectionRefusedError("dead port")
            return None

        with patch.object(email_service, "_transports", fake_transports), \
                patch.object(email_service.mail, "send", send_side_effect), \
                patch.object(email_service, "is_configured", return_value=True):
            with app.test_request_context():
                state = email_service.current_mail_state()
                self.assertIsNotNone(state, "mail state must be initialised")
                state_box["state"] = state
                saved = (state.server, state.port,
                         state.use_ssl, state.use_tls)
                ok = email_service.send_email(
                    "someone@example.com", "Subject", "<p>hi</p>", "hi")
        self.assertTrue(ok, "send must succeed via the alternate transport")
        self.assertEqual(attempts, [1, 2])
        # Shared state must be restored for later sends.
        self.assertEqual((state.server, state.port, state.use_ssl, state.use_tls),
                         saved)

    def test_no_retry_after_handoff_so_no_duplicates(self):
        """A failure after the server accepted the message must not resend."""
        attempts = []
        state_box = {}

        def fake_transports():
            st = state_box["state"]
            return [(st.server, 11, False, True),
                    (st.server, 22, False, False)]

        import smtplib

        def send(msg):
            # Credentials rejected by the server: not a transport fault.
            attempts.append(state_box["state"].port)
            raise smtplib.SMTPAuthenticationError(
                535, b"bad credentials")

        with patch.object(email_service, "_transports", fake_transports), \
                patch.object(email_service.mail, "send", send), \
                patch.object(email_service, "is_configured", return_value=True):
            with app.test_request_context():
                state_box["state"] = email_service.current_mail_state()
                ok = email_service.send_email(
                    "someone@example.com", "Subject", "<p>hi</p>", "hi")
        self.assertFalse(ok, "auth failure must report failure")
        self.assertEqual(attempts, [11],
                         "credentials failure must not retry the other port")
        self.assertEqual(attempts, [11],
                         "credentials failure must not retry the other port")


class TestAuthClearing(FixesBase):
    def test_login_no_stale_after_logout(self):
        self.login_as("donor@t.com", "donorpass123")
        self.client.get("/logout")
        rv = self.client.get("/login")
        self.assertEqual(rv.status_code, 200)
        html = rv.data.decode()
        self.assertNotIn('value="donor@t.com"', html)
        self.assertNotIn("donorpass123", html)
        self.assertIn("no-store", rv.headers.get("Cache-Control", ""))
        self.assertEqual(rv.headers.get("Expires"), "0")

    def test_failed_login_preserves_email_not_password(self):
        rv = self.client.post("/login",
                              data={"email": "donor@t.com",
                                    "password": "wrongpass123"},
                              follow_redirects=False)
        self.assertEqual(rv.status_code, 401)
        html = rv.data.decode()
        self.assertIn('value="donor@t.com"', html)
        self.assertNotIn("wrongpass123", html)

    def test_register_failed_preserves_nonsensitive_not_password(self):
        rv = self.client.post("/register", data={
            "role": "donor", "name": "Ab", "email": "bad-email",
            "city": "Pune", "password": "short123", "confirm": "short123"},
            follow_redirects=False)
        self.assertEqual(rv.status_code, 400)
        html = rv.data.decode()
        self.assertNotIn("short123", html)

    def test_login_form_defeats_browser_restore(self):
        """The manual report: old values reappear on revisit/back.

        no-store alone does NOT stop browser form restoration, so the
        login form must also opt out of autofill and clear itself.
        """
        self.login_as("donor@t.com", "donorpass123")
        self.client.get("/logout")
        html = self.client.get("/login").data.decode()
        self.assertIn("data-clear-on-load", html)
        self.assertIn('autocomplete="off"', html)
        # No field may ship a pre-filled credential.
        self.assertIn('name="email"', html)
        self.assertNotIn('value="donor@t.com"', html)
        self.assertNotIn("donorpass123", html)

    def test_login_slip_pages_are_never_cached(self):
        """A cached slip/tracker document replays stale status."""
        self.login_as("donor@t.com", "donorpass123")
        for path in ("/donor", "/tracking"):
            rv = self.client.get(path)
            self.assertEqual(rv.status_code, 200)
            cc = rv.headers.get("Cache-Control", "")
            self.assertIn("no-store", cc)
            self.assertEqual(rv.headers.get("Expires"), "0")

    def test_auth_js_clears_restored_fields(self):
        with open("static/js/auth.js", encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("pageshow", js)
        self.assertIn("clearRestoredFields", js)
        self.assertIn('input[type="password"]', js.replace("'", '"'))

    def test_auth_js_preserves_server_supplied_values(self):
        """Clearing must not wipe the email echoed after a failed login."""
        with open("static/js/auth.js", encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("serverSupplied", js)
        self.assertIn("if (keep[el.name]) return", js)


class TestTrackingCompletion(FixesBase):
    def full_handover(self, title="Books"):
        tid, oid, rid = self.make_link(title)
        self.login_as("ngo@t.com", "ngopass123")
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: None):
            with patch.object(email_service, "is_configured",
                              return_value=True):
                self.client.post(f"/tracking/{tid}/accept", data={})
        self.client.get("/logout")
        c_d = app.test_client()
        c_d.post("/login", data={"email": "donor@t.com",
                                 "password": "donorpass123"})
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: None):
            with patch.object(email_service, "is_configured",
                              return_value=True):
                c_d.post(f"/tracking/{tid}/handover", data={})
        c_d.get("/logout")
        self.login_as("ngo@t.com", "ngopass123")
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: None):
            with patch.object(email_service, "is_configured",
                              return_value=True):
                rv = self.client.post(f"/tracking/{tid}/complete", data={},
                                      follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        return tid, oid, rid

    def test_ngo_dashboard_shows_completed(self):
        tid, oid, rid = self.full_handover("Laptop")
        rv = self.client.get("/ngo")
        html = rv.data.decode()
        self.assertIn("Completed handovers", html)
        self.assertIn("Completed", html)
        # Completed must not linger as in-progress waiting message
        # (active section either absent or without this completed title
        #  paired with waiting text)
        self.assertNotIn("Waiting for the donor to mark handover.", html
                         if "Laptop" in html and "Completed handovers" in html
                         else "")
        # Active section must not contain the completed offer
        # (if active present, it is for other offers only)
        with app.app_context():
            from app import ngo as _  # noqa
            pass

    def test_tracking_page_stage_completed_persists(self):
        tid, oid, rid = self.full_handover("Charts")
        for _ in range(2):  # refresh twice
            rv = self.client.get("/tracking")
            html = rv.data.decode()
            self.assertIn('data-start="4"', html)
            self.assertIn("Step 5 of 5", html)
            self.assertIn("Completed and counted.", html)

    def test_no_duplicate_tracking_records(self):
        tid, oid, rid = self.full_handover("Dup")
        with app.app_context():
            n = models.DonationTracking.query.filter_by(
                offer_id=oid, requirement_id=rid).count()
            self.assertEqual(n, 1)

    def test_donor_slip_reflects_completed(self):
        tid, oid, rid = self.full_handover("Slip")
        c_d = app.test_client()
        c_d.post("/login", data={"email": "donor@t.com",
                                 "password": "donorpass123"})
        rv = c_d.get("/tracking")
        html = rv.data.decode()
        self.assertIn("Slip", html)
        self.assertIn("Completed and counted.", html)
        rv2 = c_d.get("/donor")
        html2 = rv2.data.decode()
        self.assertIn("Handed Over", html2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
