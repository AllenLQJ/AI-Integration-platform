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
            return {k: task[k] for k in ('workspace', 'baseline') if k in task}
        if task['mode'] == 'read_only':
            return {'workspace': str(cwd)}
        if git(cwd, 'status', '--porcelain').strip():
            raise HubError('DIRTY_WORKSPACE', '主目录存在未提交内容，请先明确任务基线')
        baseline = git(cwd, 'rev-parse', 'HEAD').strip()
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / task['id']
        git(cwd, 'worktree', 'add', '-b', 'hub/' + task['id'], str(target), baseline)
        return {'workspace': str(target), 'baseline': baseline}

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
                'changed_files': git(cwd, 'status', '--porcelain'), 'diff': diff}
