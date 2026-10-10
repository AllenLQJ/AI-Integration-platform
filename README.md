# 本地 Agent Hub

用一个 MCP 接口提交 Codex、Claude Code 和 Muse 任务。任务由独立后台服务执行，SQLite 保存记录，代码修改放在独立 Git worktree，返回结果与差异供审阅。

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

本机 Codex 已登记 `agent_hub`；重新加载 MCP 或打开新聊天后查看工具。源码当前位于 `C:\Users\your-name\.codex\worktrees\agent-hub-v1\AI-Integration-platform`，注册命令使用该目录的 `.venv\Scripts\python.exe`，请保留此目录。其他机器在 MCP 设置中填写自己的绝对路径。

Claude Code 可以作为总控连接同一服务，例如保存下述本机 MCP 配置（换成自己的 Python 绝对路径）：

```json
{"mcpServers":{"agent_hub":{"command":"C:/Users/your-name/.codex/worktrees/agent-hub-v1/AI-Integration-platform/.venv/Scripts/python.exe","args":["-m","agent_hub","mcp"],"env":{"PYTHONUTF8":"1"}}}}
```

使用 `claude --strict-mcp-config --mcp-config <配置文件绝对路径>` 启动。总控必须加载 Hub MCP；不要套用执行器的空 MCP 配置或 `--safe-mode`。本机 Claude / DeepSeek API 模式在测试时使用 `ENABLE_TOOL_SEARCH=false` 禁用动态工具发现，再明确允许相应 Hub 工具，才产生真实调用。此环境变量可仅对启动终端设置：`$env:ENABLE_TOOL_SEARCH='false'`。遵循客户端正常的工具授权流程，无需改变全局权限。

示例任务：让总控“通过 agent_hub 把本次调研交给 Muse，使用 read_only，工作目录指定为项目绝对路径；记录任务 ID，查询到 ready=true 后汇总 answer 与 source_urls”。模型提示词之外，检查实际 MCP 调用和任务结果才算成功。

## 提交任务

让总控依次调用：

1. `list_agents` 查看配置、版本和模式。
2. `submit_task(request_id, agent, prompt, cwd, mode)`，记录返回的 `id`。
3. `get_task(task_id)` / `get_events(task_id, after_seq)` 查看状态。
4. `get_result(task_id)` 读取 `ready=true` 的结果。
5. `continue_task(request_id, task_id, prompt)` 复用原生会话和工作区；返回子任务 ID。

request_id 应为稳定且唯一的请求键。相同键和内容只执行一次，改变内容返回 REQUEST_CONFLICT。任务超时为 10–7200 秒，默认 1800；总控退出不改变任务期限。

编码任务使用 `edit_worktree`，要求 Git 主目录干净且有基线提交。Hub 不复制未提交内容、不自动提交或合并、不删除任务 worktree。返回 diff、变更文件和日志路径；模型说完成不等于代码测试通过。worktree 不是安全沙箱。

## 执行器限制

- Codex：read_only 使用自身 read-only 沙箱；edit_worktree 使用 workspace-write；不加载用户 MCP 配置。Windows 默认使用已在本机探针通过的 MXC（Codex 0.162.0）；其他设备可明确配置 windows_sandbox，不能通过关闭沙箱解决权限问题。
- Claude：当前本机通过 CCR/apiKeyHelper 调用 DeepSeek API。read_only 禁用所有工具，适合提供上下文后的文本分析；edit_worktree 仅 Read/Glob/Grep/Edit/Write，第一版不允许运行命令。safe-mode 禁用自定义技能/插件，严格空 MCP 配置防止递归。
- Muse：文本调研，每个初始任务独立会话。结果采用会话、消息序号与最终回答标记关联；不宣称远端工作流已完成。取消/超时仅停止本地等待，远端可能继续。发送状态不确定时不自动重发。
  正文链接与后续 external_link 附件均汇入 source_urls；两次稳定轮询后返回。该列表保留 Muse 返回的来源，不等于 Hub 已核实网页内容。
  MCP 内层 `ok=false` 作为失败处理并保留上游错误码。仅历史读取遇到临时网关、超时或 VM 不可用时最多尝试三次，间隔为 1、2 秒；受任务期限和取消约束，并记录 retry 事件。认证失败和异常响应格式不重试。会话创建和任务发送不自动重试，避免重复会话或重复执行。
  completion_evidence 保留最终回复的 reply_to_message_id（可能为空）；非空字段不会排除已完成且带本任务标记的回复。读取失败耗尽后保留远端状态不确定，不将其视为远端已停止。
  Muse 发送使用 fire_and_forget，只取得派发确认；回复由 Hub 的历史轮询统一回收，不在发送工具内重复等待回复。
  Hub 专用 Muse 子进程启用 `MUSE_MCP_REUSE_GATEWAY=1`，配套 muse-mcp 支持在锁保护下复用连接，减少重复 HTTPS 认证；连接异常会丢弃缓存，不重放业务操作。普通 Muse 工具默认仍使用独立连接。当前本机配套实现位于 `D:\projects\AI-Integration-platform\muse-mcp`，部署到其他设备时需要同步该实现。
- 后台重启：queued 继续；running/cancelling 标 interrupted，不按旧 PID 杀进程，也不自动重跑。无法确认旧执行已停止的工作区隔离，拒绝直接续接；需人工核对，在新工作区提交。正常终态使用已保存的 session_id 续接。
- Mac 与 DeepSeek harness 尚未实机验收。Mac 不使用 Windows lock 文件：创建环境后 `pip install -e .`，按本机路径配置；实际支持需要另行验收。

## 诊断与验证

`status` 只检查可执行路径、版本和已配置能力；账号有效性由实际任务确认。任务状态为 failed 时读取 get_result 和私有日志。出现 delivery_unknown 时先检查 Muse 原会话，不重复提交。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 明确授权后的短真实账号验收，会消耗账号额度
.\.venv\Scripts\python.exe scripts/acceptance.py
```

开发设计、实施计划、开发进度和验收记录位于 docs。认证和运行数据不进入 Git；完整日志仅在用户私有数据目录保存。
