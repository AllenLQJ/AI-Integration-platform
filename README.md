# 本地 Agent Hub

当前进度（2026-10-11）：Windows 第一版与 Mac 本机接入已实现，提供 11 个 MCP 工具。新增 Cursor CLI / Grok Bot 双向接入，真实总控派发、执行器回复和续接通过，见 [接入记录](docs/2026-10-11-cursor-grokbot-validation.md)。Mac 最新完整自动回归 129 项：128 通过、1 项 Windows ACL 检查跳过（49.332 秒）。真实核心任务与补充异常验收已完成本轮记录；严格休眠超时恢复等仍未验证，不能宣称全部功能都已验收。当前状态与验证边界见 [开发进度](docs/开发进度.md) 和 [第一版验收](docs/第一版验收.md)。Windows 记录来自 10-09/10-10，最新修改未在 Windows 重新回归。

Muse 原会话远端取消、SSH 跨机器 Hub 调度已实现。Muse 取消/期限/正常退出在同一专用会话实测通过；SSH 调度完成两个隔离 Hub 的逻辑验证，真实双机 SSH 尚未验收。配置与使用见 [新增功能记录](docs/2026-10-11-muse-cancel-remote-hub.md)。

用一个 MCP 接口提交 Codex、Claude Code、Muse、Cursor CLI 和 Grok Bot 任务。任务由独立后台服务执行，SQLite 保存记录，代码修改放在独立 Git worktree，返回结果与差异供审阅。

## Mac 安装

在源码目录使用 Python 3.10 或更新版本创建环境；本机已验证 Python 3.12.14 和 uv：

```zsh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
# 当前测试显式导入 exceptiongroup，运行回归前安装
uv pip install --python .venv/bin/python exceptiongroup
.venv/bin/python -m agent_hub status
.venv/bin/python -m agent_hub mcp
```

在 `~/.agent-hub/config.json` 填写自己的 Codex、Claude 和 Muse 连接器可执行文件绝对路径。Muse 使用独立 Python 环境运行 `-m muse_mcp`，先按 [连接器补丁说明](vendor/muse-mcp/README.md) 安装，再验证认证。MCP 注册的 command 使用本机 `.venv/bin/python` 绝对路径，args 为 `["-m", "agent_hub", "mcp"]`。CLI 登录、Cookie 和模型密钥保留在各执行器的私有用户配置中。详细本机配置历史见 [Mac 配置记录](docs/2026-10-11-macos-setup-validation.md)。

## Windows 安装

使用 Python 3.10 或更新版本，在源码目录执行：

```powershell
& 'D:\My_Software\Anaconda3\envs\py310\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

在 `%USERPROFILE%\.agent-hub\config.json` 配置实际路径，参见 `config.example.json`。代理按实际环境填写，7897 是网络代理，Claude 的 CCR 3456 保留在 Claude 自己的配置中。无需把 Cookie 或模型密钥写进 Hub 配置。

并发在 config.json 的 `concurrency` 中设置：`{"global":2,"per_agent":{"codex":1,"claude":1,"muse":1}}`。各额度允许 1–32 的整数；未配置的执行器默认 1。修改后在没有在途任务时停止后台，下次调用自动加载新配置。同一实际工作区始终串行。

```powershell
.\.venv\Scripts\python.exe -m agent_hub status
.\.venv\Scripts\python.exe -m agent_hub mcp
# 显式停止后台（会取消本地运行任务，Muse 远端可能继续）
.\.venv\Scripts\python.exe -m agent_hub stop
```

MCP 使用 stdio：command 为虚拟环境 python.exe 的绝对路径，args 为 `["-m", "agent_hub", "mcp"]`。可以通过 `AGENT_HUB_DATA_DIR` 指定另一份私有数据目录。默认目录按当前 OS 用户保护；不监听外部网卡。Windows 自动启动通过 WMI 避免 MCP 前端关闭时杀掉后台；若 WMI 被系统策略禁用，可以在终端先运行 daemon 命令。

## 总控接入

Mac 三方入口的最新路由与验证见 [Claude/Codex/Cursor 双向接入](docs/2026-10-11-local-three-agents.md)：本机 `cursor` 默认原生 IDE 界面桥，`cursor_cli` 显式选择 CLI。Claude、Codex 的真实总控与被调用双向复验通过。开通 Pro 后，Cursor IDE 总控调度 Claude、Hub 委托 IDE 纯文本任务和同会话续接均实测通过，Cursor CLI 读取随机文件复验通过。IDE 桥需要具备电脑控制能力的外部 worker 在线，尚无常驻无人值守驱动；IDE 编辑、取消及重开后会话定位尚未验收。

在 Codex 的 MCP 设置中登记 `agent_hub`，command 使用本机项目虚拟环境 Python 的绝对路径，args 为 `["-m", "agent_hub", "mcp"]`。重新加载 MCP 或打开新聊天后应看到 11 个工具。移动源码或环境后需同步修改 MCP 注册路径；Windows 测试环境的 D 盘迁移见 `docs/2026-10-10-d-drive-migration.md`，Mac 使用自己的项目路径。

Claude Code 可以作为总控连接同一服务，例如保存下述本机 MCP 配置（换成自己的 Python 绝对路径）：

```json
{"mcpServers":{"agent_hub":{"command":"D:/path/to/AI-Integration-platform/.venv/Scripts/python.exe","args":["-m","agent_hub","mcp"],"env":{"PYTHONUTF8":"1"}}}}
```

使用 `claude --strict-mcp-config --mcp-config <配置文件绝对路径>` 启动。总控必须加载 Hub MCP；不要套用执行器的空 MCP 配置或 `--safe-mode`。本机 Claude / DeepSeek API 模式在测试时使用 `ENABLE_TOOL_SEARCH=false` 禁用动态工具发现，再明确允许相应 Hub 工具，才产生真实调用。此环境变量可仅对启动终端设置：`$env:ENABLE_TOOL_SEARCH='false'`。遵循客户端正常的工具授权流程，无需改变全局权限。

示例任务：让总控“通过 agent_hub 把本次调研交给 Muse，使用 delegate，工作目录指定为项目绝对路径；记录任务 ID，查询到 ready=true 后汇总 answer 与 source_urls”。模型提示词之外，检查实际 MCP 调用和任务结果才算成功。

## 提交任务

让总控依次调用：

1. `list_agents` 查看配置、版本和模式。
2. `submit_task(request_id, agent, prompt, cwd, mode)`，记录返回的 `id`。
3. `wait_for_task(task_id, after_seq=0, timeout_seconds=10)` 等待结果；也可用 `get_task` / `get_events` 即时查询。
4. `get_result(task_id)` 读取 `ready=true` 的结果。
5. `continue_task(request_id, task_id, prompt)` 复用原生会话和工作区；返回子任务 ID。

request_id 应为稳定且唯一的请求键。相同键和内容只执行一次，改变内容返回 REQUEST_CONFLICT。任务超时为 10–7200 秒，默认 1800；总控退出不改变任务期限。

### 持续等待任务

`wait_for_task` 是第八个只读工具，单次最多等待 10 秒，默认 10 秒，0 秒立即返回快照。检测到 succeeded、failed、timed_out、cancelled、interrupted、delivery_unknown 任一终态就提前返回。MCP 与带令牌的回环 HTTP `/rpc` 使用相同契约。

返回 `state`、`events`、`next_seq`、`has_more`、`timed_out`。`timed_out=true` 只表示本次等待到期，不会取消或重发任务；任务自身期限到期则是 `state="timed_out"`，这时等待返回 `timed_out=false`。

总控应保存最后实际收到的 next_seq：若未终态，立刻用该游标再次等待；若终态但 has_more=true，继续读取至 has_more=false，再调用 get_result 获取完整答案。每页最多 200 条事件，游标只前进到已返回的最后一条。断线后使用最后收到的游标续接；响应丢失可能重复读取，按事件 seq 去重，不重新提交任务。

等待由 Hub 执行，但持续续接由总控负责；不会自动唤醒已结束聊天或发桌面通知。服务关闭时等待返回 SERVICE_STOPPING，重连后继续原任务游标。

更新源码后，已运行后台不会自动加载新工具。在无在途任务且明确允许停止的时机运行 `python -m agent_hub stop`，下次调用启动新版后台；重新加载 MCP 或开启新聊天刷新八个工具。验收脚本使用隔离服务；2026-10-10 已按用户授权重启共享后台并验证八个工具，见等待验收记录。后续更新仍需手动重载。

编码任务使用 `edit_worktree`，以 `HEAD` 已提交版本为基线，要求已跟踪文件没有未提交修改且有基线提交。主目录可以存在未跟踪文件：这些文件保留在原目录，不复制到任务工作区，任务状态和结果的 `excluded_untracked_files` 列出未纳入的路径，`baseline` 记录提交 SHA。需要使用未提交内容时，先明确并准备相应基线。Hub 不自动提交或合并、不删除任务 worktree。返回 diff、变更文件和日志路径；模型说完成不等于代码测试通过。worktree 不是安全沙箱。

## 执行器限制

- Cursor CLI：read_only 使用 ask；edit_worktree 仅放行工作区文件工具，原生 resume。执行配置独立于总控 MCP，拒绝项目级 MCP/hooks/权限配置；不使用 force，不开放命令。已完成本机文件、续接、取消及超时探针，未独立审计完整 OS 沙箱。
- Grok Bot：delegate，通过 Cursor BDK 公布的 session API 接收并回收任务；使用专用 Bot，cwd 不自动同步。续接、远端取消、期限中断和正常后台退出已实测；只有 interrupt 后独立确认回合停止，或本任务已自然完成并确认 idle，才报告 `remote_cancelled=true`，已发生操作不回滚。不确定派发或停止保留跨数据目录的私有锁，先人工核对再续发。两者总控及使用说明见 [双向接入](docs/2026-10-11-cursor-grokbot-validation.md)。

- Codex：read_only 使用自身 read-only 沙箱；edit_worktree 使用 workspace-write；不加载用户 MCP 配置。Windows 默认使用已在本机探针通过的 MXC（Codex 0.162.0）；其他设备可明确配置 windows_sandbox，不能通过关闭沙箱解决权限问题。
- Claude：已验证 Windows CCR/apiKeyHelper 和 Mac DeepSeek Anthropic 兼容 API 配置；Claude 订阅认证尚未验收。text_only 禁用所有工具，适合提供上下文后的文本分析；read_only 仅开放 Read/Glob/Grep；edit_worktree 仅 Read/Glob/Grep/Edit/Write，第一版不允许运行命令。safe-mode 禁用自定义技能/插件，严格空 MCP 配置防止递归。
- Muse：使用 delegate 委托模式，Hub 不强制只读，可能调用云端及已连接 Mac 的写入工具；操作由 Muse/客户端权限与审批决定。文本调研，每个初始任务独立会话。结果采用会话、消息序号与最终回答标记关联；不宣称远端工作流已完成。取消/超时仅停止本地等待，远端可能继续。发送状态不确定时不自动重发。
  正文链接与后续 external_link 附件均汇入 source_urls；两次稳定轮询后返回。该列表保留 Muse 返回的来源，不等于 Hub 已核实网页内容。
  MCP 内层 `ok=false` 作为失败处理并保留上游错误码。仅历史读取遇到临时网关、超时或 VM 不可用时最多尝试三次，间隔为 1、2 秒；受任务期限和取消约束，并记录 retry 事件。认证失败和异常响应格式不重试。会话创建和任务发送不自动重试，避免重复会话或重复执行。
  completion_evidence 保留最终回复的 reply_to_message_id（可能为空）；非空字段不会排除已完成且带本任务标记的回复。读取失败耗尽后保留远端状态不确定，不将其视为远端已停止。
  Muse 发送使用 fire_and_forget，只取得派发确认；回复由 Hub 的历史轮询统一回收，不在发送工具内重复等待回复。
  Hub 专用 Muse 子进程启用 `MUSE_MCP_REUSE_GATEWAY=1`，配套 muse-mcp 支持在锁保护下复用连接，减少重复 HTTPS 认证；连接异常会丢弃缓存，不重放业务操作。普通 Muse 工具默认仍使用独立连接。配套兼容性补丁和基线安装步骤见 [vendor/muse-mcp/README.md](vendor/muse-mcp/README.md)，部署时需应用补丁并配置自己的认证与 Python 路径。
- 后台重启：running/cancelling 标 interrupted，不按旧 PID 杀进程，也不自动重跑。无法确认旧执行已停止的工作区隔离，拒绝直接续接；同工作区此前已排队的任务也转 interrupted，其他工作区 queued 继续。需人工核对，在新工作区提交。正常终态使用已保存的 session_id 续接。Mac 上强制杀死后台不保证旧执行器停止。
- Mac 已完成本机核心闭环及补充实机验收，详见 [Mac 验收记录](docs/2026-10-11-macos-full-acceptance.md) 与 [补充异常验收](docs/2026-10-11-macos-resilience-validation.md)。严格的睡眠期间到期后恢复处理仍未验证。DeepSeek Harness、Claude 订阅模式未验收；跨机器 Hub 调度和 Muse 远端取消不属于当前实现能力。Mac 不使用 Windows lock 文件：创建环境后 `pip install -e .`，按本机路径配置。

## 诊断与验证

`status` 只检查可执行路径、版本和已配置能力；账号有效性由实际任务确认。任务状态为 failed 时读取 get_result 和私有日志。出现 delivery_unknown 时先检查 Muse 原会话，不重复提交。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 明确授权后的短真实账号验收，会消耗账号额度
.\.venv\Scripts\python.exe scripts/acceptance.py
```

开发设计、实施计划、开发进度和验收记录位于 docs。认证和运行数据不进入 Git；完整日志仅在用户私有数据目录保存。

Mac 最新记录分为 [核心验收](docs/2026-10-11-macos-full-acceptance.md) 与 [异常及稳定性验收](docs/2026-10-11-macos-resilience-validation.md)。仍未验证严格的睡眠期间到期后恢复处理、Muse 底层浏览器动作独立审计、数小时持续稳定性、真实登录自然过期后的恢复。跨机器 Hub 调度和 Muse 远端取消尚未实现；DeepSeek Harness、Claude 订阅模式按本轮范围排除。

`scripts/macos_*.py` 是本轮隔离验收及证据复核脚本，使用固定私有目录和请求键，不能把重放旧结果当作新验收。涉及真实账号的脚本会使用账号额度；休眠脚本会让整台 Mac 休眠，须每次明确授权并准备手动唤醒。它们不由普通自动回归执行。

## 任务模式与权限（2026-10-11）

| 执行器 | 模式 | 实际限制 |
|---|---|---|
| Claude | text_only | 无工具，仅文字分析 |
| Claude | read_only | 仅 Read/Glob/Grep，无写入、命令或 MCP 工具 |
| Claude | edit_worktree | 独立 worktree，Read/Glob/Grep/Edit/Write，无命令；不是 OS 沙箱 |
| Codex | read_only | Codex 只读沙箱 |
| Codex | edit_worktree | Codex workspace-write 沙箱与独立 worktree |
| Muse | delegate | Hub 不强制只读；实际操作由 Muse 和客户端权限决定 |
| Cursor CLI | read_only / edit_worktree | ask 或工作区文件白名单，无 Shell/MCP/WebFetch；未全面审计 OS 沙箱 |
| Grok Bot | delegate | 云端专用 Bot，Hub 不强制只读，取消仅停止本地等待 |

list_agents 的 permissions 按模式说明 enforced_by、enforced、allows_write、allows_commands、os_sandbox 及作用范围；submit_task、get_task、get_result 也返回 permissions。enforced 表示声明的模式限制由执行器强制，而非所有执行器拥有同等隔离保障。Muse 的 allows_write/commands 表示可能发生，不能据此认为已获客户端授权。

旧任务保留原始记录和 request_id 幂等性。历史 Claude read_only 续接按 text_only 执行；历史 Muse read_only 禁止续接，需按明确操作范围新建 delegate 任务。仅 get_result 成功不代表实际操作正确，仍需核对文件或外部结果。更新后需无在途任务时重载后台，并刷新客户端 MCP 工具说明。

## HubGateway v1（服务版本 0.2.1）

新增 `get_capabilities()`、`get_artifact(artifact_id)`，MCP 当前共十一个工具（包括同期新增的 resume_task）。
`submit_task` 新增可选 `context`，省略或 null 保持旧请求哈希兼容；对象内容全部参与幂等哈希。
任务状态保留原始 `state`；结果另提供 `gateway_state`、`execution_uncertain`、`errors` 和 `media_artifacts`。
实现边界、配置和测试见 [HubGateway 交接说明](docs/HubGateway-v1-实现交接.md)。

## Muse 等待人工处理

Muse 遇到人机验证、登录或审批时，可报告 `waiting_for_user`（非终态）。`get_task`、`get_result`、`wait_for_task` 返回 `intervention`，含 `intervention_id`、原因、消息、Muse `session_id` 和人工等待期限；结果保持 `ready=false`。`wait_for_task` 提前返回 `needs_user=true`、`timed_out=false`，总控应停止连续轮询并提示用户在 Muse 原会话接管处理。

仅当用户明确确认处理完成后，调用 `resume_task(request_id, task_id, intervention_id)`。此工具恢复原任务和原会话，不创建子任务；同一介入即使收到不同 request_id 也只投递一次继续指令。之后查询原 task_id；不要重新 submit，也不要对等待中的任务调用 continue_task。`acknowledged=true` 仅表示本地确认已登记，最终回答仍需 get_result 核实。

每个任务人工等待总预算为 15 分钟，人工等待期间暂停普通执行期限；恢复后使用剩余执行时间。人工等待到期、用户取消或服务停止沿用 Muse 原会话取消与独立停止核验；停止不确定时保留锁，不自动续发。浏览器中人工处理后，如果 Muse 自行给出最终回答，也会回收原任务结果。

当前检测优先使用派发提示中约定的 `[HUB_WAIT:<task_id>:human_verification|login|approval]` 消息标记，保守识别最新助手消息中的明确阻碍和接管请求作为辅助，并标明 `evidence`。尚未验证 Muse 原生结构化人工等待字段；这不是通用浏览器状态探测，模型不报告时可能仍需人工查看。Hub 不操作人机验证。

本机 MCP 增至 11 工具；后台需在无在途任务时重载，Cursor/Codex 客户端需重新连接 MCP 才能发现 resume_task 和新版等待说明。已开始的旧 worker 不会热升级。SSH 执行器的人工等待透传不在本次实现范围。

HubGateway 媒体接入已完成首轮真实文本审核与 Muse 默认图片路由验收：`text.review/deepseek-flash[1m]` 和 `image.generate/muse:auto` 已登记证据。`muse:auto` 表示默认路由，底层模型未知；仅支持已验收的一张 PNG 无参考图路径。
