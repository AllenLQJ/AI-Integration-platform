"""Regressions for uncertainty isolation and resilient, bounded projections."""
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent_hub.dashboard import Dashboard
from agent_hub.dashboard_chains import DashboardChains
from agent_hub.models import HubError, TERMINAL
from agent_hub.service import Service
from test_service import Fake


class SecondOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = Service(self.root / 'data', {'fake': Fake()}, per_agent_concurrency={'fake': 0})
        self.addCleanup(self.service.close)

    def task(self, request, **fields):
        task = self.service.submit(request, 'fake', request, str(self.root), 'read_only', **fields)
        return self.service.store.get(task['id'])

    def test_unknown_remote_workspace_blocks_queued_and_new_tasks(self):
        class Unknown(Fake):
            cancel_scope = 'remote_session'

            def run(self, task, cancel, event):
                raise HubError('REMOTE_STATE_UNKNOWN', 'synthetic unknown', {'remote_cancelled': False})

        self.service.adapters['fake'] = Unknown()
        first = self.task('first')
        queued = self.task('queued')
        self.service.store.update(first['id'], state='running')
        self.service.execute(first, threading.Event())
        self.assertTrue(self.service.store.get(first['id']).get('workspace_quarantined'))
        with self.assertRaises(HubError) as error:
            self.task('new')
        self.assertEqual(error.exception.code, 'WORKSPACE_UNCERTAIN')
        self.service.per_agent_concurrency['fake'] = 1
        end = time.monotonic() + 3
        while time.monotonic() < end:
            result = self.service.store.get(queued['id'])
            if result['state'] in TERMINAL:
                break
            time.sleep(.02)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['result']['error_code'], 'WORKSPACE_UNCERTAIN')

    def test_confirmed_remote_stop_does_not_quarantine(self):
        task = self.task('confirmed')
        self.service.adapters['fake'].cancel_scope = 'remote_session'
        self.service._finish_execution(task, 'cancelled', {'remote_cancelled': True}, threading.Event())
        self.assertFalse(self.service.store.get(task['id']).get('workspace_quarantined'))
        self.assertEqual(self.task('new')['state'], 'queued')

    def test_cancellation_before_executor_dispatch_does_not_quarantine(self):
        task = self.task('cancel-before-dispatch')
        task['acceptance'] = {'criteria': [{'id': 'review', 'kind': 'content_review', 'required': True}]}
        self.service.adapters['fake'].cancel_scope = 'remote_session'
        cancel = threading.Event()
        with patch.object(self.service.verification, 'snapshot', side_effect=lambda *_: cancel.set()):
            self.service.execute(task, cancel)
        result = self.service.call('get_result', {'task_id': task['id']})
        self.assertEqual(self.service.adapters['fake'].calls, 0)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['cancel_scope'], 'not_started')
        self.assertFalse(self.service.store.get(task['id']).get('workspace_quarantined'))
        self.assertEqual(self.task('new')['state'], 'queued')

    def test_unreadable_results_preserve_chain_and_execution_truth(self):
        for content in (None, 'broken-json', '[]'):
            with self.subTest(content=content):
                task = self.task('broken-' + str(content))
                path = self.root / (task['id'] + '.json')
                if content is not None:
                    path.write_text(content, encoding='utf-8')
                self.service.store.finish(task['id'], 'succeeded', {'path': str(path)})
                child = self.task('child-' + str(content), parent_id=task['id'])
                self.service.store.finish(child['id'], 'succeeded', {'answer': 'retained child answer'})
                result = self.service.call('get_result', {'task_id': task['id']})
                self.assertEqual(result['state'], 'succeeded')
                self.assertFalse(result['result_available'])
                self.assertEqual(result['result_error_code'], 'RESULT_UNAVAILABLE')
                detail = Dashboard(self.service).detail(task['id'])
                self.assertTrue(any('结果文件不可用' in m['text'] for m in detail['messages']))
                chain = DashboardChains(Dashboard(self.service)).detail(child['id'])
                self.assertEqual(len(chain['nodes']), 2)
                self.assertTrue(any(m['text'] == 'retained child answer' for m in chain['messages']))

    def test_work_item_projection_batches_long_branching_history(self):
        tasks = []
        for i in range(50):
            fields = {'parent_id': tasks[-1]['id']} if tasks else {}
            task = self.task('attempt-' + str(i), **fields)
            self.service.store.finish(task['id'], 'succeeded', {'answer': 'done'})
            tasks.append(task)
        branch = self.task('branch', parent_id=tasks[0]['id'])
        self.service.store.finish(branch['id'], 'failed', {'error': 'branch failed'})
        for i in range(25):
            self.service.store.event(tasks[-1]['id'], 'progress', {'index': i})
        queries = []
        original = self.service.store.connection

        @contextmanager
        def counted():
            with original() as db:
                db.set_trace_callback(lambda sql: queries.append(sql) if sql.lstrip().upper().startswith(('SELECT', 'WITH')) else None)
                yield db

        with patch.object(self.service.store, 'connection', counted):
            item = self.service.verification.work_item(tasks[-1]['id'])
        self.assertEqual(item['root_task_id'], tasks[0]['id'])
        self.assertEqual(len(item['attempts']), 51)
        self.assertEqual(item['current_task_id'], branch['id'])
        attempt = next(a for a in item['attempts'] if a['task_id'] == tasks[-1]['id'])
        self.assertEqual(attempt['attempt'], 50)
        self.assertEqual([e['data']['index'] for e in attempt['events']], list(range(5, 25)))
        self.assertLessEqual(len(queries), 16)

    def test_older_event_pages_are_chronological_and_exclusive(self):
        task = self.task('history')
        for i in range(450):
            self.service.store.event(task['id'], 'progress', {'index': i})
        latest = Dashboard(self.service).events(task['id'], before_seq=9223372036854775807)
        self.assertEqual(len(latest['events']), 200)
        older = Dashboard(self.service).events(task['id'], before_seq=latest['events'][0]['seq'])
        self.assertEqual(len(older['events']), 200)
        self.assertLess(older['events'][-1]['seq'], latest['events'][0]['seq'])
        self.assertEqual([e['data']['index'] for e in older['events']], list(range(50, 250)))


if __name__ == '__main__':
    unittest.main()
