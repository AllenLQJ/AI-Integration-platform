import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_hub.processes import environment, run_process
from agent_hub.models import HubError


class ProcessTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX 独立 session')
    def test_detached_child_is_cleaned_on_cancel_and_timeout(self):
        import subprocess
        for cancelling in (True, False):
            with self.subTest(cancelling=cancelling), tempfile.TemporaryDirectory() as td:
                child_file = Path(td) / 'detached.pid'
                code = ('import subprocess,sys,time;from pathlib import Path;'
                        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],'
                        'start_new_session=True);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)')
                cancel = threading.Event()
                timer = threading.Timer(.7, cancel.set)
                if cancelling: timer.start()
                try:
                    with self.assertRaises(HubError):
                        run_process([sys.executable, '-c', code, str(child_file)], td,
                                    environment(), cancel, time.time() + (5 if cancelling else .7),
                                    td, lambda *a: None)
                    pid = int(child_file.read_text())
                    for _ in range(50):
                        status = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                                capture_output=True, text=True).stdout.strip()
                        if not status or status.startswith('Z'): break
                        time.sleep(.05)
                    self.assertTrue(not status or status.startswith('Z'), status)
                finally:
                    timer.cancel()

    def test_environment_isolation(self):
        old = os.environ.get('ANTHROPIC_API_KEY')
        os.environ['ANTHROPIC_API_KEY'] = 'not-a-real-key'
        try:
            self.assertNotIn('ANTHROPIC_API_KEY', environment())
            self.assertNotIn('CLAUDECODE', environment())
            if os.name == 'nt' and 'ProgramFiles' in os.environ:
                self.assertEqual(environment()['PROGRAMFILES'], os.environ['ProgramFiles'])
                self.assertEqual(environment()['SYSTEMDRIVE'], os.environ['SystemDrive'])
        finally:
            if old is None: os.environ.pop('ANTHROPIC_API_KEY', None)
            else: os.environ['ANTHROPIC_API_KEY'] = old

    def test_proxy_bypass_merges_user_entries_and_covers_lowercase(self):
        with patch.dict(os.environ, {'NO_PROXY': 'corp.example, localhost'}, clear=False):
            if os.name != 'nt':
                os.environ['no_proxy'] = 'internal.example'
            env = environment()
        entries = env['NO_PROXY'].split(',')
        for host in ('127.0.0.1', 'localhost', '::1', 'corp.example'):
            self.assertIn(host, entries)
        self.assertEqual(len(entries), len(set(entries)))
        if os.name != 'nt':
            self.assertIn('internal.example', entries)
            self.assertEqual(env['no_proxy'], env['NO_PROXY'])

    @unittest.skipIf(os.name == 'nt', 'POSIX 后代追踪')
    def test_missing_ps_degrades_to_process_group_cleanup(self):
        from agent_hub import processes
        real = processes.subprocess.check_output
        def no_ps(args, *rest, **kwargs):
            if args[0] == 'ps':
                raise PermissionError(1, 'Operation not permitted', 'ps')
            return real(args, *rest, **kwargs)
        with patch.object(processes.subprocess, 'check_output', no_ps), tempfile.TemporaryDirectory() as td:
            self.assertIsNone(processes.posix_processes())
            tracked = {}
            processes.track_descendants(os.getpid(), tracked)
            self.assertEqual(tracked, {})
            processes.stop_descendants({os.getpid(): 'unverifiable'})
            rc, out, _, _ = run_process([sys.executable, '-c', 'print("ok")'], td, environment(),
                                        threading.Event(), time.time() + 5, td, lambda *a: None)
            self.assertEqual((rc, out.strip()), (0, 'ok'))
            with self.assertRaises(HubError) as error:
                run_process([sys.executable, '-c', 'import time;time.sleep(30)'], td, environment(),
                            threading.Event(), time.time() + .3, td, lambda *a: None)
            self.assertEqual(error.exception.code, 'TIMED_OUT')

    def test_timeout_and_unicode_output(self):
        with tempfile.TemporaryDirectory(prefix='执行 中文 ') as td:
            rc, out, _, _ = run_process([sys.executable, '-c', 'print("中文")'], td,
                                        environment(), threading.Event(), time.time()+5, td, lambda *a: None)
            self.assertEqual(rc, 0)
            self.assertIn('中文', out)
            with self.assertRaises(HubError) as error:
                run_process([sys.executable, '-c', 'import time;time.sleep(30)'], td,
                            environment(), threading.Event(), time.time()+.2, td, lambda *a: None)
            self.assertEqual(error.exception.code, 'TIMED_OUT')
    def test_child_is_cleaned_when_parent_exits(self):
        with tempfile.TemporaryDirectory() as td:
            child_file = Path(td) / 'child.pid'
            code = ('import subprocess,sys,time;from pathlib import Path;'
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
                    'Path(sys.argv[1]).write_text(str(p.pid));time.sleep(.3)')
            rc, _, _, _ = run_process([sys.executable, '-c', code, str(child_file)], td,
                    environment(), threading.Event(), time.time()+10, td, lambda *a: None)
            pid = int(child_file.read_text())
            if os.name == 'nt':
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    status = ctypes.c_ulong()
                    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(status))
                    ctypes.windll.kernel32.CloseHandle(handle)
                    self.assertNotEqual(status.value, 259)
            else:
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
