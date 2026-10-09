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

```powershell
.\.venv\Scripts\python.exe -m agent_hub status
.\.venv\Scripts\python.exe -m agent_hub mcp
# 显式停止后台（会取消本地运行任务，Muse 远端可能继续）
.\.venv\Scripts\python.exe -m agent_hub stop
```

MCP 使用 stdio：command 为虚拟环境 python.exe 的绝对路径，args 为 `["-m", "agent_hub", "mcp"]`。可以通过 `AGENT_HUB_DATA_DIR` 指定另一份私有数据目录。默认目录按当前 OS 用户保护；不监听外部网卡。Windows 自动启动通过 WMI 避免 MCP 前端关闭时杀掉后台；若 WMI 被系统策略禁用，可以在终端先运行 daemon 命令。

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
