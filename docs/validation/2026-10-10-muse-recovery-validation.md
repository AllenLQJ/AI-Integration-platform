# Muse 错误恢复修改与验证（2026-10-10）

> 2026-10-11 更新：本文保留 Windows 恢复阶段证据；当前 Muse 模式为 delegate，最新 Mac 认证及真实断线恢复见 [补充验收](2026-10-11-macos-resilience-validation.md)，当前状态见 [开发进度](../开发进度.md)。

> 分阶段历史验收记录：前文失败、未提交和旧共享进程描述均为当时状态。后续真实成功回收、共享切换及本地 Git 保存结果见本文后半部分；最新等待工具和后台重载见 `2026-10-10-wait-for-task-validation.md`。本轮新增可复现配套补丁见 `../vendor/muse-mcp/README.md`。

工作树：`%USERPROFILE%\.codex\worktrees\agent-hub-v1\AI-Integration-platform`，分支 `feature/agent-hub-v1`，修改前基线 `c7ba64a`。本次修改保留在工作区，未提交或推送。

## 修改

- MCP 外层 isError=false、内层 ok=false 仍作为错误处理，保留上游码、阶段，并脱敏诊断文本。
- SDK 1.30.0 的 McpError 408 归一化 TIMEOUT；其他 MCP 错误不可自动重试，原始 rpc_code 保留。
- 仅历史读取发生 GATEWAY_ERROR、TIMEOUT、VM_UNAVAILABLE 时最多尝试三次，退避 1、2 秒，受取消和任务期限约束，写入 retry 事件。
- 异常历史格式不能作为空历史继续发送；认证和格式错误不重试。
- 会话创建、任务发送不自动重试。发送确认失败保留 DELIVERY_UNKNOWN；发送后读取失败耗尽保留 REMOTE_STATE_UNKNOWN。
- Worker 和父进程保留错误阶段与上游码；完成证据增加 reply_to_message_id。回复消失或读取发生重试时重新积累两次稳定读取。

## 自动测试

使用项目现有 `.venv\Scripts\python.exe`：

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

最终全量结果：47 项测试全部通过，95.467 秒，退出码 0。新增 17 项测试覆盖真实适配器控制流（MCP transport/session 使用受控替身），包括真实 SDK McpError 类型、非空关联回复回收、失败恢复、重试耗尽、禁止重发、取消、期限、诊断跨进程传递、异常格式及脱敏。依赖检查无破损；diff 检查通过，Git 有 LF/CRLF 自动转换提示。

最初默认 Python 缺少 MCP 依赖，随后切换现有虚拟环境；新增错误识别、SDK 类型和诊断传递测试均在修复前实际失败，修复后通过。未安装依赖。

## 真实 Muse 验收及限制

- 任务 ID：`a45475ef0a6d49e383defeaf77753fd9`。
- request_id：`muse-recovery-fixed-live-20261010-01a1235c-v1`。
- 目录：`D:\path\to\AI-Integration-platform`；模式：read_only。
- 会话：`2e1bf27a-038f-433a-bf8f-4e9b018be761`。
- 事件明确记录 baseline_history 两次 GATEWAY_ERROR 重试，随后恢复并进入发送。
- 发送确认超时，Hub 最终 ready=true、state=delivery_unknown，error_code=DELIVERY_UNKNOWN，错误文本保留 TIMEOUT；没有重发。
- 后续实际读取会话确认用户消息 seq=6245，最终回复 seq=6246 包含本任务完成标记，reply_to_message_id 为空。远端已经回复不能改变当时发送确认不确定的事实，未手动改写 Hub 终态。
- 本任务 Worker 启动于修改过程中：它验证了当时的内层错误识别与历史重试；最终补充的 SDK 408 和跨进程诊断修复由最终自动测试验证，未再次启动真实任务。

结论：代码修改与项目全量自动测试完成；真实连接的成功回收验收本次未通过，仍受 Muse 网关稳定性影响。没有获得新的非空 reply_to_message_id 真实任务，因此非空关联的完整适配器流程由受控自动测试覆盖，不能声称真实端到端非空字段已验收。

独立只读审查发现的 SDK 超时类型和错误诊断丢失已修正。尚未专门测量退避的精确时长；取消与期限测试覆盖停止重试，但不代表远端任务可取消。

## 后续真实成功回收验收（同日）

真实任务揭示一个协议处理回归：muse-mcp.send 在确认派发后短等待到期，会返回 `sent=true, ok=false, error.code=TIMEOUT`。这属于已派发后的回复等待到期，不能判定为发送失败。已限定仅此组合继续历史回收；无确认的超时和其他错误仍保留 DELIVERY_UNKNOWN，绝不重发。新增用例修复前实际失败、修复后通过，独立审查确认与上游 send 实现一致。

最终再次运行全量自动测试：48 项全部通过，52.135 秒，退出码 0；pip check 和 git diff --check 通过。此前 47 项结果为前一版本的历史记录。

新增 `scripts/muse_success_acceptance.py`：使用当前项目 Python、实际 MCP stdio、隔离 Hub 守护进程及真实 Muse，通过 list_agents → submit_task → get_task/get_result/get_events 验收。每轮使用独立 request_id；成功必须满足 succeeded、验收词、专属完成标记、两次稳定读取、会话与序号一致，以及新关联字段存在。终态后仅关闭自己的隔离实例，共享守护进程未重启。

| 任务 ID | 运行范围 | 终态 | 直接证据 |
|---|---|---|---|
| e45bffd5999d48eb84fe5e596b204213 | 共享 Hub，协议修正前 | delivery_unknown | 实际读取到 Muse 最终回复 seq=6256，但回复等待 TIMEOUT 被误判；同时确认共享父进程仍加载旧版本，最终结果丢失新阶段字段 |
| 66641c4346a04c408761cd395213e0cc | 隔离 Hub，协议修正后 | failed | session.start / GATEWAY_ERROR，未发送任务 |
| 048aa387f0394446903a0a3544955fcf | 隔离 Hub，协议修正后 | interrupted | 已记录 accepted；reply_history 三次网关错误，最终 REMOTE_STATE_UNKNOWN，保留 stage 和 upstream_code |
| b13c5b18680b4603a8b7179ea13bfbc0 | 隔离 Hub，协议修正后 | failed | baseline_history 三次网关错误，发送前结束 |

三个隔离实例的原始验收摘要保存在用户私有数据目录 `.agent-hub/acceptance/muse-success-<运行编号>/acceptance-summary.json`。分别对应运行编号 `0320fdeda69d40d4b76deab7f7302272`、`e69a2e7a9ff44e669e8768b7e3885086`、`b00414b6c2304e96842ec3407184ce26`。

本轮结论：实际进行了成功回收验收，但未通过。协议误判已修正并通过自动回归；连续 Muse 网关错误仍阻止当前代码的真实成功回收。没有手动修改任务状态，没有将原任务重新发送，也没有提交或推送。共享 Hub 仍为旧父进程版本，后续切换需先确认共享实例无活动任务；本轮隔离实例已避免该版本问题。

## 再次调整与测试（同日）

发送工具改为 `wait_seconds=0, fire_and_forget=true`：只取得一次派发确认，回复统一由 Hub 历史轮询回收，减少发送工具内重复的回复读取。保留旧短等待返回格式的兼容分支；非 TIMEOUT 业务错误即使包含 sent=true 也不会被误当接受。新增发送模式和相邻错误反例测试；发送模式用例修复前失败，修复后通过。

最新全量测试：50 项全部通过，42.044 秒，退出码 0。没有改动日常目录中 muse-mcp 的既有 cli.py 未提交修改。

调整后的两轮隔离真实任务：

- `c47942be47174f3aa5a626e3e11bffd1`：会话已创建；muse_send / GATEWAY_ERROR，终态 delivery_unknown，未重发。
- `3f055a945ee84c9bb7be48a5eb1215f4`：session.start / GATEWAY_ERROR，终态 failed，尚未创建任务会话。

增加只读 `scripts/muse_transport_probe.py`，只输出阶段、异常类型、数值码和函数名，不输出异常消息、认证材料或服务响应。必须使用 muse-mcp 自身虚拟环境（Hub 环境未安装 noise）：

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONPATH='%USERPROFILE%\.codex\worktrees\agent-hub-v1\AI-Integration-platform\src'
& 'D:\path\to\AI-Integration-platform\muse-mcp\.venv\Scripts\python.exe' -B scripts/muse_transport_probe.py
# 可选只读对照：--direct 或 --socks；不会改写代理配置。
```

传输实测：配置的 HTTP 代理探针两次成功建立连接并读取历史；直连在 fetch_session_info HTTPS 请求超时（curl 28）；模拟 Hub 精简环境和本机 SOCKS 对照均在 fetch_session_info 出现 SSLError（curl 35）。说明存在间歇 TLS/连接问题，尚未定位是本地代理上游还是远端服务。保持现有代理、TLS 校验和不重发保护，未安装依赖或修改系统网络配置。

当前结论：发送调整和最新全量自动测试完成；调整后真实成功回收仍未通过，需先解决间歇 HTTPS 握手失败。所有先前结果均保留，未将失败任务改写为 succeeded。

## 持续任务最终验收通过（同日）

上述未通过结论是前一阶段记录。当前版本已完成真实成功回收验收。

继续定位发现，每次 MCP 工具调用重新执行会话发现、认证和 WebSocket 握手，放大间歇连接失败。实际修改如下：

- 配套 Muse 仓库 `D:\path\to\AI-Integration-platform\muse-mcp\muse.py`：仅 HTTPS 认证/会话发现的 curl 35 临时 TLS 错误最多尝试三次，退避 0.5、1 秒；证书验证错误与其他错误不重试。保留 TLS 校验，不重试 Gateway 业务操作。
- 通过真实会话发现取得当前 VM ID，写入用户私有 Muse 配置的 vm_id 字段，避免每次重新发现；未写入认证材料。VM 分配改变时需刷新该字段。
- 配套 MuseClient 增加显式可选连接复用，默认关闭；Hub 只对每个任务自己的 Muse stdio 子进程启用。RLock 保证单消费者，cookie/VM 改变会更换连接；异常关闭并清空缓存，不重放原操作。进程退出关闭连接。
- 保留日常 Muse 仓库原有 cli.py 未提交修改；新增修改在独立 Muse 仓库中，迁移 Hub 时需要同时同步配套实现。

新增连接重试与复用测试均先实际失败后实现。独立审查未发现阻断缺陷；补充了证书错误、POST 参数保持、退避上限、并发单消费者、cookie 变化、mutation 中断不重放等反例。

最终自动验证：Hub 51 项全部通过（42.464 秒，退出码 0）；Muse 29 项全部通过（0.436 秒，退出码 0）。Hub pip check 无损坏依赖；Muse 虚拟环境未安装 pip，未声称对该环境完成 pip check。两仓 git diff --check 通过。

### 真实通过证据

- task_id：`7fee6536de2e49c5bb4ceed7f8076971`
- request_id：`live-success-8ddf9ecd9839404eb9d3796c3975455c`
- session_id：`27a42b4b-e420-471e-ae77-d69b1e08c080`
- 最终状态：`ready=true, state=succeeded`
- 最终正文：`MUSE_SUCCESS_8ddf9ecd9839404eb9d3796c3975455c` 与 `[HUB:7fee6536de2e49c5bb4ceed7f8076971]`
- completion_evidence：baseline=0，reply_seq=6310，stable_polls=2，会话一致。
- 本次 reply_to_message_id 为空；非空关联字段的控制流回归通过，本次不宣称取得新的非空字段真实成功样本。
- 真实事件依次为 queued、running、process、session、accepted、succeeded，没有历史读取重试事件。任务用时约 23.55 秒。
- 当前代码实际 MCP stdio 验收脚本退出码 0，输出 `LIVE_ACCEPTANCE_PASSED`。
- 原始摘要：`%USERPROFILE%\.agent-hub\acceptance\muse-success-8ddf9ecd9839404eb9d3796c3975455c\acceptance-summary.json`

通过前失败任务保留原终态：cc041142…创建会话失败；631f795e…已派发后本地超时；1e231036…发送不确定；1874c6c6…创建会话失败。未重发这些任务，后续使用各自独立验收标记；未手动改写任务状态。

验收范围为当前工作树代码、隔离 Hub 守护进程与真实 Muse 账号。隔离实例已关闭，共享旧 Hub 父进程未重启；本记录不表示共享父进程已加载全部新错误诊断逻辑，也不保证外部代理/网关始终稳定。没有提交或推送。

## 共享日常服务重启与真实验收通过（同日）

用户明确要求完成共享进程切换。重启前直接只读查询共享 tasks.db，queued/running/cancelling 为零；正常调用 shutdown，等待旧 connection.json 删除，再使用当前工作树虚拟环境启动共享服务。旧 PID=8676，新 PID=24756；模块路径为当前工作树 src/agent_hub。

通过配置的日常 agent_hub MCP 调用 list_agents → submit_task → get_result/get_events 完成验收，没有另建隔离数据目录。

- 首轮 task_id=`56c1530eda8b4b118d4ad2507b4accbf`，session.start / GATEWAY_ERROR，创建会话前失败，保留原终态。结果已保留新版 stage/upstream_code，确认新版父进程诊断逻辑实际生效。
- 通过任务 task_id=`f5e98adf229a486f9e9483c07650fbca`，request_id=`shared-restart-20261010-24756-b`。
- 最终 `ready=true, state=succeeded`；正文为 `SHARED_HUB_RESTART_20261010_24756_B` 和 `[HUB:f5e98adf229a486f9e9483c07650fbca]`。
- session_id=`986a2e5a-3d43-424a-8458-3131dd796906`；completion_evidence 会话一致、baseline=0、reply_seq=6316、stable_polls=2、reply_to_message_id 为空。
- 真实事件 seq 121–126 依次为 queued、running、process、session、accepted、succeeded；用时约 23.29 秒。
- 日常私有日志位于 `%USERPROFILE%\.agent-hub\logs\f5e98adf229a486f9e9483c07650fbca\`，任务结果与事件持久保存在共享 tasks.db。

共享服务继续运行，历史任务未清除。当前结论：共享进程已切换且日常真实回收通过；前文“共享旧进程未重启”属于切换前的历史记录。外部 Muse/代理仍出现过间歇连接失败，本次通过不表示这些外部错误已消失。此次只做进程切换、真实验收与记录更新，无代码行为变更，没有重复运行全量自动测试、提交或推送。

## 日常真实调研案例与 Git 保存

日常 Hub 提交 OWASP 多用户刷题应用调研案例，任务 `c28992506625405b9f653b59cebf4ca4` 在约 75 秒后返回 ready=true/state=succeeded。Muse 给出数据隔离、会话安全、注册滥用控制三项建议及官方链接；session_id=`3d92370d-9ca4-42ca-9975-bf0156e4792c`，baseline=0、reply_seq=6326、stable_polls=2，完成标记一致，Hub 事件没有 retry。随后实际独立读取 Muse 原会话确认最终回复存在。Muse 自述已查阅网页，但该历史接口没有展示浏览工具操作明细，未将自述视为独立验证。

用户要求本地 Git 保存后，重新执行两仓全量自动测试，并按明确文件范围暂存。配套 Muse 修复提交为 `c7040ee578db7e925f837daeeaed80bd80e9ab6c`，分支 `codex/muse-gateway-recovery`；原有 cli.py 修改保留未提交。Hub 修复保存于现有 feature/agent-hub-v1 分支。私有运行配置、cookies 和日志不进入提交；未推送。
