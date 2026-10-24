import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent_hub.models import TERMINAL
from agent_hub.service import Service
from agent_hub.store import Store
from agent_hub.workspaces import Workspaces, MAX_ARTIFACT_BYTES


class StoreListFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'tasks.db')
        self.spec = dict(agent='fake', prompt='x', cwd=self.tmp.name,
                         mode='read_only', timeout_seconds=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_filters_in_sql_and_keeps_created_order(self):
        queued = [self.store.create(f'q{i}', self.spec) for i in range(3)]
        done = self.store.create('done', self.spec)
        self.store.finish(done['id'], 'succeeded', {'path': 'n/a'})
        running = self.store.create('run', self.spec)
        self.store.update(running['id'], state='running')
        with self.store.connection() as db:
            indexes = {row['name'] for row in db.execute("PRAGMA index_list('tasks')")}
            self.assertIn('tasks_state', indexes)
            # Prove filtering happens in SQL: full table has more rows than queued query.
            total = db.execute('SELECT COUNT(*) AS n FROM tasks').fetchone()['n']
            queued_sql = db.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE state IN ('queued')").fetchone()['n']
        self.assertEqual(total, 5)
        self.assertEqual(queued_sql, 3)
        listed = self.store.list({'queued'})
        self.assertEqual([t['id'] for t in listed], [t['id'] for t in queued])
        self.assertEqual([t['state'] for t in listed], ['queued'] * 3)
        terminal = self.store.list(TERMINAL)
        self.assertEqual([t['id'] for t in terminal], [done['id']])
        self.assertEqual(self.store.list(set()), [])

    def test_list_quarantined_uses_json_extract(self):
        clean = self.store.create('clean', self.spec)
        dirty = self.store.create('dirty', dict(self.spec, workspace=self.tmp.name))
        self.store.update(dirty['id'], workspace_quarantined=True, workspace=self.tmp.name)
        self.store.finish(dirty['id'], 'interrupted', {'error': 'x'})
        found = self.store.list_quarantined()
        self.assertEqual([t['id'] for t in found], [dirty['id']])
        self.assertTrue(found[0]['workspace_quarantined'])
        self.assertNotIn(clean['id'], [t['id'] for t in found])


class SlowDiagnose:
    capabilities = ['read_only']
    remote_cancel = False

    def __init__(self, delay, started, released):
        self.delay = delay
        self.started = started
        self.released = released
        self.calls = 0

    def diagnose(self):
        self.calls += 1
        self.started.set()
        self.released.wait(timeout=5)
        time.sleep(self.delay)
        return {'available': True, 'capabilities': list(self.capabilities)}

    def permission_policy(self, mode):
        return {'enforced_by': 'test', 'enforced': True, 'allows_write': False}

    def run(self, task, cancel, event):
        return {'answer': 'ok'}


class FastFake:
    capabilities = ['read_only']
    remote_cancel = False
    calls = 0

    def diagnose(self):
        return {'available': True}

    def permission_policy(self, mode):
        return {'enforced_by': 'test', 'enforced': True, 'allows_write': False}

    def run(self, task, cancel, event):
        self.calls += 1
        return {'answer': 'ok'}


class ListAgentsLockTests(unittest.TestCase):
    def test_list_agents_diagnose_does_not_block_other_calls(self):
        root = Path(tempfile.mkdtemp())
        started = threading.Event()
        released = threading.Event()
        slow = SlowDiagnose(0.01, started, released)
        fast = FastFake()
        service = Service(root / "data", {"slow": slow, "fast": fast}, concurrency=1)
        try:
            task = service.store.create(
                "probe",
                dict(agent="fast", prompt="x", cwd=str(root), mode="read_only", timeout_seconds=10),
            )
            results = {}

            def list_agents():
                results["agents"] = service.call("list_agents", {})

            def get_task_while_diagnose_runs():
                started.wait(timeout=5)
                begin = time.monotonic()
                results["task"] = service.call("get_task", {"task_id": task["id"]})
                results["task_wait"] = time.monotonic() - begin

            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(list_agents)
                second = pool.submit(get_task_while_diagnose_runs)
                self.assertTrue(started.wait(timeout=5))
                deadline = time.monotonic() + 2
                while "task" not in results and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIn("task", results)
                self.assertLess(results["task_wait"], 0.5)
                released.set()
                first.result(timeout=5)
                second.result(timeout=5)
            self.assertIn("slow", results["agents"])
            self.assertTrue(results["agents"]["slow"]["available"])
            self.assertEqual(results["task"]["id"], task["id"])
            self.assertEqual(slow.calls, 1)
        finally:
            service.close()


class ScheduleQuarantineTests(unittest.TestCase):
    def test_schedule_does_not_full_scan_per_queued_task(self):
        root = Path(tempfile.mkdtemp())
        fake = FastFake()
        service = Service(root / 'data', {'fake': fake}, concurrency=1)
        try:
            # Seed one quarantined workspace and several queued tasks.
            q = service.store.create('q0', dict(agent='fake', prompt='x', cwd=str(root),
                                                mode='read_only', timeout_seconds=10,
                                                workspace=str(root), workspace_quarantined=True))
            service.store.finish(q['id'], 'interrupted', {'error': 'old'})
            for i in range(5):
                service.store.create(f'queued{i}', dict(agent='fake', prompt='x', cwd=str(root / 'other'),
                                                        mode='read_only', timeout_seconds=10))
            calls = []
            original_list = service.store.list
            original_q = service.store.list_quarantined

            def counting_list(states=None):
                calls.append(('list', None if states is None else frozenset(states)))
                return original_list(states)

            def counting_q():
                calls.append(('quarantined', None))
                return original_q()

            service.store.list = counting_list
            service.store.list_quarantined = counting_q
            # Drive one scheduling pass by waiting briefly for the daemon thread.
            time.sleep(0.2)
            full_scans = [c for c in calls if c == ('list', None)]
            self.assertEqual(full_scans, [], msg=f'unexpected full-table list calls: {calls}')
            self.assertTrue(any(c[0] == 'quarantined' for c in calls), calls)
            queued_filters = [c for c in calls if c[0] == 'list' and c[1] == frozenset({'queued'})]
            self.assertGreaterEqual(len(queued_filters), 1)
        finally:
            service.close()


class WorkspacesArtifactBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self._git('init')
        self._git('config', 'user.email', 'test@example.com')
        self._git('config', 'user.name', 'test')
        (self.repo / 'tracked.txt').write_text('base\n', encoding='utf-8')
        self._git('add', 'tracked.txt')
        self._git('commit', '-m', 'init')
        self.baseline = self._git('rev-parse', 'HEAD').strip()

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        import subprocess
        result = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True,
                                encoding='utf-8', errors='replace', check=True)
        return result.stdout

    def test_small_untracked_remains_compatible(self):
        ws = Workspaces(self.root / 'data')
        prepared = ws.prepare(dict(id='t1', cwd=str(self.repo), mode='edit_worktree'))
        target = Path(prepared['workspace'])
        (target / 'note.txt').write_text('hello budget\n', encoding='utf-8')
        artifacts = ws.artifacts(dict(prepared, mode='edit_worktree'))
        self.assertIn('hello budget', artifacts['diff'])
        self.assertEqual(artifacts['artifact_budget_bytes'], MAX_ARTIFACT_BYTES)
        self.assertNotIn('artifact_truncated', artifacts)

    def test_budget_marks_truncation_without_partial_diff(self):
        ws = Workspaces(self.root / 'data', max_artifact_bytes=200)
        prepared = ws.prepare(dict(id='t2', cwd=str(self.repo), mode='edit_worktree'))
        target = Path(prepared['workspace'])
        (target / 'tracked.txt').write_text('base\n' + ('x' * 500) + '\n', encoding='utf-8')
        artifacts = ws.artifacts(dict(prepared, mode='edit_worktree'))
        self.assertTrue(artifacts.get('artifact_truncated'))
        self.assertEqual(artifacts['diff'], '')
        self.assertTrue(any(item.get('reason') == 'diff_budget_exceeded'
                            for item in artifacts['artifact_omitted']))
        self.assertTrue(any(item.get('path') == 'tracked.txt'
                            for item in artifacts['artifact_omitted']))

    def test_untracked_budget_lists_omitted_files(self):
        ws = Workspaces(self.root / 'data', max_artifact_bytes=80)
        prepared = ws.prepare(dict(id='t3', cwd=str(self.repo), mode='edit_worktree'))
        target = Path(prepared['workspace'])
        (target / 'big.txt').write_text('y' * 200, encoding='utf-8')
        artifacts = ws.artifacts(dict(prepared, mode='edit_worktree'))
        self.assertTrue(artifacts.get('artifact_truncated'))
        omitted = {item['path']: item for item in artifacts['artifact_omitted']}
        self.assertIn('big.txt', omitted)
        self.assertEqual(omitted['big.txt']['reason'], 'budget_exceeded')

    def test_skips_symlink_and_binary_with_markers(self):
        ws = Workspaces(self.root / 'data')
        prepared = ws.prepare(dict(id='t4', cwd=str(self.repo), mode='edit_worktree'))
        target = Path(prepared['workspace'])
        (target / 'bin.dat').write_bytes(b'hello\x00world')
        link = target / 'link.txt'
        try:
            link.symlink_to(target / 'tracked.txt')
            linked = True
        except OSError:
            linked = False
        artifacts = ws.artifacts(dict(prepared, mode='edit_worktree'))
        omitted = {item['path']: item['reason'] for item in artifacts.get('artifact_omitted', [])}
        self.assertEqual(omitted.get('bin.dat'), 'binary')
        if linked:
            self.assertEqual(omitted.get('link.txt'), 'symlink')
        self.assertTrue(artifacts.get('artifact_truncated'))


if __name__ == '__main__':
    unittest.main()
