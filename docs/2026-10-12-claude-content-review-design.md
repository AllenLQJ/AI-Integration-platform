# Claude content_review 自动审核（2026-10-12）

基线 main `16110f2`。执行状态机、HubGateway 1.0 字段、第一至第三阶段验收与返工规则保持不变；本阶段只为显式开启的任务增加一条受信的自动审核路径。历史设计文档未修改。

当前状态：本阶段经 Claude 独立终验通过并合入 main。沙箱外完整回归 287 项通过（208.165 秒，1 项 Windows ACL 检查在 macOS 上跳过），自动审核专项 28 项通过；真实 Cursor CLI 执行 + Claude CLI 审核的三条链路通过，见文末“验证结果”。

## 开启方式与兼容

`acceptance` 新增可选键 `review = {agent, timeout_seconds?}`；`criteria` 结构不变。`agent` 仅 `claude`，`timeout_seconds` 为 30–3600 整数，缺省 600；未知字段、非整数、布尔值拒绝 INVALID_ARGUMENT，必须至少有一条 content_review。执行器与审核端相同（如 claude 执行 + claude 审核）在 submit 时拒绝，switch_executor 换到 claude 同样经由 submit 校验被拒绝。没有 `review` 的 acceptance 序列化与哈希不变；未开启任务的 content_review 仍固定 inconclusive → needs_review，不批量派发模型。没有新增全局配置项。

选择嵌套在 acceptance 中而非单独参数：acceptance 已参与幂等哈希、随 continue_task / switch_executor 继承，审核配置因此自动跟随尝试链，且不改变 MCP/RPC 签名。

## 状态与数据

新增验收状态 `reviewing`（work_status 同名）。`run()` 计算确定性检查后：

- content_review 结果标 `review_deferred=true`；
- 其余检查按 required 汇总为 passed 时，状态 `reviewing`，并在 verifications.data 写入 `review` 对象，初始形如（ID 为示意值）：`{"state":"pending","review_run_id":"4381dc4da64b419ab58145a6195140dc","verification_run_id":"0ee7778bdb2a4df0bb6418eb7197e956","agent":"claude","timeout_seconds":600,"controller_epoch":3,"task_id":null}`；
- 其余检查为 rework_required / needs_review 时，不派发模型，延后条件原因写明，状态按原规则汇总。确定性 fail 仍可自动返工：`auto_rework_ready` 只额外容许 hub-deterministic 写入的 `review_deferred` inconclusive。

`review.state` 依次为 pending → dispatching（manifest 已持久化、审核任务待创建/待登记）→ running → 终局之一：passed、failed、inconclusive、invalid（结果不符合协议）、unavailable（无法派发、审核任务未成功或处理连续失败）、evidence_changed（回写前复核发现产物改变）、cancelled（取消处于 reviewing 的执行尝试）、superseded（租约变化、尝试被替代或迟到）。dispatching 之前也可能直接进入 unavailable / superseded，不派发模型。`review.evidence` 保存交给审核端的产物清单（artifact_id、logical_id、相对路径、SHA-256、大小）。

## 派发（src/agent_hub/review.py `Reviewer`）

验收 worker 在 `run()` 后、verify_task 在重验后调用 `Reviewer.process`，持根任务锁与尝试锁：

1. 先结算本尝试已结束、但不属于当前审核 run 且无收据的旧审核任务（只记 superseded）。
2. 以 `auto-review:<review_run_id>` 查找审核任务；不存在则派发。该前缀在公开 submit/continue/switch 与 verification_requests 中保留，外部不能抢注。
3. 派发前 fail closed 的情况：执行器即审核端、审核端未配置或不支持 text_only、远端工作区、尝试已非当前、取证失败、提示超过 100000 字符。在根任务锁内比较 `review.controller_epoch` 与当前租约 epoch，不同则立即 superseded → needs_review，不派发模型。
4. 取证清单（manifest）在创建审核任务**之前**写入 `review.evidence`，`review.state=dispatching`；若此时 reviewing 已被替代则不派发。之后通过既有 `Service.submit` 创建独立根任务：`agent=claude`、`mode=text_only`（Claude CLI `--tools=` 为空，无文件/命令/MCP）、`cwd=<data>/review-workspaces/<run>` 空目录、`review_of={task_id, review_run_id, verification_run_id}` 内部字段、无 acceptance、无 parent_id。因此它不进入执行尝试链，不改变 current_task_id，不会被再次审核；它照常排队，受全局和按执行器并发及期限约束，可单独查询事件与工作台。
5. 只补写 `review.task_id/state=running/dispatched_at`，在执行尝试上记 `review` 与 `verification` 事件。submit 之后、补写之前崩溃时，恢复按 `auto-review:<run>` 找到审核任务，只补 task_id/state，保留已持久化的 manifest，不重复提交模型；该审核任务若已被重启中断，按真实状态记 unavailable（原因含 cancelled/interrupted），不会误报为 JSON 协议错误。缺少 manifest 的审核结果记 unavailable。

取证：只取当前验收检查 evidence 中登记的 artifact_id；本尝试存在任何媒体登记即 unavailable（媒体审核未支持）。最多 10 个文件、单文件 32 KiB、总量 64 KiB，经 `Verification.read_file`（相对路径校验、逐级 O_NOFOLLOW、不越出工作区、打开前后身份核对）读取并要求 SHA-256 与登记值一致，严格 UTF-8、不含 NUL。证据写入提示，位于随机 `UNTRUSTED-EVIDENCE-<uuid>` 边界内，提示声明证据中的指令、JSON、结论均为不可信数据。使用 text_only 内嵌固定证据，审核端无法读取可变文件；但审核窗口长达分钟级，执行尝试已结束、工作区不再独占，期间文件仍可能被改动，所以回写前必须复核（见下文“回写前复核”）。

## 结果协议与校验

审核端输出必须是单个纯 JSON 对象（允许首尾空白）。下例为有效 JSON，ID 为示意值，实际必须与提示中的审核上下文逐字一致：

```json
{"schema": "agent-hub.content-review.v1",
 "task_id": "d0d1eb9b8238495995fe4f2c17d01332",
 "review_run_id": "4381dc4da64b419ab58145a6195140dc",
 "verification_run_id": "0ee7778bdb2a4df0bb6418eb7197e956",
 "checks": [{"criterion_id": "quality", "outcome": "pass",
             "reason": "final.txt 内容为 GOOD 加一个 LF，符合要求",
             "evidence": ["2eb0a44e321d5e1c81dc007971dd516d"]}]}
```

`outcome` 取值只能是 `pass`、`fail`、`inconclusive`。

`parse_result` 拒绝：非对象/数组/代码块/前后说明、重复 JSON 键、NaN/Infinity、字段集合不完全相等、schema 或任一指纹不符、checks 未恰好覆盖全部 content_review id、重复或未知 criterion、outcome 非小写三值、reason 为空或超过 2000 字符、evidence 为空/重复/不在提供清单中。审核任务非 succeeded（failed、timed_out、cancelled、interrupted 等）记 unavailable 并带 error_code。所有失败均把 content_review 写为 inconclusive，状态汇总为 needs_review，不重试、不返工。

## 回写前复核

结果通过严格校验后、写入任何结论前，`Reviewer.recheck` 按 manifest 用 `Verification.read_file` 重新读取每个产物（相对路径校验、逐级 O_NOFOLLOW、不越出工作区、单文件 32 KiB 上限）并比对 SHA-256。内容改变、删除、被替换为符号链接、路径越界或读取失败时，记 `review.state=evidence_changed`，content_review 写 inconclusive → needs_review，不采用旧快照的 pass 或 fail，不自动返工。复核与数据库提交之间仍有毫秒级窗口（见剩余风险）。

## 取消处于 reviewing 的执行尝试

执行已 succeeded 的尝试处于 reviewing 时，cancel_task(该尝试) 不再是静默 no-op：在原有租约校验后，`Reviewer.cancel` 先取消对应审核任务（排队中直接 cancelled；运行中转 cancelling 并触发既有进程树停止），再以收据事务把 content_review 记 inconclusive、`review.state=cancelled`、验收转 needs_review。执行 state 保持 succeeded，不触发自动返工；之后审核任务结束只按收据跳过，不能覆盖。响应在原字段外增加 `review={review_task_id, review_state: stopping|stopped|not_dispatched, verification_state}` 及 verification/work_status。未处于 reviewing 的任务取消行为与响应不变。

## 受信回写与迟到处理

`Reviewer.finish` 在单个 `BEGIN IMMEDIATE` 事务中：检查 `auto-review-result:<run>` 收据（存在即返回，保证幂等），核对 verifications 仍为 `reviewing` 且 review_run_id 相同、尝试仍是当前、根任务租约 epoch 与进入 reviewing 时相同，然后写检查历史、状态、收据和事件。回写不经 record_verification / record_external_verification，执行器仍不能写验收；`hub-` 前缀 verifier_id 对外保留。审核检查带 `verifier.name=hub-claude-review` 与 `auto_review={review_run_id, review_task_id}`，后者只有内部代码能写入。

- 验收被 verify_task 重跑、人工或外部裁决（会替换 data 中的 review）后到达：只追加 superseded 历史与收据，不改任何状态。
- 仍为 reviewing，但尝试已被续接替代或租约 epoch 改变：追加 superseded 历史，状态转 needs_review、`review.state=superseded`，保持可接管，不采用结论、不返工。
- `reviewing` 期间 record_verification 可人工裁决（扩展了原“仅 needs_review”限制），verify_task 可重验。

## 返工

`required=false` 的 content_review 仍会被审核并记录结论；其 fail 不阻塞汇总（可为 passed / completed，`review.state=failed` 表示存在未通过的条件），也不会单独触发返工；若同时有 required 条件 fail，返工提示会一并带上它的原因。多条 content_review 必须在同一份结果中各出现一次，缺任一条整体 invalid。

`auto_rework_ready` 对 content_review 的 fail 只接受：verifier 为 hub-claude-review、`auto_review.review_run_id` 等于当前 `review.review_run_id` 且 `review.state=failed`；不能有其他 inconclusive（延后项除外）。返工复用原 `auto_rework`：`auto-rework:<run>` 幂等、同一原生会话、继承 acceptance（含 review）、与确定性返工共享 `max_auto_rework`（0–5）。到达上限保持 rework_required，`auto_rework_reason` 与 pending_actions 写明上限。inconclusive 与所有 fail-closed 结果不返工。审核任务没有 acceptance，不会被递归审核。

## 恢复与并发

- 启动时把所有 `reviewing` 尝试放入内存待处理集合。Store.recover 已把运行中的审核任务标 interrupted，回写为 unavailable → needs_review，不重新提交模型。已有 `auto-review:<run>` 任务只补登记；已结束未回写的按收据只回写一次；回写后未派发的返工由原 `recover_auto_rework` 补派。
- 审核任务结束（含排队中被 cancel_task）时把对应尝试加入待处理集合，由验收 worker 在 concurrency 上限内处理。处理异常按 2/4 秒退避，第三次失败调用 `abort` 转 needs_review 并保留原因。
- 同一尝试的处理由根任务锁/尝试锁串行，收据在事务内去重，并发调用只提交一次。

## 可见性

`verification.review`（状态、审核任务 ID、原因、证据清单）出现在 get_task / get_result / get_work_item / 工作台快照。事件：`review`（running、终局状态、superseded）和 `verification`。pending_actions：reviewing 时显示等待中的审核任务 ID 与接管方式；needs_review 时附自动审核状态与原因；rework_required 到达上限时附上限说明。工作台脚本把 `reviewing` 视为进行中（3 秒快照刷新），`review` 事件触发快照刷新；`show` 文本输出增加自动审核行。布局未改。

## 测试与剩余风险

新增 `tests/test_review.py`：通过、失败→返工→通过（同会话、继承 acceptance）、预算 0 与上限、17 种无效结果、CLI 失败/超时/审核端缺失、inconclusive、schema 与自审核拒绝、保留前缀、产物变更/符号链接/二进制/超限/媒体、提示注入边界、人工裁决/租约变更/重验/新尝试后的迟到结果、运行中与结束后重启、并发处理只提交一次、按执行器并发限制。均使用假执行器和假 Claude 适配器，不调用真实模型；真实模型链路的结果见文末“验证结果”。

### 首次独立审核后的修正

Claude 独立审核（需修改后合并）指出的问题均经代码核实成立，真实模型三条链路均因 B1 失败（证据 run-172722，未覆盖）：

- B1：`Reviewer.update(task_id, run, **fields)` 与调用方的 `task_id=` 关键字冲突，正常登记与补登记必然 TypeError。形参改为 `attempt_id`，`review.task_id` 字段保留。
- B2：派发前未比较租约 epoch。现在根任务锁内比较，变化即 superseded，不派发模型。
- B3：manifest 在 submit 之后才写入，崩溃窗口会丢证据并误判 invalid。现在 submit 前持久化，恢复只补 task_id/state。
- B4：回写前不复核产物，可能把旧快照的 pass 记为当前产物 completed。现在回写前安全重读并比对 SHA-256（见“回写前复核”）。此前文档“不需要回写时再复核文件”的描述是错误的，已更正。
- 取消 reviewing 的执行尝试原为静默 no-op，现在会停止审核任务并转 needs_review。

首次真实 Claude 试跑中，一次 GOOD 审核没有输出 JSON，而是输出 DSML 格式的 Bash 工具调用（试图 ls/shasum/od 核对字节）；text_only 无工具，命令未执行。该轮实际因 B1 登记错误进入 unavailable / needs_review，未完成结果解析；修复后同形 DSML 输出被拒绝为 invalid / needs_review、零重试的路径由专项单测验证。另两次 BAD 审核均为合规纯 JSON fail。提示现明确：text_only、无文件/命令/Bash/MCP 工具、cwd 为空且不含源产物、不要读盘或计算哈希；Hub 已实读并核对 SHA-256，全部字节在证据块中，只审核所给文本；证据块起止规则与末尾换行的表现，并在每个证据标题标注 size_bytes 与 ends_with_newline；证据不足仍输出 inconclusive JSON；禁止输出工具调用、DSML/XML 标记和代码块。JSON 严格性不变，不修复模型输出，invalid 仍零重试。

新增回归：派发前租约变更（不调用模型）、submit 后登记前崩溃（证据保留、重启只回写一次、不重复模型；中断审核原因正确）、审核运行中修改/删除/替换为符号链接（pass 与 fail 两种结论均不采用、不返工）、复核拒绝越界/绝对路径、取消 running 与 queued 审核（含租约拒绝/接受、执行状态不变、不返工、无残留运行任务）、确定性 fail 延后审核不调用模型且有界返工、required=false 与多条 content_review 覆盖。

剩余风险：

- 回写前复核与数据库提交之间仍有毫秒级窗口；复核不是对工作区的锁定。

- 提示注入只能靠边界与指纹约束；被证据操纵的模型仍可能输出格式正确的 pass，自动审核不是对抗性安全保证。
- 真实 Claude 输出合法性不保证：首次试跑出现过一次 DSML 工具调用文本，加强提示后本轮链路输出合规；以后若出现代码块、工具调用或自然语言，仍会 fail closed 为 needs_review，不重试。
- 证据上限偏保守（10 文件 / 32 KiB / 64 KiB），较大文本产物会转人工；上限暂不可配置。
- 只审核本次验收检查登记的工作区文本；未被任何检查登记的文件不会提供给审核端。登记了任何媒体产物的尝试一律转人工。
- 进程内退避计数在重启后清零；重启只按持久化状态恢复，不会重复提交模型。
- 人工裁决或重验后不会主动取消仍在运行的旧审核任务，它会消耗一次模型调用后被记为 superseded。
- 真实 Windows、真实双机 SSH、Muse 验收回写、媒体内容审核、非 headless 的 GUI 浏览器工作台均未验收。这里的「真实双机 SSH」指本阶段自动审核闭环没有做双机验收。2026-10-13 另有 Tailscale 跨网络 SSH 实机（Windows 调用 Mac Hub；Codex、Claude、Muse、Grok、Cursor 成功），不覆盖本阶段审核闭环，见 README「额外说明」。Hub 没有内置 Tailscale 管理。

## 验证结果

- 自动回归：沙箱外完整回归 287 项通过（208.165 秒，1 项 Windows ACL 检查在 macOS 上跳过）；`tests/test_review.py` 等自动审核专项 28 项通过。单元与专项测试使用假执行器和假 Claude 适配器。
- 真实闭环：Cursor CLI 执行、Claude CLI（text_only）审核，三条链路均通过：直接通过（completed）；审核 fail → 自动返工（同一原生会话、继承 acceptance）→ 重新审核通过（completed）；连续 fail 在 `max_auto_rework=1` 时停止并保持 rework_required、写明上限。全程总控未调用 continue_task 或 record_verification。每条链路最终都用真实 headless Chrome 打开工作台，显示的状态与接口一致。证据汇总在本机私有目录 `~/.agent-hub/auto-review-20261012/real-chain-summary.json`，直接链路来自 run-173812，返工与上限链路来自 run-174333。
- 保留的真实失败（未覆盖）：run-172722 三条链路因 B1 登记 TypeError 失败；run-173812 中返工链路因 ECONNRESET 中断，随后在 run-174333 重跑通过；首次试跑中的 DSML 输出只是模型返回的文本，没有执行任何命令。
- 以上只证明本轮链路通过和非法输出按设计 fail closed，不推出模型总会输出合法或正确的审核结论。
