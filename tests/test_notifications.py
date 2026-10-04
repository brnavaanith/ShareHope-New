"""ShareHope notification tests — isolated SQLite harness.

Preserves production MySQL (`sharehope` DB) by swapping the
Flask-SQLAlchemy engine to a temp SQLite file for the whole suite.
No production code is modified here; only reads app.py/models.py.

Covers:
  CRUD, unread counts, user isolation, donation events,
  CSRF protection, regression (auth/UI/donation workflow).

Harness notes (no outer app context during requests):
  - Engine is swapped once in setUpClass to a temp SQLite file.
  - Each test rebuilds tables inside a short-lived app context.
  - Only integer ids are stored (never ORM instances) to avoid
    DetachedInstanceError across contexts.
  - All direct DB reads/writes use `with app.app_context():`.
  - Test client requests run with NO outer context held, so
    Flask-Login sees the correct per-request user.
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
    """Point the global app to a temp SQLite file. Returns (path, old_url)."""
    from sqlalchemy import create_engine
    with app.app_context():
        old_engine = db._app_engines[app].get(None)
        old_url = str(old_engine.url) if old_engine is not None else None
        try:
            if old_engine is not None:
                old_engine.dispose()
        except Exception:
            pass
        fd, path = tempfile.mkstemp(prefix="sharehope_notif_test_", suffix=".db")
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
        return path, old_url


def _q_count_notifs(recipient_id, **kw):
    with app.app_context():
        q = models.Notification.query.filter_by(recipient_id=recipient_id, **kw)
        return q.count()


def _q_first_notif(recipient_id, **kw):
    with app.app_context():
        return models.Notification.query.filter_by(
            recipient_id=recipient_id, **kw).first()


def _q_all_notifs(recipient_id):
    with app.app_context():
        return models.Notification.query.filter_by(
            recipient_id=recipient_id).all()


def _q_get(model, ident):
    with app.app_context():
        return db.session.get(model, ident)


class NotificationTestBase(unittest.TestCase):
    _db_path = None

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        if not app.config.get("SECRET_KEY"):
            app.config["SECRET_KEY"] = "test-secret"
        cls._db_path, cls._old_url = _swap_engine_to_sqlite()
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
        # Never touch real SMTP in notification regression tests.
        app.config["MAIL_SUPPRESS_SEND"] = True
        self.client = app.test_client()
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            self._seed_users()

    def tearDown(self):
        app.config["WTF_CSRF_ENABLED"] = False
        with app.app_context():
            try:
                db.session.remove()
            except Exception:
                pass

    # ---------- helpers (each opens its own context) ----------
    def _seed_users(self):
        # called inside an app context from setUp
        donor = models.User(
            name="Donor One", email="donor1@example.com",
            role="donor", city="Pune")
        donor.set_password("donorpass123")
        donor2 = models.User(
            name="Donor Two", email="donor2@example.com",
            role="donor", city="Mumbai")
        donor2.set_password("donorpass123")
        ngo = models.User(
            name="NGO One", email="ngo1@example.com",
            role="ngo", city="Pune",
            org_name="Helping Hands", org_reg_id="MH/2020/001")
        ngo.set_password("ngopass123")
        ngo2 = models.User(
            name="NGO Two", email="ngo2@example.com",
            role="ngo", city="Mumbai",
            org_name="Second Help", org_reg_id="MH/2021/002")
        ngo2.set_password("ngopass123")
        admin = models.User(
            name="Admin", email="admin@example.com", role="admin",
            city="Pune")
        admin.set_password("adminpass123")
        db.session.add_all([donor, donor2, ngo, ngo2, admin])
        db.session.commit()
        self.donor_id = donor.id
        self.donor2_id = donor2.id
        self.ngo_id = ngo.id
        self.ngo2_id = ngo2.id
        self.admin_id = admin.id

    def login(self, email, password):
        return self.client.post(
            "/login", data={"email": email, "password": password},
            follow_redirects=False)

    def login_as(self, user_email, password):
        rv = self.login(user_email, password)
        self.assertIn(rv.status_code, (302, 303),
                      f"login failed for {user_email}: {rv.status_code} {rv.data[:500]}")
        return rv

    def make_notif(self, recipient_id, content="Hello", ntype="info", is_read=False):
        with app.app_context():
            n = models.Notification(
                recipient_id=recipient_id, content=content,
                type=ntype, is_read=is_read)
            db.session.add(n)
            db.session.commit()
            nid = n.id
        return nid

    def get_notif(self, nid):
        with app.app_context():
            n = db.session.get(models.Notification, nid)
            if n is None:
                return None
            # detach values
            return {"id": n.id, "recipient_id": n.recipient_id,
                    "content": n.content, "type": n.type,
                    "is_read": n.is_read}

    def make_offer(self, donor_id=None, title="Books bundle", category="books",
                   location="Pune", status="pending"):
        with app.app_context():
            o = models.DonationOffer(
                donor_id=donor_id or self.donor_id, title=title,
                description="Good condition books for kids, many pages.",
                category=category, quantity="10 books",
                location=location, availability="Flexible", status=status)
            db.session.add(o)
            db.session.commit()
            oid = o.id
        return oid

    def make_requirement(self, ngo_id=None, title="Need books", category="books",
                         location="Pune", urgency="open", status="open"):
        with app.app_context():
            r = models.NGORequirement(
                ngo_id=ngo_id or self.ngo_id, title=title,
                description="Need books for school library urgently.",
                category=category, required_quantity="10 books",
                location=location, urgency=urgency, status=status)
            db.session.add(r)
            db.session.commit()
            rid = r.id
        return rid

    def make_tracking(self, offer_id, req_id, acceptance="pending", handover="pending"):
        from datetime import datetime
        with app.app_context():
            t = models.DonationTracking(
                offer_id=offer_id, requirement_id=req_id,
                acceptance_status=acceptance, handover_status=handover)
            if acceptance == "accepted":
                t.accepted_at = datetime.utcnow()
            if handover == "handed_over":
                t.handed_over_at = datetime.utcnow()
            db.session.add(t)
            db.session.commit()
            tid = t.id
            if acceptance == "accepted":
                offer = db.session.get(models.DonationOffer, offer_id)
                if offer is not None and offer.status == "pending":
                    offer.status = "accepted"
                    db.session.commit()
        return tid


class TestNotificationCRUD(NotificationTestBase):
    def test_notify_creates_and_dedups(self):
        from app import _notify
        with app.app_context():
            _notify(self.donor_id, "info", "Welcome")
            db.session.commit()
        self.assertEqual(_q_count_notifs(self.donor_id), 1)
        with app.app_context():
            _notify(self.donor_id, "info", "Welcome")
            db.session.commit()
        self.assertEqual(_q_count_notifs(self.donor_id), 1)
        with app.app_context():
            _notify(self.donor_id, "info", "Something else")
            db.session.commit()
        self.assertEqual(_q_count_notifs(self.donor_id), 2)
        with app.app_context():
            _notify(self.donor_id, "accept", "Welcome")
            db.session.commit()
        self.assertEqual(_q_count_notifs(self.donor_id), 3)

    def test_notify_ignores_empty_and_none(self):
        from app import _notify
        with app.app_context():
            _notify(None, "info", "Hi")
            _notify(self.donor_id, "info", "   ")
            _notify(self.donor_id, "info", "")
            _notify(0, "info", "Hi")
            db.session.commit()
        self.assertEqual(_q_count_notifs(self.donor_id), 0)

    def test_notify_truncates_to_500(self):
        from app import _notify
        with app.app_context():
            _notify(self.donor_id, "info", "x" * 800)
            db.session.commit()
        with app.app_context():
            n = models.Notification.query.filter_by(
                recipient_id=self.donor_id).first()
            self.assertIsNotNone(n)
            self.assertLessEqual(len(n.content), 500)
            self.assertEqual(len(n.content), 500)

    def test_list_shows_only_own_ordering(self):
        self.make_notif(self.donor_id, "First")
        self.make_notif(self.donor_id, "Second")
        self.make_notif(self.ngo_id, "Other user")
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.get("/notifications")
        self.assertEqual(rv.status_code, 200)
        html = rv.data.decode()
        self.assertIn("First", html)
        self.assertIn("Second", html)
        self.assertNotIn("Other user", html)

    def test_filter_all_unread_read_and_counts(self):
        self.make_notif(self.donor_id, "U1", is_read=False)
        self.make_notif(self.donor_id, "U2", is_read=False)
        self.make_notif(self.donor_id, "R1", is_read=True)
        self.login_as("donor1@example.com", "donorpass123")
        html_all = self.client.get("/notifications?show=all").data.decode()
        self.assertIn("U1", html_all)
        self.assertIn("R1", html_all)
        self.assertIn("All (3)", html_all)
        self.assertIn("Unread (2)", html_all)
        self.assertIn("Read (1)", html_all)
        html_un = self.client.get("/notifications?show=unread").data.decode()
        self.assertIn("U1", html_un)
        self.assertNotIn("R1", html_un)
        html_re = self.client.get("/notifications?show=read").data.decode()
        self.assertNotIn("U1", html_re)
        self.assertIn("R1", html_re)

    def test_invalid_show_defaults_to_all(self):
        self.make_notif(self.donor_id, "Hello")
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.get("/notifications?show=bogus")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("Hello", rv.data.decode())

    def test_mark_single_read(self):
        nid = self.make_notif(self.donor_id, "To read", is_read=False)
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post(f"/notifications/{nid}/read",
                              data={"show": "unread"}, follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("show=unread", rv.headers.get("Location", ""))
        self.assertTrue(self.get_notif(nid)["is_read"])
        rv2 = self.client.post(f"/notifications/{nid}/read",
                               data={"show": "all"}, follow_redirects=False)
        self.assertIn(rv2.status_code, (302, 303))
        self.assertTrue(self.get_notif(nid)["is_read"])

    def test_mark_single_read_preserves_row(self):
        nid = self.make_notif(self.donor_id, "Keep me")
        self.login_as("donor1@example.com", "donorpass123")
        self.client.post(f"/notifications/{nid}/read", data={})
        self.assertIsNotNone(self.get_notif(nid))

    def test_mark_all_read(self):
        self.make_notif(self.donor_id, "A", is_read=False)
        self.make_notif(self.donor_id, "B", is_read=False)
        self.make_notif(self.donor_id, "C", is_read=True)
        self.make_notif(self.ngo_id, "Other", is_read=False)
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post("/notifications/read-all", data={},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            mine = models.Notification.query.filter_by(
                recipient_id=self.donor_id).all()
            self.assertTrue(all(x.is_read for x in mine))
            other = models.Notification.query.filter_by(
                recipient_id=self.ngo_id).first()
            self.assertFalse(other.is_read)

    def test_unknown_id_404(self):
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post("/notifications/999999/read", data={},
                              follow_redirects=False)
        self.assertEqual(rv.status_code, 404)


class TestUnreadCounts(NotificationTestBase):
    def test_anonymous_gets_zero_no_badge(self):
        rv = self.client.get("/")
        self.assertEqual(rv.status_code, 200)
        self.assertNotIn("notif-badge", rv.data.decode())

    def test_badge_reflects_unread(self):
        self.make_notif(self.donor_id, "A", is_read=False)
        self.make_notif(self.donor_id, "B", is_read=False)
        self.make_notif(self.donor_id, "C", is_read=True)
        self.login_as("donor1@example.com", "donorpass123")
        html = self.client.get("/").data.decode()
        self.assertIn("notif-badge", html)
        # badge shows 2 (unread). Check surrounding badge markup.
        self.assertRegex(html, r'notif-badge[^>]*>\s*2\s*<')
        # mark one read -> badge 1
        with app.app_context():
            n = models.Notification.query.filter_by(
                recipient_id=self.donor_id, is_read=False).first()
            nid = n.id
        self.client.post(f"/notifications/{nid}/read", data={})
        html2 = self.client.get("/").data.decode()
        self.assertIn("notif-badge", html2)
        self.assertRegex(html2, r'notif-badge[^>]*>\s*1\s*<')

    def test_badge_hidden_when_zero(self):
        self.make_notif(self.donor_id, "A", is_read=True)
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.get("/")
        self.assertNotIn("notif-badge", rv.data.decode())

    def test_mark_all_button_only_when_unread(self):
        self.make_notif(self.donor_id, "A", is_read=False)
        self.login_as("donor1@example.com", "donorpass123")
        self.assertIn("Mark all as read",
                      self.client.get("/notifications").data.decode())
        self.client.post("/notifications/read-all", data={})
        self.assertNotIn("Mark all as read",
                         self.client.get("/notifications").data.decode())

    def test_context_processor_values(self):
        with app.test_request_context("/"):
            from app import inject_unread_count
            val = inject_unread_count()
            self.assertEqual(val, {"unread_count": 0})
        self.make_notif(self.donor_id, "X", is_read=False)
        self.make_notif(self.donor_id, "Y", is_read=False)
        self.login_as("donor1@example.com", "donorpass123")
        self.assertIn("Unread (2)",
                      self.client.get("/notifications").data.decode())


class TestIsolation(NotificationTestBase):
    def test_anonymous_redirects(self):
        rv = self.client.get("/notifications", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/login", rv.headers.get("Location", ""))
        rv2 = self.client.post("/notifications/1/read", data={},
                               follow_redirects=False)
        self.assertIn(rv2.status_code, (302, 303))
        self.assertIn("/login", rv2.headers.get("Location", ""))
        rv3 = self.client.post("/notifications/read-all", data={},
                               follow_redirects=False)
        self.assertIn(rv3.status_code, (302, 303))
        self.assertIn("/login", rv3.headers.get("Location", ""))

    def test_cannot_see_others(self):
        self.make_notif(self.donor_id, "Donor1 secret")
        self.make_notif(self.donor2_id, "Donor2 secret")
        self.login_as("donor1@example.com", "donorpass123")
        html = self.client.get("/notifications").data.decode()
        self.assertIn("Donor1 secret", html)
        self.assertNotIn("Donor2 secret", html)

    def test_cannot_mark_others_read(self):
        other_id = self.make_notif(self.donor2_id, "чужой", is_read=False)
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post(f"/notifications/{other_id}/read", data={},
                              follow_redirects=False)
        self.assertEqual(rv.status_code, 404)
        self.assertFalse(self.get_notif(other_id)["is_read"])

    def test_all_roles_can_access_own(self):
        for email, pwd in [("donor1@example.com", "donorpass123"),
                           ("ngo1@example.com", "ngopass123"),
                           ("admin@example.com", "adminpass123")]:
            client = app.test_client()
            rv_login = client.post("/login", data={"email": email, "password": pwd},
                                   follow_redirects=False)
            self.assertIn(rv_login.status_code, (302, 303))
            rv = client.get("/notifications")
            self.assertEqual(rv.status_code, 200, f"failed for {email}")


class TestDonationEvents(NotificationTestBase):
    def test_offer_new_notifies_matching_ngo(self):
        self.make_requirement(ngo_id=self.ngo_id, title="Need books",
                              category="books", location="Pune")
        self.make_requirement(ngo_id=self.ngo2_id, title="Need food",
                              category="food", location="Mumbai")
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post("/offers/new", data={
            "title": "Books bundle",
            "category": "books",
            "description": "Good condition textbooks for school kids.",
            "quantity": "20 books",
            "location": "Pune",
            "availability": "Flexible"}, follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            ngo_notes = models.Notification.query.filter_by(
                recipient_id=self.ngo_id).all()
            self.assertTrue(any("Books bundle" in n.content for n in ngo_notes),
                            f"ngo notes: {[n.content for n in ngo_notes]}")
            self.assertTrue(all(n.type == "need" for n in ngo_notes))
            ngo2_notes = models.Notification.query.filter_by(
                recipient_id=self.ngo2_id).all()
            self.assertEqual(len(ngo2_notes), 0)

    def test_requirement_new_notifies_matching_donor(self):
        self.make_offer(donor_id=self.donor_id, title="Old books",
                        category="books", location="Pune")
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.post("/requirements/new", data={
            "title": "Need books urgently",
            "category": "books",
            "description": "School library needs textbooks soon.",
            "required_quantity": "15 books",
            "location": "Pune",
            "urgency": "urgent",
            "deadline": ""}, follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            d1 = models.Notification.query.filter_by(
                recipient_id=self.donor_id).all()
            self.assertTrue(any("Need books urgently" in n.content for n in d1))
            d2 = models.Notification.query.filter_by(
                recipient_id=self.donor2_id).all()
            self.assertEqual(len(d2), 0)

    def test_accept_notifies_donor(self):
        offer_id = self.make_offer(title="Warm clothes")
        with app.app_context():
            offer = db.session.get(models.DonationOffer, offer_id)
            offer.category = "books"
            db.session.commit()
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need clothes",
                description="Need clothes for shelter, many items needed.",
                category="books", required_quantity="5",
                location="Pune", urgency="open", status="open")
            db.session.add(req)
            db.session.commit()
            req_id = req.id
        track_id = self.make_tracking(offer_id, req_id, acceptance="pending")
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.post(f"/tracking/{track_id}/accept", data={},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            notes = models.Notification.query.filter_by(
                recipient_id=self.donor_id).all()
            self.assertTrue(any(n.type == "accept" and "Warm clothes" in n.content
                                for n in notes),
                            f"notes: {[(n.type, n.content) for n in notes]}")
            self.assertEqual(
                db.session.get(models.DonationOffer, offer_id).status, "accepted")

    def test_decline_notifies_donor_offer_stays_pending(self):
        with app.app_context():
            offer = models.DonationOffer(
                donor_id=self.donor_id, title="Old toys",
                description="Clean toys and games for children.",
                category="toys", quantity="5",
                location="Pune", availability="Flexible", status="pending")
            db.session.add(offer)
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need toys",
                description="Need toys for children shelter home.",
                category="toys", required_quantity="5",
                location="Pune", urgency="open", status="open")
            db.session.add(req)
            db.session.commit()
            offer_id, req_id = offer.id, req.id
        track_id = self.make_tracking(offer_id, req_id, acceptance="pending")
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.post(f"/tracking/{track_id}/decline", data={},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            notes = models.Notification.query.filter_by(
                recipient_id=self.donor_id).all()
            self.assertTrue(any(n.type == "info" and "Old toys" in n.content
                                for n in notes))
            self.assertEqual(
                db.session.get(models.DonationOffer, offer_id).status, "pending")
            self.assertEqual(
                db.session.get(models.DonationTracking, track_id).acceptance_status,
                "declined")

    def test_decide_accept_and_decline(self):
        with app.app_context():
            offer = models.DonationOffer(
                donor_id=self.donor_id, title="Furniture set",
                description="Sturdy tables and chairs for classroom.",
                category="furniture", quantity="4",
                location="Pune", availability="Flexible", status="pending")
            db.session.add(offer)
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need furniture",
                description="Classroom needs tables and chairs urgently.",
                category="furniture", required_quantity="4",
                location="Pune", urgency="open", status="open")
            db.session.add(req)
            db.session.commit()
            offer_id, req_id = offer.id, req.id
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.post(f"/offers/{offer_id}/decide",
                              data={"requirement_id": str(req_id),
                                    "action": "accept"},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            self.assertEqual(
                db.session.get(models.DonationOffer, offer_id).status, "accepted")
            notes = models.Notification.query.filter_by(
                recipient_id=self.donor_id).all()
            self.assertTrue(any(n.type == "accept" for n in notes))
        with app.app_context():
            offer2 = models.DonationOffer(
                donor_id=self.donor_id, title="Shoes lot",
                description="Paired wearable shoes, many sizes.",
                category="furniture", quantity="10",
                location="Pune", availability="Flexible", status="pending")
            db.session.add(offer2)
            db.session.commit()
            offer2_id = offer2.id
        rv2 = self.client.post(f"/offers/{offer2_id}/decide",
                               data={"requirement_id": str(req_id),
                                     "action": "decline"},
                               follow_redirects=False)
        self.assertIn(rv2.status_code, (302, 303))
        with app.app_context():
            notes2 = models.Notification.query.filter_by(
                recipient_id=self.donor_id).all()
            self.assertTrue(any("Shoes lot" in n.content for n in notes2))

    def test_handover_notifies_ngo(self):
        with app.app_context():
            offer = models.DonationOffer(
                donor_id=self.donor_id, title="Laptop",
                description="Working laptop with charger, good battery.",
                category="electronics", quantity="1",
                location="Pune", availability="Flexible", status="pending")
            db.session.add(offer)
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need laptop",
                description="Training centre needs a laptop urgently.",
                category="electronics", required_quantity="1",
                location="Pune", urgency="open", status="open")
            db.session.add(req)
            db.session.commit()
            offer_id, req_id = offer.id, req.id
        track_id = self.make_tracking(offer_id, req_id, acceptance="accepted")
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post(f"/tracking/{track_id}/handover", data={},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            ngo_notes = models.Notification.query.filter_by(
                recipient_id=self.ngo_id).all()
            self.assertTrue(any(n.type == "handover" and "Laptop" in n.content
                                for n in ngo_notes))

    def test_complete_notifies_both(self):
        with app.app_context():
            offer = models.DonationOffer(
                donor_id=self.donor_id, title="Blankets",
                description="Warm blankets, washed and packed.",
                category="home", quantity="10",
                location="Pune", availability="Flexible", status="pending")
            db.session.add(offer)
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need blankets",
                description="Shelter needs blankets for winter.",
                category="home", required_quantity="10",
                location="Pune", urgency="open", status="open")
            db.session.add(req)
            db.session.commit()
            offer_id, req_id = offer.id, req.id
        track_id = self.make_tracking(offer_id, req_id, acceptance="accepted",
                                      handover="handed_over")
        with app.app_context():
            offer = db.session.get(models.DonationOffer, offer_id)
            offer.status = "accepted"
            db.session.commit()
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.post(f"/tracking/{track_id}/complete", data={},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        with app.app_context():
            self.assertEqual(
                db.session.get(models.DonationOffer, offer_id).status,
                "handed_over")
            donor_notes = models.Notification.query.filter_by(
                recipient_id=self.donor_id).all()
            ngo_notes = models.Notification.query.filter_by(
                recipient_id=self.ngo_id).all()
            self.assertTrue(any("Blankets" in n.content for n in donor_notes))
            self.assertTrue(any("Blankets" in n.content for n in ngo_notes))

    def test_no_duplicate_notification_on_double_accept(self):
        with app.app_context():
            offer = models.DonationOffer(
                donor_id=self.donor_id, title="Dup test",
                description="Duplicate test item, good condition.",
                category="books", quantity="1",
                location="Pune", availability="Flexible", status="pending")
            db.session.add(offer)
            req = models.NGORequirement(
                ngo_id=self.ngo_id, title="Need dup",
                description="Need duplicate test item urgently.",
                category="books", required_quantity="1",
                location="Pune", urgency="open", status="open")
            db.session.add(req)
            db.session.commit()
            offer_id, req_id = offer.id, req.id
        track_id = self.make_tracking(offer_id, req_id, acceptance="pending")
        self.login_as("ngo1@example.com", "ngopass123")
        self.client.post(f"/tracking/{track_id}/accept", data={})
        count1 = _q_count_notifs(self.donor_id)
        rv2 = self.client.post(f"/tracking/{track_id}/accept", data={},
                               follow_redirects=False)
        self.assertIn(rv2.status_code, (302, 303))
        count2 = _q_count_notifs(self.donor_id)
        self.assertEqual(count1, count2)


class TestCSRF(NotificationTestBase):
    def test_forms_contain_csrf_when_enabled(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            # login page always carries a token when CSRF is on
            rv_login = self.client.get("/login")
            self.assertEqual(rv_login.status_code, 200)
            self.assertIsNotNone(
                _extract_csrf(rv_login.data.decode()),
                "login page missing csrf_token when CSRF enabled")
            # notifications page carries tokens for read actions
            # login first (with token) then check notifications page
            token_login = _extract_csrf(rv_login.data.decode())
            self.client.post("/login",
                             data={"email": "donor1@example.com",
                                   "password": "donorpass123",
                                   "csrf_token": token_login},
                             follow_redirects=False)
            # seed one unread so the per-row form renders
            self.make_notif(self.donor_id, "CSRF row", is_read=False)
            rv = self.client.get("/notifications")
            html = rv.data.decode()
            self.assertIn('name="csrf_token"', html)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_post_without_token_rejected(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            client = app.test_client()
            token = _extract_csrf(client.get("/login").data.decode())
            self.assertIsNotNone(token)
            rv_login = client.post(
                "/login",
                data={"email": "donor1@example.com",
                      "password": "donorpass123",
                      "csrf_token": token},
                follow_redirects=False)
            self.assertIn(rv_login.status_code, (302, 303))
            nid = self.make_notif(self.donor_id, "CSRF test", is_read=False)
            rv = client.post(f"/notifications/{nid}/read", data={},
                             follow_redirects=False)
            self.assertEqual(rv.status_code, 400)
            self.assertFalse(self.get_notif(nid)["is_read"])
            rv2 = client.post("/notifications/read-all", data={},
                              follow_redirects=False)
            self.assertEqual(rv2.status_code, 400)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_post_with_valid_token_succeeds(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            client = app.test_client()
            token_login = _extract_csrf(client.get("/login").data.decode())
            self.assertIsNotNone(token_login)
            client.post("/login",
                        data={"email": "donor1@example.com",
                              "password": "donorpass123",
                              "csrf_token": token_login},
                        follow_redirects=False)
            nid = self.make_notif(self.donor_id, "CSRF ok", is_read=False)
            token = _extract_csrf(client.get("/notifications").data.decode())
            self.assertIsNotNone(token)
            rv = client.post(f"/notifications/{nid}/read",
                             data={"csrf_token": token, "show": "all"},
                             follow_redirects=False)
            self.assertIn(rv.status_code, (302, 303))
            self.assertTrue(self.get_notif(nid)["is_read"])
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


class TestRegression(NotificationTestBase):
    def test_health(self):
        rv = self.client.get("/health")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("ok", rv.data.decode())

    def test_auth_still_works(self):
        rv = self.client.post("/register", data={
            "role": "donor", "name": "New User",
            "email": "newuser@example.com", "city": "Pune",
            "org": "", "regid": "",
            "password": "newpass123", "confirm": "newpass123"},
            follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.client.get("/logout")
        rv2 = self.client.post("/login", data={
            "email": "newuser@example.com", "password": "newpass123"},
            follow_redirects=False)
        self.assertIn(rv2.status_code, (302, 303))
        self.client.get("/logout")
        client2 = app.test_client()
        rv3 = client2.post("/login", data={
            "email": "newuser@example.com", "password": "wrongpass"},
            follow_redirects=False)
        self.assertEqual(rv3.status_code, 401)
        rv4 = client2.post("/register", data={
            "role": "donor", "name": "Dup",
            "email": "newuser@example.com", "city": "Pune",
            "password": "newpass123", "confirm": "newpass123"},
            follow_redirects=False)
        self.assertEqual(rv4.status_code, 409)

    def test_offer_validation_preserved(self):
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.post("/offers/new", data={
            "title": "AB", "category": "books",
            "description": "Good condition textbooks for school kids.",
            "quantity": "5", "location": "Pune", "availability": "Flexible"},
            follow_redirects=False)
        self.assertEqual(rv.status_code, 400)
        rv2 = self.client.post("/offers/new", data={
            "title": "Valid title", "category": "bogus",
            "description": "Good condition textbooks for school kids.",
            "quantity": "5", "location": "Pune", "availability": "Flexible"},
            follow_redirects=False)
        self.assertEqual(rv2.status_code, 400)

    def test_requirement_validation_preserved(self):
        self.login_as("ngo1@example.com", "ngopass123")
        rv = self.client.post("/requirements/new", data={
            "title": "AB", "category": "books",
            "description": "Need books for library urgently here.",
            "required_quantity": "5", "location": "Pune",
            "urgency": "urgent", "deadline": ""},
            follow_redirects=False)
        self.assertEqual(rv.status_code, 400)

    def test_dashboard_role_gating(self):
        self.login_as("donor1@example.com", "donorpass123")
        rv = self.client.get("/ngo", follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/donor", rv.headers.get("Location", ""))
        client2 = app.test_client()
        client2.post("/login", data={"email": "ngo1@example.com",
                                     "password": "ngopass123"})
        rv2 = client2.get("/donor", follow_redirects=False)
        self.assertIn(rv2.status_code, (302, 303))

    def test_tracking_pages_load(self):
        self.login_as("donor1@example.com", "donorpass123")
        self.assertEqual(self.client.get("/tracking").status_code, 200)
        client2 = app.test_client()
        client2.post("/login", data={"email": "ngo1@example.com",
                                     "password": "ngopass123"})
        self.assertEqual(client2.get("/tracking").status_code, 200)

    def test_empty_states(self):
        self.login_as("donor1@example.com", "donorpass123")
        html_all = self.client.get("/notifications?show=all").data.decode()
        self.assertIn("No notifications yet", html_all)
        html_un = self.client.get("/notifications?show=unread").data.decode()
        self.assertIn("All caught up", html_un)
        html_re = self.client.get("/notifications?show=read").data.decode()
        self.assertIn("Nothing read yet", html_re)

    def test_donation_workflow_end_to_end(self):
        self.login_as("donor1@example.com", "donorpass123")
        self.client.post("/offers/new", data={
            "title": "E2E books", "category": "books",
            "description": "Complete set of textbooks, good condition.",
            "quantity": "30 books", "location": "Pune",
            "availability": "Weekends"})
        with app.app_context():
            offer = models.DonationOffer.query.filter_by(
                title="E2E books").first()
            self.assertIsNotNone(offer)
            offer_id = offer.id
        self.client.get("/logout")
        self.login_as("ngo1@example.com", "ngopass123")
        self.client.post("/requirements/new", data={
            "title": "E2E need books", "category": "books",
            "description": "Library needs full textbook set urgently.",
            "required_quantity": "30 books", "location": "Pune",
            "urgency": "urgent", "deadline": ""})
        with app.app_context():
            req = models.NGORequirement.query.filter_by(
                title="E2E need books").first()
            self.assertIsNotNone(req)
            req_id = req.id
        rv = self.client.post(f"/offers/{offer_id}/decide",
                              data={"requirement_id": str(req_id),
                                    "action": "accept"})
        self.assertIn(rv.status_code, (200, 302, 303))
        with app.app_context():
            self.assertEqual(
                db.session.get(models.DonationOffer, offer_id).status,
                "accepted")
            track = models.DonationTracking.query.filter_by(
                offer_id=offer_id, requirement_id=req_id).first()
            self.assertIsNotNone(track)
            track_id = track.id
        self.client.get("/logout")
        self.login_as("donor1@example.com", "donorpass123")
        rv_h = self.client.post(f"/tracking/{track_id}/handover", data={})
        self.assertIn(rv_h.status_code, (200, 302, 303))
        self.client.get("/logout")
        self.login_as("ngo1@example.com", "ngopass123")
        rv_c = self.client.post(f"/tracking/{track_id}/complete", data={})
        self.assertIn(rv_c.status_code, (200, 302, 303))
        with app.app_context():
            self.assertEqual(
                db.session.get(models.DonationOffer, offer_id).status,
                "handed_over")


if __name__ == "__main__":
    unittest.main(verbosity=2)
