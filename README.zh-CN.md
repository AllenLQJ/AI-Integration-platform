# AI Integration Platform

可选的[跨目录诊断工具](docs/portable-helpers.md)支持显式指定 Muse Python 环境并继承代理配置；Windows 认证脚本提供仅检查本机命令的 `-CheckOnly` 模式。

[English](README.md) | **简体中文**

本地 Agent Hub：通过统一 MCP 接口协调 Codex、Claude Code、Muse、Cursor CLI 和 Grok Bot。独立后台执行任务，SQLite 保存状态、事件和结果。代码编辑任务可使用隔离的 Git worktree，并返回差异供审阅。edit_worktree 收集的 diff 与未跟踪文本摘录合计默认最多 1 MiB（UTF-8，`Workspaces.MAX_ARTIFACT_BYTES`）；超限时设置 `artifact_truncated` 与 `artifact_omitted`，不会把不完整 diff 当作完整变更。省略路径列表（如 artifact_omitted、changed_files）属于预算外元数据，整个结果 JSON 不设上限。

**Python 3.10+ · Windows / macOS · 服务版本 0.3.0 · 20 个 MCP 工具**

**通用接入：**[执行器插件与命令桥接](docs/executor-plugins.md) 支持不修改 Hub 核心接入新 AI 工具。插件安装在 Hub 环境中；账号、机器配置、任务和日志保存在各设备独立的数据目录。提供可独立安装的示例包及 Windows/macOS/Linux 契约测试 CI；配置了 CI 不等于已完成跨平台实测。

## 核心能力

- 持久化任务、幂等提交、会话续接、事件游标与有界等待。
- 按执行器提供任务模式和权限声明，编辑任务使用隔离 worktree。
- 独立验收、产物证据、本机工作台、总控接管与显式开启的可配置执行器内容审核。
- 通过标准 SSH 调用远端 Hub；可使用已有 Tailscale 网络连接，不开放公网 Hub 服务。
- [本机只读看板](docs/dashboard.md)：搜索任务链、阅读统一协作/操作时间线（消息与已观测/已上报的读文件、命令、测试等操作）、执行事件，分别查看执行与验收状态，并展示综合 `work_status` 标签（界面 `workStatusLabel`）。运行 `agent-hub dashboard` 打开，安装包自带静态资源，无需 Node.js 运行时。总控可通过已认证的 `record_activity` 补记活动，走共享 `normalize_activity`（脱敏后输出摘录合计 <=16 KiB UTF-8；不会自动截获未公开的客户端内部工具）。

实现、历史实机验收与尚未验证的范围分别说明。`ready=true` 表示执行结束；有验收条件时，应进一步检查 `verification` 和 `work_status`。

## 导航

- [Mac 安装](#mac-安装) · [Windows 安装](#windows-安装)
- [SSH 与 Tailscale](#额外说明) · [总控接入](#总控接入)
- [提交任务](#提交任务) · [执行器限制](#执行器限制)
- [诊断与验证](#诊断与验证) · [任务模式与权限](#任务模式与权限)
- [HubGateway](#hubgateway-v1服务版本-030) · [Muse 人工处理](#muse-等待人工处理)
- [任务验收](#独立任务验收第一阶段) · [工作台与总控](#第二阶段工作台与总控)
- [节点与产物传输](#第三阶段节点与产物传输) · [历史验证记录](#历史验证记录)

## Mac 安装

在源码目录使用 Python 3.10 或更新版本创建环境；本机已验证 Python 3.12.14 和 uv：

```zsh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m agent_hub status
.venv/bin/python -m agent_hub mcp
```

在 `~/.agent-hub/config.json` 填写自己的 Codex、Claude 和 Muse 连接器可执行文件绝对路径。CLI 类执行器（codex / claude / cursor_cli）的 `executable` 也可写成会随升级变化的形式：支持环境变量与 `~`（如 `%LOCALAPPDATA%`）、通配符（匹配多个时取修改时间最新的文件，如 Windows Codex 桌面版 `%LOCALAPPDATA%/OpenAI/Codex/bin/*/codex.exe`，可避免更新后哈希目录变化找不到），以及仅写命令名（按 PATH 查找）；每次检测和启动任务时重新解析，原绝对路径写法不变；`status` 会同时显示 `resolved_executable`。Muse、Remote 与 Cursor IDE 不走该解析逻辑，仍需填写绝对路径（或其自身约定的写法）。Muse 使用独立 Python 环境运行 `-m muse_mcp`，先按 [连接器补丁说明](vendor/muse-mcp/README.md) 安装，再验证认证。MCP 注册的 command 使用本机 `.venv/bin/python` 绝对路径，args 为 `["-m", "agent_hub", "mcp"]`。CLI 登录、Cookie 和模型密钥保留在各执行器的私有用户配置中。详细本机配置历史见 [Mac 配置记录](docs/2026-10-11-macos-setup-validation.md)。

## Windows 安装

使用 Python 3.10 或更新版本，在源码目录执行：

```powershell
& 'C:\path\to\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

在 `%USERPROFILE%\.agent-hub\config.json` 配置实际路径，参见 `config.example.json`。代理按实际环境填写，127.0.0.1:PORT 是网络代理，Claude 的 CCR 3456 保留在 Claude 自己的配置中。无需把 Cookie 或模型密钥写进 Hub 配置。

Windows 新执行器接入与真实问候验收见 [2026-10-12 Windows 记录](docs/2026-10-12-windows-executor-validation.md)。Cursor IDE 可配置 `executable` 为实际桌面程序路径，仍需在线界面 worker。Cursor CLI 可通过原生 Node 的 `executable` 与入口脚本 `args` 启动，并用 `credentials_file` 引用 BDK 的既有私有凭据；`sandbox` 默认 enabled，本轮经用户明确批准仅 Windows CLI 使用 disabled 权限白名单模式，不提供 OS 沙箱。

并发在 config.json 的 `concurrency` 中设置：`{"global":2,"per_agent":{"codex":1,"claude":1,"muse":1}}`。各额度允许 1–32 的整数；未配置的执行器默认 1。修改后在没有在途任务时停止后台，下次调用自动加载新配置。同一实际工作区始终串行。

```powershell
.\.venv\Scripts\python.exe -m agent_hub status
.\.venv\Scripts\python.exe -m agent_hub mcp
# 显式停止后台（会取消本地运行任务，Muse 远端可能继续）
.\.venv\Scripts\python.exe -m agent_hub stop
```

MCP 使用 stdio：command 为虚拟环境 python.exe 的绝对路径，args 为 `["-m", "agent_hub", "mcp"]`。可以通过 `AGENT_HUB_DATA_DIR` 指定另一份私有数据目录。默认目录按当前 OS 用户保护；不监听外部网卡。Windows 自动启动通过 WMI 避免 MCP 前端关闭时杀掉后台；若 WMI 被系统策略禁用，可以在终端先运行 daemon 命令。

## 额外说明

两台机器不在同一网络时，可以用 Tailscale 让它们互相 SSH 可达，不需要公网 IP，也不需要端口转发。Hub 仍通过本机 `ssh` 调用对端私有 Hub。Tailscale 只打通网络层。安装、登录和 tailnet 由用户自己完成；本次没有在代码里加入受管 Tailscale 隧道。

已验证的布置：Windows 与 Mac 分处不同网络；专用 SSH 私钥在 Windows 用户 `.ssh`，对应公钥已写入 Mac `authorized_keys`；Windows 经 Tailscale 上的标准 SSH 调用 Mac Hub，Codex、Claude、Muse、Grok、Cursor 成功。main `5ca9724` 已合并 SSH 通用修复：请求与响应按 UTF-8 字节传输 JSON，远端 Python 使用 `-X utf8`；RPC 错误与任务自身失败分开处理；提交或续接在响应未确认时不自动重发，并保留已得到的目标任务 ID。

配置：

1. 在 Windows 和 Mac 上安装 Tailscale，登录同一账号，或加入同一个 tailnet。
2. 在 Tailscale 客户端的机器列表或 `tailscale status` 中确认对端在线。
3. 在发起连接一侧的 SSH 配置中，把主机别名指到对端的 Tailscale IP 或 MagicDNS 主机名。Windows 配置文件在用户目录 `.ssh/config`。端口、密钥和 ProxyJump 写在这份配置里；`SshClient` 传主机别名（或 `user@host`）、按显式 platform/shell 构造的目标命令，以及 `BatchMode=yes`、`StrictHostKeyChecking=yes`、`ConnectTimeout=5`。
4. Hub 继续使用现有远端参数。命令行是 `--ssh-host`、`--remote-python`、`--remote-data-dir`；`config.json` 里 remote 执行器仍是 `ssh_host`、`python`、`data_dir`。`--ssh-host` / `ssh_host` 填写上一步的主机别名。远端 Python、数据目录与 remote 执行器 cwd 填写目标平台上的绝对路径。省略新增连接参数保持旧 POSIX 行为。

Windows 调用 Mac 的示例（别名和路径换成自己的）：

```
Host mac-hub
    HostName <Tailscale IP 或 MagicDNS 主机名>
    User <Mac 用户名>
    IdentityFile <Windows 上专用私钥路径>
```

首次连接前，先在终端手动执行一次 `ssh mac-hub`（或你配置的主机别名），核对并接受目标机主机指纹，把密钥写入本机 `known_hosts`。Hub 使用 `BatchMode=yes` 与 `StrictHostKeyChecking=yes`，未知或变化的主机密钥会被拒绝，不会交互提示。

```powershell
.\.venv\Scripts\python.exe -m agent_hub status --ssh-host mac-hub --remote-python /path/to/project/.venv/bin/python --remote-data-dir /Users/your-name/.agent-hub
```

```json
{"agents":{"remote-worker":{"kind":"remote","platform":"darwin","ssh_host":"mac-hub","python":"/path/to/project/.venv/bin/python","data_dir":"/Users/your-name/.agent-hub","agent":"claude","cwd":"/Users/your-name/project","mode":"read_only"}}}
```

目标机需能接受该密钥的 SSH 登录，并已安装本项目与执行器。未知或变化的主机密钥会被拒绝。节点声明只记录身份，`remote_connection_verified` 仍为 false。

边界：Tailscale 的安装与登录由用户自己完成。网络不稳定时 Tailscale 可能偶发不稳定，连通由用户的 tailnet 负责。SSH 密钥、`authorized_keys` 和 Hub 配置留在各自机器的私有目录，不进入 Git。已验证的方向是 Windows 调用 Mac；原生 Windows 目标机支持见下一节；本次没有新增真实 Windows SSH 实测结论。


### 原生 Windows SSH 目标机

目标 Hub 已安装于原生 Windows Python 环境时，指定 `--remote-platform windows`，并按目标 Windows OpenSSH 的已配置默认 shell 指定 `--remote-shell cmd` 或 `powershell`（Windows 省略 shell 时为 cmd）。不探测 shell、不自动降级、不修改 OpenSSH DefaultShell。`cmd` 指 cmd.exe；`powershell` 指 Windows PowerShell 或 PowerShell 默认 shell，目标 PATH 必须能找到 `powershell.exe`（Windows PowerShell 5.1）。自定义启动器、WSL、Git Bash 等不在 Windows shell 支持范围内。

```sh
python -m agent_hub status --ssh-host windows-hub --remote-platform windows --remote-shell cmd --remote-python 'C:/Hub/.venv/Scripts/python.exe' --remote-data-dir 'C:/Users/your-name/.agent-hub'
```

以上单引号是 POSIX 发起端终端写法；Windows cmd 发起端用双引号。同样的远端连接参数适用于 `rpc`、`show`、`mcp`；任务参数用 `--arguments -` 送入 stdin，prompt 不放在远端命令里。remote 执行器示例：

```json
{"agents":{"windows-worker":{"kind":"remote","platform":"windows","shell":"cmd","ssh_host":"windows-hub","python":"C:/Hub/.venv/Scripts/python.exe","data_dir":"C:/Users/your-name/.agent-hub","agent":"claude","cwd":"D:/中文 项目","mode":"read_only"}}}
```

支持盘符绝对路径（`C:/...`、`C:\...`）与常规 UNC（`//server/share/...`、JSON 中 `\\\\server\\share\\...`）。不展开环境变量或 `~`；拒绝盘符相对路径 `C:foo`、无盘符根路径、控制字符、设备命名空间、ADS 和 Windows 非法文件名字符。UNC 是否可访问取决于目标 SSH 用户的权限，Hub 不配置网络凭据。中文、空格、单引号、`&`、`$`、反引号、`%`、`!`、`^` 按路径字面值传递。

两种默认 shell 都运行固定的 `powershell.exe -NoProfile -NonInteractive -EncodedCommand`，脚本使用 UTF-16LE/Base64。路径与 Windows CRT 参数串使用 PowerShell 单引号字面量引用；按 [CodeGeneration.EscapeSingleQuotedStringContent](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.codegeneration.escapesinglequotedstringcontent) 语义，将 ASCII 单引号及 U+2018/U+2019/U+201A/U+201B 全部加倍，防止智能引号结束字面量。

Python 改由 `ProcessStartInfo` 启动，而不是 PowerShell 原生命令调用：`UseShellExecute=false`、`RedirectStandardInput=false` 让 Python 继承 SSH stdin（[.NET Framework Process 实现](https://github.com/microsoft/referencesource/blob/main/System/services/monitoring/system/diagnosticts/Process.cs)将 stdin 设置为原 `STD_INPUT_HANDLE`）。脚本不读 stdin、不引用 `$input`，不建立文本管道；stdout/stderr 各通过 BaseStream.CopyToAsync 并发转发到原标准句柄，不解码、不合并。PowerShell 默认 shell 的外层启动器也使用相同方式，避免 native stderr warning 变成错误记录、污染成功 RPC 的 JSON。Python 使用 `-X utf8`，实际路径与末尾反斜杠由 Windows CRT 引号规则传递，退出码由子进程 `ExitCode` 显式传出。所有初始化均在 try/catch 内；启动或转发异常写 stderr 并退出 1。保留 host key 严格检查、RPC 与任务失败区分、不确定派发不重发、8000 字符命令长度限制。

关于 stdin：已核查开源 PowerShell v7.4 的 [ConsoleHost](https://github.com/PowerShell/PowerShell/blob/v7.4.0/src/Microsoft.PowerShell.ConsoleHost/host/msh/ConsoleHost.cs)、[Executor](https://github.com/PowerShell/PowerShell/blob/v7.4.0/src/Microsoft.PowerShell.ConsoleHost/host/msh/Executor.cs) 和 [NativeCommandProcessor](https://github.com/PowerShell/PowerShell/blob/v7.4.0/src/System.Management.Automation/engine/NativeCommandProcessor.cs)。redirected input 可令 ConsoleHost 异步执行，但 ReadInputObjects 在命令 AST 使用 `$input` 时才开启；Executor 否则关闭对象输入管道，不反序列化 stdin。原生命令另依 ExpectingInput 决定重定向。当前脚本通过 .NET 直接启动子进程，避开该原生命令路径。这是源码核查依据，不能替代闭源 Windows PowerShell 5.1 的目标实测。[5.1 官方文档](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_powershell_exe?view=powershell-5.1) 的 InputFormat 只支持 Text/XML，未使用无效的 `-InputFormat None`。

sshd 外层仍按目标配置启动 `cmd /c` 或 `powershell -c`，本项目不关闭其 AutoRun/profile，也不改 SSH 配置。外层 AutoRun、PowerShell profile 不得向 stdout 输出内容、读取 stdin 或改动标准句柄，否则会破坏 JSON 协议；内层 `-NoProfile` 无法撤销外层初始化的影响。

配置文件的 remote 节点必须显式声明具体 `platform` 为 `darwin`、`linux` 或 `windows`，连接与节点声明共用该值，不能写 `posix` 或省略。`shell=posix` 适用于 darwin/linux。旧 POSIX CLI、SshClient 和直接 Remote 构造仍保留默认 `platform=posix`，无需增加参数。

Windows 专用实进程测试使用与 sshd 匹配的外层 `cmd.exe /c`、`powershell.exe -c`，不额外禁用外层 profile 或编码外层命令；覆盖大 UTF-8 stdin、智能引号正常路径与注入路径、官方逃逸 API 对照、独立 stderr warning、退出码 0/37 及 JSON 完整性。在 macOS/Linux 自动跳过。实进程测试不能替代 Windows OpenSSH 端到端验收；需在目标环境运行 `python -m unittest discover -s tests -p test_remote.py -v` 并验证已有 SSH 连接后验收。

## 总控接入

Mac 三方入口的最新路由与验证见 [Claude/Codex/Cursor 双向接入](docs/2026-10-11-local-three-agents.md)：本机 `cursor` 默认原生 IDE 界面桥，`cursor_cli` 显式选择 CLI；项目示例里 Cursor IDE 默认 `enabled: false`（见 `config.example.json`），`cursor_cli` 单独配置。Claude、Codex 的真实总控与被调用双向复验通过。开通 Pro 后，Cursor IDE 总控调度 Claude、Hub 委托 IDE 纯文本任务和同会话续接均实测通过，Cursor CLI 读取随机文件复验通过。IDE 桥需要具备电脑控制能力的外部 worker 在线，尚无常驻无人值守驱动；IDE 编辑、取消及重开后会话定位尚未验收。

在 Codex 的 MCP 设置中登记 `agent_hub`，command 使用本机项目虚拟环境 Python 的绝对路径，args 为 `["-m", "agent_hub", "mcp"]`。重新加载 MCP 或打开新聊天后应看到 20 个工具（含 `create_work_item`、要求 `work_item_id` 的 `submit_task`、`link_task`、`reconcile_task`、`record_activity`）。移动源码或环境后需同步修改 MCP 注册路径；Windows 测试环境的 D 盘迁移见 `docs/2026-10-10-d-drive-migration.md`，Mac 使用自己的项目路径。

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

`wait_for_task` 是只读等待工具，单次最多等待 10 秒，默认 10 秒，0 秒立即返回快照。检测到 succeeded、failed、timed_out、cancelled、interrupted、delivery_unknown 任一终态就提前返回。MCP 与带令牌的回环 HTTP `/rpc` 使用相同契约。

返回 `state`、`events`、`next_seq`、`has_more`、`timed_out`。`timed_out=true` 只表示本次等待到期，不会取消或重发任务；任务自身期限到期则是 `state="timed_out"`，这时等待返回 `timed_out=false`。

总控应保存最后实际收到的 next_seq：若未终态，立刻用该游标再次等待；若终态但 has_more=true，继续读取至 has_more=false，再调用 get_result 获取完整答案。每页最多 200 条事件，游标只前进到已返回的最后一条。断线后使用最后收到的游标续接；响应丢失可能重复读取，按事件 seq 去重，不重新提交任务。

等待由 Hub 执行，但持续续接由总控负责；不会自动唤醒已结束聊天或发桌面通知。服务关闭时等待返回 SERVICE_STOPPING，重连后继续原任务游标。

更新源码后，已运行后台不会自动加载新工具。在无在途任务且明确允许停止的时机运行 `python -m agent_hub stop`，下次调用启动新版后台；重新加载 MCP 或开启新聊天刷新当前 20 个工具。验收脚本使用隔离服务；2026-10-10 已按用户授权重启共享后台并验证八个工具，见等待验收记录。后续更新仍需手动重载。

编码任务使用 `edit_worktree`，以 `HEAD` 已提交版本为基线，要求已跟踪文件没有未提交修改且有基线提交。主目录可以存在未跟踪文件：这些文件保留在原目录，不复制到任务工作区，任务状态和结果的 `excluded_untracked_files` 列出未纳入的路径，`baseline` 记录提交 SHA。需要使用未提交内容时，先明确并准备相应基线。Hub 不自动提交或合并、不删除任务 worktree。返回 diff、变更文件和日志路径；模型说完成不等于代码测试通过。worktree 不是安全沙箱。

## 执行器限制

- Cursor CLI：read_only 使用 ask；edit_worktree 仅放行工作区文件工具，原生 resume。执行配置独立于总控 MCP，拒绝项目级 MCP/hooks/权限配置；不使用 force，不开放命令。已完成本机文件、续接、取消及超时探针，未独立审计完整 OS 沙箱。
- Grok Bot：delegate，通过 Cursor BDK 公布的 session API 接收并回收任务；使用专用 Bot，cwd 不自动同步。续接、远端取消、期限中断和正常后台退出已实测；只有 interrupt 后独立确认回合停止，或本任务已自然完成并确认 idle，才报告 `remote_cancelled=true`，已发生操作不回滚。不确定派发或停止保留跨数据目录的私有锁，先人工核对再续发。两者总控及使用说明见 [双向接入](docs/2026-10-11-cursor-grokbot-validation.md)。

- Codex：read_only 使用自身 read-only 沙箱；edit_worktree 使用 workspace-write；不加载用户 MCP 配置。Windows 默认使用已在本机探针通过的 MXC（Codex 0.162.0）；其他设备可明确配置 windows_sandbox，不能通过关闭沙箱解决权限问题。
- Claude：已验证 Windows CCR/apiKeyHelper 和 Mac DeepSeek Anthropic 兼容 API 配置；Claude 订阅认证尚未验收。text_only 禁用所有工具，适合提供上下文后的文本分析；read_only 仅开放 Read/Glob/Grep；edit_worktree 仅 Read/Glob/Grep/Edit/Write，第一版不允许运行命令。safe-mode 禁用自定义技能/插件，严格空 MCP 配置防止递归。
- Muse：使用 delegate 委托模式，Hub 不强制只读，可能调用云端及已连接 Mac 的写入工具；操作由 Muse/客户端权限与审批决定。文本调研，每个初始任务独立会话。结果采用会话、消息序号与最终回答标记关联；不宣称远端工作流已完成。取消/超时会尝试中断原 Muse 会话，并独立核验停止状态；以结果中的 `remote_cancelled` 为准，未确认时远端仍可能继续。发送状态不确定时不自动重发。
  正文链接与后续 external_link 附件均汇入 source_urls；两次稳定轮询后返回。该列表保留 Muse 返回的来源，不等于 Hub 已核实网页内容。
  MCP 内层 `ok=false` 作为失败处理并保留上游错误码。仅历史读取遇到临时网关、超时或 VM 不可用时最多尝试三次，间隔为 1、2 秒；受任务期限和取消约束，并记录 retry 事件。认证失败和异常响应格式不重试。会话创建和任务发送不自动重试，避免重复会话或重复执行。
  completion_evidence 保留最终回复的 reply_to_message_id（可能为空）；非空字段不会排除已完成且带本任务标记的回复。读取失败耗尽后保留远端状态不确定，不将其视为远端已停止。
  Muse 发送使用 fire_and_forget，只取得派发确认；回复由 Hub 的历史轮询统一回收，不在发送工具内重复等待回复。
  Hub 专用 Muse 子进程启用 `MUSE_MCP_REUSE_GATEWAY=1`，配套 muse-mcp 支持在锁保护下复用连接，减少重复 HTTPS 认证；连接异常会丢弃缓存，不重放业务操作。普通 Muse 工具默认仍使用独立连接。配套兼容性补丁和基线安装步骤见 [vendor/muse-mcp/README.md](vendor/muse-mcp/README.md)，部署时需应用补丁并配置自己的认证与 Python 路径。
- 后台重启：running/cancelling 标 interrupted，不按旧 PID 杀进程，也不自动重跑。无法确认旧执行已停止的工作区隔离，拒绝直接续接；同工作区此前已排队的任务也转 interrupted，其他工作区 queued 继续。需人工核对，在新工作区提交。正常终态使用已保存的 session_id 续接。Mac 上强制杀死后台不保证旧执行器停止。
- Mac 已完成本机核心闭环及补充实机验收，详见 [Mac 验收记录](docs/2026-10-11-macos-full-acceptance.md) 与 [补充异常验收](docs/2026-10-11-macos-resilience-validation.md)。严格的睡眠期间到期后恢复处理仍未验证。DeepSeek Harness、Claude 订阅模式未验收。Muse 原会话远端取消和 SSH 跨机器调度已实现；2026-10-11 当时真实双机 SSH 尚未验收，2026-10-13 的跨网络实机见文首与「额外说明」。Mac 不使用 Windows lock 文件：创建环境后 `pip install -e .`，按本机路径配置。

## 诊断与验证

`status` 只检查可执行路径、版本和已配置能力；账号有效性由实际任务确认。任务状态为 failed 时读取 get_result 和私有日志。出现 delivery_unknown 时先检查 Muse 原会话，不重复提交。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 明确授权后的短真实账号验收，会消耗账号额度
.\.venv\Scripts\python.exe scripts/acceptance.py
```

开发设计、实施计划、开发进度和验收记录位于 docs。认证和运行数据不进入 Git；完整日志仅在用户私有数据目录保存。

Mac 2026-10-11 的记录分为 [核心验收](docs/2026-10-11-macos-full-acceptance.md) 与 [异常及稳定性验收](docs/2026-10-11-macos-resilience-validation.md)。仍未验证严格的睡眠期间到期后恢复处理、Muse 底层浏览器动作独立审计、数小时持续稳定性、真实登录自然过期后的恢复。Muse 远端取消和 SSH 调度随后已实现。该日记录中的真实双机 SSH 当时尚未验收；2026-10-13 已另有 Tailscale 跨网络实机，见「额外说明」。DeepSeek Harness、Claude 订阅模式按该轮范围排除。

`scripts/macos_*.py` 是本轮隔离验收及证据复核脚本，使用固定私有目录和请求键，不能把重放旧结果当作新验收。涉及真实账号的脚本会使用账号额度；休眠脚本会让整台 Mac 休眠，须每次明确授权并准备手动唤醒。它们不由普通自动回归执行。

## 任务模式与权限

| 执行器 | 模式 | 实际限制 |
|---|---|---|
| Claude | text_only | 无工具，仅文字分析 |
| Claude | read_only | 仅 Read/Glob/Grep，无写入、命令或 MCP 工具 |
| Claude | edit_worktree | 独立 worktree，Read/Glob/Grep/Edit/Write，无命令；不是 OS 沙箱 |
| Codex | read_only | Codex 只读沙箱 |
| Codex | edit_worktree | Codex workspace-write 沙箱与独立 worktree |
| Muse | delegate | Hub 不强制只读；实际操作由 Muse 和客户端权限决定 |
| Cursor CLI | read_only / edit_worktree | ask 或工作区文件白名单，无 Shell/MCP/WebFetch；未全面审计 OS 沙箱 |
| Grok Bot | delegate | 云端专用 Bot，Hub 不强制只读；取消尝试远端中断，停止状态以 remote_cancelled 为准 |

list_agents 的 permissions 按模式说明 enforced_by、enforced、allows_write、allows_commands、os_sandbox 及作用范围；submit_task、get_task、get_result 也返回 permissions。enforced 表示声明的模式限制由执行器强制，而非所有执行器拥有同等隔离保障。Muse 的 allows_write/commands 表示可能发生，不能据此认为已获客户端授权。

旧任务保留原始记录和 request_id 幂等性。历史 Claude read_only 续接按 text_only 执行；历史 Muse read_only 禁止续接，需按明确操作范围新建 delegate 任务。仅 get_result 成功不代表实际操作正确，仍需核对文件或外部结果。更新后需无在途任务时重载后台，并刷新客户端 MCP 工具说明。

## HubGateway v1（服务版本 0.3.0）

提供 `get_capabilities()`、`get_artifact(artifact_id)`。当前共 20 个 MCP 工具。公开 `submit_task` 必须带 `work_item_id`（先 `create_work_item`，默认标题「工作项」，可选 `acceptance`）；缺失返回 `WORK_ITEM_REQUIRED`，不会静默建根。软件看板模板见 `Client.software_dashboard_acceptance()`（tests/ui/review），对话见 `conversation_acceptance()`；开发/返修继承、审核不继承；`continue_task.acceptance` 仅作用于新子任务。历史任务用 `link_task` 补关联；`reconcile_task` 仅 GET 核对已 accepted 但本地观测中断的远端任务。`interrupted`/`delivery_unknown` 的 `gateway_state` 为 `unknown`，不得当成确定失败。
`submit_task` 新增可选 `context`，省略或 null 保持旧请求哈希兼容；对象内容全部参与幂等哈希。
任务状态保留原始 `state`；结果另提供 `gateway_state`、`execution_uncertain`、`errors` 和 `media_artifacts`。
实现边界、配置和测试见 [HubGateway 交接说明](docs/HubGateway-v1-实现交接.md)。

## Muse 等待人工处理

Muse 遇到人机验证、登录或审批时，可报告 `waiting_for_user`（非终态）。`get_task`、`get_result`、`wait_for_task` 返回 `intervention`，含 `intervention_id`、原因、消息、Muse `session_id` 和人工等待期限；结果保持 `ready=false`。`wait_for_task` 提前返回 `needs_user=true`、`timed_out=false`，总控应停止连续轮询并提示用户在 Muse 原会话接管处理。

仅当用户明确确认处理完成后，调用 `resume_task(request_id, task_id, intervention_id)`。此工具恢复原任务和原会话，不创建子任务；同一介入即使收到不同 request_id 也只投递一次继续指令。之后查询原 task_id；不要重新 submit，也不要对等待中的任务调用 continue_task。`acknowledged=true` 仅表示本地确认已登记，最终回答仍需 get_result 核实。

每个任务人工等待总预算为 15 分钟，人工等待期间暂停普通执行期限；恢复后使用剩余执行时间。人工等待到期、用户取消或服务停止沿用 Muse 原会话取消与独立停止核验；停止不确定时保留锁，不自动续发。浏览器中人工处理后，如果 Muse 自行给出最终回答，也会回收原任务结果。

当前检测优先使用派发提示中约定的 `[HUB_WAIT:<task_id>:human_verification|login|approval]` 消息标记，保守识别最新助手消息中的明确阻碍和接管请求作为辅助，并标明 `evidence`。尚未验证 Muse 原生结构化人工等待字段；这不是通用浏览器状态探测，模型不报告时可能仍需人工查看。Hub 不操作人机验证。

当前本机 MCP 共 20 个工具；后台需在无在途任务时重载，Cursor/Codex 客户端需重新连接 MCP 才能发现 `record_activity`、resume_task 和新版等待说明。已开始的旧 worker 不会热升级。SSH 执行器的人工等待透传不在该次实现范围。

HubGateway 媒体接入已完成首轮真实文本审核与 Muse 默认图片路由验收：`text.review/deepseek-flash[1m]` 和 `image.generate/muse:auto` 已登记证据。`muse:auto` 表示默认路由，底层模型未知；仅支持已验收的一张 PNG 无参考图路径。

## 独立任务验收（第一阶段）

`submit_task` 可传 `acceptance` 对象，路径相对于任务实际工作区：

```json
{"criteria":[{"id":"output","kind":"file_exists","path":"output.txt"},{"id":"digest","kind":"sha256","path":"output.txt"}]}
```

还支持 `artifact_required` 和 `content_review`（默认需人工裁决，可显式开启下文的 Claude 自动审核）；`required` 默认 true。验收条件参与 request_id 幂等哈希，continue_task 自动继承。

执行 state 和 Gateway 契约保持原样；读取新增的 `verification`、`work_status`：只有验收 passed 映射为 completed；无条件的旧任务成功仍为 succeeded。`ready=true` 只表示执行结束。`get_work_item(task_id)` 汇总尝试链、检查原因、产物逻辑 ID/哈希及待处理动作，`verify_task(request_id, task_id)` 幂等重验。

```sh
python -m agent_hub show TASK_ID --data-dir /path/to/private-data
# needs_review 的人工裁决仅开放本机已认证 RPC，不注册 MCP
python -m agent_hub rpc record_verification --data-dir /path/to/private-data --arguments '{"request_id":"review-1","task_id":"TASK_ID","verifier_id":"human","decision":"pass","reason":"已核对"}'
```

MediaFlow 以 context + acceptance 提交，通过 get_work_item / get_result.verification 读取。外部检查结果由下文的 `record_external_verification` 写入，Hub 仍不生成图片或解码视频；此外部验收写入尚未接通 Muse。设计与边界见 [验收设计](docs/2026-10-12-verification-phase1-design.md)。第一阶段当时新增 verify_task、get_work_item；加上后续阶段与时间线上报后，MCP 现为 20 个工具。客户端需重新加载后才能看到新工具。

带 acceptance 的尝试链保持线性：只能从当前末端 continue_task，旧尝试的新请求返回 ATTEMPT_SUPERSEDED（同键重放仍幂等）。artifact_required 要求本次新增或内容哈希改变，已有声明文件、touch 及复用其他任务媒体均不算产物。验收异常有界重试后转 needs_review。

`show TASK_ID --ssh-host HOST --remote-python /path/to/python --remote-data-dir /path/to/data` 可读取远端交接包（SSH 放行只读 get_work_item）。verify_task 和 record_verification 不经 SSH；人工裁决仍只使用本机已认证 RPC。

验收扫描与关机等待预算可在本数据目录的 `config.json` 中配置（下例为缺省值）：

```json
{"verification":{"max_files":1000,"max_bytes":268435456,"max_entries":5000,"max_seconds":10,"join_seconds":20}}
```

字段只接受范围内整数，未知字段也会返回 `INVALID_CONFIG`；范围见[设计文档](docs/2026-10-12-verification-phase1-design.md)。已登记媒体不占读取文件数额度；基线不完整时交给人工审核。相同内容重新生成仍可能标为 `copied`，不计作 `artifact_required` 证据。

## 第二阶段：工作台与总控

三个 CLI（Codex、Claude、Cursor CLI）使用结构化输出映射实际过程事件；缺失工具/审批信息明确标示，进度自述不算验收证据。第二阶段当时只核实了 CLI help；之后 Cursor CLI 与 Claude CLI 的结构化输出已在自动审核阶段的真实闭环中使用并验收，但这不等于各类工具/审批事件语义都已实测。

本机工作台地址为当前独立后台的 `http://127.0.0.1:PORT/work/TASK_ID`。先由受信本机客户端向 `POST /work/TASK_ID/session` 发送 `Authorization: Bearer TOKEN`，接收绑定 TASK_ID 的 cookie 并交给同一浏览器会话；随后 GET 页面及 `/events?after_seq=N` 使用 HttpOnly cookie，访问其他任务路径返回 401。页面通过该任务的 events 接口及受归属校验的 attempt_id 参数读取同一尝试链，不扩大 cookie 的任务路径权限。也可直接用 Bearer 获取页面。不要把令牌加进 URL、HTML 或命令行参数。`show TASK_ID --data-dir DATA_DIR` 保留文本查看方式。

页面顶部分别显示工作状态（当前尝试的综合 `work_status`）、当前尝试、当前尝试执行状态与当前尝试验收状态；执行 `succeeded` 只表示执行结束，不等于验收通过。每次尝试单独显示执行状态、执行时间线、验收条件、当前结论与原因，以及按 `artifact_id` 去重的产物列表；新结论整体替换旧结论。

页面数据分两个通道：

- 事件通道：每次尝试使用 `/events?attempt_id=...&after_seq=N` 的 seq 长轮询（服务端最长等待 10 秒）。当前尝试和未结束的尝试连续轮询；已结束的非当前尝试每 30 秒轮询一次，以收到后续的人工验收或续接事件。执行状态事件会立即更新对应尝试的执行状态。
- 状态快照通道：`GET /work/TASK_ID/snapshot`，鉴权与页面相同，响应 `Cache-Control: no-store`。只返回当前尝试 ID、工作状态、当前验收结论、待处理事项，以及每次尝试的 `task_id`、`parent_id`、`attempt`、执行状态、`work_status`、验收结论和产物；不含事件、会话、工作区路径与总控租约。顶部状态和验收结论以快照为准。收到执行状态、验收、验收重试、会话或产物事件后立即刷新一次快照；另外，有尝试在执行或验收中时每 3 秒刷新，全部结束后每 15 秒刷新，用来覆盖没有事件的阶段（如进入 `verifying`，或没有验收条件的续接）。快照出现新尝试时整页刷新。

两个通道的故障分别记录、分别恢复，一个通道成功不会清除另一个通道的故障。网络错误或非 2xx 响应时，页面显示“连接中断”，注明是哪个通道、原因、失败次数，并说明页面停留在哪个时间点的快照；该通道按 1、2、4…最长 30 秒退避重试。返回 401 时显示“未授权”，提示由总控重新调用 `POST /work/TASK_ID/session` 签发会话，并每 5 秒重试；会话恢复后自动继续同步。快照通道在一次快照成功后恢复；事件通道在每个出错尝试各自的轮询重新成功后恢复。两个通道都正常时才显示“实时同步中”。

总控通过 MCP 或本机 RPC 接管（SSH 也支持 claim 和 get_work_item）：

```json
{"tool":"claim_controller","arguments":{"request_id":"claim-1","task_id":"TASK_ID","controller_id":"controller-A"}}
```

返回 `controller_epoch`，随后 continue_task、cancel_task、verify_task、record_verification、record_external_verification 和 switch_executor 参数均带 `controller_id` 与 `controller_epoch`。另一个 claim 会递增 epoch，旧总控收到 CONTROLLER_STALE。相同 request_id 返回原接管收据；没有租约的旧任务仍可直接操作。

`switch_executor` 参数为 `request_id, task_id, agent, mode, prompt` 加可选租约字段。须从已终止的当前尝试交接，创建新尝试并附目标、验收条件、失败原因、产物哈希与最近事件；不复用原生会话。同执行器续接仍调用 continue_task。

独立数据目录的 config.json 可设置：

```json
{"verification":{"max_auto_rework":2}}
```

范围 0–5，0 关闭自动返工。只有 Hub 确定性 fail 且没有 inconclusive 的 rework_required 才自动续接原执行器/会话，最多累计该次数；保留全部尝试，同验收 run 不重复派发；验收已落盘但返工未派发时，重启会按同一 run 与次数上限补派，不重新验收。needs_review、验证器不可用，以及未开启 `acceptance.review` 时的审核失败（人工或外部写入的 content_review fail）都不自动派发；开启后，只有 Hub 受信校验的所选执行器审核 fail 会自动返工，见下文「Claude 自动内容审核」。copied 文件只有同一路径的显式 expected SHA-256 匹配 Hub 实读值时，才可满足 artifact_required。

审核写入仍只允许本机 RPC。record_verification 的 content_review 结果必须提供 `reason` 与已有 evidence 的 `artifact_id`。未开启 `acceptance.review` 的任务未收到审核结果时保持 inconclusive/needs_review，不会自动调用审核模型（开启方式见下节）。

### Claude 自动内容审核（opt-in）

在单个任务的 acceptance 中加入 `review`，criteria 写法不变：

```json
{"criteria":[{"id":"out","kind":"file_exists","path":"report.md"},
             {"id":"quality","kind":"content_review","description":"报告覆盖三项结论并给出依据"}],
 "review":{"agent":"claude","timeout_seconds":600}}
```

`agent` 可选择任何已配置且支持 `text_only` 的执行器（含符合契约的插件），`timeout_seconds` 为 30–3600 的整数（缺省 600），至少需要一条 content_review。同一个执行器不能自审核。未带 `review` 的任务、历史任务和请求哈希都不变，content_review 仍为 needs_review，Hub 不批量派发模型。上方 Claude 示例仍兼容；通用命令桥接不声明 `text_only` 能力。

流程：确定性检查全部通过后，验收进入 `reviewing`（work_status 同名），Hub 以内部请求键 `auto-review:<review_run_id>` 创建一个**独立根任务**（所选审核端 `text_only`，无工具，空私有 cwd，不是执行尝试的子任务，不改变 current_task_id），照常经过全局/按执行器并发调度。审核任务 ID 见 `verification.review.task_id`、`review` 事件和工作台；可对它单独 get_task / get_events / 打开工作台。若确定性检查本身失败，审核条件标为延后，不派发模型，按原规则返工。

证据只取本次验收检查已登记的工作区文本产物：最多 10 个文件、单文件 32 KiB、总计 64 KiB，严格 UTF-8，不跟随符号链接、不越出任务工作区，读取时复核登记 SHA-256；内容放入带随机边界的提示中，明确为不可信数据。登记了媒体产物、远端工作区、二进制/超限/已改变的文件，均直接 needs_review 并写明原因，不派发模型。

审核端必须只输出一个纯 JSON 对象，例如（ID 为示意值，实际必须与提示中的审核上下文逐字一致）：

```json
{"schema":"agent-hub.content-review.v1","task_id":"d0d1eb9b8238495995fe4f2c17d01332","review_run_id":"4381dc4da64b419ab58145a6195140dc","verification_run_id":"0ee7778bdb2a4df0bb6418eb7197e956","checks":[{"criterion_id":"quality","outcome":"fail","reason":"内容不是 GOOD 加一个换行，请改为仅含 GOOD 并以单个 LF 结尾","evidence":["2eb0a44e321d5e1c81dc007971dd516d"]}]}
```

`outcome` 只能是 `pass`、`fail` 或 `inconclusive`；每条 content_review 恰好出现一次、reason 非空、evidence 只能引用提示中提供的 artifact_id。代码块包裹、自然语言、工具调用/DSML 文本、缺失/重复/未知条件、指纹不符、伪造证据、CLI 失败、超时、取消或审核端未配置，一律 fail closed 为 needs_review（`verification.review.state` 为 invalid/unavailable 等并保留原因），不自动重试、不返工。真实试跑中曾出现一次 DSML 格式的 Bash 工具调用文本（text_only 无工具，命令未执行），即按 invalid 处理；提示随后加强，但不保证模型每次都输出合法 JSON。

采用结论前，Hub 按取证清单重读产物并核对 SHA-256；审核期间文件被修改、删除、替换为符号链接或越界时，记 `review.state=evidence_changed` 并转 needs_review，不采用旧结论、不返工。派发前若总控租约已变更，直接 superseded，不调用模型。对处于 reviewing 的执行尝试调用 cancel_task，会停止对应审核任务（响应含 `review.review_state` 为 stopping/stopped/not_dispatched），验收转 needs_review，执行 state 仍为 succeeded，不自动返工。

全部 pass → passed / completed；任何 required fail → rework_required（`required=false` 的 content_review fail 只记录，不阻塞、不单独返工），并按 `max_auto_rework` 共享预算自动 continue_task（同一原生会话、继承 acceptance），新尝试重新确定性检查和 Claude 审核；到达上限保持 rework_required，pending_actions 写明上限。inconclusive 只转 needs_review。回写由 Hub 内部服务单事务完成并写 `auto-review-result:<run>` 收据：验收被 verify_task 重跑、人工/外部裁决、新尝试续接或总控 claim 改变 epoch 后到达的审核结果，只追加 superseded 历史；仍在 reviewing 时转 needs_review 便于接管。`reviewing` 期间总控可直接 record_verification 人工裁决或 verify_task 重验。`auto-review` 前缀请求键和 `hub-` 前缀 verifier_id 保留给内部，外部调用会被拒绝。重启时进行中的审核任务按原规则标 interrupted 并转 needs_review，已结束未回写的结果按收据只回写一次，均不重新提交模型。

多媒体项目可以把已经完成的检查回写到已有验收条件，调用仅本机 RPC 的 record_external_verification：

```json
{"request_id":"media-check-1","task_id":"TASK_ID","verifier_id":"media-project-checker","controller_id":"orchestrator","controller_epoch":1,"checks":[{"criterion_id":"dimensions","outcome":"pass","reason":"外部项目已检查尺寸符合要求","evidence":["REGISTERED_ARTIFACT_ID"]}]}
```

通过 `python -m agent_hub rpc record_external_verification --data-dir DATA_DIR --arguments -` 从 stdin 提交。evidence 仅接受该尝试已登记的 artifact_id；criterion_id 必须属于 acceptance.criteria（外部尺寸/质量检查可预先声明为 content_review）。同请求幂等；旧尝试迟到结果仅记 superseded。审核与外部结果接口不进入 MCP/SSH，执行器不能写验收结果。Hub 不生成图片或解码视频，此接口尚未接通 Muse 或在真实 Windows 验证。

CLI 过程事件只从 stdout 映射；stderr 仅脱敏写日志。CLI 会话只接受 stdout 的首个初始化记录，后续 init 或最终 result 不覆盖 session_id；缺少有效首个 init 时不提供原生续接。permission_denials 日志只保留工具名称和调用 ID。外部检查对非当前尝试（包括执行失败的旧尝试）只追加 superseded 历史，仍校验条件与证据归属。


## 第三阶段：节点与产物传输

`list_nodes` 可从本机 RPC、MCP、SSH 读取节点声明。跨网络时的 Tailscale 与 SSH 主机别名见上文「额外说明」。remote 配置必须显式声明 platform（darwin/windows/linux），可声明 node_id/host_id；非 local 节点不得使用本机 host_id，本机平台按系统登记，Linux 为 linux；配置本身不证明连通，remote_connection_verified 保持 false。submit_task 可带 node_id；省略保持原路由，指定时节点必须声明 agent 与 mode，否则 NODE_UNAVAILABLE。SSH 节点使用原 Remote 适配器的 delegate 模式，本机 cwd 不上传。

跨主机 get_artifact 带 `target_host_id`（也可带 `target_node_id` 由登记节点推导主机），不同 host_id 返回 transfer_required，不含发送端路径。仅本机 Bearer RPC 可调用 `transfer_artifact`，参数：

```json
{"request_id":"send-1","artifact_id":"artifact://REGISTERED_ID","target_node_id":"TARGET_NODE"}
```

接收端核对哈希后写 receipt，同请求不复制第二份，重启/超时保留 retryable，不改变执行或验收状态。SSH 传输返回 NODE_UNVERIFIED；其他机器的 local 接收端目前仅支持测试内注入的双目录模拟，没有生产跨机器传输。外部检查示例的 controller_id/epoch 须替换为 claim_controller 返回的租约；无租约可省略。


SSH 节点或 Remote 适配器任务的 `file_exists`、`sha256`、`artifact_required` 固定返回 inconclusive，原因是远端工作区不在本机验收范围；不读取本机 cwd 作为证据，也不因未看到远端产物而自动返工。所需检查须在目标 Hub 的任务工作区执行，发送端不能把远端成功自述当验收证据。

接收收据以 `(sender_host_id, request_id)` 为键，文件命名含发送方 host_id 的哈希，按实际内容 SHA-256 复用已存文件；不同发送方使用相同请求键不会串号，更换请求键不会重复存同一内容。config.json 可设置：

```json
{"artifact_transfer":{"max_received_bytes":1073741824,"max_received_files":10000}}
```

以上分别限制接收目录总字节、文件及收据总数（默认 1 GiB、10000）；达到上限返回 ARTIFACT_QUOTA_EXCEEDED，不写成功收据。旧收据迁移为 legacy-unattributed 历史，不能冒充任一已知发送方重放；可核验的旧接收文件加入内容索引。节点能力校验放在幂等重放之后，节点后来不可用时同键同内容仍返回原任务。

## 历史验证记录

以下是各日期、平台与源码基线上的历史验证，不代表本次文档编辑重新完成了这些实测。当前功能和工具数以上文及源码为准。

当前进度（2026-10-13）：Windows 与 Mac 分处不同网络，已用 Tailscale 加标准 SSH 实机验证 Windows 调用 Mac Hub，Codex、Claude、Muse、Grok、Cursor 成功。产品继续使用 SSH，Hub 不监听公网，也没有内置 Tailscale 管理。配置见下文「额外说明」。SSH 通用修复在 main `5ca9724`。

当前进度（2026-10-12）：在 main 基线 `16110f2` 上完成 Claude content_review 自动审核阶段（每任务显式 `acceptance.review` 开启，见下文「Claude 自动内容审核」与 [设计记录](docs/2026-10-12-claude-content-review-design.md)），经 Claude 独立终验通过后合入 main。任务验收第一至第三阶段为 `08f0542`、`b495f73`、`807ed0c`。MCP 仍为 16 个工具，含 `verify_task`、`get_work_item`、`claim_controller`、`switch_executor`、`list_nodes`。`transfer_artifact`、人工裁决和外部验收写入只在本机已认证 RPC。本阶段沙箱外完整回归 287 项通过（208.165 秒，1 项 Windows ACL 检查在 macOS 上跳过），其中自动审核专项 28 项。

真实 Cursor CLI 执行 + Claude CLI 审核闭环已验收：直接通过、审核 fail → 自动返工 → 通过、连续 fail 在 `max_auto_rework=1` 停止三条链路均符合预期，全程没有总控 continue_task 或 record_verification 介入；真实 headless Chrome 打开的工作台最终状态与接口一致。证据汇总在本机私有目录 `~/.agent-hub/auto-review-20261012/real-chain-summary.json`（直接通过链路来自 run-173812，返工与上限链路来自 run-174333）。此前的真实失败保留为历史证据、未被覆盖：run-172722 三条链路因审核登记 TypeError 失败；run-173812 中返工链路因 ECONNRESET 中断后在 run-174333 重跑。这些结果只说明本轮链路通过及非法输出按设计 fail closed，不代表模型总能输出合法结果。Windows 本机核心回归与六入口真实问候已通过，见 [Windows 验收记录](docs/2026-10-12-windows-executor-validation.md)；该记录的 259 项对应整合自动审核阶段之前的基线。新增自动审核闭环在 Windows 上的真实任务、Muse 验收回写、非 headless 的 GUI 浏览器工作台仍未验收。跨网络调用 Mac Hub 的后续实机见下文「额外说明」，该实机不覆盖本段自动审核闭环。用法见下文第二、三阶段；验收总设计见 [验收设计](docs/2026-10-12-verification-phase1-design.md)。

截至 2026-10-11 的记录：Windows 第一版与 Mac 本机接入已实现，当时为 11 个 MCP 工具。新增 Cursor CLI / Grok Bot 双向接入，真实总控派发、执行器回复和续接通过，见 [接入记录](docs/2026-10-11-cursor-grokbot-validation.md)。该日完整自动回归 129 项：128 通过、1 项 Windows ACL 检查跳过（49.332 秒）。严格休眠超时恢复等仍未验证。当日状态见 [开发进度](docs/开发进度.md) 和 [第一版验收](docs/第一版验收.md)；该文件保留历史测试数，不代表现在的工具数量。该日 Windows 记录来自 10-09/10-10，后续 Windows 回归与执行器接入见 10-12 验收记录。

Muse 原会话远端取消、SSH 跨机器 Hub 调度已实现。Muse 取消/期限/正常退出在同一专用会话实测通过；2026-10-11 的 SSH 调度完成两个隔离 Hub 的逻辑验证。跨网络实机已于 2026-10-13 用 Tailscale 加标准 SSH 验证，见下文「额外说明」与 [新增功能记录](docs/2026-10-11-muse-cancel-remote-hub.md)。
