"""Antigravity CLI (agy) adapter — mode routing mirrors Codex/Cursor."""
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import CLI
from ..models import HubError


def _load_agy_env(path):
    """Parse KEY=VALUE lines from agy.env (no shell). Missing file → {}."""
    env = {}
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        if key:
            env[key] = value.strip().strip('"').strip("'")
    return env


class Antigravity(CLI):
    """Hub executor for Google Antigravity CLI.

    Mode routing (Codex/Cursor equivalent):
      read_only     → disposable git worktree (or copy) + ``--dangerously-skip-permissions``
                      so the model may use tools, but the original tree is untouched
                      (agy ``--mode plan`` hangs headless on write-oriented prompts)
      edit_worktree → ``--dangerously-skip-permissions`` (Hub isolates via git worktree)
      delegate      → plain ``-p`` print (no forced write / plan)

    Credentials live in the system keyring and need DISPLAY/DBUS; those are
    merged from ``~/.agent-hub/agy.env`` (or AGENT_HUB_AGY_ENV) into the process
    environment at construction time. USER/LOGNAME/TERM are also injected because
    Hub's process env whitelist omits them and agy hangs headless without USER.
    """

    remote_cancel = False
    cancel_scope = 'remote_session'
    # Codex exposes read_only + edit_worktree (+ continue). Keep delegate for
    # chat-style Hub tasks that already worked before the adapter existed.
    capabilities = ['read_only', 'edit_worktree', 'delegate']

    def __init__(self, executable, env=None, args=None):
        merged = dict(env or {})
        agy_env_path = os.environ.get('AGENT_HUB_AGY_ENV', str(Path.home() / '.agent-hub' / 'agy.env'))
        for key, value in _load_agy_env(agy_env_path).items():
            merged.setdefault(key, value)
        path = merged.get('PATH') or os.environ.get('PATH', '/usr/bin:/bin')
        local_bin = str(Path.home() / '.local' / 'bin')
        if local_bin not in path.split(':'):
            merged['PATH'] = local_bin + ':' + path
        try:
            login = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            login = os.environ.get('USER') or os.environ.get('LOGNAME') or 'box'
        merged.setdefault('USER', os.environ.get('USER') or login)
        merged.setdefault('LOGNAME', os.environ.get('LOGNAME') or login)
        merged.setdefault('TERM', os.environ.get('TERM') or 'dumb')
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        if not runtime:
            cand = f'/run/user/{os.getuid()}'
            if Path(cand).is_dir():
                runtime = cand
        if runtime:
            merged.setdefault('XDG_RUNTIME_DIR', runtime)
        super().__init__(executable, merged)
        if args is not None and (not isinstance(args, list) or any(not isinstance(a, str) for a in args)):
            raise ValueError('Antigravity args must be a string list')
        self.args = list(args or [])

    def permission_policy(self, mode):
        if mode == 'delegate':
            return {
                'enforced_by': 'antigravity_delegate',
                'enforced': False,
                'allows_file_read': True,
                'allows_write': None,
                'allows_commands': None,
                'os_sandbox': False,
                'workspace': 'original',
                'scope_note': 'delegate print mode; Hub does not enforce tool permissions',
            }
        if mode == 'read_only':
            return {
                'enforced_by': 'antigravity_disposable_worktree',
                'enforced': True,
                'allows_file_read': True,
                'allows_write': False,
                'allows_commands': True,
                'os_sandbox': False,
                'workspace': 'original',
                'scope_note': (
                    'read_only runs in a disposable git worktree (or copy) with '
                    '--dangerously-skip-permissions; the original directory is not modified'
                ),
            }
        return {
            'enforced_by': 'antigravity_mode_and_sandbox',
            'enforced': True,
            'allows_file_read': True,
            'allows_write': True,
            'allows_commands': True,
            'os_sandbox': False,
            'workspace': 'git_worktree',
            'scope_note': 'edit_worktree uses Hub git worktree + --dangerously-skip-permissions',
        }

    def version_command(self):
        return [self.executable, '--version']

    def command(self, task):
        mode = task['mode']
        args = [self.executable, *self.args]
        if mode == 'read_only':
            # Disposable tree (see run()) provides isolation; skip-permissions avoids
            # headless approval hangs that agy --mode plan hits on write-oriented prompts.
            args += ['--dangerously-skip-permissions']
        elif mode == 'edit_worktree':
            args += ['--dangerously-skip-permissions']
        elif mode != 'delegate':
            raise HubError('UNSUPPORTED_MODE', 'Antigravity does not support ' + str(mode))
        args += [
            '-p', task['prompt'],
            '--output-format', 'text',
            '--add-dir', task['workspace'],
        ]
        if task.get('resume_session'):
            args += ['--conversation', task['resume_session']]
        return args

    def parse(self, output):
        text = (output or '').rstrip('\r\n')
        if not text.strip():
            raise HubError('INVALID_OUTPUT', 'Antigravity returned empty output')
        return {'answer': text}

    def _prepare_readonly_workspace(self, original):
        """Return (workspace_path, cleanup_callable) that must not touch *original*."""
        original = Path(original).resolve()
        tmp = Path(tempfile.mkdtemp(prefix='agy-ro-'))
        git_dir = original / '.git'
        if git_dir.exists():
            try:
                subprocess.run(
                    ['git', 'worktree', 'add', '--detach', str(tmp), 'HEAD'],
                    cwd=str(original), check=True, capture_output=True, text=True, timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as error:
                shutil.rmtree(tmp, ignore_errors=True)
                raise HubError('INVALID_WORKSPACE', 'Antigravity read_only worktree failed: ' + str(error)) from None

            def cleanup():
                subprocess.run(
                    ['git', 'worktree', 'remove', '--force', str(tmp)],
                    cwd=str(original), capture_output=True, text=True, timeout=60,
                )
                shutil.rmtree(tmp, ignore_errors=True)
            return str(tmp), cleanup

        # Non-git: full copy (follow Hub's "original untouched" contract).
        try:
            shutil.copytree(original, tmp, dirs_exist_ok=True, symlinks=True,
                            ignore=shutil.ignore_patterns('.git'))
        except OSError as error:
            shutil.rmtree(tmp, ignore_errors=True)
            raise HubError('INVALID_WORKSPACE', 'Antigravity read_only copy failed: ' + str(error)) from None

        def cleanup():
            shutil.rmtree(tmp, ignore_errors=True)
        return str(tmp), cleanup

    def run(self, task, cancel, event):
        if task.get('mode') != 'read_only':
            return super().run(task, cancel, event)
        workspace, cleanup = self._prepare_readonly_workspace(task['workspace'])
        try:
            return super().run(dict(task, workspace=workspace), cancel, event)
        finally:
            cleanup()
