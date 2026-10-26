"""Regression coverage for result publication and bounded dashboard reads."""
import json
import tempfile
import threading
import time
import unittest
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub.dashboard import Dashboard
from agent_hub.dashboard_chains import DashboardChains
from agent_hub.service import Service
from agent_hub.models import HubError
from agent_hub.store import Store
from agent_hub.verification import Verification
from agent_hub.work_items import WorkItems
from test_service import Fake


class ResultPublicationTests(unittest.TestCase):
    def test_published_terminal_result_has_already_released_its_slot(self):
        with tempfile.TemporaryDirectory() as td:
            service = Service(Path(td) / 'data', {'fake': Fake()})
            published, release = threading.Event(), threading.Event()
            finish = service._finish_execution

            def pause_after_publication(*args):
                finish(*args)
                published.set()
                release.wait(4)

            try:
                with patch.object(service, '_finish_execution', pause_after_publication):
                    task = service.submit('published', 'fake', 'hello', td, 'read_only')
                    self.assertTrue(published.wait(4))
                    self.assertTrue(service.call('get_result', {'task_id': task['id']})['ready'])
                    self.assertNotIn(task['id'], service.active)
            finally:
                release.set()
                service.close()

    def test_postprocessing_failure_preserves_remote_uncertainty_and_blocks_resume(self):
        class RemoteUnknown(Fake):
            cancel_scope = 'remote_session'

            def run(self, task, cancel, event):
                raise HubError('REMOTE_STATE_UNKNOWN', 'remote outcome unknown', {'remote_cancelled': False})

        with tempfile.TemporaryDirectory() as td:
            service = Service(Path(td) / 'data', {'fake': RemoteUnknown()}, per_agent_concurrency={'fake': 0})
            try:
                task = service.submit('unknown', 'fake', 'work', td, 'edit_worktree')
                service.store.update(task['id'], state='running', workspace=td, session_id='existing-session')
                task = service.store.get(task['id'])
                with self.assertLogs('agent_hub.service', level='ERROR'), patch.object(
                        service.workspaces, 'artifacts', side_effect=OSError('synthetic collection failure')):
                    service.execute(task, threading.Event())
                final = service.store.get(task['id'])
                self.assertEqual(final['state'], 'interrupted')
                self.assertTrue(final.get('workspace_quarantined'))
                result = service.call('get_result', {'task_id': task['id']})
                self.assertFalse(result['remote_cancelled'])
                self.assertEqual(result['error_code'], 'REMOTE_STATE_UNKNOWN')
                with self.assertRaises(HubError):
                    service.call('continue_task', dict(request_id='unsafe-resume', task_id=task['id'], prompt='next'))
            finally:
                service.close()

    def test_write_and_replace_failures_release_slot_and_allow_followup(self):
        for failure in ('write', 'replace'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                service = Service(Path(td) / 'data', {'fake': Fake()}, concurrency=1)
                self.addCleanup(service.close)
                original_write, original_replace = Path.write_text, __import__('os').replace

                def write(path, *args, **kwargs):
                    if path.parent.name == 'results':
                        raise OSError('synthetic full disk')
                    return original_write(path, *args, **kwargs)

                def replace(source, target):
                    if Path(target).parent.name == 'results':
                        raise OSError('synthetic replace failure')
                    return original_replace(source, target)

                target, replacement = ('pathlib.Path.write_text', write) if failure == 'write' else ('os.replace', replace)
                try:
                    with patch(target, replacement), patch.object(threading, 'excepthook'):
                        first = service.submit('first', 'fake', 'hello', td, 'read_only')
                        deadline = time.monotonic() + 4
                        while time.monotonic() < deadline:
                            current = service.store.get(first['id'])
                            if current['state'] == 'failed' and first['id'] not in service.active:
                                break
                            time.sleep(.02)
                        self.assertEqual(current['state'], 'failed')
                        self.assertNotIn(first['id'], service.active)
                        result = service.call('get_result', {'task_id': first['id']})
                        self.assertEqual(result['error_code'], 'RESULT_PERSISTENCE_ERROR')
                        self.assertNotIn('path', current['result'])
                    second = service.submit('second', 'fake', 'next', td, 'read_only')
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        following = service.store.get(second['id'])
                        if following['state'] == 'succeeded':
                            break
                        time.sleep(.02)
                    self.assertEqual(following['state'], 'succeeded')
                    self.assertEqual(list((service.root / 'results').glob('*.tmp')), [])
                finally:
                    service.close()


class DashboardReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.verification = Verification(self.store, None)
        self.items = WorkItems(self.store)
        self.service = SimpleNamespace(store=self.store, verification=self.verification, adapters={})
        self.service.call = lambda tool, args: self.store.get(args['task_id']).get('result', {})
        self.dashboard = Dashboard(self.service)
        self.chains = DashboardChains(self.dashboard)

    def seed(self, count, title='Visible container', agent='coder'):
        item = self.items.create('item-' + str(count) + title, title=title)
        with self.store.connection() as db:
            for i in range(count):
                ident = item['work_item_id'] + str(i)
                data = json.dumps(dict(agent=agent, prompt='ordinary prompt', mode='read_only', result={'answer': 'done'}))
                db.execute('INSERT INTO tasks(id,request_id,digest,state,data,created,updated) VALUES (?,?,?,?,?,?,?)',
                           (ident, ident, 'd', 'succeeded', data, i, i))
                db.execute('INSERT INTO work_item_members VALUES (?,?,?,?,?,?)',
                           (item['work_item_id'], ident, 'development', None, i, ident))
        return item['work_item_id']

    def test_chain_detail_uses_bounded_batch_queries(self):
        wid = self.seed(50)
        queries = []
        original = self.store.connection

        @contextmanager
        def counted():
            with original() as db:
                db.set_trace_callback(lambda sql: queries.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
                yield db

        with patch.object(self.store, 'connection', counted):
            detail = self.chains.detail(wid)
        self.assertEqual(len(detail['nodes']), 50)
        self.assertEqual(sum(m['text'] == 'done' for m in detail['messages']), 50)
        self.assertLessEqual(len(queries), 16, 'chain reads must not grow by several SELECTs per node')

    def test_chain_projection_releases_history_before_global_trimming(self):
        wid = self.seed(10)
        payload = json.dumps({'message': 'x' * 10000, 'role': 'assistant'})
        with self.store.connection() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM tasks')]
            db.executemany('INSERT INTO events(task_id,kind,data,timestamp) VALUES (?,?,?,?)',
                           [(ident, 'message', payload, i) for ident in ids for i in range(50)])
        tracemalloc.start()
        try:
            detail = self.chains.detail(wid)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLessEqual(sum(len(m['text']) for m in detail['messages']), 1000000)
        self.assertLess(peak, 12 * 1024 * 1024, 'bounded output must not retain all raw and projected histories')

    def test_visible_title_is_searchable_literal_and_respects_member_filters(self):
        wid = self.seed(2, 'Release 100%_done')
        self.seed(1, 'Release 100XXdone', agent='reviewer')
        found = self.chains.chains(q='100%_done', state='succeeded', agent='coder')['items']
        self.assertEqual([item['id'] for item in found], [wid])
        self.assertEqual(self.chains.chains(q='100%_done', agent='reviewer')['items'], [])

    def test_work_item_title_is_redacted_before_returning_to_browser(self):
        wid = self.seed(1, 'Release api_key=DEMO_SECRET C:\\private\\source.txt')
        title = self.chains.detail(wid)['chain']['title']
        self.assertNotIn('DEMO_SECRET', title)
        self.assertNotIn('private', title)
        self.assertIn('[REDACTED]', title)


class ReconcileFixtureTests(unittest.TestCase):
    def test_interrupted_history_fixture_cannot_dispatch_while_being_seeded(self):
        from test_work_items import ReconcileTaskTests
        fixture = ReconcileTaskTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        bot = fixture._install_history_bot([])
        create = fixture.service.store.create

        def slow_create(*args, **kwargs):
            task = create(*args, **kwargs)
            time.sleep(.2)  # Give the real scheduler a chance to see the queued row.
            return task

        with patch.object(fixture.service.store, 'create', slow_create):
            task = fixture._interrupted_grok_like('fixture-race')
        self.assertEqual(task['state'], 'interrupted')
        self.assertEqual(bot.calls, [], 'seeding history must not launch a live executor')


if __name__ == '__main__':
    unittest.main()
