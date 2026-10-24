import asyncio
import datetime
import os
import json
import sys
import time
import hashlib
import threading
from pathlib import Path
from ..models import HubError, redact
from ..processes import environment, run_process


class Muse:
    capabilities = ['delegate', 'continue']
    remote_cancel = True
    cancel_scope = 'remote_session'
    cancellation_timeout = 12

    def __init__(self, executable, args=None, env=None, session_id=None):
        self.executable = executable
        self.args = args or ['-m', 'muse_mcp']
        self.env = env or {}
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            raise HubError('INVALID_CONFIG', 'Muse 固定 session_id 需要非空字符串')
        self.session_id = session_id

    def permission_policy(self, mode):
        return {'enforced_by': 'muse_and_client_permissions', 'enforced': False,
                'allows_file_read': True, 'allows_write': True,
                'allows_commands': True, 'os_sandbox': False,
                'workspace': 'remote_and_connected_devices',
                'scope_note': 'Hub 无法限制 Muse 内部工具；写入和其他动作取决于 Muse/客户端权限及审批'}

    def diagnose(self):
        return {'available': Path(self.executable).is_file(), 'capabilities': self.capabilities,
                'platform': os.name, 'remote_cancel': True, 'cancel_scope': self.cancel_scope,
                'session_id': self.session_id,
                'remote_review_required': bool(self.session_id and self.state_file(self.session_id).exists()),
                'authentication': '现有 muse-mcp 认证；实际调用确认有效性',
                'permissions': {'delegate': self.permission_policy('delegate')}}

    @staticmethod
    def response_payload(response):
        # MCP 对 Any 返回值可能只提供 JSON 文本块，不能当成空结果。
        structured = response.structuredContent
        if structured is not None:
            return structured.get('result', structured) if isinstance(structured, dict) else structured
        values = []
        for block in getattr(response, 'content', ()):
            if getattr(block, 'type', None) != 'text':
                raise HubError('INVALID_RESPONSE', 'Muse MCP 返回非 JSON 文本内容')
            try:
                values.append(json.loads(block.text))
            except (ValueError, TypeError) as error:
                raise HubError('INVALID_RESPONSE', 'Muse MCP 返回无效 JSON') from error
        if len(values) == 1:
            return values[0]
        return values

    @staticmethod
    def find_reply(events, baseline, marker):
        if not isinstance(events, list): return None
        replies = [e for e in events if e.get('role') in {'assistant', 'message.assistant'}
                   and e.get('raw', {}).get('payload', {}).get('display_text_ready', True)
                   and e.get('raw', {}).get('payload', {}).get('status', 'completed') == 'completed'
                   and (e.get('seq') or 0) > baseline and marker in e.get('text', '')]
        return max(replies, key=lambda e: e['seq']) if replies else None

    @staticmethod
    def owned_visible_messages(events, baseline, seen):
        """Yield public assistant messages owned by this task after baseline.

        Does not cross sessions or expose non-display/thought payloads. Completion
        marker matching and human-wait rules remain in the caller.
        """
        if not isinstance(events, list):
            return
        for entry in events:
            if entry.get('role') not in {'assistant', 'message.assistant'}:
                continue
            payload = entry.get('raw', {}).get('payload', {}) if isinstance(entry.get('raw'), dict) else {}
            if payload.get('display_text_ready', True) is not True:
                continue
            if payload.get('status', 'completed') != 'completed':
                continue
            if payload.get('kind') in {'thought', 'reasoning', 'hidden'}:
                continue
            seq = entry.get('seq') or 0
            if type(seq) is not int or seq <= baseline or not isinstance(entry.get('text'), str) or not entry['text']:
                continue
            raw_revision = payload.get('revision')
            revision = raw_revision if type(raw_revision) is int else seq
            prior = seen.get(seq)
            if prior is not None and revision <= prior:
                continue
            seen[seq] = revision
            yield entry, revision

    @staticmethod
    def answer_with_links(events, reply):
        import re
        links = list(dict.fromkeys(url.rstrip('.,;:!?。，；：！？') for url in
                    re.findall(r'https?://[^\s<>\[\]"\x27（）。，；！？)]+', reply['text'])))
        appended = []
        for item in sorted(events, key=lambda e: e.get('seq') or 0):
            if (item.get('seq') or 0) <= reply['seq']: continue
            if item.get('role') in {'user', 'message.user'}: break
            payload = item.get('raw', {}).get('payload', {})
            if item.get('event_name') == 'delta.presentation' and payload.get('kind') == 'external_link':
                url = payload.get('data', {}).get('url', '')
                if isinstance(url, str) and url.startswith(('https://', 'http://')) and url not in links:
                    links.append(url)
                    appended.append(url)
        return reply['text'] + ('\n\n' + '\n'.join(appended) if appended else ''), links

    def run(self, task, cancel, event):
        requested = task.get('context', {}).get('payload', {}).get('requested_output', {})
        if requested.get('media_type') == 'video':
            raise HubError('UNSUPPORTED_MEDIA_TYPE', 'Muse 视频适配尚未验收')
        if requested.get('media_type') == 'image' and requested.get('model') != 'muse:auto':
            raise HubError('UNSUPPORTED_MODEL', 'Muse 仅支持已约定的 muse:auto 默认图片路由')
        path = Path(task['log_dir']) / 'muse-input.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'task': task, 'executable': self.executable,
                       'args': self.args, 'env': self.env, 'session_id': self.session_id}, ensure_ascii=False), encoding='utf-8')
        path.chmod(0o600)
        try:
            rc, stdout, stderr, logs = run_process(
                [sys.executable, '-m', 'agent_hub.adapters.muse_worker', str(path)],
                task['workspace'], environment(), cancel, lambda: task['deadline'], task['log_dir'], event)
        except HubError as error:
            if error.code not in {'CANCELLED', 'TIMED_OUT'}:
                raise
            raise self.cancel_after_local_stop(task, path, error, event) from None
        result = None
        for line in stdout.splitlines():
            try: data = json.loads(line)
            except ValueError: continue
            if isinstance(data, dict) and 'hub_result' in data: result = data['hub_result']
        if not result: raise HubError('MUSE_ERROR', 'Muse 执行器未返回结构化结果')
        if result.get('error_code'):
            details = {key: result[key] for key in ('stage', 'upstream_code', 'rpc_code') if key in result}
            error = HubError(result['error_code'], result['error'], details)
            if error.code in {'CANCELLED', 'TIMED_OUT'}:
                raise self.cancel_after_local_stop(task, path, error, event) from None
            raise error
        exports = result.pop('media_exports', [])
        if exports:
            result['media_artifacts'] = self.fetch_media(task, exports, cancel, event)
            result['media_generation_evidence'] = [a['generation_evidence'] for a in result['media_artifacts']]
        result['artifacts'] = {'logs': logs}
        return result

    def fetch_media(self, task, images, cancel, event):
        from PIL import Image
        root = Path(task['media_artifact_root']) / task['id']
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for index, item in enumerate(images):
            item['filename'] = 'result-' + str(index + 1) + ('.png' if item['mime_type'] == 'image/png' else '.jpeg')
        input_path = Path(task['log_dir']) / 'muse-media-input.json'
        data = {'root': str(root), 'images': images,
                'timeout_seconds': max(.1, min(30, task['deadline'] - time.time()))}
        input_path.write_text(json.dumps(data), encoding='utf-8')
        input_path.chmod(0o600)
        _, stdout, _, _ = run_process(
            [self.executable, str(Path(__file__).with_name('muse_media_worker.py')), str(input_path)],
            task['workspace'], environment(self.env), cancel, task['deadline'],
            str(Path(task['log_dir']) / 'media-transfer'), event)
        responses = [json.loads(line)['hub_result'] for line in stdout.splitlines()
                     if line.startswith('{') and 'hub_result' in line]
        if not responses or not responses[-1].get('ok'):
            raise HubError('ARTIFACT_FETCH_FAILED', 'Muse 授权原图取回失败；未重发生成')
        artifacts = []
        for item in images:
            path = root / item['filename']
            try:
                with Image.open(path) as decoded:
                    if decoded.format != ('PNG' if item['mime_type'] == 'image/png' else 'JPEG'):
                        raise ValueError()
                    decoded.verify()
                with Image.open(path) as decoded:
                    decoded.load()
                    dimensions = {'width': decoded.width, 'height': decoded.height}
            except Exception:
                raise HubError('INVALID_ARTIFACT', 'Muse 成品无法按声明格式解码') from None
            artifacts.append({'path': str(path), 'mime_type': item['mime_type'],
                'generation_evidence': {'presentation_id': item['presentation_id'], 'seq': item['seq'],
                    'size_bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'transfer': 'authenticated_noise_original', **dimensions}})
        return artifacts

    def cancel_after_local_stop(self, task, path, error, event):
        context = {}
        # 原等待进程树已终止，再开单独连接清理原会话，避免 Noise 双消费者。
        output = Path(task['log_dir']) / 'stdout.log'
        for line in output.read_text(encoding='utf-8').splitlines() if output.exists() else []:
            try:
                entry = json.loads(line).get('hub_event', {})
                if entry.get('kind') == 'accepted':
                    context = entry['data']
            except (ValueError, AttributeError, KeyError):
                pass
        details = {'remote_cancelled': False, 'cancel_scope': self.cancel_scope}
        if context.get('session_id'):
            data = json.loads(path.read_text(encoding='utf-8'))
            data['cancel_context'] = context
            cleanup = Path(task['log_dir']) / 'muse-cancel-input.json'
            cleanup.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            cleanup.chmod(0o600)
            try:
                _, text, _, _ = run_process(
                    [sys.executable, '-m', 'agent_hub.adapters.muse_cancel_worker', str(cleanup)],
                    task['workspace'], environment(), threading.Event(),
                    time.time()+20, str(Path(task['log_dir'])/'cancel'), event)
                for line in text.splitlines():
                    value = json.loads(line)
                    if isinstance(value, dict) and 'hub_result' in value:
                        details.update(value['hub_result'])
            except (HubError, ValueError):
                pass
        if not details['remote_cancelled']:
            details['warning'] = 'Muse 原会话停止未确认；保留会话锁，禁止直接续发'
        event('remote_cancel', details)
        return HubError(error.code, '本地进程已停止；远端停止见 remote_cancelled',
                       dict(error.details, **details))

    def state_file(self, sid):
        root = Path(self.env.get('HUB_MUSE_STATE_DIR', str(Path.home()/'.config/agent-hub/muse-sessions')))
        return root / (hashlib.sha256(sid.encode()).hexdigest()[:24]+'.json')

    def reserve(self, task, sid):
        path = self.state_file(sid)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with path.open('x', encoding='utf-8') as output:
                path.chmod(0o600)
                json.dump({'task_id': task['id'], 'session_id': sid}, output)
        except FileExistsError:
            raise HubError('REMOTE_STATE_UNKNOWN', 'Muse 原会话存在未核对派发；禁止续发') from None

    @staticmethod
    def unnest(value):
        for _ in range(4):
            if isinstance(value, dict) and 'result' in value:
                value = value['result']
            else:
                break
        return value

    async def cancel_remote(self, task, context):
        from .muse_cancellation import cancel_remote
        return await cancel_remote(self, task, context)

    @staticmethod
    def unwrap(error):
        if isinstance(error, HubError): return error
        children = [Muse.unwrap(e) for e in getattr(error, 'exceptions', ())]
        return next((e for e in children if isinstance(e, HubError)), children[0] if children else error)

    async def execute(self, task, cancel, event):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.shared.exceptions import McpError
        params = StdioServerParameters(command=self.executable, args=self.args,
                                       env=environment(dict(self.env, MUSE_MCP_ENABLE_RAW='1',
                                                            MUSE_MCP_REUSE_GATEWAY='1')))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                async def call(name, arguments):
                    stage = arguments.get('method', name)
                    if cancel.is_set(): raise HubError('CANCELLED', '已停止本地等待')
                    if time.time() >= task['deadline']: raise HubError('TIMED_OUT', '任务期限到达')
                    operation = asyncio.create_task(session.call_tool(name, arguments,
                                      read_timeout_seconds=datetime.timedelta(seconds=45)))
                    try:
                        while not operation.done():
                            if cancel.is_set(): raise HubError('CANCELLED', '已停止本地等待')
                            if time.time() >= task['deadline']: raise HubError('TIMED_OUT', '任务期限到达')
                            await asyncio.wait({operation}, timeout=.2)
                        try:
                            response = operation.result()
                        except McpError as error:
                            code = 'TIMEOUT' if error.error.code == 408 else 'MUSE_ERROR'
                            raise HubError(code, stage + ': ' + code + ': ' + redact(error.error.message),
                                           {'stage': stage, 'upstream_code': code,
                                            'rpc_code': error.error.code}) from error
                    finally:
                        if not operation.done():
                            operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
                    if response.isError:
                        raise HubError('MUSE_ERROR', stage + ': Muse MCP 调用失败')
                    payload = self.response_payload(response)
                    if name == 'muse_history' and response.structuredContent is None and isinstance(payload, dict) and 'ok' not in payload:
                        payload = [payload]
                    if isinstance(payload, dict) and payload.get('ok') is False:
                        error = payload.get('error') or {}
                        if not isinstance(error, dict): error = {}
                        # Muse's short reply wait can expire after confirmed dispatch.
                        # This is not a failed send: collect the reply through history.
                        if (name == 'muse_send' and payload.get('sent') is True
                                and error.get('code') == 'TIMEOUT'):
                            return payload
                        code = redact(error.get('code') or 'MUSE_ERROR')
                        message = redact(error.get('message') or 'Muse 请求失败')
                        raise HubError(code, stage + ': ' + code + ': ' + message,
                                       dict({'stage': stage, 'upstream_code': code}, **{
                                           key: error[key] for key in
                                           ('status', 'exception_type', 'dispatch_attempted') if key in error},
                                           **({'muse_stage': error['stage']} if 'stage' in error else {})))
                    return payload

                async def pause(seconds):
                    end = time.monotonic() + seconds
                    while time.monotonic() < end:
                        if cancel.is_set(): raise HubError('CANCELLED', '已停止本地等待')
                        remaining = task['deadline'] - time.time()
                        if remaining <= 0: raise HubError('TIMED_OUT', '任务期限到达')
                        await asyncio.sleep(min(.2, remaining, end - time.monotonic()))

                async def read_history(stage):
                    for attempt in range(1, 4):
                        try:
                            history = await call('muse_history', {'thread': sid, 'limit': 100})
                            if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
                                raise HubError('INVALID_RESPONSE', stage + ': Muse 历史必须是事件列表')
                            return history, attempt > 1
                        except (HubError, OSError, TimeoutError) as error:
                            code = error.code if isinstance(error, HubError) else 'GATEWAY_ERROR'
                            if code not in {'GATEWAY_ERROR', 'TIMEOUT', 'VM_UNAVAILABLE'} or attempt == 3:
                                raise
                            event('retry', {'stage': stage, 'session_id': sid,
                                            'attempt': attempt, 'error_code': code})
                            await pause(attempt)
                sid = task.get('resume_session') or self.session_id
                if self.session_id and sid != self.session_id:
                    raise HubError('SESSION_UNAVAILABLE', '固定 Muse 会话不能续接另一会话')
                if sid:
                    status = self.unnest(await call('muse_raw', {'method': 'sessions.get',
                                                        'path_params': {'id': sid}}))
                    if (not isinstance(status, dict) or status.get('session_id') != sid
                            or status.get('status') not in {'completed', 'interrupted'}):
                        raise HubError('WORKSPACE_BUSY', 'Muse 原会话尚未停止，不投递新的任务')
                if not sid:
                    created = await call('muse_raw', {'method': 'session.start', 'body': {
                        'method': '/api/session/start', 'params': {
                            'origin': 'fresh', 'lifecycle': 'persistent', 'title': 'Agent Hub ' + task['id'][:8]}}})
                    payload = created
                    for _ in range(4):
                        if isinstance(payload, dict) and payload.get('session_id'): break
                        payload = payload.get('result', {}) if isinstance(payload, dict) else {}
                    sid = payload.get('session_id') if isinstance(payload, dict) else None
                    if not sid: raise HubError('MUSE_ERROR', '无法创建独立 Muse 会话')
                event('session', {'session_id': sid})
                history, _ = await read_history('baseline_history')
                baseline = max((e.get('seq', 0) for e in history), default=0)
                marker = '[HUB:' + task['id'] + ']'
                prompt = (task['prompt'] + '\n完成本轮回答后，请在最终回答的最后一行逐字写出 ' + marker +
                          '。不要在接收确认、进度消息或工具执行前写此标记。'
                          '\n遇到必须由用户处理的人机验证、登录或审批时，立即说明阻碍并在求助消息末尾写出 '
                          '[HUB_WAIT:' + task['id'] + ':human_verification]（登录改为 login，审批改为 approval）。'
                          '等待用户接管；不要自动完成人机验证。求助消息不能写最终完成标记。')
                if cancel.is_set(): raise HubError('CANCELLED', '已停止本地等待')
                owned_messages = [prompt]
                def save_owned_messages():
                    if task.get('log_dir'):
                        ledger = Path(task['log_dir']) / 'muse-owned-messages.json'
                        ledger.write_text(json.dumps(owned_messages, ensure_ascii=False), encoding='utf-8')
                        ledger.chmod(0o600)
                if task.get('log_dir'):
                    self.reserve(task, sid)
                    save_owned_messages()
                try:
                    sent = await call('muse_send', {'thread': sid, 'message': prompt,
                                                  'wait_seconds': 0, 'fire_and_forget': True})
                except Exception as error:
                    if isinstance(error, HubError) and error.code == 'CANCELLED': raise
                    code = error.code if isinstance(error, HubError) else type(error).__name__
                    details = dict(error.details) if isinstance(error, HubError) else {}
                    details.update(stage='muse_send', upstream_code=code)
                    raise HubError('DELIVERY_UNKNOWN', '发送状态不确定，未重发: ' + code,
                                   details) from error
                if not isinstance(sent, dict) or not sent.get('sent'):
                    raise HubError('DELIVERY_UNKNOWN', '未取得确定的发送确认，未重发')
                event('accepted', {'session_id': sid, 'baseline': baseline, 'acknowledged': sent.get('acknowledged', False)})
                from .muse_wait import HumanWait, find_intervention
                human_wait = HumanWait(task, sid, event)
                previous_snapshot = None
                progress_seen = {}
                while time.time() < task['deadline']:
                    if cancel.is_set(): raise HubError('CANCELLED', '仅停止本地等待')
                    try:
                        history, retried = await read_history('reply_history')
                        if retried: previous_snapshot = None
                    except HubError as error:
                        if error.code in {'CANCELLED', 'TIMED_OUT'}: raise
                        details = dict(error.details, stage='reply_history', upstream_code=error.code)
                        raise HubError('REMOTE_STATE_UNKNOWN', '已发送，但读取回复失败；远端可能继续执行: ' +
                                       error.code, details) from error
                    except Exception as error:
                        raise HubError('REMOTE_STATE_UNKNOWN', '已发送，但连接中断；远端可能继续执行') from error
                    for entry, revision in self.owned_visible_messages(history, baseline, progress_seen):
                        text = entry['text']
                        event('message', {
                            'message': text[:1000],
                            'role': 'assistant',
                            'provider_message_id': str(entry.get('seq')),
                            'provider_revision': revision if type(revision) is int else 0,
                            'truncated': len(text) > 1000,
                        })
                    intervention = human_wait.observe(history, baseline)
                    reply = self.find_reply(history, max(baseline, human_wait.reply_after), marker)
                    if intervention and find_intervention(history, baseline, task['id']):
                        reply = None
                    if reply and human_wait.current:
                        human_wait.resume()
                    if human_wait.consume_confirmation():
                        try:
                            resume_prompt = ('用户已确认人工处理完成，请在原任务范围内继续；若仍受阻请再次报告。'
                                             '完成后最后一行写出 ' + marker)
                            owned_messages.append(resume_prompt)
                            save_owned_messages()
                            resumed = await call('muse_send', {'thread': sid, 'message': resume_prompt,
                                                  'wait_seconds': 0, 'fire_and_forget': True})
                            if not isinstance(resumed, dict) or not resumed.get('sent'):
                                raise HubError('DELIVERY_UNKNOWN', '未取得恢复发送确认')
                        except Exception as error:
                            if isinstance(error, HubError) and error.code in {'CANCELLED', 'TIMED_OUT'}:
                                raise
                            raise HubError('DELIVERY_UNKNOWN', '恢复发送状态不确定，未重发') from error
                    if reply:
                        answer, links = self.answer_with_links(history, reply)
                        media_requested = task.get('context', {}).get('payload', {}).get('requested_output', {}).get('media_type') == 'image'
                        from .muse_media import images_for_task
                        images = images_for_task(history, baseline) if media_requested else []
                        snapshot = (reply['seq'], answer, reply.get('reply_to_message_id', ''),
                                    json.dumps(images, sort_keys=True))
                        if snapshot == previous_snapshot:
                            media_artifacts = []
                            if media_requested:
                                status = self.unnest(await call('muse_raw', {'method': 'sessions.get', 'path_params': {'id': sid}}))
                                if not isinstance(status, dict) or status.get('status') != 'completed':
                                    await pause(1)
                                    continue
                                if not images:
                                    raise HubError('ARTIFACT_NOT_FOUND', 'Muse 回复已完成，但未提供结构化真实成品附件')
                                media_artifacts = [dict(i, url=None) for i in images]
                                for image in images:
                                    answer = answer.replace(image['url'], '[媒体附件取回地址已隐藏]')
                                links = [url for url in links if url not in {i['url'] for i in images}]
                            if task.get('log_dir'):
                                self.state_file(sid).unlink()
                            return {'answer': answer, 'source_urls': links, 'session_id': sid,
                                    'media_exports': [{k: v for k, v in a.items() if k != 'url'} for a in media_artifacts],
                                    'completion_evidence': {
                                'session_id': sid, 'baseline': baseline, 'reply_seq': reply['seq'], 'stable_polls': 2,
                                'reply_to_message_id': reply.get('reply_to_message_id', ''),
                                'method': '独立会话+序号+completed消息+最终标记+两次稳定读取（不等于远端工作流完成）'}}
                        previous_snapshot = snapshot
                    else:
                        previous_snapshot = None
                    await pause(1)
                raise HubError('TIMED_OUT', 'Muse 等待超时；远端可能继续执行')
