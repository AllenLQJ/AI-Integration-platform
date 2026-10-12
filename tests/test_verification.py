import hashlib
import os
import threading
from contextlib import contextmanager
import tempfile
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub.service import Service
from agent_hub.models import HubError
from agent_hub.verification import render


class Fake:
    capabilities = ['read_only', 'edit_worktree']
    remote_cancel = True
    def diagnose(self):
        return {'available': True}
    def run(self, task, cancel, event):
        if task['prompt'] == 'write':
            (Path(task['workspace']) / 'output.txt').write_bytes(b'hello')
        return {'answer': '完成', 'session_id': 'fake-session'}


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.service = Service(self.root / 'data', {'fake': Fake()})
        # Phase-one tests exercise manual continuation; explicitly opt out of automatic rework.
        self.service.max_auto_rework = 0
    def tearDown(self):
        self.service.close(); self.tmp.cleanup()
    def submit(self, criteria=None, **kw):
        args = dict(request_id='first', agent='fake', prompt='完成', cwd=str(self.workspace), mode='read_only')
        if criteria is not None:
            args['acceptance'] = {'criteria': criteria}
        args.update(kw)
        return self.service.call('submit_task', args)['id']
    def wait(self, ident):
        for _ in range(200):
            result = self.service.call('get_result', {'task_id': ident})
            if result['ready'] and result['verification']['state'] not in {'awaiting_verification', 'verifying'}:
                return result
            time.sleep(.02)
        self.fail('验收未完成')
    def record(self, ident, **kw):
        args = dict(request_id='review', task_id=ident, verifier_id='human', decision='pass', reason='已核对')
        task = self.service.store.get(ident)
        if any(c['kind'] == 'content_review' for c in task['acceptance']['criteria']):
            path = Path(task.get('workspace') or task['cwd']) / 'review-evidence.txt'
            path.write_text('review evidence')
            args['artifact_id'] = self.service.verification.file(task, 'review-evidence.txt')['artifact_id']
        args.update(kw)
        return self.service.call('record_verification', args)
    @contextmanager
    def cli_output(self, stdout, stderr):
        """Exercise both real pump threads using in-memory process pipes, without ps."""
        import json
        from io import BytesIO
        from types import SimpleNamespace
        def process(*args, **kwargs):
            return SimpleNamespace(pid=12345, poll=lambda: 0, wait=lambda timeout: 0,
                                   stdout=BytesIO(('\n'.join(json.dumps(row) for row in stdout) + '\n').encode()),
                                   stderr=BytesIO(('\n'.join(json.dumps(row) for row in stderr) + '\n').encode()))
        with patch('agent_hub.processes.subprocess.Popen', side_effect=process) as popen, \
                patch('agent_hub.processes.stop_descendants'):
            yield popen

    def test_cli_stderr_cannot_emit_events_or_change_session(self):
        import sys
        from agent_hub.adapters.claude import Claude
        self.service.adapters['claude'] = Claude(sys.executable)
        stdout = [dict(hub_event=dict(kind='session', data=dict(session_id='stdout-hub-spoof', source='cli_init'))),
                  dict(type='system', subtype='init', session_id='trusted-session'),
                  dict(type='system', subtype='notice', session_id='notice-spoof'),
                  dict(type='system', subtype='init', session_id='second-init-spoof'),
                  dict(type='result', result='done', is_error=False, session_id='final-spoof')]
        stderr = [dict(type='system', subtype='init', session_id='stderr-spoof'),
                  dict(type='tool_result', name='Write', is_error=False),
                  dict(type='user', message={'content': [{'type': 'tool_result', 'tool_use_id': 'bad'}]}),
                  dict(hub_event=dict(kind='session', data=dict(session_id='stderr-hub-spoof')))]
        with self.cli_output(stdout, stderr) as popen:
            ident = self.submit(agent='claude')
            result = self.wait(ident)
            events = self.service.store.events(ident, limit=200)['events']
            sessions = [e['data']['session_id'] for e in events if e['kind'] == 'session']
            self.assertEqual(sessions, ['trusted-session'])
            self.assertFalse(any(e['kind'] == 'tool' for e in events))
            self.assertEqual(self.service.store.get(ident)['session_id'], 'trusted-session')
            self.assertEqual(result['session_id'], 'trusted-session')
            child = self.service.call('continue_task', dict(request_id='trusted-resume', task_id=ident, prompt='next'))['id']
            self.wait(child)
            self.assertEqual(self.service.store.get(child)['resume_session'], 'trusted-session')
            self.assertIn('trusted-session', popen.call_args.args[0])
            self.assertIn('--resume', popen.call_args.args[0])

    def test_cli_without_stdout_init_cannot_resume_stderr_or_result_session(self):
        import sys
        from agent_hub.adapters.claude import Claude
        self.service.adapters['claude'] = Claude(sys.executable)
        with self.cli_output([dict(type='result', result='done', is_error=False, session_id='final-spoof')],
                             [dict(type='system', subtype='init', session_id='stderr-spoof')]):
            ident = self.submit(agent='claude')
            self.assertIsNone(self.wait(ident)['session_id'])
        self.assertNotIn('session_id', self.service.store.get(ident))
        self.assertFalse(any(e['kind'] == 'session' for e in self.service.store.events(ident)['events']))
        with self.assertRaises(HubError) as caught:
            self.service.call('continue_task', dict(request_id='untrusted-resume', task_id=ident, prompt='next'))
        self.assertEqual(caught.exception.code, 'SESSION_UNAVAILABLE')

    def test_cli_invalid_first_init_cannot_be_replaced(self):
        import sys
        from agent_hub.adapters.claude import Claude
        self.service.adapters['claude'] = Claude(sys.executable)
        with self.cli_output([dict(type='system', subtype='init'),
                              dict(type='system', subtype='init', session_id='later-spoof'),
                              dict(type='result', result='done', is_error=False, session_id='later-spoof')], []):
            ident = self.submit(agent='claude')
            self.assertIsNone(self.wait(ident)['session_id'])
        self.assertNotIn('session_id', self.service.store.get(ident))

    def test_permission_denials_log_only_tool_metadata(self):
        import json
        import sys
        from agent_hub.adapters.claude import Claude
        self.service.adapters['claude'] = Claude(sys.executable)
        result = dict(type='result', result='done', is_error=False, session_id='trusted',
                      permission_denials=[dict(tool_name='Write', tool_use_id='denied-id',
                                               tool_input={'content': 'PRIVATE_FILE_CONTENT' * 1000},
                                               other_large_field='OTHER_PRIVATE_CONTENT' * 1000)])
        with self.cli_output([dict(type='system', subtype='init', session_id='trusted'), result], [result]):
            ident = self.submit(agent='claude'); self.wait(ident)
        for name in ('stdout.log', 'stderr.log'):
            log = (self.root / 'data' / 'logs' / ident / name).read_text()
            self.assertNotIn('PRIVATE_FILE_CONTENT', log)
            self.assertNotIn('OTHER_PRIVATE_CONTENT', log)
            denial = json.loads(log.splitlines()[-1])['permission_denials'][0]
            self.assertEqual(denial, dict(tool_name='Write', tool_use_id='denied-id'))
        approvals = [e for e in self.service.store.events(ident)['events'] if e['kind'] == 'approval']
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]['data']['status'], 'denied')

    def test_tool_result_without_outcome_is_unknown(self):
        from agent_hub.processes import stream_events
        events = list(stream_events(dict(type='user', message={'content': [dict(type='tool_result', tool_use_id='id')]})))
        self.assertEqual(events, [('tool', dict(tool_use_id='id', status='unknown'))])

    def test_external_verification_enforces_controller_epoch_before_receipt(self):
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'output.txt'},
                             {'id': 'r', 'kind': 'content_review', 'description': 'review'}], prompt='write')
        self.wait(ident)
        artifact = self.service.verification.artifacts(ident)[0]['artifact_id']
        args = dict(request_id='leased-external', task_id=ident, verifier_id='reviewer',
                    checks=[dict(criterion_id='r', outcome='pass', reason='checked', evidence=[artifact])])
        first = self.service.call('claim_controller', dict(request_id='external-claim-a', task_id=ident, controller_id='a'))
        second = self.service.call('claim_controller', dict(request_id='external-claim-b', task_id=ident, controller_id='b'))
        before = self.service.verification.summary(self.service.store.get(ident))
        history = self.service.verification.history(ident)
        for lease in ({}, dict(controller_id='a', controller_epoch=first['controller_epoch']),
                      dict(controller_id='b', controller_epoch=first['controller_epoch'])):
            with self.assertRaises(HubError) as caught:
                self.service.call('record_external_verification', dict(args, **lease))
            self.assertEqual(caught.exception.code, 'CONTROLLER_STALE')
            self.assertEqual(before, self.service.verification.summary(self.service.store.get(ident)))
            self.assertEqual(history, self.service.verification.history(ident))
            with self.service.store.connection() as db:
                self.assertIsNone(db.execute('SELECT * FROM verification_requests WHERE request_id=?', (args['request_id'],)).fetchone())
        accepted = self.service.call('record_external_verification', dict(args, controller_id='b', controller_epoch=second['controller_epoch']))
        self.assertEqual(accepted['work_status'], 'completed')
        self.assertEqual(accepted, self.service.call('record_external_verification', dict(args, controller_id='b', controller_epoch=second['controller_epoch'])))
        self.service.call('claim_controller', dict(request_id='external-claim-c', task_id=ident, controller_id='c'))
        with self.assertRaises(HubError) as caught:
            self.service.call('record_external_verification', dict(args, controller_id='b', controller_epoch=second['controller_epoch']))
        self.assertEqual(caught.exception.code, 'CONTROLLER_STALE')

    def test_restart_recovers_saved_rework_once_without_reverification(self):
        # Model the crash window: completed save, before invoking auto_rework.
        with patch.object(self.service, 'auto_rework', return_value=None):
            ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'missing'}])
            self.wait(ident)
            self.service.close()
        summary = self.service.verification.summary(self.service.store.get(ident))
        run_id = summary['verification_run_id']
        count = len(self.service.verification.history(ident))
        import json
        (self.root / 'data' / 'config.json').write_text(json.dumps({'verification': {'max_auto_rework': 1}}))
        self.service = Service(self.root / 'data', {'fake': Fake()})
        for _ in range(300):
            item = self.service.call('get_work_item', dict(task_id=ident))
            if len(item['attempts']) == 2 and item['work_status'] == 'rework_required':
                break
            time.sleep(.02)
        self.assertEqual(len(item['attempts']), 2)
        child = self.service.store.by_request('auto-rework:' + run_id)
        self.assertIsNotNone(child)
        self.assertEqual(child['auto_rework_run_id'], run_id)
        self.assertEqual(len(self.service.verification.history(ident)), count)
        self.service.close()
        self.service = Service(self.root / 'data', {'fake': Fake()})
        self.assertEqual(self.service.auto_rework_pending, set())
        self.assertEqual(len(self.service.call('get_work_item', dict(task_id=ident))['attempts']), 2)
        self.assertEqual(self.service.verification.summary(self.service.store.get(ident))['verification_run_id'], run_id)

    def test_restart_does_not_dispatch_saved_review_or_inconclusive(self):
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'missing'},
                             {'id': 'r', 'kind': 'content_review', 'description': 'review'}])
        self.wait(ident); self.service.close()
        self.service = Service(self.root / 'data', {'fake': Fake()})
        self.assertNotIn(ident, self.service.auto_rework_pending)
        self.assertEqual(len(self.service.call('get_work_item', dict(task_id=ident))['attempts']), 1)

    def test_external_late_failed_attempt_only_appends_superseded(self):
        class Failed(Fake):
            def run(self, task, cancel, event):
                (Path(task['workspace']) / 'output.txt').write_bytes(b'hello')
                event('session', dict(session_id='failed-session'))
                raise HubError('CLI_FAILED', 'failed after writing')
        self.service.adapters['fake'] = Failed()
        ident = self.submit([{'id': 'r', 'kind': 'content_review', 'description': 'review'}])
        self.assertEqual(self.wait(ident)['state'], 'failed')
        artifact = self.service.verification.file(self.service.store.get(ident), 'output.txt')['artifact_id']
        args = dict(request_id='failed-late', task_id=ident, verifier_id='reviewer',
                    checks=[dict(criterion_id='r', outcome='pass', reason='checked existing evidence', evidence=[artifact])])
        with self.assertRaises(HubError) as caught:
            self.service.call('record_external_verification', args)
        self.assertEqual(caught.exception.code, 'INVALID_STATE')
        self.service.adapters['fake'] = Fake()
        child = self.service.call('continue_task', dict(request_id='failed-next', task_id=ident, prompt='next'))['id']
        self.wait(child)
        before = {t: self.service.call('get_task', dict(task_id=t)) for t in (ident, child)}
        result = self.service.call('record_external_verification', args)
        self.assertTrue(result['superseded'])
        history = self.service.verification.history(ident)
        self.assertTrue(history[-1]['superseded'])
        self.assertEqual(result, self.service.call('record_external_verification', args))
        self.assertEqual(history, self.service.verification.history(ident))
        for t in before:
            self.assertEqual(before[t], self.service.call('get_task', dict(task_id=t)))
        for change in (dict(criterion_id='unknown'), dict(evidence=['unknown'])):
            with self.assertRaises(HubError) as caught:
                self.service.call('record_external_verification', dict(args, request_id='invalid-late-' + next(iter(change)), checks=[dict(args['checks'][0], **change)]))
            self.assertEqual(caught.exception.code, 'INVALID_ARGUMENT')

    def test_controller_takeover_rejects_old_epoch_and_replays_claim(self):
        ident = self.submit()
        self.wait(ident)
        args = dict(request_id='claim-1', task_id=ident, controller_id='a')
        first = self.service.call('claim_controller', args)
        self.assertEqual(first['controller_epoch'], 1)
        second = self.service.call('claim_controller', dict(args, request_id='claim-2', controller_id='b'))
        self.assertEqual(second['controller_epoch'], 2)
        self.assertEqual(self.service.call('claim_controller', args), first)
        before = self.service.store.get(ident)
        for tool, extra in [('continue_task', dict(request_id='stale-next', prompt='next')),
                            ('cancel_task', {}), ('verify_task', dict(request_id='stale-verify')),
                            ('record_verification', dict(request_id='stale-record', verifier_id='review', decision='pass', reason='ok'))]:
            with self.assertRaises(HubError) as caught:
                self.service.call(tool, dict(task_id=ident, controller_id='a', controller_epoch=1, **extra))
            self.assertEqual(caught.exception.code, 'CONTROLLER_STALE')
        self.assertEqual(self.service.store.get(ident), before)
        child = self.service.call('continue_task', dict(request_id='new-next', task_id=ident, prompt='next', controller_id='b', controller_epoch=2))
        self.assertEqual(child['parent_id'], ident)

    def test_switch_executor_starts_fresh_session_and_handoff(self):
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'missing.txt'}])
        self.wait(ident)
        args = dict(request_id='switch', task_id=ident, agent='fake', mode='read_only', prompt='fix')
        child = self.service.call('switch_executor', args)
        self.assertEqual(child['parent_id'], ident)
        task = self.service.store.get(child['id'])
        self.assertNotIn('resume_session', task)
        self.assertIn('original_goal', task['prompt'])
        self.assertIn('missing.txt', task['prompt'])
        self.assertEqual(child, self.service.call('switch_executor', args))

    def test_switch_to_edit_does_not_reuse_original_workspace(self):
        ident = self.submit(); self.wait(ident)
        child = self.service.call('switch_executor', dict(request_id='switch-edit', task_id=ident,
                                  agent='fake', mode='edit_worktree', prompt='edit'))
        self.assertNotIn('workspace', child)
        self.assertNotIn('baseline', child)
        self.assertEqual(child['mode'], 'edit_worktree')

    def test_auto_rework_limit_and_same_run_no_duplicate(self):
        self.service.max_auto_rework = 2
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'missing.txt'}])
        for _ in range(300):
            item = self.service.call('get_work_item', dict(task_id=ident))
            if len(item['attempts']) == 3 and item['work_status'] == 'rework_required':
                break
            time.sleep(.02)
        self.assertEqual(len(item['attempts']), 3)
        for a in item['attempts']:
            self.service.auto_rework(a['task_id'])
        self.assertEqual(len(self.service.call('get_work_item', dict(task_id=ident))['attempts']), 3)
        for a in item['attempts'][1:]:
            task = self.service.store.get(a['task_id'])
            self.assertEqual(task['resume_session'], 'fake-session')
            self.assertNotIn('original_goal', task['prompt'])
        events = self.service.store.events(item['current_task_id'], limit=200)['events']
        self.assertTrue(any('上限' in str(e['data']) for e in events))

    def test_needs_review_does_not_auto_rework(self):
        self.service.max_auto_rework = 2
        ident = self.submit([{'id': 'r', 'kind': 'content_review', 'description': 'review'}])
        self.assertEqual(self.wait(ident)['work_status'], 'needs_review')
        self.service.auto_rework(ident)
        self.assertEqual(len(self.service.call('get_work_item', dict(task_id=ident))['attempts']), 1)

    def test_copied_expected_hash_satisfies_artifact_required(self):
        (self.workspace / 'baseline.txt').write_bytes(b'hello')
        class Copy(Fake):
            def run(self, task, cancel, event):
                (Path(task['workspace']) / 'output.txt').write_bytes(b'hello')
                return super().run(task, cancel, event)
        self.service.adapters['fake'] = Copy()
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'},
                             {'id': 'h', 'kind': 'sha256', 'path': 'output.txt', 'expected': hashlib.sha256(b'hello').hexdigest()}])
        self.assertEqual(self.wait(ident)['work_status'], 'completed')
        self.assertEqual(self.service.verification.artifacts(ident)[0]['change'], 'copied')

    def test_external_verification_receipt_and_late_history(self):
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'output.txt'},
                             {'id': 'r', 'kind': 'content_review', 'description': 'review'}], prompt='write')
        self.wait(ident)
        artifact = self.service.verification.artifacts(ident)[0]['artifact_id']
        args = dict(request_id='external', task_id=ident, verifier_id='media-checker',
                    checks=[dict(criterion_id='r', outcome='pass', reason='checked dimensions', evidence=[artifact])])
        first = self.service.call('record_external_verification', args)
        self.assertEqual(first['work_status'], 'completed')
        self.assertEqual(self.service.call('record_external_verification', args), first)
        child = self.service.call('continue_task', dict(request_id='external-next', task_id=ident, prompt='next'))['id']
        self.wait(child)
        before = self.service.verification.summary(self.service.store.get(child))
        late = self.service.call('record_external_verification', dict(args, request_id='late-external', checks=[dict(args['checks'][0], outcome='fail')]))
        self.assertTrue(late['superseded'])
        self.assertEqual(before, self.service.verification.summary(self.service.store.get(child)))
        self.assertTrue(self.service.verification.history(ident)[-1]['superseded'])
        with self.assertRaises(HubError):
            self.service.call('record_external_verification', dict(args, request_id='bad-evidence', checks=[dict(args['checks'][0], evidence=['unknown'])]))

    def test_review_requires_registered_evidence(self):
        ident = self.submit([{'id': 'r', 'kind': 'content_review', 'description': 'review'}])
        self.wait(ident)
        with self.assertRaises(HubError) as caught:
            self.service.call('record_verification', dict(request_id='no-evidence', task_id=ident,
                              verifier_id='reviewer', decision='pass', reason='checked', criterion_id='r'))
        self.assertEqual(caught.exception.code, 'INVALID_ARGUMENT')
        self.assertEqual(self.service.verification.summary(self.service.store.get(ident))['state'], 'needs_review')

    def test_workbench_auth_cookie_html_and_event_cursor(self):
        import json
        import urllib.request
        import urllib.error
        from agent_hub.daemon import make_server
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'output.txt'}], prompt='write')
        self.wait(ident)
        self.service.store.event(ident, 'tool', dict(name='Read', status='success'))
        token = 'workbench-private-token'
        server = make_server(self.service, token)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        base = 'http://127.0.0.1:' + str(server.server_port) + '/work/' + ident
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base)
            self.assertEqual(error.exception.code, 401)
            request = urllib.request.Request(base + '/session', method='POST', headers={'Authorization': 'Bearer ' + token})
            with urllib.request.urlopen(request) as response:
                cookie = response.headers['Set-Cookie']
            self.assertIn('HttpOnly', cookie); self.assertIn('SameSite=Strict', cookie)
            request = urllib.request.Request(base, headers={'Cookie': cookie.split(';')[0]})
            with urllib.request.urlopen(request) as response:
                html = response.read().decode()
            self.assertIn('Read', html); self.assertIn('passed', html); self.assertNotIn(token, html)
            self.assertNotIn(cookie.split(';')[0], html)
            with urllib.request.urlopen(urllib.request.Request(base + '/events?after_seq=0', headers={'Cookie': cookie.split(';')[0]})) as response:
                events = json.load(response)
            self.assertTrue(events['events']); self.assertGreater(events['next_seq'], 0)
            other = self.submit(request_id='other-http-workbench'); self.wait(other)
            for suffix in ('', '/events?after_seq=0'):
                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    urllib.request.urlopen(urllib.request.Request(base.rsplit('/', 1)[0] + '/' + other + suffix, headers={'Cookie': cookie.split(';')[0]}))
                self.assertEqual(unauthorized.exception.code, 401)
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_workbench_handler_without_listening(self):
        from io import BytesIO
        from agent_hub.daemon import make_server
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'output.txt'}], prompt='write')
        self.wait(ident)
        self.service.store.event(ident, 'tool', dict(name='Read', status='success'))
        token = 'handler-test-private-token'
        with patch('agent_hub.daemon.ThreadingHTTPServer', side_effect=lambda address, handler: handler):
            Handler = make_server(self.service, token)
        handler = Handler.__new__(Handler)
        handler.client_address = ('127.0.0.1', 12345)
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'GET /work HTTP/1.1'
        def request(method, suffix='', headers=None):
            handler.path = '/work/' + ident + suffix
            handler.headers = headers or {}
            handler.wfile = BytesIO()
            handler._headers_buffer = []
            getattr(handler, method)()
            return handler.wfile.getvalue().decode()
        self.assertIn('401', request('do_GET'))
        session = request('do_POST', '/session', {'Authorization': 'Bearer ' + token})
        self.assertIn('HttpOnly', session)
        cookie = next(line.split(': ', 1)[1].split(';')[0] for line in session.splitlines() if line.startswith('Set-Cookie:'))
        html = request('do_GET', headers={'Cookie': cookie})
        self.assertIn('200', html); self.assertIn('Read', html); self.assertIn('passed', html)
        self.assertNotIn(token, html); self.assertNotIn(cookie, html)
        self.assertIn('401', request('do_GET', headers={'Cookie': cookie, 'Origin': 'https://example.com'}))
        events = request('do_GET', '/events?after_seq=0', {'Cookie': cookie})
        self.assertIn('next_seq', events)
        self.assertIn('401', request('do_GET', '/events?after_seq=0'))
        other = self.submit(request_id='unrelated-workbench'); self.wait(other)
        original = ident
        ident = other
        self.assertIn('401', request('do_GET', headers={'Cookie': cookie}))
        self.assertIn('401', request('do_GET', '/events?after_seq=0', {'Cookie': cookie}))
        self.assertIn('401', request('do_POST', '/session', {'Cookie': cookie}))
        ident = original
        self.assertIn('401', request('do_GET', '/events?after_seq=0&attempt_id=' + other, {'Cookie': cookie}))
        child = self.service.call('continue_task', dict(request_id='workbench-next', task_id=ident, prompt='next'))['id']
        self.wait(child)
        self.assertIn('200', request('do_GET', '/events?after_seq=0&attempt_id=' + child, {'Cookie': cookie}))

    def test_mixed_inconclusive_and_fail_does_not_auto_rework(self):
        self.service.max_auto_rework = 2
        ident = self.submit([{'id': 'f', 'kind': 'file_exists', 'path': 'missing'},
                             {'id': 'r', 'kind': 'content_review', 'description': 'review'}])
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')
        self.service.auto_rework(ident)
        self.assertEqual(len(self.service.call('get_work_item', dict(task_id=ident))['attempts']), 1)

    def test_controller_lease_survives_restart(self):
        ident = self.submit(); self.wait(ident)
        lease = self.service.call('claim_controller', dict(request_id='persistent-claim', task_id=ident, controller_id='owner'))
        self.service.close()
        self.service = Service(self.root / 'data', {'fake': Fake()})
        with self.assertRaises(HubError) as caught:
            self.service.call('cancel_task', dict(task_id=ident))
        self.assertEqual(caught.exception.code, 'CONTROLLER_STALE')
        self.assertEqual(self.service.call('claim_controller', dict(request_id='persistent-claim', task_id=ident, controller_id='owner')), lease)

    def test_stream_events_only_observed_metadata(self):
        from agent_hub.processes import stream_events
        events = list(stream_events(dict(type='item.completed', item=dict(type='mcp_tool_call', tool='Read', status='completed', result='secret content'))))
        self.assertEqual(events, [('tool', {'name': 'Read', 'status': 'success'})])
        self.assertNotIn('secret content', str(events))

    def test_registered_media_history_exceeds_file_budget(self):
        from PIL import Image
        import json
        root = self.service.gateway.artifact_root; root.mkdir(exist_ok=True)
        path = root / 'archived.png'; Image.new('RGB', (2, 2), 'red').save(path)
        self.service.gateway.register('archived', [{'path': str(path), 'mime_type': 'image/png'}])
        with self.service.store.connection() as db:
            data = db.execute('SELECT data FROM media_artifacts').fetchone()['data']
            db.executemany('INSERT INTO media_artifacts VALUES (?,?,?)',
                           [(f'archived-{i}', f'owner-{i}', data) for i in range(1001)])
        ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}], prompt='write')
        self.assertEqual(self.wait(ident)['work_status'], 'completed')

    def test_execution_dispatch_recovers_from_list_error(self):
        failed = threading.Event()
        original = self.service.store.list
        def listing(states=None):
            if states == {'queued'} and not failed.is_set():
                failed.set(); raise RuntimeError('SQLite locked')
            return original(states)
        with self.assertLogs('agent_hub.service', level='ERROR'), patch.object(self.service.store, 'list', side_effect=listing):
            ident = self.submit()
            self.assertEqual(self.wait(ident)['state'], 'succeeded')
        self.assertTrue(failed.is_set())
        self.assertTrue(self.service.thread.is_alive())

    def test_execution_dispatch_reclaims_unstarted_worker(self):
        failed = threading.Event()
        original = threading.Thread.start
        def start(worker):
            if worker._target == self.service.execute and not failed.is_set():
                failed.set(); raise RuntimeError('启动失败')
            return original(worker)
        with self.assertLogs('agent_hub.service', level='ERROR'), patch.object(threading.Thread, 'start', new=start):
            ident = self.submit()
            self.assertEqual(self.wait(ident)['state'], 'succeeded')
        self.assertTrue(failed.is_set())
        self.assertTrue(self.service.thread.is_alive())
        self.assertNotIn(ident, self.service.active)

    def test_execution_dispatch_recovers_from_update_error(self):
        failed = threading.Event()
        original = self.service.store.update
        def update(ident, **data):
            if data.get('state') == 'running' and not failed.is_set():
                failed.set(); raise RuntimeError('磁盘满')
            return original(ident, **data)
        with self.assertLogs('agent_hub.service', level='ERROR'), patch.object(self.service.store, 'update', side_effect=update):
            ident = self.submit()
            self.assertEqual(self.wait(ident)['state'], 'succeeded')
        self.assertTrue(self.service.thread.is_alive())
        self.assertNotIn(ident, self.service.active)

    def test_media_without_artifact_criterion_is_created(self):
        from PIL import Image
        def run(task, cancel, event):
            root = Path(task['media_artifact_root']); root.mkdir(exist_ok=True)
            path = root / 'new.png'; Image.new('RGB', (2, 2), 'red').save(path)
            return {'media_artifacts': [{'path': str(path), 'mime_type': 'image/png'}]}
        self.service.adapters['fake'].run = run
        ident = self.submit([{'id': 'review', 'kind': 'content_review', 'description': '审核'}])
        self.assertEqual(self.wait(ident)['work_status'], 'needs_review')
        item = self.service.call('get_work_item', {'task_id': ident})
        self.assertEqual(item['attempts'][0]['artifacts'][0]['change'], 'created')

    def test_shutdown_during_snapshot_preserves_recovery(self):
        entered = threading.Event()
        original = self.service.verification.snapshot
        def snapshot(task, cancel=None):
            entered.set()
            self.service.stopping.wait(2)
            return original(task, cancel)
        with patch.object(self.service.verification, 'snapshot', side_effect=snapshot), patch.object(self.service.adapters['fake'], 'run', side_effect=AssertionError('不应执行')):
            ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}])
            self.assertTrue(entered.wait(2))
            self.service.close()
        self.assertEqual(self.service.store.get(ident)['state'], 'running')
        self.assertFalse((self.root / 'data' / 'results' / (ident + '.json')).exists())
        self.service = Service(self.root / 'data', {'fake': Fake()})
        task = self.service.store.get(ident)
        self.assertEqual(task['state'], 'interrupted')
        self.assertTrue(task['workspace_quarantined'])
        self.assertEqual(self.service.verification.history(ident), [])

    def test_not_requested_row_gets_normal_automatic_verdict(self):
        with patch.object(self.service.verification, 'pending', return_value=[]):
            ident = self.submit([{'id': 'file', 'kind': 'file_exists', 'path': 'output.txt'}], prompt='write')
            for _ in range(200):
                if self.service.store.get(ident)['state'] == 'succeeded':
                    break
                time.sleep(.01)
            self.assertEqual(self.service.store.get(ident)['state'], 'succeeded')
            with self.service.store.connection() as db:
                db.execute("UPDATE verifications SET state='not_requested',data='{}' WHERE task_id=?", (ident,))
            result = self.service.verification.run(ident, automatic=True)
        self.assertEqual(result['work_status'], 'completed')
        checks = self.service.verification.history(ident)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['outcome'], 'pass')
        self.assertEqual(checks[0]['verifier']['name'], 'hub-deterministic')

    def test_verification_config_defaults_and_overrides(self):
        import json
        from agent_hub.config import verification_options
        values = verification_options(self.root / 'data')
        self.assertEqual(values, dict(max_files=1000, max_bytes=256 * 1024 * 1024, max_entries=5000, max_seconds=10, join_seconds=20, max_auto_rework=2))
        self.service.close()
        (self.root / 'data' / 'config.json').write_text(json.dumps({'verification': dict(max_files=20, max_bytes=2000, max_entries=30, max_seconds=2, join_seconds=1)}))
        self.service = Service(self.root / 'data', {'fake': Fake()})
        self.assertEqual(self.service.verification.max_files, 20)
        self.assertEqual(self.service.verification.max_total_bytes, 2000)
        self.assertEqual(self.service.verification.max_entries, 30)
        self.assertEqual(self.service.verification.max_scan_seconds, 2)
        self.assertEqual(self.service.verification_join_seconds, 1)

    def test_verification_config_rejects_invalid_values(self):
        import json
        from agent_hub.config import verification_options
        path = self.root / 'data' / 'config.json'
        for values in (None, [], {'unknown': 1}, {'max_files': True}, {'max_files': 0},
                       {'max_files': 1000001}, {'max_bytes': '100'}, {'max_bytes': 1024 ** 4 + 1},
                       {'max_entries': 0}, {'max_entries': 10000001}, {'max_seconds': 1.5},
                       {'max_seconds': 3601}, {'join_seconds': False}, {'join_seconds': 301}, {'max_auto_rework': True}, {'max_auto_rework': -1}, {'max_auto_rework': 6}, {'max_auto_rework': 1.5}):
            with self.subTest(values=values):
                path.write_text(json.dumps({'verification': values}))
                with self.assertRaises(HubError) as raised:
                    verification_options(self.root / 'data')
                self.assertEqual(raised.exception.code, 'INVALID_CONFIG')
        path.unlink()

    def test_registered_media_snapshot_avoids_rehash(self):
        from PIL import Image
        root = self.service.gateway.artifact_root
        root.mkdir(exist_ok=True)
        path = root / 'old.png'; Image.new('RGB', (2, 2), 'red').save(path)
        self.service.gateway.register('old-owner', [{'path': str(path), 'mime_type': 'image/png'}])
        original = self.service.verification.read_file
        def read(root, name, limit=None):
            self.assertNotEqual(name, 'old.png')
            return original(root, name, limit)
        with patch.object(self.service.verification, 'read_file', side_effect=read):
            ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}], prompt='write')
            self.assertEqual(self.wait(ident)['work_status'], 'completed')

    def test_copied_media_index_and_show(self):
        from PIL import Image
        import shutil
        Image.new('RGB', (2, 2), 'red').save(self.workspace / 'source.png')
        def run(task, cancel, event):
            root = Path(task['media_artifact_root']); root.mkdir(exist_ok=True)
            path = root / 'copy.png'; shutil.copyfile(self.workspace / 'source.png', path)
            return {'media_artifacts': [{'path': str(path), 'mime_type': 'image/png'}]}
        self.service.adapters['fake'].run = run
        ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}])
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')
        item = self.service.call('get_work_item', {'task_id': ident})
        self.assertEqual(item['attempts'][0]['artifacts'][0]['change'], 'copied')
        self.assertIn('change=copied', render(item))

    def test_media_registered_during_hash_is_excluded(self):
        from PIL import Image
        def run(task, cancel, event):
            root = Path(task['media_artifact_root']); root.mkdir(exist_ok=True)
            path = root / 'race.png'; Image.new('RGB', (2, 2), 'red').save(path)
            return {'media_artifacts': [{'path': str(path), 'mime_type': 'image/png'}]}
        self.service.adapters['fake'].run = run
        original = self.service.verification.read_file
        def read(root, name, limit=None):
            actual = original(root, name, limit)
            if name == 'race.png':
                self.service.gateway.register('other-owner', [{'path': str(Path(root) / name), 'mime_type': 'image/png'}])
            return actual
        with patch.object(self.service.verification, 'read_file', side_effect=read):
            ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}])
            self.assertEqual(self.wait(ident)['work_status'], 'rework_required')
        self.assertEqual(self.service.verification.artifacts(ident)[0]['change'], 'reused')

    def test_close_during_verification_and_restart(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.verification.check
        def slow(task, criterion):
            entered.set(); release.wait(3)
            return original(task, criterion)
        self.service.verification_join_seconds = .1
        with patch.object(self.service.verification, 'check', side_effect=slow):
            ident = self.submit([{'id': 'review', 'kind': 'content_review', 'description': '审核'}])
            self.assertTrue(entered.wait(2))
            started = time.monotonic(); self.service.close()
            self.assertLess(time.monotonic() - started, 1)
            release.set()
            for _ in range(100):
                if not self.service.verification_active:
                    break
                time.sleep(.01)
            self.assertEqual(self.service.verification.summary(self.service.store.get(ident))['state'], 'awaiting_verification')
            self.assertEqual(self.service.verification.history(ident), [])
        self.service = Service(self.root / 'data', {'fake': Fake()})
        self.assertEqual(self.wait(ident)['verification']['state'], 'needs_review')

    def test_existing_not_requested_recovers_after_ensure_failure(self):
        with patch.object(self.service.verification, 'ensure', side_effect=RuntimeError('落盘失败')):
            ident = self.submit([{'id': 'review', 'kind': 'content_review', 'description': '审核'}])
            with self.service.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO verifications VALUES (?,'not_requested','{}')", (ident,))
            for _ in range(100):
                if self.service.store.get(ident)['state'] == 'succeeded':
                    break
                time.sleep(.01)
            self.assertNotIn(self.service.call('get_task', {'task_id': ident})['work_status'], {'succeeded', 'completed'})
        self.assertEqual(self.wait(ident)['verification']['state'], 'needs_review')
        self.assertEqual(self.service.verification.pending(), [])

    def test_snapshot_count_and_byte_budgets(self):
        for key, value in [('max_files', 1), ('max_total_bytes', 2), ('max_entries', 0), ('max_scan_seconds', 0)]:
            with self.subTest(key=key):
                (self.workspace / 'a').write_bytes(b'old')
                (self.workspace / 'b').write_bytes(b'other')
                with patch.object(self.service.verification, key, value):
                    ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}], request_id=key, prompt='write')
                    self.assertEqual(self.wait(ident)['verification']['state'], 'needs_review')
                (self.workspace / 'output.txt').unlink()

    def test_snapshot_time_excluded_from_execution_deadline(self):
        original = self.service.verification.snapshot
        deadlines = []
        class DeadlineFake(Fake):
            def run(inner, task, cancel, event):
                deadlines.append(task['deadline'] - time.time())
                return super().run(task, cancel, event)
        self.service.adapters['fake'] = DeadlineFake()
        def slow(task, cancel=None):
            time.sleep(.25)
            return original(task, cancel)
        with patch.object(self.service.verification, 'snapshot', side_effect=slow):
            ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}], prompt='write', timeout_seconds=10)
            self.assertEqual(self.wait(ident)['state'], 'succeeded')
        self.assertGreater(deadlines[0], 9.9)

    def test_copy_of_existing_file_is_not_new_artifact(self):
        (self.workspace / 'existing').write_bytes(b'hello')
        ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}], prompt='write')
        self.assertEqual(self.wait(ident)['verification']['state'], 'rework_required')

    def test_copy_of_media_file_is_not_new_artifact(self):
        self.service.gateway.artifact_root.mkdir(exist_ok=True)
        (self.service.gateway.artifact_root / 'old.txt').write_bytes(b'hello')
        ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}], prompt='write')
        self.assertEqual(self.wait(ident)['verification']['state'], 'rework_required')

    def test_pending_without_acceptance_does_not_read_database(self):
        with self.service.lock:
            with patch.object(self.service.store, 'connection', side_effect=AssertionError('轮询读库')):
                self.assertEqual(self.service.verification.pending(), [])
        self.assertTrue(self.service.thread.is_alive())

    def test_lock_registry_releases_unused_locks(self):
        import gc
        lock = self.service.verification.task_lock('example')
        self.assertIs(lock, self.service.verification.task_lock('example'))
        del lock; gc.collect()
        self.assertNotIn('example', self.service.verification.task_locks)
        self.service.verification.request('transient', {'test': 1}, lambda: {'ok': True})
        gc.collect()
        self.assertNotIn('transient', self.service.verification.request_locks)

    def test_snapshot_responds_to_cancellation(self):
        original = self.service.verification.snapshot
        def cancelled(task, cancel=None):
            cancel.set()
            return original(task, cancel)
        with patch.object(self.service.verification, 'snapshot', side_effect=cancelled), patch.object(self.service.adapters['fake'], 'run', side_effect=AssertionError('不应执行')):
            ident = self.submit([{'id': 'artifact', 'kind': 'artifact_required'}], prompt='write')
            self.assertEqual(self.wait(ident)['state'], 'cancelled')
        self.assertEqual(self.service.verification.history(ident), [])

    def test_no_acceptance_skips_snapshot(self):
        with patch.object(self.service.verification, 'snapshot', side_effect=AssertionError('不应快照')):
            self.assertEqual(self.wait(self.submit())['state'], 'succeeded')

    def test_legacy(self):
        result = self.wait(self.submit())
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['gateway_state'], 'succeeded')
        self.assertEqual(result['answer'], '完成')
        self.assertEqual(result['media_artifacts'], [])
        self.assertEqual(result['verification']['state'], 'not_requested')
        self.assertEqual(result['work_status'], 'succeeded')
    def test_artifact_required(self):
        (self.workspace / 'existing-source.py').write_text('# 既有源码不能冒充本次产物')
        result = self.wait(self.submit([{'id': 'a', 'kind': 'artifact_required'}]))
        self.assertEqual(result['work_status'], 'rework_required')
        self.assertIn('请生成', result['verification']['checks'][0]['reason'])
    def test_files_and_hash(self):
        # 使用准备好的隔离工作区，测试不依赖沙箱内 git init 权限。
        self.service.workspaces.prepare = lambda task: {'workspace': str(self.workspace), 'baseline': 'fake'}
        self.service.workspaces.artifacts = lambda task: {}
        criteria = [{'id': 'exists', 'kind': 'file_exists', 'path': 'output.txt'},
                    {'id': 'hash', 'kind': 'sha256', 'path': 'output.txt', 'expected': hashlib.sha256(b'hello').hexdigest()}]
        ident = self.submit(criteria, prompt='write', mode='edit_worktree')
        self.assertEqual(self.wait(ident)['work_status'], 'completed')
        criteria[1]['expected'] = '0' * 64
        other = self.submit(criteria, request_id='wrong', prompt='write', mode='edit_worktree')
        self.assertEqual(self.wait(other)['work_status'], 'rework_required')
        item = self.service.call('get_work_item', {'task_id': ident})
        self.assertTrue(item['attempts'][0]['artifacts'][0]['logical_id'].startswith('artifact://'))
        self.assertIn('sha256=', render(item))
    def test_actual_edit_worktree(self):
        # 沙箱允许仓库内部临时目录的 Git fixture；不修改本项目 Git 历史。
        with tempfile.TemporaryDirectory(prefix='.verification-fixture-', dir=Path.cwd()) as name:
            repo = Path(name)
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            git('init'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@localhost')
            (repo / 'base.txt').write_text('base')
            git('add', 'base.txt'); git('commit', '-m', 'fixture baseline')
            criteria = [{'id': 'file', 'kind': 'file_exists', 'path': 'output.txt'},
                        {'id': 'hash', 'kind': 'sha256', 'path': 'output.txt', 'expected': hashlib.sha256(b'hello').hexdigest()},
                        {'id': 'artifact', 'kind': 'artifact_required'}]
            ident = self.submit(criteria, cwd=str(repo), mode='edit_worktree', prompt='write')
            self.assertEqual(self.wait(ident)['work_status'], 'completed')
            self.assertFalse((repo / 'output.txt').exists())
            child = self.service.call('continue_task', dict(request_id='actual-child', task_id=ident, prompt='write'))['id']
            self.assertEqual(self.wait(child)['work_status'], 'rework_required')
            self.assertEqual(self.service.store.get(child)['workspace'], self.service.store.get(ident)['workspace'])

    def test_media_claims_are_not_evidence(self):
        from PIL import Image
        def run(task, cancel, event):
            folder = Path(task['media_artifact_root']); folder.mkdir(exist_ok=True)
            path = folder / 'image.png'; Image.new('RGB', (2, 2), 'red').save(path)
            return {'answer': '完成', 'media_artifacts': [{'path': str(path), 'mime_type': 'image/png',
                    'sha256': '0'*64, 'size_bytes': 999999, 'url': 'https://user:password@example.com/image'}]}
        self.service.adapters['fake'].run = run
        ident = self.submit([{'id': 'media', 'kind': 'artifact_required'}])
        result = self.wait(ident)
        self.assertEqual(result['work_status'], 'completed')
        self.assertEqual(result['verification']['checks'][0]['evidence']['artifacts'][0]['change'], 'created')
        artifact = self.service.verification.artifacts(ident)[0]
        self.assertEqual(artifact['source'], 'media')
        self.assertNotEqual(artifact['sha256'], artifact['claimed']['sha256'])
        self.assertNotEqual(artifact['size_bytes'], artifact['claimed']['size_bytes'])
        self.assertNotIn('url', artifact)
        self.service.close()
        self.service = Service(self.root / 'data', {'fake': Fake()})
        self.assertEqual(self.service.verification.artifacts(ident)[0], artifact)

    def test_specific_review_and_fail(self):
        ident = self.submit([{'id': 'one', 'kind': 'content_review', 'description': '一'},
                             {'id': 'two', 'kind': 'content_review', 'description': '二'}])
        self.assertEqual(self.wait(ident)['work_status'], 'needs_review')
        self.assertEqual(self.record(ident, criterion_id='one')['work_status'], 'needs_review')
        self.assertEqual(self.record(ident, request_id='second', criterion_id='two', decision='fail')['work_status'], 'rework_required')

    def test_paths(self):
        for path in ('../secret', '/tmp/secret', 'C:\\secret', 'a/../../secret'):
            with self.subTest(path=path), self.assertRaises(HubError):
                self.submit([{'id': 'p', 'kind': 'file_exists', 'path': path}])
        outside = self.root / 'secret'; outside.write_text('secret')
        (self.workspace / 'link').symlink_to(outside)
        result = self.wait(self.submit([{'id': 'p', 'kind': 'file_exists', 'path': 'link'}]))
        self.assertEqual(result['work_status'], 'rework_required')
    def test_review_idempotency_and_late_record(self):
        ident = self.submit([{'id': 'review', 'kind': 'content_review', 'description': '核对质量'}])
        self.assertEqual(self.wait(ident)['work_status'], 'needs_review')
        first = self.record(ident)
        count = len(self.service.verification.history(ident))
        self.assertEqual(first['work_status'], 'completed')
        self.assertEqual(first, self.record(ident))
        self.assertEqual(count, len(self.service.verification.history(ident)))
        with self.assertRaises(HubError) as error:
            self.record(ident, reason='不同')
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_CONFLICT')
        child = self.service.call('continue_task', dict(request_id='child', task_id=ident, prompt='返工'))['id']
        self.assertEqual(self.wait(child)['work_status'], 'needs_review')
        old_before = self.service.call('get_task', {'task_id': ident})
        child_before = self.service.call('get_task', {'task_id': child})
        late = self.record(ident, request_id='late', decision='fail')
        self.assertTrue(late['superseded'])
        self.assertEqual(self.service.call('get_task', {'task_id': ident}), old_before)
        self.assertEqual(self.service.call('get_task', {'task_id': child}), child_before)
        self.assertTrue(self.service.verification.history(ident)[-1]['superseded'])
        item = self.service.call('get_work_item', {'task_id': child})
        self.assertEqual(item['work_status'], 'needs_review')
        self.assertEqual([a['attempt'] for a in item['attempts']], [1, 2])
        self.assertEqual(self.service.store.get(child)['acceptance'], self.service.store.get(ident)['acceptance'])
    def test_exception_and_io(self):
        with patch.object(self.service.verification, 'check', side_effect=RuntimeError('boom')):
            ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}])
            self.assertEqual(self.wait(ident)['work_status'], 'needs_review')
        with patch.object(self.service.verification, 'file', side_effect=PermissionError()):
            ident = self.submit([{'id': 'p', 'kind': 'file_exists', 'path': 'x'}], request_id='io')
            self.assertEqual(self.wait(ident)['verification']['checks'][0]['outcome'], 'inconclusive')
    def test_restart_and_verify_idempotency(self):
        ident = self.submit([{'id': 'p', 'kind': 'sha256', 'path': 'output.txt'}], prompt='write')
        self.assertEqual(self.wait(ident)['work_status'], 'completed')
        result = self.service.call('verify_task', dict(request_id='v', task_id=ident))
        count = len(self.service.verification.history(ident))
        self.assertEqual(result, self.service.call('verify_task', dict(request_id='v', task_id=ident)))
        self.assertEqual(count, len(self.service.verification.history(ident)))
        artifacts = self.service.verification.artifacts(ident)
        self.service.close()
        with self.service.store.connection() as db:
            db.execute("UPDATE verifications SET state='verifying' WHERE task_id=?", (ident,))
        self.service = Service(self.root / 'data', {'fake': Fake()})
        self.assertEqual(self.wait(ident)['work_status'], 'completed')
        self.assertGreater(len(self.service.verification.history(ident)), count)
        self.assertEqual(artifacts, self.service.verification.artifacts(ident))
        self.assertEqual(result, self.service.call('verify_task', dict(request_id='v', task_id=ident)))
    def test_acceptance_conflict_and_validation(self):
        self.submit([{'id': 'a', 'kind': 'artifact_required'}])
        with self.assertRaises(HubError) as error:
            self.submit([{'id': 'a', 'kind': 'content_review', 'description': '审核'}])
        self.assertEqual(error.exception.code, 'REQUEST_CONFLICT')
        for criteria in ([{'id': 'a', 'kind': 'unknown'}],
                         [{'id': 'a', 'kind': 'artifact_required'}] * 2,
                         [{'id': 'x'*201, 'kind': 'artifact_required'}],
                         [{'id': 'a', 'kind': 'content_review', 'description': 'x'*10001}]):
            with self.subTest(criteria=criteria), self.assertRaises(HubError):
                self.submit(criteria, request_id='invalid')
    def test_hub_runtime_files_are_not_artifacts(self):
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}], cwd=str(self.service.root))
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')

    def test_declared_existing_and_touch_are_not_artifacts(self):
        path = self.workspace / 'existing.txt'; path.write_text('unchanged')
        criteria = [{'id': 'a', 'kind': 'artifact_required'},
                    {'id': 'declared', 'kind': 'sha256', 'path': 'existing.txt', 'required': False}]
        ident = self.submit(criteria)
        result = self.wait(ident)
        self.assertEqual(result['work_status'], 'rework_required')
        self.assertEqual(result['verification']['checks'][1]['outcome'], 'pass')
        def touch(task, cancel, event):
            os.utime(path, None)
            return {'session_id': 'fake', 'answer': 'touch'}
        self.service.adapters['fake'].run = touch
        ident = self.submit(criteria, request_id='touch')
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')

    def test_renaming_existing_files_does_not_create_artifacts(self):
        existing = self.workspace / 'old.txt'; existing.write_bytes(b'unchanged')
        def rename(task, cancel, event):
            existing.rename(self.workspace / 'new.txt')
            return {'session_id': 'fake'}
        self.service.adapters['fake'].run = rename
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}])
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')
        from PIL import Image
        folder = self.service.gateway.artifact_root; folder.mkdir(exist_ok=True)
        old = folder / 'old.png'; Image.new('RGB', (2, 2), 'red').save(old)
        def rename_media(task, cancel, event):
            new = folder / 'new.png'; old.rename(new)
            return {'media_artifacts': [{'path': str(new), 'mime_type': 'image/png'}]}
        self.service.adapters['fake'].run = rename_media
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}], request_id='rename-media')
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')

    def test_created_changed_and_hash_limit(self):
        (self.workspace / 'output.txt').write_bytes(b'before')
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}], prompt='write')
        check = self.wait(ident)['verification']['checks'][0]
        self.assertEqual(check['evidence']['artifacts'][0]['change'], 'changed')
        self.service.verification.max_file_bytes = 2
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}], request_id='large')
        self.assertEqual(self.wait(ident)['work_status'], 'needs_review')

    def test_media_reuse_is_not_a_new_artifact(self):
        from PIL import Image
        folder = self.service.gateway.artifact_root; folder.mkdir(exist_ok=True)
        path = folder / 'old.png'; Image.new('RGB', (2, 2), 'red').save(path)
        def reuse(task, cancel, event):
            return {'session_id': 'fake', 'media_artifacts': [{'path': str(path), 'mime_type': 'image/png'}]}
        self.service.adapters['fake'].run = reuse
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}])
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')
        # 原文件已登记给第一项任务，即使第二项任务改内容也不能复用为新产物。
        def changed(task, cancel, event):
            Image.new('RGB', (2, 2), 'blue').save(path)
            return reuse(task, cancel, event)
        self.service.adapters['fake'].run = changed
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}], request_id='reuse')
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')

    def test_media_hardlink_reuse_is_rejected(self):
        from PIL import Image
        folder = self.service.gateway.artifact_root; folder.mkdir(exist_ok=True)
        path = folder / 'old.png'; Image.new('RGB', (2, 2), 'red').save(path)
        self.service.gateway.register('old-task', [{'path': str(path), 'mime_type': 'image/png'}])
        def link(task, cancel, event):
            alias = folder / 'alias.png'; os.link(path, alias); path.unlink()
            return {'media_artifacts': [{'path': str(alias), 'mime_type': 'image/png'}]}
        self.service.adapters['fake'].run = link
        ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}])
        self.assertEqual(self.wait(ident)['work_status'], 'rework_required')

    def test_state_initialization_failure_has_bounded_recovery(self):
        with patch.object(self.service.verification, 'ensure', side_effect=RuntimeError('state failed')):
            ident = self.submit([{'id': 'p', 'kind': 'sha256', 'path': 'output.txt'}], prompt='write')
            result = self.wait(ident)
            self.assertEqual(result['work_status'], 'needs_review')
            self.assertEqual(result['verification']['retry_count'], 3)
            self.assertTrue(self.service.thread.is_alive())
            self.assertNotIn(ident, self.service.active)

    def test_attempts_are_linear_and_replays_survive(self):
        criteria = [{'id': 'r', 'kind': 'content_review', 'description': '核对'}]
        parent = self.submit(criteria); self.wait(parent)
        args = dict(request_id='child', task_id=parent, prompt='next')
        child = self.service.call('continue_task', args)['id']; self.wait(child)
        with self.assertRaises(HubError) as error:
            self.service.call('continue_task', dict(args, request_id='branch'))
        self.assertEqual(error.exception.code, 'ATTEMPT_SUPERSEDED')
        self.assertEqual(error.exception.details['current_task_id'], child)
        self.assertEqual(self.service.call('continue_task', args)['id'], child)
        grandchild = self.service.call('continue_task', dict(request_id='third', task_id=child, prompt='next'))['id']
        self.wait(grandchild)
        self.assertEqual(self.service.call('continue_task', args)['id'], child)
        self.assertEqual(self.service.call('get_work_item', {'task_id': parent})['current_task_id'], grandchild)

    def test_legacy_attempts_can_still_branch(self):
        parent = self.submit(); self.wait(parent)
        for request in ('one', 'two'):
            child = self.service.call('continue_task', dict(request_id=request, task_id=parent, prompt='next'))['id']
            self.wait(child)
        self.assertEqual(len(self.service.call('get_work_item', {'task_id': parent})['attempts']), 3)

    def test_verify_conflict_invalid_state_and_failed_key_reuse(self):
        review = [{'id': 'r', 'kind': 'content_review', 'description': '核对'}]
        first = self.submit(review); self.wait(first)
        second = self.submit(review, request_id='second'); self.wait(second)
        self.service.call('verify_task', dict(request_id='verify', task_id=first))
        with self.assertRaises(HubError) as error:
            self.service.call('verify_task', dict(request_id='verify', task_id=second))
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_CONFLICT')
        with self.service.lock:
            self.service.per_agent_concurrency['fake'] = 0
            queued = self.submit(review, request_id='queued')
        with self.assertRaises(HubError) as error:
            self.service.call('verify_task', dict(request_id='failed-key', task_id=queued))
        self.assertEqual(error.exception.code, 'INVALID_STATE')
        self.assertEqual(self.service.call('verify_task', dict(request_id='failed-key', task_id=first))['work_status'], 'needs_review')
        self.service.call('cancel_task', {'task_id': queued})
        with self.assertRaises(HubError) as error:
            self.service.call('verify_task', dict(request_id='cancelled', task_id=queued))
        self.assertEqual(error.exception.code, 'INVALID_STATE')

    def test_record_rejections_do_not_consume_keys(self):
        ident = self.submit([{'id': 'r', 'kind': 'content_review', 'description': '核对'}]); self.wait(ident)
        with self.assertRaises(HubError) as error:
            self.record(ident, criterion_id='missing')
        self.assertEqual(error.exception.code, 'INVALID_ARGUMENT')
        self.assertEqual(self.record(ident, criterion_id='r')['work_status'], 'completed')
        with self.assertRaises(HubError) as error:
            self.record(ident, request_id='invalid-state')
        self.assertEqual(error.exception.code, 'INVALID_STATE')
        with self.service.store.connection() as db:
            self.assertIsNone(db.execute('SELECT * FROM verification_requests WHERE request_id=?', ('invalid-state',)).fetchone())

    def test_required_fail_wins_over_inconclusive(self):
        result = self.wait(self.submit([{'id': 'missing', 'kind': 'file_exists', 'path': 'missing'},
                                       {'id': 'r', 'kind': 'content_review', 'description': '核对'}]))
        self.assertEqual(result['work_status'], 'rework_required')
        self.assertEqual([c['outcome'] for c in result['verification']['checks']], ['fail', 'inconclusive'])

    def test_save_failure_retries_without_stopping_execution(self):
        with patch.object(self.service.verification, 'save', side_effect=RuntimeError('save failed')) as save:
            ident = self.submit([{'id': 'p', 'kind': 'sha256', 'path': 'output.txt'}], prompt='write')
            other = self.submit(request_id='other')
            self.assertEqual(self.wait(other)['state'], 'succeeded')
            result = self.wait(ident)
            self.assertEqual(result['work_status'], 'needs_review')
            self.assertEqual(save.call_count, 3)
            self.assertTrue(self.service.thread.is_alive())
            self.assertTrue(all(c['outcome'] == 'inconclusive' for c in result['verification']['checks']))
            self.assertTrue(all(c['outcome'] != 'pass' for c in self.service.verification.history(ident)))
            kinds = [e['kind'] for e in self.service.store.events(ident, limit=200)['events']]
            self.assertIn('verification_retry', kinds)

    def test_enqueue_and_pending_errors_do_not_leak_slots(self):
        with patch.object(self.service.verification, 'ensure', side_effect=RuntimeError('enqueue failed')):
            ident = self.submit([{'id': 'p', 'kind': 'sha256', 'path': 'output.txt'}], prompt='write')
            for _ in range(100):
                if self.service.store.get(ident)['state'] == 'succeeded':
                    break
                time.sleep(.02)
            with self.service.lock:
                self.assertNotIn(ident, self.service.active)
            self.assertTrue(self.service.thread.is_alive())
        self.assertEqual(self.wait(ident)['work_status'], 'completed')
        with patch.object(self.service.verification, 'pending', side_effect=RuntimeError('pending failed')):
            other = self.submit(request_id='after-pending-error')
            self.assertEqual(self.wait(other)['state'], 'succeeded')
            self.assertTrue(self.service.thread.is_alive())

    def test_polling_is_read_only_and_index_is_idempotent(self):
        self.service.close()
        task = self.service.store.create('raw', dict(agent='fake', prompt='x', cwd=str(self.workspace), mode='read_only', timeout_seconds=30))
        original = self.service.store.connection
        statements = []
        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.service.store, 'connection', traced):
            for tool in ('get_task', 'get_result', 'get_work_item'):
                self.service.call(tool, {'task_id': task['id']})
        self.assertFalse(any(q.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')) for q in statements))
        data = dict(sha256='0'*64, size_bytes=0)
        first = self.service.verification.index(task, 'workspace', 'x', data)
        self.assertEqual(first, self.service.verification.index(task, 'workspace', 'x', data))
        self.assertEqual(len(self.service.verification.artifacts(task['id'])), 1)
        self.assertEqual(self.service.verification.history(task['id']), [])

    def test_windows_open_identity_race_is_inconclusive(self):
        inside = self.workspace / 'output.txt'; inside.write_bytes(b'inside')
        outside = self.root / 'outside'; outside.write_bytes(b'outside')
        self.service.verification.use_dir_fd = False
        original = Path.open
        def swapped(path, *args, **kwargs):
            return original(outside if path == inside.resolve() else path, *args, **kwargs)
        with patch.object(Path, 'open', swapped):
            ident = self.submit([{'id': 'p', 'kind': 'file_exists', 'path': 'output.txt'}])
            result = self.wait(ident)
        self.assertEqual(result['work_status'], 'needs_review')
        self.assertEqual(result['verification']['checks'][0]['outcome'], 'inconclusive')
        self.assertEqual(self.service.verification.artifacts(ident), [])

    def test_slow_verification_does_not_block_other_work_items(self):
        review = self.submit([{'id': 'r', 'kind': 'content_review', 'description': '核对'}]); self.wait(review)
        other_workspace = self.root / 'other'; other_workspace.mkdir()
        entered, release = threading.Event(), threading.Event()
        original = self.service.verification.check
        def slow(task, criterion):
            if criterion['id'] == 'slow':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test timed out')
            return original(task, criterion)
        with patch.object(self.service.verification, 'check', slow):
            try:
                ident = self.submit([{'id': 'slow', 'kind': 'sha256', 'path': 'output.txt'}], request_id='slow', prompt='write', cwd=str(other_workspace))
                self.assertTrue(entered.wait(3))
                results = []
                def control():
                    results.append(self.record(review))
                    results.append(self.service.call('continue_task', dict(request_id='concurrent-child', task_id=review, prompt='next')))
                thread = threading.Thread(target=control); thread.start(); thread.join(1)
                self.assertFalse(thread.is_alive(), '其他工作项被大文件验收锁阻塞')
                self.assertEqual(results[0]['work_status'], 'completed')
                self.assertEqual(self.wait(results[1]['id'])['work_status'], 'needs_review')
            finally:
                release.set()
            self.assertEqual(self.wait(ident)['work_status'], 'completed')

    def test_stale_verification_run_cannot_write_results(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.verification.check
        def slow(task, criterion):
            entered.set(); release.wait(3)
            return original(task, criterion)
        with patch.object(self.service.verification, 'check', slow):
            try:
                ident = self.submit([{'id': 'p', 'kind': 'sha256', 'path': 'output.txt'}], prompt='write')
                self.assertTrue(entered.wait(3))
                with self.service.store.connection() as db:
                    db.execute("UPDATE verifications SET state='needs_review',data=? WHERE task_id=?", ('{"verification_run_id":"new-run"}', ident))
            finally:
                release.set()
            self.wait(ident)
            for _ in range(100):
                with self.service.lock:
                    if ident not in self.service.verification_active:
                        break
                time.sleep(.02)
        self.assertEqual(self.service.verification.summary(self.service.store.get(ident))['verification_run_id'], 'new-run')
        self.assertEqual(self.service.verification.history(ident), [])

    def test_ssh_show_whitelist(self):
        from agent_hub.ssh_client import SshClient, TOOLS
        self.assertIn('get_work_item', TOOLS)
        self.assertNotIn('record_verification', TOOLS)
        self.assertNotIn('verify_task', TOOLS)
        client = SshClient('host', '/python', '/data')
        with patch('agent_hub.ssh_client.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{"work_status":"completed"}', '')) as run:
            self.assertEqual(client.call('get_work_item', {'task_id': 'task'})['work_status'], 'completed')
            self.assertIn('get_work_item', run.call_args.args[0][-1])

    def test_optional_and_non_success(self):
        ident = self.submit([{'id': 'optional', 'kind': 'content_review', 'description': '可选', 'required': False}])
        self.assertEqual(self.wait(ident)['work_status'], 'completed')
        with self.service.lock:
            self.service.per_agent_concurrency['fake'] = 0
            ident = self.submit([{'id': 'a', 'kind': 'artifact_required'}], request_id='cancel')
            self.service.call('cancel_task', {'task_id': ident})
        self.assertEqual(self.wait(ident)['verification']['state'], 'not_requested')
        self.assertEqual(self.wait(ident)['work_status'], 'cancelled')

if __name__ == '__main__':
    unittest.main()
