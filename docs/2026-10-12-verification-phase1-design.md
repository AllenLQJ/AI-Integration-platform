# 任务验收第一阶段

执行状态机、终态保护、wait_for_task 和 HubGateway 1.0 映射不变。验收是独立生命周期，每个任务代表一次执行尝试。未提交 acceptance 的任务保持 not_requested；有条件且执行 succeeded 时进入 awaiting_verification → verifying → passed / rework_required / needs_review。其他执行终态不进入验收。

get_task、get_result 只新增 verification 和 work_status。执行未成功时 work_status 等于执行状态；成功且未请求验收仍为 succeeded，passed 映射 completed，其他验收状态直接呈现。ready 仍表示执行结果已经就绪，不表示验收通过。

## 数据与检查

所有表与 tasks 同处 tasks.db，使用 CREATE TABLE IF NOT EXISTS：

- verifications：task_id 主键、验收 state、当前检查摘要 JSON。
- verification_checks：递增 seq、task_id、检查 JSON，只追加，包括人工裁决及 superseded 标记。
- artifact_index：逻辑产物 ID、task_id、来源、Hub 计算的大小与哈希、claimed 自报值；同内容登记稳定，不覆盖历史产物。
- verification_inputs：执行前工作区和媒体目录的内容 SHA-256、文件身份及其他任务媒体归属快照；逐文件记录读取失败或超限，不以大小/mtime 判断内容改变。
- verification_requests：request_id、内容哈希、响应，用于重验与人工裁决幂等。

acceptance.criteria 限 1–100 项，id 唯一且最多 200 字符；required 默认 true。未知类型/字段、非法路径、超长描述/哈希拒绝 INVALID_ARGUMENT。完整 acceptance 参与原有任务幂等哈希，未传或 null 不写入 spec，保留旧哈希。

file_exists 检查实际任务工作区内普通、可读文件；sha256 分块读取计算 SHA-256，有 expected 时比较，无时记录。拒绝绝对路径、.. 和符号链接；POSIX 使用目录描述符逐级打开并禁止跟随符号链接。文件缺失或不匹配为 fail，IO/验证器异常为 inconclusive。路径检查会返回任务 cwd/实际工作区内文件的存在性、哈希和大小，不返回文件内容；这是有意设计的检查范围，并非任意主机文件读取接口。

Windows 分支在预检后打开文件，比较 os.stat 与 os.fstat 的 (st_dev, st_ino)，并在打开后及读取结束重新核对真实路径、文件身份与大小/mtime。不一致为 inconclusive，不登记证据。仍有 Windows 重解析点反复切换、部分文件系统身份信息不足及外部写入恢复时间戳的残余竞态风险；第一阶段不是 OS 安全沙箱，尚未在真实 Windows 运行本次补丁。

artifact_required 只计执行前不存在或内容 SHA-256 改变的文件，证据标注 created / changed，改名/硬链接保持同一文件身份且内容不变也不算 created；旧格式快照不能证明来源时转 inconclusive。声明为 file_exists/sha256（包括 required=false）也不能绕过快照比较，但这两类检查自身不要求文件本次产生。媒体同样比较执行前媒体目录快照；已被其他任务登记的同一路径或同一文件身份（含硬链接）不计为本次产物，即使内容改变。Gateway 登记与对外契约不变。artifact_required 的快照与读取每文件上限 64 MiB，超限/基线哈希未知时为 inconclusive，不产生 pass；file_exists/sha256 自身分块读取文件，同样受单次验收总字节与时间预算保护。忽略 .git 和嵌在工作区中的 Hub 运行数据；成功自述、正文链接及自报大小/哈希不构成证据。索引仅暴露 artifact://ID、媒体原 ID 或工作区相对路径、实际哈希/大小；不保存传输 URL。content_review 无审核端，固定 inconclusive。

任何 required fail → rework_required；否则存在 required inconclusive → needs_review；否则 passed。每条结果包含 criterion_id、outcome、可操作 reason、evidence、verifier 名称/版本、checked_at、verification_run_id。

## 幂等、迟到与恢复

verify_task 使用独立 request_id 收据，同键返回原响应，不重复检查；不同内容为 IDEMPOTENCY_CONFLICT；operation 抛错且未提交响应时删除占位，失败键可改内容重试。仅 succeeded 且有 acceptance 的任务可重验，否则 INVALID_STATE。重验不改变执行状态。

验收由独立 worker 执行，不占用 schedule 线程；验收 worker 数以配置的 concurrency 为上限。文件 IO 不持 Service.lock 或全局验收锁；每任务锁串行化同一尝试的重验、裁决和续接，不阻塞其他工作项。落盘事务核对 state=verifying 和本次 verification_run_id，失效结果不写入状态/检查历史。读取 get_task/get_result/get_work_item 只 SELECT；验收行缺失时只读推导摘要，启动/结束接线和后台 pending 发现缺项、run 负责补齐。

run 的状态切换/落盘异常退回 awaiting_verification，记录 verification_retry，并按 0.2/0.4 秒退避重跑；累计第三次失败转 needs_review，写入 inconclusive 原因和历史。恢复路径不调用可能故障的 save。pending 发现异常退避 1 秒；pending/worker 异常被隔离，执行调度继续；finish 后确保 active 槽在 finally 释放，pending 补齐成功但未入队的任务。若数据库自身不可写，恢复记录也无法立即持久化，应先恢复数据库可用性；执行任务不会因此重发。

record_verification 仅在本机令牌认证 RPC 开放，不注册 MCP，也不开放 SSH 转发。当前尝试只有 needs_review 可裁决；指定 criterion_id 时改对应项，否则裁决所有 inconclusive 项，再按 required 重新汇总。收据与检查/状态在同一事务持久化。旧尝试迟到回调只追加 superseded=true 的历史和收据，不改变任何尝试的状态。

continue_task 创建新尝试并继承 acceptance，attempt 按 parent_id 链深度 +1。get_work_item 从任意尝试找根，列出全部后代尝试；带 acceptance 的工作项只允许从线性链末端续接；旧父尝试使用新 request_id 续接报 ATTEMPT_SUPERSEDED，details 返回 current_task_id，同键重放仍返回原子任务。current 为线性链末端；无 acceptance 的历史续接/分支行为保持不变，其汇总仍选最新尝试。交接包附执行状态、原生 session_id、工作区、检查历史、产物、最近 20 条事件、work_status 和 pending_actions，作为接管交接包。show 子命令渲染该包；SSH 仅增加只读 get_work_item，verify_task 与 record_verification 不转发。

重启时 verifying 退回 awaiting_verification；确定性检查可重跑并保留历史。启动补齐成功任务的验收入队，覆盖 finish 后尚未入队就退出的窗口。执行恢复仍使用旧 interrupted 与工作区隔离规则，不自动重发执行任务。

## 多媒体与第二阶段

MediaFlow 通过 context 携带关联信息，同时提交 acceptance；读取 get_work_item 或 get_result.verification。Gateway 媒体契约及原始审核回答保持不变。第一阶段只提供确定性检查与人工裁决。第二阶段已实现外部结果写入和人工裁决约束，见下文「第二阶段实现」；审核模型不会自动派发，Muse 仍未接通。

第二阶段以“工作项 = 根任务 + 尝试链”管理持续会话和总控接管。引入 controller_id 租约与递增 epoch，每次控制指令校验 epoch，拒绝失效总控。更换执行器创建新尝试，生成交接摘要（目标、约束、检查失败原因、产物和事件），不冒充同一原生会话；原生 session_id 仅在兼容执行器内续接。

执行过程可视化计划：Claude 改用 stream-json 等结构化流，统一映射进度、工具调用、会话、人工等待、产物与验收事件；通过事件 seq 续接并按尝试展示时间线。需要先校验各执行器事件语义和脱敏，不把模型进度自述当验收证据。

### 第三轮复审核实与补强

验收 worker 关机共用最多 20 秒的 join 预算；目录逐项遍历、检查循环和每个 1 MiB 读取块检查停止标志。执行前快照还检查任务取消。停止后不写入检查历史或 passed 等终局结论，verifying 回退 awaiting_verification，重启重跑；即使底层文件系统调用迟迟不返回，也不无限阻塞 close，旧 worker 返回后不得提交结论。

只有有 acceptance 且包含 artifact_required 的任务做执行前基线快照；无 acceptance 完全不做快照。单文件上限 64 MiB，单次扫描最多 1000 个读取文件、256 MiB 实读字节、5000 个目录条目和 10 秒，均可通过 Verification 实例属性调整。预算耗尽或任一基线文件不可核对使基线不完整，artifact_required 返回 inconclusive，不能接受其他文件作为通过证据。快照完成后才启动验收任务的执行期限，快照耗时不消耗执行超时。已登记媒体利用 Hub 登记的实际 SHA-256 与文件身份建立基线，只读取未登记媒体。执行后的哈希一次读取直接用于产物索引，不再次读取同一产物；变化判断始终基于内容哈希，而非 size/mtime。

新路径的内容哈希若等于工作区或媒体基线任一既有文件，标记 copied，不计为本次产物证据；没有其他可接受产物时 artifact_required 为 fail。索引 change 为 created/changed/copied/reused，交接包与 show 展示该标记。接受媒体证据前重新查询其他任务登记，排除哈希期间已被其他任务登记的文件；最终归属查询与验收提交之间仍有窄并发窗口，这是第一阶段已知残余风险。

成功执行但验收行缺失或仍为 not_requested 时，对外推导 awaiting_verification；自动验收先补齐状态再检查。验收候选由提交、执行结束、重试及启动恢复维护内存集合，空集合轮询不读库；只按候选任务主键读取，并为验收状态、历史和产物查询建立索引。每任务/请求锁以弱引用保存，持锁与等待者保留强引用，空闲后回收，避免锁注册表无限增长。

### 合并前局部修正

已登记媒体逐行读取实际哈希和身份，不占 max_files 额度，不设置累计登记行数上限；通过 checkpoint 检查停止和 max_seconds 时间预算。读取未登记媒体和工作区文件仍受全部扫描预算保护。执行派发异常写入 daemon 日志并退避 0.2 秒重试；尚未启动的 worker 回收 active 槽位，并恢复其 queued 状态。停机打断执行前快照时保留在途执行状态，不写 cancelled 或结果文件；重启后 Store.recover 按原规则标 interrupted 并隔离工作区。用户取消则仍为 cancelled。

数据目录 config.json 的 verification 对象可配置 max_files、max_bytes、max_entries、max_seconds、join_seconds，缺省分别为 1000、268435456、5000、10、20。全部字段仅接受整数（拒绝布尔值、字符串、浮点数和未知字段）；范围依次为 1–1000000、1–1099511627776 字节、1–10000000、1–3600 秒、1–300 秒，错误返回 INVALID_CONFIG。join_seconds 是验收 worker 共用的关机等待预算，不改变执行 worker 的等待规则。

copied 规则也会把合法重新生成的相同内容判为非证据，这是第一阶段的保守限制；第二阶段通过显式期望产物哈希支持可复现任务。无 artifact_required 的媒体索引 created 标记仅描述提交附件的新路径分类，不是验收通过证据。

## 第二阶段实现（2026-10-12）

本阶段仍使用 Store 执行状态、verification 生命周期、verification_requests 收据与 events 表；没有引入第二套执行状态机。无 acceptance 的任务不做自动验收或返工；没有 controller 租约的任务继续接受原操作。HubGateway 1.0 字段与执行终态保护保留。

### 统一过程与本机工作台

本机 `claude --help` 和 Cursor `agent --help` 均列出 stream-json，Claude 使用 `--output-format stream-json --verbose`，Cursor CLI 使用 `--output-format stream-json`，Codex 保持 `--json`。没有发起模型调用，本轮没有验证各版本的真实在线流。结构化记录存在才映射 session、progress（初始化、turn/result 结束）、message、tool 与真实 permission_denials 的 approval；Codex tool 记录工具名和结果状态，Claude tool_result 用 tool_use_id 关联名称。message/progress 最多 1000 字，tool 不存输入、完整文件内容或完整输出。stdout/stderr 保留最终回复用于解析，工具输入和输出从日志删除；既有脱敏继续应用，并移除 URL。权限参数、工具白名单没有放宽。没有观测到的信息标为缺失，进度和模型自述不成为验收证据。Hub 实际登记产物与检查落盘另写 artifact、verification 事件。

当前核实的三个 CLI 均支持流式格式；无法据此声称在线工具/审批事件已经实测。Cursor IDE、Muse、Grok Bot 不因本轮新增虚构的原生过程事件，继续使用其现有桥接/轮询事件。

回环服务增加 `POST /work/<task_id>/session`，只接受原 Bearer 认证，签发独立随机 HttpOnly cookie（Path=/work、SameSite=Strict）；令牌和 cookie 不进入 URL、日志或 HTML。`GET /work/<task_id>` 接受 Bearer 或 cookie，服务端按尝试展示状态、已有消息/工具/审批事件、条件、检查结论、原因及产物逻辑 ID/哈希。内容先脱敏再 HTML 转义。`GET /work/<task_id>/events?after_seq=N` 同样认证，只查询该尝试 seq>N 的事件，最多等待 10 秒、每批最多 200 条。页面用 cookie 和游标续接，验收事件更新右侧结论；新尝试 session 事件触发一次页面重载。没有整库定时轮询，不嵌执行器原生窗口，show 仍为文本版。拒绝 Origin 与跨站请求；cookie 不可用于 /rpc 或 /shutdown。

### 总控接管、执行器交接与返工

controllers 表按根任务持久化 controller_id、epoch。claim_controller(request_id, task_id, controller_id) 接管任何执行/验收阶段，成功接管 epoch+1，request_id 重放返回原 epoch（不恢复旧租约）。controller_id 非空且最多 200 字符。带租约的 continue_task、cancel_task、verify_task、record_verification、switch_executor 在根任务锁下核对 controller_id/controller_epoch；缺失或不符为 CONTROLLER_STALE，拒绝前不改状态。MCP 与本机 RPC 开放 claim；SSH 放行 get_work_item 与幂等 claim，仍禁止验收写入及 verify_task。

switch_executor 从当前已终止且未隔离的尝试创建子尝试，继承 acceptance，但不传 resume_session；已有 edit_worktree 可继承隔离工作区/基线，从只读切到 edit_worktree 则由原 Workspaces 规则创建新的隔离工作区。远端停止未确认时沿用原禁止重发规则。提示词前附截断后的根目标、条件、失败检查、Hub 产物引用/哈希、最近事件，并明确这是新的原生会话。执行中的尝试须先取消并确认终止；接管租约本身不重发任务。同执行器原生续接保持 continue_task。

verification.max_auto_rework 缺省 2，仅接受整数 0–5。后台确定性验收及显式重验后，只有 rework_required、存在 hub-deterministic fail、所有失败均为非 content_review 且没有任何 inconclusive 才自动续接。复用 continue_task 的尝试链与原生会话，原目标保留在根任务；子提示词只含失败原因和产物引用。子任务创建时持久化 auto_rework_run_id，request_id 为 auto-rework:<verification_run_id>，同 run 不重复派发，按工作项链累计次数，重启后仍可计数。达到上限保持 rework_required，并在验收摘要/事件写明原因。没有会话或不满足既有工作区安全规则时不重发，记录无法返工原因。needs_review、验证器不可用、审核失败和外部审核结果不自动派发执行器。

### 显式哈希、审核与外部检查

copied 文件仍不算本次产物，唯一新增例外是同一路径的 sha256 条件显式 expected 与 Hub 实读哈希匹配；允许该 copied 文件满足 artifact_required，保留 copied 来源标记。旧媒体归属、快照完整性和扫描预算规则仍生效。

record_verification 保持仅本机 Bearer RPC；不进入 MCP 或 SSH。对 content_review 的 pass/fail 必须带 reason，并以 artifact_id 引用本尝试已登记 evidence。原人工裁决仍只接受当前 needs_review；迟到尝试仅追加 superseded 历史。旧人工审核测试补入实际文件证据，保留原裁决、幂等和迟到断言。未收到审核端写入时 content_review 仍为 inconclusive，不自动调用审核模型。执行器名称不能用作 verifier_id；执行器不获得本机 RPC 令牌，CLI 的 MCP/工具隔离继续生效。本机 RPC 令牌持有方是可信审核调用方，verifier_id 是审计标识，并非独立身份认证。

record_external_verification(request_id, task_id, verifier_id, checks, controller_id?, controller_epoch?) 同样仅本机 RPC。checks 每项为 criterion_id、outcome(pass/fail/inconclusive)、非空 reason、evidence（非空 artifact_id 数组）；拒绝未知/重复条件、未登记或其他尝试的产物。支持向既有条件回写尺寸、时长、格式等外部项目已做的检查，不新增 Hub 解码器或多媒体生成业务。写入检查历史并按既有 required 规则重算 passed/rework_required/needs_review；未提交的条件保留原检查或 inconclusive。收据与结果在同一事务提交；同 request_id 重放不追加，不同内容冲突；旧尝试仅记 superseded，不覆盖任何验收状态。这是可调用的本机接口，未接通 Muse 或 Windows，也没有配置/派发审核模型。

### 本轮验证范围

扩展 tests/test_verification.py：租约接管/旧 epoch 拒绝与重放/重启、交接不复用原会话、有限返工与幂等、不确定/审核不返工、copied 显式哈希、外部结果重放/迟到、审核 evidence、工作台认证/页面/事件、结构化工具元数据。保留真实 HTTP 测试；另以同一 handler 的内存请求验证响应，不能代替真实监听测试。沙箱拒绝回环 bind 或 ps 时按环境错误报告，不改旧测试规避限制。

本轮实际结果：Python 3.12 隔离虚拟环境，从本机 uv 缓存离线安装依赖；未连接共享后台，未调用真实模型。命令前均设 `AGENT_HUB_DATA_DIR=/private/tmp/phase2-isolated-default PYTHONPATH=src`。` .venv/bin/python -m unittest discover -s tests -p test_verification.py` 跑 66 项，65 项通过，真实 HTTP 工作台测试因回环 bind 的 PermissionError 报错。`.venv/bin/python -m unittest discover -s tests -v` 跑 226 项，12 个错误、1 项跳过：5 个错误来自 ps 受限（含同一测试两次子用例），5 个来自回环 bind 受限，2 个是隔离后台未启动/connection.json 缺失；无断言失败。未调整这些既有测试以规避沙箱。五项关键功能单独运行通过（handler、切换到隔离工作区、租约接管、返工上限、外部迟到/幂等）。compileall、生成页面脚本的 node --check、git diff --check 通过。本轮不构成真实模型流、真实 HTTP 监听、Muse 接通或 Windows 实测通过证明。


### 第二阶段只读评审修正

六条评审均经代码核实成立。本次修正：

- run_process 仅在 stdout 映射结构化事件；stderr 不派发任何事件，继续脱敏写日志。通用 Hub worker 的 stdout hub_event 保留；CLI 调用显式禁用 hub_event。CLI session 只来自首个 system/init 或 Codex thread.started，后续 init、非 init system 与最终 result 均不可覆盖。首个 init 缺失合法 ID 时不采用后续 ID。服务层再约束 CLI 会话写入，保持 Muse、Grok Bot、SSH 与 IDE 既有会话机制。tool_result 未提供 is_error 时记 unknown，不能默认成功。
- cookie 按精确 task_id 绑定，其他 /work/<task_id> 及 events 路径返回 401；同一工作项中的尝试事件通过已授权任务的 events?attempt_id= 接口获取，并逐次核对根任务归属，仅按对应尝试 seq 查询，避免整库轮询。
- record_external_verification 纳入与其他控制命令相同的根任务租约锁及 epoch 检查；有租约时缺失或不符参数先返回 CONTROLLER_STALE，不创建请求收据、不追加检查、不改验收状态。无租约接口用法不变。
- stdout/stderr 的 permission_denials 日志逐项只留 tool_name、tool_use_id；tool_input 与其他大字段全部删除。
- Service 启动时只把当前尝试、确定性 rework_required、没有 auto-rework:<run_id> 任务创建收据且次数未超限的任务加入内存补派集合；由已有验收 worker 按并发限制处理，不重跑检查、不生成新验收 run。派发仍走原 continue_task、工作区规则与 run_id 幂等，已有子尝试、上限和 inconclusive/审核失败不会补派。
- record_external_verification 先校验条件与 evidence 归属，再处理 superseded；非当前旧尝试即使执行 failed，也只事务追加 superseded 历史/收据，不修改旧尝试或当前尝试。只有当前尝试继续要求 succeeded。

新增针对 stderr 注入、首个 init/最终结果覆盖、permission_denials 裁剪、外部租约拒绝/收据不落盘、重启补派/不重验/不重复、失败旧尝试迟到的测试；工作台 handler 与真实 HTTP 测试均补跨任务 cookie 断言。权限测试的假 Claude executor 补上模拟 init 事件，原权限与续接断言保留。

本次评审修正后的实际验证：命令前设置 `AGENT_HUB_DATA_DIR=/private/tmp/phase2-review-isolated PYTHONPATH=src`。`.venv/bin/python -m unittest discover -s tests -p test_verification.py`：75 项，74 项通过，工作台真实 HTTP 测试因回环 bind 被拒绝报 1 个环境错误。`.venv/bin/python -m unittest discover -s tests -v`：235 项，12 个环境错误、1 项跳过，无断言失败；错误为 5 个 ps 受限、5 个回环 bind 受限及 2 个后台未启动/connection.json 缺失。另运行注入/会话、日志裁剪、外部租约、重启返工、迟到写入与工作台 handler 共 10 项定向测试全部通过。compileall、生成页面脚本 node --check、git diff --check 通过。本轮未改原 HTTP/ps 测试以迁就沙箱，未发起模型调用或访问共享后台；完整回归不能报告为全通过。

## 第三阶段实现（2026-10-12）

执行状态机、第一/二阶段验收汇总、无 acceptance 任务行为与 HubGateway 1.0 既有字段保留；传输状态独立于执行和验收，receipt 不使任务 completed，不自动生成或重发执行。

### 节点与选择

同一 tasks.db 增加 nodes 表，字段 node_id、platform(darwin/windows/linux)、host_id、capabilities(agent→mode 数组)、transport(local/ssh)。启动按当前配置刷新声明，本机节点只列本机适配器；kind=remote 的适配器声明 SSH 节点，仍 remote_connection_verified=false。可配置 node_id/platform/host_id，缺省按执行器名称和 SSH 别名构造不暴露主机名的标识；同 node_id 的兼容声明合并能力，身份冲突拒绝。list_nodes 在本机 RPC、MCP、SSH 可读，不发起探测。

submit_task 可选 node_id，省略不改变原 spec/幂等哈希/执行器路由；指定后核对 agent 与 mode，错误为 NODE_UNAVAILABLE。SSH 任务沿用原 Remote 适配器（Hub 侧 delegate，实际远端 agent/cwd/mode 仍由已有配置固定），不上传本机 cwd。当前没有生产远端 local 执行路由，测试注入的 local 接收节点不能据此执行任务。续接继承指定节点。

### 受控产物与收据

get_artifact 增加 target_host_id 与 target_node_id；跨 host_id 返回 transfer_required 和逻辑元数据，移除发送端绝对路径及 allowed_root。同主机保留原媒体 local_file 契约，并支持验收索引的 artifact://ID；媒体业务继续留在现有适配器。

transfer_artifact(request_id, artifact_id, target_node_id) 只开放本机 Bearer RPC/本机 CLI，未注册 MCP 或 SSH。目标接收端是服务内私有绑定，不接受 RPC 传入目录或任意路径。当前生产绑定只有本机接收端；双目录跨主机为测试注入，不是网络传输。SSH 节点无已验证传输，固定 NODE_UNVERIFIED，不调用 SSH，不返回成功。

artifact_transfers 保存请求键、产物 ID、目标节点、transferring/retryable/delivered/superseded、响应。接收端按请求键派生单个文件名，写入自身 received-artifacts 根目录；读取只允许已登记产物，64 MiB 上限，POSIX 逐级 O_NOFOLLOW 打开，Windows 保留文件身份复核（仍有前阶段所述重解析点残余竞态，未实机验证）。接收端对收到字节及暂存文件分别计算 SHA-256/大小，符合登记元数据后才写 artifact_receipts：artifact_id、target_node_id、sha256、size_bytes、received_at、superseded。缺文件、哈希不符、超时不记成功 receipt；超时暂存/持久字节可能存在，但发送端不得据此标 delivered。写文件和 SQLite receipt 不是跨资源原子事务；崩溃可能留下无收据文件，重试按同一文件名覆盖，不产生第二份。接收键内容冲突拒绝。

发送端请求键重放返回原响应，内容冲突 IDEMPOTENCY_CONFLICT；同键重试不生成第二个产物文件，不推进验收。根任务锁下确认当前尝试，旧尝试收据只追加历史并标 superseded，不覆盖当前产物。重启将 transferring 恢复 retryable，不把在途传输当执行成功，不自动传输或重新生成。默认单次传输确认预算 30 秒，超时可按原键重试；同步文件系统调用不能保证立即中止，但返回后仍核对期限与停止标志。

### 第二阶段修正与验证范围

自动返工持续非 HubError 异常按 2/4 秒退避，累计第三次失败记一次上限错误并移除待返工项，停止本进程热循环。计数是进程内重试保护，重启仍按原持久化验收 run/次数上限恢复；验收 run 本身的异常恢复规则保留。README 外部检查示例补 controller_id/controller_epoch，说明有租约必须使用 claim 返回的值。

新增双临时目录测试：能力拒绝、节点声明/本机选择、跨主机取回无路径、接收哈希不符无 receipt、重复请求不写第二份、重启在途可重试、缺文件与超时、未验证 SSH、旧尝试 superseded、返工故障上限。所有跨主机/Windows 身份都是同进程模拟，未接通真实 Windows、真实 SSH、模型或多媒体审核端。真实 HTTP/进程树测试保留；沙箱限制不通过修改既有测试规避。

本轮最终实跑：Python 3.12 临时虚拟环境 `/private/tmp/phase3-venv`，依赖从本机 uv 缓存离线安装；全部测试命令设置 `AGENT_HUB_DATA_DIR=/private/tmp/phase3-isolated-default PYTHONPATH=src`（单类测试另加 tests）。`python -m unittest discover -s tests -p test_verification.py`：85 项，84 项通过，1 个回环 bind PermissionError，无断言失败；`python -m unittest discover -s tests -v`：245 项，232 项通过，12 个环境错误，1 项跳过，无断言失败。12 个错误为 5 个 ps 权限错误（含同一测试两次子用例）、5 个回环 bind 权限错误、2 个隔离后台启动失败/connection.json 缺失。单独 `python -m unittest test_verification.NodeTransferTests -v`：10 项全部通过。`python -m compileall -q src tests` 与 `git diff --check` 通过。日志分别在 `/private/tmp/phase3-verification-final.log`、`/private/tmp/phase3-regression-final.log`、`/private/tmp/phase3-nodes-final.log`；这不是真实 Windows、SSH 或模型调用验收。


### 第三阶段评审修正

七项评审均核实成立。Remote 适配器（含未指定 node_id 的旧路由）、持久化 remote_machine 权限或 node transport=ssh 的任务，file_exists/sha256/artifact_required 一律 inconclusive：远端工作区不在本机验收范围。执行前跳过本机文件快照，文件登记入口拒绝本机证据，不能以本机 cwd 无关文件证明远端产物，不因远端产物在本机缺失而 fail/自动返工。required 汇总和执行状态保持不变；目标 Hub 的验收仍须在目标工作区运行，发送端尚未接通远端验收结果回写。MCP 保留 list_nodes，stdio 集成断言更新为 16 并明确包含该工具。

返工 pending/retry/failure 集合的工作线程读写与调度快照均用 Service.lock。幂等 spec 先构造和核对已有 request_id，再检查节点当前能力，同内容重放不会因节点消失而拒绝；不同内容仍 REQUEST_CONFLICT。

非 local 节点声明本机 host_id 返回 INVALID_CONFIG；get_artifact 根据节点 transport 和 host_id 决定 local_file，即使旧数据库有非法同 host_id 的 SSH 声明也不暴露路径。RPC 不接受内部 target_transport。remote 平台必须显式声明有效字符串 darwin/windows/linux；本机 platform.system() 分别映射 Darwin/Windows/Linux，非法或非字符串平台返回 INVALID_CONFIG。

artifact_receipts 主键改为 (sender_host_id, request_id)。旧单键表迁移到 legacy-unattributed 命名空间保存历史，不猜测发送方身份。received_blobs 按 SHA-256 索引实际文件，文件名由首次发送方 host_id 的哈希加内容哈希组成，暂存名纳入发送方及请求键；以后同内容的任何请求/发送方复用同一文件。接收端复核既有文件后才追加新收据；已有旧文件可核验时迁入索引。文件先落盘、数据库后提交的崩溃窗口仍可能留下无收据文件，原内容重试核对并复用文件，不额外落盘。

artifact_transfer.max_received_bytes 默认 1073741824（1 GiB），范围 1–1099511627776；max_received_files 默认 10000，范围 1–1000000，并同时限制成功收据总数。只接受整数、拒绝未知字段。目录总量计算包含旧文件及崩溃遗留文件，达上限不写成功 receipt；复用内容不新增存储，但新收据仍受数量限制。未实现自动清理或生产跨机接收连接。

新增回归覆盖 Remote/SSH 三类检查与不返工、工具声明、删除节点后的重放/内容冲突、非 local 同主机拒绝及旧声明不泄露路径、跨发送方同键/换键内容去重、配额、旧收据和文件迁移、显式平台及 Linux 映射。RemoteTests 的单机桥接 fixture 补有效 platform 声明，这是新配置契约的适配，不是规避沙箱。所有 Remote/SSH 执行仍为 mock 或本机脚本模拟，未连真实主机。

本次评审修正最终实跑（仍在沙箱）：`AGENT_HUB_DATA_DIR=/private/tmp/phase3-review-isolated PYTHONPATH=src /private/tmp/phase3-venv/bin/python -m unittest discover -s tests -p test_verification.py`：93 项，92 项通过，1 个真实回环 bind PermissionError，无断言失败。相同环境运行 `-m unittest discover -s tests -v`：253 项，240 项通过，12 个环境错误、1 项 Windows 跳过，无断言失败；错误仍为 5 个 ps 权限错误、5 个回环 bind 权限错误、2 个隔离后台启动/connection.json 缺失错误。单独 `PYTHONPATH=src:tests ... -m unittest test_integration.IntegrationTests.test_mcp_tools_over_stdio -v`：16 项工具数量与 list_nodes 名单断言已通过，随后 /health 读取隔离后台 connection.json 缺失报错，未将该集成测试写成通过。`... -m unittest test_verification.NodeTransferTests -v`：18 项全部通过，包括不依赖监听的 MCP 工具声明检查。compileall 和 git diff --check 通过。日志为 `/private/tmp/phase3-review-verification-current.log`、`/private/tmp/phase3-review-regression-current.log`、`/private/tmp/phase3-review-stdio.log`、`/private/tmp/phase3-review-focused-final.log`。没有调用真实模型、连接真实 SSH、访问共享后台或提交 Git。

以上两段回归数字是实现方在受限环境中的记录，其中的 ps、回环监听和隔离后台错误不能当成代码失败。总控随后在沙箱外、使用项目 `.venv` 与 `PYTHONPATH` 指向本工作区 `src`，运行 `.venv/bin/python -m unittest discover -s tests`：253 项，252 通过，1 项 Windows ACL 检查在 macOS 上跳过，无失败。这次复跑没有改写上面的沙箱日志。
