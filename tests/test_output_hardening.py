import json
import io
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub.models import HubError
from agent_hub.processes import environment, run_process
from agent_hub.verification import safe
from agent_hub.service import Service
from agent_hub.adapters.muse import Muse
from test_service import Fake


class OutputHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_child(self, source, event=lambda *_: None, **kwargs):
        return run_process([sys.executable, '-c', source], self.root, environment(),
            threading.Event(), time.time() + 4, self.root / 'logs', event, **kwargs)

    def test_reader_failure_stops_a_live_child_and_reaches_the_caller(self):
        payload = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'answer'}})
        source = 'import time;print(' + repr(payload) + ',flush=True);time.sleep(30)'
        for error_type in (OSError, ValueError):
            with self.subTest(error_type=error_type):
                def broken_event(kind, data):
                    if kind == 'message':
                        raise error_type('synthetic persistence failure')
                start = time.monotonic()
                with patch.object(threading, 'excepthook'), self.assertRaises(HubError) as error:
                    self.run_child(source, broken_event)
                self.assertEqual(error.exception.code, 'PROCESS_STREAM_ERROR')
                self.assertEqual(error.exception.details['cause'], error_type.__name__)
                self.assertLess(time.monotonic() - start, 2.5)

    def test_explicit_cancellation_takes_priority_over_a_simultaneous_reader_error(self):
        cancel = threading.Event()
        payload = json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'answer'}})
        def callback(kind, data):
            if kind == 'message':
                cancel.set()
                raise OSError('callback failed while cancellation was requested')
        source = 'import time;print(' + repr(payload) + ',flush=True);time.sleep(10)'
        with self.assertRaises(HubError) as error:
            run_process([sys.executable, '-c', source], self.root, environment(), cancel,
                        time.time()+4, self.root / 'logs', callback)
        self.assertEqual(error.exception.code, 'CANCELLED')

    def test_cancellation_or_deadline_during_reader_cleanup_preserves_the_final_reason(self):
        for cancelling in (True, False):
            with self.subTest(cancelling=cancelling):
                cancel = threading.Event()
                deadline = [time.time()+4]
                payload = json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'answer'}})
                def callback(kind, data):
                    if kind == 'message':
                        raise OSError('stream failed before cleanup')
                original_join = threading.Thread.join
                def late_request(thread, timeout=None):
                    if timeout == 15:
                        if cancelling:
                            cancel.set()
                        else:
                            deadline[0] = time.time()-1
                    return original_join(thread, timeout)
                source = 'import time;print('+repr(payload)+',flush=True);time.sleep(10)'
                with patch.object(threading.Thread, 'join', late_request), self.assertRaises(HubError) as error:
                    run_process([sys.executable,'-c',source],self.root,environment(),cancel,
                        lambda:deadline[0],self.root/'logs',callback)
                self.assertEqual(error.exception.code, 'CANCELLED' if cancelling else 'TIMED_OUT')

    def test_process_wait_timeout_keeps_the_reason_and_reports_unconfirmed_stopping(self):
        class StuckProcess:
            pid = 2147483647
            def __init__(self):
                self.stdout, self.stderr = io.BytesIO(b'hello\n'), io.BytesIO()
            def poll(self): return None
            def wait(self, timeout=None): raise subprocess.TimeoutExpired('synthetic child', timeout)
            def stop_tree(self): self.wait(15)
            def close_tree(self): pass
            def close(self): self.stdout.close(); self.stderr.close()
        cancel = threading.Event(); cancel.set()
        with patch('agent_hub.processes.spawn_process', return_value=StuckProcess()):
            with self.assertRaises(HubError) as error:
                run_process(['synthetic'],self.root,environment(),cancel,time.time()+4,self.root/'logs',lambda *_:None)
        self.assertEqual(error.exception.code, 'CANCELLED')
        self.assertTrue(error.exception.details['wait_timeout'])
        self.assertFalse(error.exception.details['local_process_stopped'])

    def test_unconfirmed_local_stopping_blocks_workspace_reuse(self):
        for remote_confirmed in (False, True):
            with self.subTest(remote_confirmed=remote_confirmed):
                class Unconfirmed(Fake):
                    cancel_scope = 'local_process_tree'
                    def run(self, task, cancel, event):
                        raise HubError('PROCESS_STREAM_ERROR','wait timeout', {
                            'local_process_stopped':False,'wait_timeout':True,'remote_cancelled':remote_confirmed})
                service = Service(self.root/('local-unknown-'+str(remote_confirmed)),
                    {'codex':Unconfirmed()},per_agent_concurrency={'codex':0})
                try:
                    task=service.submit('local-unknown','codex','work',str(self.root),'read_only')
                    service.execute(service.store.get(task['id']),threading.Event())
                    result=service.call('get_result',{'task_id':task['id']})
                    self.assertEqual(result['state'],'interrupted')
                    self.assertTrue(result['execution_uncertain'])
                    self.assertEqual(result['remote_cancelled'],remote_confirmed)
                    self.assertTrue(service.store.get(task['id'])['workspace_quarantined'])
                finally:
                    service.close()

    def test_log_open_failure_is_reported_even_if_child_already_exited(self):
        original = Path.open
        def deny_log(path, *args, **kwargs):
            if path.name == 'stdout.log':
                raise OSError('synthetic disk failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', deny_log), patch.object(threading, 'excepthook'):
            with self.assertRaises(HubError) as error:
                self.run_child('print("hello")')
        self.assertEqual(error.exception.code, 'PROCESS_STREAM_ERROR')
        self.assertEqual(error.exception.details['stream'], 'stdout')

    def test_oversized_line_is_omitted_before_capture_and_fails_explicitly(self):
        captured = []
        with patch('agent_hub.processes.MAX_LINE_BYTES', 128, create=True):
            with self.assertRaises(HubError) as error:
                self.run_child('print("secret-body"*100)', stdout_line=captured.append)
        self.assertEqual(error.exception.code, 'OUTPUT_TOO_LARGE')
        self.assertEqual(captured, [])
        logged = (self.root / 'logs' / 'stdout.log').read_text(encoding='utf-8')
        self.assertIn('omitted', logged)
        self.assertNotIn('secret-body', logged)

    def test_stdout_budget_never_returns_a_partly_parsed_success(self):
        with patch('agent_hub.processes.MAX_STDOUT_BYTES', 256, create=True):
            with self.assertRaises(HubError) as error:
                self.run_child('for i in range(30):print("record-"+str(i))')
        self.assertEqual(error.exception.code, 'OUTPUT_TOO_LARGE')
        self.assertTrue(Path(error.exception.details['artifacts']['logs']['stdout']).is_file())

    def test_stderr_returns_a_bounded_tail_and_keeps_the_disk_log(self):
        with patch('agent_hub.processes.MAX_STDERR_BYTES', 64, create=True):
            code, _, stderr, logs = self.run_child('import sys;sys.stderr.write("prefix"*100+"TAIL_MARKER")')
        self.assertEqual(code, 0)
        self.assertLessEqual(len(stderr.encode()), 64)
        self.assertTrue(stderr.endswith('TAIL_MARKER'))
        self.assertTrue(logs['stderr_truncated'])
        self.assertGreater(Path(logs['stderr']).stat().st_size, 64)

    def test_protocol_callback_does_not_require_a_second_stdout_copy(self):
        captured = []
        with patch('agent_hub.processes.MAX_STDOUT_BYTES', 64, create=True):
            code, stdout, _, _ = self.run_child('for i in range(30):print("record-"+str(i))',
                parse_cli_events=False, stdout_line=captured.append, capture_stdout=False)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, '')
        self.assertEqual(len(captured), 30)

    def test_lost_remote_output_does_not_claim_the_remote_execution_failed_or_stopped(self):
        for code in ('PROCESS_STREAM_ERROR', 'OUTPUT_TOO_LARGE'):
            with self.subTest(code=code):
                class RemoteOutputFailure(Fake):
                    cancel_scope = 'remote_session'
                    def run(self, task, cancel, event):
                        raise HubError(code, 'output unavailable', {'local_process_stopped': True})
                service = Service(self.root / code, {'fake': RemoteOutputFailure()}, per_agent_concurrency={'fake': 0})
                try:
                    task = service.submit(code, 'fake', 'work', str(self.root), 'read_only')
                    task = service.store.get(task['id'])
                    service.execute(task, threading.Event())
                    final = service.store.get(task['id'])
                    result = service.call('get_result', {'task_id': task['id']})
                    self.assertEqual(final['state'], 'interrupted')
                    self.assertTrue(final.get('workspace_quarantined'))
                    self.assertFalse(result['remote_cancelled'])
                    self.assertTrue(result['execution_uncertain'])
                    self.assertEqual(result['error_code'], code)
                finally:
                    service.close()


class RedactionBudgetTests(unittest.TestCase):
    def test_long_plain_text_redaction_is_responsive(self):
        text = 'x' * 32000
        start = time.monotonic()
        self.assertEqual(safe(text), text)
        self.assertLess(time.monotonic() - start, 1)

    def test_long_scheme_without_credentials_is_responsive(self):
        text = 'x' * 32000 + '://host/path'
        start = time.monotonic()
        self.assertEqual(safe(text), text)
        self.assertLess(time.monotonic() - start, 1)

    def test_credentials_are_redacted_for_custom_schemes_and_embedded_prefixes(self):
        for text, expected in [
            ('ssh://user:password@host/path', '[URL]'),
            ('123ssh://user:password@host/path', '123[URL]'),
            ('123+.-ssh://user:password@host/path', '123+.-[URL]'),
            ('prefix=abc123ssh://user:password@host/path', 'prefix=[URL]'),
            ('https://example.test/path', '[URL]'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(safe(text), expected)


class MuseOutputCleanupTests(unittest.TestCase):
    def test_unconfirmed_local_stop_does_not_open_a_second_remote_consumer(self):
        with tempfile.TemporaryDirectory() as td:
            task = dict(id='muse-unknown',prompt='question',workspace=td,log_dir=td,deadline=time.time()+4)
            def fake_process(*args, **kwargs):
                args[6]('accepted', {'session_id':'original-session','baseline':4})
                raise HubError('CANCELLED','stop unconfirmed', {'local_process_stopped':False})
            bot = Muse('fixture',env={'HUB_MUSE_STATE_DIR':td})
            with patch('agent_hub.adapters.muse.run_process',side_effect=fake_process) as run:
                with self.assertRaises(HubError) as error:
                    bot.run(task,threading.Event(),lambda *_:None)
            self.assertEqual(run.call_count,1)
            self.assertFalse(error.exception.details['remote_cancelled'])
            self.assertFalse(error.exception.details['local_process_stopped'])

    def test_cancel_uses_the_observed_acceptance_without_rereading_the_full_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); logs = root/'logs'; logs.mkdir()
            context = {'session_id':'original-session','baseline':4}
            (logs/'stdout.log').write_text(json.dumps({'hub_event':{'kind':'accepted','data':context}})+'\n')
            task = dict(id='muse-cleanup',prompt='question',workspace=td,log_dir=str(logs),deadline=time.time()+4)
            cleanup = []
            def fake_process(argv, cwd, env, cancel, deadline, log_dir, event, **kwargs):
                if 'agent_hub.adapters.muse_worker' in argv:
                    event('accepted',context)
                    raise HubError('CANCELLED','stopped')
                request = json.loads(Path(argv[-1]).read_text(encoding='utf-8'))
                cleanup.append(request['cancel_context'])
                return 0,json.dumps({'hub_result':{'remote_cancelled':True}}),'',{}
            original = Path.read_text
            def no_full_log_read(path, *args, **kwargs):
                if path.name == 'stdout.log':
                    raise AssertionError('cancellation must not load the entire stdout log')
                return original(path,*args,**kwargs)
            bot = Muse('fixture',env={'HUB_MUSE_STATE_DIR':td})
            with patch('agent_hub.adapters.muse.run_process',side_effect=fake_process), patch.object(Path,'read_text',no_full_log_read):
                with self.assertRaises(HubError) as error:
                    bot.run(task,threading.Event(),lambda *_:None)
            self.assertEqual(error.exception.code,'CANCELLED')
            self.assertTrue(error.exception.details['remote_cancelled'])
            self.assertEqual(cleanup,[context])


if __name__ == '__main__':
    unittest.main()
