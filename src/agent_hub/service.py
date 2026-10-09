import json
import threading
import time
from pathlib import Path
from .models import HubError, TERMINAL
from .store import Store
from .workspaces import Workspaces


class Service:
    def __init__(self, data_dir, adapters, concurrency=2):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / 'tasks.db')
        self.store.recover()
        self.workspaces = Workspaces(self.root)
        self.adapters = adapters
        self.concurrency = concurrency
        self.lock = threading.RLock()
        self.active = {}
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.schedule, daemon=True)
        self.thread.start()

    def call(self, tool, arguments):
        with self.lock:
            if tool == 'list_agents':
                return {name: adapter.diagnose() for name, adapter in self.adapters.items()}
            if tool == 'submit_task':
                allowed = {'request_id', 'agent', 'prompt', 'cwd', 'mode', 'timeout_seconds'}
                if set(arguments) - allowed:
                    raise HubError('INVALID_ARGUMENT', '不允许设置内部任务字段')
                return self.submit(**arguments)
            if tool == 'continue_task':
                return self.continue_task(**arguments)
            if tool == 'get_events':
                return self.store.events(**arguments)
            if tool == 'get_task':
                return self.public(self.store.get(arguments['task_id']))
            if tool == 'get_result':
                task = self.store.get(arguments['task_id'])
                if task['state'] not in TERMINAL:
                    return {'ready': False, 'state': task['state']}
                result = dict(task.get('result', {}))
                if result.get('path'):
                    result = json.loads(Path(result['path']).read_text(encoding='utf-8'))
                return dict(result, ready=True, state=task['state'])
            if tool == 'cancel_task':
                return self.cancel(**arguments)
            raise HubError('UNKNOWN_TOOL', '未知工具')

    @staticmethod
    def public(task):
        return {k: v for k, v in task.items() if k not in {'prompt', 'resume_session'}}

    def submit(self, request_id, agent, prompt, cwd, mode, timeout_seconds=1800, **internal):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        if not isinstance(prompt, str) or not prompt or len(prompt) > 100000:
            raise HubError('INVALID_ARGUMENT', 'prompt 需要非空字符串，最多 100000 字符')
        if type(timeout_seconds) not in (int, float) or not 10 <= timeout_seconds <= 7200:
            raise HubError('INVALID_ARGUMENT', '期限须在 10–7200 秒内')
        spec = dict(agent=agent, prompt=prompt, cwd=str(Path(cwd).resolve()), mode=mode,
                    timeout_seconds=timeout_seconds, **internal)
        if self.store.by_request(request_id):
            return self.public(self.store.create(request_id, spec))
        if agent not in self.adapters:
            raise HubError('AGENT_UNAVAILABLE', '执行器未配置')
        adapter = self.adapters[agent]
        if mode not in adapter.capabilities:
            raise HubError('UNSUPPORTED_MODE', '执行器不支持此模式')
        if not Path(cwd).is_dir():
            raise HubError('INVALID_WORKSPACE', '目录不存在')
        resolved = str(Path(cwd).resolve())
        if any(t.get('workspace_quarantined') and t.get('workspace', t['cwd']) == resolved
               for t in self.store.list()):
            raise HubError('WORKSPACE_UNCERTAIN', '此工作区存在未核对的旧执行状态')
        task = self.store.create(request_id, spec)
        return self.public(task)

    def continue_task(self, request_id, task_id, prompt, timeout_seconds=1800):
        parent = self.store.get(task_id)
        if self.store.by_request(request_id):
            return self.submit(request_id, parent['agent'], prompt, parent['cwd'], parent['mode'],
                               timeout_seconds, parent_id=task_id, workspace=parent.get('workspace'),
                               baseline=parent.get('baseline'), resume_session=parent.get('session_id'))
        if parent.get('workspace_quarantined'):
            raise HubError('WORKSPACE_UNCERTAIN', '旧执行状态不确定，工作区已隔离；请人工核对后在新工作区提交')
        if parent['agent'] == 'muse' and parent['state'] in {'interrupted', 'delivery_unknown'}:
            raise HubError('REMOTE_STATE_UNKNOWN', 'Muse 原任务远端状态不确定，禁止直接续发')
        if parent['state'] not in TERMINAL:
            raise HubError('WORKSPACE_BUSY', '父任务仍在执行')
        if not parent.get('session_id'):
            raise HubError('SESSION_UNAVAILABLE', '没有可续接的原生会话')
        siblings = self.store.list({'queued', 'running', 'cancelling'})
        if any(t.get('workspace') == parent.get('workspace') for t in siblings):
            # Idempotent replay of the exact same child is allowed.
            if not any(t['request_id'] == request_id for t in siblings):
                raise HubError('WORKSPACE_BUSY', '工作区已有另一轮任务')
        return self.submit(request_id, parent['agent'], prompt, parent['cwd'], parent['mode'],
                           timeout_seconds, parent_id=task_id, workspace=parent['workspace'],
                           baseline=parent.get('baseline'), resume_session=parent['session_id'])

    def cancel(self, task_id, reason='用户取消'):
        task = self.store.get(task_id)
        remote = self.adapters[task['agent']].remote_cancel
        if task['state'] == 'queued':
            self.store.finish(task_id, 'cancelled', {'error': reason, 'remote_cancelled': False})
        elif task['state'] not in TERMINAL:
            self.store.update(task_id, state='cancelling', cancel_reason=reason)
            if task_id in self.active:
                self.active[task_id]['cancel'].set()
        return {'task_id': task_id, 'state': self.store.get(task_id)['state'],
                'remote_cancelled': False, 'cancel_scope': 'local_process_tree' if remote else 'local_wait_only'}

    def schedule(self):
        while not self.stopping.wait(.05):
            with self.lock:
                used_agents = {a['agent'] for a in self.active.values()}
                used_workspaces = {a['workspace'] for a in self.active.values()}
                for task in self.store.list({'queued'}):
                    if len(self.active) >= self.concurrency:
                        break
                    if task['agent'] in used_agents or task.get('workspace', task['cwd']) in used_workspaces:
                        continue
                    cancel = threading.Event()
                    worker = threading.Thread(target=self.execute, args=(task, cancel), daemon=True)
                    self.active[task['id']] = dict(agent=task['agent'], workspace=task.get('workspace', task['cwd']),
                                                   cancel=cancel, thread=worker)
                    used_agents.add(task['agent'])
                    used_workspaces.add(task.get('workspace', task['cwd']))
                    self.store.update(task['id'], state='running')
                    worker.start()

    def execute(self, task, cancel):
        ident = task['id']
        deadline = time.time() + task['timeout_seconds']
        timed_out = threading.Event()
        def expire():
            timed_out.set(); cancel.set()
        timer = threading.Timer(task['timeout_seconds'], expire)
        timer.daemon = True
        timer.start()
        try:
            prepared = self.workspaces.prepare(task)
            task.update(prepared, deadline=deadline, log_dir=str(self.root / 'logs' / ident))
            self.store.update(ident, **prepared)
            self.store.event(ident, 'state', {'state': 'running'})
            def event(kind, data):
                if kind == 'session' and data.get('session_id'):
                    self.store.update(ident, session_id=data['session_id'])
                if kind == 'accepted':
                    self.store.update(ident, delivery='accepted', baseline=data.get('baseline'))
                self.store.event(ident, kind, data)
            result = self.adapters[task['agent']].run(task, cancel, event)
            if result.get('session_id'):
                self.store.update(ident, session_id=result['session_id'])
            result['artifacts'] = dict(result.get('artifacts', {}), **self.workspaces.artifacts(task))
            state = 'timed_out' if timed_out.is_set() else 'cancelled' if cancel.is_set() else 'succeeded'
        except HubError as error:
            state = {'CANCELLED': 'cancelled', 'TIMED_OUT': 'timed_out',
                     'DELIVERY_UNKNOWN': 'delivery_unknown', 'REMOTE_STATE_UNKNOWN': 'interrupted'}.get(error.code, 'failed')
            if timed_out.is_set() and state == 'cancelled':
                state = 'timed_out'
            result = dict(error.details, error=str(error), error_code=error.code)
        except Exception as error:
            state, result = 'failed', {'error': type(error).__name__, 'error_code': 'EXECUTION_ERROR'}
        finally:
            timer.cancel()
        if state != 'succeeded' and task.get('workspace') and task['mode'] == 'edit_worktree':
            try:
                result['artifacts'] = dict(result.get('artifacts', {}), **self.workspaces.artifacts(task))
            except HubError as error:
                result['artifact_error'] = str(error)
        if not self.adapters[task['agent']].remote_cancel and state in {'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}:
            result.update(remote_cancelled=False, warning='仅停止本地等待；远端可能继续执行')
        result = self.store.safe(result)
        path = self.root / 'results' / (ident + '.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        with self.lock:
            if state == 'succeeded' and self.store.get(ident)['state'] == 'cancelling':
                state = 'timed_out' if timed_out.is_set() else 'cancelled'
            self.store.finish(ident, state, {'path': str(path)})
            self.active.pop(ident, None)

    def close(self):
        self.stopping.set()
        self.thread.join(5)
        with self.lock:
            workers = list(self.active.values())
            for active in workers:
                active['cancel'].set()
        for active in workers:
            active['thread'].join(20)
