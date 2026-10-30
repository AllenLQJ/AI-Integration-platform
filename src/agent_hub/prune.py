"""Manual cleanup of finished work: bulky events, result files, logs and task worktrees.

Task rows, state events, verification records and idempotency receipts are kept,
so history and replayed requests keep working. Nothing runs unless the user asks.
"""
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from .models import HubError, TERMINAL
from .store import Store
from .verification import Verification

# Verification states that need no further action; anything else keeps the group.
SETTLED_VERIFICATION = frozenset({'passed', 'failed', 'superseded', 'not_requested'})
KEEP_EVENT_KIND = 'state'


def _groups(store):
    """Tasks connected by parent, review or work item membership form one group."""
    tasks = {task['id']: task for task in store.list()}
    parent = {ident: ident for ident in tasks}

    def find(ident):
        while parent[ident] != ident:
            parent[ident] = parent[parent[ident]]
            ident = parent[ident]
        return ident

    def join(left, right):
        if left in tasks and right in tasks:
            parent[find(left)] = find(right)

    for ident, task in tasks.items():
        join(ident, task.get('parent_id'))
        review = task.get('review_of')
        if isinstance(review, dict):
            join(ident, review.get('task_id'))
    try:
        with store.connection() as db:
            members = db.execute('SELECT work_item_id, task_id FROM work_item_members').fetchall()
    except sqlite3.OperationalError:
        members = []
    first = {}
    for member in members:
        if member['task_id'] in tasks:
            join(member['task_id'], first.setdefault(member['work_item_id'], member['task_id']))
    grouped = {}
    for ident in tasks:
        grouped.setdefault(find(ident), []).append(tasks[ident])
    return list(grouped.values())


def _verification_states(store):
    try:
        with store.connection() as db:
            return {row['task_id']: row for row in db.execute('SELECT * FROM verifications')}
    except sqlite3.OperationalError:
        return {}


def _tree_size(path):
    path = Path(path)
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())
    except OSError:
        return 0


def _result_files(root, ident):
    directory = Path(root) / 'results'
    if not directory.is_dir():
        return []
    return [directory / (ident + '.json'), *directory.glob(ident + '.reconciled.*.json')]


def _git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                          encoding='utf-8', errors='replace', timeout=60)


def _worktree(root, task):
    """The task's own worktree under the data directory, or None."""
    workspace = task.get('workspace')
    if task.get('mode') != 'edit_worktree' or not workspace:
        return None
    path = Path(workspace)
    owned = Path(root).resolve() / 'worktrees'
    try:
        if path.resolve().parent != owned:
            return None
    except OSError:
        return None
    return path


def _worktree_problem(path):
    """Why this worktree must be kept, or None when it is clean (or already gone)."""
    if not path.is_dir():
        return None
    status = _git(path, 'status', '--porcelain', '--untracked-files=all')
    if status.returncode:
        return 'worktree 状态无法读取'
    if status.stdout.strip():
        return 'worktree 有未提交改动'
    return None


def plan(store, root, older_than_days, now=None):
    if type(older_than_days) is not int or older_than_days < 1:
        raise HubError('INVALID_ARGUMENT', '--older-than 需要不小于 1 的整数天数')
    cutoff = (time.time() if now is None else now) - older_than_days * 86400
    verifications = _verification_states(store)
    selected, skipped = [], []
    for tasks in _groups(store):
        tasks.sort(key=lambda task: task['created'])
        label = tasks[0]['id']
        reason = None
        if any(task['state'] not in TERMINAL for task in tasks):
            reason = '有未结束的任务'
        elif max(task['updated'] for task in tasks) >= cutoff:
            reason = '最近仍有活动'
        else:
            for task in tasks:
                state = Verification.summary_from_row(task, verifications.get(task['id']))['state']
                if state not in SETTLED_VERIFICATION:
                    reason = '验收未完成：' + state
                    break
        worktrees = []
        if reason is None:
            for task in tasks:
                path = _worktree(root, task)
                if path is None:
                    continue
                problem = _worktree_problem(path)
                if problem:
                    reason = problem + '：' + task['id']
                    break
                worktrees.append(dict(task_id=task['id'], path=str(path), cwd=task['cwd']))
        if reason:
            skipped.append(dict(root_task_id=label, tasks=len(tasks), reason=reason))
            continue
        ids = [task['id'] for task in tasks]
        with store.connection() as db:
            marks = ','.join('?' * len(ids))
            count, size = db.execute(
                f'SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0) FROM events '
                f'WHERE task_id IN ({marks}) AND kind != ?', (*ids, KEEP_EVENT_KIND)).fetchone()
        files = [path for ident in ids for path in _result_files(root, ident) if path.exists()]
        logs = [Path(root) / 'logs' / ident for ident in ids if (Path(root) / 'logs' / ident).is_dir()]
        selected.append(dict(root_task_id=label, task_ids=ids, events=count, bytes=size
                             + sum(_tree_size(path) for path in files + logs),
                             result_files=[str(path) for path in files], log_dirs=[str(path) for path in logs],
                             worktrees=worktrees))
    return dict(older_than_days=older_than_days, groups=selected, skipped=skipped,
                total_events=sum(group['events'] for group in selected),
                total_bytes=sum(group['bytes'] for group in selected))


def _remove_worktree(item):
    path = Path(item['path'])
    if path.is_dir():
        removed = _git(item['cwd'], 'worktree', 'remove', str(path))
        if removed.returncode:
            return '移除 worktree 失败：' + removed.stderr.strip()[:200]
    # The branch only holds the baseline unless the agent committed; -d refuses unmerged work.
    branch = _git(item['cwd'], 'branch', '-d', 'hub/' + item['task_id'])
    return None if not branch.returncode else '保留分支 hub/' + item['task_id'] + '：' + branch.stderr.strip()[:200]


def _delete(path):
    path = Path(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as error:
        return '无法删除 ' + str(path) + '：' + type(error).__name__
    return None


def apply(store, root, report):
    """Carry out a plan() report. Worktrees go first; a failure keeps the rest of the group."""
    done, warnings = [], []
    for group in report['groups']:
        blocked = None
        for item in group['worktrees']:
            message = _remove_worktree(item)
            if message and message.startswith('移除'):
                blocked = message
                break
            if message:
                warnings.append(message)
        if blocked:
            warnings.append(group['root_task_id'] + ' 已跳过：' + blocked)
            continue
        for path in group['result_files'] + group['log_dirs']:
            message = _delete(path)
            if message:
                warnings.append(message)
        ids = group['task_ids']
        with store.connection(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            marks = ','.join('?' * len(ids))
            db.execute(f'DELETE FROM events WHERE task_id IN ({marks}) AND kind != ?', (*ids, KEEP_EVENT_KIND))
        done.append(group['root_task_id'])
    return dict(pruned=done, warnings=warnings)


def vacuum(path):
    connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        connection.execute('VACUUM')
    finally:
        connection.close()


def run(root, older_than_days, apply_changes=False, compact=False):
    root = Path(root)
    path = root / 'tasks.db'
    if not path.is_file():
        raise HubError('NO_DATA', '数据目录里没有 tasks.db：' + str(root))
    store = Store(path)
    report = plan(store, root, older_than_days)
    report['dry_run'] = not apply_changes
    if apply_changes:
        report.update(apply(store, root, report))
        if compact:
            try:
                vacuum(path)
                report['vacuumed'] = True
            except sqlite3.Error as error:
                report['vacuumed'] = False
                report.setdefault('warnings', []).append('VACUUM 失败：' + type(error).__name__)
    return report
