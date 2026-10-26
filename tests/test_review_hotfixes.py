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

    def test_list_quarantined_uses_indexed_column(self):
        clean = self.store.create('clean', self.spec)
        dirty = self.store.create('dirty', dict(self.spec, workspace=self.tmp.name))
        self.store.update(dirty['id'], workspace_quarantined=True, workspace=self.tmp.name)
        self.store.finish(dirty['id'], 'interrupted', {'error': 'x'})
        found = self.store.list_quarantined()
        self.assertEqual([t['id'] for t in found], [dirty['id']])
        self.assertTrue(found[0]['workspace_quarantined'])
        self.assertNotIn(clean['id'], [t['id'] for t in found])
        with self.store.connection() as db:
            indexes = {row['name'] for row in db.execute("PRAGMA index_list('tasks')")}
            self.assertIn('tasks_quarantined', indexes)
            row = db.execute(
                'SELECT workspace_quarantined FROM tasks WHERE id=?', (dirty['id'],)
            ).fetchone()
            self.assertEqual(row['workspace_quarantined'], 1)


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
            calls = []
            pass_observed = threading.Event()
            original_list = service.store.list
            original_q = service.store.list_quarantined

            def counting_list(states=None):
                calls.append(('list', None if states is None else frozenset(states)))
                return original_list(states)

            def counting_q():
                calls.append(('quarantined', None))
                pass_observed.set()
                return original_q()

            # Seed and instrument atomically: an in-flight pass must not call
            # the original list() and then the newly installed quarantine spy.
            with service.lock:
                q = service.store.create('q0', dict(agent='fake', prompt='x', cwd=str(root),
                                                    mode='read_only', timeout_seconds=10,
                                                    workspace=str(root), workspace_quarantined=True))
                service.store.finish(q['id'], 'interrupted', {'error': 'old'})
                for i in range(5):
                    service.store.create(f'queued{i}', dict(agent='fake', prompt='x', cwd=str(root / 'other'),
                                                            mode='read_only', timeout_seconds=10))
                service.store.list = counting_list
                service.store.list_quarantined = counting_q
            # Direct Store writes use the bounded recovery poll, not a fixed
            # 50ms scheduling cadence. Observe a pass instead of sleeping .2s.
            self.assertTrue(pass_observed.wait(2))
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




class BoundedDiffHangRegressionTests(unittest.TestCase):
    """P1: overflow must kill/reap before blocking stderr EOF; large stdout must not hang."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "test")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "init")
        self.baseline = self._git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, encoding="utf-8", errors="replace", check=True)
        return result.stdout

    def test_large_diff_overflow_returns_without_hang(self):
        ws = Workspaces(self.root / "data", max_artifact_bytes=1024)
        prepared = ws.prepare(dict(id="hang", cwd=str(self.repo), mode="edit_worktree"))
        target = Path(prepared["workspace"])
        (target / "tracked.txt").write_text("base\n" + ("A" * 2_000_000) + "\n", encoding="utf-8")
        box = {}

        def run():
            try:
                box["artifacts"] = ws.artifacts(dict(prepared, mode="edit_worktree"))
            except Exception as error:
                box["error"] = error

        worker = threading.Thread(target=run, daemon=True)
        started = time.monotonic()
        worker.start()
        worker.join(timeout=5)
        elapsed = time.monotonic() - started
        self.assertFalse(worker.is_alive(), f"artifacts hung for {elapsed:.2f}s")
        self.assertNotIn("error", box, box.get("error"))
        artifacts = box["artifacts"]
        self.assertTrue(artifacts.get("artifact_truncated"))
        self.assertEqual(artifacts["diff"], "")
        self.assertTrue(any(item.get("reason") == "diff_budget_exceeded"
                            for item in artifacts["artifact_omitted"]))


class Utf8ChunkBoundaryTests(unittest.TestCase):
    """P2: UTF-8 codepoints split across 64KiB chunks must decode exactly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "test")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "init")
        self.baseline = self._git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, encoding="utf-8", errors="replace", check=True)
        return result.stdout

    def test_utf8_diff_exact_across_64kib_boundary(self):
        import subprocess
        han = chr(0x4e00) * 50_000
        ws = Workspaces(self.root / "data", max_artifact_bytes=2 * 1024 * 1024)
        prepared = ws.prepare(dict(id="utf8", cwd=str(self.repo), mode="edit_worktree"))
        target = Path(prepared["workspace"])
        (target / "tracked.txt").write_text("base\n" + han + "\n", encoding="utf-8")
        expected = subprocess.run(
            ["git", "-C", str(target), "diff", prepared["baseline"], "--"],
            capture_output=True, check=True).stdout.decode("utf-8")
        self.assertGreater(len(expected.encode("utf-8")), 64 * 1024)
        artifacts = ws.artifacts(dict(prepared, mode="edit_worktree"))
        self.assertFalse(artifacts.get("artifact_truncated", False))
        self.assertNotIn("\ufffd", artifacts["diff"])
        self.assertEqual(artifacts["diff"], expected)



class StuckFake:
    capabilities = ['read_only']
    remote_cancel = False

    def __init__(self, started, release):
        self.started = started
        self.release = release

    def diagnose(self):
        return {'available': True}

    def permission_policy(self, mode):
        return {'enforced_by': 'test', 'enforced': True, 'allows_write': False}

    def run(self, task, cancel, event):
        self.started.set()
        # Stay running so concurrency stays full until the test releases.
        self.release.wait(timeout=30)
        return {'answer': 'ok'}


class QuarantineIndexAndScheduleSkipTests(unittest.TestCase):
    """P2: indexed quarantine column; schedule skips lookup when idle or concurrency full."""

    def test_quarantine_column_indexed_and_schedule_skips_when_idle(self):
        root = Path(tempfile.mkdtemp())
        started = threading.Event()
        release = threading.Event()
        stuck = StuckFake(started, release)
        service = Service(root / 'data', {'fake': stuck}, concurrency=1)
        try:
            with service.store.connection() as db:
                cols = {row['name'] for row in db.execute("PRAGMA table_info(tasks)")}
                self.assertIn('workspace_quarantined', cols)
                indexes = {row['name'] for row in db.execute("PRAGMA index_list('tasks')")}
                self.assertIn('tasks_quarantined', indexes)
                plan = db.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM tasks WHERE workspace_quarantined = 1"
                ).fetchall()
                plan_text = ' '.join(str(dict(row)) for row in plan).upper()
                self.assertTrue('INDEX' in plan_text or 'TASKS_QUARANTINED' in plan_text, plan_text)

            calls = {'n': 0}
            original_q = service.store.list_quarantined

            def counting_q():
                calls['n'] += 1
                return original_q()

            service.store.list_quarantined = counting_q
            before = calls['n']
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                self.assertEqual(calls['n'], before, 'list_quarantined called while queue empty')
                time.sleep(0.05)

            # Fill concurrency with a really running worker (not a fake active entry).
            (root / 'b').mkdir(parents=True, exist_ok=True)
            for i in range(3):
                (root / f'q{i}').mkdir(parents=True, exist_ok=True)
            service.store.create(
                'blocker', dict(agent='fake', prompt='x', cwd=str(root / 'b'),
                                mode='read_only', timeout_seconds=30))
            self.assertTrue(started.wait(timeout=5), 'stuck worker did not start')
            for i in range(3):
                service.store.create(
                    f'queued{i}', dict(agent='fake', prompt='x', cwd=str(root / f'q{i}'),
                                       mode='read_only', timeout_seconds=10))
            before = calls['n']
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                with service.lock:
                    self.assertGreaterEqual(len(service.active), 1)
                self.assertEqual(calls['n'], before, 'list_quarantined called while concurrency full')
                time.sleep(0.05)

            release.set()
            # Wait until active drains so queued can be considered; quarantine may then be called.
            deadline = time.monotonic() + 3
            while calls['n'] == before and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertGreater(calls['n'], before, 'list_quarantined not called with queued+room')

            dirty = service.store.create(
                'dirty', dict(agent='fake', prompt='x', cwd=str(root),
                              mode='read_only', timeout_seconds=10, workspace=str(root)))
            service.store.update(dirty['id'], workspace_quarantined=True, workspace=str(root))
            service.store.finish(dirty['id'], 'interrupted', {'error': 'x'})
            found = original_q()
            self.assertEqual([t['id'] for t in found], [dirty['id']])
            with service.store.connection() as db:
                row = db.execute(
                    'SELECT workspace_quarantined FROM tasks WHERE id=?', (dirty['id'],)
                ).fetchone()
                self.assertEqual(row['workspace_quarantined'], 1)
        finally:
            release.set()
            service.close()


class UntrackedBoundedReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "test")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def _git(self, *args):
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True, encoding="utf-8", errors="replace", check=True)
        return result.stdout

    def test_untracked_bounded_read_growing_file(self):
        """After stat says file fits, a grown file must not be absorbed unbounded."""
        import io
        ws = Workspaces(self.root / "data", max_artifact_bytes=10_000)
        prepared = ws.prepare(dict(id="grow", cwd=str(self.repo), mode="edit_worktree"))
        target = Path(prepared["workspace"])
        grow = target / "grow.txt"
        grow.write_text("small\n", encoding="utf-8")
        real_open = Path.open

        def fake_open(self, *a, **k):
            if self.name == "grow.txt" and (not a or a[0] == "rb" or k.get("mode") == "rb"):
                # Stat already saw small size; return a large payload on read.
                return io.BytesIO(("Z" * 50_000).encode("utf-8"))
            return real_open(self, *a, **k)

        with patch.object(Path, "open", fake_open):
            artifacts = ws.artifacts(dict(prepared, mode="edit_worktree"))
        self.assertTrue(artifacts.get("artifact_truncated"))
        omitted = {item["path"]: item["reason"] for item in artifacts.get("artifact_omitted", [])}
        self.assertEqual(omitted.get("grow.txt"), "budget_exceeded")
        self.assertLess(len(artifacts.get("diff", "")), 40_000)



if __name__ == '__main__':
    unittest.main()
