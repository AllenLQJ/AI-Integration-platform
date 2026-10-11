import json
import math
import os
import threading
import time
from pathlib import Path
from .models import HubError, TERMINAL
from .store import Store
from .gateway import Gateway, result_fields
from .workspaces import Workspaces


class Service:
    def __init__(self, data_dir, adapters, concurrency=2, per_agent_concurrency=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / 'tasks.db')
        self.store.recover()
        self.gateway = Gateway(self.root, self.store)
        self.workspaces = Workspaces(self.root)
        self.adapters = adapters
        self.concurrency = concurrency
        self.per_agent_concurrency = dict(per_agent_concurrency or {})
        self.lock = threading.RLock()
        self.active = {}
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.schedule, daemon=True)
        self.thread.start()

    def call(self, tool, arguments):
        if tool == 'wait_for_task':
            if not isinstance(arguments, dict) or set(arguments) - {'task_id', 'after_seq', 'timeout_seconds'} or 'task_id' not in arguments:
                raise HubError('INVALID_ARGUMENT', '等待参数不正确')
            return self.wait_for_task(**arguments)
        with self.lock:
            if tool == 'get_capabilities':
                return self.gateway.capabilities(self.adapters)
            if tool == 'get_artifact':
                return self.gateway.artifact(**arguments)
            if tool == 'list_agents':
                return {name: adapter.diagnose() for name, adapter in self.adapters.items()}
            if tool == 'submit_task':
                allowed = {'request_id', 'agent', 'prompt', 'cwd', 'mode', 'timeout_seconds', 'context'}
                if set(arguments) - allowed:
                    raise HubError('INVALID_ARGUMENT', '不允许设置内部任务字段')
                return self.submit(**arguments)
            if tool == 'resume_task':
                return self.resume_task(**arguments)
            if tool == 'continue_task':
                return self.continue_task(**arguments)
            if tool == 'get_events':
                return dict(self.store.events(**arguments), contract_version='1.0')
            if tool == 'get_task':
                return self.public(self.store.get(arguments['task_id']))
            if tool == 'get_result':
                task = self.store.get(arguments['task_id'])
                if task['state'] not in TERMINAL:
                    return dict(ready=False, state=task['state'], intervention=task.get('intervention'), permissions=self.permissions(task),
                                **result_fields(task['state'], {}))
                result = dict(task.get('result', {}))
                if result.get('path'):
                    result = json.loads(Path(result['path']).read_text(encoding='utf-8'))
                result.update(result_fields(task['state'], result))
                return dict(result, ready=True, state=task['state'], permissions=self.permissions(task))
            if tool == 'cancel_task':
                return self.cancel(**arguments)
            raise HubError('UNKNOWN_TOOL', '未知工具')

    def wait_for_task(self, task_id, after_seq=0, timeout_seconds=10):
        if not isinstance(task_id, str) or not task_id:
            raise HubError('INVALID_ARGUMENT', 'task_id 需要非空字符串')
        if type(after_seq) is not int or not 0 <= after_seq <= 9223372036854775807:
            raise HubError('INVALID_ARGUMENT', 'after_seq 需要非负 SQLite 整数游标')
        if (type(timeout_seconds) not in (int, float) or not 0 <= timeout_seconds <= 10
                or not math.isfinite(timeout_seconds)):
            raise HubError('INVALID_ARGUMENT', '单次等待须在 0–10 秒内')
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.stopping.is_set():
                raise HubError('SERVICE_STOPPING', '服务正在停止，请稍后按原游标续接')
            snapshot = self.store.wait_snapshot(task_id, after_seq)
            terminal = snapshot['state'] in TERMINAL
            remaining = deadline - time.monotonic()
            needs_user = snapshot['state'] == 'waiting_for_user'
            if terminal or needs_user or remaining <= 0:
                return dict(snapshot, timed_out=not terminal and not needs_user, needs_user=needs_user, contract_version='1.0')
            # Neither the global service lock nor a DB transaction is held here.
            self.stopping.wait(min(.1, remaining))

    def permissions(self, task):
        if 'permissions' in task:
            return task['permissions']
        adapter = self.adapters.get(task['agent'])
        if hasattr(adapter, 'permission_policy'):
            mode = task['mode']
            if task['agent'] == 'claude' and mode == 'read_only':
                mode = 'text_only'  # 历史任务的 read_only 禁用全部工具。
            return adapter.permission_policy(mode)
        return {'enforced_by': 'unknown', 'enforced': False, 'allows_write': None}

    def public(self, task):
        result = {k: v for k, v in task.items() if k not in {'prompt', 'resume_session'}}
        result['permissions'] = self.permissions(task)
        result['contract_version'] = '1.0'
        result_fields(task['state'], {})
        return result

    def submit(self, request_id, agent, prompt, cwd, mode, timeout_seconds=1800, context=None, **internal):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        if not isinstance(prompt, str) or not prompt or len(prompt) > 100000:
            raise HubError('INVALID_ARGUMENT', 'prompt 需要非空字符串，最多 100000 字符')
        if type(timeout_seconds) not in (int, float) or not 10 <= timeout_seconds <= 7200:
            raise HubError('INVALID_ARGUMENT', '期限须在 10–7200 秒内')
        spec = dict(agent=agent, prompt=prompt, cwd=str(Path(cwd).resolve()), mode=mode,
                    timeout_seconds=timeout_seconds, **internal)
        self.gateway.context(context)
        if context is not None:
            spec['context'] = context
        if self.store.by_request(request_id):
            return self.public(self.store.create(request_id, spec))
        if agent not in self.adapters:
            raise HubError('AGENT_UNAVAILABLE', '执行器未配置')
        adapter = self.adapters[agent]
        if mode == 'continue' or mode not in adapter.capabilities:
            raise HubError('UNSUPPORTED_MODE', '执行器不支持此模式；可提交模式：' +
                           ', '.join(m for m in adapter.capabilities if m != 'continue'))
        if not Path(cwd).is_dir():
            raise HubError('INVALID_WORKSPACE', '目录不存在')
        resolved = str(Path(cwd).resolve())
        if any(t.get('workspace_quarantined') and t.get('workspace', t['cwd']) == resolved
               for t in self.store.list()):
            raise HubError('WORKSPACE_UNCERTAIN', '此工作区存在未核对的旧执行状态')
        task = self.store.create(request_id, spec)
        policy = adapter.permission_policy(mode) if hasattr(adapter, 'permission_policy') else self.permissions(task)
        self.store.update(task['id'], permissions=policy)
        task = self.store.get(task['id'])
        return self.public(task)

    def continue_task(self, request_id, task_id, prompt, timeout_seconds=1800):
        parent = self.store.get(task_id)
        if parent['agent'] == 'muse' and parent['mode'] == 'read_only' and not self.store.by_request(request_id):
            raise HubError('LEGACY_MODE', '旧 Muse read_only 不保证只读；请使用 delegate 新建任务并确认操作范围')
        if parent['agent'] == 'claude' and parent['mode'] == 'read_only' and 'permissions' not in parent and not self.store.by_request(request_id):
            parent = dict(parent, mode='text_only')
        workspace_metadata = ({'excluded_untracked_files': parent['excluded_untracked_files']}
                              if 'excluded_untracked_files' in parent else {})
        if self.store.by_request(request_id):
            return self.submit(request_id, parent['agent'], prompt, parent['cwd'], parent['mode'],
                               timeout_seconds, parent_id=task_id, workspace=parent.get('workspace'),
                               baseline=parent.get('baseline'), resume_session=parent.get('session_id'),
                               **workspace_metadata)
        if parent.get('workspace_quarantined'):
            raise HubError('WORKSPACE_UNCERTAIN', '旧执行状态不确定，工作区已隔离；请人工核对后在新工作区提交')
        if (getattr(self.adapters[parent['agent']], 'cancel_scope', None) in {'remote_session', 'remote_hub_task'}
                or parent['agent'] == 'muse') and parent['state'] in {'interrupted', 'delivery_unknown'}:
            raise HubError('REMOTE_STATE_UNKNOWN', '原任务远端状态不确定，禁止直接续发')
        if (getattr(self.adapters[parent['agent']], 'cancel_scope', None) in {'remote_session', 'remote_hub_task'}
                and parent['state'] in {'cancelled', 'timed_out'}
                and not self.call('get_result', {'task_id': task_id}).get('remote_cancelled')):
            raise HubError('REMOTE_STATE_UNKNOWN', '原任务远端停止未确认，禁止直接续发')
        if parent['state'] not in TERMINAL:
            raise HubError('WORKSPACE_BUSY', '父任务仍在执行')
        if not parent.get('session_id'):
            raise HubError('SESSION_UNAVAILABLE', '没有可续接的原生会话')
        siblings = self.store.list({'queued', 'running', 'waiting_for_user', 'cancelling'})
        if any(t.get('workspace') == parent.get('workspace') for t in siblings):
            # Idempotent replay of the exact same child is allowed.
            if not any(t['request_id'] == request_id for t in siblings):
                raise HubError('WORKSPACE_BUSY', '工作区已有另一轮任务')
        return self.submit(request_id, parent['agent'], prompt, parent['cwd'], parent['mode'],
                           timeout_seconds, parent_id=task_id, workspace=parent['workspace'],
                           baseline=parent.get('baseline'), resume_session=parent['session_id'],
                           **workspace_metadata)

    def resume_task(self, request_id, task_id, intervention_id):
        """确认人工处理已完成；恢复同一任务，不创建子任务。"""
        if any(not isinstance(v, str) or not v or len(v) > 200
               for v in (request_id, task_id, intervention_id)):
            raise HubError('INVALID_ARGUMENT', '恢复参数需要非空字符串，最多 200 字符')
        task = self.store.get(task_id)
        receipts = task.get('resume_receipts', {})
        if request_id in receipts:
            if receipts[request_id] != intervention_id:
                raise HubError('IDEMPOTENCY_CONFLICT', '恢复请求内容发生变化')
            return dict(task_id=task_id, intervention_id=intervention_id, acknowledged=True)
        if intervention_id in receipts.values():
            receipts[request_id] = intervention_id
            self.store.update(task_id, resume_receipts=receipts)
            return dict(task_id=task_id, intervention_id=intervention_id, acknowledged=True)
        intervention = task.get('intervention') or {}
        if (task['state'] != 'waiting_for_user' or intervention.get('intervention_id') != intervention_id
                or task_id not in self.active or task['agent'] != 'muse'):
            raise HubError('NOT_WAITING_FOR_USER', '任务没有等待此人工处理；请先查询原任务')
        if time.time() >= intervention['wait_deadline']:
            raise HubError('USER_WAIT_EXPIRED', '人工等待期限已到，不续发')
        # 相同介入的不同 request_id 也只写一次；worker 在发送前移走指令。
        if intervention_id not in receipts.values():
            path = self.root / 'logs' / task_id / 'muse-resume.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(intervention_id=intervention_id)), encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(path)
        receipts[request_id] = intervention_id
        self.store.update(task_id, state='running', resume_receipts=receipts,
                          intervention=dict(intervention, resume_pending=True))
        return dict(task_id=task_id, intervention_id=intervention_id, acknowledged=True)

    def cancel(self, task_id, reason='用户取消'):
        task = self.store.get(task_id)
        remote = self.adapters[task['agent']].remote_cancel
        if task['state'] == 'queued':
            self.store.finish(task_id, 'cancelled', {'error': reason, 'remote_cancelled': False,
                                                    'cancel_scope': 'not_started'})
        elif task['state'] not in TERMINAL:
            self.store.update(task_id, state='cancelling', cancel_reason=reason)
            if task_id in self.active:
                self.active[task_id]['cancel'].set()
        scope = getattr(self.adapters[task['agent']], 'cancel_scope',
                        'local_process_tree' if remote else 'local_wait_only')
        if task['state'] == 'queued':
            scope = 'not_started'
        result = self.call('get_result', {'task_id': task_id}) if task['state'] in TERMINAL else {}
        return {'contract_version': '1.0', 'task_id': task_id, 'state': self.store.get(task_id)['state'],
                'remote_cancelled': result.get('remote_cancelled', False),
                'cancel_scope': result.get('cancel_scope', scope)}

    def schedule(self):
        while not self.stopping.wait(.05):
            with self.lock:
                from collections import Counter
                used_agents = Counter(a['agent'] for a in self.active.values())
                used_workspaces = {a['workspace'] for a in self.active.values()}
                for task in self.store.list({'queued'}):
                    if len(self.active) >= self.concurrency:
                        break
                    actual_workspace = task.get('workspace') or (
                        str(self.workspaces.root / task['id']) if task['mode'] == 'edit_worktree' else task['cwd'])
                    workspace_key = os.path.normcase(str(Path(actual_workspace).resolve()))
                    quarantined = {os.path.normcase(str(Path(t.get('workspace') or t['cwd']).resolve()))
                                   for t in self.store.list() if t.get('workspace_quarantined')}
                    if workspace_key in quarantined:
                        self.store.update(task['id'], workspace=actual_workspace, workspace_quarantined=True)
                        self.store.finish(task['id'], 'interrupted', {
                            'error_code': 'WORKSPACE_UNCERTAIN',
                            'error': '工作区旧执行状态不确定，排队任务未启动；请人工核对后使用新工作区'})
                        continue
                    if (used_agents[task['agent']] >= self.per_agent_concurrency.get(task['agent'], 1)
                            or workspace_key in used_workspaces):
                        continue
                    cancel = threading.Event()
                    worker = threading.Thread(target=self.execute, args=(task, cancel), daemon=True)
                    self.active[task['id']] = dict(agent=task['agent'], workspace=workspace_key,
                                                   cancel=cancel, thread=worker)
                    used_agents[task['agent']] += 1
                    used_workspaces.add(workspace_key)
                    self.store.update(task['id'], state='running')
                    worker.start()

    def execute(self, task, cancel):
        ident = task['id']
        deadline = time.time() + task['timeout_seconds']
        timed_out = threading.Event()
        task['deadline'] = deadline
        watchdog_stop = threading.Event()
        def expire():
            while not watchdog_stop.wait(.1):
                if time.time() >= task['deadline']:
                    timed_out.set(); cancel.set(); return
        timer = threading.Thread(target=expire)
        timer.daemon = True
        timer.start()
        try:
            prepared = self.workspaces.prepare(task)
            task.update(prepared, deadline=deadline, log_dir=str(self.root / 'logs' / ident),
                        media_artifact_root=str(self.gateway.artifact_root))
            self.store.update(ident, **prepared)
            self.store.event(ident, 'state', {'state': 'running'})
            def event(kind, data):
                if kind == 'session' and data.get('session_id'):
                    self.store.update(ident, session_id=data['session_id'])
                if kind == 'accepted':
                    self.store.update(ident, delivery='accepted', baseline=data.get('baseline'))
                if kind in {'waiting_for_user', 'user_wait_resumed'}:
                    with self.lock:
                        current = self.store.get(ident)
                        if current['state'] not in TERMINAL and current['state'] != 'cancelling':
                            task['deadline'] = data['execution_deadline']
                            self.store.update(ident, state='waiting_for_user' if kind == 'waiting_for_user' else 'running',
                                              intervention=self.store.safe(data) if kind == 'waiting_for_user' else None)
                self.store.event(ident, kind, data)
            result = self.adapters[task['agent']].run(task, cancel, event)
            if result.get('session_id'):
                self.store.update(ident, session_id=result['session_id'])
            result['media_artifacts'] = self.gateway.register(ident, result.get('media_artifacts', []))
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
            watchdog_stop.set()
            timer.join(1)
        if state != 'succeeded' and task.get('workspace') and task['mode'] == 'edit_worktree':
            try:
                result['artifacts'] = dict(result.get('artifacts', {}), **self.workspaces.artifacts(task))
            except HubError as error:
                result['artifact_error'] = str(error)
        if not self.adapters[task['agent']].remote_cancel and state in {'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}:
            result.update(remote_cancelled=False, warning='仅停止本地等待；远端可能继续执行')
        elif getattr(self.adapters[task['agent']], 'cancel_scope', None) in {'remote_session', 'remote_hub_task'} and state in {'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}:
            result.setdefault('remote_cancelled', False)
            if not result['remote_cancelled']:
                result.setdefault('warning', '远端停止未确认；须核对专用 Bot 和私有状态文件')
        result['permissions'] = self.permissions(task)
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
            adapter = self.adapters[active['agent']]
            active['thread'].join(30 if getattr(adapter, 'cancel_scope', None) in {'remote_session', 'remote_hub_task'} else 20)
