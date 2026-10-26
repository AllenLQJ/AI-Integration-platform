import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_hub.adapters.command import Command
from agent_hub.config import adapters
from agent_hub.models import HubError
from agent_hub.plugins import PluginContext


WORKER = '''import json, os, sys, time
from pathlib import Path
request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if request["prompt"] == "sleep": time.sleep(30)
if request["prompt"] == "fail": sys.exit(7)
print(json.dumps({"answer": request["prompt"], "session_id": request.get("resume_session") or "native-1", "source_urls": []}, ensure_ascii=False))
'''


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hub bridge 中文 ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worker = self.root / 'worker with spaces.py'
        self.worker.write_text(WORKER, encoding='utf-8')
        self.context = PluginContext('custom', self.root / 'data' / 'plugins' / 'custom')
        self.task = dict(id='task1', prompt='你好 " & $() ` % ! {mode}\nnext', mode='delegate',
                         workspace=str(self.root), cwd=str(self.root), deadline=time.time() + 20,
                         log_dir=str(self.root / 'data' / 'logs' / 'task1'))

    def config(self, **override):
        return dict(args=[str(self.worker), '{request_file}'], **override)

    def run_command(self, config=None, task=None, cancel=None):
        return Command(config or self.config(), self.context).run(
            task or self.task, cancel or threading.Event(), lambda *a: None)

    def assert_no_requests(self):
        self.assertEqual(list(self.root.rglob('request-*.json')), [])

    def test_request_file_unicode_and_spaces_and_cleanup(self):
        result = self.run_command()
        self.assertEqual(result['answer'], self.task['prompt'])
        self.assertEqual(result['session_id'], 'native-1')
        self.assert_no_requests()

    def test_request_has_only_protocol_fields_not_internal_hub_state(self):
        self.worker.write_text('import json,sys\nfrom pathlib import Path\nr=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))\nprint(json.dumps({"answer":json.dumps(r)}))', encoding='utf-8')
        task = dict(self.task, secret_internal='must not pass', context={'contract_version': '1.0'})
        request = json.loads(self.run_command(task=task)['answer'])
        self.assertEqual(request['schema'], 'agent-hub.command.v1')
        self.assertNotIn('secret_internal', request)
        self.assertEqual(request['context'], task['context'])
        self.assertEqual(request['plugin_data_dir'], str(self.context.data_dir))

    def test_templates_are_literal_single_arguments_and_never_recursive(self):
        self.worker.write_text('import json,sys\nprint(json.dumps({"answer": json.dumps(sys.argv[1:])}))', encoding='utf-8')
        cfg = {'args': [str(self.worker), '{prompt}', '{workspace}', '{mode}']}
        self.assertEqual(json.loads(self.run_command(cfg)['answer']),
                         [self.task['prompt'], self.task['workspace'], 'delegate'])

    def test_json_response_does_not_emit_native_cli_or_hub_events(self):
        self.worker.write_text('import json\nprint(json.dumps({"type":"system","subtype":"init","session_id":"fake"}))\nprint(json.dumps({"hub_event":{"kind":"accepted","data":{}}}))\nprint(json.dumps({"item":4,"message":4}))\nprint(json.dumps({"answer":"ok"}))', encoding='utf-8')
        events = []
        Command(self.config(), self.context).run(self.task, threading.Event(), lambda k, d: events.append(k))
        self.assertEqual(events, ['process'])

    def test_continuation_explicitly_declared_and_passed_to_wrapper(self):
        cfg = self.config(resume_args=[str(self.worker), '{request_file}'])
        self.assertIn('continue', Command(cfg, self.context).capabilities)
        self.assertEqual(self.run_command(cfg, dict(self.task, resume_session='keep-me'))['session_id'], 'keep-me')
        with self.assertRaises(HubError) as caught:
            self.run_command(task=dict(self.task, resume_session='not-supported'))
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_MODE')

    def test_configuration_rejects_false_permissions_and_bad_values(self):
        cases = [{'args': 'shell string'}, {'args': []}, {'args': [1]},
                 {'args': ['{unknown}']}, self.config(modes=['read_only']),
                 self.config(modes=['text_only']), self.config(modes=[]),
                 self.config(modes=['delegate', 'delegate']), self.config(result_format='xml'),
                 self.config(env_from={'KEY': 1}), self.config(env={'KEY': 'literal-secret'}),
                 self.config(executable=12), self.config(resume_args=[])]
        for cfg in cases:
            with self.subTest(cfg=cfg), self.assertRaises(HubError) as caught:
                Command(cfg, self.context)
            self.assertEqual(caught.exception.code, 'INVALID_CONFIG')

    def test_default_executable_and_permissions_are_portable_and_honest(self):
        command = Command(self.config(), self.context)
        info = command.diagnose()
        self.assertEqual(info['resolved_executable'], sys.executable)
        self.assertTrue(info['available'])
        self.assertFalse(command.remote_cancel)
        self.assertEqual(command.cancel_scope, 'remote_session')
        self.assertFalse(command.permission_policy('delegate')['enforced'])
        self.assertFalse(command.permission_policy('delegate')['os_sandbox'])
        self.assertNotIn('continue', command.capabilities)

    def test_env_from_resolves_at_execution_and_missing_reference_fails(self):
        self.worker.write_text('import json,os\nprint(json.dumps({"answer":os.environ["BRIDGE_VALUE"]}))', encoding='utf-8')
        cfg = self.config(env_from={'BRIDGE_VALUE': 'HUB_EXAMPLE_VALUE'})
        with patch.dict(os.environ, {'HUB_EXAMPLE_VALUE': 'hello'}):
            self.assertEqual(self.run_command(cfg)['answer'], 'hello')
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Command(cfg, self.context).diagnose()['available'])
            with self.assertRaises(HubError) as caught:
                self.run_command(cfg)
            self.assertEqual(caught.exception.code, 'AGENT_UNAVAILABLE')
        self.assert_no_requests()

    def test_plain_text_output(self):
        self.worker.write_text('print("first\\nsecond")', encoding='utf-8')
        self.assertEqual(self.run_command(self.config(result_format='text'))['answer'], 'first\nsecond')

    def test_invalid_response_and_nonzero_exit_fail_and_clean_requests(self):
        for output in ('not json', '[]', '{}', '{"answer":1}', '{"answer":"x","answer":"y"}',
                       '{"answer":"x","state":"succeeded"}', '{"answer":"x","remote_cancelled":true}',
                       '{"answer":"x","session_id":false}', ''):
            self.worker.write_text('print(' + repr(output) + ')', encoding='utf-8')
            with self.subTest(output=output), self.assertRaises(HubError):
                self.run_command()
            self.assert_no_requests()
        self.worker.write_text(WORKER, encoding='utf-8')
        with self.assertRaises(HubError) as caught:
            self.run_command(task=dict(self.task, prompt='fail'))
        self.assertEqual(caught.exception.code, 'CLI_FAILED')
        self.assertEqual(caught.exception.details['exit_code'], 7)
        self.assert_no_requests()

    def test_cancel_and_timeout_stop_process_and_clean_requests(self):
        for cancelled in (False, True):
            event = threading.Event()
            timer = threading.Timer(.3, event.set)
            if cancelled:
                timer.start()
            try:
                with self.assertRaises(HubError) as caught:
                    self.run_command(task=dict(self.task, prompt='sleep', deadline=time.time() + (.4 if not cancelled else 10)), cancel=event)
                self.assertEqual(caught.exception.code, 'CANCELLED' if cancelled else 'TIMED_OUT')
                self.assertFalse(caught.exception.details['remote_cancelled'])
                self.assertTrue(caught.exception.details['local_process_stopped'])
                self.assert_no_requests()
            finally:
                timer.cancel(); timer.join() if cancelled else None

    def test_precancelled_task_never_spawns(self):
        stop = threading.Event(); stop.set()
        with patch('agent_hub.adapters.command.run_process') as run:
            with self.assertRaises(HubError):
                self.run_command(cancel=stop)
            run.assert_not_called()

    def test_cancel_and_timeout_preserve_unconfirmed_local_stop(self):
        for code in ('CANCELLED', 'TIMED_OUT'):
            error = HubError(code, 'Process did not confirm exit',
                             {'local_process_stopped': False, 'wait_timeout': True})
            with self.subTest(code=code), patch('agent_hub.adapters.command.run_process', side_effect=error):
                with self.assertRaises(HubError) as caught:
                    self.run_command()
                self.assertIs(caught.exception, error)
                self.assertFalse(error.details['local_process_stopped'])
                self.assertTrue(error.details['wait_timeout'])
                self.assertFalse(error.details['remote_cancelled'])
                self.assertEqual(error.details['cancel_scope'], 'remote_session')
                self.assert_no_requests()

    def test_new_config_instance_loads_without_core_registration(self):
        (self.root / 'config.json').write_text(json.dumps({'agents': {'any-ai': dict(kind='command', **self.config())}}))
        adapter = adapters(self.root)['any-ai']
        self.assertEqual(adapter.run(self.task, threading.Event(), lambda *a: None)['answer'], self.task['prompt'])

    def test_protocol_is_parsed_before_log_redaction(self):
        response = {'answer': 'Use the Authorization: Bearer header.',
                    'session_id': 'https://provider.example/sessions/opaque-id',
                    'source_urls': ['https://source.example/document']}
        self.worker.write_text('print(' + repr(json.dumps(response)) + ')', encoding='utf-8')
        result = self.run_command()
        for key, value in response.items():
            self.assertEqual(result[key], value)
        logged = Path(result['artifacts']['logs']['stdout']).read_text(encoding='utf-8')
        self.assertNotIn('Bearer header.', logged)
        self.assertNotIn('https://provider.example', logged)

    def test_oversized_response_fails_without_unbounded_capture(self):
        self.worker.write_text('print("x" * 129)', encoding='utf-8')
        with patch('agent_hub.adapters.command.MAX_RESPONSE_CHARS', 128, create=True), self.assertRaises(HubError) as caught:
            self.run_command(self.config(result_format='text'))
        self.assertEqual(caught.exception.code, 'INVALID_RESULT')
        self.assert_no_requests()


if __name__ == '__main__':
    unittest.main()
