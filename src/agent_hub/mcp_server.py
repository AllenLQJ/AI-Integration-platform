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
    except HubError as error: return {'error': error.payload()}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def list_agents() -> dict:
    """列出本机执行器、版本和能力；不发送模型任务。"""
    return invoke('list_agents', {})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def create_work_item(request_id: str, title: str = '工作项', acceptance: dict | None = None) -> dict:
    """创建稳定协作工作项容器（work_item_id），独立于验收 attempt / Verification.root_id。

    可选 acceptance：软件看板可用 software_dashboard（tests/ui/review 三条 required content_review），
    普通对话可用 {'policy':'no_extra'}。开发/返修派发默认继承；审核任务不继承。
    公开派发开发、审核、返修前须先创建并携带同一 work_item_id。刷新 MCP 工具列表后可见本工具。
    """
    args = {k: v for k, v in locals().items() if v is not None}
    return invoke('create_work_item', args)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def submit_task(request_id: str, agent: str, prompt: str, cwd: str,
                mode: str, timeout_seconds: int = 1800, context: dict | None = None, acceptance: dict | None = None,
                node_id: str | None = None, work_item_id: str | None = None,
                relation: str | None = None, related_task_id: str | None = None) -> dict:
    """提交后台任务并立即返回 ID。同一个 request_id 和内容幂等。

    必须提供 work_item_id（先 create_work_item）。缺失返回 WORK_ITEM_REQUIRED，不会静默创建根任务。
    审核/返修还须 relation=review|rework 与同一工作项内的 related_task_id。
    Claude: text_only/read_only/edit_worktree；Codex、cursor_cli: read_only/edit_worktree。
    cursor 配置 kind=ide 时为 IDE 原生界面桥，delegate/edit_worktree，需要外部 UI worker 在线。
    Muse、Grok Bot 仅 delegate，Hub 不保证其只读，权限由远端/客户端决定。
    插件与 command 执行器名称可由配置定义；提交前用 list_agents 检查 modes/permissions。
    permissions 返回限制执行方及允许写入等信息。
    acceptance.review={agent:执行器名称, timeout_seconds?} 显式开启 content_review 自动审核，审核端须支持 text_only 且不能自审核。
    迁移：刷新 MCP 工具列表；旧历史任务仍可读，可用 link_task 补关联。"""
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
    """等待终态、waiting_for_user 或单次超时（0–10 秒）；超时不取消任务。

    用 next_seq 续接，has_more=true 时继续取事件；响应丢失按 seq 去重。
    终态后调用 get_result 获取回答。needs_user=true 时停止轮询并提示用户接管 Muse；
    用户确认处理完成后调用 resume_task，保留原任务，不能重复提交或调用 continue_task。
    """
    return await asyncio.to_thread(invoke, 'wait_for_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_result(task_id: str) -> dict:
    """获取完成结果与差异；未完成返回 ready=false。"""
    return invoke('get_result', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def continue_task(request_id: str, task_id: str, prompt: str, timeout_seconds: int = 1800,
                  acceptance: dict | None = None, controller_id: str | None = None,
                  controller_epoch: int | None = None) -> dict:
    """续接原生会话并创建子任务，保留旧结果和同一工作区；已被续接的验收尝试返回 ATTEMPT_SUPERSEDED。

    可选 acceptance 仅作用于新子任务（不追改旧 attempt）；省略则继承父任务验收。
    需刷新 MCP／新客户端后可见本参数。安全原生验收写入口不暴露给模型 MCP。
    """
    args = {k: v for k, v in locals().items() if v is not None}
    return invoke('continue_task', args)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def cancel_task(task_id: str, reason: str = '用户取消', controller_id: str | None = None, controller_epoch: int | None = None) -> dict:
    """请求取消；Grok Bot 尝试会话中断，get_result 确认 remote_cancelled。
    Muse 同样尝试原会话中断；SSH 执行器取消原目标机任务。已发生操作不自动回滚。
    """
    return invoke('cancel_task', locals())


def run(connection=None):
    global client
    if connection is not None:
        client = connection
    mcp.run(transport='stdio')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_capabilities() -> dict:
    """查询 HubGateway v1 能力、实际模型与验收证据，不发送模型任务。"""
    return invoke('get_capabilities', {})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_artifact(artifact_id: str, target_host_id: str | None = None, target_node_id: str | None = None) -> dict:
    """返回已登记媒体附件的授权取回描述；仅支持同主机 local_file。"""
    return invoke('get_artifact', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def resume_task(request_id: str, task_id: str, intervention_id: str) -> dict:
    """仅在用户明确确认人工验证/登录/审批已处理后调用；恢复原 Muse 任务。

    intervention_id 来自 get_task/wait_for_task 的 intervention；同一介入只恢复一次。
    不代替用户完成验证，不创建新任务；确认后继续查询原 task_id。
    """
    return invoke('resume_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def verify_task(request_id: str, task_id: str, controller_id: str | None = None, controller_epoch: int | None = None) -> dict:
    """由 Hub 重新检查当前执行尝试；相同请求键只检查一次；非 succeeded 任务返回 INVALID_STATE。"""
    return invoke('verify_task', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_work_item(task_id: str) -> dict:
    """读取根任务及所有尝试、验收历史、产物和待处理动作。"""
    return invoke('get_work_item', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def claim_controller(request_id: str, task_id: str, controller_id: str) -> dict:
    """接管工作项，返回递增 controller_epoch；相同 request_id 返回原租约收据。"""
    return invoke('claim_controller', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def switch_executor(request_id: str, task_id: str, agent: str, mode: str, prompt: str,
                    controller_id: str | None = None, controller_epoch: int | None = None) -> dict:
    """从当前已终止尝试交接到新执行器，不复用原生 session_id。"""
    return invoke('switch_executor', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def list_nodes() -> dict:
    """读取节点声明；SSH 配置不代表已连通。"""
    return invoke('list_nodes', {})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def record_activity(request_id: str, task_id: str, event_id: str, activity: dict,
                    controller_id: str | None = None, controller_epoch: int | None = None) -> dict:
    """向任务时间线追加一条活动记录（消息/读文件/命令/测试/变更/系统），不执行命令也不改任务状态。

    需要本机管理认证。若工作项已有 controller 租约，必须提供匹配的 controller_id/epoch。
    相同 request_id 与相同内容幂等返回原收据；相同键不同内容报冲突。
    activity 严格白名单，JSON 不超过 64KiB；operation 内全部保留输出与 metadata 共享 UTF-8 16KiB 预算。路径按工作区相对显示。
    可在终态后补记，但不会复活执行、改 updated 或影响验收。浏览器只读会话不能调用。
    上报方填写的模型名称仅为显示标签，前端标明“总控上报”，不冒充执行器观测。
    """
    return invoke('record_activity', locals())


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def link_task(request_id: str, work_item_id: str, task_id: str, relation: str = 'linked',
              related_task_id: str | None = None, controller_id: str | None = None,
              controller_epoch: int | None = None) -> dict:
    """显式将历史任务补关联到工作项：只改分组元数据，不重发、不改执行结果/验收。

    需要管理认证与（若有）controller 租约。拒绝跨工作项、未知 task、自环，以及伪造内部 review 字段。
    不靠相似提示词/cwd/session 猜测关系。"""
    args = {k: v for k, v in locals().items() if v is not None}
    return invoke('link_task', args)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def reconcile_task(request_id: str, task_id: str, controller_id: str | None = None,
                   controller_epoch: int | None = None) -> dict:
    """核对/回收已 accepted 但本地观测中断的远端任务（如 Grok REMOTE_STATE_UNKNOWN）。

    仅使用已持久化 session/baseline/marker 做有界 GET；禁止 POST 新消息/新会话/interrupt/重复派发。
    远端仍忙则记录“远端仍在运行”；仅有归属正确、baseline 之后、匹配本任务结束标记且稳定 idle 才回收结果。
    不复活用户已取消的执行，不覆盖新 attempt 验收。只读看板不能调用；客户端应沿原 task_id 重试本工具。
    """
    args = {k: v for k, v in locals().items() if v is not None}
    return invoke('reconcile_task', args)
