# 输出采集与调度优化验证

日期：2026-10-25。分支：`codex/reliability-dashboard-performance`。变更仍未提交。

## 本轮实现

1. 输出泵线程通过有界错误队列向主线程报告回调和日志 I/O 故障，停止子进程树并返回 `PROCESS_STREAM_ERROR`。JSON 解码错误与回调 `ValueError` 分开处理；同时收到取消请求时优先保留 `CANCELLED`。
2. 单行输入最多 4 MiB；原生执行器完整 stdout 返回最多 8 MiB，超限返回 `OUTPUT_TOO_LARGE`，不解析截断的成功结果。stderr 返回最后 64 KiB，并标记截断；磁盘保留已采集的脱敏日志，超长单行明确记录为省略。command 原始协议使用独立有界回调，避免再复制整份脱敏 stdout。
3. URL 脱敏对无 `://` 的文本快速返回，对候选 scheme 限定起点，保留原有数字/符号前缀与凭据脱敏语义。
4. 验收初始化只投影 id/state/acceptance，批量补齐记录，保留旧 verdict/evidence/retry 状态；pending 发现改为单连接分组读取。
5. 调度器按提交、取消和 worker 收尾通知唤醒，最多 1 秒兜底发现直接数据库写入；执行槽满不扫描队列，验收槽满不扫描 pending，按已知重试时间提前唤醒。旧调度测试改为等待观察到实际调度 pass，保留查询范围断言。
6. 提交、调度与续接共用规范工作区键，Windows 大小写和 `..` 别名在提交入口即被拒绝。

对于远端执行器，输出采集失败或超限仅表明本地结果不可用，保留 `interrupted`、`remote_cancelled=false` 和工作区隔离。本地停止未确认时同样隔离工作区，并保留 `local_process_stopped=false` / `wait_timeout` 证据；已有远端停止确认不会被覆盖。未放宽 command/插件的保守取消语义。

## 本机合成数据

以下是同一完全访问环境下的单次对照，不是供应商或生产负载基准：

- 500 条历史任务：原逐条 ensure 算法 502 次连接、约 2,646 ms；批量初始化 2 次连接、约 11.1 ms。
- 32,000 个 ASCII 字符：原 URL 正则约 3,187 ms；当前 safe 快速路径约 0.03 ms。
- 空闲调度 1.05 秒样本：1 次数据库连接；前一审查样本约 16 次/秒。
- 8 MiB 无换行输出：约 185 ms 内明确返回 `OUTPUT_TOO_LARGE`，Python tracemalloc 峰值约 9.96 MiB。该值不包含原生库及子进程内存。

原算法对照调用保留的逐条 ensure 方法，并复刻原先正则；完整数据位于 `.tools/rv/hardening-comparable-metrics.json` 与 `hardening-metrics.json`。

## 验证进度

- 输出/调度专项 17 项通过，覆盖真实子进程、回调/日志故障、超长行、stdout 截断拒绝、stderr 尾部、协议独立捕获、远端不确定状态、取消优先级、历史初始化与路径别名。
- 自查取消优先级修正前的完整 Python 套件：504 项，无失败，5 跳过；耗时 340.723 秒。
- Cursor 反馈修正前的完整 Python 套件：505 项，500 通过、5 跳过，0 失败/错误；耗时 371.716 秒，退出码 0，日志 `.tools/rv/hardening-final-regression.log`。
- Cursor 两项 P2 修正和本地停止证据专项：35 项通过。Muse 日志回读修正后相关模块 53 项通过；补充“本地停止未确认不开第二消费者”后，Muse 取消专项 11 项通过。新问题均先观察到失败再修正。
- 修正后的最终完整 Python 套件：510 项，505 通过、5 跳过，0 失败/错误；耗时 355.379 秒，退出码 0，日志 `.tools/rv/hardening-reviewed-final-regression.log`。跳过项为 POSIX 独立 session、POSIX SSH 模拟，以及 3 项依赖 fcntl/文件描述符的浏览器集成；未声称这些路径已在 Windows 实测。
- 前端：19 项通过。
- 反馈修正后 wheel 重新构建成功，48 项 Python/前端资源与当前源码逐字节一致；`git diff --check` 通过。文件 `.tools/rv/hardening-reviewed-wheels/local_agent_hub-0.3.0-py3-none-any.whl`，189,137 字节，SHA-256 `5222b87ec40df5394b65d98fa731e70c04307694be45855ef6d38d00065c1edd`。

## Cursor 复审

用户明确要求修复后再次请 Cursor 审核。前两次 `cursor_cli/read_only` 因连接问题失败：

- `d85a408b8a214e3eb62daf8f6c39ac03`：TLS 连接建立前断开，未进入审核。
- `fd0876f419864ed9acca44e088f3f89a`：Cursor API/代理连通性错误，未进入审核。

本机配置的代理 TCP 端口可连接；通过相同代理探测 `api2.cursor.sh` 时出现 TLS EOF，而 `api.cursor.com` 返回 HTTP 200。CLI 源码表明前者是其生产对话 API，后者用于其他 API，未擅自替换端点或更改代理配置。上述失败不视为完成复审。

随后使用原配置探测生产 API 已恢复 HTTP 200，第三次 `6214870c0ff84c5095a102db6cfb3bd7` 审核成功，Cursor session `ffd4141c-4b3b-40f7-a42d-4157a34e1ad3`。审核前后 96 文件快照一致。原文保存于 `.tools/rv/cursor-post-hardening-answer.md`。

Cursor 为只读静态审核，没有运行测试；其结论是六项主路径已落地，未发现明确 P1，提出两项 P2 和三项 P3。本地逐项核对如下：

1. **P2：reader 收尾期间取消/期限到达，仍可能发布流错误。已修。** 新回归在 join 期间确定性注入取消/期限变化，观察到旧实现失败；最终错误发布统一重检取消与期限，避免依赖主循环检查顺序。
2. **P2：进程 wait 超时丢失原取消/流错误。已修。** 卡住进程替身复现 `TimeoutExpired`；现在保留原 `HubError` 与等待超时证据。未确认本地停止时不宣称完成停止，并保持工作区隔离。
3. **P3：Muse 取消全量读取 stdout 日志。已修。** 使用运行中观察到的 accepted 会话信息，移除事后 `read_text().splitlines()`。测试禁止读取日志，仍确认原会话清理参数；worker 自身返回超时的路径同样覆盖。本地停止未确认时不另开远端消费者，并保留会话锁。
4. **P3：queued 兄弟任务缺 workspace 时续接入口未判忙。保留后续。** 真实续接会传递父 workspace；缺 workspace 的新 `edit_worktree` 任务实际使用独立目录，不能直接将其 cwd 当作执行目录，否则误拒绝。需要按 mode 和实际工作区语义补齐旧数据场景后再改。
5. **P3：tool_names 映射持续累积。保留后续。** 已在自查建议记录，有界 stdout 不代表所有辅助状态都有界。

上述修正发生在 Cursor 报告之后，由本地失败回归、修正和重新验证确认；不将报告描述为已审核最终修正代码。

## 本地自查后续建议

- 为 `tool_names` 等跨帧辅助映射增加生命周期回收或有界缓存，进一步控制超长 CLI 会话的内存。
- 为磁盘日志与历史事件增加容量统计、保留策略及可预览归档；保护在途任务、未确认远端状态和幂等收据。
- 对直接读取的结果 JSON 文件增加独立字节预算，覆盖不经过 CLI 输出采集器的插件及历史大结果。
- 将 `get_capabilities` 的执行器诊断移出 Service 全局锁并考虑缓存。本机门控 Fake 复现：诊断阻塞时，独立 `get_task` 同样等待；与此前已移出锁的 `list_agents` 路径不同。

Cursor 的下一轮建议同样集中于结果 JSON 读取预算、磁盘日志/事件保留策略及 tool_names 上限。建议先处理结果读取预算，再处理辅助映射和诊断锁，最后设计有预览及恢复能力的归档策略。这些后续项未在本轮扩展实现。

日常 Hub 未重启，因此运行中的服务尚未加载本轮源码；未提交、推送或发布。
