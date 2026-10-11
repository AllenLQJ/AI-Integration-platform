# Cursor 与 Grok Bot 双向接入设计

日期：2026-10-11。起点为 main 的 9d4b5c4，工作区干净。用户选择两者既可作为总控调用 Hub，也可作为被 Hub 调度的执行器；账号尚未登录。检测到 Grok Bot.app 0.68.1，Cursor CLI 尚未安装。

## 接入方向

| 方向 | 实现路径 | 验收要求 |
|---|---|---|
| Cursor → Hub | 用户 MCP 配置中的本机 stdio 服务 | 八工具发现、实际派发和结果回收 |
| Hub → Cursor | 官方 agent CLI 的 print JSON、明确模式、原生 resume | 真实文件探针、续接、取消/期限、权限与空 MCP 隔离 |
| Grok Bot → Hub | 已配对 Mac 上经用户批准执行 Hub 的 rpc 命令 | 从云端实际调用本机命令并回收真实任务；不开放公网服务 |
| Hub → Grok Bot | Cursor BDK 0.2.23 公布的 Grok Bot session API | 专用 Bot、发送确认、序号和本任务标记、读取完成；不将派发确认当作最终结果 |

Cursor 只读使用官方 ask 模式，编辑仅在 Hub worktree 中进行。每任务权限与 MCP 配置隔离，拒绝 shell、MCP 及外部写入；不关闭系统沙箱。官方 CLI 不同版本的配置和权限行为必须实测，未验证前不宣称只读或全功能可用。

Grok Bot 只提供 delegate，不承诺云端 Bot 只读，不把本机 cwd 当成已同步的云端目录。使用专用 Bot，沿用其会话与记忆；发送前拒绝已有在途回合。只读请求失败可以有限重试，会话创建/消息发送不自动重发；不确定发送或读取失败保留 delivery_unknown。取消、任务超时及正常服务关闭会尝试远端中断，只有独立核验停止后才声明 remote_cancelled=true。不能从 idle 状态或未关联的旧回复推断本任务成功。

总控侧与执行器侧分开配置，避免执行器继承 Hub MCP 后递归调度。Grok Bot 本机命令仍由客户端审批，不更改永久执行权限。登录、订阅和额度由用户管理；不购买套餐，不将凭据写进仓库或聊天。

## 来源与限制

- [Cursor CLI 参数](https://cursor.com/docs/cli/reference/parameters)、[输出契约](https://cursor.com/docs/cli/reference/output-format)、[权限](https://cursor.com/docs/cli/reference/permissions)、[配置](https://cursor.com/docs/cli/reference/configuration)。
- [Cursor BDK 官方包](https://www.npmjs.com/package/@cursor/bdk)：0.2.23 的 `docs/guides/grokbot-agents.md` 和 `dist/extensions/cursor-grokbot-agents/lib/backend.js` 公布 `/v0/grokbot` 的四个 session 路由，主机为 api.cursor.com。鉴权来自 Cursor 用户 API key 或 bdk login 私有凭据。此接口随版本变化，当前只是实现依据，账号访问和返回契约须真实验证。
- [Grok Bot 本机执行设置](https://cursor.com/docs/grok-bot/settings)：本机执行默认逐命令审批。云端 stdio 不能直接读取 Mac 路径；当前 Hub 不开放公网监听。

先完成安装、实现和隔离测试，再由用户完成 Cursor/BDK/Grok Bot 登录及必要的本机命令批准，最后做真实双向任务验收。没有登录或真实证据时保留“待验收”，不覆盖现有三个已验收执行器的状态。

## 追加需求：Grok Bot 远端取消

用户于 2026-10-11 要求增加 Grok Bot 远端取消，同时安装 Cursor IDE。Muse 本地取消语义保持不变。

- `cancel_task` 首次响应只说明取消已受理，scope 为 `remote_session`，不提前报告停止成功；终态结果提供 `interrupt_requested`、`interrupt_acknowledged` 与 `remote_cancelled`。
- 只中断本适配器明确接收、私有状态锁仍归本任务、会话 ID 匹配的专用 Bot。取消前从任务基线核对提交原文；检测另一用户消息或排队回合时拒绝会话中断。未确认派发不自动中断或重发。
- 远端 interrupt 单次 POST，不自动重试。成功响应仅为请求确认，随后需四次无新增条目、idle=true、inFlight=false、queued=0 才报告远端停止；若已回收到本任务完成标记并稳定 idle，标为 already_idle，不伪称已发送中断；仅消息可见和启动前 idle 不能视为自然结束。
- 任务期限届满后仍给清理独立最多 12 秒；正常网络请求最多 10 秒。取消或清理异常保留状态文件与未确认结论，后续任务不能绕过锁。确认停止后清锁，后续任务可继续；再次取消旧终态任务不能发中断。
- 正常后台退出走同一中断与确认路径。强制杀进程、断电或 HTTP 不确定不保证中断成功，重启仍保留既有崩溃隔离规则。
- API 按整个 Bot 会话中断，不支持按 messageId 原子条件取消。专用 Bot 禁止人工或其他客户端并发投递；无法完全消除外部消息在核验和 POST 之间到达的竞态。确认的是远端回合停止，已发生的文件修改、外部动作和已启动的独立进程不会自动回滚。

自动测试覆盖请求与确认区别、未确认派发、外来消息/锁变化、请求失败不重试、自然结束以及服务 scope。独立真实验收使用新命名临时 Bot，验证用户取消、期限、正常关闭及取消后新任务，不触及原有 Bot。

- 用户追加同一 Bot 复用要求：验收必须绑定已核对的既有 session_id，不每场景新建。固定 ID 模式跳过按名称 get-or-create；所有取消目标必须与原任务 session 相等，并在报告保存两者及 same_session。默认状态锁解析后以 session_id 为键，不同名称别名不能并发绕过锁；旧名称锁存在时拒绝启动并提示人工核对。
