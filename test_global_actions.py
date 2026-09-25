"""Global controls reuse durable per-frame requests and tolerate partial failures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import controller as c
from test_configuration import config
from webui import create_app


class GlobalActionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.config = config()
        self.config['frames'].append({**self.config['frames'][0], 'name': 'bedroom',
                                      'address': '192.0.2.2:5555'})
        self.registry = c.FrameRegistry(self.config, self.data)
        (self.data / 'web-auth.json').write_text(json.dumps(dict(id='test')))
        self.app = create_app(self.config, self.registry, self.data)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session.update(account_id='test', csrf='csrf-token')

    def post(self, action, **changes):
        data = dict(csrf='csrf-token', request_id='a' * 32)
        data.update(changes)
        return self.client.post('/frames/actions/' + action, data=data, follow_redirects=True)

    def test_auth_csrf_method_and_token(self):
        for action in ('wake', 'sleep'):
            self.assertEqual(self.client.post('/frames/actions/' + action).status_code, 302)
        self.login()
        for action in ('wake', 'sleep'):
            self.assertEqual(self.client.get('/frames/actions/' + action).status_code, 405)
            self.assertEqual(self.post(action, csrf='wrong').status_code, 400)
            self.assertEqual(self.post(action, request_id='bad').status_code, 400)
        self.assertTrue(all(not frame.state for frame in self.registry.frames.values()))

    def test_both_actions_persist_for_every_frame_without_sending_adb(self):
        self.login()
        for action, mode in (('wake', 'day'), ('sleep', 'night')):
            for frame in self.registry.frames.values():
                frame.state = {}
            with patch.object(c.ADB, 'connect') as connect:
                response = self.post(action)
                connect.assert_not_called()
            self.assertIn(b'requested for 2 frame(s)', response.data)
            restored = c.FrameRegistry(self.config, self.data)
            for frame in restored.frames.values():
                self.assertEqual(frame.state['manual']['action'], action)
                self.assertEqual(frame.state['manual']['phase'], 'queued')
                self.assertEqual(frame.state['override']['mode'], mode)
            previous = [deepcopy(frame.state) for frame in self.registry.frames.values()]
            self.post(action)
            self.assertEqual(previous, [frame.state for frame in self.registry.frames.values()])

    def test_global_buttons_use_each_frames_action_settings(self):
        self.login()
        http_frame = self.registry.frames['living-room']
        adb_frame = self.registry.frames['bedroom']
        http_frame.cfg.update(morning_action='undim', night_action='dim')
        for morning_action in ('reboot', 'restart_app'):
            adb_frame.cfg['morning_action'] = morning_action
            for action, command in (('wake', 'undim'), ('sleep', 'dim')):
                with self.subTest(morning_action=morning_action, action=action):
                    for frame in (http_frame, adb_frame):
                        frame.state = {}
                        frame.adb = Mock()
                        frame.adb.shell.return_value = 'Status: ok'
                    with patch.object(c, 'urlopen') as request:
                        request.return_value.__enter__.return_value.status = 200
                        response = self.post(action)
                        self.assertIn(b'requested for 2 frame(s)', response.data)
                        for frame in (http_frame, adb_frame):
                            frame.tick()
                            self.assertEqual(frame.state['manual']['phase'], 'completed')
                        request.assert_called_once_with(
                            'http://192.0.2.1:53287/' + command, timeout=20)
                    self.assertEqual(http_frame.adb.mock_calls, [])
                    adb_frame.adb.connect.assert_called_once()
                    adb_frame.adb.run.assert_not_called()
                    if action == 'wake':
                        adb_frame.adb.shell.assert_any_call(
                            'am', 'start', '-W', '-n', adb_frame.cfg['component'])
                    else:
                        adb_frame.adb.shell.assert_any_call(
                            'am', 'force-stop', adb_frame.cfg['package'])
                        adb_frame.adb.shell.assert_any_call(
                            'settings', 'put', 'system', 'screen_brightness', '0')

    def test_global_actions_skip_disabled_frames(self):
        frame = self.registry.frames['living-room']
        frame.cfg['enabled'] = False
        for action in ('wake', 'sleep'):
            self.registry.frames['bedroom'].state = {}
            accepted, errors = self.registry.request_all(action, 'a' * 32)
            self.assertEqual(accepted, ['bedroom'])
            self.assertEqual(errors, {})
            self.assertFalse(frame.state)

    def test_busy_frame_does_not_block_other_frames(self):
        self.login()
        frame = self.registry.frames['living-room']
        frame.mutex.acquire()
        try:
            response = self.post('wake')
        finally:
            frame.mutex.release()
        self.assertIn(b'living-room: Frame is busy', response.data)
        self.assertIn(b'requested for 1 frame(s): bedroom', response.data)
        self.assertFalse(frame.state)
        self.assertEqual(self.registry.frames['bedroom'].state['manual']['action'], 'wake')

    def test_storage_failure_does_not_block_other_frames(self):
        self.login()
        frame = self.registry.frames['living-room']
        with patch.object(frame, 'save', side_effect=OSError('disk full')):
            with self.assertLogs(c.LOG, level='ERROR'):
                response = self.post('sleep')
        self.assertIn(b'living-room: Could not save the request', response.data)
        self.assertIn(b'requested for 1 frame(s): bedroom', response.data)
        self.assertFalse(frame.state)

    def test_empty_registry_and_button_markup(self):
        self.login()
        page = self.client.get('/')
        self.assertIn(b'Wake all frames', page.data)
        self.assertIn(b'Sleep all frames', page.data)
        self.registry.frames.clear()
        self.assertIn(b'No frames configured', self.post('wake').data)
        self.assertIn(b'disabled>Wake all frames', self.client.get('/').data)


if __name__ == '__main__':
    unittest.main()
