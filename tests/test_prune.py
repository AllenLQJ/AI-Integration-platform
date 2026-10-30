import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from agent_hub import prune
from agent_hub.models import HubError
from agent_hub.store import Store

DAY = 86400


def git(cwd, *args):
    subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True)


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'data'
        self.root.mkdir()
        self.store = Store(self.root / 'tasks.db')
        self.addCleanup(self.tmp.cleanup)

    def task(self, name, state='succeeded', age_days=30, **spec):
        spec = dict(dict(agent='fake', prompt=name, cwd=self.tmp.name, mode='read_only', timeout_seconds=10), **spec)
        ident = self.store.create(name, spec)['id']
        if state != 'queued':
            self.store.finish(ident, state, {})
        with self.store.connection(write=True) as db:
            db.execute('UPDATE tasks SET updated=? WHERE id=?', (time.time() - age_days * DAY, ident))
        return ident

    def files(self, ident):
        (self.root / 'results').mkdir(exist_ok=True)
        result = self.root / 'results' / (ident + '.json')
        result.write_text('{"answer": "x"}')
        log = self.root / 'logs' / ident
        log.mkdir(parents=True)
        (log / 'stdout.log').write_text('output')
        self.store.event(ident, 'message', {'text': 'big payload'})
        self.store.event(ident, 'tool', {'name': 'edit'})
        return result, log

    def kinds(self, ident):
        with self.store.connection() as db:
            return sorted(row['kind'] for row in db.execute('SELECT kind FROM events WHERE task_id=?', (ident,)))

    def prune(self, apply=False, days=7):
        return prune.run(self.root, days, apply_changes=apply)

    def test_dry_run_reports_without_changing_anything(self):
        ident = self.task('old')
        result, log = self.files(ident)
        before = self.kinds(ident)
        report = self.prune()
        self.assertTrue(report['dry_run'])
        self.assertEqual([g['task_ids'] for g in report['groups']], [[ident]])
        self.assertEqual(report['groups'][0]['events'], 2)
        self.assertGreater(report['total_bytes'], 0)
        self.assertTrue(result.exists() and log.exists())
        self.assertEqual(self.kinds(ident), before)

    def test_apply_removes_bulk_data_but_keeps_the_task_and_its_state_history(self):
        ident = self.task('old')
        result, log = self.files(ident)
        report = self.prune(apply=True)
        self.assertEqual(report['pruned'], [report['groups'][0]['root_task_id']])
        self.assertFalse(result.exists() or log.exists())
        self.assertEqual(self.kinds(ident), ['state', 'state'])
        self.assertEqual(self.store.get(ident)['state'], 'succeeded')
        # A second run finds nothing left to remove.
        self.assertEqual(self.prune(apply=True)['groups'][0]['events'], 0)

    def test_recent_and_unfinished_work_is_left_alone(self):
        recent = self.task('recent', age_days=1)
        running = self.task('running', state='queued')
        parent = self.task('parent')
        child = self.task('child', state='queued', parent_id=parent)
        for ident in (recent, running, child):
            self.files(ident)
        report = self.prune(apply=True)
        self.assertEqual(report['groups'], [])
        self.assertEqual(len(report['skipped']), 3)
        for ident in (recent, running, child):
            self.assertTrue((self.root / 'results' / (ident + '.json')).exists())

    def test_related_tasks_are_pruned_together_or_not_at_all(self):
        parent = self.task('parent')
        review = self.task('review', age_days=1, review_of={'task_id': parent})
        self.files(parent)
        report = self.prune(apply=True)
        self.assertEqual(report['groups'], [])
        self.assertTrue((self.root / 'results' / (parent + '.json')).exists())
        self.assertIn('最近仍有活动', report['skipped'][0]['reason'])
        del review

    def test_unsettled_verification_keeps_the_group(self):
        ident = self.task('awaiting', acceptance={'criteria': [{'id': 'p', 'kind': 'file_exists', 'path': 'out.txt'}]})
        self.files(ident)
        report = self.prune(apply=True)
        self.assertEqual(report['groups'], [])
        self.assertIn('awaiting_verification', report['skipped'][0]['reason'])

    def test_age_must_be_a_positive_number_of_days(self):
        for bad in (0, -1, 1.5, '7'):
            with self.assertRaises(HubError):
                prune.plan(self.store, self.root, bad)

    def test_missing_database_is_reported(self):
        with self.assertRaises(HubError) as error:
            prune.run(self.root / 'elsewhere', 7)
        self.assertEqual(error.exception.code, 'NO_DATA')

    def worktree_task(self, name, dirty):
        repo = Path(self.tmp.name) / 'repo'
        if not repo.exists():
            repo.mkdir()
            git(repo, 'init', '-q')
            git(repo, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base')
        ident = self.task(name, mode='edit_worktree', cwd=str(repo))
        path = self.root / 'worktrees' / ident
        path.parent.mkdir(exist_ok=True)
        git(repo, 'worktree', 'add', '-q', '-b', 'hub/' + ident, str(path))
        with self.store.connection(write=True) as db:
            db.execute("UPDATE tasks SET data=json_set(data,'$.workspace',?) WHERE id=?", (str(path), ident))
        if dirty:
            (path / 'edited.txt').write_text('uncommitted work')
        return ident, repo, path

    def branches(self, repo):
        return subprocess.run(['git', '-C', str(repo), 'branch', '--list', 'hub/*'], capture_output=True,
                              encoding='utf-8').stdout

    def test_clean_worktree_and_branch_are_removed(self):
        ident, repo, path = self.worktree_task('clean', dirty=False)
        result, _ = self.files(ident)
        report = self.prune(apply=True)
        self.assertEqual(report['warnings'], [])
        self.assertFalse(path.exists() or result.exists())
        self.assertEqual(self.branches(repo).strip(), '')

    def test_worktree_with_uncommitted_changes_keeps_the_whole_group(self):
        ident, repo, path = self.worktree_task('dirty', dirty=True)
        result, log = self.files(ident)
        report = self.prune(apply=True)
        self.assertEqual(report['groups'], [])
        self.assertIn('未提交改动', report['skipped'][0]['reason'])
        self.assertTrue((path / 'edited.txt').exists() and result.exists() and log.exists())
        self.assertIn('hub/' + ident, self.branches(repo))
        self.assertIn('tool', self.kinds(ident))


if __name__ == '__main__':
    unittest.main()
