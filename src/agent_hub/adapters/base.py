import contextlib
import glob
import os
import shutil
import subprocess
import threading
from pathlib import Path
from ..models import HubError
from ..processes import SpawnError, environment, run_process


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None  # file vanished between glob() and stat()


def resolve_executable(spec, env=None):
    """Resolve a configured executable that may change location on upgrade.

    * environment variables (``$VAR`` / ``%VAR%``) and ``~`` are expanded;
    * glob patterns pick the newest matching *file* by modification time, e.g.
      ``%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe``. Tie-break: when mtimes are
      equal the lexicographically greatest path wins (``max`` over ``(mtime, path)``),
      so the choice is deterministic. Files that disappear during the scan are skipped;
    * bare command names (no path separator) are resolved with ``shutil.which``
      against PATH (the adapter's ``env['PATH']`` when given, else the process PATH);
    * anything unresolvable is returned expanded but otherwise unchanged.
    """
    spec = str(spec)
    expanded = os.path.expanduser(os.path.expandvars(spec))
    if glob.has_magic(expanded):
        candidates = []
        for match in glob.glob(expanded):
            if Path(match).is_file():
                mtime = _mtime(match)
                if mtime is not None:
                    candidates.append((mtime, match))
        return max(candidates)[1] if candidates else expanded
    if os.sep not in expanded and '/' not in expanded:
        path = (env or {}).get('PATH')
        found = shutil.which(expanded, path=path) if path else shutil.which(expanded)
        return found or expanded
    return expanded


def executable_available(executable):
    """True only for an existing regular file (directories do not count)."""
    return Path(executable).is_file()


class CLI:
    remote_cancel = True
    capabilities = ['read_only', 'edit_worktree', 'continue']

    def __init__(self, executable, env=None):
        self._pin = threading.local()
        self.env = env or {}
        self.executable = executable

    @property
    def executable(self):
        # Inside run()/diagnose() the path resolved once at entry is reused so the
        # version probe / command line never mix two different upgrades. Outside of
        # them it is re-resolved on every use so a long-running daemon follows upgrades.
        pinned = getattr(self._pin, 'value', None)
        return pinned if pinned is not None else self.resolve_executable()

    def resolve_executable(self):
        return resolve_executable(self.executable_spec, self.env)

    @contextlib.contextmanager
    def pinned_executable(self):
        if getattr(self._pin, 'value', None) is not None:
            yield self._pin.value
            return
        self._pin.value = self.resolve_executable()
        try:
            yield self._pin.value
        finally:
            self._pin.value = None

    def unavailable(self, resolved, reason=None):
        message = '\u6267\u884c\u5668\u8def\u5f84\u4e0d\u53ef\u7528: spec=' + self.executable_spec + ', resolved=' + str(resolved)
        if reason:
            message += ' (' + str(reason) + ')'
        return HubError('AGENT_UNAVAILABLE', message,
                        {'executable_spec': self.executable_spec, 'resolved_executable': str(resolved)})

    @executable.setter
    def executable(self, value):
        self.executable_spec = str(value)

    def permission_policy(self, mode):
        return {'enforced_by': 'unknown', 'enforced': False, 'allows_write': None}

    def version_command(self):
        return [self.executable, '--version']

    def diagnose(self):
        with self.pinned_executable() as resolved:
            return self._diagnose(resolved)

    def _diagnose(self, resolved):
        exists = executable_available(resolved)
        version = None
        if exists:
            try:
                version = subprocess.run(self.version_command(), env=environment(self.env),
                                         capture_output=True, text=True, timeout=10).stdout.strip()[:100]
            except (OSError, subprocess.TimeoutExpired):
                pass
        return {'available': exists, 'version': version, 'platform': os.name,
                'executable_spec': self.executable_spec, 'resolved_executable': resolved,
                'authentication': '使用 CLI 现有认证；有效性由实际任务确认',
                'capabilities': self.capabilities,
                'permissions': {mode: self.permission_policy(mode) for mode in self.capabilities if mode != 'continue'}}

    def run(self, task, cancel, event):
        with self.pinned_executable() as resolved:
            return self._run(resolved, task, cancel, event)

    def _run(self, resolved, task, cancel, event):
        if not executable_available(resolved):
            raise self.unavailable(resolved, 'not found')
        session_id = None
        def observed(kind, data):
            nonlocal session_id
            if kind == 'session':
                if data.get('source') != 'cli_init' or session_id is not None:
                    return
                session_id = data['session_id']
            event(kind, data)
        argv = self.command(task)
        try:
            rc, out, err, logs = run_process(argv, task['workspace'], environment(self.env),
                                           cancel, task['deadline'], task['log_dir'], observed, allow_hub_events=False)
        except SpawnError as error:
            # Only process creation is mapped (file vanished mid-upgrade, not executable,
            # bad format; OSError or Windows pywintypes.error). Log-dir / workspace / log IO
            # failures propagate with their own codes.
            raise self.unavailable(resolved, str(error)) from error.cause
        if rc:
            raise HubError('CLI_FAILED', '执行器退出码 ' + str(rc) + ': ' + err[-1000:],
                           {'exit_code': rc, 'artifacts': {'logs': logs}})
        try: result = self.parse(out)
        except HubError as error:
            error.details.update(exit_code=rc, artifacts={'logs': logs})
            raise
        # Final result records cannot replace the stdout initialization session.
        result['session_id'] = session_id
        if 'blocked by policy' in err or 'Failed to write' in err or 'Failed to delete' in err:
            result['warnings'] = ['执行日志包含工具失败，请检查差异和日志；CLI 回合结束不等于任务目标已完成']
        result.update(exit_code=rc, artifacts={'logs': logs})
        return result
