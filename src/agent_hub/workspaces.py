import subprocess
from pathlib import Path
from .models import HubError


def git(cwd, *args):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                            encoding='utf-8', errors='replace', timeout=30)
    if result.returncode:
        raise HubError('INVALID_WORKSPACE', 'Git 操作失败：' + result.stderr[:500])
    return result.stdout


class Workspaces:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / 'worktrees'

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

    def artifacts(self, task):
        if task['mode'] != 'edit_worktree':
            return {}
        cwd = task['workspace']
        diff = git(cwd, 'diff', task['baseline'], '--')
        untracked = git(cwd, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
        # Include untracked files in the review record without staging them.
        for name in filter(None, untracked):
            file = Path(cwd) / name
            if file.is_file() and not file.is_symlink() and file.stat().st_size <= 1024 * 1024:
                diff += '\n--- /dev/null\n+++ ' + name + '\n' + file.read_text(encoding='utf-8', errors='replace')
        return {'workspace': cwd, 'baseline': task['baseline'],
                'excluded_untracked_files': task.get('excluded_untracked_files', []),
                'changed_files': git(cwd, 'status', '--porcelain'), 'diff': diff}
