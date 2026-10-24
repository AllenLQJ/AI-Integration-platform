"""基于 @cursor/bdk 0.2.23 公布的 Grok Bot session API。"""
import json
import hashlib
import os
import re
import threading
import time
import uuid
import httpx
from pathlib import Path
from ..models import HubError


class GrokBot:
    capabilities = ['delegate', 'continue']
    remote_cancel = True
    cancel_scope = 'remote_session'
    cancellation_timeout = 12

    def __init__(self, name, credentials_file=None, state_file=None, env=None, session_id=None):
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise HubError('INVALID_CONFIG', 'Grok Bot 需要明确的专用 Bot 名称')
        self.name = name
        if session_id is not None and (not isinstance(session_id, str)
                or not re.fullmatch(r'[\w-]{1,128}', session_id, re.ASCII)):
            raise HubError('INVALID_CONFIG', 'Grok Bot 固定 session_id 无效')
        self.session_id = session_id
        self.env = env or {}
        config = Path(self.env.get('AGENT_SERVE_CONFIG_DIR', os.environ.get('AGENT_SERVE_CONFIG_DIR',
            str(Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'agent-serve'))))
        self.credentials_file = Path(credentials_file) if credentials_file else config / 'credentials.json'
        # 同一用户不同 Hub 数据目录也不能向同一 Bot 同时派发。
        state_name = 'hub-grokbot-' + hashlib.sha256(name.encode()).hexdigest()[:16] + '.json'
        self.config_dir = config
        self.legacy_state_file = config / state_name
        self.explicit_state_file = state_file is not None
        self.state_file = Path(state_file) if state_file else (
            self.session_state_file(session_id) if session_id else self.legacy_state_file)
        self.lock = threading.Lock()

    def session_state_file(self, sid):
        return self.config_dir / ('hub-grokbot-session-' + hashlib.sha256(sid.encode()).hexdigest()[:16] + '.json')

    def legacy_reservations(self, sid):
        """不静默遗失旧名称锁；旧别名的有效会话锁也需人工核对。"""
        if self.explicit_state_file:
            return []
        found = []
        for path in self.config_dir.glob('hub-grokbot-*.json'):
            if path == self.state_file:
                continue
            if path == self.legacy_state_file:
                found.append(path)
                continue
            try:
                if sid is not None and json.loads(path.read_text(encoding='utf-8')).get('session_id') == sid:
                    found.append(path)
            except (OSError, ValueError, AttributeError):
                continue
        return found

    def permission_policy(self, mode):
        return {'enforced_by': 'grokbot_and_client', 'enforced': False,
                'allows_write': True, 'allows_commands': True, 'os_sandbox': False,
                'workspace': 'remote_bot', 'scope_note': '本机 cwd 不同步到云端；取消需独立核验远端 idle，已发生操作不回滚'}

    def key(self):
        key = self.env.get('CURSOR_API_KEY', os.environ.get('CURSOR_API_KEY'))
        if key:
            return key
        key_file = self.env.get('CURSOR_API_KEY_FILE', os.environ.get('CURSOR_API_KEY_FILE'))
        path = Path(key_file) if key_file else self.credentials_file
        try:
            if os.name != 'nt' and path.stat().st_mode & 0o077:
                raise HubError('AUTH_REQUIRED', 'Cursor 凭据文件权限须为 600')
            if key_file:
                key = path.read_text(encoding='utf-8').strip()
            else:
                data = json.loads(path.read_text(encoding='utf-8'))
                if data.get('backendUrl') != 'https://api2.cursor.sh':
                    raise ValueError('backend')
                key = data.get('apiKey')
            if not isinstance(key, str) or not key:
                raise ValueError('key')
            return key
        except (OSError, ValueError, AttributeError):
            raise HubError('AUTH_REQUIRED', '请使用 bdk login 完成 Cursor 用户 API key 登录') from None

    def diagnose(self):
        try:
            self.key()
            auth = '已发现私有凭据；账号权限与额度需真实任务确认'
            available = True
        except HubError:
            auth, available = '需要 bdk login', False
        return {'available': available, 'version': 'BDK session API 0.2.23',
                'authentication': auth, 'bot_name': self.name, 'remote_cancel': True,
                'session_id': self.session_id, 'session_binding': 'fixed_existing' if self.session_id else 'get_or_create_by_name',
                'cancel_scope': self.cancel_scope,
                'capabilities': self.capabilities, 'permissions': {'delegate': self.permission_policy('delegate')},
                'remote_review_required': self.state_file.exists() or bool(self.legacy_reservations(self.session_id)),
                'state_file': str(self.state_file)}

    def request(self, method, path, body, timeout):
        headers = {'Authorization': 'Bearer ' + self.key(),
                   'x-cursor-client-type': 'agent-serve', 'x-request-id': str(uuid.uuid4())}
        proxy = self.env.get('HTTPS_PROXY', os.environ.get('HTTPS_PROXY'))
        with httpx.Client(proxy=proxy, trust_env=False, timeout=timeout) as client:
            response = client.request(method, 'https://api.cursor.com' + path, headers=headers, json=body)
            response.raise_for_status()
            return response.json()

    def checkpoint(self, task, cancel):
        if cancel.is_set():
            raise HubError('CANCELLED', '已停止本地等待；远端可能继续执行')
        if time.time() >= task['deadline']:
            raise HubError('TIMED_OUT', '本地等待超时；远端可能继续执行')

    def call(self, method, path, body, task, cancel, accepted=False):
        for attempt in range(3 if method == 'GET' else 1):
            self.checkpoint(task, cancel)
            try:
                return self.request(method, path, body, max(.1, min(10,
                                                                  task['deadline'] - time.time())))
            except HubError:
                raise
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if method == 'GET' and (status == 429 or status >= 500) and attempt < 2:
                    cancel.wait(.5)
                    continue
                code = 'REMOTE_STATE_UNKNOWN' if accepted else 'REMOTE_FAILED'
                if method == 'POST' and (status >= 500 or status == 408):
                    code = 'DELIVERY_UNKNOWN'
                raise HubError(code, 'Grok Bot 接口 HTTP ' + str(status)) from None
            except (httpx.RequestError, OSError, ValueError):
                if method == 'GET' and attempt < 2:
                    cancel.wait(.5)
                    continue
                raise HubError('DELIVERY_UNKNOWN' if method == 'POST' else
                               ('REMOTE_STATE_UNKNOWN' if accepted else 'REMOTE_FAILED'),
                               'Grok Bot 接口响应不可确认；未自动重发消息') from None

    @staticmethod
    def page(data, accepted=False):
        try:
            if not isinstance(data, dict) or not isinstance(data['entries'], list):
                raise ValueError()
            cursor = str(data['latestUpdatedSeq'])
            if not re.fullmatch(r'-?\d+', cursor) or int(cursor) < -1:
                raise ValueError()
            turn = data['turn']
            if (type(turn['idle']) is not bool or type(turn['inFlight']) is not bool
                    or type(turn['queued']) is not int or turn['queued'] < 0):
                raise ValueError()
            for entry in data['entries']:
                if not isinstance(entry, dict) or not str(entry['seq']).isdigit() or not str(entry['updatedSeq']).isdigit():
                    raise ValueError()
            return data['entries'], cursor, turn
        except (KeyError, TypeError, ValueError):
            raise HubError('REMOTE_STATE_UNKNOWN' if accepted else 'REMOTE_FAILED',
                           'Grok Bot 历史响应不符合已核对的 BDK 契约') from None

    def reserve(self, task, session_id):
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 崩溃或不确定派发后留存文件，人工核对专用 Bot 后才能清除。
        with self.state_file.open('x', encoding='utf-8') as output:
            self.state_file.chmod(0o600)
            json.dump({'task_id': task['id'], 'session_id': session_id, 'bot_name': self.name,
                       'review_required': True}, output)

    def run(self, task, cancel, event):
        while not self.lock.acquire(timeout=.1):
            self.checkpoint(task, cancel)
        try:
            return self.execute(task, cancel, event)
        finally:
            self.lock.release()

    def execute(self, task, cancel, event):
        context = {}
        try:
            return self.execute_task(task, cancel, event, context)
        except HubError as error:
            if error.code in {'CANCELLED', 'TIMED_OUT'}:
                details = self.cancel_remote(task, context)
                event('remote_cancel', details)
                raise HubError(error.code, '任务已停止本地等待；远端停止状态见 remote_cancelled',
                               dict(error.details, **details)) from None
            raise

    def cancel_remote(self, task, context):
        """取消独立于已过期任务期限，单次 POST；保留锁直到会话停止已确认。"""
        result = {'remote_cancelled': False, 'cancel_scope': self.cancel_scope,
                  'interrupt_requested': False, 'interrupt_acknowledged': False}
        if not context.get('accepted'):
            result['warning'] = '消息未确认接收，不发送会话中断；待核对派发保留'
            return result
        end = time.monotonic() + self.cancellation_timeout
        sid, baseline, prompt = context['sid'], context['baseline'], context['prompt']
        path = '/v0/grokbot/sessions/' + sid
        def ownership():
            state = json.loads(self.state_file.read_text(encoding='utf-8'))
            if (state.get('task_id') != task['id'] or state.get('session_id') != sid
                    or state.get('bot_name') != self.name):
                raise ValueError('ownership')
        def request(method, suffix, body=None):
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise ValueError('cancel deadline')
            return self.request(method, path + suffix, body, min(3, remaining))
        cursor, own_message, interrupted, idle_count = str(baseline), False, False, 0
        completed = False
        marker = '[HUB-END:' + task['id'] + ']'
        try:
            ownership()
            while True:
                entries, new_cursor, turn = self.page(request('GET', '/entries?afterUpdatedSeq=' + cursor), True)
                if int(new_cursor) < int(cursor) or (entries and int(new_cursor) <= int(cursor)):
                    raise ValueError('cursor')
                for entry in entries:
                    if entry.get('role') == 'user' and int(entry['updatedSeq']) > baseline:
                        if entry.get('kind') != 'message' or entry.get('text') != prompt or own_message:
                            raise ValueError('another user turn')
                        own_message = True
                    if (int(entry['updatedSeq']) > baseline and entry.get('kind') == 'send-message'
                            and entry.get('role') in (None, 'assistant')
                            and isinstance(entry.get('text'), str) and entry['text'].rstrip().endswith(marker)):
                        completed = True
                cursor = new_cursor
                if turn['queued']:
                    raise ValueError('another queued turn')
                busy = turn['inFlight'] or not turn['idle']
                if busy and own_message and not interrupted and not entries:
                    ownership()
                    # session interrupt 不支持按 messageId 条件取消；专用 Bot 禁止并发人工消息。
                    result['interrupt_requested'] = True
                    request('POST', '/interrupt', {'reason': 'Agent Hub cancelled task ' + task['id']})
                    result['interrupt_acknowledged'] = True
                    interrupted = True
                    idle_count = 0
                else:
                    # 已接收但尚未启动也可能暂时 idle；自然结束必须有本任务最终回复。
                    idle_count = idle_count + 1 if own_message and (interrupted or completed) and not entries and not busy else 0
                if idle_count >= 4:
                    ownership()
                    self.state_file.unlink()
                    result.update(remote_cancelled=True, cancellation_evidence={
                        'session_id': sid, 'latest_updated_seq': cursor, 'idle_polls': idle_count,
                        'own_message_observed': own_message, 'stop_reason': 'interrupt' if interrupted else 'already_idle'})
                    return result
                if time.monotonic() >= end:
                    raise ValueError('cancel deadline')
                time.sleep(min(.25, max(0, end - time.monotonic())))
        except (HubError, httpx.RequestError, httpx.HTTPStatusError, OSError, ValueError, TypeError, KeyError):
            result['warning'] = '远端停止未确认或会话出现其他消息；保留私有状态文件，禁止自动续发'
            return result

    def execute_task(self, task, cancel, event, context):
        if self.state_file.exists() or self.legacy_reservations(self.session_id):
            raise HubError('REMOTE_STATE_UNKNOWN', '专用 Grok Bot 存在未核对派发；请人工检查远端和私有状态文件')
        # 固定既有会话时不调用 get-or-create，拼错名称也不会创建另一个 Bot。
        session = (dict(id=self.session_id, name=self.name, latestUpdatedSeq='-1') if self.session_id else
                   self.call('POST', '/v0/grokbot/sessions', {'name': self.name}, task, cancel))
        if (not isinstance(session, dict) or not isinstance(session.get('id'), str)
                or not session['id'] or session.get('name') != self.name
                or not re.fullmatch(r'[\w-]{1,128}', session['id'], re.ASCII)
                or not re.fullmatch(r'-?\d+', str(session.get('latestUpdatedSeq', '')))):
            raise HubError('REMOTE_FAILED', 'Grok Bot 会话响应不符合契约')
        sid = session['id']
        if not self.explicit_state_file:
            self.state_file = self.session_state_file(sid)
            if self.state_file.exists() or self.legacy_reservations(sid):
                raise HubError('REMOTE_STATE_UNKNOWN', '此 Grok Bot 会话已有待核对派发或旧名称锁；禁止自动迁移或续发')
        if task.get('resume_session') and task['resume_session'] != sid:
            raise HubError('SESSION_UNAVAILABLE', '专用 Bot 会话已变化，禁止续接到另一会话')
        path = '/v0/grokbot/sessions/' + sid
        event('session', {'session_id': sid})
        cursor = '-1'
        while True:
            data = self.call('GET', path + '/entries?afterUpdatedSeq=' + cursor, None, task, cancel)
            entries, new_cursor, turn = self.page(data)
            if turn['inFlight'] or turn['queued'] or not turn['idle']:
                raise HubError('WORKSPACE_BUSY', '专用 Grok Bot 已有在途回合，未发送本任务')
            if not entries:
                cursor = new_cursor
                break
            if int(new_cursor) <= int(cursor):
                raise HubError('REMOTE_FAILED', 'Grok Bot 历史游标没有前进')
            cursor = new_cursor
        baseline = int(cursor)
        marker = '[HUB-END:' + task['id'] + ']'
        prompt = self.dispatched_prompt(task)
        self.checkpoint(task, cancel)
        try:
            self.reserve(task, sid)
        except FileExistsError:
            raise HubError('REMOTE_STATE_UNKNOWN', '专用 Bot 已被另一进程占用或存在待核对派发') from None
        context.update(sid=sid, baseline=baseline, prompt=prompt)
        result = self.call('POST', path + '/messages',
                           {'text': prompt, 'messageId': str(uuid.UUID(task['id']))}, task, cancel)
        if not isinstance(result, dict) or result.get('delivery') not in {'accepted_temporal', 'duplicate'}:
            raise HubError('DELIVERY_UNKNOWN', 'Grok Bot 未返回明确消息接收确认，保留待核对状态')
        context['accepted'] = True
        event('accepted', {'baseline': baseline})
        self.checkpoint(task, cancel)
        replies = {}
        progress = {}
        idle_count = 0
        while True:
            data = self.call('GET', path + '/entries?afterUpdatedSeq=' + cursor, None, task, cancel, accepted=True)
            entries, new_cursor, turn = self.page(data, accepted=True)
            if int(new_cursor) < int(cursor) or (entries and int(new_cursor) <= int(cursor)):
                raise HubError('REMOTE_STATE_UNKNOWN', 'Grok Bot 回复游标异常，保留待核对状态')
            for entry in entries:
                text = entry.get('text')
                # Only public assistant send-message after baseline; ignore thought and foreign roles.
                if (int(entry['updatedSeq']) > baseline and entry.get('kind') == 'send-message'
                        and entry.get('role') in (None, 'assistant') and isinstance(text, str)):
                    seq = int(entry['seq'])
                    revision = int(entry['updatedSeq'])
                    prior = progress.get(seq)
                    if prior is None or revision > prior:
                        progress[seq] = revision
                        event('message', {
                            'message': text[:1000],
                            'role': 'assistant',
                            'provider_message_id': str(seq),
                            'provider_revision': revision,
                            'truncated': len(text) > 1000,
                        })
                    if text.rstrip().endswith(marker):
                        replies[seq] = text
            cursor = new_cursor
            idle_count = idle_count + 1 if not entries and turn['idle'] and not turn['inFlight'] and turn['queued'] == 0 else 0
            if replies and idle_count >= 4:
                self.checkpoint(task, cancel)
                self.state_file.unlink()
                return {'answer': replies[max(replies)], 'session_id': sid,
                        'completion_evidence': {'reply_seq': max(replies), 'latest_updated_seq': cursor,
                                                'marker': marker, 'idle_polls': idle_count},
                        'warnings': ['Grok Bot 在云端执行；本机 cwd 未同步，取消不回滚已发生操作']}
            self.checkpoint(task, cancel)
            cancel.wait(.5)

    def dispatched_prompt(self, task):
        """Same prompt text used by execute_task / cancel_remote for ownership matching."""
        marker = '[HUB-END:' + task['id'] + ']'
        return (task['prompt'] + '\n\n这是 Hub 委派的云端任务。本机工作区未同步，请勿假设可读取 Mac 路径。'
                + '\n完成全部工作后，在最终发给用户的回复最后单独写一行：' + marker)

    def _reconcile_get(self, path, cursor, timeout):
        """GET-only; convert transport/HTTP/JSON failures to HubError for network_unknown."""
        try:
            data = self.request('GET', path + '/entries?afterUpdatedSeq=' + cursor, None, timeout)
            return self.page(data, accepted=True)
        except HubError:
            raise
        except (httpx.HTTPStatusError, httpx.RequestError, OSError, ValueError, TypeError, KeyError) as error:
            raise HubError('REMOTE_STATE_UNKNOWN', 'Grok Bot 核对 GET 失败: ' + type(error).__name__) from error

    def _reconcile_resume(self, task, sid, baseline, marker):
        """Load prior probe progress only when session/baseline/marker still match this task."""
        progress = task.get('reconcile_progress')
        if not isinstance(progress, dict):
            return str(baseline), 0, {}, False
        if (progress.get('session_id') != sid or progress.get('baseline') != baseline
                or progress.get('marker') != marker):
            return str(baseline), 0, {}, False
        cursor = progress.get('cursor')
        idle = progress.get('idle_count')
        if not isinstance(cursor, str) or not re.fullmatch(r'-?\d+', cursor):
            return str(baseline), 0, {}, False
        if int(cursor) < baseline:
            return str(baseline), 0, {}, False
        if type(idle) is not int or not 0 <= idle <= 10000:
            idle = 0
        replies = {}
        answer = progress.get('answer')
        reply_seq = progress.get('reply_seq')
        if (isinstance(answer, str) and answer.rstrip().endswith(marker)
                and type(reply_seq) is int):
            replies[reply_seq] = answer
        elif not progress.get('own_marker_seen'):
            idle = 0
        return cursor, idle, replies, bool(progress.get('own_message_observed'))

    def reconcile(self, task, *, timeout_seconds=24):
        """GET-only recovery using persisted session/baseline/marker. Never POST.

        Pure observation: does not delete local reservation locks (Service releases
        after a validated complete transaction under a live lease). Progress
        (cursor/idle/marker answer) resumes across calls when ownership matches.
        """
        sid = task.get('session_id')
        baseline = task.get('baseline')
        if not isinstance(sid, str) or not sid:
            raise HubError('INVALID_ARGUMENT', '核对需要已持久化的 session_id')
        if type(baseline) is not int:
            raise HubError('INVALID_ARGUMENT', '核对需要已持久化的 baseline')
        if task.get('delivery') != 'accepted':
            raise HubError('INVALID_STATE', '仅 delivery=accepted 的任务可核对')
        marker = '[HUB-END:' + task['id'] + ']'
        prompt = self.dispatched_prompt(task)
        path = '/v0/grokbot/sessions/' + sid
        deadline = time.monotonic() + max(1.0, min(float(timeout_seconds), 30.0))
        # Refuse if a foreign lock is present; never delete locks here.
        if self.state_file.exists():
            try:
                owned = json.loads(self.state_file.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                owned = {}
            if (owned.get('task_id') not in (None, task['id'])
                    or owned.get('session_id') not in (None, sid)
                    or owned.get('bot_name') not in (None, self.name)):
                raise HubError('REMOTE_STATE_UNKNOWN', '专用 Bot 锁属于其他任务；禁止为重连删除或重发')
        cursor, idle_count, replies, own_message = self._reconcile_resume(task, sid, baseline, marker)
        foreign_user = False
        error_marker = False
        latest_turn = None

        def progress_snapshot():
            payload = {
                'session_id': sid, 'baseline': baseline, 'marker': marker,
                'cursor': cursor, 'idle_count': idle_count,
                'own_marker_seen': bool(replies),
                'own_message_observed': own_message,
            }
            if replies:
                payload['reply_seq'] = max(replies)
                payload['answer'] = replies[max(replies)]
            return payload

        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                entries, new_cursor, turn = self._reconcile_get(path, cursor, min(10.0, remaining))
            except HubError as error:
                return {
                    'status': 'network_unknown',
                    'remote_observation': '远端状态未知',
                    'error_code': error.code,
                    'error': str(error),
                    'session_id': sid,
                    'baseline': baseline,
                    'own_message_observed': own_message,
                    'own_marker_seen': bool(replies),
                    'progress': progress_snapshot(),
                    'observed_at': time.time(),
                }
            if int(new_cursor) < int(cursor) or (entries and int(new_cursor) <= int(cursor)):
                return {
                    'status': 'network_unknown',
                    'remote_observation': '远端状态未知',
                    'error_code': 'REMOTE_STATE_UNKNOWN',
                    'error': 'Grok Bot 回复游标异常',
                    'session_id': sid,
                    'baseline': baseline,
                    'own_message_observed': own_message,
                    'own_marker_seen': bool(replies),
                    # Do not advance progress on cursor anomalies.
                    'observed_at': time.time(),
                }
            for entry in entries:
                seq_u = int(entry['updatedSeq'])
                if seq_u <= baseline:
                    continue
                # Same ownership rules as cancel_remote: own user prompt once; any other user is foreign.
                if entry.get('role') == 'user' and entry.get('kind') == 'message':
                    text = entry.get('text')
                    if (not own_message and isinstance(text, str) and text == prompt):
                        own_message = True
                    else:
                        foreign_user = True
                text = entry.get('text')
                if (entry.get('kind') == 'send-message' and entry.get('role') in (None, 'assistant')
                        and isinstance(text, str)):
                    if text.rstrip().endswith(marker):
                        replies[int(entry['seq'])] = text
                    elif 'HUB-END:' in text and not text.rstrip().endswith(marker):
                        error_marker = True
            cursor = new_cursor
            latest_turn = turn
            # Attribution before busy/complete decisions.
            if foreign_user or error_marker:
                return {
                    'status': 'ambiguous',
                    'remote_observation': '远端出现其他消息或非本任务结束标记',
                    'session_id': sid,
                    'baseline': baseline,
                    'foreign_user': foreign_user,
                    'foreign_marker': error_marker,
                    'own_message_observed': own_message,
                    'own_marker_seen': bool(replies),
                    'observed_at': time.time(),
                }
            busy = turn['inFlight'] or not turn['idle'] or turn['queued'] > 0
            idle_count = idle_count + 1 if not entries and not busy else 0
            if busy:
                return {
                    'status': 'remote_running',
                    'remote_observation': '远端仍在运行',
                    'session_id': sid,
                    'baseline': baseline,
                    'own_message_observed': own_message,
                    'own_marker_seen': bool(replies),
                    'progress': progress_snapshot(),
                    'evidence': {
                        'latest_updated_seq': cursor,
                        'inFlight': turn['inFlight'],
                        'idle': turn['idle'],
                        'queued': turn['queued'],
                        'own_message_observed': own_message,
                        'own_marker_seen': bool(replies),
                    },
                    'observed_at': time.time(),
                }
            # Match execute_task: marker + stable idle for 4 polls; idle alone is never enough.
            if replies and idle_count >= 4:
                return {
                    'status': 'completed',
                    'remote_observation': '远端已完成',
                    'answer': replies[max(replies)],
                    'session_id': sid,
                    'baseline': baseline,
                    'completion_evidence': {
                        'reply_seq': max(replies), 'latest_updated_seq': cursor,
                        'marker': marker, 'idle_polls': idle_count,
                        'own_message_observed': own_message,
                        'turn_idle': latest_turn['idle'] if latest_turn else None,
                        'turn_in_flight': latest_turn['inFlight'] if latest_turn else None,
                    },
                    'observed_at': time.time(),
                    'warnings': ['经由 reconcile_task GET 核对回收；未 POST 新消息'],
                }
            time.sleep(min(0.35, max(0, deadline - time.monotonic())))
        if replies:
            return {
                'status': 'network_unknown',
                'remote_observation': '见到本任务结束标记但 idle 尚未稳定',
                'session_id': sid,
                'baseline': baseline,
                'own_message_observed': own_message,
                'own_marker_seen': True,
                'progress': progress_snapshot(),
                'observed_at': time.time(),
            }
        return {
            'status': 'network_unknown',
            'remote_observation': '核对超时；远端状态仍未知',
            'session_id': sid,
            'baseline': baseline,
            'own_message_observed': own_message,
            'own_marker_seen': bool(replies),
            'progress': progress_snapshot(),
            'observed_at': time.time(),
        }
