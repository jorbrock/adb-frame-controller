import tempfile
import unittest
from datetime import datetime, timedelta
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

    def test_reboot_exits_animation_only_after_boot_and_retries_failure(self):
        frame = self.frame()
        frame.tick()
        exit_animation = call("setprop", "service.bootanim.exit", "1")
        self.assertNotIn(exit_animation, frame.adb.shell.call_args_list)
        frame.adb.boot_id.return_value = "new-boot"
        frame.adb.shell.return_value = "0"
        with self.assertRaisesRegex(RuntimeError, "Android boot completion"):
            frame.tick()
        self.assertNotIn(exit_animation, frame.adb.shell.call_args_list)
        frame.adb.shell.side_effect = ["1", RuntimeError("setprop failed")]
        with self.assertRaisesRegex(RuntimeError, "setprop failed"):
            frame.tick()
        self.assertNotIn("ready_at", frame.state)
        self.assertNotIn("completed_window", frame.state)
        recovered = self.frame()
        recovered.adb.boot_id.return_value = "new-boot"
        recovered.adb.shell.side_effect = lambda *args, **kwargs: (
            "Status: ok" if args[:2] == ("am", "start") else "1")
        recovered.tick()
        calls = recovered.adb.shell.call_args_list
        self.assertLess(calls.index(call("getprop", "sys.boot_completed")), calls.index(exit_animation))
        self.assertLess(calls.index(exit_animation), calls.index(
            call("am", "start", "-W", "-n", self.cfg["component"])))
        recovered.adb.run.assert_not_called()
        self.assertIn("completed_window", recovered.state)

    def test_reboot_then_launch_once(self):
        frame = self.frame()
        frame.tick()
        frame.adb.run.assert_called_once_with("reboot", timeout=60)
        frame.adb.boot_id.return_value = "new-boot"
        frame.adb.shell.side_effect = lambda *args, **kwargs: "Status: ok" if args[:2] == ("am", "start") else "1"
        frame.tick()
        self.assertEqual(frame.state["completed_window"], "2026-09-18")
        recovered = self.frame()
        recovered.tick()
        recovered.adb.connect.assert_not_called()

    def test_morning_completion_logs_once_across_ticks_and_restart(self):
        self.cfg["morning_action"] = "restart_app"
        frame = self.frame()
        frame.adb.shell.side_effect = lambda *args, **kwargs: (
            "Status: ok" if args[:2] == ("am", "start") else "1")
        start = self.mock_time.now.return_value
        frame.tick()
        frame.adb.reset_mock()
        for seconds in (30, 60):
            self.mock_time.now.return_value = start + timedelta(seconds=seconds)
            frame.tick()
        frame.adb.connect.assert_not_called()
        recovered = self.frame()
        self.mock_time.now.return_value = start + timedelta(seconds=90)
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        records = list(c.frame_log.entries(frame.log_path))
        self.assertEqual([r["result"] for r in records], ["morning_sequence_completed"])
        self.assertEqual(records[0]["updated_at"], start.isoformat())
        self.assertEqual(c.read_json(frame.status_path, {})["updated_at"],
                         (start + timedelta(seconds=90)).isoformat())
        # A later morning must still record its successful launch.
        self.mock_time.now.return_value = start.replace(hour=23)
        recovered.tick()
        self.mock_time.now.return_value = start + timedelta(days=1)
        recovered.adb.shell.side_effect = lambda *args, **kwargs: (
            "Status: ok" if args[:2] == ("am", "start") else "1")
        recovered.tick()
        self.assertEqual([r["result"] for r in c.frame_log.entries(frame.log_path)],
                         ["morning_sequence_completed", "night_commands_sent",
                          "morning_sequence_completed"])

    def test_http_undim_retries_then_completes_across_restart_without_adb(self):
        self.cfg["morning_action"] = "undim"
        frame = self.frame()
        with patch.object(c, "urlopen") as request:
            request.side_effect = OSError("unreachable")
            with self.assertRaisesRegex(RuntimeError, "ImmichFrame undim failed"):
                frame.tick()
            self.assertNotIn("completed_window", frame.state)
            request.side_effect = None
            request.return_value.__enter__.return_value.status = 200
            frame.tick()
            request.assert_called_with("http://192.0.2.1:53287/undim", timeout=20)
            recovered = self.frame()
            recovered.tick()
            self.assertEqual(request.call_count, 2)
            self.assertEqual(frame.adb.mock_calls, [])
            self.assertEqual(recovered.adb.mock_calls, [])
            self.mock_time.now.return_value += timedelta(days=1)
            recovered.tick()
            self.assertEqual(request.call_count, 3)

    def test_http_dim_rechecks_and_retries_without_adb(self):
        self.cfg["night_action"] = "dim"
        self.mock_time.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo("UTC"))
        frame = self.frame()
        with patch.object(c, "urlopen") as request, patch.object(c.time, "monotonic", return_value=1000) as clock:
            request.return_value.__enter__.return_value.status = 500
            with self.assertRaisesRegex(RuntimeError, "ImmichFrame dim failed.*500"):
                frame.tick()
            self.assertEqual(frame.last_night, 0)
            request.return_value.__enter__.return_value.status = 200
            frame.tick()
            frame.tick()
            self.assertEqual(request.call_count, 2)
            clock.return_value = 1300
            frame.tick()
            self.assertEqual(request.call_count, 3)
            request.assert_called_with("http://192.0.2.1:53287/dim", timeout=20)
        self.assertEqual(frame.adb.mock_calls, [])

    def test_failed_launch_retries_without_reboot(self):
        frame = self.frame()
        frame.tick()
        frame.adb.boot_id.return_value = "new-boot"
        frame.adb.shell.side_effect = lambda *args, **kwargs: "Error: Activity not found" if args[:2] == ("am", "start") else "1"
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "launch not confirmed"):
                frame.tick()
        frame.adb.run.assert_called_once_with("reboot", timeout=60)
        self.assertNotIn("completed_window", frame.state)

    def test_cache_trim_precedes_reboot(self):
        frame = self.frame()
        frame.tick()
        calls = frame.adb.mock_calls
        stop = call.shell("am", "force-stop", self.cfg["package"])
        trim = call.shell("pm", "trim-caches", "999G", timeout=120)
        self.assertLess(calls.index(stop), calls.index(trim))
        self.assertLess(calls.index(trim), calls.index(call.run("reboot", timeout=60)))
        self.assertEqual(frame.state["cache_trimmed_window"], "2026-09-18")

    def test_cache_trim_failure_retries_before_reboot(self):
        frame = self.frame()
        frame.adb.shell.side_effect = ["", RuntimeError("trim failed")]
        with self.assertRaisesRegex(RuntimeError, "trim failed"):
            frame.tick()
        frame.adb.run.assert_not_called()
        self.assertNotIn("attempted_window", frame.state)
        self.assertNotIn("cache_trimmed_window", frame.state)
        frame.adb.shell.side_effect = None
        frame.tick()
        frame.adb.run.assert_called_once_with("reboot", timeout=60)

    def test_app_only_trim_survives_failed_launch_and_controller_restart(self):
        self.cfg["morning_action"] = "restart_app"
        frame = self.frame()
        frame.adb.shell.side_effect = lambda *args, **kwargs: (
            "Error: unavailable" if args[:2] == ("am", "start") else "1")
        with self.assertRaisesRegex(RuntimeError, "launch not confirmed"):
            frame.tick()
        calls = frame.adb.shell.call_args_list
        trim = call("pm", "trim-caches", "999G", timeout=120)
        self.assertLess(calls.index(call("am", "force-stop", self.cfg["package"])),
                        calls.index(trim))
        self.assertLess(calls.index(trim), calls.index(
            call("am", "start", "-W", "-n", self.cfg["component"])))
        recovered = self.frame()
        recovered.adb.shell.side_effect = lambda *args, **kwargs: (
            "Status: ok" if args[:2] == ("am", "start") else "1")
        recovered.tick()
        self.assertNotIn(trim, recovered.adb.shell.call_args_list)
        self.assertEqual(recovered.state["completed_window"], "2026-09-18")
        self.mock_time.now.return_value = datetime(2026, 9, 19, 8, tzinfo=ZoneInfo("UTC"))
        recovered.tick()
        self.assertIn(trim, recovered.adb.shell.call_args_list)

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
        def shell(*args, **kwargs):
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
        frame.adb.shell.side_effect = lambda *args, **kwargs: "Status: ok" if args[:2] == ("am", "start") else "1"
        frame.tick()
        frame.adb.run.assert_not_called()
        frame.adb.boot_id.assert_not_called()
        self.assertIn("completed_window", frame.state)
        self.assertNotIn(call("setprop", "service.bootanim.exit", "1"),
                         frame.adb.shell.call_args_list)


if __name__ == "__main__":
    unittest.main()
