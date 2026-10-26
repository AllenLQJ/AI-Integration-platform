# Muse 取消与跨机器 Hub 调度

日期：2026-10-11。当前接入快照 `5b52abb` 已推送 GitHub，本文记录之后的实现。

## Muse

`remote_cancel=true` 表示具有原会话取消路径，不保证每次取消成功。首次 cancel_task 返回 cancelling；get_result 的 remote_cancelled 与 cancellation_evidence 为最终证据。取消未确认时保留私有会话锁，拒绝直接续接。

实现使用既有 muse_raw 工具，无需更改或重新打包 muse-mcp。依据登录后的 [Muse 官方网页](https://muse.ai/) 当前客户端 `38gmmn9teg36m.js`：thread 路由把 session_id 加入 chat.cancel 空参数，响应读取 cancelled；路由表 POST /chat/cancel。真实 sessions.get 状态从 running 变为 interrupted，不能仅凭取消响应作停止确认。原本地进程树先退出，再开独立连接；清理最多 20 秒，内部确认最多 12 秒。已发生的文件、外部动作与独立后台进程不回滚。

专用验收会话 `60339828-f0de-4982-beb8-bdb7eb551f7f` 只创建一次，后续复用。运行任务 `3fe9786f6d5c47b694cb2f5e0036f0ce` 被中断，remote_cancelled=true、interrupt_acknowledged=true，独立两次稳定读取确认 interrupted；同一会话随后任务 `2bdca028b13b4e36a9b03d0103de6569` 正常回复随机标记，重复取消旧任务没有影响新任务。私有证据在 `~/.agent-hub/muse-cancel-20261011/hub-fixed/report.json`，协议探测与网页文件留在私有目录，不纳入 Git。

同一会话追加实测：10 秒期限任务 `aa5e7ec6eb8949579090680716024994` 最终 timed_out，正常后台退出任务 `f659c0f13dcf493c9977d5263ad36cb4` 最终 cancelled；两者均实际调用 chat.cancel，remote_cancelled=true，并独立确认 interrupted 与两次稳定读取。私有证据为该目录 lifecycle-report.json；两个隔离后台均已退出。

可选固定会话配置为 agents.muse.session_id。未指定时沿用独立会话与原生续接；不能把主聊天会话用作 Hub 专用工作会话。复现只使用已核对、空闲的专用会话：

```zsh
.venv/bin/python scripts/muse_cancel_acceptance.py --root "$HOME/.agent-hub/新的Muse验收目录" --session-id "已核对的专用会话ID"
```

## 跨机器

目标机须已安装本项目与执行器、完成自己的账号认证、配置 SSH。当前实现的目标机命令要求 POSIX shell/绝对路径；没有增加 Windows 目标机实测结论。SSH 使用已有 known_hosts，未知或变化的密钥拒绝连接，不自动接受。

直接将 CLI / MCP 总控连接到目标 Hub，原有八工具保持不变：

```zsh
.venv/bin/python -m agent_hub status --ssh-host your-known-host --remote-python /path/to/project/.venv/bin/python --remote-data-dir /home/your-name/.agent-hub
.venv/bin/python -m agent_hub mcp --ssh-host your-known-host --remote-python /path/to/project/.venv/bin/python --remote-data-dir /home/your-name/.agent-hub
```

也可在本机 config.json 添加命名执行器，按 delegate 提交本地队列任务：

```json
{"agents":{"remote-worker":{"kind":"remote","ssh_host":"your-known-host","python":"/path/to/project/.venv/bin/python","data_dir":"/home/your-name/.agent-hub","agent":"claude","cwd":"/home/your-name/project","mode":"read_only"}}}
```

本机任务 cwd 仅作本地调度工作区，实际工作区为配置中的目标机 cwd，目标机 edit_worktree 由目标 Hub 管理。本机文件不上传，目标机产物以 remote_artifacts 返回，路径属于目标机。代理的 session_id 是目标 Hub 任务 ID，remote_task.native_session_id 则保留真正原生会话。续接调用目标机 continue_task，取消原 remote_task_id；不会新建一个任务代替取消。

SSH 参数使用独立 argv，目标命令逐项引用，prompt 只走 stdin JSON。请求 ID 与目标 ID 在私有日志 remote-task.json 原子保存；发送响应不确定不自动重发，读取失联保留目标 ID 与 interrupted。服务重启不自动恢复在途远端任务，仍按既有规则隔离等待人工核对。list_agents 的远端条目只报告已配置 SSH，不递归探测另一 Hub，避免双向配置形成诊断循环；实际任务或远端 status 确认可达性。

自动验证使用 SSH 命令模拟器及两个隔离 Hub 实例，经真实 CLI/回环 RPC 检查提交幂等、原生续接、目标工作区修改与差异、原仓库保持基线、取消目标及断线后不重发。命令模拟器不会验收 SSH 登录、加密链路或真实双机网络；本轮仅有一台 Mac，当时仍须目标设备实测。2026-10-13 的跨网络实机见文末。测试代码见 tests/test_remote.py。


## 最终收尾

完整自动回归 129 项：128 通过、1 项 Windows ACL 跳过（49.332 秒），依赖与差异检查通过。共享后台已在空闲时重载，新 PID 73396，Muse/Grok 声明原会话取消路径，五执行器已启用，排队及在途 0；没有停止用户其他任务或更改账号配置。测试日志存放私有 `~/.agent-hub/remaining-features-20261011/full-regression.log`。

Muse 取消和 SSH Hub 调度的代码已实现；写本文时真实双机 SSH、严格物理休眠期间到期、自然登录过期、长时稳定性、Muse 底层浏览器动作独立审计与 Cursor IDE 实际派发尚未完成验收。Grok 同 Bot 的正常退出实际 interrupt 已补测通过，但强制期限中断仍未覆盖（再次自然结束）。DeepSeek Harness、Claude 订阅模式保持排除。

## 后续：Tailscale 跨网络实机（2026-10-13）

上文「本轮仅有一台 Mac」和收尾里的「真实双机 SSH 尚未验收」是 2026-10-11 的范围。2026-10-13 已在不同网络的 Windows 与 Mac 上，用 Tailscale 加标准 SSH 验证 Windows 调用 Mac Hub，Codex、Claude、Muse、Grok、Cursor 成功。专用私钥在 Windows 用户 `.ssh`，对应公钥在 Mac `authorized_keys`。

这是用户自己的 Tailscale 与标准 SSH 配置。Hub 仍调用本机 `ssh`，使用主机别名、`BatchMode=yes` 和 `StrictHostKeyChecking=yes`；端口、密钥和 ProxyJump 留在 `~/.ssh/config`。配置步骤见 README「额外说明」。SSH 传输修复在 main `5ca9724`（UTF-8 二进制 JSON、远端 `-X utf8`、RPC 错误与任务失败区分、不确定投递保留任务 ID）。

Hub 没有内置 Tailscale 管理。密钥和 Hub 配置留在本机私有目录。网络不稳定时 Tailscale 可能偶发不稳定。Windows 作为 SSH 目标机仍没有新的实测结论；目标机命令仍要求 POSIX shell 与绝对路径。自动审核闭环的 Windows 真实任务、Muse 验收回写和非 headless GUI 工作台不在这次实机范围内。
