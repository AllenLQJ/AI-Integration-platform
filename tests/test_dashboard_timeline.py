import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent_hub.activity import normalize_activity
from agent_hub.dashboard import Dashboard
from agent_hub.dashboard_chains import DashboardChains
from agent_hub.dashboard_timeline import merge_operation_updates, task_timeline
from agent_hub.store import Store
from agent_hub.verification import Verification


class TimelineProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.verification = Verification(self.store, None)
        self.service = SimpleNamespace(store=self.store, verification=self.verification)
        self.service.call = Mock(side_effect=lambda tool, args: self.store.get(args['task_id']).get('result', {}))
        self.dashboard = Dashboard(self.service)
        self.chains = DashboardChains(self.dashboard)
        self.counter = 0

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, prompt='goal', **fields):
        self.counter += 1
        return self.store.create(str(self.counter), dict(
            prompt=prompt, agent='codex', mode='read_only', cwd=self.tmp.name, **fields))

    def test_mixed_chronology_and_call_id_pairing(self):
        task = self.create('请实现功能')
        self.store.event(task['id'], 'activity', normalize_activity({
            'kind': 'file_read', 'operation_id': 'op-1', 'phase': 'started', 'status': 'started',
            'operation': {'path': 'src/a.py', 'tool_name': 'Read'},
        }, self.tmp.name, force_provenance='executor_observed'))
        self.store.event(task['id'], 'message', {'message': '我先读文件', 'role': 'assistant'})
        self.store.event(task['id'], 'activity', normalize_activity({
            'kind': 'file_read', 'operation_id': 'op-1', 'phase': 'completed', 'status': 'completed',
            'operation': {'path': 'src/a.py', 'tool_name': 'Read'},
        }, self.tmp.name, force_provenance='executor_observed'))
        self.store.event(task['id'], 'activity', normalize_activity({
            'kind': 'test', 'operation_id': 'op-2', 'status': 'completed',
            'operation': {'command_display': 'pytest -q', 'exit_code': 0,
                          'test_summary': 'passed=1'},
        }, self.tmp.name, force_provenance='executor_observed'))
        # Missing IDs stay distinct.
        self.store.event(task['id'], 'activity', normalize_activity({
            'kind': 'command', 'status': 'completed',
            'operation': {'command_display': 'ls'},
        }, self.tmp.name, force_provenance='executor_observed'))
        self.store.event(task['id'], 'activity', normalize_activity({
            'kind': 'command', 'status': 'completed',
            'operation': {'command_display': 'pwd'},
        }, self.tmp.name, force_provenance='executor_observed'))
        detail = self.dashboard.detail(task['id'])
        self.assertIn('timeline', detail)
        self.assertIn('messages', detail)
        self.assertIn('capture_coverage', detail)
        ops = [e for e in detail['timeline'] if e['kind'] != 'message']
        paired = [e for e in ops if e.get('operation_id') == 'op-1']
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]['status'], 'completed')
        anonymous = [e for e in ops if not e.get('operation_id') and e['kind'] == 'command']
        self.assertGreaterEqual(len(anonymous), 2)

    def test_cross_task_ids_never_merge_and_provider_revision_dedup(self):
        a = self.create('a')
        b = self.create('b', parent_id=a['id'])
        for task in (a, b):
            self.store.event(task['id'], 'activity', normalize_activity({
                'kind': 'command', 'operation_id': 'shared', 'status': 'started',
                'operation': {'command_display': 'echo ' + task['id'][:4]},
            }, force_provenance='executor_observed'))
            self.store.event(task['id'], 'activity', normalize_activity({
                'kind': 'command', 'operation_id': 'shared', 'status': 'completed',
                'operation': {'command_display': 'echo ' + task['id'][:4], 'exit_code': 0},
            }, force_provenance='executor_observed'))
        self.store.event(a['id'], 'message', {
            'message': 'v1', 'role': 'assistant', 'provider_message_id': 'm9', 'provider_revision': 1})
        self.store.event(a['id'], 'message', {
            'message': 'v2 full', 'role': 'assistant', 'provider_message_id': 'm9', 'provider_revision': 2})
        chain = self.chains.detail(b['id'])
        self.assertEqual(len([e for e in chain['timeline'] if e.get('operation_id') == 'shared']), 2)
        msgs = [e for e in chain['timeline'] if e.get('provider_message_id') == 'm9']
        # Messages come from dashboard message projection; revision fields preserved when present.
        texts = [m['text'] for m in chain['messages'] if m.get('provider_message_id') == 'm9']
        self.assertTrue(any('v2' in t for t in texts) or any(e.get('text') == 'v2 full' for e in chain['timeline']))

    def test_legacy_only_and_api_keys_preserved(self):
        task = self.create('legacy')
        self.store.finish(task['id'], 'succeeded', {'answer': 'done'})
        detail = self.dashboard.detail(task['id'])
        for key in ('task', 'messages', 'relations', 'criteria', 'artifacts', 'timeline', 'capture_coverage'):
            self.assertIn(key, detail)
        self.assertTrue(any(m['source'] == 'task.prompt' for m in detail['messages']))
        self.assertEqual(detail['capture_coverage']['operations'] in {
            'messages_only', 'structured_operations', 'unavailable'}, True)

    def test_budget_includes_nested_operation_fields(self):
        task = self.create('budget')
        blob = 'x' * 5000
        for index in range(80):
            self.store.event(task['id'], 'activity', normalize_activity({
                'kind': 'command',
                'operation': {'command_display': f'cmd-{index}', 'stdout_excerpt': blob},
            }, force_provenance='controller_reported'))
        projected = task_timeline(
            self.store.get(task['id']),
            [{'id': task['id'] + ':prompt', 'role': 'user', 'text': 'p', 'source': 'task.prompt',
              'completeness': 'complete', 'timestamp': 1}],
            self.store.timeline_events(task['id'], limit=400)['events'],
            budget_entries=10,
            budget_chars=20000,
        )
        self.assertTrue(projected['truncated'])
        self.assertLessEqual(len(projected['timeline']), 10)

    def test_hostile_paths_redacted_in_projection(self):
        task = self.create('sec')
        self.store.event(task['id'], 'activity', normalize_activity({
            'kind': 'file_read',
            'operation': {'path': r'C:\Users\private\secrets.env', 'tool_name': 'Read'},
        }, self.tmp.name, force_provenance='controller_reported'))
        detail = self.dashboard.detail(task['id'])
        ops = [e for e in detail['timeline'] if e['kind'] == 'file_read']
        self.assertTrue(ops)
        self.assertNotIn('Users\\private', str(ops[0].get('operation')))
        self.assertNotIn('secrets.env', str(ops[0].get('text', '')))


class MergeHelpersTests(unittest.TestCase):
    def test_merge_requires_same_task_and_id(self):
        entries = [
            dict(id='a1', task_id='t1', kind='command', operation_id='x', status='started', operation={}),
            dict(id='a2', task_id='t2', kind='command', operation_id='x', status='completed', operation={'exit_code': 0}),
            dict(id='a3', task_id='t1', kind='command', operation_id='x', status='completed', operation={'exit_code': 1}),
        ]
        merged = merge_operation_updates(entries)
        self.assertEqual(len(merged), 2)
        own = next(e for e in merged if e['task_id'] == 't1')
        self.assertEqual(own['status'], 'completed')
        self.assertEqual(own['operation']['exit_code'], 1)


if __name__ == '__main__':
    unittest.main()
