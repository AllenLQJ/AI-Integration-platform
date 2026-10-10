import asyncio
import datetime
import os
import json
import sys
import time
from pathlib import Path
from ..models import HubError, redact
from ..processes import environment, run_process


class Muse:
    capabilities = ['read_only', 'continue']
    remote_cancel = False

    def __init__(self, executable, args=None, env=None):
        self.executable = executable
        self.args = args or ['-m', 'muse_mcp']
        self.env = env or {}

    def diagnose(self):
        return {'available': Path(self.executable).is_file(), 'capabilities': self.capabilities,
                'platform': os.name, 'remote_cancel': False,
                'authentication': '现有 muse-mcp 认证；实际调用确认有效性'}

    @staticmethod
    def find_reply(events, baseline, marker):
        if not isinstance(events, list): return None
        replies = [e for e in events if e.get('role') in {'assistant', 'message.assistant'}
                   and e.get('raw', {}).get('payload', {}).get('display_text_ready', True)
                   and e.get('raw', {}).get('payload', {}).get('status', 'completed') == 'completed'
                   and (e.get('seq') or 0) > baseline and marker in e.get('text', '')]
        return max(replies, key=lambda e: e['seq']) if replies else None

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
        path = Path(task['log_dir']) / 'muse-input.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'task': task, 'executable': self.executable,
                       'args': self.args, 'env': self.env}, ensure_ascii=False), encoding='utf-8')
        rc, stdout, stderr, logs = run_process(
            [sys.executable, '-m', 'agent_hub.adapters.muse_worker', str(path)],
            task['workspace'], environment(), cancel, task['deadline'], task['log_dir'], event)
        result = None
        for line in stdout.splitlines():
            try: data = json.loads(line)
            except ValueError: continue
            if isinstance(data, dict) and 'hub_result' in data: result = data['hub_result']
        if not result: raise HubError('MUSE_ERROR', 'Muse 执行器未返回结构化结果')
        if result.get('error_code'):
            details = {key: result[key] for key in ('stage', 'upstream_code', 'rpc_code') if key in result}
            raise HubError(result['error_code'], result['error'], details)
        result['artifacts'] = {'logs': logs}
        return result

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
                    payload = (response.structuredContent or {}).get('result')
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
                                       {'stage': stage, 'upstream_code': code})
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
                sid = task.get('resume_session')
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
                          '。不要在接收确认、进度消息或工具执行前写此标记。')
                if cancel.is_set(): raise HubError('CANCELLED', '已停止本地等待')
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
                previous_snapshot = None
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
                    reply = self.find_reply(history, baseline, marker)
                    if reply:
                        answer, links = self.answer_with_links(history, reply)
                        snapshot = (reply['seq'], answer, reply.get('reply_to_message_id', ''))
                        if snapshot == previous_snapshot:
                            return {'answer': answer, 'source_urls': links, 'session_id': sid, 'completion_evidence': {
                                'session_id': sid, 'baseline': baseline, 'reply_seq': reply['seq'], 'stable_polls': 2,
                                'reply_to_message_id': reply.get('reply_to_message_id', ''),
                                'method': '独立会话+序号+completed消息+最终标记+两次稳定读取（不等于远端工作流完成）'}}
                        previous_snapshot = snapshot
                    else:
                        previous_snapshot = None
                    await pause(1)
                raise HubError('TIMED_OUT', 'Muse 等待超时；远端可能继续执行')
