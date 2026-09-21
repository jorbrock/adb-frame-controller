"""Persistent frame history and its authenticated UI."""
import json
from pathlib import Path
import tempfile
import unittest

import controller as c
import frame_log
from test_configuration import config
from webui import create_app


class FrameLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.config = config()
        self.registry = c.FrameRegistry(self.config, self.data)
        self.frame = self.registry.frames['living-room']
        (self.data / 'web-auth.json').write_text(json.dumps(dict(id='test')))
        self.app = create_app(self.config, self.registry, self.data)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session['account_id'] = 'test'

    def test_history_survives_restart_and_rename(self):
        self.frame.status('first')
        self.frame.status('error', error='connection failed')
        restored = c.FrameRegistry(self.config, self.data)
        self.assertEqual([r['result'] for r in restored.log_page('living-room', 1)[1]], ['error', 'first'])
        restored.change('living-room', {**self.frame.cfg, 'name': 'renamed'}, restored.revision)
        restored.frames['renamed'].status('recovered')
        records = restored.log_page('renamed', 1)[1]
        self.assertEqual([r['result'] for r in records], ['recovered', 'error', 'first'])
        self.assertTrue(all(r['updated_at'] for r in records))

    def test_legacy_status_is_visible_and_seeded_once(self):
        c.atomic_json(self.frame.status_path, dict(result='legacy'))
        self.assertEqual(self.registry.log_page('living-room', 1)[1][0]['result'], 'legacy')
        self.frame.status('first')
        self.frame.status('second')
        self.assertEqual([r['result'] for r in frame_log.entries(self.frame.log_path)], ['second', 'first', 'legacy'])

    def test_ui_auth_empty_unknown_pagination_and_escaping(self):
        path = '/frames/living-room/log'
        self.assertEqual(self.client.get(path).status_code, 302)
        self.login()
        self.assertIn(b'No activity recorded yet.', self.client.get(path).data)
        self.assertEqual(self.client.get('/frames/unknown/log').status_code, 404)
        for value in ('0', '-1', 'abc', '1000001'):
            self.assertEqual(self.client.get(path + '?page=' + value).status_code, 400)
        for number in range(52):
            self.frame.status(f'event_{number}', error='<script>bad()</script>', mode='day')
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Event 51', response.data)
        self.assertNotIn(b'Event 1<', response.data)
        self.assertIn(b'Older results', response.data)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertNotIn(b'<script>bad()', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        older = self.client.get(path + '?page=2')
        self.assertIn(b'Event 0', older.data)
        self.assertNotIn(b'Older results', older.data)
        self.assertIn(b'View full log', self.client.get('/').data)

    def test_large_unicode_records_and_partial_tail(self):
        self.frame.status('first', error='é' * 10000)
        self.frame.status('second')
        with self.frame.log_path.open('ab') as stream:
            stream.write(b'{"partial":')
        records = list(frame_log.entries(self.frame.log_path))
        self.assertEqual([r['result'] for r in records], ['second', 'first'])
        self.assertEqual(records[1]['error'], 'é' * 10000)
        self.frame.status('after_interruption')
        self.assertEqual([r['result'] for r in frame_log.entries(self.frame.log_path)],
                         ['after_interruption', 'second', 'first'])


if __name__ == '__main__':
    unittest.main()
