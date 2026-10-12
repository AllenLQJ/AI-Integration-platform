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

MediaFlow 通过 context 携带关联信息，同时提交 acceptance；读取 get_work_item 或 get_result.verification。Gateway 媒体契约及原始审核回答保持不变。第一阶段只提供确定性检查与人工裁决；统一多媒体验收结果写入、审核端与质量门禁对接留到第二阶段。

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
