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
    """提交后台任务并立即返回 ID。同一个 request_id 和内容幂等。"""
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
def get_result(task_id: str) -> dict:
    """获取完成结果与差异；未完成返回 ready=false。"""
    return invoke('get_result', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def continue_task(request_id: str, task_id: str, prompt: str, timeout_seconds: int = 1800) -> dict:
    """续接原生会话并创建子任务，保留旧结果和同一工作区。"""
    return invoke('continue_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def cancel_task(task_id: str, reason: str = '用户取消') -> dict:
    """请求取消。Muse 仅停止本地等待，不保证远端停止。"""
    return invoke('cancel_task', locals())


def run(): mcp.run(transport='stdio')
