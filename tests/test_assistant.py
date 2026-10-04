"""ShareHope assistant + sidebar tests — isolated SQLite harness.

Never touches production MySQL. Swaps the global Flask-SQLAlchemy
engine to a temp SQLite file (same pattern as the other suites).
Suppresses real SMTP. AI provider calls are mocked; no network.

Covers: page render, successful replies (mocked live + offline
fallback), empty/oversize input, auth posture (public preserved),
CSRF, timeouts, API errors, rate limits, no-PII payloads,
read-only guarantee, session isolation, XSS-safe rendering,
history clear, and sidebar animation/static checks.
"""

import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from app import app, db
import models
import assistant_service


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
        fd, path = tempfile.mkstemp(prefix="sharehope_assistant_test_",
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


class AssistantBase(unittest.TestCase):
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
        app.config.pop("ASSISTANT_RATE_LIMIT", None)
        self.client = app.test_client()
        with app.app_context():
            db.session.remove()
            db.drop_all()
            db.create_all()
            self._seed()

    def tearDown(self):
        app.config["WTF_CSRF_ENABLED"] = False
        app.config.pop("ASSISTANT_RATE_LIMIT", None)
        # Never leak a fake provider key into other suites.
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GEMINI_MODEL", None)
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
                          org_name="Helping Hands",
                          org_reg_id="MH/2020/001")
        ngo.set_password("ngopass123")
        db.session.add_all([donor, ngo])
        db.session.commit()
        off = models.DonationOffer(
            donor_id=donor.id, title="Books bundle",
            description="Good condition textbooks for school kids.",
            category="books", quantity="10 books",
            location="Pune", availability="Flexible", status="pending")
        db.session.add(off)
        db.session.commit()

    def ask_form(self, client, text):
        return client.post("/assistant/ask", data={"message": text},
                           follow_redirects=False)

    def ask_json(self, client, text):
        return client.post(
            "/assistant/ask", data={"message": text},
            headers={"X-Requested-With": "fetch",
                     "Accept": "application/json"})

    def counts(self):
        with app.app_context():
            return (models.User.query.count(),
                    models.DonationOffer.query.count(),
                    models.NGORequirement.query.count(),
                    models.DonationTracking.query.count(),
                    models.Message.query.count(),
                    models.Notification.query.count())


class TestPage(AssistantBase):
    def test_page_loads_public(self):
        rv = self.client.get("/assistant")
        self.assertEqual(rv.status_code, 200)
        html = rv.data.decode()
        self.assertNotIn("no replies yet", html.lower())
        self.assertNotIn("Mock bubbles", html)
        self.assertIn('id="botComposer"', html)
        self.assertIn('id="botbox"', html)
        self.assertIn("assistant.js", html)

    def test_offline_notice_without_key(self):
        os.environ.pop("GEMINI_API_KEY", None)
        html = self.client.get("/assistant").data.decode()
        self.assertIn("Built-in guide", html)


class TestAsk(AssistantBase):
    def test_form_success_and_history(self):
        with patch.object(assistant_service, "_call_gemini",
                          return_value="Mocked live answer"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                rv = self.ask_form(self.client, "How do I donate?")
        self.assertIn(rv.status_code, (302, 303))
        html = self.client.get("/assistant").data.decode()
        self.assertIn("How do I donate?", html)
        self.assertIn("Mocked live answer", html)

    def test_json_success(self):
        with patch.object(assistant_service, "_call_gemini",
                          return_value="Mocked live answer"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                rv = self.ask_json(self.client, "Hello?")
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["reply"], "Mocked live answer")
        self.assertFalse(data["fallback"])

    def test_offline_fallback_no_key(self):
        os.environ.pop("GEMINI_API_KEY", None)
        rv = self.ask_json(self.client, "Where is tracking?")
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["fallback"])
        self.assertIn("/tracking", data["reply"])

    def test_offline_never_invents_records(self):
        os.environ.pop("GEMINI_API_KEY", None)
        data = self.ask_json(self.client, "Which NGO needs books?").get_json()
        self.assertNotIn("Helping Hands", data["reply"])
        self.assertNotIn("Books bundle", data["reply"])

    def test_empty_rejected(self):
        self.assertEqual(self.ask_form(self.client, "   ").status_code, 400)
        rv = self.ask_json(self.client, "")
        self.assertEqual(rv.status_code, 400)
        self.assertFalse(rv.get_json()["ok"])

    def test_too_long_rejected(self):
        rv = self.ask_form(self.client, "x" * 2001)
        self.assertEqual(rv.status_code, 400)

    def test_clear_history(self):
        with patch.object(assistant_service, "_call_gemini",
                          return_value="hi"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                self.ask_form(self.client, "Hello?")
        self.assertIn("data-chat", self.client.get("/assistant").data.decode())
        self.assertIn(self.client.post("/assistant/clear").status_code,
                      (302, 303))
        self.assertNotIn("data-chat",
                         self.client.get("/assistant").data.decode())

    def test_session_isolation(self):
        with patch.object(assistant_service, "_call_gemini",
                          return_value="hi"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                self.ask_form(self.client, "My private question xyz?")
        other = app.test_client()
        self.assertNotIn("My private question xyz?",
                         other.get("/assistant").data.decode())

    def test_read_only(self):
        before = self.counts()
        with patch.object(assistant_service, "_call_gemini",
                          return_value="hi"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                for q in ("Hello?", "How do I donate?", "Thanks!"):
                    self.ask_json(self.client, q)
        self.assertEqual(before, self.counts())


class TestRobustness(AssistantBase):
    def test_timeout_graceful(self):
        with patch.object(assistant_service, "_call_gemini",
                          side_effect=TimeoutError("timed out")):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                rv = self.ask_json(self.client, "Hello?")
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["fallback"])

    def test_model_not_found_classified(self):
        with patch.object(assistant_service, "_call_gemini",
                          side_effect=assistant_service._ModelNotFound()):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                rv = self.ask_json(self.client, "Hello?")
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertTrue(data["fallback"])

    def test_service_unavailable_classified(self):
        with patch.object(assistant_service, "_call_gemini",
                          side_effect=assistant_service._ServiceUnavailable()):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                rv = self.ask_json(self.client, "Hello?")
        self.assertEqual(rv.status_code, 200)
        data = rv.get_json()
        self.assertTrue(data["fallback"])

    def test_api_error_graceful(self):
        with patch.object(assistant_service, "_call_gemini",
                          side_effect=urllib.error.HTTPError(
                              "http://x", 500, "boom", {}, io.BytesIO())):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                rv = self.ask_json(self.client, "Hello?")
                rform = self.ask_form(self.client, "Hello again?")
        self.assertEqual(rv.status_code, 200)
        self.assertTrue(rv.get_json()["fallback"])
        self.assertIn(rform.status_code, (302, 303))

    def test_provider_rate_limit_message(self):
        with patch.object(assistant_service, "_call_gemini",
                          side_effect=assistant_service._RateLimited()):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                rv = self.ask_json(self.client, "Hello?")
        self.assertEqual(rv.status_code, 200)
        self.assertIn("wait", rv.get_json()["reply"].lower())

    def test_session_rate_limit_429(self):
        app.config["ASSISTANT_RATE_LIMIT"] = 3
        with patch.object(assistant_service, "_call_gemini",
                          return_value="hi"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                for _ in range(3):
                    self.assertEqual(
                        self.ask_json(self.client, "Hi?").status_code, 200)
                rv = self.ask_json(self.client, "One more?")
                self.assertEqual(rv.status_code, 429)
                self.assertFalse(rv.get_json()["ok"])
                rf = self.ask_form(self.client, "One more?")
                self.assertEqual(rf.status_code, 429)

    def test_no_pii_to_provider(self):
        seen = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"candidates": [{"content": {"parts": [
                        {"text": "Hello!"}]}}]}).encode()

        def fake_open(req, timeout=None):
            seen["body"] = req.data.decode()
            seen["url"] = req.full_url
            return FakeResp()

        self.client.post("/login", data={"email": "donor1@example.com",
                                         "password": "donorpass123"})
        with patch.dict(os.environ, {"GEMINI_API_KEY": "secret-key-xyz"}):
            with patch("urllib.request.urlopen", side_effect=fake_open):
                rv = self.ask_json(self.client, "Hi provider")
        self.assertEqual(rv.status_code, 200)
        body = seen.get("body", "")
        for secret in ("donor1@example.com", "donorpass123", "password123",
                       "secret-key-xyz", "MAIL_PASSWORD"):
            self.assertNotIn(secret, body)
        self.assertNotIn("secret-key-xyz", seen.get("url", "").split("key=")[0])

    def test_role_passed_not_identity(self):
        seen_roles = []
        orig = assistant_service._call_gemini

        def spy(prompt, history, role="visitor"):
            seen_roles.append(role)
            return orig(prompt, history, role) if False else "ok"

        self.client.post("/login", data={"email": "donor1@example.com",
                                         "password": "donorpass123"})
        with patch.object(assistant_service, "_call_gemini",
                          side_effect=spy):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                self.ask_json(self.client, "Hi?")
        self.assertEqual(seen_roles, ["donor"])


class TestSecurity(AssistantBase):
    def test_csrf_rejected(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            client = app.test_client()
            token = _extract_csrf(client.get("/login").data.decode())
            client.post("/login",
                        data={"email": "donor1@example.com",
                              "password": "donorpass123",
                              "csrf_token": token},
                        follow_redirects=False)
            rv = client.post("/assistant/ask", data={"message": "Hi?"},
                             follow_redirects=False)
            self.assertEqual(rv.status_code, 400)
            # valid token succeeds
            page = client.get("/assistant").data.decode()
            tok2 = _extract_csrf(page)
            self.assertIsNotNone(tok2)
            with patch.object(assistant_service, "_call_gemini",
                              return_value="hi"):
                with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                    rv2 = client.post(
                        "/assistant/ask",
                        data={"message": "Hi?", "csrf_token": tok2},
                        follow_redirects=False)
            self.assertIn(rv2.status_code, (302, 303))
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_xss_escaped_in_html(self):
        with patch.object(assistant_service, "_call_gemini",
                          return_value="<script>alert(1)</script><b>x</b>"):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
                self.ask_form(self.client, "Hi?")
        html = self.client.get("/assistant").data.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_no_safe_filter_in_template(self):
        with open("templates/assistant.html", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("|safe", src)


class TestSidebar(AssistantBase):
    def _css(self):
        with open("static/css/sidebar.css", encoding="utf-8") as fh:
            return fh.read()

    def _js(self):
        with open("static/js/main.js", encoding="utf-8") as fh:
            return fh.read()

    def test_no_display_none_collapse_pop(self):
        css = self._css()
        collapsed = [ln.strip() for ln in css.splitlines()
                     if "sidebar-collapsed" in ln
                     and ("display:none" in ln.replace(" ", "")
                          or "display: none" in ln)]
        # Drawer overrides use display: inline-flex/block/inline (never
        # none); collapsed desktop chrome must morph, never pop.
        self.assertEqual(collapsed, [])

    def test_shared_transition_curve(self):
        css = self._css()
        self.assertIn("prefers-reduced-motion", css)
        # Rail and content must glide on the same duration.
        self.assertGreaterEqual(css.count("280ms"), 2)
        self.assertIn("visibility", css)
        # Icons glide via padding/gap: the old instant centering flip
        # on collapsed links must be gone (badge centering is fine).
        self.assertNotIn("sidebar-collapsed .side-link { justify-content",
                         css)

    def test_hover_intent_debounce(self):
        js = self._js()
        self.assertIn("120", js)
        self.assertIn("280", js)
        self.assertIn("clearTimeout", js)
        self.assertIn("mouseenter", js)
        self.assertIn("mouseleave", js)
        self.assertIn("focusin", js)

    def test_drawer_and_tooltips_preserved(self):
        js = self._js()
        for token in ("navToggle", '"open"', "scrim", "data-tip",
                      "aria-expanded", "Escape"):
            self.assertIn(token, js)
        css = self._css()
        self.assertIn(".sidebar.open", css)
        self.assertIn(".topbar", css)
        self.assertIn(".side-tip", css)

    def test_no_collapse_button_added(self):
        with open("templates/base.html", encoding="utf-8") as fh:
            base = fh.read()
        self.assertNotIn("collapse", base.lower().replace(
            "sidebar-collapsed", ""))
        # Collapsed icon-only layout + titles for tooltips intact.
        self.assertIn('id="sidebar"', base)
        self.assertIn("side-label", base)

    def test_no_animation_loops(self):
        js = self._js()
        # No rAF/interval driven width animation; CSS transitions only.
        self.assertNotIn("requestAnimationFrame", js.split("side-tip")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
