"""Offline synthetic-data tests; no Telegram credentials or VPS access."""
import importlib.util
import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('control_api', BASE / 'ops/khabarchin_control_api.py')
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = Path(self.folder.name)
        self.db = self.config / 'db.sqlite3'
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE feed(id INTEGER PRIMARY KEY, title TEXT, link TEXT, state INTEGER, error_count INTEGER)')
        con.execute('CREATE TABLE sub(id INTEGER PRIMARY KEY, user_id INTEGER, feed_id INTEGER, state INTEGER, title TEXT)')
        con.execute('INSERT INTO feed VALUES(1, "Scientific feed", "https://example.org/feed", 1, 0)')
        con.execute('INSERT INTO sub VALUES(1, -10001, 1, 1, "Research")')
        con.commit()
        con.close()
        con = sqlite3.connect(self.config / 'rssplus.sqlite3')
        con.execute('CREATE TABLE items(id TEXT, chat_id INTEGER, sub_id INTEGER, status TEXT, created_at TEXT, payload TEXT)')
        con.execute('INSERT INTO items VALUES("abc", -10001, 1, "pending", "2026-11-20", "PRIVATE_BODY")')
        con.commit()
        con.close()
        self.config_file = self.config / 'rssplus.json'
        self.config_file.write_text(json.dumps({'enabled': True, 'review_chat_id': 123,
            'webhook_token': 'SECRET_SHOULD_NOT_LEAK', 'targets': {},
            'default': {'mode': 'auto'}}))
        self.token = b'only_synthetic_api_token_abcdefghijklmnopqrstuvwxyz'
        self.allow_write = False
        self._start_server(False)

    def _start_server(self, write):
        self.server = api.Controller(('127.0.0.1', 0), self.config, self.db, self.token, write)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.thread.join, 2)

    def request(self, method, path, body=None, headers=None, auth=True):
        hdr = dict(headers or {})
        if auth:
            hdr['Authorization'] = 'Bearer ' + self.token.decode()
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        if isinstance(body, dict):
            body = json.dumps(body)
            hdr['Content-Type'] = 'application/json'
        client.request(method, path, body, headers=hdr)
        response = client.getresponse()
        payload = response.read().decode()
        result = (response.status, json.loads(payload), response.getheader('ETag'))
        client.close()
        return result

    def test_auth_required(self):
        status, body, _ = self.request('GET', '/v1/health', auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(body['error'], 'unauthorized')

    def test_health_does_not_assert_bot_alive(self):
        code, data, _ = self.request('GET', '/v1/health')
        self.assertEqual(code, 200)
        self.assertEqual(data['bot_running'], 'not_checked')

    def test_status_and_feed_listing(self):
        code, data, _ = self.request('GET', '/v1/status')
        self.assertEqual((code, data['feeds'], data['subscriptions']), (200, 1, 1))
        self.assertEqual(self.request('GET', '/v1/feeds')[1]['items'][0]['title'], 'Scientific feed')
        self.assertEqual(self.request('GET', '/v1/subscriptions')[1]['items'][0]['channel_id'], -10001)

    def test_config_redacts_webhook_token(self):
        code, data, etag = self.request('GET', '/v1/rules')
        self.assertEqual(code, 200)
        self.assertNotIn('SECRET_SHOULD_NOT_LEAK', json.dumps(data))
        self.assertTrue(etag.startswith('"'))

    def test_nested_unknown_fields_are_never_returned(self):
        original = json.loads(self.config_file.read_text())
        original['default']['future_secret'] = 'SECRET_NESTED'
        original['targets']['-10001'] = {'mode':'auto', 'internal_token':'SECRET_TARGET'}
        self.config_file.write_text(json.dumps(original))
        code, data, _ = self.request('GET', '/v1/rules')
        self.assertEqual(code, 200)
        self.assertNotIn('SECRET_NESTED', json.dumps(data))
        self.assertNotIn('SECRET_TARGET', json.dumps(data))

    def test_review_redacts_payload(self):
        _, data, _ = self.request('GET', '/v1/reviews')
        self.assertNotIn('PRIVATE_BODY', json.dumps(data))
        self.assertEqual(data['items'][0]['status'], 'pending')

    def test_write_disabled_by_default(self):
        etag = self.request('GET', '/v1/rules')[2]
        code, _, _ = self.request('PUT', '/v1/rules/-10001', {'mode': 'review'}, {'If-Match': etag})
        self.assertEqual(code, 403)
        self.assertEqual(json.loads(self.config_file.read_text())['targets'], {})

    def test_write_enabled_requires_etag(self):
        self.server.allow_rules_write = True
        code, _, _ = self.request('PUT', '/v1/rules/-10001', {'mode': 'review'})
        self.assertEqual(code, 428)

    def test_allowed_write_atomic_and_audited(self):
        self.server.allow_rules_write = True
        etag = self.request('GET', '/v1/rules')[2]
        code, data, next_etag = self.request('PUT', '/v1/rules/-10001',
                                             {'mode': 'review', 'include': ['science']}, {'If-Match': etag})
        self.assertEqual(code, 200)
        self.assertNotEqual(next_etag, etag)
        cfg = json.loads(self.config_file.read_text())
        self.assertEqual(cfg['targets']['-10001']['mode'], 'review')
        self.assertEqual(cfg['webhook_token'], 'SECRET_SHOULD_NOT_LEAK')
        self.assertIn('rules.update', (self.config / '.khabarchin-api-audit.jsonl').read_text())
        code, _, _ = self.request('PUT', '/v1/rules/-10001', {'mode': 'auto'}, {'If-Match': etag})
        self.assertEqual(code, 412)

    def test_reject_unapproved_fields(self):
        self.server.allow_rules_write = True
        etag = self.request('GET', '/v1/rules')[2]
        code, _, _ = self.request('PUT', '/v1/rules/-10001', {'shell': 'rm -rf /'}, {'If-Match': etag})
        self.assertEqual(code, 400)

    def test_limit_validation(self):
        self.assertEqual(self.request('GET', '/v1/feeds?limit=51')[0], 400)

    def test_host_rebinding_blocked(self):
        self.assertEqual(self.request('GET', '/v1/health', headers={'Host': 'attacker.example'})[0], 403)

    def test_missing_db_no_false_positive(self):
        self.db.unlink()
        self.assertEqual(self.request('GET', '/v1/status')[0], 503)

if __name__ == '__main__':
    unittest.main()
