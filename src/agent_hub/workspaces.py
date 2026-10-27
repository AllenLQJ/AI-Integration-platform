import logging
import subprocess
import os
from pathlib import Path
from .models import HubError

_log = logging.getLogger(__name__)


def workspace_key(path):
    """Canonical identity shared by admission, quarantine and scheduling."""
    return os.path.normcase(str(Path(path).resolve()))

# Total UTF-8 budget for edit_worktree review payload (diff + untracked excerpts).
# Boundary: artifact_omitted path list / metadata is NOT counted against this budget;
# only diff text and untracked file body excerpts are. The result JSON as a whole is not capped.
MAX_ARTIFACT_BYTES = 1024 * 1024
_READ_CHUNK = 64 * 1024
_SINGLE_FILE_LIMIT = 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_DIFF_READ_TIMEOUT = 30


def git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                            encoding='utf-8', errors='replace', timeout=30)
    if result.returncode:
        raise HubError('INVALID_WORKSPACE', 'Git 操作失败：' + result.stderr[:500])
    return result.stdout


def _utf8_len(text):
    return len(text.encode('utf-8'))


class Workspaces:
    def __init__(self, data_dir, max_artifact_bytes=MAX_ARTIFACT_BYTES):
        self.root = Path(data_dir) / 'worktrees'
        self.max_artifact_bytes = max_artifact_bytes

    def prepare(self, task):
        cwd = Path(task['cwd']).resolve()
        if not cwd.is_dir():
            raise HubError('INVALID_WORKSPACE', '目录不存在')
        if task.get('workspace'):
            workspace = Path(task['workspace'])
            if not workspace.is_dir():
                raise HubError('INVALID_WORKSPACE', '原工作区已不存在')
            return {k: task[k] for k in ('workspace', 'baseline', 'excluded_untracked_files') if k in task}
        if task['mode'] in {'read_only', 'text_only', 'delegate'}:
            return {'workspace': str(cwd)}
        if git(cwd, 'status', '--porcelain', '--untracked-files=no').strip():
            raise HubError('DIRTY_WORKSPACE', '已跟踪文件存在未提交修改；edit_worktree 使用 HEAD 基线，不复制本地修改')
        excluded = list(filter(None, git(cwd, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')))
        baseline = git(cwd, 'rev-parse', 'HEAD').strip()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / task['id']
        git(cwd, 'worktree', 'add', '-b', 'hub/' + task['id'], str(target), baseline)
        return {'workspace': str(target), 'baseline': baseline,
                'excluded_untracked_files': excluded}

    def _bounded_diff(self, cwd, baseline, budget):
        """Stream git diff under a byte budget; kill before any blocking stderr EOF on overflow.

        Accumulates raw stdout bytes and decodes UTF-8 once (errors=replace) so codepoints
        split across read chunks are not corrupted, and non-UTF-8 bytes stay readable.
        A module-level read deadline watchdog kills a silent stalled child even while a
        blocking stdout.read is in progress. On overflow / replace-expansion past budget /
        timeout: empty diff + name-only omitted entries (diff_budget_exceeded).
        """
        import threading
        import time as _time

        process = subprocess.Popen(
            ['git', '-C', str(cwd), 'diff', baseline, '--'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunks, size = [], 0
        truncated = False
        timed_out = False
        stderr_buf = bytearray()
        deadline = _time.monotonic() + _DIFF_READ_TIMEOUT
        stop_watch = threading.Event()
        kill_lock = threading.Lock()

        def _kill_child():
            with kill_lock:
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass

        def _watchdog():
            nonlocal timed_out, truncated
            # Wait until deadline; if still running, kill so blocking reads unblock.
            remaining = deadline - _time.monotonic()
            if remaining > 0 and stop_watch.wait(remaining):
                return
            if stop_watch.is_set():
                return
            timed_out = True
            truncated = True
            _kill_child()

        def _drain_stderr():
            try:
                while True:
                    block = process.stderr.read(8192)
                    if not block:
                        break
                    remain = _STDERR_LIMIT - len(stderr_buf)
                    if remain > 0:
                        stderr_buf.extend(block[:remain])
            except Exception:
                _log.debug('diff stderr 读取中断', exc_info=True)

        watch = threading.Thread(target=_watchdog, name='hub-diff-watchdog', daemon=True)
        stderr_thread = threading.Thread(target=_drain_stderr, name='hub-diff-stderr', daemon=True)
        watch.start()
        stderr_thread.start()
        code = 0
        try:
            while True:
                if timed_out or truncated:
                    break
                if _time.monotonic() > deadline:
                    timed_out = True
                    truncated = True
                    _kill_child()
                    break
                block = process.stdout.read(_READ_CHUNK)
                if not block:
                    break
                if size + len(block) > budget:
                    truncated = True
                    _kill_child()
                    break
                chunks.append(block)
                size += len(block)
            if truncated or timed_out:
                try:
                    while process.stdout.read(_READ_CHUNK):
                        if _time.monotonic() > deadline + 2:
                            break
                except Exception:
                    _log.debug('截断后排空 diff 输出失败', exc_info=True)
                try:
                    code = process.wait(timeout=5)
                except Exception:
                    code = process.poll() if process.poll() is not None else -1
            else:
                try:
                    code = process.wait(timeout=max(0.1, deadline - _time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    truncated = True
                    _kill_child()
                    try:
                        code = process.wait(timeout=5)
                    except Exception:
                        code = -1
        finally:
            stop_watch.set()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        _log.debug('关闭 diff 输出流失败', exc_info=True)
            if process.poll() is None:
                _kill_child()
                try:
                    process.wait(timeout=5)
                except Exception:
                    _log.debug('等待 diff 进程退出失败', exc_info=True)
            stderr_thread.join(timeout=2)
            watch.join(timeout=1)

        stderr = bytes(stderr_buf).decode('utf-8', errors='replace')
        if timed_out:
            truncated = True
        if code and not truncated:
            raise HubError('INVALID_WORKSPACE', 'Git 操作失败：' + stderr[:500])
        if truncated:
            names = [line for line in git(cwd, 'diff', '--name-only', baseline, '--').splitlines() if line]
            omitted = [{'path': name, 'reason': 'diff_budget_exceeded'} for name in names]
            return '', 0, True, omitted
        # Decode once after the full bounded byte buffer is collected.
        raw = b''.join(chunks)
        text = raw.decode('utf-8', errors='replace')
        used = _utf8_len(text)
        # Replacement chars are 3 UTF-8 bytes; if expansion exceeds the documented UTF-8
        # budget, treat as truncated rather than returning an over-budget "complete" diff.
        if used > budget:
            names = [line for line in git(cwd, 'diff', '--name-only', baseline, '--').splitlines() if line]
            omitted = [{'path': name, 'reason': 'diff_budget_exceeded'} for name in names]
            return '', 0, True, omitted
        return text, used, False, []

    def artifacts(self, task):
        if task['mode'] != 'edit_worktree':
            return {}
        cwd = task['workspace']
        budget = self.max_artifact_bytes
        diff, used, truncated, omitted = self._bounded_diff(cwd, task['baseline'], budget)
        untracked = git(cwd, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
        # Include untracked files in the review record without staging them.
        for name in filter(None, untracked):
            file = Path(cwd) / name
            if file.is_symlink():
                omitted.append({'path': name, 'reason': 'symlink'})
                truncated = True
                continue
            try:
                if not file.resolve().is_relative_to(Path(cwd).resolve()):
                    omitted.append({'path': name, 'reason': 'outside_workspace'})
                    truncated = True
                    continue
            except OSError:
                omitted.append({'path': name, 'reason': 'unreadable'})
                truncated = True
                continue
            if not file.is_file():
                continue
            try:
                size = file.stat().st_size
            except OSError:
                omitted.append({'path': name, 'reason': 'unreadable'})
                truncated = True
                continue
            if size > _SINGLE_FILE_LIMIT:
                omitted.append({'path': name, 'reason': 'file_too_large', 'size_bytes': size})
                truncated = True
                continue
            remaining = budget - used
            header = '\n--- /dev/null\n+++ ' + name + '\n'
            header_bytes = _utf8_len(header)
            if size + header_bytes > remaining:
                omitted.append({'path': name, 'reason': 'budget_exceeded', 'size_bytes': size})
                truncated = True
                continue
            # Bound the read: never file.read_bytes() the whole file after a stale stat.
            body_limit = min(remaining - header_bytes, _SINGLE_FILE_LIMIT)
            try:
                with file.open('rb') as handle:
                    raw = handle.read(body_limit + 1)
            except OSError:
                omitted.append({'path': name, 'reason': 'unreadable'})
                truncated = True
                continue
            if len(raw) > body_limit:
                omitted.append({'path': name, 'reason': 'budget_exceeded',
                                'size_bytes': max(size, len(raw))})
                truncated = True
                continue
            if b'\x00' in raw:
                omitted.append({'path': name, 'reason': 'binary', 'size_bytes': size})
                truncated = True
                continue
            try:
                body = raw.decode('utf-8')
            except UnicodeDecodeError:
                omitted.append({'path': name, 'reason': 'binary', 'size_bytes': size})
                truncated = True
                continue
            piece = header + body
            piece_bytes = _utf8_len(piece)
            if used + piece_bytes > budget:
                omitted.append({'path': name, 'reason': 'budget_exceeded', 'size_bytes': size})
                truncated = True
                continue
            diff += piece
            used += piece_bytes
        result = {'workspace': cwd, 'baseline': task['baseline'],
                  'excluded_untracked_files': task.get('excluded_untracked_files', []),
                  'changed_files': git(cwd, 'status', '--porcelain'), 'diff': diff,
                  'artifact_budget_bytes': budget}
        if truncated:
            result['artifact_truncated'] = True
            result['artifact_omitted'] = omitted
            if any(item.get('reason') == 'diff_budget_exceeded' for item in omitted):
                result['diff'] = ''
        return result
