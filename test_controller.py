import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import controller as c


class ScheduleTests(unittest.TestCase):
    def test_day_boundaries(self):
        frame = {"wake": "07:00", "sleep": "22:00"}
        for hour, minute, expected in [(6, 59, "night"), (7, 0, "day"), (21, 59, "day"), (22, 0, "night")]:
            self.assertEqual(c.window(datetime(2026, 9, 18, hour, minute), frame)[0], expected)

    def test_window_across_midnight(self):
        self.assertEqual(c.window(datetime(2026, 9, 19, 2), {"wake": "20:00", "sleep": "06:00"}),
                         ("day", "2026-09-18"))

    def test_dst_repeated_hour_same_token(self):
        frame = {"wake": "01:00", "sleep": "22:00"}
        first = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/Los_Angeles"), fold=0)
        second = first.replace(fold=1)
        self.assertEqual(c.window(first, frame), c.window(second, frame))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(c, "DATA", Path(self.temp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.cfg = dict(name="test", address="192.0.2.1:5555", package="com.example.frame",
                        component="com.example.frame/.MainActivity", wake="07:00", sleep="22:00",
                        morning_action="reboot", boot_delay_seconds=0, night_recheck_seconds=300)
        self.clock = patch.object(c, "datetime")
        self.mock_time = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.mock_time.now.return_value = datetime(2026, 9, 18, 8, tzinfo=ZoneInfo("UTC"))

    def frame(self):
        frame = c.Frame(self.cfg, "UTC")
        frame.adb = Mock()
        frame.adb.boot_id.return_value = "old-boot"
        frame.adb.shell.return_value = "1"
        return frame

    def test_reboot_failure_not_reissued_after_restart(self):
        frame = self.frame()
        frame.adb.run.side_effect = RuntimeError("connection lost")
        with self.assertRaises(RuntimeError):
            frame.tick()
        recovered = self.frame()
        with self.assertRaisesRegex(RuntimeError, "changed boot ID"):
            recovered.tick()
        recovered.adb.run.assert_not_called()

    def test_reboot_then_launch_once(self):
        frame = self.frame()
        frame.tick()
        frame.adb.run.assert_called_once_with("reboot", timeout=60)
        frame.adb.boot_id.return_value = "new-boot"
        frame.adb.shell.side_effect = lambda *args: "Status: ok" if args[:2] == ("am", "start") else "1"
        frame.tick()
        self.assertEqual(frame.state["completed_window"], "2026-09-18")
        recovered = self.frame()
        recovered.tick()
        recovered.adb.connect.assert_not_called()

    def test_failed_launch_retries_without_reboot(self):
        frame = self.frame()
        frame.tick()
        frame.adb.boot_id.return_value = "new-boot"
        frame.adb.shell.side_effect = lambda *args: "Error: Activity not found" if args[:2] == ("am", "start") else "1"
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "launch not confirmed"):
                frame.tick()
        frame.adb.run.assert_called_once_with("reboot", timeout=60)
        self.assertNotIn("completed_window", frame.state)

    def test_night_attempts_dimming_even_if_stop_fails(self):
        self.mock_time.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo("UTC"))
        frame = self.frame()
        frame.adb.shell.side_effect = [RuntimeError("stop failed"), "", ""]
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            frame.tick()
        frame.adb.shell.assert_any_call("settings", "put", "system", "screen_brightness_mode", "0")
        frame.adb.shell.assert_any_call("settings", "put", "system", "screen_brightness", "0")
        self.assertFalse(any(call.args == ("input", "keyevent", "223")
                             for call in frame.adb.shell.call_args_list))
        frame.adb.run.assert_not_called()

    def test_night_rechecks_and_retries_failed_brightness(self):
        self.mock_time.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo("UTC"))
        frame = self.frame()
        with patch.object(c.time, "monotonic", return_value=1000):
            frame.adb.shell.side_effect = ["", "", RuntimeError("brightness failed")]
            with self.assertRaisesRegex(RuntimeError, "brightness failed"):
                frame.tick()
            self.assertEqual(frame.last_night, 0)
            frame.adb.shell.side_effect = None
            frame.tick()
            self.assertEqual(frame.last_night, 1000)
            frame.adb.shell.reset_mock()
            frame.tick()
            frame.adb.shell.assert_not_called()
        with patch.object(c.time, "monotonic", return_value=1300):
            frame.tick()
            frame.adb.shell.assert_any_call("settings", "put", "system", "screen_brightness", "0")

    def test_launch_restores_day_brightness_before_start(self):
        for brightness in (None, 200):
            with self.subTest(brightness=brightness):
                if brightness is not None:
                    self.cfg["day_brightness"] = brightness
                frame = self.frame()
                frame.adb.shell.return_value = "Status: ok"
                frame.launch()
                calls = [call.args for call in frame.adb.shell.call_args_list]
                mode = ("settings", "put", "system", "screen_brightness_mode", "0")
                level = ("settings", "put", "system", "screen_brightness", str(brightness or 128))
                start = ("am", "start", "-W", "-n", self.cfg["component"])
                self.assertLess(calls.index(mode), calls.index(level))
                self.assertLess(calls.index(level), calls.index(start))

    def test_brightness_failure_prevents_morning_completion(self):
        self.cfg["morning_action"] = "restart_app"
        frame = self.frame()
        def shell(*args):
            if args[:4] == ("settings", "put", "system", "screen_brightness"):
                raise RuntimeError("brightness failed")
            return "1"
        frame.adb.shell.side_effect = shell
        with self.assertRaisesRegex(RuntimeError, "brightness failed"):
            frame.tick()
        self.assertNotIn("completed_window", frame.state)
        self.assertFalse(any(call.args[:2] == ("am", "start")
                             for call in frame.adb.shell.call_args_list))

    def test_corrupt_state_does_not_reset_reboot_history(self):
        (c.DATA / "test.state.json").write_text("broken")
        with self.assertRaises(ValueError):
            self.frame()

    def test_restart_app_mode_never_reboots(self):
        self.cfg["morning_action"] = "restart_app"
        frame = self.frame()
        frame.adb.shell.side_effect = lambda *args: "Status: ok" if args[:2] == ("am", "start") else "1"
        frame.tick()
        frame.adb.run.assert_not_called()
        frame.adb.boot_id.assert_not_called()
        self.assertIn("completed_window", frame.state)


if __name__ == "__main__":
    unittest.main()
