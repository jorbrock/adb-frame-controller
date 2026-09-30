"""Manual display overrides and schedule boundaries, with no real ADB calls."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
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
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.now = datetime(2026, 9, 18, 12, tzinfo=ZoneInfo('UTC'))
        for target, kwargs in [('datetime', {'wraps': datetime}), ('POWER', {})]:
            context = patch.object(c, target, **kwargs)
            setattr(self, target, context.start())
            self.addCleanup(context.stop)
        self.datetime.now.side_effect = lambda zone: self.now.astimezone(zone)
        context = patch.object(c.time, 'time', side_effect=lambda: self.now.timestamp())
        context.start()
        self.addCleanup(context.stop)
        self.cfg = dict(name='frame', address='192.0.2.1:5555', package='com.example.frame',
                        component='com.example.frame/.MainActivity', wake='07:00', sleep='22:00',
                        day_brightness=200, boot_delay_seconds=60, wyze_mac='AABBCCDDEEFF')
        self.frame = self.recover()

    def recover(self, scheduled=True):
        frame = c.Frame(deepcopy(self.cfg), 'UTC', scheduled, self.data)
        frame.adb = Mock()
        frame.adb.shell.side_effect = self.shell
        return frame

    def shell(self, *args, **kwargs):
        return 'Status: ok' if args[:2] == ('am', 'start') else ''

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def wake(self, frame=None):
        frame = frame or self.frame
        frame.request_action('wake', 'a' * 32)
        frame.tick()
        self.advance(self.cfg['boot_delay_seconds'])
        frame.tick()
        self.assertEqual(frame.state['manual']['phase'], 'completed')

    def sleep(self, frame=None):
        frame = frame or self.frame
        frame.request_action('sleep', 'b' * 32)
        frame.tick()
        self.advance(30)
        frame.tick()
        self.assertEqual(frame.state['manual']['phase'], 'completed')

    def test_wake_waits_full_boot_delay_then_initializes_in_order(self):
        frame = self.frame
        frame.request_action('wake', 'a' * 32)
        frame.adb.connect.assert_not_called()
        self.POWER.set_power.assert_not_called()
        frame.tick()
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], True)
        for seconds in (0, 30, 29):
            self.advance(seconds)
            frame.tick()
            frame.adb.connect.assert_not_called()
        self.advance(1)
        frame.tick()
        self.assertEqual(frame.adb.shell.call_args_list, [
            call('setprop', 'service.bootanim.exit', '1'),
            call('settings', 'put', 'system', 'screen_brightness_mode', '0'),
            call('settings', 'put', 'system', 'screen_brightness', '200'),
            call('sh', '-c', 'pidof com.example.frame || [ "$?" = 1 ]'),
            call('am', 'start', '-W', '-n', self.cfg['component'])])
        frame.adb.run.assert_not_called()
        self.assertEqual(frame.state['manual']['phase'], 'completed')

    def test_home_app_is_not_stopped_or_relaunched(self):
        self.frame.adb.shell.side_effect = lambda *args: '1234' if args[:2] == ('sh', '-c') else ''
        self.wake()
        self.assertFalse(any(args.args[:2] in (('am', 'start'), ('am', 'force-stop'))
                             for args in self.frame.adb.shell.call_args_list))
        self.frame.adb.shell.assert_any_call('settings', 'put', 'system', 'screen_brightness', '200')

    def test_five_attempts_30_seconds_apart_and_error_after_third(self):
        frame = self.frame
        frame.request_action('wake', 'a' * 32)
        frame.tick()
        frame.adb.connect.side_effect = RuntimeError('offline')
        self.advance(60)
        for attempt in range(1, 6):
            with self.assertLogs(c.LOG, level='ERROR' if attempt >= 3 else 'WARNING'):
                frame.tick()
            self.assertEqual(frame.adb.connect.call_count, attempt)
            if attempt == 3:
                status = frame.snapshot()['status']
                self.assertIn('3/5', status['error'])
                self.assertIn('3/5', frame.log_path.read_text())
            self.advance(29)
            frame.tick()
            self.assertEqual(frame.adb.connect.call_count, attempt)
            self.advance(1)
        self.assertEqual(frame.state['manual']['phase'], 'failed')
        frame.tick()
        self.assertEqual(frame.adb.connect.call_count, 5)
        self.POWER.set_power.assert_called_once()
        self.assertIn('5/5', frame.snapshot()['status']['error'])
        recovered = self.recover()
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.assertIn('5/5', recovered.snapshot()['status']['error'])

    def test_restart_preserves_boot_delay_and_connection_attempts(self):
        self.frame.request_action('wake', 'a' * 32)
        self.frame.tick()
        self.advance(59)
        recovered = self.recover()
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.advance(1)
        recovered.adb.connect.side_effect = RuntimeError('offline')
        recovered.tick()
        self.advance(29)
        recovered = self.recover()
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.advance(1)
        recovered.tick()
        self.assertEqual(recovered.state['manual']['connect_attempts'], 2)
        self.assertEqual(recovered.state['manual']['phase'], 'completed')
        self.POWER.set_power.assert_called_once()

    def test_can_recover_on_fourth_connection_after_error(self):
        self.frame.request_action('wake', 'a' * 32)
        self.frame.tick()
        self.advance(60)
        self.frame.adb.connect.side_effect = [RuntimeError('offline')] * 3 + [None]
        for _ in range(4):
            self.frame.tick()
            self.advance(30)
        self.assertEqual(self.frame.state['manual']['phase'], 'completed')
        self.assertNotIn('error', self.frame.snapshot()['status'])
        self.assertIn('3/5', self.frame.log_path.read_text())

    def test_brightness_or_launch_failure_does_not_report_success(self):
        for failure in ('settings', 'am', 'sh'):
            with self.subTest(failure=failure):
                self.frame.state = {}
                self.frame.request_action('wake', 'a' * 32)
                self.frame.tick()
                self.advance(60)
                def shell(*args):
                    if args[0] == failure:
                        raise RuntimeError('command failed')
                    return self.shell(*args)
                self.frame.adb.shell.side_effect = shell
                self.frame.tick()
                self.assertNotEqual(self.frame.state['manual']['phase'], 'completed')
                self.assertIn('command failed', self.frame.snapshot()['status']['error'])

    def test_sleep_grace_period_and_no_night_rechecks_across_restart(self):
        self.frame.request_action('sleep', 'b' * 32)
        self.frame.tick()
        self.frame.adb.shell.assert_called_once_with('svc', 'power', 'shutdown')
        self.POWER.set_power.assert_not_called()
        self.advance(29)
        frame = self.recover()
        frame.tick()
        self.POWER.set_power.assert_not_called()
        self.advance(1)
        frame.tick()
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], False)
        frame.adb.connect.assert_not_called()
        self.assertEqual(frame.state['power'], 'off')
        self.assertFalse(frame.schedule_paused())
        for _ in range(3):
            self.advance(3600)
            frame = self.recover()
            frame.tick()
            frame.adb.connect.assert_not_called()
        self.POWER.set_power.assert_called_once()

    def test_sleep_falls_back_to_reboot_poweroff_command(self):
        self.frame.adb.shell.side_effect = [RuntimeError('unsupported'), '']
        self.sleep()
        self.assertEqual(self.frame.adb.shell.call_args_list, [call('svc', 'power', 'shutdown'), call('reboot', '-p')])
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], False)

    def test_sleep_cuts_power_immediately_when_adb_is_unreachable(self):
        self.frame.adb.connect.side_effect = c.subprocess.TimeoutExpired('adb', 20)
        self.frame.request_action('sleep', 'b' * 32)
        self.frame.tick()
        self.frame.adb.shell.assert_not_called()
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], False)
        self.assertTrue(self.frame.state['manual']['forced'])
        self.assertEqual(self.frame.state['manual']['phase'], 'completed')

    def test_shutdown_fails_three_times_then_forces_power_off(self):
        self.frame.adb.shell.side_effect = RuntimeError('permission denied')
        self.frame.request_action('sleep', 'b' * 32)
        for attempt in (1, 2):
            self.frame.tick()
            self.POWER.set_power.assert_not_called()
            self.assertEqual(self.frame.state['manual']['shutdown_attempts'], attempt)
        recovered = self.recover()
        recovered.adb.shell.side_effect = RuntimeError('permission denied')
        recovered.tick()
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], False)
        self.assertEqual(recovered.adb.connect.call_count, 1)
        self.assertEqual(recovered.state['manual']['shutdown_attempts'], 3)
        self.assertEqual(recovered.state['manual']['phase'], 'completed')

    def test_shutdown_error_output_with_zero_exit_is_not_success(self):
        self.frame.adb.shell.side_effect = None
        self.frame.adb.shell.return_value = 'Failed to shutdown.'
        self.frame.request_action('sleep', 'b' * 32)
        for _ in range(3):
            self.frame.tick()
        self.assertTrue(self.frame.state['manual']['forced'])
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], False)

    def test_failed_plug_off_retries_without_repeating_android_shutdown(self):
        self.frame.request_action('sleep', 'b' * 32)
        self.frame.tick()
        self.advance(30)
        self.POWER.set_power.side_effect = RuntimeError('plug unavailable')
        self.frame.tick()
        self.assertEqual(self.frame.state['power'], 'unknown')
        recovered = self.recover()
        self.POWER.set_power.side_effect = None
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.assertEqual(recovered.state['manual']['phase'], 'completed')

    def test_scheduled_sleep_then_wake_even_after_restart_while_off(self):
        self.now = self.now.replace(hour=22)
        self.frame.tick()
        self.advance(30)
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['source'], 'scheduled')
        self.assertEqual(self.frame.state['power'], 'off')
        self.now = self.now.replace(day=19, hour=7, minute=0, second=0)
        frame = self.recover()
        frame.tick()
        frame.adb.connect.assert_not_called()
        self.POWER.set_power.assert_called_with(self.cfg['wyze_mac'], True)
        self.advance(60)
        frame.tick()
        self.assertEqual(frame.state['manual']['phase'], 'completed')
        recovered = self.recover()
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.assertEqual(self.POWER.set_power.call_count, 2)

    def test_wake_button_works_after_explicit_power_off(self):
        self.frame.state.update(power='off', power_paused=True)
        self.frame.save()
        self.wake()
        self.assertFalse(self.frame.schedule_paused())

    def test_missing_plug_fails_clearly_without_device_commands(self):
        self.frame.cfg['wyze_mac'] = ''
        with self.assertRaisesRegex(RuntimeError, 'Pair a Wyze'):
            self.frame.request_action('wake', 'a' * 32)
        self.frame.tick()
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'failed')
        self.assertIn('Pair a Wyze', self.frame.snapshot()['status']['error'])
        self.frame.adb.connect.assert_not_called()
        self.POWER.set_power.assert_not_called()

    def test_late_wake_holds_through_next_morning_until_sleep(self):
        self.now = self.now.replace(hour=23)
        self.wake()
        self.now = self.now.replace(day=19, hour=7)
        recovered = self.recover()
        with patch.object(recovered, 'app_health', return_value=None) as health:
            recovered.tick()
        recovered.adb.connect.assert_called_once()
        health.assert_called_once()
        self.POWER.set_power.assert_called_once()
        self.now = self.now.replace(hour=22)
        recovered.tick()
        self.assertEqual(recovered.state['manual']['action'], 'sleep')
        self.advance(30)
        recovered.tick()
        self.assertEqual(recovered.state['power'], 'off')

    def test_sleep_override_with_scheduler_disabled_survives_next_day(self):
        frame = self.recover(scheduled=False)
        self.sleep(frame)
        self.advance(86400)
        recovered = self.recover(scheduled=False)
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.POWER.set_power.assert_called_once()

    def test_overnight_schedule_sleeps_then_wakes_at_evening_boundary(self):
        self.cfg.update(wake='20:00', sleep='06:00')
        frame = self.recover()
        frame.tick()
        self.advance(30)
        frame.tick()
        self.assertEqual(frame.state['power'], 'off')
        self.now = self.now.replace(hour=20)
        frame.tick()
        self.POWER.set_power.assert_called_with(self.cfg['wyze_mac'], True)

    def test_wake_crossing_sleep_boundary_does_not_launch(self):
        self.now = self.now.replace(hour=21, minute=59, second=0)
        self.frame.request_action('wake', 'a' * 32)
        self.frame.tick()
        self.advance(60)
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['action'], 'sleep')
        self.assertFalse(any(args.args[:2] == ('am', 'start') for args in self.frame.adb.shell.call_args_list))

    def test_slow_connect_crossing_boundary_does_not_initialize(self):
        self.cfg['boot_delay_seconds'] = 0
        frame = self.recover()
        self.now = self.now.replace(hour=21, minute=59)
        frame.request_action('wake', 'a' * 32)
        frame.tick()
        frame.adb.connect.side_effect = lambda: self.advance(60)
        frame.tick()
        self.assertEqual(frame.state['manual']['phase'], 'cancelled')
        frame.adb.shell.assert_not_called()

    def test_shutdown_crossing_wake_boundary_finishes_then_wakes(self):
        self.now = self.now.replace(hour=6, minute=59, second=50)
        self.frame.request_action('sleep', 'b' * 32)
        self.frame.tick()
        self.advance(30)
        self.frame.tick()
        self.assertEqual(self.frame.state['power'], 'off')
        self.frame.tick()
        self.POWER.set_power.assert_called_with(self.cfg['wyze_mac'], True)

    def test_duplicate_busy_and_failed_journal_send_no_extra_commands(self):
        self.frame.request_action('wake', 'a' * 32)
        expiry = self.frame.state['override']['expires_at']
        self.frame.request_action('wake', 'a' * 32)
        with self.assertRaisesRegex(RuntimeError, 'already in progress'):
            self.frame.request_action('sleep', 'b' * 32)
        self.assertEqual(self.frame.state['override']['expires_at'], expiry)
        self.frame.state = {}
        with patch.object(self.frame, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.frame.request_action('wake', 'a' * 32)
        self.assertEqual(self.frame.state, {})
        self.POWER.set_power.assert_not_called()
        self.frame.adb.connect.assert_not_called()

    def test_dst_repeated_hour_does_not_undo_wake_or_sleep(self):
        zone = ZoneInfo('America/Los_Angeles')
        for boundary in ('wake', 'sleep'):
            with self.subTest(boundary=boundary):
                self.frame.cfg[boundary] = '01:30'
                first = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
                second = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
                self.assertEqual(self.frame.schedule_event(first), self.frame.schedule_event(second))
                self.assertEqual(self.frame.schedule_event(second)[0], boundary)
                self.frame.cfg[boundary] = self.cfg[boundary]

    def test_schedule_gap_starts_at_first_valid_minute(self):
        zone = ZoneInfo('America/Los_Angeles')
        self.frame.cfg['wake'] = '02:30'
        before = datetime(2026, 3, 8, 1, 59, tzinfo=zone)
        after = datetime(2026, 3, 8, 3, tzinfo=zone)
        self.assertEqual(self.frame.schedule_event(before), ('sleep', after.timestamp()))
        self.assertEqual(self.frame.schedule_event(after)[0], 'wake')

    def test_scheduled_failure_remains_visible_and_does_not_restart_attempts(self):
        self.frame.adb.connect.side_effect = RuntimeError('offline')
        self.frame.tick()
        self.advance(60)
        for _ in range(5):
            self.frame.tick()
            self.advance(30)
        self.assertEqual(self.frame.state['manual']['phase'], 'failed')
        self.assertEqual(self.frame.snapshot()['status']['result'], 'scheduled_wake_failed')
        recovered = self.recover()
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.POWER.set_power.assert_called_once()

    def test_connection_errors_are_shown_on_dashboard_and_log_page(self):
        import json
        from webui import create_app
        registry = c.FrameRegistry(dict(timezone='UTC', enabled=True, frames=[self.cfg]), self.data, [self.frame])
        (self.data / 'web-auth.json').write_text(json.dumps(dict(id='test')))
        client = create_app(registry.config, registry, self.data).test_client()
        with client.session_transaction() as session:
            session.update(account_id='test')
        self.frame.request_action('wake', 'a' * 32)
        self.frame.tick()
        self.advance(60)
        self.frame.adb.connect.side_effect = RuntimeError('offline')
        for _ in range(3):
            self.frame.tick()
            self.advance(30)
        for path in ('/', '/frames/frame/log'):
            page = client.get(path)
            self.assertIn(b'Wake attempt 3/5 failed', page.data)
            self.assertIn(b'offline', page.data)


if __name__ == '__main__':
    unittest.main()
