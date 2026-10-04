"""Regression tests: Impact Tracker removal + admin-controlled Home slip.

Isolated SQLite, never touches MySQL. CSRF is disabled in this suite
(like the other project suites); a dedicated test re-enables it to prove
the admin action stays protected.
"""
import os
import re
import tempfile
import unittest

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
        fd, path = tempfile.mkstemp(prefix="sharehope_featured_", suffix=".db")
        os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass
        db._app_engines[app][None] = create_engine(f"sqlite:///{path}")
        try:
            db.session.remove()
        except Exception:
            pass
        return path


class Base(unittest.TestCase):
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
        donor = models.User(name="Donor", email="donor@t.com", role="donor",
                            city="Pune")
        donor.set_password("donorpass123")
        ngo = models.User(name="NGO", email="ngo@t.com", role="ngo", city="Pune",
                          org_name="Trust", org_reg_id="R/1")
        ngo.set_password("ngopass123")
        admin = models.User(name="Admin", email="admin@t.com", role="admin",
                            city="Pune")
        admin.set_password("adminpass123")
        db.session.add_all([donor, ngo, admin])
        db.session.commit()
        self.donor_id, self.ngo_id, self.admin_id = donor.id, ngo.id, admin.id
        self.offer_a = models.DonationOffer(
            donor_id=donor.id, title="Alpha books",
            description="Good condition textbooks for school children.",
            category="books", quantity="25 books", location="Pune",
            availability="Weekends", status="pending")
        self.offer_b = models.DonationOffer(
            donor_id=donor.id, title="Beta blankets",
            description="Warm washed blankets ready for pickup.",
            category="home", quantity="10 blankets", location="Pune",
            availability="Flexible", status="accepted")
        self.offer_withdrawn = models.DonationOffer(
            donor_id=donor.id, title="Withdrawn lot",
            description="This one was withdrawn from the ledger.",
            category="books", quantity="3", location="Pune",
            availability="Flexible", status="declined")
        db.session.add_all([self.offer_a, self.offer_b, self.offer_withdrawn])
        db.session.commit()
        self.offer_a_id, self.offer_b_id = self.offer_a.id, self.offer_b.id
        self.withdrawn_id = self.offer_withdrawn.id
        req = models.NGORequirement(
            ngo_id=ngo.id, title="Need books",
            description="Library needs textbooks for children urgently.",
            category="books", required_quantity="25 books", location="Pune",
            urgency="open", status="open")
        db.session.add(req)
        db.session.commit()

    # -- helpers -----------------------------------------------------
    def login_as(self, email, pwd):
        return self.client.post(
            "/login", data={"email": email, "password": pwd},
            follow_redirects=False)

    def login_admin(self):
        return self.login_as("admin@t.com", "adminpass123")

    def feature(self, offer_id, client=None):
        c = client or self.client
        return c.post("/admin/featured-slip",
                      data={"action": "feature", "offer_id": str(offer_id)},
                      follow_redirects=False)

    def clear(self, client=None):
        c = client or self.client
        return c.post("/admin/featured-slip", data={"action": "clear"},
                      follow_redirects=False)

    def home(self):
        """Home page as a real visitor: fresh client, no flashes/session."""
        return app.test_client().get("/").data.decode()

    def hero_slip(self, html=None):
        """Just the hero slip markup, so prose elsewhere cannot match."""
        html = html if html is not None else self.home()
        if 'class="slip"' not in html:
            return ""
        start = html.index('class="slip"')
        return html[start:html.index("</div>", html.index("slip-foot", start))]

    def picker_table(self, html):
        """Just the eligible-donations picker table."""
        marker = 'aria-label="Choose the featured donation"'
        if marker not in html:
            return ""
        tail = html.split(marker, 1)[1]
        return tail[:tail.index("</table>")]


# ============ 1. Impact Tracker removal ============

class TestImpactTrackerRemoved(Base):
    def test_impact_route_gone(self):
        rules = {str(r) for r in app.url_map.iter_rules()}
        self.assertNotIn("/impact", rules)

    def test_no_link_to_impact_anywhere(self):
        """No template may link to the removed page."""
        for name in os.listdir("templates"):
            if not name.endswith(".html"):
                continue
            with open(os.path.join("templates", name), encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn('"/impact"', src,
                             f"{name} still links to /impact")
            self.assertNotIn("'/impact'", src,
                             f"{name} still links to /impact")

    def test_sidebar_has_no_impact_tracker_item(self):
        with open("templates/base.html", encoding="utf-8") as fh:
            base = fh.read()
        self.assertNotIn("Impact Tracker", base)
        self.assertIn("/tracking", base)
        # Donation Tracking survives.
        self.assertIn("Donation Tracking", base)

    def test_impact_template_deleted(self):
        self.assertFalse(os.path.exists("templates/impact.html"))

    def test_impact_css_removed(self):
        with open("static/css/landing.css", encoding="utf-8") as fh:
            css = fh.read()
        self.assertNotIn(".impact-grid", css)
        self.assertNotIn(".impact-cell", css)
        self.assertNotIn(".impact-note", css)
        # The slip design must be untouched.
        self.assertIn(".slip {", css)

    def test_donation_tracking_still_routed(self):
        rules = {str(r) for r in app.url_map.iter_rules()}
        for rule in ("/tracking", "/tracking/<int:track_id>/accept",
                     "/tracking/<int:track_id>/decline",
                     "/tracking/<int:track_id>/handover",
                     "/tracking/<int:track_id>/complete"):
            self.assertIn(rule, rules)

    def test_home_has_no_impact_section(self):
        with open("templates/index.html", encoding="utf-8") as fh:
            home = fh.read()
        self.assertNotIn('id="impact"', home)
        self.assertNotIn("Impact ledger", home)

    def test_other_pages_render_after_removal(self):
        for path in ("/", "/explore", "/requirements", "/analytics"):
            self.assertEqual(self.client.get(path).status_code, 200, path)


# ============ 2. Admin-controlled featured slip ============

class TestFeaturedSlipAdminControl(Base):
    def test_home_empty_state_without_selection(self):
        html = self.client.get("/").data.decode()
        self.assertIn("No featured slip yet", html)
        # No random/auto donation leaks in.
        self.assertNotIn("Alpha books", html)
        self.assertNotIn("Beta blankets", html)

    def test_admin_can_feature_and_home_shows_it(self):
        self.login_admin()
        rv = self.feature(self.offer_a_id)
        self.assertIn(rv.status_code, (302, 303))
        html = self.client.get("/").data.decode()
        self.assertIn("Alpha books", html)
        self.assertNotIn("Beta blankets", html)
        # Slip design preserved.
        self.assertIn('class="slip"', html)
        self.assertIn('class="slip-head"', html)
        self.assertIn('class="slip-foot"', html)

    def test_home_does_not_auto_select_when_none_chosen(self):
        """Even with offers present, nothing is picked automatically."""
        for _ in range(3):
            html = self.client.get("/").data.decode()
            self.assertIn("No featured slip yet", html)
            self.assertNotIn("Alpha books", html)

    def test_selection_persists_across_requests(self):
        self.login_admin()
        self.feature(self.offer_a_id)
        for _ in range(3):
            html = self.client.get("/").data.decode()
            self.assertIn("Alpha books", html)

    def test_selection_survives_session_restart(self):
        """A brand-new client (new session) still sees the same slip."""
        self.login_admin()
        self.feature(self.offer_a_id)
        fresh = app.test_client()
        html = fresh.get("/").data.decode()
        self.assertIn("Alpha books", html)
        with app.app_context():
            row = models.FeaturedSlip.query.first()
            self.assertIsNotNone(row)
            self.assertEqual(row.offer_id, self.offer_a_id)
            self.assertEqual(row.updated_by, self.admin_id)

    def test_admin_can_change_featured_slip(self):
        self.login_admin()
        self.feature(self.offer_a_id)
        self.feature(self.offer_b_id)
        html = self.home()
        self.assertIn("Beta blankets", html)
        self.assertNotIn("Alpha books", html)
        # Exactly one row, no duplicates created.
        with app.app_context():
            self.assertEqual(models.FeaturedSlip.query.count(), 1)

    def test_admin_can_clear_featured_slip(self):
        self.login_admin()
        self.feature(self.offer_a_id)
        self.clear()
        html = self.home()
        self.assertIn("No featured slip yet", html)
        self.assertNotIn("Alpha books", html)
        with app.app_context():
            row = models.FeaturedSlip.query.first()
            self.assertIsNotNone(row)
            self.assertIsNone(row.offer_id)

    def test_admin_panel_shows_current_and_eligible(self):
        self.login_admin()
        self.feature(self.offer_a_id)
        html = self.client.get("/admin").data.decode()
        self.assertIn("Featured Home-page slip", html)
        self.assertIn("Currently on the Home page", html)
        self.assertIn("Remove from Home", html)
        picker = self.picker_table(html)
        self.assertIn("Alpha books", picker)
        self.assertIn("Beta blankets", picker)
        # Withdrawn offers are not eligible for the featured slot. They
        # may still appear in the separate donation-oversight queue.
        self.assertNotIn("Withdrawn lot", picker)

    def test_withdrawn_offer_cannot_be_featured(self):
        self.login_admin()
        rv = self.feature(self.withdrawn_id)
        self.assertEqual(rv.status_code, 400)
        html = self.client.get("/").data.decode()
        self.assertIn("No featured slip yet", html)

    def test_unknown_offer_cannot_be_featured(self):
        self.login_admin()
        rv = self.feature(999999)
        self.assertEqual(rv.status_code, 400)

    def test_donor_cannot_change_featured_slip(self):
        self.login_as("donor@t.com", "donorpass123")
        rv = self.feature(self.offer_b_id)
        # Redirected away from admin action.
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/donor", rv.headers.get("Location", ""))
        html = self.client.get("/").data.decode()
        self.assertIn("No featured slip yet", html)

    def test_ngo_cannot_change_featured_slip(self):
        self.login_as("ngo@t.com", "ngopass123")
        rv = self.feature(self.offer_b_id)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/ngo", rv.headers.get("Location", ""))
        html = self.client.get("/").data.decode()
        self.assertIn("No featured slip yet", html)

    def test_anonymous_cannot_change_featured_slip(self):
        rv = self.feature(self.offer_b_id)
        self.assertIn(rv.status_code, (302, 303))
        self.assertIn("/login", rv.headers.get("Location", ""))

    def test_get_not_allowed_on_action(self):
        self.login_admin()
        rv = self.client.get("/admin/featured-slip")
        self.assertEqual(rv.status_code, 405)

    def test_no_internal_id_in_home_slip(self):
        """No internal id or technical label is shown to visitors."""
        self.login_admin()
        self.feature(self.offer_a_id)
        slip = self.hero_slip()
        self.assertNotIn("Slip №", slip)
        self.assertNotIn("offer_id", slip)
        # The featured row id must not be echoed into the markup.
        html = self.home()
        self.assertNotIn(f'value="{self.offer_a_id}"', html)

    def test_slip_reflects_real_record_fields(self):
        self.login_admin()
        self.feature(self.offer_a_id)
        html = self.client.get("/").data.decode()
        self.assertIn("25 books", html)
        self.assertIn("Pune", html)
        self.assertIn("Books &amp; Education", html)
        self.assertIn("Donor", html)

    def test_featured_slip_is_not_invented(self):
        """The slip must come from a real donation_offers row."""
        self.login_admin()
        self.feature(self.offer_a_id)
        html = self.client.get("/").data.decode()
        with app.app_context():
            real = db.session.get(models.DonationOffer, self.offer_a_id)
            self.assertIn(real.title, html)
            self.assertIn(real.quantity, html)


class TestFeaturedSlipCSRF(Base):
    def test_action_requires_csrf_token(self):
        """CSRF stays enforced on the admin action."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            c = app.test_client()
            page = c.get("/login").data.decode()
            tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
            c.post("/login", data={"email": "admin@t.com",
                                   "password": "adminpass123",
                                   "csrf_token": tok},
                   follow_redirects=False)
            with app.app_context():
                oid = models.DonationOffer.query.filter_by(
                    title="Alpha books").first().id
            # POST without a token must be rejected.
            rv = c.post("/admin/featured-slip",
                        data={"action": "feature", "offer_id": str(oid)},
                        follow_redirects=False)
            self.assertEqual(rv.status_code, 400)
            self.assertIn("No featured slip yet",
                          c.get("/").data.decode())
            # With a valid token it succeeds.
            admin_page = c.get("/admin").data.decode()
            tok2 = re.search(r'name="csrf_token" value="([^"]+)"',
                             admin_page).group(1)
            rv2 = c.post("/admin/featured-slip",
                         data={"action": "feature", "offer_id": str(oid),
                               "csrf_token": tok2},
                         follow_redirects=False)
            self.assertIn(rv2.status_code, (302, 303))
            self.assertIn("Alpha books", c.get("/").data.decode())
        finally:
            app.config["WTF_CSRF_ENABLED"] = False


if __name__ == "__main__":
    unittest.main(verbosity=2)