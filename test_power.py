"""Power safety, restart recovery, Wyze SDK boundary, and authenticated UI."""
from copy import deepcopy
from datetime import datetime
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch
from zoneinfo import ZoneInfo

import controller as c
from test_configuration import config
import test_webui
from wyze_power import PowerError, WyzePower, normalize_mac


class PowerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.cfg = config()
        self.cfg['enabled'] = True
        self.cfg['frames'][0].update(wyze_mac='AABBCCDDEEFF', boot_delay_seconds=0)
        for target, kwargs in [('POWER', {}), ('datetime', {'wraps': datetime})]:
            context = patch.object(c, target, **kwargs)
            setattr(self, target, context.start())
            self.addCleanup(context.stop)
        self.datetime.now.return_value = datetime(2026, 9, 18, 12, tzinfo=ZoneInfo('UTC'))
        context = patch.object(c.time, 'time', return_value=1000)
        self.time = context.start()
        self.addCleanup(context.stop)
        self.frame = self.recover()

    def recover(self):
        frame = c.Frame(deepcopy(self.cfg['frames'][0]), 'UTC', True, self.data)
        frame.adb = Mock()
        frame.adb.boot_id.return_value = 'new-boot'
        frame.adb.shell.side_effect = lambda *args, **kwargs: 'Status: ok' if args[:2] == ('am', 'start') else '1'
        return frame

    def test_off_stops_all_device_work_and_persists_across_restart(self):
        self.frame.request_action('power_off', 'a' * 32)
        self.frame.tick()
        self.POWER.set_power.assert_called_once_with('AABBCCDDEEFF', False)
        self.assertEqual(self.frame.state['power'], 'off')
        restored = self.recover()
        for hour in (8, 23):
            self.datetime.now.return_value = datetime(2026, 9, 19, hour, tzinfo=ZoneInfo('UTC'))
            restored.tick()
        self.frame.adb.connect.assert_not_called()
        restored.adb.connect.assert_not_called()
        for action in ('reset_app', 'reboot', 'hard_reboot'):
            with self.assertRaisesRegex(RuntimeError, 'Power it on'):
                restored.request_action(action, 'b' * 32)
        restored.request_action('power_off', 'a' * 32)  # idempotent replay
        restored.tick()
        self.POWER.set_power.assert_called_once()

    def test_hard_reboot_requires_no_adb_and_waits_30_seconds(self):
        frame = self.frame
        frame.request_reboot('a' * 32, 'hard')
        frame.tick()
        frame.adb.connect.assert_not_called()
        self.assertEqual(frame.state['manual']['phase'], 'power_wait')
        self.time.return_value = 1029.99
        frame.tick()
        self.POWER.set_power.assert_called_once_with('AABBCCDDEEFF', False)
        self.time.return_value = 1030
        frame.tick()
        self.assertEqual(self.POWER.set_power.call_args_list, [call('AABBCCDDEEFF', False), call('AABBCCDDEEFF', True)])
        frame.tick()
        self.assertEqual(frame.state['manual']['phase'], 'completed')
        frame.tick()
        frame.adb.run.assert_not_called()  # also suppresses a second scheduled reboot
        frame.adb.shell.assert_any_call('am', 'start', '-W', '-n', frame.cfg['component'])

    def test_restart_during_power_cycle_does_not_repeat_off(self):
        self.frame.request_reboot('a' * 32, 'hard')
        self.frame.tick()
        self.time.return_value = 1010
        frame = self.recover()
        self.time.return_value = 1039
        frame.tick()
        self.POWER.set_power.assert_called_once()
        self.time.return_value = 1040
        frame.tick()
        self.assertEqual(self.POWER.set_power.call_args_list, [call('AABBCCDDEEFF', False), call('AABBCCDDEEFF', True)])
        frame = self.recover()
        frame.tick()
        self.POWER.set_power.assert_called_with('AABBCCDDEEFF', True)
        self.assertEqual(self.POWER.set_power.call_count, 2)
        self.assertEqual(frame.state['manual']['phase'], 'completed')

    def test_ambiguous_off_failure_still_restores_power_without_repeating_off(self):
        self.POWER.set_power.side_effect = [PowerError('connection lost'), None]
        self.frame.request_reboot('a' * 32, 'hard')
        with self.assertLogs(c.LOG, level='WARNING'):
            self.frame.tick()
        self.assertTrue(self.frame.power_suspended())
        self.assertEqual(self.frame.state['manual']['phase'], 'power_wait')
        self.time.return_value = 1030
        self.frame.tick()
        self.assertEqual(self.POWER.set_power.call_args_list, [call('AABBCCDDEEFF', False), call('AABBCCDDEEFF', True)])
        self.assertFalse(self.frame.power_suspended())
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'failed')
        self.assertIn('not confirmed', self.frame.state['manual']['message'])

    def test_restore_retries_even_after_job_timeout_and_restart(self):
        self.frame.request_reboot('a' * 32, 'hard')
        self.frame.tick()
        self.POWER.set_power.side_effect = PowerError('offline')
        self.time.return_value = 2000
        with self.assertLogs(c.LOG, level='WARNING'):
            self.frame.tick()
        restored = self.recover()
        self.assertEqual(restored.state['manual']['phase'], 'power_on_pending')
        self.POWER.set_power.side_effect = None
        restored.tick()
        self.assertEqual(restored.state['power'], 'on')
        restored.tick()
        self.assertEqual(restored.state['manual']['phase'], 'completed')

    def test_on_restores_display_without_sending_adb_reboot(self):
        self.frame.request_action('power_off', 'a' * 32)
        self.frame.tick()
        self.frame.request_action('power_on', 'b' * 32)
        self.frame.tick()
        self.frame.adb.connect.assert_not_called()
        self.frame.tick()
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'completed')
        self.assertEqual(self.frame.state['power'], 'on')
        self.frame.adb.run.assert_not_called()

    def test_power_off_failure_is_unknown_and_suppresses_monitoring(self):
        self.POWER.set_power.side_effect = PowerError('offline')
        self.frame.request_action('power_off', 'a' * 32)
        with self.assertLogs(c.LOG, level='WARNING'):
            self.frame.tick()
        self.time.return_value = 2000
        self.frame.tick()  # timeout
        self.frame.tick()  # still no schedule or monitoring
        self.assertEqual(self.frame.state['power'], 'unknown')
        self.assertEqual(self.frame.state['manual']['phase'], 'failed')
        self.frame.adb.connect.assert_not_called()

    def test_no_power_command_if_intent_cannot_be_saved(self):
        self.frame.request_reboot('a' * 32, 'hard')
        with patch.object(self.frame, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.frame.tick()
        self.POWER.set_power.assert_not_called()

    def test_hard_reboot_respects_night_mode(self):
        self.datetime.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo('UTC'))
        self.frame.request_reboot('a' * 32, 'hard')
        self.frame.tick()
        self.time.return_value = 1030
        self.frame.tick()
        self.frame.tick()
        self.frame.adb.shell.assert_any_call('svc', 'power', 'shutdown')
        self.time.return_value = 1060
        self.frame.tick()
        self.assertEqual(self.frame.state['power'], 'off')
        self.assertEqual(self.frame.state['manual']['phase'], 'completed')
        self.assertFalse(any(x.args[:2] == ('am', 'start') for x in self.frame.adb.shell.call_args_list))

    def test_pairing_required_cooldown_and_overlapping_actions(self):
        self.frame.cfg['wyze_mac'] = ''
        with self.assertRaisesRegex(RuntimeError, 'Pair a Wyze'):
            self.frame.request_reboot('a' * 32, 'hard')
        self.frame.cfg['wyze_mac'] = 'AABBCCDDEEFF'
        self.frame.request_reboot('a' * 32, 'hard')
        self.frame.request_reboot('a' * 32, 'hard')
        with self.assertRaisesRegex(RuntimeError, 'already in progress'):
            self.frame.request_action('power_off', 'b' * 32)
        self.frame.state['manual']['phase'] = 'completed'
        with self.assertRaisesRegex(RuntimeError, 'two minutes'):
            self.frame.request_reboot('b' * 32)

    def test_global_wake_includes_off_frames_and_edit_cannot_interrupt_cycle(self):
        registry = c.FrameRegistry(self.cfg, self.data, [self.frame])
        self.frame.request_action('power_off', 'a' * 32)
        self.frame.tick()
        with self.assertRaisesRegex(RuntimeError, 'Power the frame on'):
            registry.change('living-room', {**self.frame.cfg, 'wyze_mac': ''}, registry.revision)
        self.assertEqual(registry.request_all('wake', 'b' * 32), (['living-room'], {}))
        self.frame.tick()
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'completed')
        self.frame.request_reboot('c' * 32, 'hard')
        self.frame.tick()
        for cfg in (None, {**self.frame.cfg, 'enabled': False}, self.frame.cfg):
            with self.assertRaisesRegex(RuntimeError, 'power action'):
                registry.change('living-room', cfg, registry.revision)

    def test_soft_reboot_shutdown_cannot_be_interrupted_by_disabling_management(self):
        self.datetime.now.return_value = datetime(2026, 9, 18, 23, tzinfo=ZoneInfo('UTC'))
        registry = c.FrameRegistry(self.cfg, self.data, [self.frame])
        self.frame.request_reboot('a' * 32)
        self.frame.adb.boot_id.return_value = 'old-boot'
        self.frame.tick()
        self.time.return_value = 1890  # Boot completed near the reboot timeout.
        self.frame.adb.boot_id.return_value = 'new-boot'
        self.frame.tick()
        self.assertEqual(self.frame.state['manual']['phase'], 'shutdown_wait')
        with self.assertRaisesRegex(RuntimeError, 'power action'):
            registry.change('living-room', {**self.frame.cfg, 'enabled': False}, registry.revision)
        self.time.return_value = 1920
        self.frame.tick()
        self.POWER.set_power.assert_called_once_with('AABBCCDDEEFF', False)
        self.assertEqual(self.frame.state['manual']['phase'], 'completed')

    def test_mac_validation_and_duplicates(self):
        self.assertEqual(normalize_mac(' aa:bb:cc:dd:ee:ff '), 'AABBCCDDEEFF')
        self.assertEqual(normalize_mac('abcdef1234567890'), 'ABCDEF1234567890')
        for value in ('x', 'ABCD', None, 12):
            with self.assertRaises(ValueError):
                normalize_mac(value)
        cfg = deepcopy(self.cfg)
        cfg['frames'].append({**cfg['frames'][0], 'name': 'other', 'address': '192.0.2.2:5555', 'wyze_mac': 'aa:bb:cc:dd:ee:ff'})
        with self.assertRaisesRegex(ValueError, 'only one frame'):
            c.validate_config(cfg)


class WyzeTests(unittest.TestCase):
    def setUp(self):
        context = patch.dict(os.environ, {'WYZE_ACCESS_TOKEN': 'secret', 'WYZE_REFRESH_TOKEN': 'refresh'}, clear=True)
        context.start()
        self.addCleanup(context.stop)
        context = patch('wyze_sdk.Client')
        self.Client = context.start()
        self.addCleanup(context.stop)
        self.client = self.Client.return_value
        self.client.plugs.list.return_value = [SimpleNamespace(mac='aabbccddeeff', product=SimpleNamespace(model='WLPP1'))]
        self.power = WyzePower()

    def test_sdk_session_is_reused_and_mac_resolves_model(self):
        self.power.set_power('AABBCCDDEEFF', False)
        self.power.set_power('AABBCCDDEEFF', True)
        self.Client.assert_called_once_with(token='secret', refresh_token='refresh')
        self.client.plugs.list.assert_called_once()
        self.client.plugs.turn_off.assert_called_once_with(device_mac='aabbccddeeff', device_model='WLPP1')
        self.client.plugs.turn_on.assert_called_once_with(device_mac='aabbccddeeff', device_model='WLPP1')

    def test_login_and_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PowerError, 'Configure WYZE_EMAIL'):
                self.power.set_power('AABBCCDDEEFF', False)
        self.power = WyzePower()
        with patch.dict(os.environ, dict(WYZE_EMAIL='email', WYZE_PASSWORD='pw', WYZE_KEY_ID='id', WYZE_API_KEY='key', WYZE_TOTP_KEY='totp'), clear=True):
            self.power.set_power('AABBCCDDEEFF', True)
        self.client.login.assert_called_once_with(email='email', password='pw', key_id='id', api_key='key', totp_key='totp')

    def test_expired_token_refresh_and_retry(self):
        from wyze_sdk.errors import WyzeApiError
        self.client.plugs.turn_on.side_effect = [WyzeApiError('expired', {'code': 2001}), None]
        self.power.set_power('AABBCCDDEEFF', True)
        self.client.refresh_token.assert_called_once()
        self.assertEqual(self.client.plugs.turn_on.call_count, 2)

    def test_errors_are_sanitized_and_requests_back_off(self):
        self.client.plugs.turn_off.side_effect = RuntimeError('secret-token-password')
        with self.assertRaises(PowerError) as caught:
            self.power.set_power('AABBCCDDEEFF', False)
        self.assertNotIn('secret', str(caught.exception))
        with self.assertRaisesRegex(PowerError, 'temporarily unavailable'):
            self.power.set_power('AABBCCDDEEFF', False)
        self.client.plugs.turn_off.assert_called_once()

    def test_sdk_transport_timeout_and_noninteractive_mfa(self):
        import requests
        from wyze_power import configure_sdk
        from wyze_sdk.service import auth_service
        from wyze_sdk.service.base import BaseServiceClient
        configure_sdk()
        session = Mock()
        session.merge_environment_settings.return_value = {}
        session.send.side_effect = requests.exceptions.Timeout('timed out')
        request = requests.Request('POST', 'https://example.invalid').prepare()
        service = BaseServiceClient()
        with self.assertRaises(requests.exceptions.Timeout):
            service._do_request(session, request)
        session.send.assert_called_once_with(request, timeout=(10, 30))
        with self.assertRaisesRegex(PowerError, 'WYZE_TOTP_KEY'):
            auth_service.input('MFA code: ')

    def test_unknown_device_never_switches(self):
        with self.assertRaisesRegex(PowerError, 'not found'):
            self.power.set_power('112233445566', False)
        self.client.plugs.turn_off.assert_not_called()


class PowerWebTests(unittest.TestCase):
    setUpClass = classmethod(test_webui.WebTests.setUpClass.__func__)
    setUp = test_webui.WebTests.setUp
    login = test_webui.WebTests.login
    token = test_webui.WebTests.token
    reboot = test_webui.WebTests.reboot

    def test_hard_reboot_mode_validation(self):
        self.login()
        self.assertEqual(self.reboot(mode='invalid').status_code, 400)
        self.frame.request_reboot.assert_not_called()
        self.assertEqual(self.reboot(mode='hard').status_code, 303)
        self.frame.request_reboot.assert_called_once_with('a' * 32, 'hard')

    def test_power_requires_auth_csrf_and_post(self):
        for action in ('power_on', 'power_off'):
            self.assertEqual(self.client.post('/frames/living-room/' + action).status_code, 302)
        self.login()
        for action in ('power_on', 'power_off'):
            url = '/frames/living-room/' + action
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(self.client.post(url, data={'request_id': 'a' * 32}).status_code, 400)
            self.assertEqual(self.client.post(url, data={'csrf': self.token(), 'request_id': 'bad'}).status_code, 400)
            self.assertEqual(self.client.post(url, data={'csrf': self.token(), 'request_id': 'a' * 32}).status_code, 303)
            self.frame.request_action.assert_called_with(action, 'a' * 32)

    def test_page_shows_power_controls_and_reboot_dropdown(self):
        self.login()
        self.frame.snapshot.return_value.update(wyze_mac='AABBCCDDEEFF', power='off', power_suspended=True, schedule_paused=True)
        response = self.client.get('/')
        self.assertIn(b'name="mode"', response.data)
        self.assertIn(b'Hard (plug, 30s off)', response.data)
        self.assertIn(b'Power on', response.data)
        self.assertIn(b'Power off', response.data)
        self.assertIn(b'Monitoring and scheduling are paused', response.data)


if __name__ == '__main__':
    unittest.main()
