"""Daytime watchdog diagnostics, escalation, and durable recovery safety."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

import controller as c
from test_configuration import config


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.cfg = config()['frames'][0]
        self.cfg.update(wyze_mac='AABBCCDDEEFF', boot_delay_seconds=60)
        for target, kwargs in [('POWER', {}), ('datetime', {'wraps': datetime})]:
            context = patch.object(c, target, **kwargs)
            setattr(self, target, context.start())
            self.addCleanup(context.stop)
        self.datetime.now.return_value = datetime(2026, 9, 18, 12, tzinfo=ZoneInfo('UTC'))
        context = patch.object(c.time, 'time', return_value=1000)
        self.time = context.start()
        self.addCleanup(context.stop)
        self.running = '123'
        self.flags = 'crashing=false notResponding=false'
        self.focus = self.cfg['component']
        self.start_result = 'Status: ok'
        self.frame = self.recover()
        self.frame.state['schedule_event'] = list(self.frame.schedule_event(self.datetime.now()))
        self.frame.state['next_health_check_at'] = 1000
        self.frame.save()

    def recover(self):
        frame = c.Frame(deepcopy(self.cfg), 'UTC', True, self.data)
        frame.adb = Mock()
        frame.adb.boot_id.return_value = 'old-boot'
        frame.adb.shell.side_effect = self.shell
        return frame

    def shell(self, *args, **kwargs):
        if args[:2] == ('sh', '-c'):
            return self.running
        if args[:3] == ('dumpsys', 'activity', 'processes'):
            return f'  *APP* UID 1000 ProcessRecord{{abc 123:{self.cfg["package"]}/u0a1}}\n    {self.flags}\n'
        if args[:2] == ('dumpsys', 'window'):
            return f'mCurrentFocus=Window{{abc u0 {self.focus}}}\n'
        if args[:2] == ('am', 'start'):
            return self.start_result
        if args == ('echo', 'frame-health'):
            return 'frame-health'
        if args == ('getprop', 'sys.boot_completed'):
            return '1'
        return ''

    def advance(self, seconds=30):
        self.time.return_value += seconds

    def test_healthy_poll_is_read_only_and_every_five_minutes_across_restart(self):
        self.frame.tick()
        self.assertEqual(self.frame.snapshot()['status']['result'], 'monitoring_healthy')
        self.frame.adb.run.assert_not_called()
        self.POWER.set_power.assert_not_called()
        self.assertFalse(any(x.args[0] in ('am', 'pm', 'settings') for x in self.frame.adb.shell.call_args_list))
        recovered = self.recover()
        self.advance(299)
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.advance(1)
        recovered.tick()
        recovered.adb.connect.assert_called_once()

    def test_skips_disabled_asleep_off_unknown_paused_and_manual_sleep(self):
        for change in ('disabled', 'night', 'off', 'unknown', 'paused', 'sleep'):
            with self.subTest(change=change):
                frame = self.recover()
                self.datetime.now.return_value = datetime(2026, 9, 18, 12, tzinfo=ZoneInfo('UTC'))
                if change == 'disabled':
                    frame.cfg['enabled'] = False
                elif change == 'night':
                    self.datetime.now.return_value = self.datetime.now().replace(hour=23)
                    frame.state['schedule_event'] = list(frame.schedule_event(self.datetime.now()))
                elif change in ('off', 'unknown'):
                    frame.state['power'] = change
                elif change == 'paused':
                    frame.state['power_paused'] = True
                else:
                    frame.state['override'] = dict(mode='night', expires_at=9999999999)
                frame.tick()
                frame.adb.connect.assert_not_called()
                self.POWER.set_power.assert_not_called()

    def test_monitors_with_schedule_disabled_or_manual_wake_at_night(self):
        for override in (False, True):
            frame = self.recover()
            frame.scheduled = override
            frame.state['next_health_check_at'] = self.time()
            if override:
                self.datetime.now.return_value = self.datetime.now().replace(hour=23)
                frame.state['override'] = dict(mode='day', expires_at=9999999999)
            frame.tick()
            frame.adb.connect.assert_called_once()

    def test_detects_missing_background_crashing_and_anr(self):
        for fault in ('missing', 'background', 'crashing', 'anr'):
            with self.subTest(fault=fault):
                self.running = '' if fault == 'missing' else '123'
                self.focus = 'other.package/.Main' if fault == 'background' else self.cfg['component']
                self.flags = 'crashing=true' if fault == 'crashing' else 'notResponding=true' if fault == 'anr' else ''
                self.assertIsNotNone(self.frame.app_health())

    def test_other_app_anr_and_historical_anr_do_not_trigger_reset(self):
        process = self.shell('dumpsys', 'activity', 'processes')
        process += '\n  *APP* UID 1001 ProcessRecord{def 124:other.app/u0a2}\n    notResponding=true\n'
        original = self.shell
        self.frame.adb.shell.side_effect = lambda *a, **kw: process if a[:3] == ('dumpsys', 'activity', 'processes') else original(*a, **kw)
        self.assertIsNone(self.frame.app_health())

    def test_similarly_named_foreground_package_is_not_immichframe(self):
        self.focus = 'other.' + self.cfg['component']
        self.assertEqual(self.frame.app_health(), 'ImmichFrame is not the foreground app')

    def test_connection_crossing_sleep_boundary_does_not_send_reboot(self):
        job = dict(source='monitoring', action='reboot', phase='queued',
                   requested_at=self.time(), message='restarting')
        self.frame.state['manual'] = job
        self.frame.adb.connect.side_effect = lambda: setattr(
            self.datetime.now, 'return_value', self.datetime.now().replace(hour=23))
        self.assertFalse(self.frame.recovery_tick(job))
        self.assertEqual(job['phase'], 'cancelled')
        self.frame.adb.run.assert_not_called()

    def test_reboot_disconnect_keeps_full_boot_delay_before_retry(self):
        self.frame.cfg['boot_delay_seconds'] = 600
        self.frame.state['manual'] = dict(source='monitoring', action='reboot',
                                         phase='queued', requested_at=self.time(), message='restarting')
        self.frame.adb.run.side_effect = RuntimeError('connection dropped during reboot')
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['next_attempt_at'], self.time() + 600)
        self.advance(599)
        self.frame.adb.connect.reset_mock()
        self.frame.tick()
        self.frame.adb.connect.assert_not_called()

    def test_missing_diagnostic_is_error_not_reboot_when_adb_works(self):
        original = self.shell
        self.frame.adb.shell.side_effect = lambda *a, **kw: '' if a[:2] == ('dumpsys', 'window') else original(*a, **kw)
        self.frame.tick()
        self.assertEqual(self.frame.snapshot()['status']['result'], 'monitoring_error')
        self.assertNotIn('manual', self.frame.state)
        self.POWER.set_power.assert_not_called()

    def test_reset_stops_trims_launches_and_verifies_after_settling(self):
        self.running = ''
        self.frame.tick()
        calls = self.frame.adb.shell.call_args_list
        stop = call('am', 'force-stop', self.cfg['package'])
        trim = call('pm', 'trim-caches', '999G', timeout=120)
        launch = call('am', 'start', '-W', '-n', self.cfg['component'])
        self.assertLess(calls.index(stop), calls.index(trim))
        self.assertLess(calls.index(trim), calls.index(launch))
        self.assertEqual(self.frame.state['manual']['phase'], 'starting')
        self.running = '456'
        recovered = self.recover()
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.advance()
        recovered.tick()
        self.assertEqual(recovered.state['manual']['phase'], 'completed')
        self.assertNotIn(trim, recovered.adb.shell.call_args_list)

    def queue_soft_reboot(self):
        self.focus = 'other.app/.Main'
        self.frame.tick()
        self.advance()
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['action'], 'reboot')
        self.frame.tick()
        self.frame.adb.run.assert_called_once_with('reboot', timeout=60)

    def test_failed_verification_soft_reboots_once_and_shares_wake_startup(self):
        self.queue_soft_reboot()
        recovered = self.recover()
        recovered.adb.boot_id.return_value = 'new-boot'
        self.advance(59)
        recovered.tick()
        recovered.adb.connect.assert_not_called()
        self.advance(1)
        self.focus = self.cfg['component']
        recovered.tick()
        recovered.adb.shell.assert_any_call('setprop', 'service.bootanim.exit', '1')
        recovered.adb.shell.assert_any_call('settings', 'put', 'system', 'screen_brightness', str(self.cfg['day_brightness']))
        recovered.adb.shell.assert_any_call('am', 'start', '-W', '-n', self.cfg['component'])
        self.advance()
        recovered.tick()
        self.assertEqual(recovered.state['manual']['phase'], 'completed')
        recovered.adb.run.assert_not_called()

    def test_failed_launch_escalates_to_soft_reboot(self):
        self.running = ''
        self.start_result = 'Error: activity failed'
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['action'], 'reboot')

    def test_dead_adb_hard_cycles_waits_boot_delay_and_runs_startup(self):
        self.frame.adb.connect.side_effect = RuntimeError('offline')
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'power_wait')
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], False)
        self.advance()
        self.frame.tick()
        self.assertEqual(self.POWER.set_power.call_args, call(self.cfg['wyze_mac'], True))
        self.frame.adb.connect.side_effect = None
        self.frame.adb.connect.reset_mock()
        self.advance(59)
        self.frame.tick()
        self.frame.adb.connect.assert_not_called()
        self.advance(1)
        self.frame.tick()
        self.frame.adb.shell.assert_any_call('setprop', 'service.bootanim.exit', '1')
        self.advance()
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'completed')

    def test_soft_reboot_unreachable_falls_back_to_plug_after_boot_retries(self):
        self.queue_soft_reboot()
        self.frame.adb.connect.side_effect = RuntimeError('offline')
        self.advance(60)
        for _ in range(c.WAKE_ATTEMPTS):
            self.frame.tick()
            self.advance()
        self.assertEqual(self.frame.state['manual']['action'], 'hard_reboot')
        self.frame.tick()
        self.POWER.set_power.assert_called_once_with(self.cfg['wyze_mac'], False)

    def test_unchanged_boot_id_is_bounded_and_does_not_duplicate_reboot(self):
        self.queue_soft_reboot()
        self.advance(60)
        for _ in range(c.WAKE_ATTEMPTS):
            self.frame.tick()
            self.advance()
        self.assertEqual(self.frame.state['manual']['phase'], 'failed')
        self.frame.adb.run.assert_called_once()
        self.POWER.set_power.assert_not_called()
        self.assertGreater(self.frame.state['next_health_check_at'], self.time())

    def test_sleep_boundary_cancels_reset_without_restart(self):
        self.running = ''
        self.frame.tick()
        self.frame.adb.reset_mock()
        self.datetime.now.return_value = self.datetime.now().replace(hour=23)
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['action'], 'sleep')
        self.assertFalse(any(x.args[:2] == ('am', 'start') for x in self.frame.adb.shell.call_args_list))

    def test_interrupted_power_cycle_restores_power_even_across_sleep_boundary(self):
        self.frame.adb.connect.side_effect = RuntimeError('offline')
        self.frame.tick()
        self.advance(10)
        recovered = self.recover()
        self.datetime.now.return_value = self.datetime.now().replace(hour=23)
        self.advance(29)
        recovered.tick()
        self.POWER.set_power.assert_called_once()
        self.advance(1)
        recovered.tick()
        self.assertEqual(self.POWER.set_power.call_args_list, [call(self.cfg['wyze_mac'], False), call(self.cfg['wyze_mac'], True)])
        recovered.tick()
        self.assertEqual(recovered.state['manual']['action'], 'sleep')
        self.assertFalse(any(x.args[:2] == ('am', 'start') for x in recovered.adb.shell.call_args_list))

    def test_ambiguous_off_and_failed_restore_never_repeat_off(self):
        self.frame.adb.connect.side_effect = RuntimeError('offline')
        self.POWER.set_power.side_effect = RuntimeError('plug timeout')
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'power_wait')
        for _ in range(7):
            self.advance()
            self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'power_on_pending')
        self.assertEqual(sum(x == call(self.cfg['wyze_mac'], False) for x in self.POWER.set_power.call_args_list), 1)
        self.POWER.set_power.side_effect = None
        self.advance()
        self.frame.tick()
        self.assertEqual(self.frame.state['power'], 'on')

    def test_missing_plug_reports_failure_without_plug_commands(self):
        self.frame.cfg['wyze_mac'] = ''
        self.frame.adb.connect.side_effect = RuntimeError('offline')
        for _ in range(c.WAKE_ATTEMPTS):
            self.frame.tick()
            self.advance()
        self.assertEqual(self.frame.state['manual']['phase'], 'failed')
        self.assertIn('pair a Wyze plug', self.frame.state['manual']['message'])
        self.POWER.set_power.assert_not_called()


if __name__ == '__main__':
    unittest.main()
