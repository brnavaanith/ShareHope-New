"""ShareHope email tests — isolated SQLite + mock SMTP.

Never touches production MySQL and never sends real email.
SMTP is mocked via unittest.mock; Flask-Mail config is faked with
is_configured patched True so send paths are exercised.

Covers: welcome+verify on register, verify valid/invalid/expired/
reuse, forgot (no reveal), reset valid/invalid/expired/reuse,
donation-status emails, message alerts, duplicate suppression,
SMTP-failure non-blocking.
"""

import os
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from app import app, db
import models
import email_service


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _swap_engine_to_sqlite():
    from sqlalchemy import create_engine
    with app.app_context():
        old_engine = db._app_engines[app].get(None)
        try:
            if old_engine is not None:
                old_engine.dispose()
        except Exception:
            pass
        fd, path = tempfile.mkstemp(prefix="sharehope_email_test_", suffix=".db")
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


class EmailBase(unittest.TestCase):
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
        # Email tests mock the SMTP transport, so allow send paths to
        # reach the mock (other suites suppress them entirely).
        app.config["MAIL_SUPPRESS_SEND"] = False
        self.client = app.test_client()
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            self._seed()

    def tearDown(self):
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            try:
                db.session.remove()
            except Exception:
                pass

    def _seed(self):
        donor = models.User(name="Donor One", email="donor1@example.com",
                            role="donor", city="Pune")
        donor.set_password("donorpass123")
        ngo = models.User(name="NGO One", email="ngo1@example.com",
                          role="ngo", city="Pune",
                          org_name="Helping Hands", org_reg_id="MH/2020/001")
        ngo.set_password("ngopass123")
        db.session.add_all([donor, ngo])
        db.session.commit()
        self.donor_id, self.ngo_id = donor.id, ngo.id
        off = models.DonationOffer(
            donor_id=self.donor_id, title="Books bundle",
            description="Good condition textbooks for school kids.",
            category="books", quantity="10 books",
            location="Pune", availability="Flexible", status="pending")
        req = models.NGORequirement(
            ngo_id=self.ngo_id, title="Need books",
            description="School library needs textbooks urgently.",
            category="books", required_quantity="10 books",
            location="Pune", urgency="open", status="open")
        db.session.add_all([off, req])
        db.session.commit()
        self.offer_id, self.req_id = off.id, req.id

    def login_as(self, email, pwd):
        rv = self.client.post("/login", data={"email": email, "password": pwd},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        return rv


class TestWelcomeVerify(EmailBase):
    def test_register_sends_welcome_and_verification(self):
        sent = []
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent.append(m)):
            with patch.object(email_service, "is_configured", return_value=True):
                rv = self.client.post("/register", data={
                    "role": "donor", "name": "Newbie",
                    "email": "fresh@example.com", "city": "Pune",
                    "org": "", "regid": "",
                    "password": "password123", "confirm": "password123"},
                    follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        subjects = [m.subject for m in sent]
        self.assertIn("Welcome to ShareHope", subjects)
        self.assertTrue(any("Verify" in s for s in subjects))
        # HTML + plain-text parts present, branding present
        welcome = [m for m in sent if m.subject == "Welcome to ShareHope"][0]
        self.assertIn("ShareHope", welcome.html)
        self.assertTrue(welcome.body and len(welcome.body) > 10)
        with app.app_context():
            u = models.User.query.filter_by(email="fresh@example.com").first()
            self.assertIsNotNone(u)
            self.assertFalse(u.email_verified)

    def test_verify_valid_marks_verified(self):
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            token = email_service.generate_verify_token(u)
        rv = self.client.get(f"/verify-email/{token}", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            u2 = db.session.get(models.User, self.donor_id)
            self.assertTrue(u2.email_verified)
            self.assertIsNotNone(u2.verified_at)

    def test_verify_invalid_token_400(self):
        rv = self.client.get("/verify-email/not-a-real-token",
                             follow_redirects=False)
        self.assertEqual(rv.status_code, 400)

    def test_verify_reuse_rejected(self):
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            token = email_service.generate_verify_token(u)
        self.assertIn(self.client.get(f"/verify-email/{token}").status_code,
                      (302, 303))
        rv2 = self.client.get(f"/verify-email/{token}", follow_redirects=False)
        self.assertEqual(rv2.status_code, 400)

    def test_verify_expired_rejected(self):
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            token = email_service.generate_verify_token(u)
        # Force expiry without sleeping: max_age=0 expires anything older
        # than ~0 seconds; generate then confirm with negative age.
        self.assertIsNone(email_service.confirm_verify_token(token, max_age=-1))
        # Route uses default 24h so a fresh token still verifies (sanity)
        rv = self.client.get(f"/verify-email/{token}", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))


class TestPasswordReset(EmailBase):
    def test_forgot_same_message_unknown_and_known(self):
        sent = []
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent.append(m)):
            with patch.object(email_service, "is_configured", return_value=True):
                r1 = self.client.post("/forgot-password",
                                      data={"email": "ghost@example.com"},
                                      follow_redirects=False)
                n1 = len(sent)
                r2 = self.client.post("/forgot-password",
                                      data={"email": "donor1@example.com"},
                                      follow_redirects=False)
                n2 = len(sent)
        self.assertIn(r1.status_code, (302, 303))
        self.assertIn(r2.status_code, (302, 303))
        # Same redirect target (no reveal): both go to login
        self.assertEqual(r1.headers.get("Location"), r2.headers.get("Location"))
        self.assertIn("login", r1.headers.get("Location", ""))
        self.assertEqual(n1, 0)  # unknown → no email
        self.assertEqual(n2, 1)  # known → one email
        self.assertIn("Reset", sent[0].subject)

    def test_reset_valid_flow_and_single_use(self):
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            token = email_service.generate_reset_token(u)
        self.assertEqual(self.client.get(f"/reset-password/{token}").status_code, 200)
        rv = self.client.post(f"/reset-password/{token}", data={
            "password": "brandnew123", "confirm": "brandnew123"},
            follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            u2 = db.session.get(models.User, self.donor_id)
            self.assertTrue(u2.check_password("brandnew123"))
        # Reuse must fail (hash changed → single-use)
        rv2 = self.client.get(f"/reset-password/{token}", follow_redirects=False)
        self.assertEqual(rv2.status_code, 400)
        rv3 = self.client.post(f"/reset-password/{token}", data={
            "password": "another123", "confirm": "another123"},
            follow_redirects=False)
        self.assertEqual(rv3.status_code, 400)

    def test_reset_invalid_and_expired(self):
        rv = self.client.get("/reset-password/garbage-token", follow_redirects=False)
        self.assertEqual(rv.status_code, 400)
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            token = email_service.generate_reset_token(u)
        self.assertIsNone(email_service.confirm_reset_token(token, max_age=-1))

    def test_reset_rejects_short_and_mismatch(self):
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            token = email_service.generate_reset_token(u)
        r1 = self.client.post(f"/reset-password/{token}", data={
            "password": "short", "confirm": "short"}, follow_redirects=False)
        self.assertEqual(r1.status_code, 400)
        r2 = self.client.post(f"/reset-password/{token}", data={
            "password": "longenough1", "confirm": "different2"},
            follow_redirects=False)
        self.assertEqual(r2.status_code, 400)


class TestDonationMessageEmails(EmailBase):
    def _link(self):
        with app.app_context():
            t = models.DonationTracking(
                offer_id=self.offer_id, requirement_id=self.req_id,
                acceptance_status="pending", handover_status="pending")
            db.session.add(t)
            db.session.commit()
            return t.id

    def test_accept_sends_email_once(self):
        tid = self._link()
        self.login_as("ngo1@example.com", "ngopass123")
        sent = []
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent.append(m)):
            with patch.object(email_service, "is_configured", return_value=True):
                rv = self.client.post(f"/tracking/{tid}/accept", data={},
                                      follow_redirects=False)
                self.assertIn(rv.status_code, (302, 303))
                n1 = len(sent)
                # Retry is idempotent ("Already accepted") → no second email
                rv2 = self.client.post(f"/tracking/{tid}/accept", data={},
                                       follow_redirects=False)
                self.assertIn(rv2.status_code, (302, 303))
        self.assertEqual(n1, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("accepted", sent[0].subject.lower())

    def test_decline_and_handover_complete_send(self):
        # decline path
        tid = self._link()
        self.login_as("ngo1@example.com", "ngopass123")
        sent = []
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent.append(m)):
            with patch.object(email_service, "is_configured", return_value=True):
                self.client.post(f"/tracking/{tid}/decline", data={})
        self.assertEqual(len(sent), 1)
        # handover + complete path (fresh link)
        with app.app_context():
            off = models.DonationOffer(
                donor_id=self.donor_id, title="Laptop",
                description="Working laptop with charger, good battery.",
                category="electronics", quantity="1",
                location="Pune", availability="Flexible", status="pending")
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need laptop",
                description="Training centre needs a laptop urgently.",
                category="electronics", required_quantity="1",
                location="Pune", urgency="open", status="open")
            db.session.add_all([off, req])
            db.session.commit()
            t2 = models.DonationTracking(
                offer_id=off.id, requirement_id=req.id,
                acceptance_status="accepted", handover_status="pending")
            from datetime import datetime
            t2.accepted_at = datetime.utcnow()
            off.status = "accepted"
            db.session.add(t2)
            db.session.commit()
            tid2 = t2.id
        sent2 = []
        c_donor = app.test_client()
        c_donor.post("/login", data={"email": "donor1@example.com",
                                     "password": "donorpass123"})
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent2.append(m)):
            with patch.object(email_service, "is_configured", return_value=True):
                c_donor.post(f"/tracking/{tid2}/handover", data={})
        self.assertEqual(len(sent2), 1)
        self.assertIn("handover", sent2[0].subject.lower())

    def test_message_alert_and_no_duplicate(self):
        self.login_as("donor1@example.com", "donorpass123")
        sent = []
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent.append(m)):
            with patch.object(email_service, "is_configured", return_value=True):
                self.client.post("/messages/send",
                                 data={"to": str(self.ngo_id), "content": "Hello NGO"})
                n1 = len(sent)
                # duplicate submit → ignored, no second email
                self.client.post("/messages/send",
                                 data={"to": str(self.ngo_id), "content": "Hello NGO"})
        self.assertEqual(n1, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("message", sent[0].subject.lower())

    def test_smtp_failure_does_not_undo_db(self):
        tid = self._link()
        self.login_as("ngo1@example.com", "ngopass123")
        with patch.object(email_service.mail, "send",
                          side_effect=Exception("SMTP down")):
            with patch.object(email_service, "is_configured", return_value=True):
                rv = self.client.post(f"/tracking/{tid}/accept", data={},
                                      follow_redirects=False)
        # Success stands despite email failure
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            t = db.session.get(models.DonationTracking, tid)
            self.assertEqual(t.acceptance_status, "accepted")
            o = db.session.get(models.DonationOffer, self.offer_id)
            self.assertEqual(o.status, "accepted")
        # Register still works when SMTP down
        c2 = app.test_client()
        with patch.object(email_service.mail, "send",
                          side_effect=Exception("SMTP down")):
            with patch.object(email_service, "is_configured", return_value=True):
                rv2 = c2.post("/register", data={
                    "role": "donor", "name": "SMTP Fail",
                    "email": "smtp@example.com", "city": "Pune",
                    "password": "password123", "confirm": "password123"},
                    follow_redirects=False)
        self.assertIn(rv2.status_code, (302, 303))
        with app.app_context():
            self.assertIsNotNone(
                models.User.query.filter_by(email="smtp@example.com").first())

    def test_no_email_on_failed_or_unauthorized(self):
        self.login_as("donor1@example.com", "donorpass123")
        sent = []
        with patch.object(email_service.mail, "send",
                          side_effect=lambda m: sent.append(m)):
            with patch.object(email_service, "is_configured", return_value=True):
                # empty message → 400, no email
                self.client.post("/messages/send",
                                 data={"to": str(self.ngo_id), "content": "   "})
                # invalid recipient → 404, no email
                self.client.post("/messages/send",
                                 data={"to": "999999", "content": "Hi"})
        self.assertEqual(len(sent), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
