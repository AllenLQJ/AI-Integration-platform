# wait_for_task 实施计划

日期：2026-10-10。执行方式：当前会话 inline；用户已要求完成改动并测试。
状态：Task 1–3 已完成；最终回归 62/62、真实 Codex/Claude 总控通过、独立审阅无阻断。验收见 `../../validation/2026-10-10-wait-for-task-validation.md`。
设计依据：../specs/2026-10-10-wait-for-task-addendum.md。
实施工作区：agent-hub-v1，feature/agent-hub-v1。等待功能完成后已按用户授权重启共享服务并通过真实 Codex 冒烟；文档已同步且已快进合并 main，GitHub 公开上传已获用户明确授权。

## Task 1：等待与一致快照

Interfaces：Service.wait_for_task(task_id, after_seq=0, timeout_seconds=10) 返回 state/events/next_seq/has_more/timed_out；Store.wait_snapshot(task_id, after_seq) 提供同一读事务中的状态及最多 200 条事件。

- 先写 tests/test_wait.py：六种终态、运行中超时、期限/幂等不变、取消并发、分页及重复游标重读、非法参数、关闭唤醒。
- 写终态事件插入失败回滚测试，证明 Store.finish 终态及事件必须原子提交。
- 运行 `.venv/Scripts/python.exe -m unittest discover -s tests -p test_wait.py -v`，预期新增等待测试因 UNKNOWN_TOOL 失败。
- 实现 service.py 锁外等待，monotonic 截止时间、100ms 检查、0–10 秒有限数参数；stopping 时 SERVICE_STOPPING。
- 实现 store.py 一致快照与 finish 原子事务，不改表结构、七个旧工具的返回结构及终态保护。
- 同命令通过，记录红绿结果。

## Task 2：HTTP 与 MCP

Interfaces：新增只读异步 MCP wait_for_task，asyncio.to_thread 调用现有 invoke；Client 15 秒超时保持。

- 先写真实回环 HTTP 多客户端等待/查询/提交/取消回归，以及同一 MCP stdio 会话等待期间查询仍可返回的测试。
- 修改旧握手验收为八个工具；运行新增集成测试，预期工具缺失失败。
- 新增 mcp_server.py 工具；验证 HTTP 与 stdio 返回相同结构、鉴权及参数错误。
- 同命令通过，记录证据。

## Task 3：文档、总控与完整验收

- 更新主设计、增补、README，明确 timed_out 是等待超时；终态后仍按 has_more 分页；响应丢失从最后收到游标重试并按 seq 去重；get_result 获取答案。
- 为真实 Codex/Claude 总控编写隔离数据目录验收，使用相同工具与短模拟任务，不重启共享后台。保存实际工具调用证据，不能用 SDK 代替真实总控验收。
- 运行全套 unittest、pip check、git diff --check，预期零失败。
- 按 executing-plans 的要求做一次独立最终审阅，必要修复先红后绿；保存测试数量、耗时、真实总控及未验收范围。

## Review Focus

等待持有全局锁、同步前端阻塞、终态缺事件、分页游标跳过事件、断线重发任务、NaN/布尔参数、退出挂起及新旧服务加载差异。
