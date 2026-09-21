from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import controller as c


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        context = patch.object(c, "DATA", Path(self.temp.name))
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(c, "datetime")
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
        frame.adb.shell.side_effect = lambda *args: "Status: ok" if args[:2] == ("am", "start") else "1"
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
