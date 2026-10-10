import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_hub.client import Client
from agent_hub.daemon import make_server
from agent_hub.models import HubError
from agent_hub.service import Service
from test_service import Fake


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.service = Service(self.root, {'fake': Fake()})

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def task(self, key='a'):
        # Unschedulable fixture: deterministic transitions without real executors.
        return self.service.store.create(key, dict(agent='fake', prompt='x',
            cwd=str(self.root), mode='read_only', timeout_seconds=1800))

    def wait(self, ident, **args):
        return self.service.call('wait_for_task', dict(task_id=ident, **args))

    def pause_scheduler(self):
        self.service.stopping.set()
        self.service.thread.join(2)
        self.service.stopping.clear()

    def test_all_six_terminal_states_return_early_with_terminal_event(self):
        self.pause_scheduler()
        for state in ('succeeded', 'failed', 'timed_out', 'cancelled',
                      'interrupted', 'delivery_unknown'):
            with self.subTest(state=state):
                task = self.task(state)
                with ThreadPoolExecutor(1) as pool:
                    started = time.monotonic()
                    future = pool.submit(self.wait, task['id'], timeout_seconds=2)
                    time.sleep(.1)
                    self.service.store.finish(task['id'], state, {})
                    result = future.result(1)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(result['state'], state)
                self.assertFalse(result['timed_out'])
                self.assertEqual(result['events'][-1]['data']['state'], state)
                again = self.wait(task['id'], after_seq=result['next_seq'])
                self.assertEqual(again['events'], [])
                self.assertFalse(again['timed_out'])

    def test_timeout_does_not_change_task_or_idempotency(self):
        self.pause_scheduler()
        task = self.task()
        self.service.store.update(task['id'], state='running')
        before = self.service.store.get(task['id'])
        start = time.monotonic()
        response = self.wait(task['id'], timeout_seconds=.2)
        self.assertGreaterEqual(time.monotonic() - start, .19)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(response['state'], 'running')
        self.assertTrue(response['timed_out'])
        self.assertEqual(self.service.store.get(task['id']), before)
        self.assertEqual(self.service.store.by_request('a')['id'], task['id'])
        self.service.store.finish(task['id'], 'succeeded', {'answer': 'later'})
        final = self.wait(task['id'], after_seq=response['next_seq'])
        self.assertEqual(final['events'][-1]['data']['state'], 'succeeded')
        self.assertEqual(self.service.call('get_result', {'task_id': task['id']})['answer'], 'later')

    def test_terminal_pagination_and_lost_response_replay(self):
        self.pause_scheduler()
        task = self.task()
        for n in range(405):
            self.service.store.event(task['id'], 'progress', {'n': n})
        self.service.store.finish(task['id'], 'succeeded', {})
        first = self.wait(task['id'])
        self.assertEqual(len(first['events']), 200)
        self.assertTrue(first['has_more'])
        self.assertEqual(self.wait(task['id']), first)
        events, cursor = [], 0
        while True:
            page = self.wait(task['id'], after_seq=cursor)
            events.extend(page['events'])
            cursor = page['next_seq']
            if not page['has_more']:
                break
        self.assertEqual(len(events), 407)
        self.assertEqual(len({e['seq'] for e in events}), 407)
        self.assertEqual([e['data']['n'] for e in events if e['kind'] == 'progress'], list(range(405)))
        self.assertEqual(events[-1]['data']['state'], 'succeeded')

    def test_bad_arguments_and_unknown_task(self):
        for args in ({'task_id': ''}, {'task_id': 3}, {'task_id': 'x', 'after_seq': -1},
                     {'task_id': 'x', 'after_seq': True}, {'task_id': 'x', 'after_seq': .5},
                     {'task_id': 'x', 'timeout_seconds': -1}, {'task_id': 'x', 'timeout_seconds': 11},
                     {'task_id': 'x', 'timeout_seconds': True}, {'task_id': 'x', 'timeout_seconds': float('nan')},
                     {'task_id': 'x', 'timeout_seconds': float('inf')}, {'task_id': 'x', 'extra': 1}):
            with self.subTest(args=args), self.assertRaises(HubError) as caught:
                self.service.call('wait_for_task', args)
            self.assertEqual(caught.exception.code, 'INVALID_ARGUMENT')
        with self.assertRaises(HubError) as caught:
            self.wait('missing')
        self.assertEqual(caught.exception.code, 'TASK_NOT_FOUND')

    def test_oversized_numeric_arguments_are_validation_errors(self):
        for args in ({'timeout_seconds': 10 ** 400}, {'after_seq': 2 ** 63}):
            with self.subTest(args=args):
                with self.assertRaises(HubError) as caught:
                    self.wait('missing', **args)
                self.assertEqual(caught.exception.code, 'INVALID_ARGUMENT')

    def test_zero_wait_returns_queued_snapshot_immediately(self):
        self.pause_scheduler()
        task = self.task()
        start = time.monotonic()
        result = self.wait(task['id'], timeout_seconds=0)
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(result['state'], 'queued')
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['events'][0]['data']['state'], 'queued')

    def test_shutdown_releases_waiter(self):
        self.pause_scheduler()
        task = self.task()
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.wait, task['id'])
            time.sleep(.1)
            self.service.close()
            with self.assertRaises(HubError) as caught:
                future.result(1)
        self.assertEqual(caught.exception.code, 'SERVICE_STOPPING')

    def test_terminal_state_and_event_rollback_together(self):
        self.pause_scheduler()
        task = self.task()
        with self.service.store.connection() as db:
            db.execute("CREATE TRIGGER reject_finish BEFORE INSERT ON events "
                       "WHEN NEW.data LIKE '%succeeded%' BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.store.finish(task['id'], 'succeeded', {})
        self.assertEqual(self.service.store.get(task['id'])['state'], 'queued')

    def test_http_wait_allows_submit_query_and_cancel_from_other_clients(self):
        server = make_server(self.service, 'test-private-value')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        (self.root / 'connection.json').write_text(json.dumps({
            'url': 'http://127.0.0.1:' + str(server.server_port), 'token': 'test-private-value'}))
        client = Client(self.root)
        task = client.call('submit_task', dict(request_id='wait-http', agent='fake', prompt='wait',
            cwd=str(self.root), mode='read_only'))
        try:
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(Client(self.root).call, 'wait_for_task', {'task_id': task['id']})
                time.sleep(.15)
                self.assertFalse(future.done())
                start = time.monotonic()
                current = client.call('get_task', {'task_id': task['id']})
                other = client.call('submit_task', dict(request_id='other-http', agent='fake',
                    prompt='hello', cwd=str(self.root), mode='read_only'))
                client.call('cancel_task', {'task_id': task['id']})
                self.assertLess(time.monotonic() - start, 1)
                self.assertEqual(current['state'], 'running')
                self.assertNotEqual(other['id'], task['id'])
                self.assertEqual(future.result(2)['state'], 'cancelled')
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
