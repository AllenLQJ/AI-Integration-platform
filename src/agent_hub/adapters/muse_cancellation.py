"""当前网页 chat.cancel {session_id} 契约；ACK 后独立读取 session 状态。"""
import asyncio
import datetime
import json
import time
from pathlib import Path
from .muse import Muse
from ..processes import environment


async def cancel_remote(adapter, task, context):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    sid = context.get('session_id')
    result = {'remote_cancelled': False, 'cancel_scope': 'remote_session',
              'interrupt_requested': False, 'interrupt_acknowledged': False}
    baseline = context.get('baseline')
    if not isinstance(sid, str) or not sid or type(baseline) is not int:
        return result
    marker = '[HUB:' + task['id'] + ']'
    prompt = task['prompt'] + '\n完成本轮回答后，请在最终回答的最后一行逐字写出 ' + marker + \
             '。不要在接收确认、进度消息或工具执行前写此标记。'
    expected_messages = [prompt]
    ledger = Path(task['log_dir']) / 'muse-owned-messages.json' if task.get('log_dir') else None
    if ledger and ledger.exists():
        try:
            expected_messages = json.loads(ledger.read_text(encoding='utf-8'))
            if not isinstance(expected_messages, list) or not expected_messages or any(
                    not isinstance(text, str) for text in expected_messages):
                return result
        except (ValueError, OSError):
            return result
    path = adapter.state_file(sid)
    def ownership():
        if json.loads(path.read_text(encoding='utf-8')) != {'task_id': task['id'], 'session_id': sid}:
            raise ValueError('ownership')
    params = StdioServerParameters(command=adapter.executable, args=adapter.args,
        env=environment(dict(adapter.env, MUSE_MCP_ENABLE_RAW='1', MUSE_MCP_REUSE_GATEWAY='1')))
    try:
        ownership()
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                end = time.monotonic() + adapter.cancellation_timeout
                async def call(name, args):
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    response = await asyncio.wait_for(session.call_tool(name, args,
                        read_timeout_seconds=datetime.timedelta(seconds=min(4, remaining))), min(4, remaining))
                    value = Muse.response_payload(response)
                    if response.isError or isinstance(value, dict) and value.get('ok') is False:
                        raise ValueError('response')
                    return value
                idle = 0
                fingerprint = None
                while time.monotonic() < end:
                    history = await call('muse_history', {'thread': sid, 'limit': 100})
                    if not isinstance(history, list):
                        raise ValueError('history')
                    users = [e for e in history if e.get('role') in {'user', 'message.user'}
                             and (e.get('seq') or 0) > baseline]
                    observed = [e.get('text') for e in users]
                    if not observed or observed != expected_messages[:len(observed)]:
                        # 尚未入历史则等待；其他消息则不取消整个会话。
                        if users:
                            raise ValueError('foreign message')
                        await asyncio.sleep(.25)
                        continue
                    status = Muse.unnest(await call('muse_raw', {'method': 'sessions.get',
                                              'path_params': {'id': sid}, 'timeout': 4}))
                    if status.get('session_id') != sid:
                        raise ValueError('session mismatch')
                    completed = Muse.find_reply(history, baseline, marker) is not None
                    if not completed and not result['interrupt_requested']:
                        ownership()
                        result['interrupt_requested'] = True
                        ack = Muse.unnest(await call('muse_raw', {'method': 'chat.cancel',
                                               'body': {'session_id': sid}, 'timeout': 4}))
                        result['interrupt_acknowledged'] = ack.get('cancelled') is True
                        # 取消后重新读取状态，不能复用 POST 前的 completed。
                        continue
                    snapshot = json.dumps(history, ensure_ascii=False, sort_keys=True)
                    stopped = (status.get('status') in {'completed', 'interrupted'} if result['interrupt_acknowledged']
                               else completed and status.get('status') == 'completed')
                    idle = idle+1 if (stopped and snapshot == fingerprint) else 0
                    fingerprint = snapshot
                    if idle >= 2:
                        ownership()
                        path.unlink()
                        result.update(remote_cancelled=True, cancellation_evidence={
                            'session_id': sid, 'status': status['status'], 'stable_polls': idle,
                            'own_message_observed': True,
                            'stop_reason': 'chat.cancel' if result['interrupt_acknowledged'] else 'already_completed'})
                        return result
                    await asyncio.sleep(.25)
    except Exception:
        pass
    result['warning'] = 'Muse 原会话停止未确认或出现其他消息；保留锁，未重试取消'
    return result
