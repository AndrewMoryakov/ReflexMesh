from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from reflexmesh.contracts.task import Task, Route, ValidationError
from reflexmesh.routing.jev_router import route_task, MAX_RESPONSE_BYTES


def task():
    return Task('0.1', 't', 'Generate text', (Route.CUA, Route.LLM), (Route.LLM,))


def response():
    return {'request_id': 'req_test', 'decision_id': 'dec_test', 'mode': 'decision_only',
            'status': 'selected', 'execution': {'enabled': False, 'status': 'not_started'},
            'provenance': {'jev_provider': 'typesafe'},
            'decision': {'kind': 'choice', 'selected': 'LLM', 'jev_choice': 'LLM',
                         'candidates': [{'id': 'LLM', 'jev_probability': 1.0, 'jev_confidence': 0.9,
                                         'router': {'available': True, 'allowed': True, 'filtered': False,
                                                    'requires_confirmation': False}}]},
            'fallback': {'type': None, 'reason': None},
            'raw_jev': {'answers': {'tool': {'confidence': 0.9}}}}


class Server:
    def __init__(self, body=None, status=200, delay=0):
        self.body = json.dumps(response()).encode() if body is None else body
        self.status, self.delay, self.requests = status, delay, []

    def __enter__(self):
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                outer.requests.append((self.path, json.loads(raw)))
                time.sleep(outer.delay)
                self.send_response(outer.status)
                self.end_headers()
                try:
                    self.wfile.write(outer.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class JevTests(unittest.TestCase):
    def test_http_filters_before_request_and_preserves_response(self):
        with Server() as server:
            result = route_task(task(), endpoint=server.url)
        self.assertEqual(result['status'], 'selected')
        self.assertEqual(result['route'], 'LLM')
        self.assertEqual(result['confidence'], 0.9)
        self.assertFalse(result['is_stub'])
        self.assertFalse(result['execution_performed'])
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0][0], '/route')
        self.assertEqual([c['id'] for c in server.requests[0][1]['candidates']], ['LLM'])
        self.assertEqual(len(result['trace']['response_sha256']), 64)

    def test_empty_set_never_calls_transport(self):
        with patch('reflexmesh.routing.jev_router._post') as post:
            result = route_task(Task('0.1', 'id', 'goal', (), ()))
            post.assert_not_called()
        self.assertEqual(result['status'], 'abstained')
        self.assertFalse(result['trace']['request_sent'])

    def test_confirmation_and_abstention(self):
        for status in ('needs_confirmation', 'no_decision'):
            value = response(); value['status'] = status
            if status == 'needs_confirmation':
                value['decision']['candidates'][0]['router']['requires_confirmation'] = True
            else:
                value['decision']['selected'] = None
                value['fallback']['type'] = 'low_confidence'
            with Server(json.dumps(value).encode()) as server:
                result = route_task(task(), endpoint=server.url)
            self.assertEqual(result['status'], status if status == 'needs_confirmation' else 'abstained')
            self.assertIsNone(result['route'])

    def test_reject_inconsistent_response(self):
        values = []
        for key, val in [('selected', 'CUA'), ('jev_choice', 'UNKNOWN')]:
            data = response(); data['decision'][key] = val; values.append(data)
        for flag, val in [('allowed', False), ('available', False), ('filtered', True),
                          ('requires_confirmation', True), ('available', 1)]:
            data = response(); data['decision']['candidates'][0]['router'][flag] = val; values.append(data)
        for val in [True, -0.1, 1.1, float('nan')]:
            data = response(); data['decision']['candidates'][0]['jev_probability'] = val; values.append(data)
        data = response(); data['decision']['candidates'] *= 2; values.append(data)
        data = response(); data['execution']['enabled'] = True; values.append(data)
        data = response(); data['status'] = 'completed'; values.append(data)
        for data in values:
            with self.subTest(data=data):
                with patch('reflexmesh.routing.jev_router._post', return_value=json.dumps(data).encode()):
                    result = route_task(task())
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['reason_code'], 'invalid_response')
                self.assertIsNone(result['route'])

    def test_demo_requires_explicit_opt_in(self):
        data = response(); data['provenance']['jev_provider'] = 'jevrouter-demo'
        with Server(json.dumps(data).encode()) as server:
            refused = route_task(task(), endpoint=server.url)
            accepted = route_task(task(), endpoint=server.url, allow_demo=True)
        self.assertEqual(refused['reason_code'], 'demo_not_allowed')
        self.assertTrue(refused['is_stub'])
        self.assertEqual(accepted['status'], 'selected')
        self.assertTrue(accepted['is_stub'])

    def test_missing_raw_confidence_is_not_synthesized(self):
        data = response(); data['raw_jev']['answers']['tool'] = {}
        with patch('reflexmesh.routing.jev_router._post', return_value=json.dumps(data).encode()):
            result = route_task(task())
        self.assertIsNone(result['confidence'])

    def test_transport_failures(self):
        for body, code, reason in [(b'not json', 200, 'invalid_response'),
                                    (b'\xff', 200, 'invalid_response'),
                                    (b'{"a":1,"a":2}', 200, 'invalid_response'),
                                    (b'secret-error', 500, 'http_error'),
                                    (b'redirect', 302, 'http_error'),
                                    (b' '* (MAX_RESPONSE_BYTES+1), 200, 'response_too_large')]:
            with self.subTest(reason=reason):
                with Server(body, code) as server:
                    result = route_task(task(), endpoint=server.url)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['reason_code'], reason)
                self.assertNotIn('secret-error', json.dumps(result))

    def test_timeout_and_unreachable(self):
        with Server(delay=0.15) as server:
            result = route_task(task(), endpoint=server.url, timeout=0.02)
        self.assertEqual(result['reason_code'], 'transport_timeout')
        result = route_task(task(), endpoint=server.url, timeout=0.1)
        self.assertEqual(result['reason_code'], 'transport_error')

    def test_provider_error_is_not_fallback(self):
        data = response(); data['error'] = {'code': 'jev_auth_error', 'message': 'secret-key'}
        with Server(json.dumps(data).encode()) as server:
            result = route_task(task(), endpoint=server.url)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason_code'], 'provider_error')
        self.assertNotIn('secret-key', json.dumps(result))

    def test_config(self):
        for url in ['https://127.0.0.1:8787', 'http://example.com', 'http://127.0.0.1:0',
                    'http://user:pass@127.0.0.1', 'http://127.0.0.1/other']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                route_task(task(), endpoint=url)
        for timeout in [0, -1, float('nan'), float('inf'), True, 121]:
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                route_task(task(), timeout=timeout)

    def test_cli(self):
        task_dict = {'schema_version': '0.1', 'task_id': 't', 'goal': 'text',
                     'capabilities': ['LLM'], 'allowed_routes': ['LLM']}
        for status, exit_code in [('selected', 0), ('needs_confirmation', 4), ('no_decision', 3), ('bad', 5)]:
            data = response(); data['status'] = status
            if status == 'needs_confirmation':
                data['decision']['candidates'][0]['router']['requires_confirmation'] = True
            elif status == 'no_decision':
                data['decision']['selected'] = None
            with Server(json.dumps(data).encode()) as server:
                result = subprocess.run([sys.executable, '-m', 'reflexmesh', 'route', '--provider', 'jevrouter',
                                         '--jev-url', server.url], input=json.dumps(task_dict).encode(), capture_output=True,
                                        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, timeout=5)
            self.assertEqual(result.returncode, exit_code, result.stderr)
            self.assertEqual(result.stderr, b'')
            self.assertFalse(json.loads(result.stdout)['execution_performed'])


if __name__ == '__main__':
    unittest.main()
