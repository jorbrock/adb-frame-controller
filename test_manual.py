from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

import controller as c


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        context = patch.object(c, "DATA", Path(self.temp.name))
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(c, "datetime", wraps=datetime)
        self.clock = context.start()
        self.clock.now.return_value = datetime(2026, 9, 18, 12, tzinfo=ZoneInfo("UTC"))
        self.addCleanup(context.stop)
        self.cfg = dict(name="frame", address="192.0.2.1:5555", package="com.example.frame",
                        component="com.example.frame/.MainActivity", wake="07:00", sleep="22:00",
                        morning_action="reboot", boot_delay_seconds=0, night_recheck_seconds=300)

    def frame(self, scheduled=True):
        frame = c.Frame(self.cfg, "UTC", scheduled)
        frame.adb = Mock()
        frame.adb.boot_id.return_value = "old-boot"
        frame.adb.shell.side_effect = lambda *args, **kwargs: "Status: ok" if args[:2] == ("am", "start") else "1"
        return frame

    def test_reboot_relaunch_no_scheduled_second_reboot(self):
        frame = self.frame()
        frame.request_reboot("a" * 32)
        frame.tick()
        frame.adb.boot_id.return_value = "new-boot"
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "completed")
        frame.tick()
        frame.adb.run.assert_called_once_with("reboot", timeout=60)
        frame.adb.shell.assert_any_call("am", "start", "-W", "-n", self.cfg["component"])
        frame.adb.shell.assert_any_call("settings", "put", "system", "screen_brightness", "128")

    def test_reset_app_stops_trims_and_launches_without_reboot(self):
        frame = self.frame()
        frame.request_action("reset_app", "a" * 32)
        frame.tick()
        calls = frame.adb.shell.call_args_list
        stop = call("am", "force-stop", self.cfg["package"])
        trim = call("pm", "trim-caches", "999999999999999999", timeout=120)
        start = call("am", "start", "-W", "-n", self.cfg["component"])
        self.assertLess(calls.index(stop), calls.index(trim))
        self.assertLess(calls.index(trim), calls.index(start))
        self.assertEqual(frame.state["manual"]["phase"], "completed")
        frame.adb.run.assert_not_called()
        frame.adb.boot_id.assert_not_called()
        frame.adb.reset_mock()
        frame.request_action("reset_app", "a" * 32)
        frame.tick()
        frame.adb.connect.assert_not_called()
        # A new request always trims, even on the same day.
        frame.request_action("reset_app", "b" * 32)
        frame.tick()
        frame.adb.shell.assert_any_call(*trim.args, **trim.kwargs)

    def test_reset_app_launch_retry_preserves_trim_across_restart(self):
        frame = self.frame()
        frame.request_action("reset_app", "a" * 32)
        frame.adb.shell.side_effect = lambda *args, **kwargs: (
            "Error: launch failed" if args[:2] == ("am", "start") else "1")
        frame.tick()
        self.assertTrue(frame.state["manual"]["cache_trimmed"])
        recovered = self.frame()
        recovered.tick()
        self.assertEqual(recovered.state["manual"]["phase"], "completed")
        self.assertFalse(any(c.args[:2] == ("pm", "trim-caches")
                             for c in recovered.adb.shell.call_args_list))
        recovered.adb.run.assert_not_called()

    def test_reset_app_trim_failure_does_not_launch_and_retries(self):
        frame = self.frame()
        frame.request_action("reset_app", "a" * 32)
        frame.adb.shell.side_effect = ["", RuntimeError("trim failed")]
        frame.tick()
        self.assertNotIn("cache_trimmed", frame.state["manual"])
        self.assertEqual(len(frame.adb.shell.call_args_list), 2)
        frame.adb.shell.side_effect = None
        frame.adb.shell.return_value = "Status: ok"
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "completed")
        frame.adb.run.assert_not_called()

    def test_reset_app_at_night_or_without_scheduler_holds_awake(self):
        self.clock.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo("UTC"))
        for scheduled in (True, False):
            with self.subTest(scheduled=scheduled):
                frame = self.frame(scheduled=scheduled)
                frame.request_action("reset_app", ("a" if scheduled else "b") * 32)
                frame.tick()
                self.assertEqual(frame.state["override"]["mode"], "day")
                frame.adb.shell.assert_any_call("am", "start", "-W", "-n", self.cfg["component"])
                frame.adb.reset_mock()
                frame.tick()
                frame.adb.connect.assert_not_called()

    def test_reset_app_crossing_sleep_boundary_does_not_launch(self):
        frame = self.frame()
        frame.request_action("reset_app", "a" * 32)
        def shell(*args, **kwargs):
            if args[:2] == ("pm", "trim-caches"):
                self.clock.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo("UTC"))
            return "1"
        frame.adb.shell.side_effect = shell
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "cancelled")
        self.assertFalse(any(c.args[:2] == ("am", "start")
                             for c in frame.adb.shell.call_args_list))

    def test_duplicate_busy_cooldown_and_lock(self):
        frame = self.frame()
        frame.request_reboot("a" * 32)
        frame.request_reboot("a" * 32)
        with self.assertRaisesRegex(RuntimeError, "already in progress"):
            frame.request_reboot("b" * 32)
        frame.state["manual"]["phase"] = "completed"
        with self.assertRaisesRegex(RuntimeError, "two minutes"):
            frame.request_reboot("b" * 32)
        with frame.mutex:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                frame.request_reboot("b" * 32)

    def test_restart_mid_reboot_never_reissues_command(self):
        frame = self.frame()
        frame.request_reboot("a" * 32)
        frame.adb.run.side_effect = RuntimeError("connection lost")
        frame.tick()
        recovered = self.frame()
        recovered.tick()
        recovered.adb.run.assert_not_called()
        self.assertEqual(recovered.state["manual"]["phase"], "rebooting")
        recovered.adb.boot_id.return_value = "new-boot"
        recovered.tick()
        self.assertEqual(recovered.state["manual"]["phase"], "completed")

    def test_night_reboot_returns_to_zero_brightness(self):
        self.clock.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo("UTC"))
        frame = self.frame()
        frame.request_reboot("a" * 32)
        frame.tick()
        frame.adb.boot_id.return_value = "new-boot"
        frame.tick()
        frame.adb.shell.assert_any_call("settings", "put", "system", "screen_brightness_mode", "0")
        frame.adb.shell.assert_any_call("settings", "put", "system", "screen_brightness", "0")
        self.assertFalse(any(call.args == ("input", "keyevent", "223")
                             for call in frame.adb.shell.call_args_list))
        self.assertFalse(any(call.args[:2] == ("am", "start") for call in frame.adb.shell.call_args_list))

    def test_scheduler_disabled_still_allows_manual_reboot(self):
        frame = self.frame(scheduled=False)
        frame.tick()
        frame.adb.connect.assert_not_called()
        frame.request_reboot("a" * 32)
        frame.tick()
        frame.adb.boot_id.return_value = "new-boot"
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "completed")

    def test_unreachable_times_out_without_reboot(self):
        frame = self.frame()
        frame.request_reboot("a" * 32)
        frame.adb.connect.side_effect = RuntimeError("unreachable")
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "queued")
        frame.state["manual"]["requested_at"] -= 901
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "failed")
        frame.adb.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
