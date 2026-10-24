import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent_hub.dashboard import Dashboard
from agent_hub.dashboard_chains import DashboardChains
from agent_hub.models import HubError
from agent_hub.store import Store
from agent_hub.verification import Verification


class DashboardChainsTests(unittest.TestCase):
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
        return self.store.create(str(self.counter), dict(prompt=prompt, agent='coder', mode='read_only', **fields))

    def dates(self, task, created, updated=None):
        with self.store.connection() as db:
            db.execute('UPDATE tasks SET created=?,updated=? WHERE id=?', (created, created if updated is None else updated, task['id']))

    def test_explicit_links_group_branches_and_reviews_without_prompt_inference(self):
        root = self.create()
        continued = self.create(parent_id=root['id'], resume_session='session')
        switched = self.create(parent_id=root['id'])
        review = self.create(review_of={'task_id': switched['id']})
        self.create()
        page = self.chains.chains()
        self.assertEqual(sorted(item['task_count'] for item in page['items']), [1, 4])
        detail = self.chains.detail(review['id'])
        self.assertEqual(detail['chain']['id'], root['id'])
        self.assertEqual({(e['source'], e['target'], e['kind']) for e in detail['edges']}, {
            (root['id'], continued['id'], 'continuation'), (root['id'], switched['id'], 'switch_executor'),
            (switched['id'], review['id'], 'review')})
        self.assertFalse(detail['truncated'])

    def test_filters_match_same_member_and_page_whole_chains(self):
        root = self.create('needle %')
        child = self.create('different', parent_id=root['id'])
        self.store.update(child['id'], agent='reviewer', state='running')
        other = self.create('other')
        self.dates(root, 1, 1)
        self.dates(child, 2, 10)
        self.dates(other, 3, 5)
        self.assertEqual(self.chains.chains(q='needle', agent='reviewer')['items'], [])
        self.assertEqual(self.chains.chains(q='%', agent='coder')['items'][0]['task_count'], 2)
        first = self.chains.chains(limit=1)
        self.assertEqual(first['items'][0]['id'], root['id'])
        self.assertEqual(first['next_offset'], 1)
        self.assertEqual(self.chains.chains(limit=1, offset=1)['items'][0]['id'], other['id'])
        self.assertEqual(self.chains.chains(state='running')['items'][0]['id'], root['id'])

    def test_review_does_not_replace_implementation_status_and_list_never_reads_results(self):
        root = self.create('root goal')
        self.store.finish(root['id'], 'succeeded', {'answer': 'done'})
        review = self.create('review', review_of={'task_id': root['id']})
        self.store.update(review['id'], agent='reviewer', state='running')
        item = self.chains.chains()['items'][0]
        self.assertEqual(item['title'], 'root goal')
        self.assertEqual(item['latest_task_id'], root['id'])
        self.assertEqual(item['state'], 'succeeded')
        self.assertEqual(item['active_count'], 1)
        self.assertEqual(item['agents'], ['coder', 'reviewer'])
        self.service.call.assert_not_called()

    def test_cycles_missing_links_and_root_ties_are_deterministic(self):
        a = self.create(parent_id='missing')
        b = self.create(parent_id=a['id'])
        self.store.update(a['id'], parent_id=b['id'], review_of={'task_id': 'absent'})
        self.dates(a, 1)
        self.dates(b, 2)
        detail = self.chains.detail(b['id'])
        self.assertEqual(detail['chain']['id'], a['id'])
        self.assertEqual(set(detail['warnings']), {'cycles', 'dangling_links'})
        self.assertEqual(len(detail['nodes']), 2)
        standalone = self.create(parent_id=['malformed'], review_of='bad')
        self.assertEqual(self.chains.detail(standalone['id'])['chain']['task_count'], 1)

    def test_messages_keep_provenance_chronology_and_role_aware_redaction(self):
        root = self.create('first password=private')
        child = self.create('second', parent_id=root['id'])
        self.store.event(root['id'], 'message', {'message': 'same', 'role': 'tool'})
        self.store.event(root['id'], 'message', {'message': 'same', 'role': 'assistant'})
        self.store.finish(root['id'], 'succeeded', {'answer': 'same'})
        self.store.finish(child['id'], 'succeeded', {'answer': 'same'})
        before = self.store.list()
        messages = self.chains.detail(child['id'])['messages']
        self.assertNotIn('private', json.dumps(messages))
        self.assertEqual(sum(m['text'] == 'same' for m in messages), 3)
        self.assertTrue(all(m['agent'] == 'coder' and m['task_id'] in {root['id'], child['id']} for m in messages))
        self.assertEqual([m['timestamp'] for m in messages], sorted(m['timestamp'] for m in messages))
        self.assertEqual(before, self.store.list())
        self.assertTrue(all(call.args[0] == 'get_result' for call in self.service.call.call_args_list))

    def test_node_cap_preserves_root_and_requested_node_and_bounds_result_reads(self):
        root = self.create()
        members = [root]
        for _ in range(202):
            members.append(self.create(parent_id=root['id']))
        self.dashboard.detail = Mock(wraps=self.dashboard.detail)
        detail = self.chains.detail(members[1]['id'])
        ids = {n['id'] for n in detail['nodes']}
        self.assertEqual(len(ids), 200)
        self.assertIn(root['id'], ids)
        self.assertIn(members[1]['id'], ids)
        self.assertEqual(detail['chain']['task_count'], 203)
        self.assertTrue(detail['truncated'])
        self.assertIn('nodes_truncated', detail['warnings'])
        self.assertLessEqual(self.dashboard.detail.call_count, 200)
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in detail['edges']))

    def test_conversation_caps_disclose_omissions_and_keep_recent_messages(self):
        root = self.create()
        children = [root] + [self.create(parent_id=root['id']) for _ in range(3)]
        for child in children:
            for i in range(150):
                self.store.event(child['id'], 'message', {'message': ('x' * 3000) + str(i), 'role': 'assistant'})
        detail = self.chains.detail(root['id'])
        self.assertLessEqual(len(detail['messages']), 500)
        self.assertLessEqual(sum(len(m['text']) for m in detail['messages']), 1000000)
        self.assertTrue(detail['truncated'])
        self.assertIn('messages_truncated', detail['warnings'])
        self.assertTrue(detail['messages'][-1]['text'].endswith('149'))

    def test_continued_reviews_preserve_latest_implementation_status(self):
        root = self.create()
        self.store.finish(root['id'], 'succeeded', {'answer': 'done'})
        review = self.create(review_of={'task_id': root['id']})
        continued_review = self.create(parent_id=review['id'], resume_session='review-session')
        self.store.update(continued_review['id'], state='failed')
        item = self.chains.chains()['items'][0]
        self.assertEqual(item['latest_task_id'], root['id'])
        self.assertEqual(item['state'], 'succeeded')

    def test_message_count_cap_and_null_timestamp_order(self):
        root = self.create()
        children = [root] + [self.create(parent_id=root['id']) for _ in range(2)]
        for child in children:
            for i in range(205):
                self.store.event(child['id'], 'message', {'message': 'short ' + str(i)})
        detail = self.chains.detail(root['id'])
        self.assertEqual(len(detail['messages']), 500)
        self.assertEqual([m['timestamp'] for m in detail['messages'][-3:]], [None, None, None])
        self.assertIn('messages_truncated', detail['warnings'])
        ordered = [self.chains._message_order(m) for m in detail['messages']]
        self.assertEqual(ordered, sorted(ordered))

    def test_invalid_arguments_and_missing_member(self):
        for args in ({'limit': 0}, {'limit': True}, {'offset': -1}, {'q': []}, {'state': 'x' * 65}):
            with self.subTest(args=args), self.assertRaises(HubError):
                self.chains.chains(**args)
        with self.assertRaises(HubError) as caught:
            self.chains.detail('missing')
        self.assertEqual(caught.exception.code, 'TASK_NOT_FOUND')


if __name__ == '__main__':
    unittest.main()
