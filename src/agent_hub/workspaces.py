import subprocess
from pathlib import Path
from .models import HubError

# Total UTF-8 budget for edit_worktree review payload (diff + untracked excerpts).
MAX_ARTIFACT_BYTES = 1024 * 1024
_READ_CHUNK = 64 * 1024
_SINGLE_FILE_LIMIT = 1024 * 1024


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
        """Stream git diff; on overflow discard partial text and name changed paths."""
        process = subprocess.Popen(
            ['git', '-C', str(cwd), 'diff', baseline, '--'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunks, size, truncated = [], 0, False
        try:
            while True:
                block = process.stdout.read(_READ_CHUNK)
                if not block:
                    break
                text = block.decode('utf-8', errors='replace')
                piece = _utf8_len(text)
                if size + piece > budget:
                    truncated = True
                    break
                chunks.append(text)
                size += piece
            stderr = (process.stderr.read() or b'').decode('utf-8', errors='replace')
            if truncated:
                process.kill()
            code = process.wait(timeout=30)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        if code and not truncated:
            raise HubError('INVALID_WORKSPACE', 'Git 操作失败：' + stderr[:500])
        if truncated:
            names = [line for line in git(cwd, 'diff', '--name-only', baseline, '--').splitlines() if line]
            omitted = [{'path': name, 'reason': 'diff_budget_exceeded'} for name in names]
            return '', 0, True, omitted
        return ''.join(chunks), size, False, []

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
            try:
                raw = file.read_bytes()
            except OSError:
                omitted.append({'path': name, 'reason': 'unreadable'})
                truncated = True
                continue
            if b'\x00' in raw:
                omitted.append({'path': name, 'reason': 'binary', 'size_bytes': size})
                truncated = True
                continue
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                omitted.append({'path': name, 'reason': 'binary', 'size_bytes': size})
                truncated = True
                continue
            piece = header + text
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
