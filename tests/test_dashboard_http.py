import http.client
import http.cookiejar
import urllib.request
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub.daemon import make_server
from agent_hub.service import Service
from test_service import Fake


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(self.root / 'data', {'fake': Fake()}, per_agent_concurrency={'fake': 0})
        self.server = make_server(self.service, 'native-test-secret')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.tmp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = dict(headers or {})
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            headers.setdefault('Content-Type', 'application/json')
        try:
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

    def ticket(self):
        status, headers, body = self.request('/dashboard/ticket', 'POST', {},
                                            {'Authorization': 'Bearer native-test-secret'})
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result['url'], self.origin + '/dashboard/#ticket=' + result['ticket'])
        self.assertNotIn('native-test-secret', str(result))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return result['ticket']

    def session(self):
        status, headers, body = self.request('/dashboard/session', 'POST', {'ticket': self.ticket()},
                                            {'Origin': self.origin, 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual(status, 200, body)
        cookie = headers['Set-Cookie']
        for flag in ['HttpOnly', 'Path=/dashboard', 'SameSite=Strict', 'Max-Age=28800']:
            self.assertIn(flag, cookie)
        return cookie.split(';')[0]

    def test_ticket_is_native_only_and_cookie_cannot_authorize_rpc(self):
        self.assertEqual(self.request('/dashboard/ticket', 'POST', {})[0], 401)
        self.assertEqual(self.request('/dashboard/ticket', 'POST', {},
            {'Authorization': 'Bearer native-test-secret', 'Origin': self.origin})[0], 403)
        cookie = self.session()
        self.assertEqual(self.request('/dashboard/api/v1/tasks', headers={'Cookie': cookie})[0], 200)
        for path in ['/rpc', '/shutdown', '/work/example/session', '/dashboard/ticket']:
            self.assertEqual(self.request(path, 'POST', {}, {'Cookie': cookie})[0], 401, path)
        self.assertEqual(self.request('/work/example', headers={'Cookie': cookie})[0], 401)
        self.assertEqual(self.request('/health', headers={'Authorization': 'Bearer native-test-secret'})[0], 200)

    def test_two_daemons_keep_sessions_in_shared_browser_cookie_jar(self):
        service = Service(self.root / 'other-data', {'fake': Fake()}, per_agent_concurrency={'fake': 0})
        server = make_server(service, 'other-native-secret')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origins = [self.origin, 'http://127.0.0.1:' + str(server.server_port)]
            jar = http.cookiejar.CookieJar()
            browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            for origin, secret in zip(origins, ['native-test-secret', 'other-native-secret']):
                request = urllib.request.Request(origin + '/dashboard/ticket', data=b'{}',
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret})
                with browser.open(request, timeout=3) as response:
                    ticket = json.load(response)['ticket']
                request = urllib.request.Request(origin + '/dashboard/session',
                    data=json.dumps({'ticket': ticket}).encode(),
                    headers={'Content-Type': 'application/json', 'Origin': origin})
                with browser.open(request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
            # CookieJar, like a browser, shares cookies between ports on one host.
            for origin in origins:
                with browser.open(origin + '/dashboard/api/v1/tasks', timeout=3) as response:
                    self.assertEqual(response.status, 200)
            self.assertEqual(len(jar), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            service.close()

    def test_ticket_exchange_validates_origin_and_is_one_use(self):
        ticket = self.ticket()
        for headers in [{}, {'Origin': 'https://evil.example'}, {'Origin': self.origin, 'Sec-Fetch-Site': 'cross-site'},
                        {'Origin': self.origin, 'Referer': 'https://evil.example/'}]:
            self.assertEqual(self.request('/dashboard/session', 'POST', {'ticket': ticket}, headers)[0], 403)
        self.assertEqual(self.request('/dashboard/session', 'POST', {'ticket': ticket}, {'Origin': self.origin})[0], 200)
        self.assertEqual(self.request('/dashboard/session', 'POST', {'ticket': ticket}, {'Origin': self.origin})[0], 401)

    def test_ticket_and_session_expire_on_server(self):
        with patch('agent_hub.dashboard_http.time.monotonic', return_value=100):
            ticket = self.ticket()
            cookie = self.session()
        with patch('agent_hub.dashboard_http.time.monotonic', return_value=161):
            self.assertEqual(self.request('/dashboard/session', 'POST', {'ticket': ticket}, {'Origin': self.origin})[0], 401)
        with patch('agent_hub.dashboard_http.time.monotonic', return_value=28901):
            self.assertEqual(self.request('/dashboard/api/v1/tasks', headers={'Cookie': cookie})[0], 401)

    def test_data_rejects_other_credentials_cross_site_and_rebinding(self):
        cookie = self.session()
        for headers in [{}, {'Authorization': 'Bearer native-test-secret'}, {'Cookie': 'hub_work=example'}]:
            self.assertEqual(self.request('/dashboard/api/v1/tasks', headers=headers)[0], 401)
        for extra in [{'Origin': 'https://evil.example'}, {'Referer': 'http://evil.example/'},
                      {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}, {'Host': 'evil.example'},
                      {'Host': '127.0.0.1:1'}]:
            self.assertEqual(self.request('/dashboard/api/v1/tasks', headers={'Cookie': cookie, **extra})[0], 403)

    def test_static_mime_security_headers_and_traversal(self):
        for path, mime in [('/dashboard/', 'text/html'), ('/dashboard/app.mjs', 'text/javascript'),
                           ('/dashboard/style.css', 'text/css')]:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200, body)
            self.assertTrue(headers['Content-Type'].startswith(mime))
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
            self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
        for path in ['/dashboard/../daemon.py', '/dashboard/%2e%2e/daemon.py', '/dashboard/%2fdaemon.py',
                     '/dashboard/nope.js', '/dashboard/index.html/extra', '/dashboard/api/v1/tasks/../agents']:
            self.assertEqual(self.request(path, headers={'Cookie': self.session()})[0], 404, path)

    def test_queries_and_json_body_are_bounded(self):
        cookie = self.session()
        for query in ['limit=0', 'limit=101', 'offset=-1', 'limit=bad', 'limit=1&limit=2', 'unknown=1', 'q=' + 'x' * 501]:
            self.assertEqual(self.request('/dashboard/api/v1/tasks?' + query, headers={'Cookie': cookie})[0], 400, query)
        for body, extra in [(b'not-json', {'Content-Type': 'application/json'}), (b'{}', {'Content-Type': 'text/plain'}),
                            (b'[]', {'Content-Type': 'application/json'}), (b'x' * 4097, {'Content-Type': 'application/json'})]:
            self.assertEqual(self.request('/dashboard/session', 'POST', body, {'Origin': self.origin, **extra})[0], 400)

    def test_data_routes_read_current_service_without_controller_mutation(self):
        task = self.service.call('submit_task', dict(request_id='http-dashboard', agent='fake',
            prompt='HTTP dashboard test', cwd=str(self.root), mode='read_only'))
        cookie = self.session()
        headers = {'Cookie': cookie, 'Origin': self.origin}
        status, _, body = self.request('/dashboard/api/v1/tasks?q=HTTP&agent=fake', headers=headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['items'][0]['id'], task['id'])
        for path, field in [('/dashboard/api/v1/agents', 'items'),
                            ('/dashboard/api/v1/tasks/' + task['id'], 'task'),
                            ('/dashboard/api/v1/tasks/' + task['id'] + '/events?after_seq=0', 'events')]:
            status, _, body = self.request(path, headers=headers)
            self.assertEqual(status, 200, body)
            self.assertIn(field, json.loads(body))
        self.assertEqual(self.request('/dashboard/api/v1/tasks/missing', headers=headers)[0], 404)
        after = self.service.store.get(task['id'])
        self.assertEqual(after.get('controller_epoch'), task.get('controller_epoch'))

    def test_missing_assets_reports_actionable_code(self):
        with patch('agent_hub.dashboard_http.STATIC_ROOT', self.root / 'missing-static'):
            status, _, body = self.request('/dashboard/ticket', 'POST', {}, {'Authorization': 'Bearer native-test-secret'})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)['error']['code'], 'DASHBOARD_ASSETS_MISSING')
            self.assertNotIn(str(self.root), body.decode())


class DashboardCLITests(unittest.TestCase):
    def test_command_opens_ticket_url_without_printing_ticket_or_management_token(self):
        from agent_hub.__main__ import main
        result = {'ticket': 'launch-secret', 'url': 'http://127.0.0.1:1234/dashboard/#ticket=launch-secret'}
        output = io.StringIO()
        with patch('sys.argv', ['agent-hub', 'dashboard']), patch('agent_hub.client.Client.ensure'), \
             patch('agent_hub.client.Client.request', return_value=result), \
             patch('webbrowser.open', return_value=True) as browser, patch('sys.stdout', output):
            main()
        self.assertEqual(browser.call_args.args[0], result['url'])
        self.assertNotIn('launch-secret', output.getvalue())
        self.assertTrue(json.loads(output.getvalue())['opened'])

    def test_browser_failure_is_reported_without_exposing_url(self):
        from agent_hub.__main__ import main
        output = io.StringIO()
        with patch('sys.argv', ['agent-hub', 'dashboard']), patch('agent_hub.client.Client.ensure'), \
             patch('agent_hub.client.Client.request', return_value={'url': 'http://127.0.0.1:1234/dashboard/#ticket=secret'}), \
             patch('webbrowser.open', return_value=False), patch('sys.stdout', output), self.assertRaises(SystemExit):
            main()
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'BROWSER_UNAVAILABLE')
        self.assertNotIn('#ticket=', output.getvalue())


if __name__ == '__main__':
    unittest.main()
