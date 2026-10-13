import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub.adapters.base import resolve_executable, executable_available
from agent_hub.adapters.codex import Codex
from agent_hub.models import HubError


def _task(d):
    return {'mode': 'read_only', 'workspace': d, 'prompt': 'x', 'deadline': time.time() + 60,
            'log_dir': os.path.join(d, 'logs')}


def _version_ok(*args, **kwargs):
    return subprocess.CompletedProcess(args[0] if args else [], 0, stdout='codex 1.0\n', stderr='')


class ResolveExecutableTests(unittest.TestCase):
    def test_absolute_path_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            self.assertEqual(resolve_executable(str(exe)), str(exe))
            missing = str(Path(d) / 'gone' / 'codex.exe')
            self.assertEqual(resolve_executable(missing), missing)

    def test_tilde_expanded(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {'HOME': d, 'USERPROFILE': d}):
                self.assertEqual(resolve_executable(os.path.join('~', 'codex.exe')),
                                 os.path.join(d, 'codex.exe'))

    @unittest.skipUnless(os.name == 'nt', '%VAR% expansion is Windows-only')
    def test_percent_var_expanded(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'bin' / 'h1' / 'codex.exe'; exe.parent.mkdir(parents=True); exe.write_text('')
            with patch.dict(os.environ, {'HUB_TEST_ROOT': d}):
                self.assertEqual(Path(resolve_executable('%HUB_TEST_ROOT%/bin/*/codex.exe')), exe)
                self.assertEqual(Path(resolve_executable('%HUB_TEST_ROOT%\\\\bin\\\\h1\\\\codex.exe')), exe)

    def test_directory_is_not_executable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(executable_available(d))
            (Path(d) / 'bin' / 'codex.exe').mkdir(parents=True)  # directory named like the exe
            self.assertFalse(executable_available(str(Path(d) / 'bin' / 'codex.exe')))
            self.assertEqual(resolve_executable(os.path.join(d, 'bin', '*')), os.path.join(d, 'bin', '*'))

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
                self.assertEqual(Path(adapter.command(_task(d))[0]), newer)

    def test_glob_tie_breaks_on_greatest_path(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for name in ('aaa', 'zzz', 'mmm'):
                p = Path(d) / name / 'codex.exe'; p.parent.mkdir(); p.write_text(''); paths.append(p)
            for p in paths:
                os.utime(p, (1000000, 1000000))
            self.assertEqual(Path(resolve_executable(os.path.join(d, '*', 'codex.exe'))), Path(d) / 'zzz' / 'codex.exe')

    def test_glob_skips_vanished_file(self):
        with tempfile.TemporaryDirectory() as d:
            keep = Path(d) / 'a' / 'codex.exe'; keep.parent.mkdir(); keep.write_text('')
            gone = Path(d) / 'b' / 'codex.exe'; gone.parent.mkdir(); gone.write_text('')
            from agent_hub.adapters import base
            real = base._mtime
            with patch.object(base, '_mtime', side_effect=lambda m: None if Path(m) == gone else real(m)):
                self.assertEqual(Path(resolve_executable(os.path.join(d, '*', 'codex.exe'))), keep)

    def test_glob_without_match_reports_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            spec = os.path.join(d, 'none', '*', 'codex.exe')
            adapter = Codex(spec)
            info = adapter.diagnose()
            self.assertFalse(info['available'])
            self.assertEqual(info['resolved_executable'], spec)

    def test_glob_without_match_run_raises_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            spec = os.path.join(d, 'none', '*', 'codex.exe')
            with self.assertRaises(HubError) as ctx:
                Codex(spec).run(_task(d), None, lambda *a: None)
            self.assertEqual(ctx.exception.code, 'AGENT_UNAVAILABLE')
            self.assertIn(spec, str(ctx.exception))

    def test_spawn_failure_maps_to_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            target = 'agent_hub.windows_process.WindowsProcess' if os.name == 'nt' else 'agent_hub.processes.subprocess.Popen'
            with patch(target, side_effect=OSError('boom')):
                with self.assertRaises(HubError) as ctx:
                    Codex(str(exe)).run(_task(d), None, lambda *a: None)
            self.assertEqual(ctx.exception.code, 'AGENT_UNAVAILABLE')
            self.assertIn(str(exe), str(ctx.exception))

    def test_real_spawn_failure_maps_to_unavailable(self):
        # Empty file is not a valid executable: on Windows CreateProcess raises
        # pywintypes.error (not an OSError subclass); on POSIX Popen raises OSError.
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_bytes(b'')
            with self.assertRaises(HubError) as ctx:
                Codex(str(exe)).run(_task(d), None, lambda *a: None)
            self.assertEqual(ctx.exception.code, 'AGENT_UNAVAILABLE')
            self.assertIn(str(exe), str(ctx.exception))
            self.assertEqual(ctx.exception.details['resolved_executable'], str(exe))

    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_pywintypes_error_from_createprocess_maps_to_unavailable(self):
        import pywintypes
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            err = pywintypes.error(193, 'CreateProcess', '%1 is not a valid Win32 application.')
            self.assertNotIsInstance(err, OSError)
            with patch('win32process.CreateProcess', side_effect=err):
                with self.assertRaises(HubError) as ctx:
                    Codex(str(exe)).run(_task(d), None, lambda *a: None)
            self.assertEqual(ctx.exception.code, 'AGENT_UNAVAILABLE')
            self.assertIn('error', str(ctx.exception))

    def test_log_dir_failure_is_not_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            with patch('agent_hub.processes.Path.mkdir', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    Codex(str(exe)).run(_task(d), None, lambda *a: None)

    def test_missing_workspace_is_not_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            task = _task(d); task['workspace'] = os.path.join(d, 'missing')
            with self.assertRaises(HubError) as ctx:
                Codex(str(exe)).run(task, None, lambda *a: None)
            self.assertEqual(ctx.exception.code, 'INVALID_WORKSPACE')

    def test_run_resolves_once(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            with patch('agent_hub.adapters.base.resolve_executable', return_value=str(exe)) as resolve, \
                 patch('agent_hub.adapters.base.run_process', return_value=(0, '', '', {})), \
                 patch.object(Codex, 'parse', return_value={'ok': True}, create=True):
                try:
                    Codex(str(exe)).run(_task(d), None, lambda *a: None)
                except HubError:
                    pass
            self.assertEqual(resolve.call_count, 1)

    def test_bare_name_resolves_with_which(self):
        with patch('agent_hub.adapters.base.shutil.which', return_value='/opt/bin/codex') as which:
            self.assertEqual(resolve_executable('codex'), '/opt/bin/codex')
            which.assert_called_with('codex')
        with patch('agent_hub.adapters.base.shutil.which', return_value='/x/codex') as which:
            self.assertEqual(resolve_executable('codex', {'PATH': '/x'}), '/x/codex')
            which.assert_called_with('codex', path='/x')
        with patch('agent_hub.adapters.base.shutil.which', return_value=None):
            self.assertEqual(resolve_executable('codex'), 'codex')

    def test_bare_name_diagnose(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            adapter = Codex('codex')
            with patch('agent_hub.adapters.base.shutil.which', return_value=str(exe)), \
                 patch('agent_hub.adapters.base.subprocess.run', side_effect=_version_ok) as run:
                info = adapter.diagnose()
            self.assertTrue(info['available'])
            self.assertEqual(info['resolved_executable'], str(exe))
            self.assertEqual(run.call_args[0][0][0], str(exe))
            with patch('agent_hub.adapters.base.shutil.which', return_value=None), \
                 patch('agent_hub.adapters.base.subprocess.run', side_effect=_version_ok) as run:
                self.assertFalse(adapter.diagnose()['available'])
                run.assert_not_called()

    def test_diagnose_resolves_once(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / 'codex.exe'; exe.write_text('')
            adapter = Codex(str(exe))
            with patch('agent_hub.adapters.base.resolve_executable', return_value=str(exe)) as resolve, \
                 patch('agent_hub.adapters.base.subprocess.run', side_effect=_version_ok):
                adapter.diagnose()
            self.assertEqual(resolve.call_count, 1)

if __name__ == '__main__':
    unittest.main()
