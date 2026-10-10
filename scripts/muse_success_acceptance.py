"""Explicit live Muse acceptance through an isolated current-code Hub MCP instance."""
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from agent_hub.client import Client
from agent_hub.config import data_dir, private_dir


async def main():
    original = data_dir()
    stamp = uuid.uuid4().hex
    root = original / 'acceptance' / ('muse-success-' + stamp)
    private_dir(root)
    item = json.loads((original / 'config.json').read_text(encoding='utf-8'))['agents']['muse']
    env = {key: value for key, value in item.get('env', {}).items()
           if key in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}}
    for key, value in env.items():
        if key != 'NO_PROXY' and (urlsplit(value).username or urlsplit(value).password):
            raise ValueError('credential-bearing proxy configuration is not copied')
    config = {'agents': {'muse': {'executable': item['executable'],
                                  'args': item.get('args', ['-m', 'muse_mcp']), 'env': env}}}
    (root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    params = StdioServerParameters(command=sys.executable, args=['-m', 'agent_hub', 'mcp'],
        env=dict(os.environ, AGENT_HUB_DATA_DIR=str(root), PYTHONUTF8='1'))
    final = None
    try:
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                async def call(name, args):
                    response = await session.call_tool(name, args)
                    if response.isError: raise RuntimeError('MCP invocation failed')
                    result = response.structuredContent
                    if result is None:
                        result = json.loads(next(c.text for c in response.content if c.type == 'text'))
                    return result
                agents = await call('list_agents', {})
                if not agents.get('muse', {}).get('available'): raise RuntimeError('Muse unavailable')
                marker = 'MUSE_SUCCESS_' + stamp
                task = await call('submit_task', dict(agent='muse', mode='read_only',
                    cwd=r'D:\projects\AI-Integration-platform', request_id='live-success-' + stamp,
                    timeout_seconds=300, prompt='只读验收，无需工具或搜索，不修改文件，不发送其他消息。'
                    '请只输出验收词 ' + marker + ' 和本任务要求的完成标记。'))
                if not task.get('id'): raise RuntimeError('submission failed')
                print(json.dumps({'task_id': task['id'], 'request_id': task['request_id'],
                                  'data_dir': str(root)}, ensure_ascii=False), flush=True)
                state = None
                deadline = time.monotonic() + 330
                while time.monotonic() < deadline:
                    current = await call('get_task', {'task_id': task['id']})
                    if current['state'] != state:
                        state = current['state']
                        print(json.dumps({'state': state, 'session_id': current.get('session_id')}), flush=True)
                    final = await call('get_result', {'task_id': task['id']})
                    if final.get('ready'): break
                    await asyncio.sleep(2)
                events = await call('get_events', {'task_id': task['id'], 'limit': 200})
                summary = {'task': task, 'result': final, 'events': events}
                (root / 'acceptance-summary.json').write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                evidence = (final or {}).get('completion_evidence', {})
                assert final.get('ready') is True and final.get('state') == 'succeeded', 'Hub did not succeed'
                assert marker in final.get('answer', ''), 'acceptance marker missing'
                assert '[HUB:' + task['id'] + ']' in final['answer'], 'task marker missing'
                assert evidence.get('stable_polls') == 2 and evidence.get('reply_seq', 0) > evidence.get('baseline', 0)
                assert evidence.get('session_id') == final.get('session_id')
                assert 'reply_to_message_id' in evidence, 'current-code evidence field missing'
                print('LIVE_ACCEPTANCE_PASSED', flush=True)
    finally:
        if final and final.get('ready'):
            Client(root).stop()  # Only our isolated daemon; never the shared instance.


if __name__ == '__main__': asyncio.run(main())
