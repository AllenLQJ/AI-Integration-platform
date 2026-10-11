# Mac 全量功能验收

日期：2026-10-11。用户范围：除 DeepSeek Harness、Claude 订阅认证模式外，核验当前版本其他功能。结论：当前已实现的本机功能完成本轮自动回归及下列实机验收；不等于所有设想能力都已实现，也不等于 Muse 内部所有浏览器动作已独立审计。

## 验证结果

| 项目 | 结果与边界 | 私有证据文件 |
|---|---|---|
| 环境、八个 MCP 工具、后台持久化 | 基础轮通过，配置与真实账号记录保留 | 见 macOS setup validation |
| Codex / Claude 实际编辑、请求幂等、原生续接 | 基础轮通过；原目录保持基线 | mac-acceptance-results.json |
| Claude text_only / read_only、Muse delegate 权限 | 基础轮真实探针通过；Muse 权限由客户端控制 | permission-mode-acceptance.json |
| Codex 总控经 MCP 调度真实 Claude | 调用 list_agents、submit_task、get_task、get_events、wait_for_task、get_result，取回 MAC_CONTROLLER_PASS | mac-full-acceptance-20261011/controller.json |
| 真实工具子进程取消 | 修复后 Codex、code-mode-host、sleep 均退出；取消约 0.88 秒 | mac-lifecycle-recheck-20261011/summary.json |
| 真实工具子进程超时 | 35 秒期限到达后 timed_out，观测子进程均退出；观察到 sleep 后到终态约 29.23 秒 | 同上 |
| 真实后台崩溃恢复 | SIGKILL 后任务 interrupted，工作区隔离；原 request_id 返回原任务，新提交和续接拒绝，不自动重发 | mac-crash-acceptance-20261011/summary.json |
| 崩溃前排队任务恢复 | 修复后，同目录排队任务隔离并 interrupted，其他目录排队任务恢复成功 | mac-crash-queue-20261011/summary.json |
| 不同工作区双执行器并发 | 两任务同时 running，均 succeeded | mac-full-acceptance-20261011/scheduler.json 的 parallel |
| 同工作区串行完成 | 短任务复验无重叠，Codex / Claude 均 succeeded | mac-serial-recheck-20261011/summary.json |
| 六任务并发排队压力 | 独立空闲后台，六个真实任务全部成功；最大并发 2，每执行器最多 1，六次 request_id 重放无重复任务 | mac-stress-recheck-20261011/stress.json |
| 超时后部分代码产物 | 真实 edit_worktree 先改 hello.txt 再 sleep；超时后保留 MAC_PARTIAL_PASS 差异及日志，原目录仍 BASELINE | mac-partial-artifacts-20261011/summary.json |
| Muse 认证、同会话续接、本机文件探针 | 基础轮通过；本机写入曾实际触发客户端一次性审批 | muse-mac-acceptance-results.json 与基础验收文档 |
| Muse 两网页资料与来源链接 | 返回 SQLite 两份官方文档链接；正文、source_urls、最终标记、会话、消息序号及两次稳定读取关联通过 | mac-full-acceptance-20261011/summary.json 的 muse_web |
| Muse 多步资料流程 | 返回 urljoin、HTTPError、urlopen timeout 结论及多个官方来源；自述两次站内链接跳转失败，改用搜索完成 | mac-full-acceptance-20261011/muse-workflow.json |
| Muse 真实本地取消 | 远端接受后取消，约 0.93 秒进入 cancelled；remote_cancelled=false，cancel_scope=local_wait_only | 同上 |
| 跨机器地址边界 | 非回环 HTTP 与非允许 HTTPS 地址拒绝 INVALID_CONNECTION；当前没有跨机器 Hub 调度接口 | mac-full-acceptance-20261011/boundaries.json |
| 核心阶段完整自动回归 | 72 项：71 通过，1 项 Windows ACL 在 Mac 跳过，41.060 秒；依赖检查及 git diff --check 通过 | mac-full-acceptance-20261011/regression-final.log |
| 修复加载到共享后台 | 确认无在途任务后重载，三个执行器健康可发现 | mac-full-acceptance-20261011/shared-reload-final.json |

后续最新完整自动回归为 83 项：82 通过、1 项 Windows ACL 在 Mac 跳过（41.525 秒），见 [补充验收](2026-10-11-macos-resilience-validation.md)；上述 72 项为核心阶段记录。

压力验收是六个短任务的有限压力测试，不代表长时间或任意负载稳定性。自动测试包含权限、恢复错误路径、日志脱敏、等待游标、工作区互斥、失败产物及幂等契约；模拟错误不替代所有供应商错误的实测。

## 本轮发现和修复

1. Mac 上 Codex 工具子进程可使用独立 session/进程组，原先仅停止根进程组，导致返回 cancelled/timed_out 后 sleep 仍在运行。增加 POSIX 父子进程追踪、基于 PID 与启动时间的核对和后代进程清理；补独立 session 取消及超时回归，真实复验通过。
2. 后台崩溃后，提交入口拒绝隔离工作区的新任务，但此前已排队的任务可能绕过入口继续启动。调度器现在再次核对实际工作区；同目录排队任务转 interrupted 并隔离，其他目录继续调度。新增回归及真实 SIGKILL/排队恢复验收通过。

产品修改涉及 src/agent_hub/processes.py 和 src/agent_hub/service.py；回归位于 tests/test_processes.py、tests/test_service.py。Mac 验收脚本位于 scripts/macos_*.py。其他工作区既有未提交修改属于此前权限模式等工作，本轮保留。

## 保留的失败记录

- mac-full-acceptance-20261011/summary.json 中首次真实取消和超时 passed=false：曾观测到 sleep 残留，不能把首次终态当作通过。
- 同目录长回答测试无并发重叠，但 Claude 超时，scheduler.json 的 serial 保留 passed=false；短任务独立复验通过。
- 第一轮六任务与其他验收共享后台，六任务均成功，但该批最多观测到 1 个 running，未证明该批达到双任务并发。保留 stress.json 的 passed=false；独立空闲后台复验达到 2 并发并通过。
- 中间一次回归遇到根进程已退出后 killpg 的 PermissionError；清理改为核对已跟踪的后代，不再对退出根 PID 的进程组无条件发信号。最终完整回归通过。

## 能力边界与排除项

- **跨机器 Hub 调度未实现。** 后台绑定 127.0.0.1，客户端限制回环 HTTP；Muse 访问已配对客户端不等于 Hub 跨机器调度。
- **Muse 远端取消未实现。** Hub 的 Muse 适配器未实现远端停止，Hub 仅停止本地等待；Muse 原始路由表包含 browser_task.stop 等接口，其适用范围尚未验证；远端可能继续执行，未额外发消息要求其停止。
- **Muse 内部浏览器动作未全部独立验证。** chat.history 仅提供用户和助手消息，无法独立审计底层点击、工具调用或本机命令。多网页资料任务的结果闭环已验证；浏览步骤仍为执行器自述，两次链接跳转明确失败。不能宣称任意复杂浏览器工作流都通过。
- **SIGKILL 不保证停止 Mac 执行器。** 实测旧进程可能继续；恢复采用工作区隔离，不按未经核对的历史 PID 杀进程。本轮仅清理核对后的验收进程。
- 后续补充验收已实测隔离无效凭据、真实 TLS 连接中断及读取恢复；未撤销真实登录或等待自然到期。详细证据与严格休眠未验证项见 [Mac 补充异常与稳定性验收](2026-10-11-macos-resilience-validation.md)。
- DeepSeek Harness、Claude 订阅认证模式按用户要求排除。

## 证据与当前状态

所有表中证据文件相对于 `~/.agent-hub`，权限数据、账号配置、Cookie、完整运行日志不进入 Git。基础记录见 `2026-10-11-macos-setup-validation.md`。测试 worktree、任务和 Muse 会话保留供审阅；独立验收后台已停止，共享后台保留运行。

Mac 验收脚本采用固定私有目录与请求键，用于本次证据重放，不宜当作每次生成新任务的通用测试入口。重新采样时应使用新的目录与请求键，避免将旧任务的幂等结果误记为新实测。

验收结束时本地源码尚未提交或推送；本次文档更新将对应源码、测试与脚本纳入版本管理。Hub 仍不会自动合并执行器任务 worktree。
