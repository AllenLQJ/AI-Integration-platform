import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent_hub.dashboard import Dashboard
from agent_hub.models import HubError
from agent_hub.store import Store
from agent_hub.verification import Verification


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.verification = Verification(self.store, None)
        self.service = SimpleNamespace(store=self.store, verification=self.verification,
            adapters={'custom.executor': SimpleNamespace(capabilities=['read_only', 'continue'])})
        self.service.call = Mock(side_effect=lambda tool, args: self.store.get(args['task_id']).get('result', {}))
        self.dashboard = Dashboard(self.service)

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, prompt='test task', **fields):
        return self.store.create(str(len(self.store.list())), dict(agent='custom.executor', prompt=prompt,
            cwd=r'C:\Users\private-user\project', mode='read_only', **fields))

    def test_list_is_paged_in_sql_filtered_and_allowlisted(self):
        a = self.create('alpha % literal', request_secret='sensitive')
        b = self.create('alpha other')
        self.create('beta')
        self.store.update(b['id'], state='running')
        self.store.list = Mock(side_effect=AssertionError('full-table list must not be used'))
        page = self.dashboard.tasks(limit=1, q='alpha')
        self.assertEqual(page['items'][0]['id'], b['id'])
        self.assertEqual(page['next_offset'], 1)
        last = self.dashboard.tasks(limit=1, offset=1, q='alpha')
        self.assertEqual(last['items'][0]['id'], a['id'])
        self.assertIsNone(last['next_offset'])
        self.assertEqual(len(self.dashboard.tasks(q='%', agent='custom.executor')['items']), 1)
        self.assertEqual(len(self.dashboard.tasks(state='running')['items']), 1)
        item = page['items'][0]
        self.assertEqual(set(item), {'id', 'title', 'agent', 'state', 'verification_state',
                                     'verification_label', 'verification_policy', 'work_status',
                                     'created', 'updated', 'workspace_label', 'mode'})
        self.assertEqual(item['workspace_label'], 'project')
        self.assertNotIn('private-user', json.dumps(page))

    def test_inputs_are_bounded_and_missing_tasks_raise(self):
        for args in ({'limit': 0}, {'limit': 101}, {'limit': True}, {'offset': -1},
                     {'offset': 2**63}, {'q': 'x' * 501}, {'q': []}, {'agent': 'x' * 201}):
            with self.subTest(args=args), self.assertRaises(HubError):
                self.dashboard.tasks(**args)
        with self.assertRaises(HubError):
            self.dashboard.events('missing', after_seq=-1)
        with self.assertRaises(HubError) as caught:
            self.dashboard.detail('missing')
        self.assertEqual(caught.exception.code, 'TASK_NOT_FOUND')

    def test_dynamic_agents_never_call_diagnose(self):
        self.service.adapters['custom.executor'].diagnose = Mock(side_effect=AssertionError('diagnose'))
        self.assertEqual(self.dashboard.agents(), {'items': [{'id': 'custom.executor', 'modes': ['read_only', 'continue']}]})
        self.service.call.assert_not_called()

    def test_verification_is_independent_and_read_only(self):
        task = self.create(acceptance={'criteria': [{'id': 'c', 'kind': 'artifact_required'}]})
        self.store.finish(task['id'], 'succeeded', {'answer': 'done'})
        item = self.dashboard.tasks()['items'][0]
        self.assertEqual(item['state'], 'succeeded')
        self.assertEqual(item['verification_state'], 'awaiting_verification')
        self.assertEqual(item['work_status'], 'awaiting_verification')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM controllers').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM verifications').fetchone()[0], 0)

    def test_messages_have_sources_and_final_answer_is_not_duplicated(self):
        task = self.create('prompt token=top-secret')
        self.store.event(task['id'], 'message', {'message': 'First'})
        self.store.event(task['id'], 'message', {'message': 'Second'})
        self.store.finish(task['id'], 'succeeded', {'answer': 'First\nSecond', 'session_id': 'hidden'})
        detail = self.dashboard.detail(task['id'])
        self.assertTrue({'task', 'messages', 'relations', 'criteria', 'artifacts'} <= set(detail))
        self.assertIn('timeline', detail)
        self.assertIn('capture_coverage', detail)
        self.assertEqual([m['text'] for m in detail['messages'][1:]], ['First\nSecond'])
        self.assertEqual(detail['messages'][0]['source'], 'task.prompt')
        self.assertEqual(detail['messages'][-1]['source'], 'task.result')
        self.assertEqual(detail['messages'][-1]['completeness'], 'complete')
        self.assertTrue(any(e.get('source') == 'task.result' for e in detail['timeline']))
        self.assertNotIn('top-secret', json.dumps(detail))
        self.assertEqual({c.args[0] for c in self.service.call.call_args_list}, {'get_result'})

    def test_event_messages_are_honest_about_truncation_and_results_replace_prefix(self):
        task = self.create()
        self.store.event(task['id'], 'message', {'message': 'a' * 1000})
        messages = self.dashboard.detail(task['id'])['messages']
        self.assertEqual(messages[-1]['completeness'], 'possibly_truncated')
        self.assertTrue(messages[-1]['source'].startswith('event:'))
        self.store.finish(task['id'], 'succeeded', {'answer': 'a' * 1200})
        messages = self.dashboard.detail(task['id'])['messages']
        self.assertEqual(len(messages), 2)
        self.assertEqual(len(messages[-1]['text']), 1200)

    def test_unattributed_messages_stay_unknown_and_prompt_echoes_are_omitted(self):
        task = self.create('original user prompt')
        self.store.event(task['id'], 'message', {'message': 'original user prompt'})
        self.store.event(task['id'], 'message', {'message': 'unattributed record'})
        self.store.event(task['id'], 'message', {'message': 'explicit assistant record', 'role': 'assistant'})
        self.store.event(task['id'], 'message', {'message': 'bad role', 'role': {'secret': 'bad'}})
        messages = self.dashboard.detail(task['id'])['messages']
        self.assertEqual([m['text'] for m in messages].count('original user prompt'), 1)
        # Missing/invalid role stays unset — UI uses agent fallback titles, never invents assistant.
        self.assertEqual([m['role'] for m in messages], ['user', None, 'assistant', None])
        timeline = self.dashboard.detail(task['id'])['timeline']
        unattributed = next(e for e in timeline if e.get('text') == 'unattributed record')
        self.assertTrue(unattributed.get('role_missing'))
        self.assertEqual(unattributed['title'], 'custom.executor · 任务消息')

    def test_final_dedup_preserves_explicit_non_assistant_roles(self):
        for role in ('user', 'tool', 'system'):
            for text, extra in (('answer', {}), ('ans', {'truncated': True})):
                with self.subTest(role=role, text=text):
                    task = self.create()
                    self.store.event(task['id'], 'message', dict(message=text, role=role, **extra))
                    self.store.finish(task['id'], 'succeeded', {'answer': 'answer'})
                    messages = self.dashboard.detail(task['id'])['messages']
                    self.assertEqual([(m['role'], m['text']) for m in messages[1:]],
                                     [(role, text), ('assistant', 'answer')])

    def test_aggregate_final_dedup_preserves_other_roles(self):
        task = self.create()
        self.store.event(task['id'], 'message', {'message': 'First', 'role': 'tool'})
        self.store.event(task['id'], 'message', {'message': 'Second', 'role': 'assistant'})
        self.store.finish(task['id'], 'succeeded', {'answer': 'First\nSecond'})
        messages = self.dashboard.detail(task['id'])['messages']
        self.assertEqual([m['text'] for m in messages[1:]], ['First', 'Second', 'First\nSecond'])

    def test_prompt_echo_dedup_preserves_explicit_other_roles(self):
        task = self.create('repeat this')
        for role in ('assistant', 'tool', 'system'):
            self.store.event(task['id'], 'message', {'message': 'repeat this', 'role': role})
        messages = self.dashboard.detail(task['id'])['messages']
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'tool', 'system'])

    def test_same_agent_new_session_is_switch_in_both_directions(self):
        parent = self.create(session_id='old-native-session')
        child = self.create(parent_id=parent['id'])
        self.store.update(child['id'], mode='edit_worktree', session_id='new-native-session')
        self.assertEqual(self.dashboard.detail(parent['id'])['relations'][0]['kind'], 'switch_executor')
        self.assertEqual(self.dashboard.detail(child['id'])['relations'][0]['kind'], 'switched_from')

    def test_history_bound_and_truncation_notice(self):
        task = self.create()
        for i in range(205):
            self.store.event(task['id'], 'message', {'message': 'record ' + str(i)})
        messages = self.dashboard.detail(task['id'])['messages']
        self.assertEqual(len(messages), 202)
        self.assertEqual(messages[1]['completeness'], 'truncated')
        self.assertEqual(messages[2]['text'], 'record 5')
        self.assertEqual(len(self.dashboard.events(task['id'])['events']), 200)

    def test_relations_distinguish_continuation_switch_and_review(self):
        parent = self.create()
        child = self.create(parent_id=parent['id'], resume_session='native-session')
        switched = self.create(parent_id=parent['id'])
        self.store.update(switched['id'], agent='other')
        review = self.create(review_of={'task_id': parent['id']})
        detail = self.dashboard.detail(parent['id'])
        kinds = {r['task_id']: r['kind'] for r in detail['relations']}
        self.assertEqual(kinds, {child['id']: 'continuation', switched['id']: 'switch_executor', review['id']: 'review'})
        self.assertEqual(self.dashboard.detail(review['id'])['relations'][0]['kind'], 'review_of')
        self.assertEqual(self.dashboard.detail(child['id'])['relations'][0]['kind'], 'continued_from')
        self.assertNotIn('delegat', json.dumps(detail))

    def test_custom_events_are_redacted_bounded_and_use_seq_not_updated(self):
        task = self.create()
        before = self.store.get(task['id'])['updated']
        # Bypass Store.safe to model historical/plugin records containing unsafe data.
        raw = {'api_key': 'secret-key', 'nested': {'client_secret': 'client-value'},
               'text': 'Bearer abc123 secret=hidden https://host/path?signature=private C:\\Users\\private-user\\file.txt',
               'payload': 'x' * 100000, 'custom': {'step': 3}}
        with self.store.connection() as db:
            db.execute('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                       (task['id'], 'plugin.custom', json.dumps(raw), 1))
        self.assertEqual(self.store.get(task['id'])['updated'], before)
        page = self.dashboard.events(task['id'])
        encoded = json.dumps(page)
        for secret in ('secret-key', 'client-value', 'abc123', 'hidden', 'signature=private', 'private-user'):
            self.assertNotIn(secret, encoded)
        self.assertLess(len(encoded), 25000)
        self.assertEqual(page['events'][-1]['kind'], 'plugin.custom')
        self.assertEqual(page['events'][-1]['data']['custom'], {'step': 3})
        self.assertEqual(self.dashboard.events(task['id'], page['next_seq'])['events'], [])

    def test_criteria_and_artifacts_are_whitelisted(self):
        task = self.create(acceptance={'criteria': [{'id': 'c', 'kind': 'content_review',
            'description': 'token=private', 'unknown': 'private'}]})
        self.verification.artifacts = Mock(return_value=[{'artifact_id': 'a', 'logical_id': 'artifact://a',
            'sha256': 'a' * 64, 'path': r'C:\private\secret.txt', 'url': 'https://host/?token=bad',
            'source': 'workspace', 'size_bytes': 3, 'private': 'private'}])
        detail = self.dashboard.detail(task['id'])
        self.assertNotIn('private', json.dumps(detail))
        self.assertEqual(detail['artifacts'][0]['logical_id'], 'artifact://a')
        self.assertNotIn('url', detail['artifacts'][0])


if __name__ == '__main__':
    unittest.main()
