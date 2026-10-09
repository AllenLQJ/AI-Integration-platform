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
        args = dict(request_id='a', agent='fake', prompt='hello', cwd=str(self.root), mode='read_only')
        args.update(kw)
        return self.service.call('submit_task', args)
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
        parent = self.service.store.create('a', dict(agent='fake', prompt='x', cwd=str(self.root),
            workspace=str(self.root), mode='read_only', timeout_seconds=10, session_id='sid'))
        self.service.store.update(parent['id'], state='running')
        self.service.store.recover()
        with self.assertRaises(HubError) as error:
            self.service.call('continue_task', dict(request_id='b', task_id=parent['id'], prompt='next'))
        self.assertEqual(error.exception.code, 'WORKSPACE_UNCERTAIN')

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

if __name__ == '__main__': unittest.main()
