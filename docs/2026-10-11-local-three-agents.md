# Claude、Codex、Cursor 双向接入

2026-10-11。用户选择 Cursor 默认走 IDE 原生会话，通过界面自动化桥接；可显式指定 CLI。

## 本机路由

| 入口 | 作为总控 | 被 Hub 调用 |
|---|---|---|
| Claude Code | 用户全局 MCP 配置 agent_hub | agent=claude，既有 CLI 适配器 |
| Codex | 用户全局 MCP 配置 agent_hub | agent=codex，既有 CLI 适配器 |
| Cursor IDE | ~/.cursor/mcp.json 的 agent_hub | agent=cursor，kind=ide，原生界面桥 |
| Cursor CLI | 同一 MCP 配置可作总控 | agent=cursor_cli，既有 CLI 适配器 |

Claude/Codex 总控与执行器使用不同配置。执行器不加载 Hub MCP，避免递归派发。
IDE 的 MCP 工具仍可使用，但被委托回合应仅执行任务，不再委托自己。

本机配置示例：

```json
{"agents":{"cursor":{"kind":"ide"},"cursor_cli":{"executable":"/Users/your-name/.local/bin/agent"}}}
```

cursor 旧配置只有 executable 时仍沿用 CLI，避免静默改变其他设备。当前 Mac 明确切换 kind=ide；没有把 CLI 包装成 IDE。
IDE 支持 delegate/edit_worktree；不提供可强制的 read_only。edit_worktree 由 Hub 准备独立 Git 工作区，worker 必须在 IDE 中打开该实际路径再派发。delegate 按 IDE 自身权限执行，不保证只读。

## 界面 worker 协议

桥接需要具有电脑控制能力的外部 worker 持续在线。当前 Codex 聊天可使用 cua_repl 驱动 Cursor，Hub Python 后台自身不具备电脑控制工具。没有安装常驻桌面自动化进程，关闭该 worker 后任务只排队等待，最终按任务期限结束。`list_agents` 的 requires_ui_worker=true、unattended=false 明确此边界。

三个桥接操作仅通过已认证本机 RPC 提供，不加入普通总控 MCP 工具，MCP 仍为 11 工具：

```zsh
.venv/bin/python -m agent_hub rpc ide_next_task --arguments '{"worker_id":"cursor-native-ui"}'
.venv/bin/python -m agent_hub rpc ide_task_status --arguments '{"task_id":"实际ID"}'
.venv/bin/python -m agent_hub rpc ide_task_update --arguments '{"task_id":"实际ID","worker_id":"cursor-native-ui","status":"dispatched","session_id":"实际界面会话标识"}'
.venv/bin/python -m agent_hub rpc ide_task_update --arguments '{"task_id":"实际ID","worker_id":"cursor-native-ui","status":"succeeded","session_id":"相同界面会话标识","answer":"实际完整回答"}'
```

worker 必须使用专用窗口/会话，领取后核对 workspace、mode、resume_session。使用电脑控制工具打开工作区并检查窗口；新任务新建会话，续接任务打开原会话。首次发送前持久化 dispatched，随后发送一次任务。session_id 必须是可重新定位的真实 IDE 会话标识，不能拿任务 ID 或随机字符串代替；无法取得可靠标识时应报告 failed，不能伪造成功或声称可续接。

原生界面桥的 session_id 可以是 `ui-title:<界面实际唯一会话标题>` 这样的 UI 定位符，必须核对标题唯一、会话内容与上一回合对应；它不是厂商 API session UUID。标题变化、存在重名或不能核对原会话时必须停止，不猜测续发。本轮使用实际界面标题，验证的是同一打开的原生会话续接，未验证关闭重开后的定位。

worker 每次读取 UI 后检查任务仍运行。只在界面显示对应回合结束且已取得完整回答时上报 succeeded。接口信任已授权 worker 的观察，不独立核验模型输出。不要让被调用的 Cursor 自己上报桥接成功。

额度、登录或人工审批阻碍时，用 status=failed 和 error 记录观察到的原因；派发后失败须附原 session_id。不购买订阅、不绕过验证、不自动重复发送。

claim 不自动过期，不支持抢占。每个桥实例一次领取一轮。取消/期限到达后已领取或派发任务保留 stop_requested；迟到结果被拒绝，后续新领取被阻止。worker 需在原界面核对停止，现版本不自动清锁，也不宣称远端停止成功；人工核对后保留证据再处理私有记录。后台重启也不自动重放旧 claim。已取消/超时的原任务禁止直接续接。跨多个 Hub 数据目录共用同一个 IDE 窗口不受支持，须使用不同专用窗口或串行人工协调。

## 总控使用提示

在任意已连接 Hub 的总控中：先 list_agents，按实际能力选择执行器与模式，再 submit_task；保存 id，按 next_seq 循环 wait_for_task 至终态，最后 get_result 核对 answer。request_id 保持稳定；不确定派发不能用新键重试。

例如：“通过 agent_hub 委托 codex 读取指定文件并总结，read_only；回收真实结果。”
Cursor IDE：“通过 agent_hub 委托 cursor，delegate；由在线界面 worker 执行。”
Cursor CLI：“通过 agent_hub 委托 cursor_cli，read_only。”
遇到 Muse needs_user=true 时停止轮询，用户明确确认处理后才 resume_task。

## 本轮实测

- ~/.codex/config.toml、~/.claude.json、~/.cursor/mcp.json 的 agent_hub 均指向当前项目 Python；只读检查未复制认证信息。
- Claude 被调用：读取本机随机文件，内容匹配。
- Codex 被调用：首轮探针禁止命令，无法读取文件；修正为允许只读 cat 后新请求通过。首轮未冒充通过。
- Codex 总控 → Hub → Claude：真实提交、等待、结果回收与随机标记匹配。
- Claude 总控 → Hub → Codex：首轮子任务超时；第二轮新请求通过，真实结果与标记匹配。
- Cursor CLI：真实调用返回 usage limit 错误。
- Cursor IDE：原生新聊天显示 You've hit your usage limit，Send message 禁用；真实生成和续接不能验收。IDE MCP 连接仍需从 Customize 核对本次刷新。

私有真实证据：~/.agent-hub/local-three-roles-20261011-v1/report.json 和 v2/report.json。可复现脚本 scripts/local_agents_acceptance.py 使用新的私有目录、隔离后台，不操作共享任务；支持 --executors 和 --controllers 选择，失败记录保留。该脚本的 cursor 项专门验证 CLI，不代表 IDE。

IDE 桥本机实际任务 972c1720cda0460fac52e1d6d2ace0a8 由 Codex 界面 worker 领取；原生新聊天的提示框显示 You're out of usage，无法发送。worker 回传真实阻碍，Hub 结果为 failed / CURSOR_IDE_FAILED，execution_uncertain=false，没有派发模型回合。私有证据 ~/.agent-hub/native-ide-bridge-20261011/claimed.json、result.json。这证明本机提交、领取和失败回传链路，不证明成功回复、工作区切换或原会话续接。

完整自动回归 160 项：159 通过、1 项 Windows ACL 跳过，61.439 秒。最后模式声明调整后四项 IDE 桥专项复验通过，差异检查通过。已有进程测试产生 ResourceWarning，无测试失败。无在途任务时重载共享后台，实际 list_agents 已返回 cursor/native_ui_bridge 与 cursor_cli；原配置备份为 ~/.agent-hub/config.before-native-ide-20261011.json。本轮修改尚未提交或推送。

官方接口边界：[Cursor MCP](https://cursor.com/docs/mcp) 支持 IDE/CLI 作为客户端；[deeplink](https://cursor.com/docs/reference/deeplinks) 只预填，不自动执行；[SDK](https://cursor.com/docs/sdk/typescript) 提供独立本机/云端运行时，不能作为已接通既有 IDE 窗口的证据。

## 开通 Pro 后的真实复验

2026-10-11 用户完成会员开通后继续验收。原生设置页 CURRENT PLAN=Pro；切换新聊天到 This Mac 后恢复输入和发送。没有调整按量付费设置。

| 路径 | 本轮证据 | 结果 |
|---|---|---|
| Hub → Cursor CLI | 私有 cursor-pro-cli-20261011-v1/report.json，读取随机 probe.txt | 内容核对通过 |
| Cursor IDE 原生基本回合 | 会话 Cursor pro native readiness，返回 CURSOR_PRO_NATIVE_READY | 实际界面回答与完成状态核对通过 |
| Hub → Cursor IDE | 4e0ff1d9c3ca46329565b65fcfe6b359 | 实际 IDE 回答 IDE_MEMORY_13cacc3add9f，桥回收 succeeded |
| Hub → 同一 IDE 会话续接 | c624d6b2c76543c894b79c1387f5a32c | 提示未重给完整标记，原会话再次返回相同标记，桥回收 succeeded |
| Cursor IDE → Hub → Claude | ab385afefe7a4e5685150009ebb87341 | 真实 submit/wait/get_result；IDE 显示同一任务 ID 与 IDE_CONTROLLER_3bfcd460bda3，Hub 结果匹配 |

IDE 任务的 workspace=/Users/allen，与原生界面的 On Home 对应。当前 worker 是本聊天中的 Codex + cua_repl，不是 Hub 后台自身的常驻自动化。原生 IDE 会话标题定位符为 ui-title:Cursor pro native readiness，没有假称取得 Cursor 原生 session UUID；续接只证明这次打开的同一会话记忆保留。

首轮自拟中文记忆探针经等义英文发送，回传前核对实际完整提示与标记。后续续接和总控提示以原文英文发送。本轮未验收任意中文业务提示逐字传递，实际业务 worker 应逐字核对后再发送，不自行改写。界面工具曾发生截图/控件树不同步、失效元素和 ScreenCaptureKit 错误；重连并逐步核对实际输入与输出后完成本轮，不据此推断长期稳定性。

私有证据 ~/.agent-hub/cursor-pro-native-20261011-v1/ 包含 initial.json、initial-result.json、resume.json、resume-result.json、controller.json、controller-result.json、report.json。之前额度耗尽失败记录仍保留。最终共享 Hub 在途任务数为 0。

本轮只更新验收记录及 README，没有改执行代码，沿用前轮 160 项完整回归结果，没有将旧自动回归计作新实机验收。IDE edit_worktree、独立原生取消、后台重启后的界面恢复、关闭重开后会话定位和常驻驱动仍未验收/实现。修改尚未提交或推送 Git。
