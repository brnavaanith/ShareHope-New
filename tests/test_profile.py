"""ShareHope profile tests — isolated SQLite harness.

Never touches production MySQL. Swaps the global Flask-SQLAlchemy
engine to a temp SQLite file (same pattern as the other suites).
Suppresses real SMTP.

Covers: display per role (no чужое data, no hashes), details edit
(donor + NGO fields, allowlist, validation), persistence across
logout/login, password change (current required, hashing, session
kept), role/email immutability, auth + CSRF, no per-user URLs.
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
        fd, path = tempfile.mkstemp(prefix="sharehope_profile_test_",
                                    suffix=".db")
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


class ProfileBase(unittest.TestCase):
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

    def _mkuser(self, name, email, role, city="Pune", **kw):
        u = models.User(name=name, email=email, role=role, city=city,
                        **kw)
        u.set_password("password123")
        db.session.add(u)
        db.session.commit()
        return u.id

    def _seed(self):
        self.donor_id = self._mkuser("Donor One", "donor1@example.com",
                                     "donor", city="Pune")
        self.donor2_id = self._mkuser("Donor Two", "donor2@example.com",
                                      "donor", city="Mumbai")
        self.ngo_id = self._mkuser("NGO One", "ngo1@example.com", "ngo",
                                   city="Pune", org_name="Helping Hands",
                                   org_reg_id="MH/2020/001")
        self.admin_id = self._mkuser("Admin", "admin@example.com",
                                     "admin", city="Pune")

    def login_as(self, email, pwd="password123"):
        rv = self.client.post("/login", data={"email": email,
                                              "password": pwd},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303),
                      f"login {email} -> {rv.status_code}")
        return rv

    def get_user(self, uid):
        with app.app_context():
            u = db.session.get(models.User, uid)
            return {"id": u.id, "name": u.name, "email": u.email,
                    "role": u.role, "city": u.city,
                    "org_name": u.org_name, "org_reg_id": u.org_reg_id,
                    "hash": u.password_hash}


class TestDisplay(ProfileBase):
    def test_anonymous_redirected(self):
        rv = self.client.get("/profile", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/login", rv.headers.get("Location", ""))

    def test_donor_sees_own_only(self):
        self.login_as("donor1@example.com")
        html = self.client.get("/profile").data.decode()
        self.assertEqual(self.client.get("/profile").status_code, 200)
        self.assertIn("Donor One", html)
        self.assertIn("donor1@example.com", html)
        self.assertIn("donor", html)
        self.assertIn("Pune", html)
        self.assertNotIn("donor2@example.com", html)
        self.assertNotIn("Donor Two", html)
        self.assertNotIn("password_hash", html.lower())

    def test_ngo_sees_org_fields(self):
        self.login_as("ngo1@example.com")
        html = self.client.get("/profile").data.decode()
        self.assertIn("Helping Hands", html)
        self.assertIn("MH/2020/001", html)
        self.assertIn('name="org_name"', html)
        self.assertNotIn("ngo2", html.lower().replace("ngo one", ""))

    def test_admin_view(self):
        self.login_as("admin@example.com")
        html = self.client.get("/profile").data.decode()
        self.assertEqual(self.client.get("/profile").status_code, 200)
        self.assertIn("Admin", html)
        self.assertIn("admin@example.com", html)

    def test_no_per_user_urls(self):
        self.login_as("donor1@example.com")
        for bad in (f"/profile/{self.donor2_id}", "/profile/1",
                    "/profile/donor2@example.com"):
            rv = self.client.get(bad)
            self.assertEqual(rv.status_code, 404, bad)
            self.assertNotIn("donor2@example.com", rv.data.decode())

    def test_no_hash_or_plaintext_leak(self):
        self.login_as("donor1@example.com")
        html = self.client.get("/profile").data.decode()
        self.assertNotIn("pbkdf2", html)
        self.assertNotIn("scrypt", html)
        self.assertNotIn("password123", html)


class TestEditing(ProfileBase):
    def test_donor_updates_name_city(self):
        self.login_as("donor1@example.com")
        rv = self.client.post("/profile",
                              data={"name": "Donor Renamed",
                                    "city": "Nashik"},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        u = self.get_user(self.donor_id)
        self.assertEqual(u["name"], "Donor Renamed")
        self.assertEqual(u["city"], "Nashik")
        html = self.client.get("/profile").data.decode()
        self.assertIn("Donor Renamed", html)

    def test_city_can_be_cleared(self):
        self.login_as("donor1@example.com")
        self.client.post("/profile", data={"name": "Donor One", "city": ""})
        self.assertIsNone(self.get_user(self.donor_id)["city"])

    def test_ngo_updates_org_fields(self):
        self.login_as("ngo1@example.com")
        rv = self.client.post("/profile",
                              data={"name": "NGO Renamed", "city": "Pune",
                                    "org_name": "New Trust",
                                    "org_reg_id": "MH/2022/099"},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        u = self.get_user(self.ngo_id)
        self.assertEqual(u["org_name"], "New Trust")
        self.assertEqual(u["org_reg_id"], "MH/2022/099")

    def test_donor_org_keys_ignored(self):
        self.login_as("donor1@example.com")
        self.client.post("/profile",
                         data={"name": "Donor One", "city": "Pune",
                               "org_name": "Sneaky", "org_reg_id": "X/1"})
        u = self.get_user(self.donor_id)
        self.assertIsNone(u["org_name"])
        self.assertIsNone(u["org_reg_id"])

    def test_role_and_email_immutable(self):
        self.login_as("donor1@example.com")
        rv = self.client.post("/profile",
                              data={"name": "Donor One", "city": "Pune",
                                    "role": "admin",
                                    "email": "hacker@example.com"},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        u = self.get_user(self.donor_id)
        self.assertEqual(u["role"], "donor")
        self.assertEqual(u["email"], "donor1@example.com")

    def test_invalid_inputs_400(self):
        self.login_as("donor1@example.com")
        for bad in ({"name": "A", "city": "Pune"},
                    {"name": "x" * 121, "city": "Pune"},
                    {"name": "Donor One", "city": "y" * 161}):
            rv = self.client.post("/profile", data=bad,
                                  follow_redirects=False)
            self.assertEqual(rv.status_code, 400, bad)
        self.assertEqual(self.get_user(self.donor_id)["name"], "Donor One")
        # NGO org validation (fresh session: already logged in as donor)
        self.client.get("/logout")
        self.login_as("ngo1@example.com")
        rv2 = self.client.post(
            "/profile", data={"name": "NGO One", "city": "Pune",
                              "org_name": "X", "org_reg_id": "Y"},
            follow_redirects=False)
        self.assertEqual(rv2.status_code, 400)

    def test_persist_logout_login(self):
        self.login_as("donor1@example.com")
        self.client.post("/profile",
                         data={"name": "Persisted Name", "city": "Satara"})
        self.client.get("/logout")
        c2 = app.test_client()
        c2.post("/login", data={"email": "donor1@example.com",
                                "password": "password123"})
        html = c2.get("/profile").data.decode()
        self.assertIn("Persisted Name", html)
        self.assertIn("Satara", html)

    def test_cannot_touch_other_profile(self):
        self.login_as("donor1@example.com")
        # No field exists to target another user; attempt smuggling ids.
        self.client.post("/profile",
                         data={"name": "Donor One", "city": "Pune",
                               "id": str(self.donor2_id),
                               "user_id": str(self.donor2_id)})
        other = self.get_user(self.donor2_id)
        self.assertEqual(other["name"], "Donor Two")
        self.assertEqual(other["city"], "Mumbai")

    def test_csrf_rejected(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            client = app.test_client()
            token = _extract_csrf(client.get("/login").data.decode())
            client.post("/login",
                        data={"email": "donor1@example.com",
                              "password": "password123",
                              "csrf_token": token},
                        follow_redirects=False)
            rv = client.post("/profile",
                             data={"name": "Hacked", "city": "Pune"},
                             follow_redirects=False)
            self.assertEqual(rv.status_code, 400)
            self.assertEqual(self.get_user(self.donor_id)["name"],
                             "Donor One")
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


class TestPasswordChange(ProfileBase):
    def test_change_success_and_login(self):
        self.login_as("donor1@example.com")
        rv = self.client.post("/profile/password",
                              data={"current_password": "password123",
                                    "new_password": "brandnew123",
                                    "confirm_password": "brandnew123"},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            self.assertTrue(u.check_password("brandnew123"))
            self.assertFalse(u.check_password("password123"))
            self.assertNotIn("brandnew123", u.password_hash)
        # still logged in (session kept)
        self.assertEqual(self.client.get("/profile").status_code, 200)
        # new password works after relogin
        self.client.get("/logout")
        c2 = app.test_client()
        r2 = c2.post("/login", data={"email": "donor1@example.com",
                                     "password": "brandnew123"},
                     follow_redirects=False)
        self.assertIn(r2.status_code, (302, 303))

    def test_wrong_current_rejected(self):
        self.login_as("donor1@example.com")
        rv = self.client.post("/profile/password",
                              data={"current_password": "wrongpass1",
                                    "new_password": "brandnew123",
                                    "confirm_password": "brandnew123"},
                              follow_redirects=False)
        self.assertEqual(rv.status_code, 400)
        with app.app_context():
            u = db.session.get(models.User, self.donor_id)
            self.assertTrue(u.check_password("password123"))

    def test_mismatch_and_short_rejected(self):
        self.login_as("donor1@example.com")
        r1 = self.client.post(
            "/profile/password",
            data={"current_password": "password123",
                  "new_password": "brandnew123",
                  "confirm_password": "different1"},
            follow_redirects=False)
        self.assertEqual(r1.status_code, 400)
        r2 = self.client.post(
            "/profile/password",
            data={"current_password": "password123",
                  "new_password": "short",
                  "confirm_password": "short"},
            follow_redirects=False)
        self.assertEqual(r2.status_code, 400)

    def test_anonymous_redirected(self):
        rv = self.client.post("/profile/password",
                              data={"current_password": "x",
                                    "new_password": "brandnew123",
                                    "confirm_password": "brandnew123"},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/login", rv.headers.get("Location", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
