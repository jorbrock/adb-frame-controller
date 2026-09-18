"""Configuration persistence, live workers, and authenticated editing."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import controller as c
from webui import create_app


def config():
    return c.validate_config(dict(timezone="UTC", enabled=False, frames=[dict(
        name="living-room", address="192.0.2.1:5555", package="com.example.frame",
        component="com.example.frame/.MainActivity", wake="07:00", sleep="22:00",
    )]))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.config = config()
        self.source = self.data / "config.json"
        self.source.write_text(json.dumps(self.config))
        self.registry = c.FrameRegistry(self.config, self.data)

    def change(self, name, values):
        self.registry.change(name, values, self.registry.revision)

    def load(self):
        with patch.object(c, "DATA", self.data), patch.object(c, "CONFIG", str(self.source)):
            return c.load_config()

    def test_file_config_seeds_ui_and_saved_list_survives_restart(self):
        self.assertEqual(self.load(), self.config)
        original = self.source.read_bytes()
        frame = {**self.config["frames"][0], "name": "kitchen", "address": "192.0.2.2:5555"}
        self.change(None, frame)
        restored = c.FrameRegistry(self.load(), self.data)
        self.assertEqual(set(restored.frames), {"living-room", "kitchen"})
        self.assertEqual(self.source.read_bytes(), original)
        self.change("living-room", None)
        self.change("kitchen", None)
        self.assertEqual(self.load()["frames"], [])
        self.assertEqual(self.registry.snapshots(), [])
        self.change(None, frame)
        self.assertEqual(self.load()["frames"], [frame])

    def test_corrupt_override_fails_closed(self):
        for value in ('invalid json', '{}', '[null]'):
            with self.subTest(value=value):
                (self.data / "frames.json").write_text(value)
                with self.assertRaises(ValueError):
                    self.load()

    def test_edit_and_rename_preserve_journal_and_do_not_repeat_completed_morning(self):
        frame = self.registry.frames["living-room"]
        token = c.window(c.datetime.now(frame.zone), frame.cfg)[1]
        frame.state = dict(attempted_window=token, completed_window=token,
                           previous_boot_id="old-boot", manual=dict(phase="completed", id="a" * 32))
        frame.save()
        frame.last_night = 100
        renamed = {**frame.cfg, "name": "lounge", "address": "192.0.2.3:5555", "day_brightness": 220}
        self.change("living-room", renamed)
        self.assertIs(self.registry.frames["lounge"], frame)
        self.assertEqual(frame.adb.address, renamed["address"])
        self.assertEqual(frame.last_night, 0)
        restored = c.FrameRegistry(self.load(), self.data).frames["lounge"]
        self.assertEqual(restored.state, frame.state)
        self.assertEqual(restored.cfg, renamed)
        restored.scheduled = True
        restored.adb = Mock()
        with patch.object(c, "window", return_value=("day", token)):
            restored.tick()
        restored.adb.connect.assert_not_called()
        restored.adb.run.assert_not_called()

    def test_validation_rejects_invalid_fields_duplicates_and_limits(self):
        original = deepcopy(self.config)
        for changes in (
            dict(name="../escape"), dict(name=""), dict(address="host:0"),
            dict(address="host:65536"), dict(address="host:5555;reboot"),
            dict(component="different.package/.Activity"), dict(wake="25:00"),
            dict(wake="22:00"), dict(day_brightness=256), dict(day_brightness=True),
            dict(boot_delay_seconds=-1), dict(night_recheck_seconds=29),
            dict(morning_action="anything"), dict(package=None),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.change("living-room", {**original["frames"][0], **changes})
        for changes in (dict(name="kitchen"), dict(address="192.0.2.2:5555")):
            with self.subTest(duplicate=changes), self.assertRaises(ValueError):
                self.change(None, {**original["frames"][0], **changes})
        many = [{**original["frames"][0], "name": f"frame-{i}", "address": f"host-{i}:5555"}
                for i in range(51)]
        with self.assertRaisesRegex(ValueError, "0 to 50"):
            c.validate_config({**original, "frames": many})
        self.assertEqual(self.config, original)
        self.assertFalse((self.data / "frames.json").exists())

    def test_write_failure_leaves_live_settings_unchanged(self):
        frame = self.registry.frames["living-room"]
        original = deepcopy(frame.cfg)
        revision = self.registry.revision
        with patch.object(c, "atomic_json", side_effect=OSError("disk full")):
            for name, values in (("living-room", {**original, "day_brightness": 200}),
                                 ("living-room", {**original, "name": "lounge"}),
                                 ("living-room", None),
                                 (None, {**original, "name": "other", "address": "host:5555"})):
                with self.subTest(name=name, values=values), self.assertRaises(OSError):
                    self.change(name, values)
        self.assertEqual(frame.cfg, original)
        self.assertEqual(self.registry.revision, revision)
        self.assertFalse(frame.stopped.is_set())
        self.assertEqual(list(self.registry.frames), ["living-room"])
        self.assertTrue(frame.mutex.acquire(blocking=False))
        frame.mutex.release()

    def test_busy_frame_and_manual_job_reject_edit_and_remove(self):
        frame = self.registry.frames["living-room"]
        with frame.mutex:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                self.change("living-room", frame.cfg)
        for phase in ("queued", "rebooting", "starting"):
            frame.state["manual"] = dict(phase=phase)
            for values in (frame.cfg, None):
                with self.subTest(phase=phase), self.assertRaisesRegex(RuntimeError, "reboot to finish"):
                    self.change("living-room", values)
        self.assertFalse((self.data / "frames.json").exists())

    def test_concurrent_forms_cannot_overwrite_changes(self):
        revision = self.registry.revision
        barrier = threading.Barrier(2)
        results = []
        def submit(brightness):
            barrier.wait(timeout=2)
            try:
                self.registry.change("living-room", {**config()["frames"][0], "day_brightness": brightness}, revision)
                results.append("saved")
            except RuntimeError:
                results.append("conflict")
        threads = [threading.Thread(target=submit, args=(level,)) for level in (100, 200)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(self.load()["frames"], self.config["frames"])

    def test_worker_start_failure_does_not_save_or_add_frame(self):
        self.registry.running = True
        values = {**self.config["frames"][0], "name": "kitchen", "address": "host:5555"}
        with patch.object(self.registry, "_start", side_effect=RuntimeError("Cannot start thread")):
            with self.assertRaisesRegex(RuntimeError, "Cannot start thread"):
                self.change(None, values)
        self.assertEqual(list(self.registry.frames), ["living-room"])
        self.assertFalse((self.data / "frames.json").exists())

    def test_new_worker_sends_no_commands_when_save_fails(self):
        self.registry.running = True
        values = {**self.config["frames"][0], "name": "kitchen", "address": "host:5555"}
        workers = []
        start = self.registry._start
        def track_start(frame):
            worker = start(frame)
            workers.append(worker)
            return worker
        with patch.object(c.Frame, "tick") as tick, patch.object(self.registry, "_start", side_effect=track_start):
            with patch.object(c, "atomic_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.change(None, values)
            for worker in workers:
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
            tick.assert_not_called()
        self.assertEqual(list(self.registry.frames), ["living-room"])
        self.assertEqual(self.registry.workers, {})

    def test_live_add_edit_remove_worker_lifecycle(self):
        events = {name: threading.Event() for name in ("living-room", "kitchen", "lounge")}
        def tick(frame):
            events[frame.cfg["name"]].set()
        with patch.object(c.Frame, "tick", autospec=True, side_effect=tick):
            self.registry.start()
            workers = []
            try:
                self.assertTrue(events["living-room"].wait(2))
                self.change(None, {**self.config["frames"][0], "name": "kitchen", "address": "host:5555"})
                self.assertTrue(events["kitchen"].wait(2))
                workers = list(self.registry.workers.values())
                frame = self.registry.frames["kitchen"]
                worker = self.registry.workers["kitchen"]
                self.change("kitchen", {**frame.cfg, "name": "lounge"})
                self.assertTrue(events["lounge"].wait(2))
                self.assertIs(self.registry.workers["lounge"], worker)
                self.assertTrue(self.registry.healthy())
                self.change("lounge", None)
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                with self.assertRaises(KeyError):
                    self.registry.request_reboot("lounge", "a" * 32)
            finally:
                for frame in self.registry.frames.values():
                    frame.stopped.set()
                    frame.wakeup.set()
                for worker in workers or self.registry.workers.values():
                    worker.join(timeout=2)


class ConfigurationWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        (self.data / "web-auth.json").write_text(json.dumps(dict(id="account", username="admin")))
        self.config = config()
        self.registry = c.FrameRegistry(self.config, self.data)
        self.app = create_app(self.config, self.registry, self.data)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["account_id"] = "account"
            session["csrf"] = "csrf-token"

    def form(self, **changes):
        return {**config()["frames"][0], "csrf": "csrf-token", "revision": self.registry.revision, **changes}

    def test_forms_and_empty_state(self):
        for path in ("/", "/frames/new", "/frames/living-room/edit", "/frames/living-room/remove"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"csrf-token", response.data)
        response = self.client.get("/frames/living-room/edit")
        self.assertIn(b'value="128"', response.data)
        self.assertNotIn(b'data-refresh="true"', response.data)
        self.assertFalse((self.data / "frames.json").exists())
        response = self.client.post("/frames/living-room/remove", data=self.form(), follow_redirects=True)
        self.assertIn(b"No frames yet.", response.data)

    def test_add_edit_rename_reboot_and_remove(self):
        response = self.client.post("/frames/new", data=self.form(name="kitchen", address="host:5555"))
        self.assertEqual(response.status_code, 303)
        response = self.client.post("/frames/kitchen/edit", data=self.form(name="lounge", address="host:5555", day_brightness="200"))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.registry.frames["lounge"].cfg["day_brightness"], 200)
        self.assertEqual(self.client.get("/frames/kitchen/edit").status_code, 404)
        frame = self.registry.frames["lounge"]
        with patch.object(frame, "request_reboot") as reboot:
            response = self.client.post("/frames/lounge/reboot", data=dict(csrf="csrf-token", request_id="a" * 32))
            self.assertEqual(response.status_code, 303)
            reboot.assert_called_once_with("a" * 32)
        response = self.client.post("/frames/lounge/remove", data=self.form())
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.post("/frames/lounge/reboot", data=dict(csrf="csrf-token")).status_code, 404)
        self.assertEqual([cfg["name"] for cfg in json.loads((self.data / "frames.json").read_text())], ["living-room"])

    def test_authentication_and_csrf_for_every_mutation(self):
        paths = ("/frames/new", "/frames/living-room/edit", "/frames/living-room/remove")
        for path in paths:
            for csrf in (None, "wrong", "é"):
                data = self.form()
                data.pop("csrf")
                if csrf is not None:
                    data["csrf"] = csrf
                self.assertEqual(self.client.post(path, data=data).status_code, 400)
        with self.client.session_transaction() as session:
            session.clear()
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 302)
            self.assertEqual(self.client.post(path, data=self.form()).status_code, 302)
        self.assertFalse((self.data / "frames.json").exists())

    def test_invalid_input_is_preserved_and_escaped(self):
        for changes in (dict(day_brightness="1.5"), dict(wake="25:00"),
                        dict(name="<script>alert(1)</script>"), dict(address="")):
            response = self.client.post("/frames/living-room/edit", data=self.form(**changes))
            self.assertEqual(response.status_code, 400)
            self.assertIn(b'role="alert"', response.data)
            self.assertNotIn(b"<script>alert(1)</script>", response.data)
        self.assertFalse((self.data / "frames.json").exists())

    def test_stale_edit_and_remove_cannot_overwrite_new_settings(self):
        stale = self.form(day_brightness=200)
        self.assertEqual(self.client.post("/frames/living-room/edit", data=self.form(day_brightness=100)).status_code, 303)
        for path in ("/frames/living-room/edit", "/frames/living-room/remove"):
            response = self.client.post(path, data=stale)
            self.assertEqual(response.status_code, 409)
            self.assertIn(b"another session", response.data)
            self.assertIn(stale["revision"].encode(), response.data)
        self.assertEqual(self.registry.frames["living-room"].cfg["day_brightness"], 100)

    def test_storage_errors_are_actionable(self):
        with patch.object(c, "atomic_json", side_effect=OSError("private details")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.client.post("/frames/living-room/edit", data=self.form(day_brightness=200))
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"data directory is writable", response.data)
        self.assertNotIn(b"private details", response.data)
        self.assertEqual(self.registry.frames["living-room"].cfg["day_brightness"], 128)


if __name__ == "__main__":
    unittest.main()
