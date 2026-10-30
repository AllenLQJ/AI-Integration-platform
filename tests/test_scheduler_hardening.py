import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent_hub.store import Store
from agent_hub.verification import Verification
from agent_hub.service import Service
from agent_hub.models import HubError
from test_service import Fake


class VerificationBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'tasks.db')

    def seed(self, count):
        acceptance = {'criteria': [{'id': 'f', 'kind': 'file_exists', 'path': 'x'}]}
        with self.store.connection() as db:
            for i in range(count):
                data = json.dumps({'agent': 'fake', 'prompt': 'history', 'acceptance': acceptance})
                db.execute('INSERT INTO tasks(id,request_id,digest,state,data,created,updated) VALUES(?,?,?,?,?,?,?)',
                           (str(i), str(i), 'd', 'succeeded', data, i, i))

    @contextmanager
    def connections(self, counts):
        original = self.store.connection
        @contextmanager
        def counted(write=False):
            counts.append(1)
            with original(write=write) as db:
                yield db
        with patch.object(self.store, 'connection', counted):
            yield

    def test_startup_batches_history_and_preserves_existing_verdicts(self):
        verification = Verification(self.store, None)
        self.seed(100)
        with self.store.connection() as db:
            db.execute('INSERT INTO verifications VALUES(?,?,?)', ('0', 'passed', '{"evidence":"kept"}'))
            db.execute('INSERT INTO verifications VALUES(?,?,?)', ('1', 'verifying', '{"retry_count":2}'))
        counts = []
        with self.connections(counts):
            recovered = Verification(self.store, None)
        self.assertEqual(recovered.summary(self.store.get('0')), {'state': 'passed', 'evidence': 'kept'})
        self.assertEqual(recovered.summary(self.store.get('1'))['state'], 'awaiting_verification')
        self.assertEqual(recovered.summary(self.store.get('1'))['retry_count'], 2)
        self.assertEqual(recovered.summary(self.store.get('99'))['state'], 'awaiting_verification')
        self.assertEqual(len(recovered.pending_ids), 100)
        self.assertLessEqual(len(counts), 3)

    def test_startup_does_not_decode_entire_prompts_and_results(self):
        self.seed(3)
        with patch.object(self.store, 'list', side_effect=AssertionError('unbounded history payload read')):
            verification = Verification(self.store, None)
        self.assertEqual(verification.pending_ids, {'0', '1', '2'})

    def test_pending_batches_candidates_and_respects_retry_deadlines(self):
        self.seed(100)
        verification = Verification(self.store, None)
        with self.store.connection() as db:
            db.execute("UPDATE verifications SET state='passed' WHERE task_id='0'")
            db.execute("UPDATE verifications SET data=? WHERE task_id='1'", (json.dumps({'next_retry_at': time.time()+3600}),))
        counts = []
        with self.connections(counts):
            pending = verification.pending()
        self.assertEqual(set(pending), {str(i) for i in range(2,100)})
        self.assertNotIn('0', verification.pending_ids)
        self.assertIn('1', verification.pending_ids)
        self.assertLessEqual(len(counts), 2)


class SchedulerWakeupTests(unittest.TestCase):
    def test_idle_scheduler_does_not_scan_the_queue_twenty_times_per_second(self):
        with tempfile.TemporaryDirectory() as td:
            service = Service(Path(td) / 'data', {'fake': Fake()})
            try:
                calls = []
                original = service.store.list
                def counted(states=None):
                    if states == {'queued'}:
                        calls.append(1)
                    return original(states)
                with patch.object(service.store, 'list', counted):
                    time.sleep(1.05)
                self.assertLessEqual(len(calls), 4)
            finally:
                service.close()


    def test_full_slots_skip_queue_reads_and_completion_wakes_a_followup(self):
        started, release, followed = threading.Event(), threading.Event(), threading.Event()
        class Gated(Fake):
            def run(self, task, cancel, event):
                if task['prompt'] == 'first':
                    started.set()
                    release.wait(4)
                else:
                    followed.set()
                return {'answer': 'done'}
        with tempfile.TemporaryDirectory() as td:
            service = Service(Path(td) / 'data', {'fake': Gated()}, concurrency=1)
            try:
                service.submit('first', 'fake', 'first', td, 'read_only')
                self.assertTrue(started.wait(2))
                service.submit('second', 'fake', 'second', td, 'read_only')
                original = service.store.list
                calls = []
                def counted(states=None):
                    if states == {'queued'}:
                        calls.append(1)
                    return original(states)
                with patch.object(service.store, 'list', counted):
                    time.sleep(.25)
                    self.assertEqual(calls, [])
                    release.set()
                    self.assertTrue(followed.wait(.6), 'completion should notify the scheduler without waiting for fallback')
            finally:
                release.set()
                service.close()


class WorkspaceAdmissionTests(unittest.TestCase):
    def test_quarantine_aliases_are_rejected_at_submission(self):
        for kind in (['relative', 'case'] if os.name == 'nt' else ['relative']):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix='MixedCase-') as td:
                root = Path(td)
                (root / 'alias').mkdir()
                alias = str(root / 'alias' / '..') if kind == 'relative' else str(root).lower()
                service = Service(root / 'data', {'fake': Fake()}, per_agent_concurrency={'fake': 0})
                try:
                    task = service.store.create('old', dict(agent='fake', prompt='old', cwd=td,
                        workspace=alias, workspace_quarantined=True, mode='read_only', timeout_seconds=30))
                    service.store.finish(task['id'], 'interrupted', {})
                    with self.assertRaises(HubError) as error:
                        service.submit('new', 'fake', 'new', td, 'read_only')
                    self.assertEqual(error.exception.code, 'WORKSPACE_UNCERTAIN')
                finally:
                    service.close()


if __name__ == '__main__':
    unittest.main()
