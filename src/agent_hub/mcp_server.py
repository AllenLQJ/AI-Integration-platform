import asyncio
from pydantic import StrictFloat, StrictInt
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .client import Client
from .models import HubError

mcp = FastMCP('agent-hub')
client = Client()


def invoke(name, arguments):
    try: return client.call(name, arguments)
    except HubError as error: return {'error': {'code': error.code, 'message': str(error)}}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def list_agents() -> dict:
    """列出本机执行器、版本和能力；不发送模型任务。"""
    return invoke('list_agents', {})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def submit_task(request_id: str, agent: str, prompt: str, cwd: str,
                mode: str, timeout_seconds: int = 1800) -> dict:
    """提交后台任务并立即返回 ID。同一个 request_id 和内容幂等。

    Claude: text_only/read_only/edit_worktree；Codex、Cursor: read_only/edit_worktree。
    Muse、Grok Bot 仅 delegate，Hub 不保证其只读，权限由远端/客户端决定。
    permissions 返回限制执行方及允许写入等信息。"""
    return invoke('submit_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_task(task_id: str) -> dict:
    """获取任务状态；不等待执行完成。"""
    return invoke('get_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_events(task_id: str, after_seq: int = 0, limit: int = 50) -> dict:
    """按游标读取任务事件，上限 200 条。"""
    return invoke('get_events', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
async def wait_for_task(task_id: str, after_seq: StrictInt = 0, timeout_seconds: StrictFloat = 10) -> dict:
    """等待六种终态之一或本次等待超时（0–10 秒）；超时不取消任务。

    用 next_seq 续接，has_more=true 时继续取事件；响应丢失按 seq 去重。
    终态后调用 get_result 获取回答。持续等待需要总控再次调用本工具。
    """
    return await asyncio.to_thread(invoke, 'wait_for_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_result(task_id: str) -> dict:
    """获取完成结果与差异；未完成返回 ready=false。"""
    return invoke('get_result', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def continue_task(request_id: str, task_id: str, prompt: str, timeout_seconds: int = 1800) -> dict:
    """续接原生会话并创建子任务，保留旧结果和同一工作区。"""
    return invoke('continue_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def cancel_task(task_id: str, reason: str = '用户取消') -> dict:
    """请求取消；Grok Bot 尝试会话中断，get_result 确认 remote_cancelled。
    Muse 仅停止本地等待，不保证远端停止。已发生操作不自动回滚。
    """
    return invoke('cancel_task', locals())


def run(): mcp.run(transport='stdio')
