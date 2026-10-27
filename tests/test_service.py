import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from agent_hub.service import Service
from agent_hub.models import HubError
from agent_hub.workspaces import Workspaces


class Fake:
    capabilities = ['read_only', 'edit_worktree', 'continue']
    remote_cancel = True
    def __init__(self):
        self.calls = 0
    def diagnose(self):
        return {'available': True, 'capabilities': self.capabilities}
    def run(self, task, cancel, event):
        self.calls += 1
        event('progress', {'message': 'started'})
        if task['prompt'] == 'wait':
            cancel.wait(30)
        return {'answer': task['prompt'], 'session_id': 'native-123'}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hub 中文 space ')
        self.root = Path(self.tmp.name)
        self.fake = Fake()
        self.service = Service(self.root / 'data', {'fake': self.fake})
    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()
    def submit(self, **kw):
        from hub_fixtures import with_work_item
        args = dict(request_id='a', agent='fake', prompt='hello', cwd=str(self.root), mode='read_only')
        args.update(kw)
        return self.service.call('submit_task', with_work_item(self.service, args))
    def wait(self, ident):
        for _ in range(100):
            task = self.service.call('get_task', {'task_id': ident})
            if task['state'] in {'succeeded', 'cancelled', 'failed', 'timed_out'}:
                return task
            time.sleep(.03)
        self.fail('task did not finish')
    def test_duplicate_and_continue(self):
        task = self.submit()
        self.assertEqual(task['id'], self.submit()['id'])
        self.assertEqual(self.wait(task['id'])['state'], 'succeeded')
        child = self.service.call('continue_task', {'request_id': 'b', 'task_id': task['id'], 'prompt': 'next'})
        self.assertEqual(self.wait(child['id'])['session_id'], 'native-123')
        self.assertEqual(self.fake.calls, 2)
    def test_cancel_and_result(self):
        task = self.submit(prompt='wait')
        time.sleep(.1)
        self.service.call('cancel_task', {'task_id': task['id']})
        self.assertEqual(self.wait(task['id'])['state'], 'cancelled')
        self.assertTrue(self.service.call('get_result', {'task_id': task['id']})['ready'])
    def test_remote_session_cancel_is_pending_until_adapter_confirmation(self):
        self.fake.cancel_scope = 'remote_session'
        started, release = threading.Event(), threading.Event()
        def remote_run(task, cancel, event):
            started.set()
            cancel.wait(3)
            release.wait(3)
            raise HubError('CANCELLED', 'confirmed stopped', {'remote_cancelled':True,
                'interrupt_requested':True,'interrupt_acknowledged':True})
        self.fake.run = remote_run
        task = self.submit()
        self.assertTrue(started.wait(3))
        pending = self.service.call('cancel_task',{'task_id':task['id']})
        self.assertEqual(pending['state'],'cancelling')
        self.assertEqual(pending['cancel_scope'],'remote_session')
        self.assertFalse(pending['remote_cancelled'])
        release.set()
        self.wait(task['id'])
        confirmed = self.service.call('cancel_task',{'task_id':task['id']})
        self.assertTrue(confirmed['remote_cancelled'])
        self.assertEqual(self.fake.calls,0)
    def test_queued_cancellation_does_not_claim_a_remote_request(self):
        self.fake.cancel_scope = 'remote_session'
        self.service.per_agent_concurrency['fake'] = 0
        task = self.submit()
        result = self.service.call('cancel_task',{'task_id':task['id']})
        self.assertEqual(result['cancel_scope'],'not_started')
        self.assertFalse(result['remote_cancelled'])
        self.assertEqual(self.fake.calls,0)
    def test_unconfirmed_remote_cancellation_cannot_continue(self):
        self.fake.cancel_scope='remote_hub_task'
        task=self.submit(prompt='wait')
        for _ in range(100):
            if self.fake.calls:break
            time.sleep(.03)
        self.service.call('cancel_task',{'task_id':task['id']})
        self.wait(task['id'])
        with self.assertRaises(HubError) as error:
            self.service.call('continue_task',dict(task_id=task['id'],request_id='unknown-next',prompt='next'))
        self.assertEqual(error.exception.code,'REMOTE_STATE_UNKNOWN')
    def test_validation(self):
        with self.assertRaises(HubError):
            self.submit(timeout_seconds=1)
        with self.assertRaises(HubError):
            self.submit(agent='missing')
    def test_timeout_and_parallel_followup_rejection(self):
        task = self.submit(prompt='wait', timeout_seconds=10)
        deadline = time.time() + 13
        while time.time() < deadline:
            if self.service.call('get_task', {'task_id': task['id']})['state'] == 'timed_out': break
            time.sleep(.1)
        self.assertEqual(self.service.call('get_task', {'task_id': task['id']})['state'], 'timed_out')
        parent = self.submit(request_id='parent')
        self.wait(parent['id'])
        child = self.service.call('continue_task', dict(request_id='child', task_id=parent['id'], prompt='wait'))
        duplicate = self.service.call('continue_task', dict(request_id='child', task_id=parent['id'], prompt='wait'))
        self.assertEqual(child['id'], duplicate['id'])
        with self.assertRaises(HubError) as error:
            self.service.call('continue_task', dict(request_id='other', task_id=parent['id'], prompt='wait'))
        self.assertEqual(error.exception.code, 'WORKSPACE_BUSY')
        self.service.call('cancel_task', {'task_id': child['id']})
        self.wait(child['id'])
    def test_cancel_during_result_serialization(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service.store.safe
        def blocked(value):
            entered.set(); release.wait(5)
            return original(value)
        self.service.store.safe = blocked
        task = self.submit()
        self.assertTrue(entered.wait(3))
        self.service.call('cancel_task', {'task_id': task['id']})
        release.set()
        self.assertEqual(self.wait(task['id'])['state'], 'cancelled')
    def test_interrupted_workspace_cannot_be_resumed(self):
        # The scheduler takes queued tasks under this lock; without it, a task can start running
        # between create and update, finish normally, and leave recover() nothing to interrupt.
        with self.service.lock:
            parent = self.service.store.create('a', dict(agent='fake', prompt='x', cwd=str(self.root),
                workspace=str(self.root), mode='read_only', timeout_seconds=10, session_id='sid'))
            self.service.store.update(parent['id'], state='running')
            self.service.store.recover()
        with self.assertRaises(HubError) as error:
            self.service.call('continue_task', dict(request_id='b', task_id=parent['id'], prompt='next'))
        self.assertEqual(error.exception.code, 'WORKSPACE_UNCERTAIN')

    def test_recovery_blocks_already_queued_task_in_quarantined_workspace(self):
        with self.service.lock:
            spec = dict(agent='fake', prompt='x', cwd=str(self.root), workspace=str(self.root),
                        mode='read_only', timeout_seconds=30)
            old = self.service.store.create('crashed', spec)
            self.service.store.update(old['id'], state='running')
            queued = self.service.store.create('queued-before-crash', spec)
            self.service.store.recover()
        for _ in range(100):
            task = self.service.store.get(queued['id'])
            if task['state'] == 'interrupted': break
            time.sleep(.03)
        self.assertEqual(task['state'], 'interrupted')
        self.assertTrue(task['workspace_quarantined'])
        self.assertEqual(self.fake.calls, 0)
        other = self.root / 'safe'; other.mkdir()
        safe = self.submit(request_id='unaffected', cwd=str(other))
        self.assertEqual(self.wait(safe['id'])['state'], 'succeeded')

    def test_worktree_and_dirty_rejection(self):
        repo = self.root / '原仓库'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
        git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@localhost')
        (repo / 'a.txt').write_text('base')
        git('add', '.'); git('commit', '-m', 'base')
        ws = Workspaces(self.root / 'data')
        result = ws.prepare(dict(id='123abc', cwd=str(repo), mode='edit_worktree'))
        (Path(result['workspace']) / 'a.txt').write_text('edited')
        self.assertEqual((repo / 'a.txt').read_text(), 'base')
        self.assertIn('edited', ws.artifacts(dict(result, mode='edit_worktree'))['diff'])
        (repo / 'a.txt').write_text('dirty')
        with self.assertRaises(HubError) as error:
            ws.prepare(dict(id='456def', cwd=str(repo), mode='edit_worktree'))
        self.assertEqual(error.exception.code, 'DIRTY_WORKSPACE')
        # Git worktrees live only inside this disposable test directory.

    def test_untracked_files_are_preserved_and_excluded_from_head_worktree(self):
        repo = self.root / '原仓库'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True).stdout
        git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@localhost')
        (repo / 'a.txt').write_text('base')
        git('add', '.'); git('commit', '-m', 'base')
        name = '未跟踪 space.txt'
        (repo / name).write_text('local only', encoding='utf-8')
        before = git('status', '--porcelain', '-z')
        task = self.submit(cwd=str(repo), mode='edit_worktree')
        self.assertEqual(self.wait(task['id'])['state'], 'succeeded')
        result = self.service.call('get_result', {'task_id': task['id']})
        artifacts = result['artifacts']
        self.assertEqual(artifacts['baseline'], git('rev-parse', 'HEAD').decode().strip())
        self.assertEqual(artifacts['excluded_untracked_files'], [name])
        self.assertFalse((Path(artifacts['workspace']) / name).exists())
        self.assertEqual((repo / name).read_text(encoding='utf-8'), 'local only')
        self.assertEqual(git('status', '--porcelain', '-z'), before)
        self.assertEqual(artifacts['diff'], '')
        child = self.service.call('continue_task', dict(request_id='untracked-child', task_id=task['id'], prompt='next'))
        self.assertEqual(self.wait(child['id'])['state'], 'succeeded')
        child_result = self.service.call('get_result', {'task_id': child['id']})
        self.assertEqual(child_result['artifacts']['excluded_untracked_files'], [name])
        self.assertEqual(child_result['artifacts']['workspace'], artifacts['workspace'])
        (repo / 'a.txt').write_text('dirty')
        with self.assertRaises(HubError) as error:
            self.service.workspaces.prepare(dict(id='blocked', cwd=str(repo), mode='edit_worktree'))
        self.assertEqual(error.exception.code, 'DIRTY_WORKSPACE')
        git('add', 'a.txt')
        with self.assertRaises(HubError) as error:
            self.service.workspaces.prepare(dict(id='staged-blocked', cwd=str(repo), mode='edit_worktree'))
        self.assertEqual(error.exception.code, 'DIRTY_WORKSPACE')

    def test_configurable_concurrency_and_workspace_exclusion(self):
        self.service.close()
        self.service = Service(self.root / 'limits', {'fake': self.fake},
                               concurrency=2, per_agent_concurrency={'fake': 2})
        other = self.root / 'other'
        other.mkdir()
        a = self.submit(request_id='limit-a', prompt='wait')
        b = self.submit(request_id='limit-b', prompt='wait', cwd=str(other))
        same = self.submit(request_id='limit-same', prompt='wait')
        deadline = time.time() + 3
        while time.time() < deadline and self.fake.calls < 2:
            time.sleep(.03)
        self.assertEqual(self.fake.calls, 2)
        self.assertEqual(self.service.store.get(same['id'])['state'], 'queued')
        self.assertEqual(len(self.service.active), 2)
        self.service.call('cancel_task', {'task_id': a['id']})
        self.wait(a['id'])
        deadline = time.time() + 3
        while time.time() < deadline and self.fake.calls < 3:
            time.sleep(.03)
        self.assertEqual(self.fake.calls, 3)
        for task in (b, same):
            self.service.call('cancel_task', {'task_id': task['id']})

    def test_invalid_concurrency_config(self):
        from agent_hub.config import scheduler_options
        path = self.root / 'config.json'
        path.write_text('{"concurrency":{"global":0}}', encoding='utf-8')
        with self.assertRaises(HubError) as error:
            scheduler_options(self.root)
        self.assertEqual(error.exception.code, 'INVALID_CONFIG')

    def test_failed_edit_preserves_partial_diff_and_logs(self):
        repo = self.root / 'failed repo'
        repo.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
        git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@localhost')
        (repo / 'a.txt').write_text('base')
        git('add', '.'); git('commit', '-m', 'base')
        def fail(task, cancel, event):
            (Path(task['workspace']) / 'a.txt').write_text('partial edit')
            raise HubError('CLI_FAILED', 'executor failed',
                           {'exit_code': 7, 'artifacts': {'logs': {'stderr': 'private-log'}}})
        self.fake.run = fail
        task = self.submit(cwd=str(repo), mode='edit_worktree')
        self.assertEqual(self.wait(task['id'])['state'], 'failed')
        result = self.service.call('get_result', {'task_id': task['id']})
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['artifacts']['logs']['stderr'], 'private-log')
        self.assertIn('partial edit', result['artifacts']['diff'])
        self.assertEqual((repo / 'a.txt').read_text(), 'base')

    def test_dynamic_worktree_is_reserved_for_entire_execution(self):
        self.service.close()
        self.service = Service(self.root / 'dynamic', {'fake': self.fake},
                               concurrency=2, per_agent_concurrency={'fake': 2})
        repo = self.root / 'dynamic repo'
        repo.mkdir()
        def git(*args):
            subprocess.run(['git', '-C', str(repo), *args], capture_output=True, check=True)
        git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@localhost')
        (repo / 'a.txt').write_text('base')
        git('add', '.'); git('commit', '-m', 'base')
        task = self.submit(request_id='dynamic-edit', cwd=str(repo), mode='edit_worktree', prompt='wait')
        deadline = time.time() + 5
        workspace = None
        while time.time() < deadline:
            workspace = self.service.store.get(task['id']).get('workspace')
            if workspace and self.fake.calls:
                break
            time.sleep(.03)
        self.assertTrue(workspace)
        reader = self.submit(request_id='dynamic-read', cwd=workspace, prompt='wait')
        time.sleep(.2)
        self.assertEqual(self.service.store.get(reader['id'])['state'], 'queued')
        self.assertEqual(self.fake.calls, 1)
        self.service.call('cancel_task', {'task_id': task['id']})
        self.wait(task['id'])
        deadline = time.time() + 3
        while time.time() < deadline and self.fake.calls < 2:
            time.sleep(.03)
        self.assertEqual(self.fake.calls, 2)
        self.service.call('cancel_task', {'task_id': reader['id']})

if __name__ == '__main__': unittest.main()
