import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub.adapters.base import resolve_executable
from agent_hub.adapters.codex import Codex


class ResolveExecutableTests(unittest.TestCase):
    def test_absolute_path_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            self.assertEqual(resolve_executable(str(exe)), str(exe))
            missing = str(Path(d) / 'gone' / 'codex.exe')
            self.assertEqual(resolve_executable(missing), missing)

    def test_glob_picks_newest_and_follows_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / 'bin' / 'aaa' / 'codex.exe'; old.parent.mkdir(parents=True); old.write_text('')
            new = Path(d) / 'bin' / 'bbb' / 'codex.exe'; new.parent.mkdir(parents=True); new.write_text('')
            now = time.time(); os.utime(old, (now - 100, now - 100)); os.utime(new, (now, now))
            with patch.dict(os.environ, {'HUB_TEST_ROOT': d}):
                adapter = Codex(os.path.join('$HUB_TEST_ROOT', 'bin', '*', 'codex.exe'))
                self.assertEqual(Path(adapter.executable), new)
                newer = Path(d) / 'bin' / 'ccc' / 'codex.exe'; newer.parent.mkdir(); newer.write_text('')
                os.utime(newer, (now + 100, now + 100))
                self.assertEqual(Path(adapter.executable), newer)
                self.assertEqual(Path(adapter.command({'mode': 'read_only', 'workspace': d, 'prompt': 'x'})[0]), newer)

    def test_glob_without_match_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            adapter = Codex(os.path.join(d, 'none', '*', 'codex.exe'))
            self.assertFalse(adapter.diagnose()['available'])

    def test_bare_name_uses_path(self):
        self.assertEqual(resolve_executable('codex'), 'codex')
        adapter = Codex('codex')
        with patch('agent_hub.adapters.base.shutil.which', return_value='/opt/bin/codex') as which:
            self.assertTrue(adapter.diagnose()['available'])
            which.assert_called_with('codex')
        with patch('agent_hub.adapters.base.shutil.which', return_value=None):
            self.assertFalse(adapter.diagnose()['available'])

if __name__ == '__main__':
    unittest.main()
