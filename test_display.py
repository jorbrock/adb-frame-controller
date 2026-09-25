"""Manual display overrides and schedule boundaries, with no real ADB calls."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import controller as c


class BoundaryTests(unittest.TestCase):
    def test_daily_boundary_before_at_and_after_sleep(self):
        zone = ZoneInfo("UTC")
        for now, expected in (
            (datetime(2026, 9, 18, 21, 59, tzinfo=zone), datetime(2026, 9, 18, 22, tzinfo=zone)),
            (datetime(2026, 9, 18, 22, tzinfo=zone), datetime(2026, 9, 19, 22, tzinfo=zone)),
            (datetime(2026, 9, 18, 23, tzinfo=zone), datetime(2026, 9, 19, 22, tzinfo=zone)),
            (datetime(2026, 9, 19, 1, tzinfo=zone), datetime(2026, 9, 19, 22, tzinfo=zone)),
        ):
            with self.subTest(now=now):
                self.assertEqual(c.next_boundary(now, "22:00"), expected.timestamp())

    def test_dst_gap_expires_at_first_available_minute(self):
        zone = ZoneInfo("America/Los_Angeles")
        now = datetime(2026, 3, 7, 23, tzinfo=zone)
        self.assertEqual(c.next_boundary(now, "02:30"), datetime(2026, 3, 8, 3, tzinfo=zone).timestamp())

    def test_dst_fold_is_one_daily_event(self):
        zone = ZoneInfo("America/Los_Angeles")
        before = datetime(2026, 11, 1, 0, tzinfo=zone)
        self.assertEqual(c.next_boundary(before, "01:30"), datetime(2026, 11, 1, 1, 30, tzinfo=zone).timestamp())
        after_first_event = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
        self.assertEqual(c.next_boundary(after_first_event, "01:30"), datetime(2026, 11, 2, 1, 30, tzinfo=zone).timestamp())


class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.now = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo("UTC"))
        self.origin = self.now
        clock = patch.object(c, "datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.side_effect = lambda zone: self.now.astimezone(zone)
        self.addCleanup(clock.stop)
        wall = patch.object(c.time, "time", side_effect=lambda: self.now.timestamp())
        wall.start()
        self.addCleanup(wall.stop)
        monotonic = patch.object(c.time, "monotonic", side_effect=lambda: 100000 + (self.now - self.origin).total_seconds())
        monotonic.start()
        self.addCleanup(monotonic.stop)
        self.cfg = dict(name="frame", address="192.0.2.1:5555", package="com.example.frame",
                        component="com.example.frame/.MainActivity", wake="07:00", sleep="22:00",
                        morning_action="restart_app", day_brightness=200,
                        boot_delay_seconds=0, night_recheck_seconds=300)

    def frame(self, scheduled=True):
        frame = c.Frame(self.cfg, "UTC", scheduled, self.data)
        frame.adb = Mock()
        frame.adb.boot_id.return_value = "old-boot"
        frame.adb.shell.side_effect = lambda *args, **kwargs: "Status: ok" if args[:2] == ("am", "start") else "1"
        frame.night = Mock(wraps=frame.night)
        frame.launch = Mock(wraps=frame.launch)
        return frame

    def wake(self, frame, token="a" * 32):
        frame.request_action("wake", token)
        frame.adb.connect.assert_not_called()  # HTTP request only journals the action.
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "completed")
        frame.adb.run.assert_not_called()
        frame.adb.boot_id.assert_not_called()

    def test_manual_sleep_uses_http_dim_without_adb(self):
        self.cfg["night_action"] = "dim"
        frame = self.frame()
        frame.request_action("sleep", "a" * 32)
        with patch.object(c, "urlopen") as request:
            request.return_value.__enter__.return_value.status = 200
            frame.tick()
            request.assert_called_once_with("http://192.0.2.1:53287/dim", timeout=20)
        self.assertEqual(frame.adb.mock_calls, [])
        self.assertEqual(frame.state["manual"]["phase"], "completed")
        self.assertIn("dim command completed", frame.state["manual"]["message"])

    def test_manual_night_reboot_launches_before_http_dim(self):
        self.cfg["night_action"] = "dim"
        frame = self.frame()
        frame.request_action("reboot", "a" * 32)
        frame.tick()
        frame.adb.boot_id.return_value = "new-boot"
        with patch.object(c, "urlopen") as request:
            def respond(*args, **kwargs):
                frame.launch.assert_called_once()
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.status = 200
                return response
            request.side_effect = respond
            frame.tick()
            request.assert_called_once_with("http://192.0.2.1:53287/dim", timeout=20)
        self.assertEqual(frame.state["manual"]["phase"], "completed")

    def test_late_wake_skips_night_rechecks_and_next_morning_until_next_sleep(self):
        frame = self.frame()
        self.wake(frame)
        frame.adb.shell.assert_any_call("settings", "put", "system", "screen_brightness", "200")
        frame.launch.assert_called_once()
        frame.adb.reset_mock()
        for hour, minute in ((0, 0), (6, 59), (7, 0), (12, 0), (21, 59)):
            self.now = datetime(2026, 9, 19, hour, minute, tzinfo=self.now.tzinfo)
            frame.tick()
            self.assertEqual(frame.snapshot()["mode"], "day")
            frame.night.assert_not_called()
            frame.adb.connect.assert_not_called()
        self.now = self.now.replace(hour=22, minute=0)
        frame.tick()
        self.assertNotIn("override", frame.state)
        frame.night.assert_called_once()
        self.assertEqual(frame.snapshot()["mode"], "night")
        frame.tick()
        frame.night.assert_called_once()
        self.now += timedelta(minutes=5)
        frame.tick()
        self.assertEqual(frame.night.call_count, 2)

    def test_restart_keeps_wake_hold_without_relaunch_or_dimming(self):
        self.wake(self.frame())
        self.now += timedelta(minutes=10)
        recovered = self.frame()
        recovered.tick()
        recovered.night.assert_not_called()
        recovered.launch.assert_not_called()
        self.assertEqual(recovered.snapshot()["override"]["until"], "2026-09-19T22:00:00+00:00")
        self.now += timedelta(days=2)
        recovered.tick()
        recovered.night.assert_called_once()
        self.assertNotIn("override", recovered.state)

    def test_manual_sleep_replaces_wake_and_stays_asleep_until_next_wake(self):
        frame = self.frame()
        self.wake(frame)
        frame.request_action("sleep", "b" * 32)
        frame.tick()
        frame.night.assert_called_once()
        self.assertEqual(frame.state["override"]["mode"], "night")
        self.assertEqual(frame.state["manual"]["phase"], "completed")
        self.now = self.now.replace(day=19, hour=6, minute=59)
        frame.tick()
        self.assertEqual(frame.launch.call_count, 1)
        self.now = self.now.replace(hour=7, minute=0)
        frame.tick()
        self.assertEqual(frame.launch.call_count, 2)
        self.assertNotIn("override", frame.state)
        frame.adb.run.assert_not_called()

    def test_daytime_sleep_prevents_scheduler_from_immediately_waking_frame(self):
        self.now = self.now.replace(hour=12)
        frame = self.frame()
        frame.request_action("sleep", "a" * 32)
        frame.tick()
        self.now += timedelta(minutes=6)
        frame.tick()
        frame.launch.assert_not_called()
        self.assertEqual(frame.night.call_count, 2)
        self.assertEqual(frame.snapshot()["override"]["until"], "2026-09-19T07:00:00+00:00")
        frame.request_action("wake", "b" * 32)
        frame.tick()
        frame.launch.assert_called_once()
        self.assertEqual(frame.snapshot()["mode"], "day")

    def test_overnight_schedule_wake_holds_until_following_morning_sleep(self):
        self.cfg.update(wake="20:00", sleep="06:00")
        self.now = self.now.replace(hour=8)
        frame = self.frame()
        self.wake(frame)
        self.assertEqual(frame.snapshot()["override"]["until"], "2026-09-19T06:00:00+00:00")
        self.now = self.now.replace(day=19, hour=5, minute=59)
        frame.tick()
        frame.night.assert_not_called()
        self.now += timedelta(minutes=1)
        frame.tick()
        frame.night.assert_called_once()

    def test_paused_scheduling_keeps_manual_mode_until_changed(self):
        frame = self.frame(scheduled=False)
        self.wake(frame)
        self.now += timedelta(days=2)
        frame.tick()
        frame.night.assert_not_called()
        self.assertNotIn("until", frame.snapshot()["override"])
        frame.request_action("sleep", "b" * 32)
        frame.tick()
        self.now += timedelta(days=2)
        frame.tick()
        self.assertEqual(frame.snapshot()["mode"], "night")
        self.assertEqual(frame.night.call_count, 2)
        recovered = self.frame(scheduled=True)
        recovered.tick()
        self.assertNotIn("override", recovered.state)

    def test_reboot_respects_wake_override_at_night_and_sleep_override_during_day(self):
        for action, hour in (("wake", 23), ("sleep", 12)):
            with self.subTest(action=action):
                # Each iteration gets a fresh device journal.
                self.cfg["name"] = action
                self.now = self.now.replace(hour=hour)
                frame = self.frame()
                frame.request_action(action, "a" * 32)
                frame.tick()
                frame.night.reset_mock()
                frame.launch.reset_mock()
                frame.request_reboot("b" * 32)
                frame.tick()
                frame.adb.boot_id.return_value = "new-boot"
                frame.tick()
                self.assertEqual(frame.state["manual"]["phase"], "completed")
                frame.adb.run.assert_called_once_with("reboot", timeout=60)
                if action == "wake":
                    frame.launch.assert_called_once()
                    frame.night.assert_not_called()
                else:
                    frame.night.assert_called_once()
                    frame.launch.assert_not_called()

    def test_reboot_crossing_override_expiry_restores_sleep(self):
        self.now = self.now.replace(hour=21, minute=59)
        frame = self.frame()
        self.wake(frame)
        frame.request_reboot("b" * 32)
        frame.tick()
        frame.launch.reset_mock()
        self.now += timedelta(minutes=2)
        frame.adb.boot_id.return_value = "new-boot"
        frame.tick()
        frame.launch.assert_not_called()
        frame.night.assert_called_once()
        self.assertEqual(frame.state["manual"]["phase"], "completed")
        self.assertNotIn("override", frame.state)

    def test_pre_v120_reboot_journal_resumes_without_another_reboot(self):
        frame = self.frame()
        frame.state["manual"] = dict(id="a" * 32, phase="rebooting",
                                    requested_at=self.now.timestamp(), previous_boot_id="old-boot")
        frame.save()
        recovered = self.frame()
        recovered.adb.boot_id.return_value = "new-boot"
        recovered.tick()
        recovered.adb.run.assert_not_called()
        recovered.night.assert_called_once()
        self.assertEqual(recovered.state["manual"]["phase"], "completed")

    def test_queued_wake_survives_restart_and_launches_once(self):
        frame = self.frame()
        frame.request_action("wake", "a" * 32)
        recovered = self.frame()
        recovered.tick()
        recovered.tick()
        recovered.launch.assert_called_once()
        recovered.night.assert_not_called()
        self.assertEqual(recovered.state["manual"]["phase"], "completed")

    def test_expired_queued_wake_never_launches_after_sleep_event(self):
        frame = self.frame()
        frame.request_action("wake", "a" * 32)
        self.now += timedelta(days=1)
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "cancelled")
        frame.launch.assert_not_called()
        frame.night.assert_called_once()

    def test_connection_crossing_expiry_does_not_apply_stale_wake(self):
        self.now = self.now.replace(hour=21, minute=59)
        frame = self.frame()
        frame.request_action("wake", "a" * 32)
        def connected():
            self.now = self.now.replace(hour=22, minute=0)
        frame.adb.connect.side_effect = connected
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "cancelled")
        frame.launch.assert_not_called()
        frame.night.assert_called_once()

    def test_failed_action_retries_and_times_out_without_reboot(self):
        frame = self.frame()
        frame.request_action("wake", "a" * 32)
        frame.adb.connect.side_effect = RuntimeError("unreachable")
        with self.assertLogs(c.LOG, level="WARNING"):
            frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "queued")
        self.now += timedelta(seconds=901)
        frame.tick()
        self.assertEqual(frame.state["manual"]["phase"], "failed")
        frame.tick()
        frame.night.assert_not_called()
        frame.adb.run.assert_not_called()
        self.assertEqual(frame.snapshot()["status"]["result"], "manual_wake_active")

    def test_duplicate_requests_do_not_renew_override_and_busy_actions_are_rejected(self):
        frame = self.frame()
        frame.request_action("wake", "a" * 32)
        expires = frame.state["override"]["expires_at"]
        frame.request_action("wake", "a" * 32)
        for action in ("wake", "sleep", "reboot"):
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                frame.request_action(action, "b" * 32)
        with self.assertRaisesRegex(RuntimeError, "different action"):
            frame.request_action("sleep", "a" * 32)
        frame.tick()
        self.now += timedelta(hours=1)
        frame.request_action("wake", "a" * 32)
        self.assertEqual(frame.state["override"]["expires_at"], expires)
        with frame.mutex:
            with self.assertRaisesRegex(RuntimeError, "busy"):
                frame.request_action("sleep", "b" * 32)

    def test_display_action_does_not_bypass_legacy_reboot_cooldown(self):
        frame = self.frame()
        frame.state["manual"] = dict(id="a" * 32, phase="completed", requested_at=self.now.timestamp())
        frame.save()
        frame.request_action("wake", "b" * 32)
        frame.tick()
        with self.assertRaisesRegex(RuntimeError, "two minutes"):
            frame.request_reboot("c" * 32)

    def test_save_failure_does_not_activate_an_override_or_send_commands(self):
        frame = self.frame()
        previous = deepcopy(frame.state)
        with patch.object(frame, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                frame.request_action("wake", "a" * 32)
        self.assertEqual(frame.state, previous)
        frame.adb.connect.assert_not_called()
        self.assertFalse(frame.wakeup.is_set())

    def test_rename_keeps_override_and_its_original_expiry(self):
        frame = self.frame()
        self.wake(frame)
        override = deepcopy(frame.state["override"])
        registry = c.FrameRegistry(dict(timezone="UTC", enabled=True, frames=[self.cfg]), self.data, [frame])
        registry.change("frame", {**self.cfg, "name": "renamed", "sleep": "21:00"}, registry.revision)
        recovered = c.Frame(registry.config["frames"][0], "UTC", True, self.data)
        self.assertEqual(recovered.state["override"], override)


if __name__ == "__main__":
    unittest.main()
