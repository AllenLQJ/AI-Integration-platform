# wait_for_task 验收记录

> 2026-10-10 发布准备复验：当前源码完整自动测试再次通过 62/62（56.863 秒），pip check 与差异格式检查通过。下文 67.212 秒为原功能验收结果，真实账号记录本轮未重跑。文档同步与主线集成已完成，上传需用户最终确认。

日期：2026-10-10，Asia/Shanghai。
源码：`C:\Users\95439\.codex\worktrees\agent-hub-v1\AI-Integration-platform`，分支 `feature/agent-hub-v1`，HEAD `3726926` 加当前未提交修改。原有修改保留；未合并主分支、未自动重启共享后台。

## 实际行为

- 新增第八个只读工具 wait_for_task，默认及单次上限 10 秒，允许 0 秒立即快照。原七个工具返回结构保留。
- 六种终态均提前返回；响应中的 timed_out 表示本次等待到期，任务状态 timed_out 表示任务期限到期，二者独立。
- Service 全局锁外停放，每 100ms 检查状态，monotonic 计时；没有等待期间持有的数据库事务。
- 状态及最多 200 条事件来自同一读事务，has_more 指示后续页；next_seq 只推进到已返回事件。终态及终态事件原子提交，插入失败整个事务回滚。
- 断线使用最后实际收到的游标重读，可能重复事件，调用方按 seq 去重。工具不重发任务、不改变期限或请求幂等。
- MCP 使用异步入口与工作线程，保留 client 的 15 秒 HTTP 超时。关闭服务释放等待并返回 SERVICE_STOPPING。

## 测试与证据

先写测试并观察失败：等待工具不存在；旧 finish 在终态事件插入失败时未回滚任务状态。实现后 7 项核心测试通过。
真实 stdio 用例先因缺少新工具失败，实现后通过；证明同一 MCP 连接等待期间仍能查询。
边界补测复现超大整数导致 OverflowError、MCP 把布尔转换为数字；调整范围检查顺序和严格数值类型后 10 项等待测试通过。

最终完整自动回归：**62/62，67.212 秒，OK**。

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

依赖检查返回 `No broken requirements found`；差异格式检查通过。Git 的 LF→CRLF 提示不属于差异格式失败。新增代码扫描未发现实际密钥串；运行令牌不写入源码。

覆盖六种终态、queued 零等待快照、running 等待超时及后续成功、期限与幂等不变、407 条事件完整分页及旧游标重读、HTTP 等待期间并发提交/查询/取消、同一 stdio MCP 连接并发查询、服务关闭唤醒、终态事件插入失败回滚、非法与超大参数。

独立只读审阅：未发现 Critical/Important/Minor；独立运行等待测试 **10/10，10.549 秒**，diff 检查通过。

## 真实总控

最终代码执行 `scripts/wait_controller_acceptance.py`：真实 Codex、真实 Claude Code（本机 CCR/API 模式）分别通过专用 MCP 配置连接隔离 HTTP 服务。两者均实际执行：

`submit_task → wait_for_task(timed_out=true) → wait_for_task(succeeded) → get_result`

每个总控只提交一次，退出码均为 0，返回 `WAIT_HUB_PASS`。执行器是确定性 fixture，用于隔离等待契约；不把本次结果当作真实下游模型或 Muse 执行验收。

最终私有证据：

- `C:\Users\95439\.agent-hub\wait-controller-acceptance-20261010-153437\codex\evidence.json`
- `C:\Users\95439\.agent-hub\wait-controller-acceptance-20261010-153437\claude\evidence.json`

上次修改严格类型前的验收证据另存于 `wait-controller-acceptance-20261010-073035`，目录时间是执行主机 UTC；最终脚本已使用 Asia/Shanghai 时间命名。上述最终证据对应本次修复后的代码，不依赖历史成功记录。

## 重启与共享 Hub 冒烟测试

用户于 2026-10-10 要求重启并测试。停止前读取旧后台 PID 9352，并检查该 PID 无执行器子进程；`python -m agent_hub stop` 返回 `{"ok":true}`。`Client.ensure()` 随后启动新后台，健康检查 PID 为 **40484**。直接 RPC 使用 `wait_for_task` 查询不存在的任务得到 `TASK_NOT_FOUND`，确认服务端加载了新路由。

新 MCP stdio 进程连接同一共享数据目录，握手后列出 **8 个工具**，其中包含 `wait_for_task`；缺失任务探针返回 `TASK_NOT_FOUND`。该探针在开发 worktree 的 git-ignored `.superpowers/sdd/2026-10-10-wait-for-task/live-mcp-check.py`。

然后通过当前 Hub MCP 提交了真实 Codex `read_only` 短任务 `09f297bf30b74dd5982d5cff425d3b29`（`wait-live-restart-20261010-codex-smoke-1`）。重启后源码等待客户端连续调用新工具，收到 5 条事件、状态 `succeeded`、`timed_out=false`；`get_result` 返回 `ready=true`、`exit_code=0`、回答 `HUB_WAIT_SMOKE_20261010`。客户端探针脚本保存在同一 git-ignored `.superpowers` 目录。该项证明真实下游 Codex 执行器、共享后台和新工具已实际贯通。

Codex 桌面当前会话的固定工具清单可能仍显示重启前的七个工具；新 stdio/MCP 会话已确认八个工具。重新加载 MCP 或打开新聊天即可刷新该清单。

## 使用条件与范围

代码变更后新版后台不会自行加载。本次已在检查无执行器子进程后按用户指示重启共享 Hub。全局 MCP 配置未改变；桌面会话的工具清单仍需重载。

总控仍需在单次等待超时后继续调用；工具不会自动唤醒结束的聊天，也没有桌面推送。DeepSeek Harness、Mac 实机未验收。原有工作区处理修改不属于本次实现范围。
