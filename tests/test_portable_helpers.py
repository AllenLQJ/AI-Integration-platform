"""Optional diagnostics must not depend on the author's checkout layout."""
import asyncio
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StopBeforeConnection(Exception):
    pass


class PortableHelperTests(unittest.TestCase):
    def capture_muse_parameters(self, env):
        module = load_script('verify_muse_probe', ROOT / 'verify_muse.py')
        seen = []

        def capture(params):
            seen.append(params)
            raise StopBeforeConnection

        with patch.dict(os.environ, env, clear=True), patch.object(module, 'stdio_client', capture):
            with self.assertRaises(StopBeforeConnection):
                asyncio.run(module.main())
        return seen[0]

    def test_muse_verification_defaults_to_active_python(self):
        params = self.capture_muse_parameters({})
        self.assertEqual(params.command, sys.executable)
        self.assertEqual(params.args, ['-m', 'muse_mcp'])

    def test_muse_verification_honors_external_python_and_inherited_proxy(self):
        params = self.capture_muse_parameters({
            'MUSE_PYTHON': '/opt/external tools/python',
            'HTTPS_PROXY': 'http://proxy.example:8080',
            'PRIVATE_API_KEY': 'must-not-be-forwarded',
        })
        self.assertEqual(params.command, '/opt/external tools/python')
        self.assertEqual(params.env['HTTPS_PROXY'], 'http://proxy.example:8080')
        self.assertNotIn('PRIVATE_API_KEY', params.env)

    def test_muse_verification_exposes_python_override_without_connecting(self):
        result = subprocess.run([sys.executable, str(ROOT / 'verify_muse.py'), '--help'],
                                capture_output=True, encoding='utf-8', errors='replace', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--python', result.stdout)

    def test_preflight_preserves_proxy_and_uses_path_commands(self):
        with patch.dict(os.environ, {'HTTPS_PROXY': 'http://proxy.example:8080'}, clear=True):
            module = load_script('preflight_probe', ROOT / 'docs/probes/preflight.py')
        self.assertEqual(module.ENV['HTTPS_PROXY'], 'http://proxy.example:8080')
        calls = []

        def run(argv, cwd, timeout=150):
            calls.append(argv)
            return {'exit': 1, 'events': []}

        import tempfile
        with tempfile.TemporaryDirectory() as temp, patch.object(module, 'OUT', Path(temp)), patch.object(module, 'run', run):
            with patch.dict(os.environ, {}, clear=True), patch('builtins.print'):
                module.main('codex')
                module.main('claude')
        self.assertEqual(calls[0][0], 'codex')
        self.assertEqual(calls[1][0], 'claude')

    @unittest.skipUnless(os.name == 'nt', 'Windows authentication helper')
    def test_auth_helper_resolves_external_tools_without_authenticating(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        self.assertIsNotNone(shell)
        with tempfile.TemporaryDirectory(prefix='hub external tools ') as temp:
            script = Path(temp) / 'Authenticate-Muse.ps1'
            shutil.copy2(ROOT / 'Authenticate-Muse.ps1', script)
            browser = Path(temp) / 'agent-browser.exe'
            browser.write_bytes(b'check-only placeholder: never execute')
            result = subprocess.run([
                shell, '-NoProfile', '-File', str(script),
                '-MusePython', sys.executable, '-BrowserDirectory', temp, '-CheckOnly',
            ], capture_output=True, encoding='utf-8', errors='replace', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            import json
            resolved = json.loads(result.stdout)
            self.assertEqual(Path(resolved['python']), Path(sys.executable))
            self.assertEqual(Path(resolved['browser']), browser)

    @unittest.skipUnless(os.name == 'nt', 'Windows authentication helper')
    def test_auth_helper_rejects_cmd_only_browser_without_authenticating(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'Authenticate-Muse.ps1'
            shutil.copy2(ROOT / 'Authenticate-Muse.ps1', script)
            (Path(temp) / 'agent-browser.cmd').write_text('@exit /b 99\n')
            env = dict(os.environ, PATH=temp)
            result = subprocess.run([
                shell, '-NoProfile', '-File', str(script), '-MusePython', sys.executable,
                '-BrowserDirectory', temp, '-CheckOnly',
            ], capture_output=True, encoding='utf-8', errors='replace', timeout=20, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('agent-browser.exe', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows authentication helper')
    def test_auth_helper_does_not_fall_back_from_invalid_browser_directory(self):
        shell = shutil.which('pwsh') or shutil.which('powershell')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / 'Authenticate-Muse.ps1'
            shutil.copy2(ROOT / 'Authenticate-Muse.ps1', script)
            fallback, selected = root / 'fallback', root / 'selected'
            fallback.mkdir(); selected.mkdir()
            (fallback / 'agent-browser.exe').write_bytes(b'never execute')
            result = subprocess.run([
                shell, '-NoProfile', '-File', str(script), '-MusePython', sys.executable,
                '-BrowserDirectory', str(selected), '-CheckOnly',
            ], capture_output=True, encoding='utf-8', errors='replace', timeout=20,
                env=dict(os.environ, PATH=str(fallback)))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('agent-browser.exe', result.stderr)


if __name__ == '__main__':
    unittest.main()
