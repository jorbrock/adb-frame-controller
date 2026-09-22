import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from werkzeug.security import generate_password_hash

import controller as c
from webui import create_app


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("test-password")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.auth = dict(id="account-1", username="admin", password_hash=self.password_hash)
        (self.data / "web-auth.json").write_text(json.dumps(self.auth))
        self.frame = Mock()
        self.frame.cfg = dict(name="living-room")
        self.frame.snapshot.return_value = dict(
            name="living-room", address="192.0.2.1:5555", wake="07:00", sleep="22:00",
            boot_delay_seconds=60, mode="day", status={}, job={}, busy=False,
        )
        config = dict(timezone="UTC", enabled=True)
        registry = c.FrameRegistry(config, self.data, frames=[self.frame])
        self.app = create_app(config, registry, self.data)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def token(self):
        with self.client.session_transaction() as session:
            return session["csrf"]

    def login(self):
        self.client.get("/login")
        return self.client.post("/login", data=dict(csrf=self.token(), username="admin", password="test-password"))

    def reboot(self, **overrides):
        data = dict(csrf=self.token(), request_id="a" * 32)
        data.update(overrides)
        return self.client.post("/frames/living-room/reboot", data=data)

    def test_unauthenticated_requests_never_reboot_or_list(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.post("/frames/living-room/reboot").status_code, 302)
        self.frame.request_reboot.assert_not_called()
        self.frame.snapshot.assert_not_called()

    def test_missing_account_is_locked(self):
        (self.data / "web-auth.json").unlink()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Set your login", response.data)
        self.assertEqual(self.client.post("/frames/living-room/reboot").status_code, 503)
        self.frame.request_reboot.assert_not_called()

    def test_login_csrf_and_wrong_password(self):
        self.assertEqual(self.client.post("/login", data=dict(username="admin", password="test-password")).status_code, 400)
        self.client.get("/login")
        response = self.client.post("/login", data=dict(csrf=self.token(), username="admin", password="bad"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_authenticated_reboot_is_post_with_csrf_and_configured_target(self):
        self.assertEqual(self.login().status_code, 303)
        self.assertEqual(self.client.get("/frames/living-room/reboot").status_code, 405)
        self.assertEqual(self.reboot(csrf="wrong").status_code, 400)
        self.assertEqual(self.reboot(csrf="é").status_code, 400)
        self.assertEqual(self.reboot(request_id="invalid").status_code, 400)
        self.assertEqual(self.client.post("/frames/unknown/reboot", data=dict(csrf=self.token())).status_code, 404)
        self.frame.request_reboot.assert_not_called()
        self.assertEqual(self.reboot().status_code, 303)
        self.frame.request_reboot.assert_called_once_with("a" * 32)

    def test_page_escapes_device_output_and_has_session_cookie_flags(self):
        response = self.login()
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.frame.snapshot.return_value["status"] = dict(error="<script>alert(1)</script>")
        response = self.client.get("/")
        self.assertNotIn(b"<script>alert(1)</script>", response.data)
        self.assertIn(b"&lt;script&gt;", response.data)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertNotIn(self.password_hash.encode(), response.data)

    def test_password_change_invalidates_existing_session(self):
        self.login()
        self.auth["id"] = "account-2"
        (self.data / "web-auth.json").write_text(json.dumps(self.auth))
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_display_actions_require_authentication_csrf_and_valid_request_ids(self):
        for action in ("wake", "sleep", "reset_app"):
            self.assertEqual(self.client.post(f"/frames/living-room/{action}").status_code, 302)
        self.frame.request_action.assert_not_called()
        self.login()
        for action in ("wake", "sleep", "reset_app"):
            path = f"/frames/living-room/{action}"
            self.assertEqual(self.client.get(path).status_code, 405)
            self.assertEqual(self.client.post(path, data=dict(csrf="wrong", request_id="a" * 32)).status_code, 400)
            self.assertEqual(self.client.post(path, data=dict(csrf=self.token(), request_id="invalid")).status_code, 400)
            self.assertEqual(self.client.post(f"/frames/unknown/{action}",
                data=dict(csrf=self.token(), request_id="a" * 32)).status_code, 404)
            self.frame.request_action.assert_not_called()
            self.assertEqual(self.client.post(path,
                data=dict(csrf=self.token(), request_id="a" * 32)).status_code, 303)
            self.frame.request_action.assert_called_once_with(action, "a" * 32)
            self.frame.request_action.reset_mock()

    def test_display_actions_show_busy_and_storage_errors(self):
        self.login()
        for action in ("wake", "sleep", "reset_app"):
            self.frame.request_action.side_effect = RuntimeError("A manual action is already in progress")
            response = self.client.post(f"/frames/living-room/{action}",
                data=dict(csrf=self.token(), request_id="a" * 32), follow_redirects=True)
            self.assertIn(b"already in progress", response.data)
            self.frame.request_action.side_effect = OSError("disk full")
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.client.post(f"/frames/living-room/{action}",
                    data=dict(csrf=self.token(), request_id="b" * 32), follow_redirects=True)
            self.assertIn(b"Could not save the request", response.data)

    def test_logout(self):
        self.login()
        self.assertEqual(self.client.post("/logout", data=dict(csrf=self.token())).status_code, 303)
        self.assertEqual(self.client.get("/").status_code, 302)


if __name__ == "__main__":
    unittest.main()
