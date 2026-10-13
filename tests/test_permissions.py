"""验证执行器权限说明、新模式及历史任务兼容，不调用真实账号。"""
import tempfile
import time
import unittest
from pathlib import Path
from agent_hub.adapters.claude import Claude
from agent_hub.adapters.muse import Muse
from agent_hub.adapters.codex import Codex
from agent_hub.service import Service
from agent_hub.models import HubError


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.claude = Claude('unused')
        self.muse = Muse('unused')
        self.seen = []
        def run(task, cancel, event):
            self.seen.append(task['mode'])
            if task['agent'] == 'claude':
                event('session', {'session_id': 'sid', 'source': 'cli_init'})
            return {'answer': 'done', 'session_id': 'sid'}
        self.claude.run = self.muse.run = run
        self.service = Service(self.root/'data', {'claude': self.claude, 'muse': self.muse})
    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()
    def submit(self, agent, mode, key='new'):
        return self.service.call('submit_task', dict(request_id=key, agent=agent,
            mode=mode, cwd=str(self.root), prompt='test'))
    def finish(self, task):
        for _ in range(100):
            r = self.service.call('get_result', {'task_id': task['id']})
            if r['ready']:
                self.assertEqual(r['state'], 'succeeded')
                return r
            time.sleep(.03)
        self.fail('任务未完成')
    def legacy(self, agent):
        # Publish the synthetic historical task atomically relative to the
        # scheduler, which otherwise may execute its temporary queued state.
        with self.service.lock:
            task = self.service.store.create('legacy', dict(agent=agent, mode='read_only',
                prompt='old', cwd=str(self.root), timeout_seconds=1800,
                workspace=str(self.root), session_id='sid'))
            self.service.store.finish(task['id'], 'succeeded', {'answer': 'old'})
        return task
    def test_muse_cannot_submit_false_read_only_or_continue_as_mode(self):
        for mode in ('read_only', 'continue'):
            with self.assertRaises(HubError) as caught:
                self.submit('muse', mode)
            self.assertEqual(caught.exception.code, 'UNSUPPORTED_MODE')
        t = self.submit('muse', 'delegate')
        self.assertFalse(t['permissions']['enforced'])
        self.assertTrue(t['permissions']['allows_write'])
        self.assertEqual(self.finish(t)['permissions'], t['permissions'])
    def test_claude_read_only_policy_survives_result_and_continue(self):
        t = self.submit('claude', 'read_only')
        self.assertTrue(t['permissions']['allows_file_read'])
        self.assertFalse(t['permissions']['allows_write'])
        self.assertEqual(self.submit('claude','read_only')['id'],t['id'])
        self.assertEqual(self.finish(t)['permissions'], t['permissions'])
        child = self.service.call('continue_task',dict(request_id='next',task_id=t['id'],prompt='next'))
        self.finish(child)
        self.assertEqual(self.seen,['read_only','read_only'])
    def test_legacy_claude_continues_without_expanding_tools(self):
        t=self.legacy('claude')
        old=self.service.call('get_task',{'task_id':t['id']})
        self.assertFalse(old['permissions']['allows_file_read'])
        child=self.service.call('continue_task',dict(request_id='next',task_id=t['id'],prompt='next'))
        self.assertEqual(child['mode'],'text_only')
        self.finish(child)
        self.assertEqual(self.seen,['text_only'])
    def test_legacy_muse_is_readable_but_cannot_continue(self):
        t=self.legacy('muse')
        self.assertFalse(self.service.call('get_result',{'task_id':t['id']})['permissions']['enforced'])
        with self.assertRaises(HubError) as caught:
            self.service.call('continue_task',dict(request_id='next',task_id=t['id'],prompt='next'))
        self.assertEqual(caught.exception.code,'LEGACY_MODE')
    def test_non_edit_modes_use_original_workspace(self):
        for mode in ('text_only','read_only','delegate'):
            self.assertEqual(self.service.workspaces.prepare(dict(mode=mode,cwd=str(self.root)))['workspace'],str(self.root))
    def test_codex_policies_describe_sandbox(self):
        c=Codex('unused')
        self.assertFalse(c.permission_policy('read_only')['allows_write'])
        self.assertTrue(c.permission_policy('edit_worktree')['os_sandbox'])
