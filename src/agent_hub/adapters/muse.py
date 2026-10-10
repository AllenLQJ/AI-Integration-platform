import asyncio
import datetime
import os
import json
import sys
import time
from pathlib import Path
from ..models import HubError
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
                   and not e.get('reply_to_message_id')
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
        if result.get('error_code'): raise HubError(result['error_code'], result['error'])
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
        params = StdioServerParameters(command=self.executable, args=self.args,
                                       env=environment(dict(self.env, MUSE_MCP_ENABLE_RAW='1')))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                async def call(name, arguments):
                    operation = asyncio.create_task(session.call_tool(name, arguments,
                                      read_timeout_seconds=datetime.timedelta(seconds=45)))
                    try:
                        while not operation.done():
                            if cancel.is_set(): raise HubError('CANCELLED', '已停止本地等待')
                            if time.time() >= task['deadline']: raise HubError('TIMED_OUT', '任务期限到达')
                            await asyncio.wait({operation}, timeout=.2)
                        response = operation.result()
                    finally:
                        if not operation.done():
                            operation.cancel()
                            await asyncio.gather(operation, return_exceptions=True)
                    if response.isError:
                        raise HubError('MUSE_ERROR', 'Muse MCP 调用失败')
                    return (response.structuredContent or {}).get('result')
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
                history = await call('muse_history', {'thread': sid, 'limit': 100})
                baseline = max((e.get('seq', 0) for e in history), default=0) if isinstance(history, list) else 0
                marker = '[HUB:' + task['id'] + ']'
                prompt = (task['prompt'] + '\n完成本轮回答后，请在最终回答的最后一行逐字写出 ' + marker +
                          '。不要在接收确认、进度消息或工具执行前写此标记。')
                if cancel.is_set(): raise HubError('CANCELLED', '已停止本地等待')
                try:
                    sent = await call('muse_send', {'thread': sid, 'message': prompt, 'wait_seconds': 1})
                except Exception as error:
                    raise HubError('DELIVERY_UNKNOWN', '发送状态不确定，未重发') from error
                if not isinstance(sent, dict) or not sent.get('sent'):
                    raise HubError('DELIVERY_UNKNOWN', '未取得确定的发送确认，未重发')
                event('accepted', {'session_id': sid, 'baseline': baseline, 'acknowledged': sent.get('acknowledged', False)})
                previous_snapshot = None
                while time.time() < task['deadline']:
                    if cancel.is_set(): raise HubError('CANCELLED', '仅停止本地等待')
                    try:
                        history = await call('muse_history', {'thread': sid, 'limit': 100})
                    except HubError as error:
                        if error.code in {'CANCELLED', 'TIMED_OUT'}: raise
                        raise HubError('REMOTE_STATE_UNKNOWN', '已发送，但读取回复失败；远端可能继续执行') from error
                    except Exception as error:
                        raise HubError('REMOTE_STATE_UNKNOWN', '已发送，但连接中断；远端可能继续执行') from error
                    reply = self.find_reply(history, baseline, marker)
                    if reply:
                        answer, links = self.answer_with_links(history, reply)
                        snapshot = (reply['seq'], answer)
                        if snapshot == previous_snapshot:
                            return {'answer': answer, 'source_urls': links, 'session_id': sid, 'completion_evidence': {
                                'session_id': sid, 'baseline': baseline, 'reply_seq': reply['seq'], 'stable_polls': 2,
                                'method': '独立会话+序号+completed消息+最终标记+两次稳定读取（不等于远端工作流完成）'}}
                        previous_snapshot = snapshot
                    await asyncio.sleep(1)
                raise HubError('TIMED_OUT', 'Muse 等待超时；远端可能继续执行')
