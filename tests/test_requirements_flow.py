"""ShareHope NGO requirement + donor response tests — isolated SQLite.

Never touches production MySQL. Swaps the Flask-SQLAlchemy engine to a
temp SQLite file (same pattern as the other suites).

Covers TASK 2 (donor browses needs, opens detail, responds, NGO sees
response + notification, workflow continues) and TASK 12 (admin auth).
"""

import os
import re
import tempfile
import unittest

from app import app, db
import models


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _extract_csrf(html):
    m = CSRF_RE.search(html)
    return m.group(1) if m else None


def _swap_engine_to_sqlite():
    from sqlalchemy import create_engine
    with app.app_context():
        old_engine = db._app_engines[app].get(None)
        try:
            if old_engine is not None:
                old_engine.dispose()
        except Exception:
            pass
        fd, path = tempfile.mkstemp(prefix="sharehope_req_test_", suffix=".db")
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


class ReqBase(unittest.TestCase):
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
        app.config["MAIL_SUPPRESS_SEND"] = True
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
        req = models.NGORequirement(
            ngo_id=self.ngo_id, title="Need books",
            description="School library needs textbooks urgently.",
            category="books", required_quantity="10 books",
            location="Pune", urgency="urgent", status="open")
        db.session.add(req)
        db.session.commit()
        self.req_id = req.id

    def login_as(self, email, pwd):
        rv = self.client.post("/login", data={"email": email, "password": pwd},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        return rv


class TestBrowseAndDetail(ReqBase):
    def test_requirements_list_links_to_detail(self):
        html = self.client.get("/requirements").data.decode()
        self.assertIn(f"/requirements/{self.req_id}", html)
        self.assertIn("View", html)

    def test_anonymous_can_open_detail(self):
        rv = self.client.get(f"/requirements/{self.req_id}")
        self.assertEqual(rv.status_code, 200)
        html = rv.data.decode()
        self.assertIn("Need books", html)
        self.assertIn("Helping Hands", html)
        self.assertIn("10 books", html)
        self.assertIn("Log in as a donor to respond", html)

    def test_donor_detail_shows_respond_form(self):
        self.login_as("donor1@example.com", "donorpass123")
        html = self.client.get(f"/requirements/{self.req_id}").data.decode()
        self.assertIn('action="/requirements/{}/respond"'.format(self.req_id), html)
        self.assertIn('name="title"', html)
        self.assertIn('name="quantity"', html)

    def test_missing_requirement_404(self):
        rv = self.client.get("/requirements/999999", follow_redirects=False)
        self.assertEqual(rv.status_code, 404)


class TestDonorRespond(ReqBase):
    def test_donor_submits_response(self):
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post(f"/requirements/{self.req_id}/respond", data={
            "title": "Books bundle", "description": "Good condition textbooks.",
            "quantity": "20 books", "location": "Pune",
            "availability": "Flexible"}, follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            offer = models.DonationOffer.query.filter_by(
                donor_id=self.donor_id).first()
            self.assertIsNotNone(offer)
            self.assertEqual(offer.status, "pending")
            self.assertEqual(offer.category, "books")
            link = models.DonationTracking.query.filter_by(
                offer_id=offer.id, requirement_id=self.req_id).first()
            self.assertIsNotNone(link)
            self.assertEqual(link.acceptance_status, "pending")
            hist = models.DonationHistory.query.filter_by(
                offer_id=offer.id).all()
            self.assertTrue(any(h.new_status == "pending" for h in hist))

    def test_response_creates_ngo_notification(self):
        self.login_as("donor1@example.com", "donorpass123")
        with app.app_context():
            before = models.Notification.query.filter_by(
                recipient_id=self.ngo_id).count()
        self.client.post(f"/requirements/{self.req_id}/respond", data={
            "title": "Books bundle", "description": "Good condition textbooks.",
            "quantity": "20 books", "location": "Pune",
            "availability": "Flexible"})
        with app.app_context():
            notes = models.Notification.query.filter_by(
                recipient_id=self.ngo_id).all()
            self.assertEqual(len(notes), before + 1)
            self.assertIn("Books bundle", notes[-1].content)
            self.assertFalse(notes[-1].is_read)

    def test_duplicate_response_ignored(self):
        self.login_as("donor1@example.com", "donorpass123")
        data = {"title": "Books bundle",
                "description": "Good condition textbooks.",
                "quantity": "20 books", "location": "Pune",
                "availability": "Flexible"}
        self.client.post(f"/requirements/{self.req_id}/respond", data=data)
        self.client.post(f"/requirements/{self.req_id}/respond", data=data)
        with app.app_context():
            offers = models.DonationOffer.query.filter_by(
                donor_id=self.donor_id).all()
            self.assertEqual(len(offers), 1)

    def test_ngo_cannot_respond(self):
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.post(f"/requirements/{self.req_id}/respond", data={
            "title": "X", "description": "Y", "quantity": "1",
            "location": "Pune"}, follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            self.assertEqual(models.DonationOffer.query.count(), 0)

    def test_anonymous_cannot_respond(self):
        rv = self.client.post(f"/requirements/{self.req_id}/respond", data={
            "title": "X", "description": "Y", "quantity": "1",
            "location": "Pune"}, follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/login", rv.headers.get("Location", ""))

    def test_invalid_response_400(self):
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post(f"/requirements/{self.req_id}/respond", data={
            "title": "AB", "description": "short", "quantity": "",
            "location": ""}, follow_redirects=False)
        self.assertEqual(rv.status_code, 400)
        with app.app_context():
            self.assertEqual(models.DonationOffer.query.count(), 0)

    def test_ngo_sees_response_in_inbox(self):
        self.login_as("donor1@example.com", "donorpass123")
        self.client.post(f"/requirements/{self.req_id}/respond", data={
            "title": "Books bundle", "description": "Good condition textbooks.",
            "quantity": "20 books", "location": "Pune",
            "availability": "Flexible"})
        self.client.get("/logout")
        self.login_as("ngo1@example.com", "ngopass123")
        html = self.client.get("/ngo").data.decode()
        self.assertIn("Books bundle", html)
        # NGO can accept the response via existing workflow
        with app.app_context():
            link = models.DonationTracking.query.filter_by(
                requirement_id=self.req_id).first()
            self.assertIsNotNone(link)
            link_id = link.id
        rv = self.client.post(f"/tracking/{link_id}/accept", data={},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            offer = models.DonationOffer.query.filter_by(
                donor_id=self.donor_id).first()
            self.assertEqual(offer.status, "accepted")


class TestAdminAuth(ReqBase):
    def _mkadmin(self):
        with app.app_context():
            a = models.User(name="Admin", email="admin@example.com",
                            role="admin", city="Pune")
            a.set_password("adminpass123")
            db.session.add(a)
            db.session.commit()
            return a.id

    def test_admin_login_and_dashboard(self):
        self._mkadmin()
        rv = self.client.post("/login", data={
            "email": "admin@example.com", "password": "adminpass123"},
            follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/admin", rv.headers.get("Location", ""))
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_donor_blocked_from_admin(self):
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.get("/admin", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/donor", rv.headers.get("Location", ""))

    def test_ngo_blocked_from_admin(self):
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.get("/admin", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/ngo", rv.headers.get("Location", ""))

    def test_public_signup_cannot_create_admin(self):
        for role in ("admin", "Admin", "ADMIN"):
            rv = self.client.post("/register", data={
                "role": role, "name": "Sneaky",
                "email": f"sneaky_{role}@example.com", "city": "Pune",
                "password": "password123", "confirm": "password123"},
                follow_redirects=False)
            self.assertIn(rv.status_code, (400, 409), role)
        with app.app_context():
            self.assertEqual(
                models.User.query.filter_by(role="admin").count(), 0)

    def test_admin_logout(self):
        self._mkadmin()
        self.login_as("admin@example.com", "adminpass123")
        self.client.get("/logout")
        rv = self.client.get("/admin", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/login", rv.headers.get("Location", ""))


class TestRegistrationAndAuth(ReqBase):
    def test_register_redirects_and_clears_form(self):
        rv = self.client.post("/register", data={
            "role": "donor", "name": "New User",
            "email": "newuser@example.com", "city": "Pune",
            "org": "", "regid": "",
            "password": "newpass123", "confirm": "newpass123"},
            follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/donor", rv.headers.get("Location", ""))
        # Follow redirect: dashboard renders, no stale form values
        html = self.client.get(rv.headers["Location"]).data.decode()
        self.assertNotIn('value="newuser@example.com"', html)
        self.assertNotIn('value="New User"', html)

    def test_register_validation_failure_keeps_fields_no_password(self):
        rv = self.client.post("/register", data={
            "role": "donor", "name": "New User",
            "email": "newuser@example.com", "city": "Pune",
            "org": "", "regid": "",
            "password": "short", "confirm": "short"},
            follow_redirects=False)
        self.assertEqual(rv.status_code, 400)
        html = rv.data.decode()
        # Useful fields remain
        self.assertIn('value="newuser@example.com"', html)
        self.assertIn('value="New User"', html)
        # Passwords never repopulated
        self.assertNotIn('value="short"', html)

    def test_logout_clears_session_and_login_has_no_stale_values(self):
        self.login_as("donor1@example.com", "donorpass123")
        self.client.get("/logout")
        html = self.client.get("/login").data.decode()
        self.assertNotIn('value="donor1@example.com"', html)
        self.assertNotIn('value="donorpass123"', html)
        # Cache headers prevent back-button replay
        self.assertIn("no-store", self.client.get("/login").headers.get(
            "Cache-Control", ""))

    def test_authenticated_user_redirected_from_register(self):
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.get("/register", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/donor", rv.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
