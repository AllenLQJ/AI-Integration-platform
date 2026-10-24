import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_hub.activity import (
    OUTPUT_EXCERPT_BUDGET, normalize_activity, relative_path_display,
)
from agent_hub.models import HubError
from agent_hub.store import Store
from agent_hub.verification import Verification


class NormalizeActivityTests(unittest.TestCase):
    def test_allowed_kinds_and_rejects_unknown(self):
        for kind in ('message', 'file_read', 'command', 'test', 'file_change', 'system'):
            out = normalize_activity({'kind': kind, 'title': 't'}, force_provenance='controller_reported')
            self.assertEqual(out['kind'], kind)
            self.assertEqual(out['provenance'], 'controller_reported')
        with self.assertRaises(HubError) as err:
            normalize_activity({'kind': 'shell', 'title': 'x'}, force_provenance='controller_reported')
        self.assertEqual(err.exception.code, 'INVALID_ARGUMENT')

    def test_bytes_not_codepoints_and_oversized_rejected(self):
        # 3-byte UTF-8 characters: 22000 chars ≈ 66000 bytes > 64KiB.
        huge = {'kind': 'message', 'text': '测' * 22000}
        with self.assertRaises(HubError) as err:
            normalize_activity(huge, force_provenance='controller_reported')
        self.assertEqual(err.exception.code, 'INVALID_ARGUMENT')
        with self.assertRaises(HubError):
            normalize_activity('nope', force_provenance='controller_reported')
        with self.assertRaises(HubError):
            normalize_activity({'kind': 'message', 'mystery': 1}, force_provenance='controller_reported')

    def test_caller_cannot_set_trusted_provenance(self):
        with self.assertRaises(HubError):
            normalize_activity({'kind': 'message', 'text': 'hi', 'provenance': 'executor_observed'})
        forced = normalize_activity({'kind': 'message', 'text': 'hi', 'provenance': 'executor_observed'},
                                    force_provenance='controller_reported')
        self.assertEqual(forced['provenance'], 'controller_reported')

    def test_relative_paths_and_output_budget(self):
        with tempfile.TemporaryDirectory() as td:
            workspace = str(Path(td).resolve())
            inside = Path(workspace, 'src', 'a.py')
            inside.parent.mkdir(parents=True)
            inside.write_text('x', encoding='utf-8')
            meta = relative_path_display(str(inside), workspace)
            self.assertEqual(meta['path_scope'], 'workspace')
            self.assertEqual(meta['path'], 'src/a.py')
            outside = relative_path_display(r'C:\Users\private\secret.env', workspace)
            self.assertEqual(outside['path_scope'], 'outside')
            self.assertEqual(outside['path'], '')
            big = 'x' * (OUTPUT_EXCERPT_BUDGET + 100)
            out = normalize_activity({
                'kind': 'command',
                'operation': {
                    'command_display': 'pytest -q',
                    'stdout_excerpt': big,
                    'stderr_excerpt': 'err',
                    'exit_code': 0,
                    'path': str(inside),
                },
            }, workspace, force_provenance='controller_reported')
            def op_budget(operation):
                total = 0
                for value in operation.values():
                    if isinstance(value, str):
                        total += len(value.encode('utf-8'))
                    elif isinstance(value, dict):
                        total += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
                    else:
                        total += len(str(value).encode('utf-8'))
                return total

            self.assertLessEqual(op_budget(out['operation']), OUTPUT_EXCERPT_BUDGET)
            self.assertIn('operation.stdout_excerpt', out.get('truncated_fields', []))
            self.assertEqual(out['operation']['path'], 'src/a.py')
            # Shared budget: huge Unicode metadata + excerpts still fit ≤16KiB together.
            huge_meta = '测' * 2500
            combo = normalize_activity({
                'kind': 'command',
                'operation': {
                    'command_display': huge_meta,
                    'test_summary': huge_meta,
                    'change_summary': huge_meta,
                    'stdout_excerpt': huge_meta,
                    'stderr_excerpt': huge_meta,
                    'exit_code': 1,
                    'duration_ms': 12,
                    'path': str(inside),
                },
            }, workspace, force_provenance='controller_reported')
            self.assertLessEqual(op_budget(combo['operation']), OUTPUT_EXCERPT_BUDGET)
            self.assertTrue(combo.get('truncated_fields'))
            # Every retained operation field (not only stdout/stderr) shares the UTF-8 budget.
            self.assertTrue(any(
                field.startswith('operation.') for field in combo.get('truncated_fields', [])))
            # Provider IDs / revisions stay outside operation budget and remain intact.
            kept = normalize_activity({
                'kind': 'command',
                'provider_message_id': 'prov-keep-1',
                'provider_revision': 7,
                'operation': {
                    'command_display': '测' * 5000,
                    'stdout_excerpt': '测' * 5000,
                    'stderr_excerpt': '测' * 5000,
                    'cwd_display': '测' * 200,
                    'tool_name': 'shell',
                    'exit_code': 0,
                    'duration_ms': 3,
                    'path': str(inside),
                },
            }, workspace, force_provenance='controller_reported')
            self.assertEqual(kept['provider_message_id'], 'prov-keep-1')
            self.assertEqual(kept['provider_revision'], 7)
            self.assertLessEqual(op_budget(kept['operation']), OUTPUT_EXCERPT_BUDGET)
            self.assertTrue(json.dumps(kept, ensure_ascii=False, allow_nan=False))


class RecordActivityStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.task = self.store.create('req-1', dict(
            prompt='p', agent='coder', mode='read_only', cwd=self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _activity(self, text='hello'):
        return normalize_activity({'kind': 'message', 'text': text, 'actor': 'controller'},
                                  force_provenance='controller_reported')

    def test_missing_task_rejected(self):
        with self.assertRaises(HubError) as err:
            self.store.record_activity('r1', 'missing', 'e1', self._activity())
        self.assertEqual(err.exception.code, 'TASK_NOT_FOUND')

    def test_identical_requests_append_once(self):
        activity = self._activity()
        first = self.store.record_activity('same', self.task['id'], 'evt-1', activity)
        second = self.store.record_activity('same', self.task['id'], 'evt-1', activity)
        self.assertEqual(first, second)
        with self.store.connection() as db:
            count = db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND kind='activity'",
                (self.task['id'],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_changed_content_or_event_identity_conflicts(self):
        activity = self._activity('one')
        self.store.record_activity('k', self.task['id'], 'evt-1', activity)
        with self.assertRaises(HubError) as err:
            self.store.record_activity('k', self.task['id'], 'evt-1', self._activity('two'))
        self.assertEqual(err.exception.code, 'REQUEST_CONFLICT')
        with self.assertRaises(HubError) as err:
            self.store.record_activity('k', self.task['id'], 'evt-2', activity)
        self.assertEqual(err.exception.code, 'REQUEST_CONFLICT')

    def test_concurrent_identical_requests_append_once(self):
        activity = self._activity('race')
        results, errors = [], []

        def worker():
            try:
                results.append(self.store.record_activity('race', self.task['id'], 'e-race', activity))
            except Exception as error:  # noqa: BLE001 - collect for assertion
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(len({json.dumps(item, sort_keys=True) for item in results}), 1)
        with self.store.connection() as db:
            count = db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND kind='activity'",
                (self.task['id'],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_terminal_append_does_not_change_state_updated_or_verification(self):
        self.store.finish(self.task['id'], 'succeeded', {'answer': 'done'})
        before = self.store.get(self.task['id'])
        verification = Verification(self.store, None)
        verification.ensure(before)
        with self.store.connection() as db:
            row = db.execute('SELECT state, data FROM verifications WHERE task_id=?',
                             (self.task['id'],)).fetchone()
            prior_state = row['state'] if row else None
            prior_data = row['data'] if row else None
        self.store.record_activity('late', self.task['id'], 'late-1', self._activity('补记'))
        after = self.store.get(self.task['id'])
        self.assertEqual(after['state'], 'succeeded')
        self.assertEqual(after['updated'], before['updated'])
        self.assertEqual(after.get('result'), before.get('result'))
        with self.store.connection() as db:
            row = db.execute('SELECT state, data FROM verifications WHERE task_id=?',
                             (self.task['id'],)).fetchone()
        if prior_state is None:
            self.assertIsNone(row)
        else:
            self.assertEqual(row['state'], prior_state)
            self.assertEqual(row['data'], prior_data)


class RecordActivityServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from agent_hub.service import Service
        from test_service import Fake
        self.service = Service(self.tmp.name, {'fake': Fake()}, per_agent_concurrency={'fake': 0})
        # LIFO: close service before deleting the temp data directory (Windows file locks).
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)
        from hub_fixtures import with_work_item
        self.task = self.service.call('submit_task', with_work_item(self.service, dict(
            request_id='act-1', agent='fake', prompt='p', cwd=self.tmp.name, mode='read_only')))

    def test_stale_controller_rejected_and_cannot_evade_on_retry(self):
        lease = self.service.call('claim_controller', dict(
            request_id='claim-1', task_id=self.task['id'], controller_id='ctrl'))
        activity = {'kind': 'command', 'operation': {'command_display': 'ls', 'exit_code': 0}}
        args = dict(request_id='act-a', task_id=self.task['id'], event_id='e1', activity=activity,
                    controller_id='wrong', controller_epoch=lease['controller_epoch'])
        with self.assertRaises(HubError) as err:
            self.service.call('record_activity', args)
        self.assertEqual(err.exception.code, 'CONTROLLER_STALE')
        with self.assertRaises(HubError) as err:
            self.service.call('record_activity', args)
        self.assertEqual(err.exception.code, 'CONTROLLER_STALE')
        ok = self.service.call('record_activity', dict(
            request_id='act-b', task_id=self.task['id'], event_id='e2', activity=activity,
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch']))
        self.assertTrue(ok['ok'])
        self.assertEqual(ok, self.service.call('record_activity', dict(
            request_id='act-b', task_id=self.task['id'], event_id='e2', activity=activity,
            controller_id=lease['controller_id'], controller_epoch=lease['controller_epoch'])))

    def test_mcp_declares_record_activity_and_tool_count(self):
        import asyncio
        from agent_hub.mcp_server import mcp
        names = {tool.name for tool in asyncio.run(mcp.list_tools())}
        self.assertIn('record_activity', names)
        self.assertEqual(len(names), 20)
        self.assertIn('create_work_item', names)
        self.assertIn('link_task', names)
        self.assertIn('reconcile_task', names)



class ExecutorActivityEventPathTests(unittest.TestCase):
    """Executor stream activities must share the same 16KiB normalize path as record_activity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from agent_hub.service import Service
        from test_service import Fake
        self.fake = Fake()
        self.service = Service(self.tmp.name, {'fake': self.fake})
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def _op_budget(self, operation):
        total = 0
        for value in operation.values():
            if isinstance(value, str):
                total += len(value.encode('utf-8'))
            elif isinstance(value, dict):
                total += len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            else:
                total += len(str(value).encode('utf-8'))
        return total

    def _wait(self, ident):
        import time
        for _ in range(200):
            task = self.service.call('get_task', {'task_id': ident})
            if task['state'] in {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}:
                return task
            time.sleep(0.03)
        self.fail('task did not finish')

    def test_oversized_executor_activity_is_normalized_before_store(self):
        # processes._bound_excerpts only shares budget across stdout/stderr; command/cwd sit outside.
        # Synthetic payload mirrors the review finding (~20KiB operation) and must be clipped to 16KiB.
        command = 'c' * 5000
        stdout = 'o' * 16000
        activity = {
            'kind': 'command',
            'title': 'Bash',
            'operation_id': 'op-synth-1',
            'phase': 'completed',
            'status': 'completed',
            'provenance': 'controller_reported',  # must be forced to executor_observed
            'source': 'cli_structured',
            'completeness': 'recorded',
            'provider_message_id': 'prov-msg-keep',
            'provider_revision': 9,
            'operation': {
                'tool_name': 'Bash',
                'command_display': command,
                'cwd_display': 'workspace',
                'exit_code': 0,
                'stdout_excerpt': stdout,
                'stderr_excerpt': 'e' * 200,
            },
        }
        self.assertGreater(self._op_budget(activity['operation']), OUTPUT_EXCERPT_BUDGET)

        def run(task, cancel, event):
            event('activity', activity)
            return {'answer': 'ok', 'session_id': 's1'}

        self.fake.run = run
        from hub_fixtures import with_work_item
        task = self.service.call('submit_task', with_work_item(self.service, dict(
            request_id='exec-act-1', agent='fake', prompt='emit', cwd=self.tmp.name, mode='read_only')))
        finished = self._wait(task['id'])
        self.assertEqual(finished['state'], 'succeeded')
        page = self.service.call('get_events', {'task_id': task['id'], 'after_seq': 0})
        activities = [item for item in page['events'] if item['kind'] == 'activity']
        self.assertEqual(len(activities), 1, page['events'])
        stored = activities[0]['data']
        self.assertEqual(stored['provenance'], 'executor_observed')
        self.assertEqual(stored['provider_message_id'], 'prov-msg-keep')
        self.assertEqual(stored['provider_revision'], 9)
        self.assertEqual(stored['operation_id'], 'op-synth-1')
        self.assertLessEqual(self._op_budget(stored['operation']), OUTPUT_EXCERPT_BUDGET)
        self.assertIn('truncated_fields', stored)

    def test_illegal_executor_activity_is_recorded_without_failing_task(self):
        def run(task, cancel, event):
            event('activity', {'kind': 'shell', 'title': 'bad'})
            return {'answer': 'still-ok'}

        self.fake.run = run
        from hub_fixtures import with_work_item
        task = self.service.call('submit_task', with_work_item(self.service, dict(
            request_id='exec-act-bad', agent='fake', prompt='bad-act', cwd=self.tmp.name, mode='read_only')))
        finished = self._wait(task['id'])
        self.assertEqual(finished['state'], 'succeeded')
        page = self.service.call('get_events', {'task_id': task['id'], 'after_seq': 0})
        kinds = [item['kind'] for item in page['events']]
        self.assertNotIn('activity', kinds)
        systems = [item for item in page['events'] if item['kind'] == 'system']
        self.assertTrue(
            any('activity' in json.dumps(item.get('data') or {}, ensure_ascii=False) for item in systems),
            page['events'],
        )



if __name__ == '__main__':
    unittest.main()
