import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

import controller as c


class ADBTests(unittest.TestCase):
    def test_default_connection_does_not_request_root(self):
        adb = c.ADB("host:5555")
        with patch.object(adb, "run", side_effect=["connected", "device"]) as run:
            adb.connect()
        self.assertEqual(run.call_args_list, [
            call("connect", "host:5555", targeted=False), call("get-state")])

    def test_root_is_targeted_and_verified_before_commands_on_every_connection(self):
        adb = c.ADB("host:5555", run_as_root=True)
        for output in ("restarting adbd as root", "adbd is already running as root"):
            with self.subTest(output=output), patch.object(c.subprocess, "run") as run:
                run.side_effect = [Mock(returncode=0, stdout=value, stderr="") for value in
                                   ("connected", "device", output, "connected", "", "0", "")]
                adb.connect()
                adb.shell("am", "force-stop", "com.example.frame")
                self.assertEqual([args.args[0] for args in run.call_args_list], [
                    ["adb", "connect", "host:5555"],
                    ["adb", "-s", "host:5555", "get-state"],
                    ["adb", "-s", "host:5555", "root"],
                    ["adb", "connect", "host:5555"],
                    ["adb", "-s", "host:5555", "wait-for-device"],
                    ["adb", "-s", "host:5555", "shell", "id -u"],
                    ["adb", "-s", "host:5555", "shell", "am force-stop com.example.frame"]])
                self.assertEqual(run.call_args_list[4].kwargs["timeout"], 60)

    def test_root_refusal_stops_frame_commands_even_with_success_exit_code(self):
        adb = c.ADB("host:5555", run_as_root=True)
        with patch.object(adb, "run", side_effect=[
                "connected", "device", "adbd cannot run as root in production builds",
                "connected", "", "2000"]):
            with self.assertRaisesRegex(RuntimeError, "ADB root failed.*cannot run as root"):
                adb.connect()

    def test_root_command_and_reconnection_failures_stop_preparation(self):
        for responses in (["connected", "device", RuntimeError("root failed")],
                          ["connected", "device", "restarting", "connected",
                           c.subprocess.TimeoutExpired("adb", 60)]):
            adb = c.ADB("host:5555", run_as_root=True)
            with patch.object(adb, "run", side_effect=responses) as run:
                with self.assertRaises((RuntimeError, c.subprocess.TimeoutExpired)):
                    adb.connect()
                self.assertEqual(run.call_count, len(responses))


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
                        boot_delay_seconds=0)
        self.clock = patch.object(c, "datetime", wraps=datetime)
        self.mock_time = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.mock_time.now.return_value = datetime(2026, 9, 18, 8, tzinfo=ZoneInfo("UTC"))

    def frame(self):
        frame = c.Frame(self.cfg, "UTC")
        frame.adb = Mock()
        frame.adb.boot_id.return_value = "old-boot"
        frame.adb.shell.return_value = "1"
        return frame

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

    def test_corrupt_state_does_not_reset_reboot_history(self):
        (c.DATA / "test.state.json").write_text("broken")
        with self.assertRaises(ValueError):
            self.frame()


if __name__ == "__main__":
    unittest.main()
