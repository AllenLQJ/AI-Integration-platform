# scripts/

手动运行的验收与探针脚本，**不属于自动测试**，CI 不会执行它们。大多数脚本使用真实执行器账号，会消耗额度，并且各自在独立的私有数据目录里运行；运行前先读脚本开头的说明。

自动测试在 `tests/`：`PYTHONPATH=src python -m unittest discover -s tests`。

## 通用

| 脚本 | 用途 |
|---|---|
| `acceptance.py` | 使用一次性私有 Git 仓库的真实账号验收 |
| `local_agents_acceptance.py` | 三方本机验收：真实执行器加真实 Codex/Claude 总控 |
| `controller_acceptance.py` | 真实 Codex 总控经 MCP 调用 Hub，执行器递归被隔离 |
| `wait_controller_acceptance.py` | 真实总控、独立 HTTP/MCP、确定性执行器，不重启共享后台 |
| `lifecycle_acceptance.py` | 真实 CLI 的取消与超时，以及 Muse 原生续接 |
| `cursor_grokbot_acceptance.py` | Cursor CLI 与 Grok Bot 的真实执行器验收 |
| `grokbot_cancel_acceptance.py` | 固定 Bot 会话的真实取消、期限与正常关闭 |
| `muse_cancel_acceptance.py` | 复用显式提供的 Muse 专用会话，取消后在同会话续接 |
| `muse_links_acceptance.py` | 续接已验证的 Muse 会话一次，保持 request id |
| `muse_success_acceptance.py` | 通过隔离的当前代码 Hub MCP 做 Muse 实机验收（`--long` 为长消息） |
| `muse_transport_probe.py` | 只读网关探针，不输出 token、cookie 或异常内容（`--socks`/`--direct`） |
| `synthetic_collaboration_timeline.py` | 生成隔离的合成双模型协作时间线，用于预览看板 |

## 仅 Windows

| 脚本 | 用途 |
|---|---|
| `child_cancel_acceptance.py` | Hub 取消后，真实 Codex shell 子进程已退出（依赖 pywin32） |

## 仅 macOS（`macos_*`）

`macos_resilience_common.py` 是其余脚本共用的辅助模块，不单独运行。

| 脚本 | 用途 |
|---|---|
| `macos_acceptance.py` | Mac 实机验收，独立私有后台，不停止共享 Hub |
| `macos_controller_acceptance.py` | 真实 Codex 总控经 MCP 调度真实 Claude |
| `macos_scheduler_acceptance.py` | 双执行器并发与同工作区串行 |
| `macos_serial_recheck.py` | 同工作区短任务串行完成复验 |
| `macos_stress_acceptance.py` / `macos_stress_recheck.py` | 六个真实任务的并发额度、排队、幂等 / 空闲后台复验 |
| `macos_soak_acceptance.py` | 五分钟、十批双执行器任务，采样健康、RSS 与队列 |
| `macos_crash_acceptance.py` / `macos_crash_queue_acceptance.py` | 后台崩溃与恢复隔离 / 崩溃后的排队隔离与恢复 |
| `macos_daemon_pause_acceptance.py` | 仅暂停独立 Hub 进程，验证期限跨越后的恢复 |
| `macos_sleep_resume_acceptance.py` / `macos_sleep_evidence.py` | 真实休眠与唤醒验收（需用户授权，不改电源设置）/ 依据系统电源事件核对期限 |
| `macos_lifecycle_recheck.py` | 修复后的进程树复验 |
| `macos_partial_artifacts_acceptance.py` | worktree 编辑后超时，保留部分差异和日志 |
| `macos_boundary_acceptance.py` | 当前版本拒绝跨机器 Hub 地址 |
| `macos_auth_network_acceptance.py` | 隔离认证故障、局部断连与恢复 |
| `macos_claude_auth_recheck.py` / `macos_invalid_auth_http_probe.py` | 核实真实 401 与失败终态 / 无效凭据的 HTTP 拒绝探针 |
| `macos_fault_proxy.py` | 验收专用 CONNECT 转发器，不解密 TLS，不记录请求内容 |
| `macos_muse_activity_probe.py` | 只读查询验收会话关联的活动 |
| `macos_muse_browser_challenge.py` | 本机独立网页的两步交互挑战 |
| `macos_muse_resilience_acceptance.py` / `macos_muse_workflow_acceptance.py` | Muse 的认证错误、TLS 切断与恢复 / 多步网页流程与本地取消边界 |

## 仓库根目录的辅助脚本

`verify_muse.py` 和 `Authenticate-Muse.ps1` 是对外文档化的入口，留在仓库根目录，用法见 `docs/portable-helpers.md`。历史验证记录在 `docs/validation/`。
