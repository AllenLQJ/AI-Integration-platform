import http.client
import http.cookiejar
import urllib.request
import io
import json
import re
import socket
import tempfile
import threading
import time
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

    def test_overview_needs_a_session_takes_no_query_and_revalidates(self):
        path = '/dashboard/api/v1/overview'
        self.assertEqual(self.request(path)[0], 401)
        cookie = self.session()
        self.assertEqual(self.request(path + '?days=30', headers={'Cookie': cookie})[0], 400)
        task = self.service.submit('overview-task', 'fake', 'hello', str(self.root), 'read_only')
        self.service.store.finish(task['id'], 'failed', {'error': 'boom'})
        status, headers, body = self.request(path, headers={'Cookie': cookie})
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result['totals']['executions'], 1)
        self.assertEqual(result['attention'][0]['task_id'], task['id'])
        self.assertNotIn(str(self.root), body.decode())
        etag = headers['ETag']
        status, _, body = self.request(path, headers={'Cookie': cookie, 'If-None-Match': etag})
        self.assertEqual((status, body), (304, b''))
        # The day window can move without a write, so the overview is never served from the
        # version-only fast path: it is recomputed, and only an identical result earns a 304.
        with patch('agent_hub.dashboard_chains.DashboardChains.overview', return_value={'moved': True}):
            status, _, body = self.request(path, headers={'Cookie': cookie, 'If-None-Match': etag})
        self.assertEqual((status, json.loads(body)), (200, {'moved': True}))

    def test_conditional_reads_skip_projection_and_notice_verification_only_writes(self):
        cookie = self.session()
        task = self.service.submit('etag-task', 'fake', 'hello', str(self.root), 'read_only')
        self.service.store.finish(task['id'], 'succeeded', {'answer': 'done'})
        self.service.verification.ensure(self.service.store.get(task['id']))
        path = '/dashboard/api/v1/tasks/' + task['id']
        status, headers, body = self.request(path, headers={'Cookie': cookie})
        self.assertEqual(status, 200, body)
        self.assertIn('ETag', headers)
        etag = headers['ETag']
        conditional = {'Cookie': cookie, 'If-None-Match': etag}
        with patch('agent_hub.dashboard.Dashboard.detail', side_effect=AssertionError('unchanged projection rebuilt')):
            status, headers, body = self.request(path, headers=conditional)
        self.assertEqual((status, body), (304, b''))
        self.assertEqual(headers['ETag'], etag)
        self.assertNotIn('Content-Length', headers)
        self.assertEqual(self.request(path, headers={'If-None-Match': etag})[0], 401)
        before = self.service.store.get(task['id'])['updated']
        with self.service.store.connection(write=True) as db:
            db.execute("UPDATE verifications SET state='passed' WHERE task_id=?", (task['id'],))
        status, headers, body = self.request(path, headers=conditional)
        self.assertEqual(status, 200, body)
        self.assertNotEqual(headers['ETag'], etag)
        self.assertEqual(json.loads(body)['task']['verification_state'], 'passed')
        self.assertEqual(self.service.store.get(task['id'])['updated'], before)

    def test_event_history_paging_keeps_auth_and_rejects_mixed_cursors(self):
        task = self.service.submit('history-http', 'fake', 'history', str(self.root), 'read_only')
        self.service.store.event(task['id'], 'progress', {'message': 'one'})
        self.service.store.event(task['id'], 'progress', {'message': 'two'})
        path = '/dashboard/api/v1/tasks/' + task['id'] + '/events'
        self.assertEqual(self.request(path + '?before_seq=9223372036854775807')[0], 401)
        headers = {'Cookie': self.session()}
        status, _, body = self.request(path + '?before_seq=9223372036854775807', headers=headers)
        self.assertEqual(status, 200, body)
        page = json.loads(body)
        self.assertEqual([e['kind'] for e in page['events']], ['state', 'progress', 'progress'])
        self.assertFalse(page['has_more'])
        for query in ('before_seq=0', 'before_seq=-1', 'before_seq=2&after_seq=0', 'before_seq=1&before_seq=2'):
            self.assertEqual(self.request(path + '?' + query, headers=headers)[0], 400)

    def test_conditional_result_reads_notice_file_loss_and_recovery(self):
        task = self.service.submit('file-http', 'fake', 'file', str(self.root), 'read_only')
        path = self.root / 'result.json'
        path.write_text('{"answer":"original"}', encoding='utf-8')
        self.service.store.finish(task['id'], 'succeeded', {'path': str(path)})
        headers = {'Cookie': self.session()}
        for route in ('tasks', 'chains'):
            url = '/dashboard/api/v1/' + route + '/' + task['id']
            status, response_headers, _ = self.request(url, headers=headers)
            self.assertEqual(status, 200)
            etag = response_headers.get('ETag')
            self.assertTrue(etag)
            path.unlink()
            status, response_headers, body = self.request(url, headers=dict(headers, **{'If-None-Match': etag}))
            self.assertEqual(status, 200, 'missing result must invalidate the cached conversation')
            self.assertIn('结果文件不可用', body.decode('utf-8'))
            missing_etag = response_headers['ETag']
            path.write_text('{"answer":"restored"}', encoding='utf-8')
            status, _, body = self.request(url, headers=dict(headers, **{'If-None-Match': missing_etag}))
            self.assertEqual(status, 200)
            self.assertIn('restored', body.decode('utf-8'))

    def test_chain_etag_notices_activity_and_remains_scoped_to_its_route(self):
        cookie = self.session()
        task = self.service.submit('etag-chain', 'fake', 'hello', str(self.root), 'read_only')
        path = '/dashboard/api/v1/chains/' + task['id']
        status, headers, body = self.request(path, headers={'Cookie': cookie})
        self.assertEqual(status, 200, body)
        self.assertIn('ETag', headers)
        etag = headers['ETag']
        with patch('agent_hub.dashboard_chains.DashboardChains.detail', side_effect=AssertionError('unchanged graph rebuilt')):
            self.assertEqual(self.request(path, headers={'Cookie': cookie, 'If-None-Match': etag})[0], 304)
        other = self.request('/dashboard/api/v1/chains/missing', headers={'Cookie': cookie, 'If-None-Match': etag})
        self.assertEqual(other[0], 404)
        self.service.store.event(task['id'], 'message', {'text': 'new public message', 'role': 'assistant'})
        status, headers, body = self.request(path, headers={'Cookie': cookie, 'If-None-Match': etag})
        self.assertEqual(status, 200, body)
        self.assertNotEqual(headers['ETag'], etag)
        self.assertIn('new public message', body.decode())

    def test_unrelated_writes_keep_chain_and_task_etags_valid(self):
        cookie = self.session()
        watched = self.service.submit('etag-watched', 'fake', 'watched', str(self.root), 'read_only')
        busy = self.service.submit('etag-busy', 'fake', 'busy', str(self.root), 'read_only')
        for route in ('chains', 'tasks'):
            with self.subTest(route=route):
                path = f'/dashboard/api/v1/{route}/' + watched['id']
                status, headers, body = self.request(path, headers={'Cookie': cookie})
                self.assertEqual(status, 200, body)
                conditional = {'Cookie': cookie, 'If-None-Match': headers['ETag']}
                # Another chain's progress bumps data_version but not this projection.
                self.service.store.event(busy['id'], 'progress', {'message': 'elsewhere'})
                status, again, body = self.request(path, headers=conditional)
                self.assertEqual((status, body), (304, b''))
                self.assertEqual(again['ETag'], headers['ETag'])
                # After revalidating at the new version the projection is skipped again.
                with patch('agent_hub.dashboard.Dashboard.detail', side_effect=AssertionError('rebuilt')), \
                        patch('agent_hub.dashboard_chains.DashboardChains.detail', side_effect=AssertionError('rebuilt')):
                    self.assertEqual(self.request(path, headers=conditional)[0], 304)
                self.service.store.event(watched['id'], 'message', {'text': route + ' update', 'role': 'assistant'})
                status, changed, body = self.request(path, headers=conditional)
                self.assertEqual(status, 200, body)
                self.assertNotEqual(changed['ETag'], headers['ETag'])

    def test_task_detail_folds_live_events_into_one_conditional_read(self):
        cookie = self.session()
        task = self.service.submit('live-detail', 'fake', 'live', str(self.root), 'read_only')
        self.service.store.event(task['id'], 'progress', {'message': 'one'})
        cursor = self.service.store.events(task['id'], 0, 200)['next_seq']
        path = '/dashboard/api/v1/tasks/' + task['id']
        status, headers, body = self.request(path + '?after_seq=' + str(cursor), headers={'Cookie': cookie})
        self.assertEqual(status, 200, body)
        live = json.loads(body)
        self.assertEqual(live['task']['id'], task['id'])
        self.assertEqual(live['live_events'], {'events': [], 'next_seq': cursor})
        conditional = {'Cookie': cookie, 'If-None-Match': headers['ETag']}
        self.assertEqual(self.request(path + '?after_seq=' + str(cursor), headers=conditional)[0], 304)
        self.service.store.event(task['id'], 'progress', {'message': 'two'})
        status, _, body = self.request(path + '?after_seq=' + str(cursor), headers=conditional)
        self.assertEqual(status, 200, body)
        self.assertEqual([e['data']['message'] for e in json.loads(body)['live_events']['events']], ['two'])
        # Without the cursor the detail keeps its previous shape.
        self.assertNotIn('live_events', json.loads(self.request(path, headers={'Cookie': cookie})[2]))
        for query in ('after_seq=-1', 'after_seq=x', 'after_seq=1&after_seq=2', 'before_seq=1'):
            self.assertEqual(self.request(path + '?' + query, headers={'Cookie': cookie})[0], 400, query)

    def test_workbench_renders_latest_page_and_pages_older_events(self):
        from hub_fixtures import with_work_item
        task = self.service.call('submit_task', with_work_item(self.service, dict(request_id='work-paging', agent='fake',
            prompt='paging', cwd=str(self.root), mode='read_only')))
        for index in range(250):
            self.service.store.event(task['id'], 'message', {'message': f'line {index}\nnext', 'role': 'assistant'})
        bearer = {'Authorization': 'Bearer native-test-secret'}
        status, headers, body = self.request('/work/' + task['id'], headers=bearer)
        self.assertEqual(status, 200, body)
        self.assertIn('width=device-width', headers.get('Content-Type', '') + body.decode())
        html = body.decode('utf-8')
        seqs = [int(value) for value in re.findall(r'<pre tabindex="0" data-seq="(\d+)"', html)]
        everything = self.service.store.events(task['id'], 0, 200)['events'] + self.service.store.events(task['id'], 200, 200)['events']
        newest = [event['seq'] for event in everything][-200:]
        self.assertEqual(seqs, newest)
        self.assertIn('data-more="true"', html)
        self.assertRegex(html, r'<button type="button" class="older">加载更早事件</button></p>')
        # The summary is an attribute; the element text stays the event's exact JSON.
        self.assertIn('data-summary="#' + str(newest[-1]) + ' 消息：line 249 next"', html)
        events = '/work/' + task['id'] + '/events'
        status, _, body = self.request(events + '?before_seq=' + str(newest[0]), headers=bearer)
        self.assertEqual(status, 200, body)
        older = json.loads(body)
        self.assertEqual([e['seq'] for e in older['events']], [e['seq'] for e in everything if e['seq'] < newest[0]][-200:])
        self.assertFalse(older['has_more'])
        for query in ('before_seq=0', 'before_seq=x', 'before_seq=5&after_seq=0'):
            self.assertEqual(self.request(events + '?' + query, headers=bearer)[0], 400, query)
        self.assertEqual(self.request(events + '?before_seq=5')[0], 401)
        # A short attempt hides the button.
        short = self.service.call('submit_task', with_work_item(self.service, dict(request_id='work-short', agent='fake',
            prompt='short', cwd=str(self.root), mode='read_only'), 'short'))
        html = self.request('/work/' + short['id'], headers=bearer)[2].decode('utf-8')
        self.assertIn('data-more="false"', html)
        self.assertIn('class="older" hidden', html)

    def test_every_local_module_import_is_served(self):
        from agent_hub.dashboard_http import STATIC_ROOT
        for source in STATIC_ROOT.glob('*.mjs'):
            imports = re.findall(r'''from\s+['"]\./([^'"]+)['"]''', source.read_text(encoding='utf-8'))
            for name in imports:
                with self.subTest(source=source.name, dependency=name):
                    status, headers, body = self.request('/dashboard/' + name)
                    self.assertEqual(status, 200, body)
                    self.assertEqual(headers['Content-Type'].split(';')[0], 'text/javascript')

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

    def test_rejected_origin_discards_body_arriving_after_headers(self):
        import select
        ticket = self.ticket()
        body = json.dumps({'ticket': ticket}).encode()
        headers = (f'POST /dashboard/session HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n'
                   f'Origin: https://evil.example\r\nContent-Type: application/json\r\n'
                   f'Content-Length: {len(body)}\r\n\r\n').encode()
        entered = threading.Event()
        provenance = self.server.dashboard.provenance

        def wait_for_separate_body(handler, **kwargs):
            entered.set()
            # Keep the body in the socket, outside the header parser's buffer.
            if not select.select([handler.connection], [], [], 2)[0]:
                raise TimeoutError('test body was not delivered')
            return provenance(handler, **kwargs)

        with patch.object(self.server.dashboard, 'provenance', wait_for_separate_body):
            with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2) as connection:
                connection.sendall(headers)
                self.assertTrue(entered.wait(2))
                connection.sendall(body)
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 403)
                response.read()
                self.assertEqual(connection.recv(1), b'')
                response.close()
        self.assertEqual(self.request('/dashboard/session', 'POST', {'ticket': ticket}, {'Origin': self.origin})[0], 200)

    def test_rejected_origin_does_not_wait_for_an_unfinished_body(self):
        import time
        headers = (f'POST /dashboard/session HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n'
                   'Origin: https://evil.example\r\nContent-Length: 4096\r\n\r\n').encode()
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2) as connection:
            start = time.monotonic()
            connection.sendall(headers)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 403)
            response.read()
            self.assertEqual(connection.recv(1), b'')
            self.assertLess(time.monotonic() - start, 1.5)
            response.close()

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
                           ('/dashboard/graph.mjs', 'text/javascript'), ('/dashboard/timeline.mjs', 'text/javascript'),
                           ('/dashboard/status_labels.mjs', 'text/javascript'),
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

    def test_chain_snapshot_is_shared_per_data_version_and_rebuilt_after_writes(self):
        from agent_hub.dashboard_chains import DashboardChains
        cookie = self.session()
        headers = {'Cookie': cookie, 'Origin': self.origin}
        first = self.service.submit('snapshot-one', 'fake', 'first chain', str(self.root), 'read_only')
        scan = DashboardChains._scan
        with patch.object(DashboardChains, '_scan', autospec=True, side_effect=scan) as spy:
            # List, detail and a repeated unconditional read share one scan.
            for path in ['/dashboard/api/v1/chains', '/dashboard/api/v1/chains/' + first['id'],
                         '/dashboard/api/v1/chains']:
                self.assertEqual(self.request(path, headers=headers)[0], 200, path)
            self.assertEqual(spy.call_count, 1)
            # Different filters are separate cache entries.
            self.assertEqual(self.request('/dashboard/api/v1/chains?q=first', headers=headers)[0], 200)
            self.assertEqual(spy.call_count, 2)
            # Any committed write invalidates the shared snapshot.
            second = self.service.submit('snapshot-two', 'fake', 'second chain', str(self.root), 'read_only')
            status, _, body = self.request('/dashboard/api/v1/chains', headers=headers)
            self.assertEqual(status, 200, body)
            self.assertEqual(spy.call_count, 3)
            self.assertEqual({item['id'] for item in json.loads(body)['items']}, {first['id'], second['id']})
            self.service.store.event(first['id'], 'progress', {'message': 'tick'})
            self.assertEqual(self.request('/dashboard/api/v1/chains/' + first['id'], headers=headers)[0], 200)
            self.assertEqual(spy.call_count, 4)
        # Older versions are dropped instead of accumulating.
        self.assertTrue(all(key[0] == max(k[0] for k in self.server.dashboard.snapshots.entries)
                            for key in self.server.dashboard.snapshots.entries))

    def work_session(self, task_id):
        status, headers, body = self.request('/work/' + task_id + '/session', 'POST', None,
                                             {'Authorization': 'Bearer native-test-secret'})
        self.assertEqual(status, 204, body)
        return headers['Set-Cookie']

    def test_workbench_sessions_are_task_bound_expire_and_are_bounded(self):
        from agent_hub.dashboard_http import SESSION_TTL
        from hub_fixtures import with_work_item
        task = self.service.call('submit_task', with_work_item(self.service, dict(request_id='work-session', agent='fake',
            prompt='workbench session', cwd=str(self.root), mode='read_only')))
        other = self.service.call('submit_task', with_work_item(self.service, dict(request_id='work-session-other',
            agent='fake', prompt='other', cwd=str(self.root), mode='read_only'), 'other'))
        header = self.work_session(task['id'])
        for flag in ['HttpOnly', 'Path=/work', 'SameSite=Strict', 'Max-Age=' + str(SESSION_TTL)]:
            self.assertIn(flag, header)
        name, value = header.split(';')[0].split('=', 1)
        # Port-scoped so two local Hubs on 127.0.0.1 never overwrite each other's session.
        self.assertEqual(name, 'hub_work_' + str(self.server.server_port))
        cookie = {'Cookie': name + '=' + value}
        snapshot = '/work/' + task['id'] + '/snapshot'
        self.assertEqual(self.request(snapshot, headers=cookie)[0], 200)
        self.assertEqual(self.request(snapshot, headers={'Cookie': 'hub_work=' + value})[0], 200, 'legacy name')
        self.assertEqual(self.request(snapshot, headers={'Cookie': 'hub_work_1=' + value})[0], 401, 'other port')
        self.assertEqual(self.request('/work/' + other['id'] + '/snapshot', headers=cookie)[0], 401)
        # Expired sessions are rejected and pruned instead of accumulating.
        with patch('agent_hub.daemon.time') as clock:
            clock.monotonic.return_value = time.monotonic() + SESSION_TTL + 1
            self.assertEqual(self.request(snapshot, headers=cookie)[0], 401)
        self.assertEqual(self.request(snapshot, headers=cookie)[0], 401)
        with patch('agent_hub.daemon.WORK_SESSION_LIMIT', 2):
            self.work_session(task['id'])
            self.work_session(task['id'])
            status, _, body = self.request('/work/' + task['id'] + '/session', 'POST', None,
                                           {'Authorization': 'Bearer native-test-secret'})
            self.assertEqual(status, 429, body)

    def test_data_routes_read_current_service_without_controller_mutation(self):
        from hub_fixtures import with_work_item
        task = self.service.call('submit_task', with_work_item(self.service, dict(request_id='http-dashboard', agent='fake',
            prompt='HTTP dashboard test', cwd=str(self.root), mode='read_only')))
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

    def test_chain_routes_are_authenticated_additive_and_bounded(self):
        from hub_fixtures import with_work_item
        task = self.service.call('submit_task', with_work_item(self.service, dict(request_id='chain-http', agent='fake',
            prompt='chain HTTP goal', cwd=str(self.root), mode='read_only')))
        child = self.service.store.create('chain-child', dict(agent='fake', prompt='continuation',
            mode='read_only', cwd=str(self.root), parent_id=task['id'], resume_session='existing-session'))
        self.assertEqual(self.request('/dashboard/api/v1/chains')[0], 401)
        self.assertEqual(self.request('/dashboard/api/v1/chains/' + child['id'])[0], 401)
        headers = {'Cookie': self.session(), 'Origin': self.origin}
        status, _, body = self.request('/dashboard/api/v1/chains?q=HTTP&agent=fake', headers=headers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['items'][0]['task_count'], 2)
        status, _, body = self.request('/dashboard/api/v1/chains/' + child['id'], headers=headers)
        self.assertEqual(status, 200, body)
        detail = json.loads(body)
        self.assertEqual(detail['chain']['root_task_id'], task['id'])
        self.assertEqual(detail['edges'][0]['kind'], 'continuation')
        self.assertEqual(self.request('/dashboard/api/v1/chains/missing', headers=headers)[0], 404)
        for suffix in ['?limit=0', '?state=' + 'x' * 65, '?q=x&q=y', '?after_seq=0']:
            self.assertEqual(self.request('/dashboard/api/v1/chains' + suffix, headers=headers)[0], 400)
        self.assertEqual(self.request('/dashboard/api/v1/chains/' + task['id'] + '?q=x', headers=headers)[0], 400)
        self.assertEqual(self.request('/dashboard/api/v1/chains/' + task['id'] + '/events', headers=headers)[0], 404)
        self.assertEqual(self.request('/dashboard/api/v1/chains', headers={**headers, 'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/dashboard/api/v1/tasks/' + child['id'], headers=headers)[0], 200)

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
