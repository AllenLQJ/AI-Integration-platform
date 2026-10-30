import hashlib
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
from .workspaces import Workspaces, workspace_key
from .verification import Verification, validate


class Service:
    def __init__(self, data_dir, adapters, concurrency=2, per_agent_concurrency=None):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        from .config import verification_options
        verification_config = verification_options(self.root)
        self.store = Store(self.root / 'tasks.db')
        self.event_condition = self.store.event_condition
        self.store.reuse_connections()
        self.store.recover()
        self.gateway = Gateway(self.root, self.store)
        self.stopping = threading.Event()
        self.scheduler_wakeup = threading.Event()
        self.verification = Verification(self.store, self.gateway, self.stopping, notify=self.scheduler_wakeup.set)
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
        from .work_items import WorkItems
        self.work_items = WorkItems(self.store)
        from .review import Reviewer
        self.reviewer = Reviewer(self)
        # 重启只恢复已持久化的审核：已建任务不重发，终态任务按收据幂等回写。
        self.review_pending = set(self.reviewer.recover())
        self.review_failures = {}
        self.review_retry_at = {}
        self.recover_auto_rework()
        self._locked_tools = {
            'ide_next_task': lambda a: self._tool_ide('ide_next_task', a),
            'ide_task_update': lambda a: self._tool_ide('ide_task_update', a),
            'ide_task_status': lambda a: self._tool_ide('ide_task_status', a),
            'get_capabilities': self._tool_get_capabilities,
            'get_artifact': self._tool_get_artifact,
            'create_work_item': self._tool_create_work_item,
            'submit_task': self._tool_submit_task,
            'resume_task': lambda a: self.resume_task(**a),
            'get_task': self._tool_get_task,
            'get_result': self._tool_get_result,
        }
        self.thread = threading.Thread(target=self.schedule, daemon=True)
        self.thread.start()

    # Tools that act on an existing attempt: they carry task_id and honour the controller lease.
    CONTROLLED_TOOLS = frozenset({
        'continue_task', 'cancel_task', 'verify_task', 'record_verification', 'switch_executor',
        'record_external_verification', 'record_activity', 'link_task', 'reconcile_task'})

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
        if tool == 'claim_controller' or tool in self.CONTROLLED_TOOLS:
            return self._call_controlled(tool, arguments)
        if tool == 'list_agents':
            with self.lock:
                adapters = list(self.adapters.items())
            return {name: adapter.diagnose() for name, adapter in adapters}
        # Pure SQLite snapshot reads: they do not need the scheduler/submit lock.
        if tool == 'get_work_item':
            return self.verification.work_item(**arguments)
        if tool == 'get_events':
            return dict(self.store.events(**arguments), contract_version='1.0')
        handler = self._locked_tools.get(tool)
        if handler is None:
            raise HubError('UNKNOWN_TOOL', '未知工具')
        with self.lock:
            return handler(arguments)

    def _call_controlled(self, tool, arguments):
        if any(k.startswith('_') for k in arguments):
            raise HubError('INVALID_ARGUMENT', '不允许设置内部任务字段')
        task_key = arguments.get('task_id') or arguments.get('related_task_id')
        if tool == 'link_task':
            task_key = arguments.get('task_id')
        if not isinstance(task_key, str) or not task_key:
            raise HubError('INVALID_ARGUMENT', 'task_id 需要非空字符串')
        root = self.verification.root_id(task_key)
        # reconcile_task: auth/lease under lock, network I/O outside the global Service lock
        # and outside the long-held verification lock.
        if tool == 'reconcile_task':
            args = dict(arguments)
            if set(args) - {'request_id', 'task_id', 'controller_id', 'controller_epoch'}:
                raise HubError('INVALID_ARGUMENT', 'reconcile_task 参数不正确')
            controller_id = args.pop('controller_id', None)
            controller_epoch = args.pop('controller_epoch', None)
            with self.verification.task_lock(root):
                self.verification.check_controller(args['task_id'], controller_id, controller_epoch)
                prepared = self._reconcile_prepare(args['request_id'], args['task_id'])
                if prepared.get('cached'):
                    return prepared['response']
            return self._reconcile_execute(prepared, controller_id=controller_id,
                                           controller_epoch=controller_epoch)
        with self.verification.task_lock(root):
            args = dict(arguments)
            if tool in self.CONTROLLED_TOOLS:
                self.verification.check_controller(args.get('task_id') or task_key,
                                                   args.pop('controller_id', None), args.pop('controller_epoch', None))
            return getattr(self, '_tool_' + tool)(args)

    def _reject_executor_verifier(self, args):
        verifier = args.get('verifier_id')
        if isinstance(verifier, str) and (verifier in self.adapters or verifier.startswith('hub-')):
            raise HubError('INVALID_ARGUMENT', '执行器不能写验收结果，hub- 前缀保留给内部验证器')

    def _tool_claim_controller(self, args):
        return self.verification.claim(**args)

    def _tool_record_verification(self, args):
        self._reject_executor_verifier(args)
        return self.verification.record(**args)

    def _tool_record_external_verification(self, args):
        self._reject_executor_verifier(args)
        return self.verification.external(**args)

    def _tool_record_activity(self, args):
        if set(args) - {'request_id', 'task_id', 'event_id', 'activity'}:
            raise HubError('INVALID_ARGUMENT', 'record_activity 参数不正确')
        return self.record_activity(**args)

    def _tool_link_task(self, args):
        if set(args) - {'request_id', 'work_item_id', 'task_id', 'relation', 'related_task_id'}:
            raise HubError('INVALID_ARGUMENT', 'link_task 参数不正确')
        return self.work_items.link_task(**args)

    def _tool_verify_task(self, args):
        result = self.verification.request(args['request_id'], dict(tool='verify_task', task_id=args['task_id']),
                                           lambda: self.verification.run(args['task_id'], args['request_id']))
        self.reviewer.process(args['task_id'])
        self.auto_rework(args['task_id'])
        return result

    def _tool_continue_task(self, args):
        with self.verification.task_lock(args['task_id']), self.lock:
            return self.continue_task(**args)

    def _tool_switch_executor(self, args):
        with self.verification.task_lock(args['task_id']), self.lock:
            return self.switch_executor(**args)

    def _tool_cancel_task(self, args):
        with self.verification.task_lock(args['task_id']), self.lock:
            return self.cancel(**args)

    # The handlers below run under Service.lock; each takes the raw arguments.
    def _tool_ide(self, tool, arguments):
        from .adapters.cursor_ide import CursorIDE
        adapter = self.adapters.get('cursor')
        if not isinstance(adapter, CursorIDE):
            raise HubError('AGENT_UNAVAILABLE', 'Cursor IDE 桥接未配置')
        method = {'ide_next_task': adapter.next_task, 'ide_task_update': adapter.update,
                  'ide_task_status': adapter.status}[tool]
        return method(**arguments)

    def _tool_get_capabilities(self, arguments):
        return self.gateway.capabilities(self.adapters)

    def _tool_get_artifact(self, arguments):
        if set(arguments) - {'artifact_id', 'target_host_id', 'target_node_id'}:
            raise HubError('INVALID_ARGUMENT', '产物查询字段不正确')
        args = dict(arguments)
        target_node = args.pop('target_node_id', None)
        if target_node is not None:
            node = self.nodes.get(target_node)
            args['target_host_id'] = node['host_id']
            args['target_transport'] = node['transport']
        return self.nodes.artifact(**args)

    def _tool_create_work_item(self, arguments):
        if set(arguments) - {'request_id', 'title', 'acceptance'}:
            raise HubError('INVALID_ARGUMENT', 'create_work_item 参数不正确')
        return self.work_items.create(**arguments)

    def _tool_submit_task(self, arguments):
        allowed = {'request_id', 'agent', 'prompt', 'cwd', 'mode', 'timeout_seconds', 'context',
                   'acceptance', 'node_id', 'work_item_id', 'relation', 'related_task_id'}
        if set(arguments) - allowed:
            raise HubError('INVALID_ARGUMENT', '不允许设置内部任务字段')
        args = dict(arguments)
        work_item_id = args.pop('work_item_id', None)
        relation = args.pop('relation', None)
        related_task_id = args.pop('related_task_id', None)
        if work_item_id is None:
            raise HubError('WORK_ITEM_REQUIRED',
                           '公开提交必须提供 work_item_id；请先 create_work_item，审核/返修还需 related_task_id')
        _, relation = self.work_items.require_for_submit(work_item_id, relation, related_task_id)
        if args.get('acceptance') is None:
            inherited = self.work_items.acceptance_for_dispatch(work_item_id, relation)
            if inherited is not None:
                args['acceptance'] = inherited
        # Relation participates in task digest so replay with changed link metadata conflicts.
        task = self.submit(**args, work_item_id=work_item_id,
                           work_relation=relation, work_related_task_id=related_task_id)
        self.work_items.attach(work_item_id, task['id'], relation, related_task_id, args['request_id'])
        return task

    def _tool_get_task(self, arguments):
        return self.public(self.store.get(arguments['task_id']))

    def _tool_get_result(self, arguments):
        task = self.store.get(arguments['task_id'])
        if task['state'] not in TERMINAL:
            return dict(ready=False, state=task['state'], intervention=task.get('intervention'), permissions=self.permissions(task), **self.verification.fields(task),
                        **result_fields(task['state'], {}))
        result = self.store.read_result(task)
        if task.get('remote_observation') and 'remote_observation' not in result:
            result['remote_observation'] = task['remote_observation']
        if task.get('last_reconcile') and 'last_reconcile' not in result:
            result['last_reconcile'] = task['last_reconcile']
        result.update(result_fields(task['state'], result, task))
        return dict(result, ready=True, state=task['state'], permissions=self.permissions(task), **self.verification.fields(task))

    def record_activity(self, request_id, task_id, event_id, activity):
        """Append a controller-reported activity without mutating task lifecycle."""
        if not isinstance(task_id, str) or not task_id:
            raise HubError('INVALID_ARGUMENT', 'task_id 需要非空字符串')
        task = self.store.get(task_id)
        from .activity import normalize_activity
        workspace = str(task.get('cwd') or task.get('workspace') or '')
        normalized = normalize_activity(activity, workspace, force_provenance='controller_reported')
        return self.store.record_activity(request_id, task_id, event_id, normalized)

    def _reconcile_prepare(self, request_id, task_id):
        """Validate and snapshot under lock; peek idempotent receipt. No network."""
        import hashlib
        digest = hashlib.sha256(json.dumps(
            dict(tool='reconcile_task', task_id=task_id), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM reconcile_requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['digest'] != digest or row['task_id'] != task_id:
                    raise HubError('REQUEST_CONFLICT', 'request_id 已用于不同核对内容')
                return dict(cached=True, response=json.loads(row['response']))
        task = self.store.get(task_id)
        if task['state'] not in TERMINAL:
            raise HubError('INVALID_STATE', '仅终态任务可核对；运行中任务请继续 wait/get_result')
        if task.get('delivery') != 'accepted' or type(task.get('baseline')) is not int or not task.get('session_id'):
            raise HubError('INVALID_STATE', '缺少已持久化的 accepted/baseline/session，无法安全核对')
        adapter = self.adapters.get(task['agent'])
        if adapter is None or not hasattr(adapter, 'reconcile'):
            raise HubError('UNSUPPORTED_MODE', '该执行器不支持 reconcile_task')
        # Newer acceptance attempt must not be overwritten by recycling an old task.
        try:
            current = self.verification.work_item(task_id)['current_task_id']
        except HubError:
            current = task_id
        return dict(
            cached=False, request_id=request_id, task_id=task_id, task=task,
            agent=task['agent'], current_task_id=current,
            allow_complete=(
                task['state'] in {'interrupted', 'delivery_unknown'}
                and task['state'] != 'cancelled'
                and current == task_id
            ),
        )

    def _release_task_locks(self, task, adapter):
        """After validated complete under lease: clear only locks owned by this task."""
        paths = []
        state_file = getattr(adapter, 'state_file', None)
        if state_file is not None:
            paths.append(Path(state_file))
        sid = task.get('session_id')
        if sid and hasattr(adapter, 'session_state_file'):
            paths.append(Path(adapter.session_state_file(sid)))
        if hasattr(adapter, 'legacy_state_file'):
            paths.append(Path(adapter.legacy_state_file))
        for path in paths:
            try:
                if not path.exists():
                    continue
                owned = json.loads(path.read_text(encoding='utf-8'))
                if owned.get('task_id') == task['id'] and owned.get('session_id') in (None, sid):
                    if owned.get('bot_name') in (None, getattr(adapter, 'name', None)):
                        path.unlink()
            except (OSError, ValueError, TypeError):
                continue

    @staticmethod
    def _reconcile_result_name(task_id, request_id):
        """Immutable result filename: task id + digest only (never raw request_id path segments)."""
        digest = hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:16]
        return task_id + '.reconciled.' + digest + '.json'

    def _reconcile_execute(self, prepared, controller_id=None, controller_epoch=None):
        """Network I/O outside Service.lock; re-check lease/attempt before persisting."""
        import os
        task = prepared['task']
        adapter = self.adapters[prepared['agent']]
        # Bounded probe longer than a single slow GET round-trip; client timeout must exceed this.
        observation = adapter.reconcile(task, timeout_seconds=24)
        root = self.verification.root_id(prepared['task_id'])
        with self.verification.task_lock(root):
            self.verification.check_controller(prepared['task_id'], controller_id, controller_epoch)
            fresh = self.store.get(prepared['task_id'])
            try:
                current = self.verification.work_item(prepared['task_id'])['current_task_id']
            except HubError:
                current = prepared['task_id']
            if fresh['state'] in TERMINAL and fresh['state'] not in {'interrupted', 'delivery_unknown'}:
                # Cancelled/failed/succeeded: still record observation, never complete.
                return self.store.apply_reconcile(
                    prepared['request_id'], prepared['task_id'], observation,
                    allow_complete=False, result_path=None,
                    expected_session=fresh.get('session_id'), expected_baseline=fresh.get('baseline'))
            allow_complete = (
                fresh['state'] in {'interrupted', 'delivery_unknown'}
                and current == prepared['task_id']
                and observation.get('status') == 'completed'
            )
            result_path = None
            if allow_complete:
                result = self.store.safe(dict(
                    answer=observation.get('answer'),
                    session_id=observation.get('session_id') or fresh.get('session_id'),
                    completion_evidence=observation.get('completion_evidence'),
                    remote_observation=observation.get('remote_observation'),
                    reconciled=True,
                    warnings=observation.get('warnings') or [],
                ))
                directory = self.root / 'results'
                directory.mkdir(exist_ok=True)
                final = directory / self._reconcile_result_name(prepared['task_id'], prepared['request_id'])
                staging = final.with_suffix(final.suffix + '.tmp')
                try:
                    staging.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
                    # File must be immutable and readable BEFORE any DB reference to succeeded.
                    os.replace(str(staging), str(final))
                    Path(final).read_text(encoding='utf-8')
                    result_path = str(final)
                except (OSError, ValueError, TypeError):
                    allow_complete = False
                    result_path = None
                    for path in (staging, final):
                        try:
                            path.unlink()
                        except OSError:
                            pass
            response = self.store.apply_reconcile(
                prepared['request_id'], prepared['task_id'], observation,
                allow_complete=allow_complete and bool(result_path), result_path=result_path,
                expected_session=fresh.get('session_id'), expected_baseline=fresh.get('baseline'))
            if response.get('completed'):
                self._release_task_locks(fresh, adapter)
                try:
                    self.verification.track(self.store.get(prepared['task_id']))
                    self.verification.ensure(self.store.get(prepared['task_id']))
                except Exception:
                    # The scheduler's verification discovery retries; keep the cause visible.
                    logging.getLogger(__name__).warning('核对完成后登记验收失败：%s', prepared['task_id'], exc_info=True)
            elif result_path:
                # Evidence/lease rejected after file write — do not leave an orphan result.
                try:
                    Path(result_path).unlink()
                except OSError:
                    pass
            return response

    def reconcile_task(self, request_id, task_id, controller_id=None, controller_epoch=None):
        prepared = self._reconcile_prepare(request_id, task_id)
        if prepared.get('cached'):
            return prepared['response']
        return self._reconcile_execute(prepared, controller_id=controller_id, controller_epoch=controller_epoch)

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
            token = self.store.change_token()
            if self.stopping.is_set():
                raise HubError('SERVICE_STOPPING', '服务正在停止，请稍后按原游标续接')
            snapshot = self.store.wait_snapshot(task_id, after_seq)
            terminal = snapshot['state'] in TERMINAL
            remaining = deadline - time.monotonic()
            needs_user = snapshot['state'] == 'waiting_for_user'
            if terminal or needs_user or remaining <= 0:
                return dict(snapshot, timed_out=not terminal and not needs_user, needs_user=needs_user, contract_version='1.0')
            # Neither the global service lock nor a DB transaction is held here.
            self.store.wait_for_change(token, min(1.0, remaining))

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
        work_item_id = internal.pop('work_item_id', None)
        work_relation = internal.pop('work_relation', None)
        work_related_task_id = internal.pop('work_related_task_id', None)
        spec = dict(agent=agent, prompt=prompt, cwd=str(Path(cwd).resolve()), mode=mode,
                    timeout_seconds=timeout_seconds, **internal)
        if isinstance(work_item_id, str) and work_item_id:
            spec['work_item_id'] = work_item_id
        # Include collaboration link metadata in the idempotency digest.
        if work_relation is not None:
            spec['work_relation'] = work_relation
        if work_related_task_id is not None:
            spec['work_related_task_id'] = work_related_task_id
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
        resolved = workspace_key(cwd)
        if any(workspace_key(t.get('workspace') or t['cwd']) == resolved for t in self.store.list_quarantined()):
            raise HubError('WORKSPACE_UNCERTAIN', '此工作区存在未核对的旧执行状态')
        task = self.store.create(request_id, spec)
        policy = adapter.permission_policy(mode) if hasattr(adapter, 'permission_policy') else self.permissions(task)
        self.store.update(task['id'], permissions=policy)
        task = self.store.get(task['id'])
        self.verification.track(task)
        if task.get('parent_id') and task.get('acceptance'):
            self.store.event(task['parent_id'], 'session', dict(new_attempt_id=task['id'], native_session_shared=bool(task.get('resume_session'))))
        self.scheduler_wakeup.set()
        return self.public(task)

    def continue_task(self, request_id, task_id, prompt, timeout_seconds=1800, acceptance=None,
                      _auto_rework_run_id=None):
        parent = self.store.get(task_id)
        if parent['agent'] == 'muse' and parent['mode'] == 'read_only' and not self.store.by_request(request_id):
            raise HubError('LEGACY_MODE', '旧 Muse read_only 不保证只读；请使用 delegate 新建任务并确认操作范围')
        if parent['agent'] == 'claude' and parent['mode'] == 'read_only' and 'permissions' not in parent and not self.store.by_request(request_id):
            parent = dict(parent, mode='text_only')
        workspace_metadata = ({'excluded_untracked_files': parent['excluded_untracked_files']}
                              if 'excluded_untracked_files' in parent else {})
        # Explicit acceptance override applies only to the new child; default inherits parent.
        if acceptance is not None:
            validate(acceptance)
            workspace_metadata['acceptance'] = acceptance
        else:
            workspace_metadata['acceptance'] = parent.get('acceptance')
        if parent.get('node_id'):
            workspace_metadata['node_id'] = parent['node_id']
        if _auto_rework_run_id is not None:
            workspace_metadata['auto_rework_run_id'] = _auto_rework_run_id
        wid, _, related = self.work_items.inherit_from_parent(parent)
        work_relation = 'rework' if _auto_rework_run_id is not None else 'continuation'
        if wid:
            workspace_metadata['work_item_id'] = wid
        if self.store.by_request(request_id):
            child = self.submit(request_id, parent['agent'], prompt, parent['cwd'], parent['mode'],
                               timeout_seconds, parent_id=task_id, workspace=parent.get('workspace'),
                               baseline=parent.get('baseline'), resume_session=parent.get('session_id'),
                               work_relation=work_relation, work_related_task_id=related,
                               **workspace_metadata)
            if wid:
                self.work_items.attach(wid, child['id'], work_relation, related, request_id)
            return child
        adapter = self.adapters.get(parent['agent'])
        if getattr(adapter, 'api_version', None) == 1 and 'continue' not in adapter.capabilities:
            raise HubError('UNSUPPORTED_MODE', '执行器未声明会话续接能力')
        if parent.get('acceptance'):
            current = self.verification.work_item(task_id)['current_task_id']
            if current != task_id or self.store.has_children(task_id):
                raise HubError('ATTEMPT_SUPERSEDED', '只能从当前尝试续接返工', {'current_task_id': current})
        if (getattr(self.adapters[parent['agent']], 'cancel_scope', None) in {'remote_session', 'remote_hub_task'}
                or parent['agent'] == 'muse') and parent['state'] in {'interrupted', 'delivery_unknown'}:
            raise HubError('REMOTE_STATE_UNKNOWN', '原任务远端状态不确定，禁止直接续发')
        if (getattr(self.adapters[parent['agent']], 'cancel_scope', None) in {'remote_session', 'remote_hub_task'}
                and parent['state'] in {'cancelled', 'timed_out'}
                and not self.call('get_result', {'task_id': task_id}).get('remote_cancelled')):
            raise HubError('REMOTE_STATE_UNKNOWN', '原任务远端停止未确认，禁止直接续发')
        if parent.get('workspace_quarantined'):
            raise HubError('WORKSPACE_UNCERTAIN', '旧执行状态不确定，工作区已隔离；请人工核对后在新工作区提交')
        if parent['state'] not in TERMINAL:
            raise HubError('WORKSPACE_BUSY', '父任务仍在执行')
        if not parent.get('session_id'):
            raise HubError('SESSION_UNAVAILABLE', '没有可续接的原生会话')
        siblings = self.store.list({'queued', 'running', 'waiting_for_user', 'cancelling'})
        parent_key = workspace_key(parent.get('workspace') or parent['cwd'])
        if any(t.get('workspace') and workspace_key(t['workspace']) == parent_key for t in siblings):
            # Idempotent replay of the exact same child is allowed.
            if not any(t['request_id'] == request_id for t in siblings):
                raise HubError('WORKSPACE_BUSY', '工作区已有另一轮任务')
        child = self.submit(request_id, parent['agent'], prompt, parent['cwd'], parent['mode'],
                           timeout_seconds, parent_id=task_id, workspace=parent.get('workspace'),
                           baseline=parent.get('baseline'), resume_session=parent['session_id'],
                           work_relation=work_relation, work_related_task_id=related,
                           **workspace_metadata)
        if wid:
            self.work_items.attach(wid, child['id'], work_relation, related, request_id)
        return child

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
            remote_scope = getattr(self.adapters[parent['agent']], 'cancel_scope', None)
            if (remote_scope in {'remote_session', 'remote_hub_task'} or parent['agent'] == 'muse') and parent['state'] in {'interrupted', 'delivery_unknown'}:
                raise HubError('REMOTE_STATE_UNKNOWN', '旧尝试远端状态不确定，禁止交接重发')
            if remote_scope in {'remote_session', 'remote_hub_task'} and parent['state'] in {'cancelled', 'timed_out'} and not self.call('get_result', {'task_id': task_id}).get('remote_cancelled'):
                raise HubError('REMOTE_STATE_UNKNOWN', '原尝试远端停止未确认')
            if parent.get('workspace_quarantined'):
                raise HubError('WORKSPACE_UNCERTAIN', '工作区已隔离')
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
            wid, work_relation, related = self.work_items.inherit_from_parent(parent)
            if wid:
                workspace_metadata['work_item_id'] = wid
            child = self.submit(request_id, agent, handoff + '\n用户本轮指令：\n' + prompt,
                               parent['cwd'], mode, parent['timeout_seconds'], acceptance=parent.get('acceptance'),
                               parent_id=task_id, **workspace_metadata)
            if wid:
                self.work_items.attach(wid, child['id'], work_relation or 'switch', related, request_id)
            return child
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
                    with self.store.connection(write=True) as db:
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
        self.scheduler_wakeup.set()
        return response

    def schedule(self):
        while not self.stopping.is_set():
            # Clear before discovery, never between discovery and waiting: a
            # notification arriving during a scan must trigger another pass.
            self.scheduler_wakeup.clear()
            try:
                self._dispatch_verification()
                self._dispatch_tasks()
            except Exception:
                logging.getLogger(__name__).exception('任务调度失败，将短暂退避后重试')
                self.stopping.wait(.2)
            # Direct DB writes and crash-recovery fixtures still become visible
            # within one second, without depending on an in-memory notification.
            self.scheduler_wakeup.wait(self._scheduler_delay())

    def _dispatch_verification(self):
        """Start verification/review/rework workers; discovery failures back off without blocking execution."""
        try:
            with self.lock:
                rework_pending = [i for i in self.auto_rework_pending if time.monotonic() >= self.auto_rework_retry_at.get(i, 0)]
                review_pending = [i for i in self.review_pending if time.monotonic() >= self.review_retry_at.get(i, 0)]
                verification_room = len(self.verification_active) < self.concurrency
            pending = (self.verification.pending() + review_pending + rework_pending
                       if verification_room and time.monotonic() >= self.verification_retry_at else [])
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
            logging.getLogger(__name__).warning('验收任务发现失败，1 秒后重试', exc_info=True)
            try:
                for task in self.store.list_with_acceptance({'succeeded'}):
                    if self.verification.summary(task)['state'] == 'awaiting_verification':
                        self.store.event(task['id'], 'verification_dispatch_error', {'reason': type(error).__name__})
            except Exception:
                logging.getLogger(__name__).debug('无法记录验收发现失败事件', exc_info=True)

    def _dispatch_tasks(self):
        """Start queued tasks within global, per-agent and per-workspace limits."""
        with self.lock:
            from collections import Counter
            used_agents = Counter(a['agent'] for a in self.active.values())
            used_workspaces = {a['workspace'] for a in self.active.values()}
            queued = self.store.list({'queued'}) if len(self.active) < self.concurrency else []
            # Skip quarantine lookup when idle or concurrency is already full.
            quarantined = set()
            if queued and len(self.active) < self.concurrency:
                quarantined = {
                    workspace_key(t.get('workspace') or t['cwd'])
                    for t in self.store.list_quarantined()}
            for task in queued:
                if len(self.active) >= self.concurrency:
                    break
                actual_workspace = task.get('workspace') or (
                    str(self.workspaces.root / task['id']) if task['mode'] == 'edit_worktree' else task['cwd'])
                workspace_id = workspace_key(actual_workspace)
                if workspace_id in quarantined:
                    self.store.update(task['id'], workspace=actual_workspace, workspace_quarantined=True)
                    self.store.finish(task['id'], 'interrupted', {
                        'error_code': 'WORKSPACE_UNCERTAIN',
                        'error': '工作区旧执行状态不确定，排队任务未启动；请人工核对后使用新工作区'})
                    continue
                if (used_agents[task['agent']] >= self.per_agent_concurrency.get(task['agent'], 1)
                        or workspace_id in used_workspaces):
                    continue
                cancel = threading.Event()
                worker = threading.Thread(target=self.execute, args=(task, cancel), daemon=True)
                self.active[task['id']] = dict(agent=task['agent'], workspace=workspace_id,
                                               cancel=cancel, thread=worker)
                used_agents[task['agent']] += 1
                used_workspaces.add(workspace_id)
                try:
                    self.store.update(task['id'], state='running')
                    worker.start()
                except Exception:
                    if worker.ident is None:
                        self.active.pop(task['id'], None)
                        if self.store.get(task['id'])['state'] == 'running':
                            self.store.update(task['id'], state='queued')
                    raise

    def _scheduler_delay(self):
        with self.lock:
            now = time.monotonic()
            deadlines = [self.verification_retry_at, *self.review_retry_at.values(), *self.auto_rework_retry_at.values()]
            delays = [deadline - now for deadline in deadlines if deadline > now]
            retry_at = self.verification.next_retry_at
            if retry_at is not None and retry_at > time.time():
                delays.append(retry_at - time.time())
        return min(1.0, *delays) if delays else 1.0

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
                logging.getLogger(__name__).debug('无法记录验收失败事件：%s', task_id, exc_info=True)
        finally:
            with self.lock:
                self.verification_active.pop(task_id, None)
                self.scheduler_wakeup.set()

    def execute(self, task, cancel):
        outcome = None
        try:
            outcome = self._execute_task(task, cancel)
            if outcome is not None:
                self._finish_execution(task, *outcome)
        except Exception as error:
            logging.getLogger(__name__).exception('任务收尾失败：%s', task['id'])
            state, evidence = 'failed', {'error_code': 'EXECUTION_ERROR', 'error': '任务收尾失败，请核对任务日志'}
            if outcome is not None:
                execution_state, result, _ = outcome
                if execution_state != 'succeeded':
                    state, evidence = execution_state, self._failure_evidence(result)
                evidence['postprocessing_error'] = type(error).__name__
            try:
                with self.lock:
                    try:
                        self._quarantine_uncertain_workspace(task, state, evidence)
                        self.store.finish(task['id'], state, evidence)
                    finally:
                        self._release_execution_slot(task)
            except Exception:
                logging.getLogger(__name__).exception('无法持久化任务失败状态：%s', task['id'])
        finally:
            self._release_execution_slot(task)

    def _release_execution_slot(self, task):
        with self.lock:
            if self.active.pop(task['id'], None) is not None:
                if isinstance(task.get('review_of'), dict) and task['review_of'].get('task_id'):
                    self.review_pending.add(task['review_of']['task_id'])
                self.scheduler_wakeup.set()

    def _quarantine_uncertain_workspace(self, task, state, result):
        adapter = self.adapters[task['agent']]
        remote = (not adapter.remote_cancel or
                  getattr(adapter, 'cancel_scope', None) in {'remote_session', 'remote_hub_task'})
        if (state in {'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}
                and result.get('cancel_scope') != 'not_started'
                and (result.get('local_process_stopped') is False
                     or (remote and result.get('remote_cancelled') is not True))):
            # Called under Service.lock before terminal publication/slot release.
            # Persist the reservation so both queued work and new submissions see it.
            self.store.update(task['id'], workspace=task.get('workspace') or task['cwd'],
                              workspace_quarantined=True)

    @staticmethod
    def _failure_evidence(result):
        return Store.safe({key: result[key] for key in (
            'error', 'error_code', 'remote_cancelled', 'cancel_scope', 'permissions',
            'remote_observation', 'warning', 'interrupt_requested', 'interrupt_acknowledged',
            'local_process_stopped', 'wait_timeout')
            if key in result})

    def _execute_task(self, task, cancel):
        ident = task['id']
        executor_started = False
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
                if kind == 'activity':
                    from .activity import normalize_activity
                    try:
                        workspace = str(task.get('cwd') or task.get('workspace') or '')
                        data = normalize_activity(
                            data, workspace, force_provenance='executor_observed')
                    except HubError as error:
                        # Never abort a live executor run over a bad activity frame.
                        self.store.event(ident, 'system', {
                            'message': 'activity normalize failed',
                            'error_code': error.code,
                            'detail': str(error)[:500],
                        })
                        return
                self.store.event(ident, kind, data)
            if task.get("acceptance"):
                self.verification.snapshot(task, cancel)
                self.verification.checkpoint()
                if cancel.is_set():
                    raise HubError("CANCELLED", "任务已取消")
                task["deadline"] = time.time() + task["timeout_seconds"]
                timer.start()
            if cancel.is_set():
                raise HubError('CANCELLED', '任务已取消')
            executor_started = True
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
                return
            state = {'CANCELLED': 'cancelled', 'VERIFICATION_STOPPED': 'cancelled', 'TIMED_OUT': 'timed_out',
                     'DELIVERY_UNKNOWN': 'delivery_unknown', 'REMOTE_STATE_UNKNOWN': 'interrupted'}.get(error.code, 'failed')
            if timed_out.is_set() and state == 'cancelled':
                state = 'timed_out'
            result = dict(error.details, error=str(error), error_code=error.code)
            adapter = self.adapters[task['agent']]
            remote = not adapter.remote_cancel or getattr(adapter, 'cancel_scope', None) in {'remote_session', 'remote_hub_task'}
            if (error.code in {'PROCESS_STREAM_ERROR', 'OUTPUT_TOO_LARGE'}
                    and (result.get('local_process_stopped') is False
                         or (remote and result.get('remote_cancelled') is not True))):
                state = 'interrupted'
                result.setdefault('remote_cancelled', False)
                result.setdefault('cancel_scope', getattr(adapter, 'cancel_scope', None)
                                  or ('remote_session' if remote else 'local_process_tree'))
        except Exception as error:
            state, result = 'failed', {'error': type(error).__name__, 'error_code': 'EXECUTION_ERROR'}
        finally:
            watchdog_stop.set()
            if timer.ident is not None:
                timer.join(1)
        if not executor_started and state in {'cancelled', 'timed_out'}:
            result.update(cancel_scope='not_started', remote_cancelled=False)
        return state, result, timed_out

    def _finish_execution(self, task, state, result, timed_out):
        ident = task['id']
        if state != 'succeeded' and task.get('workspace') and task['mode'] == 'edit_worktree':
            try:
                result['artifacts'] = dict(result.get('artifacts', {}), **self.workspaces.artifacts(task))
            except HubError as error:
                result['artifact_error'] = str(error)
        if result.get('cancel_scope') == 'not_started':
            pass
        elif not self.adapters[task['agent']].remote_cancel and state in {'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}:
            result.update(remote_cancelled=False, warning='仅停止本地等待；远端可能继续执行')
        elif getattr(self.adapters[task['agent']], 'cancel_scope', None) in {'remote_session', 'remote_hub_task'} and state in {'cancelled', 'timed_out', 'interrupted', 'delivery_unknown'}:
            result.setdefault('remote_cancelled', False)
            if not result['remote_cancelled']:
                result.setdefault('warning', '远端停止未确认；须核对专用 Bot 和私有状态文件')
        result['permissions'] = self.permissions(task)
        result = self.store.safe(result)
        path = self.root / 'results' / (ident + '.json')
        staging = path.with_suffix('.json.tmp')
        try:
            path.parent.mkdir(exist_ok=True)
            staging.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            os.replace(staging, path)
            stored_result = dict(self._failure_evidence(result), path=str(path))
        except (OSError, ValueError, TypeError):
            # Keep the failure readable even when the results directory is full.
            # Execution uncertainty/cancellation evidence must survive this fallback.
            stored_result = self._failure_evidence(result)
            stored_result['result_storage_error'] = 'RESULT_PERSISTENCE_ERROR'
            if state == 'succeeded':
                state = 'failed'
                stored_result.update(error_code='RESULT_PERSISTENCE_ERROR', error='无法保存执行结果，请检查数据目录')
        finally:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
        with self.lock:
            if state == 'succeeded' and self.store.get(ident)['state'] == 'cancelling':
                state = 'timed_out' if timed_out.is_set() else 'cancelled'
            self._quarantine_uncertain_workspace(task, state, result)
            self.store.finish(ident, state, stored_result)
            try:
                self.verification.track(self.store.get(ident))
                self.verification.ensure(self.store.get(ident))
            except Exception as error:
                self.store.event(ident, 'verification_enqueue_error', {'reason': type(error).__name__})
            finally:
                # Public RPC reads use this same lock: terminal publication and
                # slot release must be observed together, before execute returns.
                self._release_execution_slot(task)

    def close(self):
        with self.verification.lock:
            self.stopping.set()
            self.store.notify_change()
            self.scheduler_wakeup.set()
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
        # Release pooled SQLite handles so the data directory can be removed (Windows).
        self.store.close()
