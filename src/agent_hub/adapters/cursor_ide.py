"""Cursor 原生界面任务桥；由具备电脑控制能力的外部 worker 驱动专用窗口。"""
import json
import threading
from pathlib import Path
from ..models import HubError


class CursorIDE:
    capabilities = ['delegate', 'edit_worktree', 'continue']
    remote_cancel = False
    cancel_scope = 'remote_session'

    def __init__(self, root):
        self.root = Path(root) / 'cursor-ide-bridge'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.lock = threading.RLock()
        self.active = {}

    def permission_policy(self, mode):
        return dict(enforced_by='cursor_ide_and_ui_worker', enforced=False,
                    allows_file_read=True, allows_write=True, allows_commands=True,
                    os_sandbox=False, workspace='git_worktree' if mode == 'edit_worktree' else 'dedicated_ide_window',
                    scope_note='delegate 不保证只读；IDE 权限仍由客户端控制，需要外部界面 worker 在线')

    def diagnose(self):
        return dict(available=Path('/Applications/Cursor.app').is_dir(),
                    transport='native_ui_bridge', capabilities=self.capabilities,
                    authentication='使用 IDE 已有登录；账号额度需真实回合确认',
                    requires_ui_worker=True, unattended=False, remote_cancel=False,
                    permissions={mode: self.permission_policy(mode) for mode in ('delegate', 'edit_worktree')})

    def save(self, job):
        path = self.root / (job['task_id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.touch(mode=0o600)
        temporary.chmod(0o600)
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

    def next_task(self, worker_id):
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 200:
            raise HubError('INVALID_ARGUMENT', '需要界面 worker 标识')
        with self.lock:
            # 专用窗口一次只派发一轮；不自动回收旧 claim 或重发。
            if any(j['status'] in {'claimed', 'dispatched', 'stop_requested'} for j in self.active.values()):
                return {'ready': False}
            # 重启后的未确认派发同样阻止新任务，需人工核对原窗口。
            for path in self.root.glob('*.json'):
                old = json.loads(path.read_text())
                if old['status'] in {'claimed', 'dispatched', 'stop_requested'} and old['task_id'] not in self.active:
                    raise HubError('IDE_STATE_UNKNOWN', '旧界面任务未核对，禁止继续派发')
            for job in self.active.values():
                if job['status'] == 'pending':
                    job.update(status='claimed', worker_id=worker_id)
                    self.save(job)
                    return {'ready': True, 'task': dict(job)}
            return {'ready': False}

    def update(self, task_id, worker_id, status, session_id=None, answer=None, error=None):
        with self.lock:
            job = self.active.get(task_id)
            if not job or job.get('worker_id') != worker_id:
                raise HubError('IDE_TASK_CONFLICT', '任务不存在或不属于该 worker')
            if status not in {'dispatched', 'succeeded', 'failed'}:
                raise HubError('INVALID_ARGUMENT', '状态需要 dispatched/succeeded/failed')
            if job['status'] not in {'claimed', 'dispatched'}:
                raise HubError('IDE_TASK_CONFLICT', '旧任务已结束或请求停止，禁止迟到结果')
            if status == 'dispatched' and job['status'] != 'claimed':
                raise HubError('IDE_TASK_CONFLICT', '禁止重复派发')
            if status in {'dispatched', 'succeeded'} and (not isinstance(session_id, str) or not session_id):
                raise HubError('INVALID_ARGUMENT', '需要实际 IDE 会话标识')
            if job.get('session_id') and session_id != job['session_id']:
                raise HubError('IDE_TASK_CONFLICT', '结果会话与派发会话不一致')
            if job.get('resume_session') and session_id != job['resume_session']:
                raise HubError('IDE_TASK_CONFLICT', '续接必须使用原 IDE 会话')
            if status == 'succeeded' and (job['status'] != 'dispatched' or not isinstance(answer, str) or not answer):
                raise HubError('INVALID_ARGUMENT', '需要已派发回合的实际完整回答')
            if status == 'failed' and (not isinstance(error, str) or not error):
                raise HubError('INVALID_ARGUMENT', '需要实际失败原因')
            job.update(status=status, session_id=session_id, answer=answer, error=error)
            self.save(job)
            return {'acknowledged': True}

    def status(self, task_id):
        with self.lock:
            job = self.active.get(task_id)
            if not job:
                raise HubError('IDE_TASK_CONFLICT', '界面任务不在当前后台中；须核对持久化记录')
            return dict(job)

    def run(self, task, cancel, event):
        job = dict(task_id=task['id'], status='pending', prompt=task['prompt'], mode=task.get('mode', 'delegate'),
                   workspace=task['workspace'], resume_session=task.get('resume_session'))
        with self.lock:
            self.active[task['id']] = job
            self.save(job)
        event('ide_bridge', {'status': 'pending', 'requires_ui_worker': True})
        while True:
            with self.lock:
                if job['status'] == 'succeeded':
                    event('session', {'session_id': job['session_id']})
                    return {'answer': job['answer'], 'session_id': job['session_id'],
                            'completion_evidence': {'transport': 'native_ui_bridge',
                                                    'worker_id': job['worker_id'], 'task_id': task['id']}}
                if job['status'] == 'failed':
                    raise HubError('CURSOR_IDE_FAILED', job['error'],
                                   {'execution_uncertain': bool(job.get('session_id'))})
                if cancel.is_set():
                    uncertain = job['status'] in {'claimed', 'dispatched'}
                    job['status'] = 'stop_requested' if uncertain else 'cancelled_before_dispatch'
                    self.save(job)
                    raise HubError('CANCELLED', '界面等待已停止；已派发回合需 worker 核对停止',
                                   {'execution_uncertain': uncertain, 'remote_cancelled': False})
            cancel.wait(.1)
