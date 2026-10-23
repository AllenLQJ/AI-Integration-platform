# 开发前验证结果

> 历史记录：保留原日期、命令和验证范围。当前 Windows 版本已完成第一版与等待工具验收；最新状态以 `开发进度.md` 及第一版验收记录为准，旧暂停/未完成状态不适用于当前版本。

日期：2026-10-09；设备：当前 Windows 本机。以下为本次实际探针结果，不是 Hub 成品验收。

## 结论

Codex CLI、Claude Code CLI、Muse 的真实调用已通过；Codex 和 Claude 的真实会话续接已通过。中文/空格路径 worktree 与 Windows 合成父子进程的取消及超时清理通过。可以继续细化实施计划，但必须修正 Claude 的认证假设及 Muse 的消息关联方式。

## 验证表

| 项目 | 结果 | 证据与范围 |
|---|---|---|
| Codex 安装与登录 | 通过 | 0.162.0-alpha.2，`login status` 为 ChatGPT 登录 |
| Codex 非交互调用 | 通过 | 移除环境中的 API Key/Token，忽略用户配置和规则；read-only 短任务返回指定随机标记，exit=0 |
| Codex 原生续接 | 通过 | 从 JSONL `thread.started` 提取真实 thread_id，独立进程 `exec resume` 回忆同一标记，exit=0 |
| Claude 安装 | 通过 | 2.1.284；使用 npm 包中的真实 claude.exe，不使用 PowerShell shim 或假定 cli.js 路径 |
| Claude 登录方式 | API 路由模式 | auth status 为 apiKeyHelper，用户 settings 指向 CCR 127.0.0.1:3456；终端显示 DeepSeek/API Usage Billing。不是 Claude Pro/Max 订阅验收 |
| Claude 非交互与续接 | 通过 | 禁用内置工具、严格空 MCP 配置，JSON 返回 session_id；第二个进程 --resume 正确回忆标记，两次 exit=0 |
| Muse MCP 状态 | 通过 | 本机 stdio 握手，6 个工具；ok=true、authenticated=true |
| Muse 独立会话 | 通过 | 本次探针临时设置 MUSE_MCP_ENABLE_RAW=1，以 session.start 创建专用测试会话；没有更改全局 MCP 配置 |
| Muse 收发与关联 | 通过且有限制 | 新会话基线 seq=0；用户消息 seq=5952、助手回复 seq=5953，回复匹配随机标记。reply_to_message_id 为空，不能依赖此字段做强关联 |
| Git worktree | 通过 | 临时中文/空格路径仓库，创建独立分支与 worktree；修改仅发生在 worktree，原目录 status 为空且基线文件不变 |
| Windows 取消清理 | 通过（合成进程） | 仅终止探针创建的父进程树；taskkill /T /F 后父子 PID 均不存在 |
| Windows 超时清理 | 通过（合成进程） | 等待超时后终止父进程树；父子 PID 均不存在 |

结构化结果：`docs/probes/results/codex.json`、`claude.json`、`muse.json`、`local.json`。临时探针脚本位于 `docs/probes/`，不是产品代码。

## Claude 故障调查与恢复

截图错误为 ECONNREFUSED。检查发现 Claude 指向的 3456 无监听，但普通网络代理 127.0.0.1:PORT 可连接。3456 是模型路由 API，PORT 是外网代理，两者不能互换。

CCR 管理服务在 3458 运行，管理状态与实际网关状态不一致。请求启动网关时报告 `Core gateway did not accept runtime config within 5000ms.`。本次通过 CCR 官方 stop/start 重启管理服务，启动过程指定已有 Node 路径、回环地址绕过代理；首次网关配置接受仍超时，随后显式 startGateway 成功。

最终证据：3456 TCP 可连接，/health 返回 running，Claude 两轮真实模型请求均成功。没有手动改写模型、账号或 API 地址；CCR 启动会生成其正常运行文件并应用已有配置。当前已恢复，不据此保证重启或冷启动不再出现 5 秒配置超时。冷启动超时的深层原因尚未确定，不能归因于防火墙或供应商故障。

## 对设计的影响

1. Claude 适配器应支持明确的“现有路由/API 模式”；不能把当前账号当作订阅复用证据。订阅模式必须以后独立验收。
2. Codex、Claude 均已证明可保存并复用真实会话 ID；Hub 应持久化 ID，不只使用摘要重建。
3. Muse 支持独立会话，应每任务分配独立会话并在该会话串行追问。以会话 ID、发送前序号和任务标记组合关联；接受确认不能等同于长任务完成。
4. Muse 的 raw 路由可用于创建会话，但只在 Hub 内部白名单调用，不把通用 raw 操作暴露给总控。
5. Windows 的进程树终止方案已通过合成测试；仍需在实现阶段验收真实 CLI 长任务及工具子进程，不能把合成结果直接当成最终验收。

## 未覆盖及开发验收要求

- Mac 实际设备、DeepSeek Harness 总控/执行器：本次未验证。
- Claude Pro/Max OAuth 登录复用：当前为 CCR/API 模式，未验证。
- Muse 远端取消及长任务最终结果的结构化完成语义：短回复未证明；路由存在 chat.cancel 不代表参数和语义已确认，不能贸然调用取消其他会话。
- 后台服务断线续跑、SQLite 幂等和服务重启恢复：Hub 尚未实现，本次没有假称这些功能已通过。
- 真实 CLI 编辑任务、权限边界、工具子进程取消：属于实施期验收。本次 CLI 请求禁用/不使用工具，worktree 修改由探针完成。

## 探针使用说明

本次短请求会消耗既有账号额度。探针不读取或输出真实凭证，不将 Cookie、认证头或配置内容写入仓库。测试 worktree 在临时仓库中提交测试变化后移除；现有项目未被当作测试仓库，现有 Claude/Codex 会话未被终止。

为排查故障启动的 CCR 管理与网关服务保持运行，以供现有 Claude 终端重试。Muse 专用验证会话保留用于追溯。
