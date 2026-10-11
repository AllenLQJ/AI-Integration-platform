# Cursor 与 Grok Bot 双向接入

日期：2026-10-11。平台：当前 Apple Silicon Mac。新增 Cursor CLI 和 Grok Bot 两个执行器；两者也能作为总控调用 Hub 的八个工具。

## 本机状态

- Cursor CLI `2026.10.01-e373342` 已安装、登录；Cursor IDE 桌面应用已安装 3.24.12；已登录，IDE MCP 界面确认 agent_hub 的 8 tools enabled；IDE 内实际任务派发未单独验收，见[安装记录](2026-10-11-cursor-ide-installation.md)。
- `@cursor/bdk@0.2.23` 已安装，bdk login 已完成；保留现有 Grok Bot.app 0.68.1。
- Cursor 用户 `~/.cursor/mcp.json` 已注册 agent_hub，官方 list-tools 发现全部八工具。
- Hub 私有配置保留原三执行器并加入 cursor、grokbot；无在途任务时重载。新版共享后台两个新执行器的真实冒烟均通过。
- 云端新建 `Agent Hub Worker` 接收 Hub 任务，`Agent Hub Controller` 用于总控；未修改原有 Bot 的指令或聊天。
- Grok Bot 总控通过已连接 Mac 的本机执行工具调用 RPC。没有开公网入口或更改永久自动执行设置。

## 实测

| 功能 | 证据与结果 |
|---|---|
| Cursor → Hub → Claude | MCP 实际提交任务 a7dad957bffa44d5b0026a33f59eb14b，Hub 真实答案与总控回收标记一致 |
| Grok Bot → Mac → Hub → Claude | 本机 RPC 实际提交任务 42b04e0d7f714689b4964aeb5f418c13，真实 state/answer 与 Controller 回收一致 |
| Hub → Cursor 读取 | 正确返回仅写在本机文件中的新随机标记 |
| Cursor 原生续接 | 保留同一 session_id，未重新读取文件也能回忆上一回合标记 |
| Cursor 编辑 | 独立 worktree 文件实际改写，diff 含标记；原目录及外部探针保持原内容 |
| Cursor 只读探针 | ask 模式写入和 shell 请求未产生修改或命令创建产物；模型未调用写工具，不视为独立工具拒绝审计 |
| Cursor 取消/超时 | 真实任务进入 cancelled / timed_out；记录中的 CLI 根进程均已退出 |
| Hub → Grok Bot | 接收确认后回收 send-message，计算结果、随机标记与本任务结束标记正确 |
| Grok Bot 续接 | 同一 Bot session 正确回忆上一回合随机标记并完成新计算 |
| 新版共享后台 | 两执行器真实回答均核对通过，Grok completion_evidence 含回复序号、最终游标和四次 idle |

完整自动回归 110 项：109 通过、1 项 Windows ACL 跳过（49.121 秒）。接入最初新增 14 项；本次远端取消、固定会话与会话锁追加 13 项。覆盖单次发送、回复关联、旧消息排除、崩溃/不确定状态留存、会话变化拒绝、Cursor 配置与原生存储隔离、RPC 参数边界。自动测试不调用模型账号。

## 约束与未验证项

Cursor 使用 read_only / edit_worktree。只读使用 ask；编辑仅放行 worktree 文件工具；拒绝 Shell、MCP、WebFetch，不使用 force 或自动批准所有 MCP。执行器与总控分开配置；原生聊天目录按实际工作区和模式持久化。项目存在 `.cursor/mcp.json`、`cli.json`、`hooks.json` 或 `plugins` 时拒绝执行并返回 UNSAFE_PROJECT_CONFIG，不自动删项目配置。

请求 `--sandbox enabled`，但 permissions 保留 `os_sandbox=false`：本次证明所述文件探针与 CLI 工具配置，没有完成独立 OS 沙箱全面审计。worktree 不是安全沙箱，不自动合并或提交产物。[官方权限说明](https://cursor.com/docs/cli/reference/permissions)。

Grok Bot 仅 delegate，Hub 不保证只读。本机 cwd 不同步到云端；本机文件操作须经已连接电脑工具和客户端权限。[官方本机执行设置](https://cursor.com/docs/grok-bot/settings)。

适配器依据 [官方 BDK 包](https://www.npmjs.com/package/@cursor/bdk)的 cursor-grokbot-agents 扩展使用 api.cursor.com/v0/grokbot session API，不使用 xAI 模型 API。会话 ID 实测为数字字符串，空历史游标为 -1。发送前拒绝已有在途回合；发送/创建不自动重试，只读网络错误最多三次。需新的 updatedSeq、send-message、本任务完成标记和四次稳定 idle 才视为完成。

发送前创建私有状态文件，默认解析会话后使用 `~/.config/agent-serve/hub-grokbot-session-<session_id哈希>.json`，跨 Hub 数据目录和 Bot 名称别名保护同一会话。旧名称锁存在时拒绝续发，须人工核对，不静默迁移或清除。成功或独立确认远端停止后清除；取消/超时未确认停止、崩溃或不确定回复时保留。出现 REMOTE_STATE_UNKNOWN / DELIVERY_UNKNOWN 时先核对 Bot 会话、本地任务和远端状态，保留文件副本后再人工处理；不能盲目清锁或重发。若定制 state_file，所有调用者必须共享相同文件。

Grok `remote_cancel=true` 表示具备会话中断路径，scope 为 `remote_session`，不等于任意取消请求都成功。首次 cancel_task 返回 cancelling / remote_cancelled=false；get_result 回收终态与独立证据。正常 HTTP 请求最多 10 秒，取消清理额外最多 12 秒；正常退出等待云端清理。自然登录过期、长时运行、新执行器 Windows 验收均未完成。原有严格跨期限休眠恢复等限制不因本次接入改变。

## 使用与复现

Cursor 总控通过 agent_hub MCP 提交 `agent="cursor"` 或 `agent="grokbot"`；模式分别为 read_only/edit_worktree 和 delegate。Grok Bot 的 Agent Hub Controller 可按明确工作范围调用本机入口，使用项目虚拟环境 Python 的绝对路径：

```zsh
"/绝对路径/AI-Integration-platform/.venv/bin/python" -m agent_hub rpc list_agents
"/绝对路径/AI-Integration-platform/.venv/bin/python" -m agent_hub rpc submit_task --arguments '{"request_id":"稳定唯一键","agent":"claude","prompt":"只回复测试标记","cwd":"/实际目录","mode":"text_only","timeout_seconds":60}'
"/绝对路径/AI-Integration-platform/.venv/bin/python" -m agent_hub rpc wait_for_task --arguments '{"task_id":"返回的id","after_seq":0,"timeout_seconds":10}'
"/绝对路径/AI-Integration-platform/.venv/bin/python" -m agent_hub rpc get_result --arguments '{"task_id":"返回的id"}'
```

rpc 仅接受八工具和 JSON 对象，也可 `--arguments -` 从 stdin 读取。HubError 返回 JSON 和非零退出码。等待单次上限为 10 秒，与任务期限分别设置。

```zsh
.venv/bin/python -m unittest discover -s tests -v
# 新目录真实验收会消耗已有账号额度；代理按实际填写。
.venv/bin/python scripts/cursor_grokbot_acceptance.py --root "$HOME/.agent-hub/新的验收目录" --https-proxy http://127.0.0.1:实际端口
```

私有证据目录为 `~/.agent-hub/cursor-grokbot-20261011/`，含 core-v1、cursor-v2/v3、grok-v2/v3、cursor-edit-v1、cursor-lifecycle-v1、总控及 shared-final-report.json。Cursor 一次 socket hang up 失败已保留，新请求复验通过；Grok 会话获取时 urllib 断开，未发送任务，改用 HTTPX 后新请求通过。不删除失败记录，不重放旧结果充作新验收。

账号邮箱、密钥、登录凭据、Hub 令牌及完整日志留在私有目录。本次接入源码与文档尚未提交 GitHub，之前的 Mac 验收提交为 9d4b5c4。

## 远端取消追加验收

2026-10-11 用户要求补齐 Grok Bot 远端取消并并行安装 Cursor IDE；Muse 仍 remote_cancel=false，本次未改变它的行为。

取消、期限到达、正常服务关闭走同一 Grok 清理路径：仅对已确认接收、任务状态文件仍归当前任务且会话 ID 对应的专用 Bot 尝试中断。按发送前基线回读提交原文，发现其他用户消息或 queued 回合则不中断整段会话。interrupt 单次 POST，不自动重试；成功响应记 interrupt_acknowledged=true，随后四次没有新增条目且 idle=true/inFlight=false/queued=0 才记 remote_cancelled=true。若有本任务结束标记并稳定 idle 可确认 already_idle；没有完成标记的启动前 idle 不算结束。

初期在独立私有数据目录为每个场景创建不同临时 Bot；每次 interrupt 目标仍是该任务的原 session，没有操作 Controller 或用户原有 Bot。这些初期记录不作为“跨场景复用同一 Bot”的证据：

| 场景 | 验证结果 |
|---|---|
| 用户取消 | 从远端 busy 回合发送 interrupt，终态 cancelled、remote_cancelled=true，四次 idle 后清锁 |
| 期限到达 | 10 秒任务期限后继续独立清理，终态 timed_out、remote_cancelled=true |
| 正常后台退出 | shutdown 后独立后台完成 interrupt 和停止确认，保存 cancelled 结果并退出 |
| 取消后续任务 | 同一临时 Bot 新任务正常完成；再次取消旧终态任务不误中断新任务 |

失败响应、其他消息、状态锁变化、未确认派发及未确认停止保留文件，remote_cancelled=false。强制杀死后台或断电仍不保证中断成功。接口是整个会话的中断，缺少按 messageId 原子条件取消；专用 Bot 必须避免人工及其他调用者并发投递，核验和 POST 之间的外部消息竞态无法完全消除。确认的是远端回合 idle；已发生的文件/外部操作和独立启动的进程不自动回滚。

复现使用新私有目录（消耗已有账号额度，无工具或外部文件操作）：

```zsh
.venv/bin/python scripts/grokbot_cancel_acceptance.py --root "$HOME/.agent-hub/新的取消验收目录" --bot-name "已核对的专用 Bot 名称" --session-id "对应已有会话ID" --https-proxy http://127.0.0.1:实际端口
```

初次私有证据 `~/.agent-hub/grokbot-cancel-20261011-v1/report.json` 中任务为 340a9c279a1f4634aa7304d3468a51ec（取消）、dfdd0d98187042af9f964960b19e5f26（期限）、828bf4f454e74cdaa81f70d1984fb8b1（退出）；对应会话 12252253、12252326、12252389。三者均有 interrupt_acknowledged=true 和四次 idle 证据。取消后的新任务 d506e6d64d2e44b8a244d214c323929c 内容核对通过。最终复验和自动回归记录在下方补充。

### 同一原 Bot 会话复验

用户指出每次新建 Bot 无法证明跨场景复用后，停止新建，固定使用截图中的 `Agent Hub Cancel Probe b692c4dd59 cancel` / session `12252578`。验收脚本现在必须传 `--bot-name`、`--session-id`，先 GET 核对既有会话，再固定 ID 运行，完全不调用创建路由。三个 case 顺序在同一 Bot 下投递和取消，报告逐项保存实际 submit_session、cancel_target_session 与 same_session=true。`--case` 可仅复现一个场景。

| 场景 | 任务 | 原会话 / 取消目标 | 结论 |
|---|---|---|---|
| 用户取消 | a29584ba708645e8bf5f9990d79cd3d2 | 12252578 / 12252578 | interrupt 请求及确认、四次 idle、remote_cancelled=true |
| 期限 | 806a765c523d45a1b5a7589ca0d7ce48 | 12252578 / 12252578 | timed_out，已回收本任务完成标记并四次 idle，already_idle=true；同 Bot 强制期限中断未覆盖 |
| 正常退出 | 1f7c2f63995f4a4aa4f9390df001ee09 | 12252578 / 12252578 | cancelled，已自然完成且四次 idle，already_idle=true；同 Bot 在途退出强制中断未覆盖 |

同 Bot 取消后新任务正常回复；再次取消旧终态任务没有中断后续任务。没有把 already_idle 描述为 interrupt 成功。初期不同 Bot 的真正期限/退出 interrupt 证据仍保留。第二轮不同 Bot shutdown（12252715）在 12 秒清理期限内虽收到 interrupt 确认却没确认停止，结果 remote_cancelled=false，原任务和状态锁保留；后续回读该原会话为 idle 只是补充，不改写原失败结果。

本次共创建过六个验收 Bot，后续逐个直接 GET 原会话 12252253、12252326、12252389、12252578、12252664、12252715，全部 idle=true/inFlight=false/queued=0，未删除任何 Bot。查询清单见私有 `~/.agent-hub/grokbot-cancel-20261011-fixed/created-bots-current-state.json`。同一原会话三场景汇总为该目录 report.json；退出原始证据另保留 fixed-shutdown/report.json。

共享 `Agent Hub Worker` 固定绑定已核对的原 session 12247914，`agents.grokbot.session_id` 可选配置启用 `fixed_existing`；省略时仍支持按名称 get-or-create。固定路径不查询或创建其他名称的 Bot，名称只是本地配置标签，ID 为权威；账号下既有映射须先核对，不猜测。当前官方 BDK 没有公开 list/GET 会话元数据契约，GET 会话元数据实测 404，不假称按名称列表验证。相同 ID 的不同别名共享同一默认锁，旧名称锁须核对；显式 state_file 仍要求所有调用者共用路径。

最终完整回归 110 项：109 通过、1 项 Windows ACL 跳过（49.121 秒），差异检查通过。最终共享后台 PID 70250，固定 Worker session 12247914 的正常任务 3ad83aaea92149c98b5d3dca1a9f2849 内容核对通过，same_session=true，在途及排队任务数 0；没有调用创建路由。证据为私有目录 `grokbot-cancel-20261011-fixed/shared-release-report.json`、`full-regression.log`。所有独立测试后台均已退出，共享 Hub 保持运行。源码、需求和文档仍在本地，未提交或推送。


### 后续提交与在途退出补测

接入快照已于本日提交并推送 `5b52abb`，上文未提交状态为当时验收阶段记录。后续始终复用原 Bot / session 12252578，没有新建 Bot：正常后台退出场景已取得 interrupt_acknowledged=true、remote_cancelled=true 与四次 idle，补齐同 Bot 在途退出强制中断；10 秒期限再次自然完成，stop_reason=already_idle，强制期限中断仍未覆盖。证据为私有 `~/.agent-hub/grokbot-cancel-20261011-final-lifecycle/report.json`。Muse 取消及 SSH Hub 调度后续实现见 [新增记录](2026-10-11-muse-cancel-remote-hub.md)。


新增功能之后的完整回归为 129 项：128 通过、1 项 Windows ACL 跳过（49.332 秒），见上方新增记录；前述 110 项为接入阶段记录。
