# Muse 人工等待与原任务恢复

日期：2026-10-11，Mac 本机。针对 Cursor 已派发任务、Muse 遇到人机验证等待用户、总控仍连续轮询的现象。

## 实现

- 非终态 `waiting_for_user`，通过任务、结果和等待响应返回 intervention；等待响应立即返回 `needs_user=true`，结果仍 `ready=false`。
- Muse 派发提示约定人工等待消息标记，支持人机验证、登录和审批；最新助手消息的明确阻碍和接管请求作为保守辅助，返回检测依据。没有宣称已验证 Muse 原生结构化阻塞字段。
- 新增 `resume_task(request_id, task_id, intervention_id)`。仅在用户明确确认处理完成后使用；本地登记确认，worker 在原会话投递一次继续指令。不创建子任务，同一介入跨 request_id 去重；不确定发送不重发。
- 人工等待每任务总预算 900 秒，暂停普通执行期限，恢复后继续使用剩余执行时间；到期、取消和服务关闭复用远端取消与停止核验。服务重启后原等待任务标记 interrupted 并隔离，不自动恢复或重发。
- 已自行完成时直接收结果，不额外续发；最终回复必须位于最近一次人工等待之后。求助消息即使误带完成标记也不能报成功。
- 取消校验记录本任务原派发及恢复消息；有其他消息进入会话时仍保守拒绝整会话中断，保留锁。
- 保留现有 HubGateway 修改，兼容其 gateway_state：人工等待映射 running，精确状态保留在 state。MCP 当前 11 工具。

## 真实会话协议验收

任务 `7d7ef84d90064075badb6a078e151033`，Muse 会话 `fd8ff48a-8fe1-4661-846d-05067b2f2df9`。

真实 Muse 收到纯模拟登录等待任务；不打开浏览器、不访问账号。返回显式等待消息，Hub 进入 waiting_for_user，wait_for_task 立即返回 needs_user=true，timed_out=false。同一恢复请求重复两次，原任务最终 succeeded，原会话返回 `HUMAN_WAIT_RESUME_OK` 与匹配任务标记。completion_evidence 为两次稳定读取，reply_seq=7202。私有证据位于 `~/.agent-hub/human-wait-validation-20261011/`。

真实 stdio MCP 工具发现确认 11 工具与 resume_task。自动测试覆盖等待检测、误报边界、等待预算、幂等恢复、取消、超时、重启隔离、父进程期限延长、已自行完成和旧最终回复排除，以及恢复后的取消所有权。

## 使用与边界

Cursor → Customize → MCPs，将 agent_hub 关闭再开启，刷新工具及说明。让总控遇到 needs_user=true 后提示你到 Muse 原会话接管；你处理完成并确认后再调用 resume_task，查询原任务结果。

真实 Cloudflare 浏览器验证、用户登录和 Cursor IDE 提示交互尚未重新验收；本轮真实会话验收验证的是等待与恢复协议。旧 worker 不会热升级，已超时的旧任务不会自动复活。SSH 执行器人工等待透传未在本次实现。

最终自动回归：`.venv/bin/python -m unittest discover -s tests -q`，155 项，154 通过、1 跳过，耗时 60.795 秒；`git diff --check` 通过。完整回归后确认无在途任务并重载共享后台。工作区保留既有及同期的 HubGateway/媒体接入改动，本次没有提交或推送。
