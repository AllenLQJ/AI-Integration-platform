# 设计增补：总控通用等待机制 `wait_for_task`

日期：2026-10-10
状态：用户已于 2026-10-10 确认实施；开发 worktree 实现与验收完成；共享后台已按用户授权重启，新 MCP 会话已验证八个工具及真实 Codex 冒烟
依据：`2026-10-09-agent-hub-design.md`（下称"主设计"）、`../plans/2026-10-09-agent-hub.md`
代码基线：`%USERPROFILE%\.codex\worktrees\agent-hub-v1\AI-Integration-platform`，HEAD `3726926`

本增补只解决一个问题：**总控如何等待任务结果，且这个机制在更换总控平台后仍然成立。** 不改动既有 7 个工具的语义，不改状态机。

## 1. 问题

### 1.1 现状

Hub 只提供"查询"，不提供"等待"。`mcp_server.py:30` 的 `get_task` 文档字符串明确写着"不等待执行完成"；`get_events` 是一次性游标分页。实际代码中没有 wait / long-poll / 订阅 / 回调机制（已 grep `src/` 确认，命中的 `wait` 均为进程等待与 muse 适配器内部等待）。

结果是：**每个总控平台必须自己实现一套轮询循环。**

### 1.2 为什么这是设计缺陷而不是使用习惯

主设计 §3 的架构图首行即 `C[Codex / Claude Code / 待验证的 DeepSeek Harness] --> M[stdio MCP 前端]`，§1 明确"以后可在 Claude Code 或 DeepSeek Harness 中挂载相同接口""切换总控不自动迁移原聊天历史"，§10 第 4 阶段的验收顺序是先验证 Codex 总控、再验证 Claude 总控。

总控可换是既定目标。那么"等待"如果落在总控侧，等待质量就取决于各平台 harness 的能力差异，**同一份主设计在不同总控下的行为不一致** —— 这违背了主设计的分层意图。

### 1.3 本次实际故障（复现证据）

2026-10-10，Claude Code 作为总控提交 codex 调研任务：

- 任务创建 `1791614975`，终态 `1791615156`，实际耗时 **181 秒**
- 总控侧挂了一个 **240 秒**的定时器作为轮询节拍
- 任务在定时器触发前 59 秒已完成，中间形成盲区
- 用户必须先手动追问，才拿到早已完成的结果

任务本身成功（`exit_code=0`）。失败的是"总控如何知道它完成了"。若换 Codex 作总控，同一任务会得到另一套不同的等待行为。

### 1.4 两个不能采用的方案

**(a) 采用某个 harness 的私有能力。** 例如用 Claude Code 的后台任务唤醒或 `Monitor` 轮询。这能修好 Claude Code 当总控的场景，但把等待逻辑绑死在单一 harness 上，Codex 总控时失效 —— 直接违背 §1.2。

**(b) 外部直读 `~/.agent-hub/tasks.db`。** 主设计 §5 定义的对外接口是 MCP 工具契约，§9 的模块划分把 `store.py` 列为 Hub 内部模块。从 Hub 之外直读该库等于把实现细节当接口：改表结构或换存储会静默失效。且主设计 §3 规定"同一数据库不由多个服务竞争执行"，数据库归 daemon 独占管理。

## 2. 目标

把等待语义放进 Hub 自身，经由**既有的对外接口面**暴露，使任何总控平台调用方式一致。

起草时通用面已存在；当前实现仍复用以下接口：

- `daemon.py` 暴露 `POST /rpc`，请求体为 `{tool, arguments}`，转交 `service.call()`
- `daemon.py` 暴露 `GET /health`，`daemon.py` 暴露 `POST /shutdown`
- `mcp_server.py` 当前暴露八个工具，经 `client.call(name, arguments)` 调用 `/rpc`；wait_for_task 使用异步入口与工作线程

即 **MCP 前端不是唯一入口**。任何能发起回环 HTTP 并持有 `connection.json` 中令牌的调用方 —— Codex 的 shell、Claude Code、DeepSeek Harness、独立脚本 —— 都能调用同一个工具面。这一层天然总控无关。

起草时缺少等待工具；现已实现并完成验收。

## 3. 接口设计（现已实现）

已新增第 8 个工具：

| 工具 | 必需参数 | 可选参数 | 返回行为 |
|---|---|---|---|
| `wait_for_task` | `task_id` | `after_seq=0`, `timeout_seconds=10` | 阻塞至任务进入终态或到达等待上限；返回 `{state, events, next_seq, has_more, timed_out}` |

语义约束：

- **不改变任务期限。** 主设计 §5 已规定"总控等待超时与任务期限独立"，本工具必须遵守。
- **不重发、不改状态。** 纯读取，与 `submit_task` 的幂等键无关。
- **超时不是失败。** 到达 `timeout_seconds` 返回 `timed_out: true` 与当前状态，任务继续运行。
- **可续接。** 调用方拿 `next_seq` 再次调用，不丢事件。

## 4. 三个必须处理的实现约束（均已在代码中核实）

### 4.1 全局锁死锁 —— 最高风险

`service.py:28`：`call()` 全程持有 `self.lock`（`threading.RLock`）。
`service.py:124`：后台调度循环 `schedule()` 同样在 `with self.lock:` 内。

若把阻塞等待直接写进 `call()`，等待期间将**持续持有全局锁**，导致：

1. 调度线程无法取得锁 → 没有任何任务能启动 → 没有任何状态会变化
2. 而被等待的正是那个状态变化 → 确定的死锁
3. 连带 `submit_task` / `get_task` / `cancel_task` 全部不可用，连取消都做不到

`daemon.py:65` 使用 `ThreadingHTTPServer`，多个总控可并发调用，问题会立刻暴露。

**等待必须在锁外停放**（例如轮询 store，或用条件变量在状态跃迁时通知）。这是本增补中最需要在实现和测试中确认的一点。

### 4.2 前端 15 秒硬超时

`client.py:27`：`self.opener.open(request, timeout=15)`，超时值写死。

单次等待超过 15 秒会被 MCP 前端自己掐断。两条路线二选一：

- **保守：** 单次等待上限压在 15 秒内，由调用方用 `next_seq` 续接循环。不触碰 `client.py`，兼容性最好。
- **放开：** 将 client 超时参数化，允许更长的单次等待。需同时评估 MCP 客户端自身的超时行为。

### 4.3 终态有六个，不是一个

`models.py:3`：

```python
TERMINAL = frozenset({'succeeded', 'failed', 'timed_out', 'cancelled',
                      'interrupted', 'delivery_unknown'})
```

`wait_for_task` 必须在**全部六个终态**返回。只等 `succeeded` 的话，任务一旦失败，调用方会一直等到等待超时 —— 而 `interrupted`（服务重启后无法确认进程身份，见主设计 §6）与 `delivery_unknown`（Muse 发送后连接丢失）恰恰是最需要立刻告知调用方的状态。

## 5. 验收标准

1. **跨总控一致：** 至少 Codex 与 Claude Code 两个总控通过各自方式调用同一工具，得到一致行为。DeepSeek Harness 若未接入则明确标注未验收。
2. **全终态覆盖：** 构造 `succeeded`、`failed`、`timed_out`、`cancelled`、`interrupted`、`delivery_unknown` 六种终态，`wait_for_task` 均在状态到达时立即返回，而非等满 `timeout_seconds`。
3. **无锁阻塞（对应 §4.1）：** 在一个 `wait_for_task` 挂起期间，另一个客户端并发调用 `submit_task`、`get_task`、`cancel_task` 均正常返回。此项必须作为独立回归测试。
4. **不越权：** 等待前后任务期限、`request_id` 幂等结果、状态机均无变化。
5. **断线续接：** 调用方以最后实际收到的 `next_seq` 续接，不跳过事件。响应丢失时旧游标重试可能重复读取，调用方按事件 `seq` 去重，不承诺网络传输恰好一次。
6. **超时语义：** 到达上限返回 `timed_out: true` 且任务仍为 `running`，后续仍可正常取得结果。

## 6. 非目标

- 不做跨机器调度、不监听外部网卡、不提供公开 HTTP 服务（主设计 §2 已排除）
- 不做推送 / webhook / SSE / 网页看板
- 不改变既有 7 个工具的任何行为与返回结构
- 不改动 `tasks.db` 表结构（如确需索引，单独评估）
- 不引入自动换供应商重试、不自动合并代码（主设计 §2）

## 7. 已确认实施方案

用户已要求开启目标、完成改动并测试，采用评估时建议的保守方案：

1. 新增第八个只读工具，保留原七个工具的行为及返回结构。
2. 单次等待默认 10 秒，上限 10 秒；允许 0 秒立即快照及小数秒，拒绝负数、布尔、NaN/Infinity。client 保留 15 秒超时。after_seq 是 0 至 SQLite 有符号 64 位整数上限的整数。
3. Hub 内部每 100ms 检查持久状态，以 monotonic 计时；停放时不持有 Service 全局锁或数据库事务。关闭服务唤醒等待并返回 SERVICE_STOPPING。
4. MCP 使用 async 工具及工作线程执行阻塞 HTTP，避免阻塞前端事件循环。
5. 同一读事务获取状态及事件；每页最多 200 条，has_more 表示该快照还有后续。终态并不代表事件已取齐，按 next_seq 继续取至 has_more=false，再调用 get_result 获取回答。
6. 终态与终态事件原子提交，不改数据库表结构；复用全部六种终态。
7. 总控需在等待超时后继续调用；本工具不承诺唤醒已结束聊天，也不提供桌面通知。
8. 实施计划：`../plans/2026-10-10-wait-for-task.md`；验收记录与计划分开保存。

## 8. 附：起草阶段的文档状态偏差（历史）

起草时主目录主设计状态栏仍为“尚未开始实现”，与开发 worktree 脱节。本次已更新主目录状态，并在两份主设计中登记第八个工具；当时主目录代码尚未合并；2026-10-10 发布准备已将开发分支快进合并 main，主目录现已含完整源码。

## 9. 证据位置

- 主设计：`docs/superpowers/specs/2026-10-09-agent-hub-design.md`
- 实施计划：`docs/superpowers/plans/2026-10-09-agent-hub.md`
- 开发前验证：`docs/2026-10-09-preflight-results.md`
- Hub 源码与验收记录：`%USERPROFILE%\.codex\worktrees\agent-hub-v1\AI-Integration-platform`
- 本次故障任务：`task_id 730556f90162467f9ccb8cc0fec73d38`，日志 `%USERPROFILE%\.agent-hub\logs\730556f90162467f9ccb8cc0fec73d38\`
- 新增等待工具验收：`docs/validation/2026-10-10-wait-for-task-validation.md`，完整回归 62/62；真实 Codex/Claude 总控调用已验证，执行器为模拟 fixture；DeepSeek Harness 未验收。
