import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from agent_hub.store import Store
from agent_hub.models import HubError


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.spec = dict(agent='fake', prompt='测试', cwd=self.tmp.name,
                         mode='read_only', timeout_seconds=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_idempotency_and_conflict(self):
        with ThreadPoolExecutor(8) as pool:
            ids = list(pool.map(lambda _: self.store.create('same', self.spec)['id'], range(20)))
        self.assertEqual(len(set(ids)), 1)
        with self.assertRaises(HubError) as error:
            self.store.create('same', dict(self.spec, prompt='other'))
        self.assertEqual(error.exception.code, 'REQUEST_CONFLICT')

    def test_events_pagination_and_terminal_immutability(self):
        task = self.store.create('a', self.spec)
        for i in range(205):
            self.store.event(task['id'], 'progress', {'n': i})
        page = self.store.events(task['id'], 0, 200)
        self.assertEqual(len(page['events']), 200)
        self.assertGreater(len(self.store.events(task['id'], page['next_seq'], 200)['events']), 0)
        self.store.finish(task['id'], 'cancelled', {'answer': ''})
        self.assertFalse(self.store.finish(task['id'], 'succeeded', {'answer': 'wrong'}))
        self.assertEqual(self.store.get(task['id'])['state'], 'cancelled')

    def test_restart_does_not_requeue_running(self):
        a = self.store.create('a', self.spec)
        b = self.store.create('b', self.spec)
        self.store.update(a['id'], state='running')
        self.store.recover()
        self.assertEqual(self.store.get(a['id'])['state'], 'interrupted')
        self.assertTrue(self.store.get(a['id'])['workspace_quarantined'])
        self.assertEqual(self.store.get(b['id'])['state'], 'queued')

if __name__ == '__main__':
    unittest.main()
