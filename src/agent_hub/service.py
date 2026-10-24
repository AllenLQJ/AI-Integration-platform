import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from .models import HubError, TERMINAL
from .store import Store
from .gateway import Gateway, result_fields
from .workspaces import Workspaces
from .verification import Verification, validate


class Service:
    def __init__(self, data_dir, adapters, concurrency=2, per_agent_concurrency=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        from .config import verification_options
        verification_config = verification_options(self.root)
        self.store = Store(self.root / 'tasks.db')
        self.store.recover()
        self.gateway = Gateway(self.root, self.store)
        self.stopping = threading.Event()
        self.verification = Verification(self.store, self.gateway, self.stopping)
        for key, attribute in {'max_files': 'max_files', 'max_bytes': 'max_total_bytes',
                               'max_entries': 'max_entries', 'max_seconds': 'max_scan_seconds'}.items():
            setattr(self.verification, attribute, verification_config[key])
        self.verification_join_seconds = verification_config['join_seconds']
        self.max_auto_rework = verification_config['max_auto_rework']
        self.workspaces = Workspaces(self.root)
        self.adapters = adapters
        self.concurrency = concurrency
        self.per_agent_concurrency = dict(per_agent_concurrency or {})
        self.lock = threading.RLock()
        self.active = {}
        self.verification_active = {}
        self.verification_retry_at = 0
        self.auto_rework_pending = set()
        self.auto_rework_failures = {}
        self.auto_rework_retry_at = {}
        from .nodes import Nodes
        self.nodes = Nodes(self)
        self.verification.adapters = self.adapters
        from .review import Reviewer
        self.reviewer = Reviewer(self)
        # 重启只恢复已持久化的审核：已建任务不重发，终态任务按收据幂等回写。
        self.review_pending = set(self.reviewer.recover())
        self.review_failures = {}
        self.review_retry_at = {}
        self.recover_auto_rework()
        self.thread = threading.Thread(target=self.schedule, daemon=True)
        self.thread.start()

    def call(self, tool, arguments):
        if tool == 'transfer_artifact':
            return self.nodes.transfer(**arguments)
        if tool == 'list_nodes':
            return self.nodes.list()
        if tool == 'wait_for_task':
            if not isinstance(arguments, dict) or set(arguments) - {'task_id', 'after_seq', 'timeout_seconds'} or 'task_id' not in arguments:
                raise HubError('INVALID_ARGUMENT', '等待参数不正确')
            return self.wait_for_task(**arguments)
        if (tool in {'submit_task', 'continue_task', 'switch_executor'} and isinstance(arguments, dict)
                and isinstance(arguments.get('request_id'), str) and arguments['request_id'].startswith('auto-review:')):
            raise HubError('INVALID_ARGUMENT', 'auto-review: 请求键保留给内部审核服务')
        controlled = {'continue_task', 'cancel_task', 'verify_task', 'record_verification', 'switch_executor', 'record_external_verification'}
        if tool == 'claim_controller' or tool in controlled or tool == 'record_external_verification':
            if any(k.startswith('_') for k in arguments):
                raise HubError('INVALID_ARGUMENT', '不允许设置内部任务字段')
            root = self.verification.root_id(arguments['task_id'])
            with self.verification.task_lock(root):
                args = dict(arguments)
                if tool in controlled:
                    self.verification.check_controller(args['task_id'], args.pop('controller_id', None), args.pop('controller_epoch', None))
                if tool == 'claim_controller':
                    return self.verification.claim(**args)
                if tool in {'record_verification', 'record_external_verification'} and isinstance(args.get('verifier_id'), str) and (
                        args['verifier_id'] in self.adapters or args['verifier_id'].startswith('hub-')):
                    raise HubError('INVALID_ARGUMENT', '执行器不能写验收结果，hub- 前缀保留给内部验证器')
                if tool == 'record_verification':
                    return self.verification.record(**args)
                if tool == 'record_external_verification':
                    return self.verification.external(**args)
                if tool == 'verify_task':
                    result = self.verification.request(args['request_id'], dict(tool=tool, task_id=args['task_id']),
                                                       lambda: self.verification.run(args['task_id'], args['request_id']))
                    self.reviewer.process(args['task_id'])
                    self.auto_rework(args['task_id'])
                    return result
                with self.verification.task_lock(args['task_id']), self.lock:
                    if tool == 'continue_task':
                        return self.continue_task(**args)
                    if tool == 'switch_executor':
                        return self.switch_executor(**args)
                    return self.cancel(**args)
        if tool == 'list_agents':
            with self.lock:
                adapters = list(self.adapters.items())
            return {name: adapter.diagnose() for name, adapter in adapters}
        with self.lock:
            if tool == 'get_work_item':
                return self.verification.work_item(**arguments)
            if tool in {'ide_next_task', 'ide_task_update', 'ide_task_status'}:
                from .adapters.cursor_ide import CursorIDE
                adapter = self.adapters.get('cursor')
                if not isinstance(adapter, CursorIDE):
                    raise HubError('AGENT_UNAVAILABLE', 'Cursor IDE 桥接未配置')
                method = {'ide_next_task': adapter.next_task, 'ide_task_update': adapter.update,
                          'ide_task_status': adapter.status}[tool]
                return method(**arguments)
            if tool == 'get_capabilities':
                return self.gateway.capabilities(self.adapters)
            if tool == 'get_artifact':
                if set(arguments) - {'artifact_id', 'target_host_id', 'target_node_id'}:
                    raise HubError('INVALID_ARGUMENT', '产物查询字段不正确')
                args = dict(arguments)
                target_node = args.pop('target_node_id', None)
                if target_node is not None:
                    node = self.nodes.get(target_node)
                    args['target_host_id'] = node['host_id']
                    args['target_transport'] = node['transport']
                return self.nodes.artifact(**args)
            if tool == 'submit_task':
                allowed = {'request_id', 'agent', 'prompt', 'cwd', 'mode', 'timeout_seconds', 'context', 'acceptance', 'node_id'}
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
                    return dict(ready=False, state=task['state'], intervention=task.get('intervention'), permissions=self.permissions(task), **self.verification.fields(task),
                                **result_fields(task['state'], {}))
                result = dict(task.get('result', {}))
                if result.get('path'):
                    result = json.loads(Path(result['path']).read_text(encoding='utf-8'))
                result.update(result_fields(task['state'], result))
                return dict(result, ready=True, state=task['state'], permissions=self.permissions(task), **self.verification.fields(task))
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
        result.update(self.verification.fields(task))
        result_fields(task['state'], {})
        return result

    def submit(self, request_id, agent, prompt, cwd, mode, timeout_seconds=1800, context=None, acceptance=None, node_id=None, **internal):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise HubError('INVALID_ARGUMENT', 'request_id 需要非空字符串，最多 200 字符')
        if not isinstance(prompt, str) or not prompt or len(prompt) > 100000:
            raise HubError('INVALID_ARGUMENT', 'prompt 需要非空字符串，最多 100000 字符')
        if type(timeout_seconds) not in (int, float) or not 10 <= timeout_seconds <= 7200:
            raise HubError('INVALID_ARGUMENT', '期限须在 10–7200 秒内')
        spec = dict(agent=agent, prompt=prompt, cwd=str(Path(cwd).resolve()), mode=mode,
                    timeout_seconds=timeout_seconds, **internal)
        if node_id is not None:
            spec['node_id'] = node_id
        validate(acceptance)
        if acceptance is not None:
            if acceptance.get('review') and acceptance['review']['agent'] == agent:
                raise HubError('INVALID_ARGUMENT', '执行器不能审核自己的产物；请选择不同的审核端')
            spec['acceptance'] = acceptance
        self.gateway.context(context)
        if context is not None:
            spec['context'] = context
        if self.store.by_request(request_id):
            return self.public(self.store.create(request_id, spec))
        if node_id is not None:
            node = self.nodes.get(node_id)
            if mode not in node['capabilities'].get(agent, []):
                raise HubError('NODE_UNAVAILABLE', '节点未声明对应执行器与模式')
            if node['transport'] == 'local' and node_id != self.nodes.local_id:
                raise HubError('NODE_UNAVAILABLE', '接收节点没有本机任务执行路由')
        if agent not in self.adapters:
            raise HubError('AGENT_UNAVAILABLE', '执行器未配置')
        adapter = self.adapters[agent]
        if mode == 'continue' or mode not in adapter.capabilities:
            raise HubError('UNSUPPORTED_MODE', '执行器不支持此模式；可提交模式：' +
                           ', '.join(m for m in adapter.capabilities if m != 'continue'))
        if not Path(cwd).is_dir():
            raise HubError('INVALID_WORKSPACE', '目录不存在')
        resolved = str(Path(cwd).resolve())
        if any(t.get('workspace', t['cwd']) == resolved for t in self.store.list_quarantined()):
            raise HubError('WORKSPACE_UNCERTAIN', '此工作区存在未核对的旧执行状态')
        task = self.store.create(request_id, spec)
        policy = adapter.permission_policy(mode) if hasattr(adapter, 'permission_policy') else self.permissions(task)
        self.store.update(task['id'], permissions=policy)
        task = self.store.get(task['id'])
        self.verification.track(task)
        if task.get('parent_id') and task.get('acceptance'):
            self.store.event(task['parent_id'], 'session', dict(new_attempt_id=task['id'], native_session_shared=bool(task.get('resume_session'))))
        return self.public(task)

    def continue_task(self, request_id, task_id, prompt, timeout_seconds=1800, _auto_rework_run_id=None):
        parent = self.store.get(task_id)
        if parent['agent'] == 'muse' and parent['mode'] == 'read_only' and not self.store.by_request(request_id):
            raise HubError('LEGACY_MODE', '旧 Muse read_only 不保证只读；请使用 delegate 新建任务并确认操作范围')
        if parent['agent'] == 'claude' and parent['mode'] == 'read_only' and 'permissions' not in parent and not self.store.by_request(request_id):
            parent = dict(parent, mode='text_only')
        workspace_metadata = ({'excluded_untracked_files': parent['excluded_untracked_files']}
                              if 'excluded_untracked_files' in parent else {})
        workspace_metadata['acceptance'] = parent.get('acceptance')
        if parent.get('node_id'):
            workspace_metadata['node_id'] = parent['node_id']
        if _auto_rework_run_id is not None:
            workspace_metadata['auto_rework_run_id'] = _auto_rework_run_id
        if self.store.by_request(request_id):
            return self.submit(request_id, parent['agent'], prompt, parent['cwd'], parent['mode'],
                               timeout_seconds, parent_id=task_id, workspace=parent.get('workspace'),
                               baseline=parent.get('baseline'), resume_session=parent.get('session_id'),
                               **workspace_metadata)
        adapter = self.adapters.get(parent['agent'])
        if getattr(adapter, 'api_version', None) == 1 and 'continue' not in adapter.capabilities:
            raise HubError('UNSUPPORTED_MODE', '执行器未声明会话续接能力')
        if parent.get('acceptance'):
            current = self.verification.work_item(task_id)['current_task_id']
            if current != task_id or any(t.get('parent_id') == task_id for t in self.store.list()):
                raise HubError('ATTEMPT_SUPERSEDED', '只能从当前尝试续接返工', {'current_task_id': current})
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

    def switch_executor(self, request_id, task_id, agent, mode, prompt):
        if not isinstance(prompt, str) or not 0 < len(prompt) <= 100000:
            raise HubError('INVALID_ARGUMENT', 'prompt 需要非空字符串，最多 100000 字符')
        parent = self.store.get(task_id)
        def apply():
            existing = self.store.by_request(request_id)
            if existing:
                return self.public(existing)
            item = self.verification.work_item(task_id)
            if item['current_task_id'] != task_id:
                raise HubError('ATTEMPT_SUPERSEDED', '只能更换当前尝试的执行器')
            if parent['state'] not in TERMINAL:
                raise HubError('WORKSPACE_BUSY', '当前尝试仍在执行，请先取消并确认终止')
            if parent.get('workspace_quarantined'):
                raise HubError('WORKSPACE_UNCERTAIN', '工作区已隔离')
            remote_scope = getattr(self.adapters[parent['agent']], 'cancel_scope', None)
            if (remote_scope in {'remote_session', 'remote_hub_task'} or parent['agent'] == 'muse') and parent['state'] in {'interrupted', 'delivery_unknown'}:
                raise HubError('REMOTE_STATE_UNKNOWN', '旧尝试远端状态不确定，禁止交接重发')
            if remote_scope in {'remote_session', 'remote_hub_task'} and parent['state'] in {'cancelled', 'timed_out'} and not self.call('get_result', {'task_id': task_id}).get('remote_cancelled'):
                raise HubError('REMOTE_STATE_UNKNOWN', '原尝试远端停止未确认')
            root = self.store.get(item['root_task_id'])
            current = next(a for a in item['attempts'] if a['task_id'] == task_id)
            summary = dict(original_goal=root['prompt'][:2000], criteria=parent.get('acceptance'),
                           failures=[c for c in current['verification'].get('checks', []) if c['outcome'] != 'pass'],
                           artifacts=current['artifacts'], recent_events=current['events'])
            from .verification import safe
            handoff = '执行器交接摘要（这是新的原生会话）：\n' + json.dumps(safe(self.store.safe(summary)), ensure_ascii=False)[:16000]
            workspace_metadata = {}
            if mode != 'edit_worktree' or parent['mode'] == 'edit_worktree':
                workspace_metadata = {k: parent[k] for k in ('workspace', 'baseline', 'excluded_untracked_files') if k in parent}
            return self.submit(request_id, agent, handoff + '\n用户本轮指令：\n' + prompt,
                               parent['cwd'], mode, parent['timeout_seconds'], acceptance=parent.get('acceptance'),
                               parent_id=task_id, **workspace_metadata)
        return self.verification.request(request_id, dict(tool='switch_executor', task_id=task_id,
                                         agent=agent, mode=mode, prompt=prompt), apply)

    @staticmethod
    def auto_rework_ready(task, summary):
        from .review import verifier_name
        checks = summary.get('checks', [])
        failed = [c for c in checks if c['outcome'] == 'fail']
        criteria = {c['id']: c for c in (task.get('acceptance') or {}).get('criteria', [])}
        review = summary.get('review') or {}
        def trusted(c):
            name = c.get('verifier', {}).get('name')
            if criteria.get(c['criterion_id'], {}).get('kind') != 'content_review':
                return name == 'hub-deterministic'
            # 仅接受受信审核服务按本次审核 run 严格校验后写入的 fail。
            mark = c.get('auto_review') or {}
            return (name == verifier_name(review.get('agent')) and review.get('state') == 'failed'
                    and (name == 'hub-claude-review' or c.get('verifier', {}).get('agent') == review.get('agent'))
                    and bool(review.get('review_run_id')) and mark.get('review_run_id') == review['review_run_id'])
        def deferred(c):
            return c.get('review_deferred') and c.get('verifier', {}).get('name') == 'hub-deterministic'
        return (summary['state'] == 'rework_required' and bool(failed)
                and bool(summary.get('verification_run_id'))
                and not any(c['outcome'] == 'inconclusive' and not deferred(c) for c in checks)
                and all(trusted(c) for c in failed))

    def recover_auto_rework(self):
        if self.max_auto_rework == 0:
            return
        for task in self.store.list({'succeeded'}):
            summary = self.verification.summary(task)
            if not self.auto_rework_ready(task, summary) or self.verification.remote_workspace(task):
                continue
            if self.store.by_request('auto-rework:' + summary['verification_run_id']):
                continue
            item = self.verification.work_item(task['id'])
            count = sum(bool(self.store.get(a['task_id']).get('auto_rework_run_id')) for a in item['attempts'])
            if item['current_task_id'] == task['id'] and count < self.max_auto_rework:
                with self.lock:
                    self.auto_rework_pending.add(task['id'])

    def auto_rework(self, task_id):
        root = self.verification.root_id(task_id)
        with self.verification.task_lock(root), self.verification.task_lock(task_id), self.lock:
            task = self.store.get(task_id)
            summary = self.verification.summary(task)
            checks = summary.get('checks', [])
            failed = [c for c in checks if c['outcome'] == 'fail']
            if not self.auto_rework_ready(task, summary) or self.verification.remote_workspace(task):
                return
            item = self.verification.work_item(task_id)
            if item['current_task_id'] != task_id:
                return
            count = sum(bool(self.store.get(a['task_id']).get('auto_rework_run_id')) for a in item['attempts'])
            run_id = summary['verification_run_id']
            request_id = 'auto-rework:' + run_id
            if self.store.by_request(request_id):
                return
            if count >= self.max_auto_rework:
                if summary.get('auto_rework_reason') != '自动返工已达到配置上限':
                    summary['auto_rework_reason'] = '自动返工已达到配置上限'
                    with self.store.connection() as db:
                        data = dict(summary); data.pop('state', None)
                        db.execute('UPDATE verifications SET data=? WHERE task_id=?', (json.dumps(data), task_id))
                    self.store.event(task_id, 'verification', dict(state='rework_required', reason=summary['auto_rework_reason'], max_auto_rework=self.max_auto_rework))
                return
            prompt = json.dumps(dict(failures=[dict(criterion_id=c['criterion_id'], reason=c['reason'], evidence=c.get('evidence', {})) for c in failed],
                                     artifacts=[dict(logical_id=a['logical_id'], sha256=a['sha256']) for a in self.verification.artifacts(task_id)]), ensure_ascii=False)
            try:
                self.continue_task(request_id, task_id, prompt, task['timeout_seconds'], _auto_rework_run_id=run_id)
            except HubError as error:
                self.store.event(task_id, 'verification', dict(state='rework_required', reason='无法自动返工：' + error.code))

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
            if isinstance(task.get('review_of'), dict) and task['review_of'].get('task_id'):
                with self.lock:
                    self.review_pending.add(task['review_of']['task_id'])
        elif task['state'] not in TERMINAL:
            self.store.update(task_id, state='cancelling', cancel_reason=reason)
            if task_id in self.active:
                self.active[task_id]['cancel'].set()
        scope = getattr(self.adapters[task['agent']], 'cancel_scope',
                        'local_process_tree' if remote else 'local_wait_only')
        if task['state'] == 'queued':
            scope = 'not_started'
        result = self.call('get_result', {'task_id': task_id}) if task['state'] in TERMINAL else {}
        response = {'contract_version': '1.0', 'task_id': task_id, 'state': self.store.get(task_id)['state'],
                    'remote_cancelled': result.get('remote_cancelled', False),
                    'cancel_scope': result.get('cancel_scope', scope)}
        if task['state'] in TERMINAL and (task.get('acceptance') or {}).get('review'):
            # 执行已结束时取消作用于进行中的自动审核；执行状态不变。
            review = self.reviewer.cancel(task_id, '取消执行尝试，审核已停止：' + str(reason)[:200])
            if review:
                response['review'] = review
                response.update(self.verification.fields(self.store.get(task_id)))
        return response

    def schedule(self):
        while not self.stopping.wait(.05):
            try:
                try:
                    with self.lock:
                        rework_pending = [i for i in self.auto_rework_pending if time.monotonic() >= self.auto_rework_retry_at.get(i, 0)]
                        review_pending = [i for i in self.review_pending if time.monotonic() >= self.review_retry_at.get(i, 0)]
                    pending = (self.verification.pending() + review_pending + rework_pending
                               if time.monotonic() >= self.verification_retry_at else [])
                    with self.lock:
                        for ident in pending:
                            if self.stopping.is_set() or len(self.verification_active) >= self.concurrency:
                                break
                            if ident not in self.verification_active:
                                review_only = ident in self.review_pending
                                worker = threading.Thread(target=self.verify, args=(
                                    ident, not review_only and ident in self.auto_rework_pending, review_only), daemon=True)
                                self.verification_active[ident] = worker
                                try:
                                    worker.start()
                                except Exception:
                                    self.verification_active.pop(ident, None)
                                    raise
                except Exception as error:
                    # 全局发现失败退避，执行调度仍继续；可用时记录受影响任务。
                    self.verification_retry_at = time.monotonic() + 1
                    try:
                        for task in self.store.list({'succeeded'}):
                            if task.get('acceptance') and self.verification.summary(task)['state'] == 'awaiting_verification':
                                self.store.event(task['id'], 'verification_dispatch_error', {'reason': type(error).__name__})
                    except Exception:
                        pass
                with self.lock:
                    from collections import Counter
                    used_agents = Counter(a['agent'] for a in self.active.values())
                    used_workspaces = {a['workspace'] for a in self.active.values()}
                    queued = self.store.list({'queued'})
                    # Skip quarantine lookup when idle or concurrency is already full.
                    quarantined = set()
                    if queued and len(self.active) < self.concurrency:
                        quarantined = {
                            os.path.normcase(str(Path(t.get('workspace') or t['cwd']).resolve()))
                            for t in self.store.list_quarantined()}
                    for task in queued:
                        if len(self.active) >= self.concurrency:
                            break
                        actual_workspace = task.get('workspace') or (
                            str(self.workspaces.root / task['id']) if task['mode'] == 'edit_worktree' else task['cwd'])
                        workspace_key = os.path.normcase(str(Path(actual_workspace).resolve()))
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
                        try:
                            self.store.update(task['id'], state='running')
                            worker.start()
                        except Exception:
                            if worker.ident is None:
                                self.active.pop(task['id'], None)
                                if self.store.get(task['id'])['state'] == 'running':
                                    self.store.update(task['id'], state='queued')
                            raise
            except Exception:
                logging.getLogger(__name__).exception('任务调度失败，将短暂退避后重试')
                self.stopping.wait(.2)

    def verify(self, task_id, rework_only=False, review_only=False):
        reworking = reviewing = False
        try:
            with self.lock:
                if rework_only and self.auto_rework_failures.get(task_id, 0) >= 3:
                    return
                if review_only:
                    self.review_pending.discard(task_id)
            if not rework_only and not review_only:
                self.verification.run(task_id, automatic=True)
            if not rework_only:
                reviewing = True
                self.reviewer.process(task_id)
                reviewing = False
                with self.lock:
                    self.review_failures.pop(task_id, None)
                    self.review_retry_at.pop(task_id, None)
            reworking = True
            self.auto_rework(task_id)
            with self.lock:
                self.auto_rework_pending.discard(task_id)
                self.auto_rework_failures.pop(task_id, None)
                self.auto_rework_retry_at.pop(task_id, None)
        except Exception as error:
            if reviewing and not self.stopping.is_set():
                with self.lock:
                    failures = self.review_failures.get(task_id, 0) + 1
                    self.review_failures[task_id] = failures
                    if failures < 3:
                        self.review_pending.add(task_id)
                        self.review_retry_at[task_id] = time.monotonic() + 2 ** failures
                if failures >= 3:
                    # 有界失败后 fail closed，保留原因，等待人工接管。
                    try:
                        self.reviewer.abort(task_id, '审核处理连续失败：' + type(error).__name__)
                    except Exception:
                        logging.getLogger(__name__).exception('自动审核无法转人工')
            elif reviewing:
                with self.lock:
                    self.review_pending.add(task_id)
            with self.lock:
                if reworking:
                    failures = self.auto_rework_failures.get(task_id, 0) + 1
                    self.auto_rework_failures[task_id] = failures
                    if failures >= 3:
                        self.auto_rework_pending.discard(task_id)
                        logging.getLogger(__name__).error('自动返工重试达到上限：%s', type(error).__name__)
                    else:
                        self.auto_rework_pending.add(task_id)
                        self.auto_rework_retry_at[task_id] = time.monotonic() + 2 ** failures
            self.verification_retry_at = time.monotonic() + .2
            try:
                self.store.event(task_id, 'verification_error', {'reason': type(error).__name__})
            except Exception:
                pass
        finally:
            with self.lock:
                self.verification_active.pop(task_id, None)

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
        if not task.get("acceptance"):
            timer.start()
        try:
            prepared = self.workspaces.prepare(task)
            task.update(prepared, deadline=deadline, log_dir=str(self.root / 'logs' / ident),
                        media_artifact_root=str(self.gateway.artifact_root))
            self.store.update(ident, **prepared)
            self.store.event(ident, 'state', {'state': 'running'})
            from .adapters.base import CLI
            cli = isinstance(self.adapters[task['agent']], CLI)
            cli_session = None
            def event(kind, data):
                nonlocal cli_session
                if kind == 'session' and data.get('session_id'):
                    if cli:
                        if data.get('source') != 'cli_init' or cli_session is not None:
                            return
                        cli_session = data['session_id']
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
            if task.get("acceptance"):
                self.verification.snapshot(task, cancel)
                self.verification.checkpoint()
                if cancel.is_set():
                    raise HubError("CANCELLED", "任务已取消")
                task["deadline"] = time.time() + task["timeout_seconds"]
                timer.start()
            result = self.adapters[task['agent']].run(task, cancel, event)
            if cli:
                result['session_id'] = cli_session
            elif result.get('session_id'):
                self.store.update(ident, session_id=result['session_id'])
            media_entries = result.get('media_artifacts', [])
            result['media_artifacts'] = self.gateway.register(ident, media_entries)
            self.verification.media(task, media_entries, result['media_artifacts'])
            result['artifacts'] = dict(result.get('artifacts', {}), **self.workspaces.artifacts(task))
            state = 'timed_out' if timed_out.is_set() else 'cancelled' if cancel.is_set() else 'succeeded'
        except HubError as error:
            if error.code == 'VERIFICATION_STOPPED' and self.stopping.is_set():
                # 停机时保留在途状态，由 recover 标 interrupted 并隔离工作区。
                with self.lock:
                    self.active.pop(ident, None)
                return
            state = {'CANCELLED': 'cancelled', 'VERIFICATION_STOPPED': 'cancelled', 'TIMED_OUT': 'timed_out',
                     'DELIVERY_UNKNOWN': 'delivery_unknown', 'REMOTE_STATE_UNKNOWN': 'interrupted'}.get(error.code, 'failed')
            if timed_out.is_set() and state == 'cancelled':
                state = 'timed_out'
            result = dict(error.details, error=str(error), error_code=error.code)
        except Exception as error:
            state, result = 'failed', {'error': type(error).__name__, 'error_code': 'EXECUTION_ERROR'}
        finally:
            watchdog_stop.set()
            if timer.ident is not None:
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
            try:
                self.store.finish(ident, state, {'path': str(path)})
                self.verification.track(self.store.get(ident))
                self.verification.ensure(self.store.get(ident))
            except Exception as error:
                self.store.event(ident, 'verification_enqueue_error', {'reason': type(error).__name__})
            finally:
                if isinstance(task.get('review_of'), dict) and task['review_of'].get('task_id'):
                    self.review_pending.add(task['review_of']['task_id'])
                self.active.pop(ident, None)

    def close(self):
        with self.verification.lock:
            self.stopping.set()
        self.thread.join(5)
        with self.lock:
            workers = list(self.active.values())
            for active in workers:
                active['cancel'].set()
        for active in workers:
            adapter = self.adapters[active['agent']]
            active['thread'].join(30 if getattr(adapter, 'cancel_scope', None) in {'remote_session', 'remote_hub_task'} else 20)
        with self.lock:
            verifiers = list(self.verification_active.values())
        end = time.monotonic() + getattr(self, "verification_join_seconds", 20)
        for worker in verifiers:
            worker.join(max(0, end - time.monotonic()))
