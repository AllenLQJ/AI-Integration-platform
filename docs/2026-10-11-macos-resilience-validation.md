# Mac 补充异常与稳定性验收（本轮结束）

日期：2026-10-11。用户同意短暂休眠并手动唤醒；本轮只验证 Mac，Windows 回归排除。DeepSeek Harness、Claude 订阅模式仍排除。

本轮补充验收已结束。认证异常、连接恢复、五分钟持续任务与基本休眠恢复通过；严格的“睡眠期间到期、唤醒后处理超时”仍未验证。最后一次休眠后不再追加次数。

## 已完成

- Claude/DeepSeek：隔离无效认证凭据，真实 HTTP 返回 401。第一轮 CLI 仅 timed_out；仅在隔离复验中减少 CLI 重试后，观察到真实 401 且 Hub failed。未改变用户认证。
- Claude：专用本机 HTTP 503 服务收到六次请求，任务按期限 timed_out；健康配置下 Claude 与 Codex 后续任务均成功。
- Muse：专用无效 Cookie 副本使真实连接器返回 AUTH_REQUIRED；未接受或发送提示。
- Muse：使用不解密 TLS 的专用 CONNECT 转发器，在远端确认接受提示后切断连接，再恢复。两次读取重试后 succeeded，专用代理连接计数由 4 增至 9；原会话中仅有一条匹配该任务标记的用户消息，无自动重发。后续健康新任务成功。
- 五分钟持续验收：十批、二十个真实 Codex/Claude 任务全部成功。221 次健康采样，后台 PID 稳定；RSS 最小 23936 KiB、最大 26016 KiB、结束 24672 KiB；结束无在途任务。有限五分钟不能推导数小时或数天稳定。

- 最新完整自动回归：83 项，82 通过、1 项 Windows ACL 在 Mac 跳过，41.525 秒；uv 依赖兼容检查与 git diff --check 通过。对应私有 regression-final.log。

## 证据

私有目录：`~/.agent-hub/mac-resilience-20261011`。对应 JSON：`auth-network-summary.json`、`invalid-auth-http.json`、`claude-invalid-auth-recheck.json`、`muse-resilience-summary.json`、`soak-summary.json`。

Anthropic 兼容入口格式参考供应商官方说明：[DeepSeek Anthropic API](https://api-docs.deepseek.com/guides/anthropic_api/)。状态和通过结论来自本机实际请求，不从文档推断。

用户 Claude 设置与 Muse 认证原文件校验保持不变。隔离副本和日志在权限受限的私有目录，不纳入 Git。

## 浏览器与休眠补充证据

- Muse 随机网页交互挑战通过：本机服务器先收到 /start、带随机路径的下一步页面，再收到有效表单 POST；最终返回提示中未提供的随机标记。请求顺序、表单有效性及回答标记均核对通过。动作仍无独立 UI 日志，因此不能排除执行器改用 HTTP 工具。
- 查询了 activity.list 只读路由，但会话关联的 message_id 未匹配到目标活动。不能据此宣称取得浏览器动作审计；原路由表存在浏览器活动/任务相关接口，尚未证明这些接口可用于本任务的动作核验。
- 首次真实休眠：系统日志显示 2026-10-11 15:05:26 +0800 进入 Software Sleep，15:05:30 +0800 因蓝牙/HID 活动唤醒，实际 4 秒。Hub PID 保持，原任务随后 timed_out、排队任务 succeeded、新任务 succeeded，共享后台健康。
- 该次实际 Wake 早于原任务期限 15:05:34.903，**不算睡眠期间期限到达的证据**。脚本初始判断误用稍后的观察时间；现已改为系统 Sleep/Wake 时间核对，初始判断文件保留，修正后的 sleep-resume-summary.json 为 passed=false、basic_resume_passed=true。
- 独立后台 SIGSTOP/SIGCONT 期限跨越测试通过：暂停真实 Hub 进程至期限之后，再恢复；原任务 timed_out，排队任务成功。只证明后台暂停恢复，不替代物理休眠。
- 用户随后明确授权第二次休眠。日志：15:13:19.378 原任务已 timed_out，15:13:21 系统才进入 Software Sleep；15:13:24 DarkWake、15:14:10 Maintenance Sleep、15:14:24 完整 Wake。因此第二轮仍未覆盖“期限在真正睡眠期间到达”，但基本恢复、排队和新任务通过。不能用预计入睡时间宣称通过。
- 验收辅助逻辑补了十一项回归，覆盖早唤醒、日志秒精度、缺失 Wake、DarkWake、Wake Requests 类别及终态处理时间；均通过。系统 Sleep 日志可能在入睡通知阶段生成，必须同时核对 Hub 终态时间，不仅比较电源日志区间。
- 用户明确授权最后一次复验，已执行：15:20:15 系统记录 Software Sleep，15:20:46 DarkWake，15:20:54 完整 Wake；原期限为 15:20:16.217699。电源日志区间覆盖期限，但 Hub 在 15:20:16.417134 已处理 timed_out，15:20:16.449677 已启动排队任务，早于实际恢复。故不能证明整机暂停后才处理期限；严格 passed=false、基本恢复 true。三个观测子进程已全部退出，排队与新任务成功。
- 最后一轮初始解析把 Wake Requests 当成 Wake，已修正为完整类别栏，并保留原始判定文件。修正后仍严格未通过，因为 Hub 超时事件早于 DarkWake；不再次休眠。

对应私有 JSON：`muse-browser-challenge.json`、`muse-matched-activity.json`、`sleep-resume-summary.json`、`sleep-resume-initial-assessment.json`、`daemon-pause-summary.json`、`sleep-resume-recheck-summary.json`、`sleep-resume-final-summary.json`、`sleep-resume-final-initial-assessment.json`。

## 当前限制

- 无效凭据/无效 Cookie 的隔离探针验证了实际认证拒绝，未主动撤销真实登录或等待自然到期。
- 网络中断通过专用代理切断真实 TLS 连接；未关闭系统 Wi-Fi、修改系统代理或影响其他应用。
- 持续运行仅五分钟，压力仅二十个短任务，不表示数小时、数天或任意负载稳定。
- Windows 按用户新选择排除；当前没有运行中的 VMware Windows 虚拟机。
- 真实登录自然过期/主动撤销后的恢复尚未实测。
- 严格跨期限物理休眠恢复仍未验证；基本唤醒恢复及独立后台暂停跨期限已通过。
- 验收后台已停止，私有目录无在途任务及连接描述文件；共享后台 PID 50913 健康、无在途任务。
- 异常与稳定性补充阶段仅增加验收脚本、辅助回归与文档，未再修改产品实现；先前核心阶段的产品修复另见核心记录。验收结束时尚未提交，本次文档更新一并纳入对应已验证改动。
