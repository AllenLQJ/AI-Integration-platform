import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub.config import adapters
from agent_hub.models import HubError
from agent_hub.plugins import Executor, ExecutorPlugin, PluginContext, ValidatedExecutor, load_plugin
from agent_hub.service import Service


class Echo(Executor):
    capabilities = ['delegate', 'continue']

    def __init__(self, config, context):
        self.config, self.context = config, context

    def diagnose(self):
        return {'available': True}

    def run(self, task, cancel, event):
        return {'answer': self.config.get('prefix', '') + task['prompt'], 'session_id': 's1'}


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.factory = Mock(side_effect=lambda **kw: Echo(**kw))
        self.entry = SimpleNamespace(name='echo', load=Mock(return_value=ExecutorPlugin(1, self.factory)))
        self.discovery = patch('agent_hub.plugins.metadata.entry_points', return_value=[self.entry])
        self.discovery.start()
        self.addCleanup(self.discovery.stop)

    def load(self, name='assistant', **fields):
        return load_plugin(name, dict(kind='plugin', plugin='echo', options={'prefix': 'ok:'}, **fields), self.root)

    def test_explicit_plugin_and_separate_private_state(self):
        first, second = self.load(), self.load('writer')
        a, b = [call.kwargs['context'] for call in self.factory.call_args_list]
        self.assertEqual(a, PluginContext('assistant', self.root / 'plugins' / 'assistant'))
        self.assertNotEqual(a.data_dir, b.data_dir)
        load_plugin('assistant', {'plugin': 'echo'}, self.root / 'other-device')
        self.assertNotEqual(a.data_dir, self.factory.call_args.kwargs['context'].data_dir)
        result = first.run({'prompt': '你好'}, threading.Event(), lambda *a: None)
        self.assertEqual(result['answer'], 'ok:你好')
        diagnostic = second.diagnose()
        self.assertEqual(diagnostic['plugin'], 'echo')
        self.assertEqual(diagnostic['capabilities'], ['delegate', 'continue'])
        self.assertFalse(diagnostic['permissions']['delegate']['enforced'])
        self.assertFalse(first.remote_cancel)
        self.assertEqual(first.cancel_scope, 'remote_session')

    def test_unselected_and_disabled_plugins_are_never_loaded(self):
        other = SimpleNamespace(name='other', load=Mock(side_effect=AssertionError('must not load')))
        with patch('agent_hub.plugins.metadata.entry_points', return_value=[self.entry, other]):
            self.load()
            (self.root / 'config.json').write_text(json.dumps({'agents': {
                'disabled': {'enabled': False, 'kind': 'plugin', 'plugin': 'other'},
                'enabled': {'kind': 'plugin', 'plugin': 'echo'},
                'codex': {'executable': 'codex'},
            }}), encoding='utf-8')
            self.assertEqual(set(adapters(self.root)), {'enabled', 'codex'})
        other.load.assert_not_called()

    def test_missing_and_duplicate_entry_points_fail(self):
        for entries, code in (([], 'PLUGIN_NOT_FOUND'), ([self.entry, self.entry], 'INVALID_CONFIG')):
            with self.subTest(code=code), patch('agent_hub.plugins.metadata.entry_points', return_value=entries):
                with self.assertRaises(HubError) as caught:
                    self.load()
                self.assertEqual(caught.exception.code, code)
        self.entry.load.assert_not_called()

    def test_api_version_and_load_failure_are_actionable_without_secrets(self):
        for value in (ExecutorPlugin(2, self.factory), ExecutorPlugin(True, self.factory), object()):
            self.entry.load.return_value = value
            with self.assertRaises(HubError) as caught:
                self.load()
            self.assertEqual(caught.exception.code, 'PLUGIN_CONTRACT_ERROR')
        self.entry.load.side_effect = RuntimeError('secret-from-plugin')
        with self.assertRaises(HubError) as caught:
            self.load()
        self.assertEqual(caught.exception.code, 'PLUGIN_LOAD_FAILED')
        self.assertNotIn('secret-from-plugin', str(caught.exception))
        self.factory.assert_not_called()

    def test_invalid_names_options_and_reserved_builtin_names_fail(self):
        for name, item in (
            ('../escape', {'plugin': 'echo'}), ('hub-internal', {'plugin': 'echo'}),
            ('codex', {'plugin': 'echo'}), ('agent', {'plugin': ''}),
            ('agent', {'plugin': 'echo', 'options': []}),
        ):
            with self.subTest(name=name, item=item), self.assertRaises(HubError):
                load_plugin(name, item, self.root)
        self.entry.load.assert_not_called()

    def test_bad_contracts_rejected_before_run(self):
        for field, value in (('capabilities', []), ('capabilities', ['continue']),
                             ('capabilities', ['invented']), ('capabilities', ['delegate', 'delegate']),
                             ('remote_cancel', 'yes'), ('cancel_scope', 'unknown'), ('run', None)):
            instance = Echo({}, PluginContext('a', self.root))
            setattr(instance, field, value)
            self.factory.side_effect = None
            self.factory.return_value = instance
            with self.subTest(field=field, value=value), self.assertRaises(HubError):
                self.load()

    def test_restrictive_modes_cannot_use_unenforced_default_policy(self):
        instance = Echo({}, PluginContext('a', self.root))
        instance.capabilities = ['text_only']
        self.factory.side_effect = None
        self.factory.return_value = instance
        with self.assertRaises(HubError):
            self.load()

    def test_result_contract_and_no_implicit_continuation(self):
        instance = Echo({}, PluginContext('a', self.root))
        instance.capabilities = ['delegate']
        self.factory.side_effect = None
        self.factory.return_value = instance
        adapter = self.load()
        for result in (None, {}, {'answer': 1}, {'answer': 'ok', 'session_id': []},
                       {'answer': 'ok', 'artifacts': []}, {'answer': 'ok', 'media_artifacts': {}},
                       {'answer': 'ok', 'source_urls': [4]}):
            instance.run = Mock(return_value=result)
            with self.subTest(result=result), self.assertRaises(HubError) as caught:
                adapter.run({'prompt': 'x'}, threading.Event(), lambda *a: None)
            self.assertEqual(caught.exception.code, 'PLUGIN_CONTRACT_ERROR')
        instance.run = Mock(return_value={'answer': 'ok', 'session_id': 's1'})
        with self.assertRaises(HubError) as caught:
            adapter.run({'resume_session': 's1'}, threading.Event(), lambda *a: None)
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_MODE')
        instance.run.assert_not_called()

    def test_unknown_enabled_agent_is_not_silently_dropped(self):
        (self.root / 'config.json').write_text(json.dumps({'agents': {'new-tool': {'kind': 'typo'}}}))
        with self.assertRaises(HubError) as caught:
            adapters(self.root)
        self.assertEqual(caught.exception.code, 'INVALID_CONFIG')

    def test_service_rejects_unadvertised_continuation_without_creating_task(self):
        echo = Echo({}, PluginContext('assistant', self.root))
        echo.capabilities = ['delegate']
        service = Service(self.root / 'data', {'assistant': ValidatedExecutor(echo, 'echo')})
        try:
            task = service.call('submit_task', dict(request_id='one', agent='assistant',
                                prompt='hello', cwd=str(self.root), mode='delegate'))
            for _ in range(100):
                if service.call('get_result', {'task_id': task['id']})['ready']:
                    break
                time.sleep(.03)
            self.assertEqual(service.call('get_result', {'task_id': task['id']})['answer'], 'hello')
            with self.assertRaises(HubError) as caught:
                service.call('continue_task', dict(request_id='two', task_id=task['id'], prompt='next'))
            self.assertEqual(caught.exception.code, 'UNSUPPORTED_MODE')
            self.assertIsNone(service.store.by_request('two'))
        finally:
            service.close()


if __name__ == '__main__':
    unittest.main()
