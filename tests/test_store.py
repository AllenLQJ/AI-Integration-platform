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

    def test_has_children_uses_the_parent_link(self):
        parent = self.store.create('parent', self.spec)
        self.assertFalse(self.store.has_children(parent['id']))
        child = self.store.create('child', dict(self.spec, parent_id=parent['id']))
        self.assertTrue(self.store.has_children(parent['id']))
        self.assertFalse(self.store.has_children(child['id']))

    def test_pooled_connections_reuse_per_thread_and_keep_transactions_separate(self):
        import sqlite3
        import threading
        self.store.reuse_connections()
        try:
            with self.store.connection() as first:
                pass
            with self.store.connection(write=True) as again:
                self.assertIs(again, first)
                # Nested use gets its own connection: committing it cannot commit the outer write.
                again.execute('BEGIN IMMEDIATE')
                again.execute("INSERT INTO reconcile_requests VALUES ('outer','d','t','{}')")
                with self.assertRaises(sqlite3.OperationalError):
                    with self.store.connection() as nested:
                        self.assertIsNot(nested, again)
                        nested.execute('PRAGMA busy_timeout=0')
                        nested.execute("INSERT INTO reconcile_requests VALUES ('inner','d','t','{}')")
            with self.store.connection() as reader:
                rows = {r[0] for r in reader.execute('SELECT request_id FROM reconcile_requests')}
            self.assertEqual(rows, {'outer'})
            # Another thread never shares this thread's connection.
            seen = []
            def other():
                with self.store.connection() as db:
                    seen.append(db)
            worker = threading.Thread(target=other); worker.start(); worker.join()
            self.assertIsNot(seen[0], first)
            # A failed transaction drops the pooled connection instead of reusing it.
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.connection(write=True) as db:
                    db.execute("INSERT INTO reconcile_requests VALUES ('outer','d','t','{}')")
            with self.store.connection() as fresh:
                self.assertIsNot(fresh, first)
                self.assertFalse(fresh.in_transaction)
            task = self.store.create('pooled', self.spec)
            self.assertEqual(self.store.get(task['id'])['state'], 'queued')
        finally:
            self.store.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            fresh.execute('SELECT 1')
        # After close the store keeps working with per-call connections.
        self.assertEqual(self.store.get(task['id'])['id'], task['id'])

    def test_close_releases_pooled_connections_from_finished_threads(self):
        # A finished thread's pooled connection is kept alive by the sqlite
        # statement-cache cycle, so it outlives the thread until cyclic GC.
        # close() must drop it itself: Windows cannot delete tasks.db while
        # that handle is still open, and cleanup does not wait for GC.
        import gc
        import sqlite3
        import threading
        self.store.reuse_connections()
        def other():
            with self.store.connection() as db:
                db.execute('SELECT 1').fetchall()
        gc.disable()
        try:
            before = {id(obj) for obj in gc.get_objects() if isinstance(obj, sqlite3.Connection)}
            worker = threading.Thread(target=other)
            worker.start()
            worker.join()
            self.store.close()
            leaked = []
            for obj in gc.get_objects():
                if not isinstance(obj, sqlite3.Connection) or id(obj) in before:
                    continue
                try:
                    obj.execute('SELECT 1')
                except sqlite3.ProgrammingError:
                    continue
                leaked.append(obj)
            self.assertEqual(leaked, [])
        finally:
            gc.enable()
            gc.collect()

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
