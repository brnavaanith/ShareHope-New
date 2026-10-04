"""ShareHope analytics tests — isolated SQLite harness.

Never touches production MySQL. Swaps the global Flask-SQLAlchemy
engine to a temp SQLite file (same pattern as the other suites).
Suppresses real SMTP. All data created per-test with explicit
created_at values so date filters are deterministic.

Covers: aggregation accuracy, status counts, category counts,
date filters (incl. empty periods → zero), empty database,
role-based access (personal/admin blocks, no PII leak),
invalid filters, no-double-count.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app import app, db
import models


def _swap_engine_to_sqlite():
    from sqlalchemy import create_engine
    with app.app_context():
        old_engine = db._app_engines[app].get(None)
        try:
            if old_engine is not None:
                old_engine.dispose()
        except Exception:
            pass
        fd, path = tempfile.mkstemp(prefix="sharehope_analytics_test_",
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


class AnalyticsBase(unittest.TestCase):
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

    # ---------- fixtures ----------
    def _mkuser(self, name, email, role, city="Pune", at=None, **kw):
        u = models.User(name=name, email=email, role=role, city=city,
                        **kw)
        u.set_password("password123")
        if at is not None:
            u.created_at = at
        db.session.add(u)
        db.session.commit()
        return u.id

    def _mkoffer(self, donor_id, title, category="books",
                 status="pending", at=None):
        o = models.DonationOffer(
            donor_id=donor_id, title=title,
            description="Good condition items for donation here.",
            category=category, quantity="5", location="Pune",
            availability="Flexible", status=status)
        if at is not None:
            o.created_at = at
        db.session.add(o)
        db.session.commit()
        return o.id

    def _mkreq(self, ngo_id, title, category="books", status="open",
               at=None):
        r = models.NGORequirement(
            ngo_id=ngo_id, title=title,
            description="Need items for the shelter home soon.",
            category=category, required_quantity="5",
            location="Pune", urgency="open", status=status)
        if at is not None:
            r.created_at = at
        db.session.add(r)
        db.session.commit()
        return r.id

    def _seed(self):
        now = datetime.utcnow()
        self.donor_id = self._mkuser("Donor One", "donor1@example.com",
                                     "donor", at=now - timedelta(days=40))
        self.donor2_id = self._mkuser("Donor Two", "donor2@example.com",
                                      "donor", city="Mumbai",
                                      at=now - timedelta(days=10))
        self.ngo_id = self._mkuser("NGO One", "ngo1@example.com", "ngo",
                                   at=now - timedelta(days=40),
                                   org_name="Helping Hands",
                                   org_reg_id="MH/2020/001")
        self.admin_id = self._mkuser("Admin", "admin@example.com",
                                     "admin", at=now - timedelta(days=40))
        # Offers: one per status, spread across months.
        self._mkoffer(self.donor_id, "Pending books", "books",
                      "pending", now - timedelta(days=5))
        self._mkoffer(self.donor_id, "Accepted food", "food",
                      "accepted", now - timedelta(days=35))
        self._mkoffer(self.donor2_id, "Handed blankets", "home",
                      "handed_over", now - timedelta(days=65))
        self._mkoffer(self.donor2_id, "Declined toys", "toys",
                      "declined", now - timedelta(days=65))
        self._mkreq(self.ngo_id, "Need books", "books", "open",
                    now - timedelta(days=5))

    def login_as(self, email):
        rv = self.client.post("/login",
                              data={"email": email, "password": "password123"},
                              follow_redirects=False)
        self.assertIn(rv.status_code, (302, 303))
        return rv

    def get_json(self, path):
        rv = self.client.get(path)
        self.assertEqual(rv.status_code, 200, path)
        return rv.get_json()


class TestOverview(AnalyticsBase):
    def test_overview_cards_match_records(self):
        j = self.get_json("/analytics/data")
        self.assertTrue(j["ok"])
        ov = j["overview"]
        self.assertEqual(ov["total_offers"], 4)
        self.assertEqual(ov["completed"], 1)
        self.assertEqual(ov["pending"], 1)
        self.assertEqual(ov["donors"], 2)
        self.assertEqual(ov["active_ngos"], 1)
        self.assertEqual(ov["completion_rate"], 25.0)

    def test_completion_rate_zero_when_empty(self):
        with app.app_context():
            models.DonationOffer.query.delete()
            db.session.commit()
        j = self.get_json("/analytics/data")
        self.assertEqual(j["overview"]["total_offers"], 0)
        self.assertEqual(j["overview"]["completion_rate"], 0.0)

    def test_page_renders_live_cards(self):
        html = self.client.get("/analytics").data.decode()
        self.assertEqual(self.client.get("/analytics").status_code, 200)
        self.assertNotIn("illustrative sample", html.lower())
        self.assertIn("Total donation offers", html)
        self.assertIn("Completion rate", html)
        # server-rendered live totals present
        self.assertIn(">4<", html)


class TestBreakdowns(AnalyticsBase):
    def test_status_counts_exactly_once(self):
        j = self.get_json("/analytics/data")
        counts = {s["status"]: s["count"] for s in j["by_status"]}
        self.assertEqual(counts, {"pending": 1, "accepted": 1,
                                  "handed_over": 1, "declined": 1})
        self.assertEqual(sum(counts.values()), 4)

    def test_category_counts_all_slugs(self):
        j = self.get_json("/analytics/data")
        cats = {c["slug"]: c["offers"] for c in j["by_category"]}
        self.assertEqual(len(cats), 8)
        for slug in ("books", "food", "clothing", "electronics",
                     "toys", "home", "furniture", "shoes"):
            self.assertIn(slug, cats)
        self.assertEqual(cats["books"], 1)
        self.assertEqual(cats["food"], 1)
        self.assertEqual(cats["home"], 1)
        self.assertEqual(cats["toys"], 1)
        self.assertEqual(cats["clothing"], 0)
        self.assertEqual(sum(cats.values()), 4)

    def test_user_activity_no_pii(self):
        j = self.get_json("/analytics/data")
        self.assertEqual(j["users"]["donors"], 2)
        self.assertEqual(j["users"]["ngos"], 1)
        blob = str(j)
        for leak in ("donor1@example.com", "ngo1@example.com", "Donor One"):
            self.assertNotIn(leak, blob)

    def test_trends_sum_matches_total(self):
        j = self.get_json("/analytics/data?gran=monthly")
        self.assertEqual(sum(t["offers"] for t in j["trends"]), 4)
        self.assertEqual(sum(t["completed"] for t in j["trends"]), 1)

    def test_empty_periods_are_zero(self):
        # Narrow window around only the recent offer: other buckets zero.
        now = datetime.utcnow().date()
        frm = (now - timedelta(days=7)).isoformat()
        to = now.isoformat()
        j = self.get_json(
            f"/analytics/data?from={frm}&to={to}&gran=daily")
        self.assertEqual(sum(t["offers"] for t in j["trends"]), 1)
        zeros = [t for t in j["trends"] if t["offers"] == 0]
        self.assertTrue(len(zeros) >= 6)
        for t in j["trends"]:
            self.assertIn("bucket", t)
            self.assertIn("label", t)


class TestFilters(AnalyticsBase):
    def test_date_filter_scopes_everything(self):
        now = datetime.utcnow().date()
        frm = (now - timedelta(days=20)).isoformat()
        to = now.isoformat()
        j = self.get_json(f"/analytics/data?from={frm}&to={to}&gran=monthly")
        self.assertEqual(j["overview"]["total_offers"], 1)
        self.assertEqual(j["filters"]["gran"], "monthly")
        cats = {c["slug"]: c["offers"] for c in j["by_category"]}
        self.assertEqual(sum(cats.values()), 1)
        self.assertEqual(sum(t["offers"] for t in j["trends"]), 1)

    def test_gran_variants(self):
        for gran in ("daily", "weekly", "monthly"):
            j = self.get_json(f"/analytics/data?gran={gran}")
            self.assertEqual(j["filters"]["gran"], gran)
            self.assertEqual(sum(t["offers"] for t in j["trends"]), 4)

    def test_invalid_filters_json_400(self):
        for bad in ("/analytics/data?from=not-a-date",
                    "/analytics/data?to=2026-13-99",
                    "/analytics/data?gran=yearly",
                    "/analytics/data?from=2026-05-01&to=2026-01-01",
                    "/analytics/data?from=2020-01-01&to=2026-12-31"):
            rv = self.client.get(bad)
            self.assertEqual(rv.status_code, 400, bad)
            self.assertFalse(rv.get_json()["ok"])

    def test_invalid_filters_page_forgiving(self):
        rv = self.client.get("/analytics?gran=bogus")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("Total donation offers", rv.data.decode())

    def test_cards_and_charts_agree(self):
        now = datetime.utcnow().date()
        frm = (now - timedelta(days=20)).isoformat()
        to = now.isoformat()
        html = self.client.get(
            f"/analytics?from={frm}&to={to}&gran=weekly").data.decode()
        j = self.get_json(
            f"/analytics/data?from={frm}&to={to}&gran=weekly")
        self.assertIn(f">{j['overview']['total_offers']}<", html)
        self.assertIn(f"{frm} → {to} · weekly", html)


class TestRoles(AnalyticsBase):
    def test_anonymous_no_personal_no_admin(self):
        j = self.get_json("/analytics/data")
        self.assertIsNone(j["personal"])
        self.assertNotIn("admin", j)
        html = self.client.get("/analytics").data.decode()
        self.assertEqual(self.client.get("/analytics").status_code, 200)

    def test_donor_sees_only_own(self):
        self.login_as("donor1@example.com")
        j = self.get_json("/analytics/data")
        p = j["personal"]
        self.assertEqual(p["kind"], "donor")
        self.assertEqual(p["total_offers"], 2)
        self.assertNotIn("admin", j)
        blob = str(p)
        self.assertNotIn("donor2@example.com", blob)
        # NGO requirement internals not in donor personal block
        self.assertNotIn("total_requirements", blob)

    def test_ngo_sees_only_own(self):
        self.login_as("ngo1@example.com")
        j = self.get_json("/analytics/data")
        p = j["personal"]
        self.assertEqual(p["kind"], "ngo")
        self.assertEqual(p["total_requirements"], 1)
        self.assertNotIn("admin", j)

    def test_admin_block_only_for_admin(self):
        self.login_as("donor1@example.com")
        self.assertNotIn("admin", self.get_json("/analytics/data"))
        c2 = app.test_client()
        c2.post("/login", data={"email": "admin@example.com",
                                "password": "password123"})
        j = c2.get("/analytics/data").get_json()
        self.assertIn("admin", j)
        self.assertIn("messages", j["admin"])
        self.assertIn("notifications", j["admin"])

    def test_empty_database(self):
        with app.app_context():
            models.DonationTracking.query.delete()
            models.DonationHistory.query.delete()
            models.Message.query.delete()
            models.Notification.query.delete()
            models.DonationOffer.query.delete()
            models.NGORequirement.query.delete()
            models.User.query.delete()
            db.session.commit()
        html = self.client.get("/analytics").data.decode()
        self.assertIn("No donations in this range", html)
        j = self.get_json("/analytics/data")
        self.assertEqual(j["overview"]["total_offers"], 0)
        self.assertEqual(j["users"]["donors"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
