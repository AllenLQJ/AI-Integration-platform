import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hub.adapters.antigravity import Antigravity
from agent_hub.models import HubError


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Antigravity('/usr/bin/true', env={'DISPLAY': ':0'})

    def test_capabilities_mirror_codex_plus_delegate(self):
        self.assertEqual(self.adapter.capabilities,
                         ['read_only', 'edit_worktree', 'delegate'])

    def test_read_only_uses_skip_permissions_and_disposable_policy(self):
        argv = self.adapter.command({
            'prompt': 'summarize README', 'mode': 'read_only',
            'workspace': '/tmp/ws',
        })
        self.assertEqual(argv[0], '/usr/bin/true')
        self.assertIn('--dangerously-skip-permissions', argv)
        self.assertNotIn('--mode', argv)
        policy = self.adapter.permission_policy('read_only')
        self.assertTrue(policy['enforced'])
        self.assertFalse(policy['allows_write'])
        self.assertEqual(policy['enforced_by'], 'antigravity_disposable_worktree')

    def test_edit_worktree_skips_permissions(self):
        argv = self.adapter.command({
            'prompt': 'fix bug', 'mode': 'edit_worktree',
            'workspace': '/tmp/wt',
        })
        self.assertIn('--dangerously-skip-permissions', argv)
        policy = self.adapter.permission_policy('edit_worktree')
        self.assertTrue(policy['enforced'])
        self.assertTrue(policy['allows_write'])
        self.assertEqual(policy['workspace'], 'git_worktree')

    def test_delegate_is_plain_print(self):
        argv = self.adapter.command({
            'prompt': 'hi', 'mode': 'delegate', 'workspace': '/tmp/ws',
        })
        self.assertNotIn('--dangerously-skip-permissions', argv)
        self.assertNotIn('--sandbox', argv)
        policy = self.adapter.permission_policy('delegate')
        self.assertFalse(policy['enforced'])

    def test_unsupported_mode_rejected(self):
        with self.assertRaises(HubError) as caught:
            self.adapter.command({'prompt': 'x', 'mode': 'text_only', 'workspace': '/tmp'})
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_MODE')

    def test_parse_rejects_empty(self):
        with self.assertRaises(HubError) as caught:
            self.adapter.parse('   \n')
        self.assertEqual(caught.exception.code, 'INVALID_OUTPUT')
        self.assertEqual(self.adapter.parse('hello\n')['answer'], 'hello')

    def test_readonly_prepare_uses_git_worktree_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'README.md').write_text('hi\n', encoding='utf-8')
            import subprocess
            subprocess.check_call(['git', 'init', '-q'], cwd=td)
            subprocess.check_call(['git', 'add', 'README.md'], cwd=td)
            subprocess.check_call(
                ['git', '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-qm', 'i'], cwd=td)
            ws, cleanup = self.adapter._prepare_readonly_workspace(td)
            try:
                self.assertTrue(Path(ws).joinpath('README.md').is_file())
                self.assertNotEqual(Path(ws).resolve(), root.resolve())
            finally:
                cleanup()
            self.assertFalse(Path(ws).exists())


if __name__ == '__main__':
    unittest.main()
