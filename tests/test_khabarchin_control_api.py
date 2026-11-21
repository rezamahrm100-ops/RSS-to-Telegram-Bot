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

probe_spec = importlib.util.spec_from_file_location(
    'status_probe', BASE / 'ops/khabarchin_status_probe.py')
probe = importlib.util.module_from_spec(probe_spec)
probe_spec.loader.exec_module(probe)

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = Path(self.folder.name)
        self.db = self.config / 'db.sqlite3'
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE feed(id INTEGER PRIMARY KEY, title TEXT, link TEXT, state INTEGER, error_count INTEGER)')
        con.execute('CREATE TABLE sub(id INTEGER PRIMARY KEY, user_id INTEGER, feed_id INTEGER, state INTEGER, title TEXT, tags TEXT, interval INTEGER, notify INTEGER, send_mode INTEGER, length_limit INTEGER, link_preview INTEGER, display_author INTEGER, display_via INTEGER, display_title INTEGER, display_entry_tags INTEGER, style INTEGER, display_media INTEGER)')
        con.execute('INSERT INTO feed VALUES(1, "Scientific feed", "https://example.org/feed", 1, 0)')
        con.execute('INSERT INTO sub(id,user_id,feed_id,state,title) VALUES(1, -10001, 1, 1, "Research")')
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

    def test_status_probe_ignores_http_proxy(self):
        import contextlib
        import io
        import os
        import sys
        from unittest.mock import patch

        token_file = self.config / 'owner.token'
        token_file.write_bytes(self.token)
        token_file.chmod(0o600)
        out = io.StringIO()
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://192.0.2.1:3128',
                                     'http_proxy': 'http://192.0.2.1:3128',
                                     'NO_PROXY': '', 'no_proxy': ''}):
            with patch.object(sys, 'argv', ['probe', '--token-file', str(token_file),
                                            '--port', str(self.server.server_port)]):
                with contextlib.redirect_stdout(out):
                    probe.main()
        self.assertIn('KHABARCHIN_API_STATUS=PASS_READ_ONLY', out.getvalue())

    def test_missing_optional_rssplus_config_keeps_rsstt_status_readable(self):
        self.config_file.unlink()
        (self.config / 'rssplus.sqlite3').unlink()
        code, data, _ = self.request('GET', '/v1/status')
        self.assertEqual(code, 200)
        self.assertEqual((data['feeds'], data['subscriptions']), (1, 1))
        self.assertEqual(data['rssplus_configured'], False)
        self.assertEqual(data['rssplus_enabled'], False)
        code, rules, etag = self.request('GET', '/v1/rules')
        self.assertEqual(code, 200)
        self.assertEqual(rules, {'enabled': False, 'configured': False,
                                 'default': {}, 'targets': {}})
        self.assertIsNone(etag)
        code, reviews, _ = self.request('GET', '/v1/reviews')
        self.assertEqual((code, reviews), (200, {'items': [], 'available': False}))
        self.server.allow_rules_write = True
        self.assertEqual(self.request('PUT', '/v1/rules/-10001', {'mode': 'auto'},
                                      {'If-Match': '"' + '0'*64 + '"'})[0], 503)
        self.assertFalse(self.config_file.exists())

    def test_dangling_rssplus_config_symlink_is_not_treated_as_absence(self):
        self.config_file.unlink()
        self.config_file.symlink_to(self.config / 'does-not-exist.json')
        self.assertEqual(self.request('GET', '/v1/status')[0], 503)
        self.assertEqual(self.request('GET', '/v1/rules')[0], 503)

    def test_missing_db_no_false_positive(self):
        self.db.unlink()
        self.assertEqual(self.request('GET', '/v1/status')[0], 503)

    def test_native_bridge_source_syntax(self):
        import ast
        source = (BASE / "src/khabarchin_ipc.py").read_text()
        ast.parse(source)
        ast.parse((BASE / "src/entrypoint.py").read_text())

    def test_subscription_write_is_off_by_default(self):
        code, payload, _ = self.request('POST', '/v1/commands', {
            'action': 'subscribe', 'channel_id': -10001,
            'url': 'https://example.org/feed'})
        self.assertEqual((code, payload['error']), (403, 'subscription commands disabled'))

    def test_native_write_rejects_unapproved_channel_and_actions(self):
        self.server.allow_subscription_write = True
        self.server.allowed_channels = frozenset({-10001})
        code, _, _ = self.request('POST', '/v1/commands', {
            'action': 'subscribe', 'channel_id': -19999, 'url': 'https://example.org/feed'})
        self.assertEqual(code, 403)
        code, _, _ = self.request('POST', '/v1/commands', {
            'action': 'shell', 'channel_id': -10001, 'command': 'id'})
        self.assertEqual(code, 400)
        code, _, _ = self.request('POST', '/v1/commands', {
            'action': [], 'channel_id': -10001, 'subscription_id': 1})
        self.assertEqual(code, 400)
        code, _, _ = self.request('POST', '/v1/commands', {
            'action': 'unsubscribe', 'channel_id': -10001, 'subscription_id': True})
        self.assertEqual(code, 400)
        code, _, _ = self.request('POST', '/v1/commands', {
            'action': 'unsubscribe', 'channel_id': -10001, 'subscription_id': 1,
            'additional': 'forbidden'})
        self.assertEqual(code, 400)

    def test_native_socket_missing_fails_closed(self):
        self.server.allow_subscription_write = True
        self.server.allowed_channels = frozenset({-10001})
        code, data, _ = self.request('POST', '/v1/commands', {
            'action': 'activate', 'channel_id': -10001, 'subscription_id': 1})
        self.assertEqual(code, 503)
        self.assertEqual(data['error'], 'native bot control unavailable')
        self.assertEqual(self.request('GET', '/v1/status')[1]['subscriptions'], 1)

    def test_native_socket_real_local_round_trip_without_sql_mutation(self):
        import os
        import socketserver
        import time

        class Stub(socketserver.BaseRequestHandler):
            def handle(self):
                wire = b''
                while b'\r\n\r\n' not in wire:
                    wire += self.request.recv(4096)
                head, body = wire.split(b'\r\n\r\n', 1)
                length = 0
                for line in head.split(b'\r\n'):
                    if line.lower().startswith(b'content-length:'):
                        length = int(line.partition(b':')[2].strip())
                while len(body) < length:
                    body += self.request.recv(4096)
                parsed = json.loads(body[:length])
                assert parsed == {'action': 'activate', 'channel_id': -10001,
                                  'subscription_id': 1}
                answer = json.dumps({'success': True, 'subscription_id': 1}).encode()
                self.request.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                                     + b'Content-Length: ' + str(len(answer)).encode()
                                     + b'\r\nConnection: close\r\n\r\n' + answer)

        ipc = self.config / '.khabarchin-ipc.sock'
        backend = socketserver.UnixStreamServer(str(ipc), Stub)
        self.addCleanup(backend.server_close)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(backend.shutdown)
        self.addCleanup(thread.join, 2)
        self.server.allow_subscription_write = True
        self.server.allowed_channels = frozenset({-10001})
        code, data, _ = self.request('POST', '/v1/commands', {
            'action': 'activate', 'channel_id': -10001, 'subscription_id': 1})
        self.assertEqual(code, 200)
        self.assertTrue(data['success'])
        self.assertEqual(self.request('GET', '/v1/status')[1]['subscriptions'], 1)
        audit = (self.config / '.khabarchin-api-audit.jsonl').read_text()
        self.assertIn('subscriptions.activate', audit)


    def test_subscription_settings_scoped_read(self):
        code, _, _ = self.request('GET', '/v1/subscriptions/1/settings?channel_id=-10001')
        self.assertEqual(code, 403)
        self.server.allowed_channels = frozenset({-10001})
        code, data, _ = self.request('GET', '/v1/subscriptions/1/settings?channel_id=-10001')
        self.assertEqual(code, 200)
        self.assertEqual(data['settings']['title'], 'Research')
        self.assertEqual(data['settings']['channel_id'], -10001)
        self.assertNotIn('feed_link', data['settings'])
        self.assertEqual(self.request('GET', '/v1/subscriptions/1/settings?channel_id=-30001')[0], 403)
        self.assertEqual(self.request('GET', '/v1/subscriptions/999/settings?channel_id=-10001')[0], 404)
        self.assertEqual(self.request('GET', '/v1/subscriptions/1/settings')[0], 400)

    def test_settings_strict_input_validation_and_absent_native(self):
        self.server.allow_subscription_write = True
        self.server.allowed_channels = frozenset({-10001})
        base = {'channel_id': -10001, 'subscription_id': 1}
        invalid = [
            {'action': 'set_interval', 'value': True},
            {'action': 'set_interval', 'value': -3},
            {'action': 'set_length_limit', 'value': 4097},
            {'action': 'set_title', 'value': 'hello\nworld'},
            {'action': 'set_hashtags', 'value': 'X' * 256},
            {'action': 'set_option', 'field': 'token', 'value': 1},
            {'action': 'set_option', 'field': 'display_media', 'value': 9000},
            {'action': 'set_option', 'field': 'display_media', 'value': True},
            {'action': 'set_title', 'value': 'Fine', 'extra': 4},
        ]
        for item in invalid:
            with self.subTest(item=item):
                code, _, _ = self.request('POST', '/v1/commands', {**base, **item})
                self.assertEqual(code, 400)
        good = {'action': 'set_interval', **base, 'value': 15}
        self.assertEqual(self.request('POST', '/v1/commands', good)[0], 503)
        self.assertEqual(self.request('GET', '/v1/status')[1]['subscriptions'], 1)

    def test_settings_ipc_receipts_fail_closed_on_mismatch(self):
        import socketserver

        class Stub(socketserver.BaseRequestHandler):
            def handle(self):
                wire = b''
                while b'\r\n\r\n' not in wire:
                    wire += self.request.recv(4096)
                head, body = wire.split(b'\r\n\r\n', 1)
                length = int(next(
                    row.partition(b':')[2].strip()
                    for row in head.split(b'\r\n') if row.lower().startswith(b'content-length:')
                ))
                while len(body) < length:
                    body += self.request.recv(4096)
                request = json.loads(body[:length])
                answer = {'success': True, 'subscription_id': request['subscription_id'],
                          'field': 'interval' if request['value'] == 15 else 'title',
                          'value': request['value']}
                data = json.dumps(answer).encode()
                self.request.sendall(
                    b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                    + b'Content-Length: ' + str(len(data)).encode()
                    + b'\r\nConnection: close\r\n\r\n' + data)

        ipc = self.config / '.khabarchin-ipc.sock'
        backend = socketserver.UnixStreamServer(str(ipc), Stub)
        self.addCleanup(backend.server_close)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(backend.shutdown)
        self.addCleanup(thread.join, 2)
        self.server.allow_subscription_write = True
        self.server.allowed_channels = frozenset({-10001})
        base = {'action': 'set_interval', 'channel_id': -10001,
                'subscription_id': 1}
        code, data, _ = self.request('POST', '/v1/commands', {**base, 'value': 15})
        self.assertEqual(code, 200)
        self.assertEqual((data['field'], data['value']), ('interval', 15))
        code, data, _ = self.request('POST', '/v1/commands', {**base, 'value': 30})
        self.assertEqual(code, 502)
        self.assertEqual(data['error'], 'native settings response mismatch')

if __name__ == '__main__':
    unittest.main()
