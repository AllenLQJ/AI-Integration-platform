# 本机只读看板

看板采用任务链列表、关系图与协作对话、执行详情三栏布局。前端是独立静态模块，使用版本化 HTTP 展示 API，不直接访问数据库或执行器。安装包包含静态资源，运行时无需 Node.js。

## 打开

```sh
agent-hub dashboard
```

可通过已有 `--data-dir` 参数或 `AGENT_HUB_DATA_DIR` 环境变量选择数据目录。命令连接该目录的后台服务并打开默认浏览器。代码升级后，旧后台进程仍运行旧代码；请在已有任务结束后按正常维护流程重启 Hub，再打开看板。

命令为本机浏览器签发一次性、60 秒有效的票据，交换为最长 8 小时的只读会话。票据使用 URL fragment 传递，页面立即清除它；浏览器不会接收 Hub 的管理令牌。链接过期后重新运行打开命令。不要分享登录链接或将真实数据放入源码。

可以用不同 `--data-dir` 分别打开多个本机 Hub 看板；只读会话按服务端口区分。后台服务重启后需要重新运行打开命令。

## 阅读任务链

- 左侧每项是一条协作工作链。优先按显式 `work_item_id` 成员聚合；同时兼容旧 `parent_id`（续接/切换）与 `review_of.task_id`（审查）连线。独立提交且未关联的任务保持独立，不根据相似标题、cwd 或 session 猜测。
- 关键词可匹配工作项标题、任务编号或指令；执行器和执行状态仍须共同匹配链中的同一次执行。列表返回该执行所在的整条工作链，分页单位是工作链。工作项标题沿用看板脱敏规则。
- 图中每个节点代表一次执行。审核/返修连线清晰；可横向滚动、缩放与复位，点击节点定位协作对话并查看该次执行的详情。
- 中栏统一时间线按真实时间混排模型消息与已观测/已上报的读文件、命令、测试、变更等操作。`timeline: []` 为空数组时权威，不回退 messages。每条完整卡片可展开/折叠；顶部「全部展开/全部折叠（已载入）」只作用于当前链已载入记录。
- 短消息（≤420 字符）默认展开；长消息与操作详情默认折叠。刷新、内容更新、开始→结束状态替换会保留折叠选择；操作卡片 ID 由 task+真实 operation_id 稳定，不因事件窗口裁掉 started 而改名。
- 每条记录标明 provenance：执行器观测 / 总控上报 / 兼容摘要。仅有总控上报消息不会伪装成 CLI 工具观测；缺 ID 工具不会因存在其它 activity 而丢失。
- 兼容旧 `messages` 字段；新服务同时返回 `timeline` 与 `capture_coverage`。
- 执行事件属于当前选中的节点。
- 详情区展示综合状态标签（`workStatusLabel`）：按 `work_status` 区分执行中、等待验收、已完成、连接中断等，与单独的执行状态/验收状态并列；`succeeded` 只表示执行结束。
- 工作项标题/统计/最近主执行状态来自非审核实施节点，不被审核失败覆盖。`interrupted` / `delivery_unknown` 显示为「连接中断／远端状态未知」，不是「执行失败/已停止」。只读看板不会代为 reconcile；管理端沿原 task 调用 `reconcile_task`。
- 图和时间线会自动刷新，新续接记录无需重新打开页面。

## 工作项与派发

公开 MCP：先 `create_work_item`（默认标题「工作项」），再 `submit_task(work_item_id=…)`；审核/返修还须 `related_task_id`。缺失 `work_item_id` 返回 `WORK_ITEM_REQUIRED`，不会静默建根。历史任务用管理认证 `link_task` 补关联（只改分组元数据）。Python `Client.work_item(id)` 可绑定后自动携带。`work_item_id` 独立于 Verification.root_id / acceptance attempt。已打开的 MCP 客户端需刷新工具列表。

可选 `create_work_item.acceptance`：`Client.software_dashboard_acceptance()`（tests/ui/review 三条 required `content_review`，由总控以已登记产物写 `record_external_verification`）或 `Client.conversation_acceptance()`（`policy=no_extra`）。开发/返修默认继承；审核任务不继承。`continue_task.acceptance` 仅覆盖新子任务。看板：`not_requested` 显示为「未设置额外验收」；`no_extra` 为「已完成／无需额外验收」；无规则为「未配置验收」。执行成功≠独立验收通过；缺证据不伪造历史 passed。旧事件缺 role 时标题为「Cursor · 任务消息」一类友好标题（非认证角色）。

## 外部活动上报

总控或执行桥可通过本机管理认证调用 MCP/CLI `record_activity`（只追加活动，不执行命令、不改任务状态/updated/验收）。若工作项已有 controller 租约，须提供匹配的 `controller_id`/`controller_epoch`。浏览器只读 cookie 不能调用。请求按独立收据表原子幂等；相同键不同内容冲突。活动 JSON ≤64KiB，输出摘录合计 ≤16KiB（先脱敏再按 UTF-8 合计限额），路径为工作区相对显示。

上报入口本身不会自动截获 Codex 桌面或 Grok/Muse 未公开的内部工具参数；那些来源需要总控/hook/桥接实际调用上报，或上游已提供结构化事件。

## 数据与限制

没有明确记录的 Agent 派发关系不做推测。看板只读：不提供派发、取消、总控接管或任意文件下载。管理端 `link_task` / `reconcile_task` 不在浏览器 cookie 会话中开放。command v1 仍禁止把任意 stdout 解析为管理事件。

每条链的图最多展示 200 次执行；时间线（消息+操作，含嵌套操作字段）最多 500 条、合计 100 万显示字符。达到上限、缺失关联或出现循环记录时，页面提示数据不完整；不会补造缺失节点或完整聊天历史。「全部」仅指已载入记录。

选中执行的事件默认读取最近 200 条，实时窗口最多保留 1,000 条、合计约 2 MiB 序列化数据；对应旧 DOM 也会移除。可用「更早事件」逐页查看旧记录，用「返回最新」恢复实时事件；浏览历史页时，右侧任务详情仍会更新。单条过大的事件显示摘要，完整持久化记录可通过管理端 `get_events` 读取。

结果文件缺失、损坏或不是 JSON 对象时，仅对应执行显示结果不可用提示，其他节点及事件继续展示；已记录的执行和验收状态保持原值。管理接口 `get_result` 返回 `result_available=false` 与 `result_error_code=RESULT_UNAVAILABLE`，不会据此推断远端执行失败。

远端停止未确认的任务结束后，Hub 会在释放调度槽位前隔离其工作区，同目录已排队任务和新任务均会被阻止。看板只展示这一状态；需要管理端核对旧执行，后续任务可使用新的工作区。

过程消息可能被执行器截断；自定义插件或命令执行器可能只返回结果。执行完成不等于独立验收通过。不保存完整读取文件内容、环境变量或凭据。

## 接口与边界

展示接口位于 `/dashboard/api/v1/`：

- `chains?limit=30&offset=0&q=&state=&agent=`：分页任务链列表。
- `chains/{member_id}`：链概要、执行节点、类型化连线、`messages`（兼容）、`timeline`、`capture_coverage` 和截断/异常提示；可用链内任一任务编号查询。
- `tasks`、`agents`、`tasks/{id}`、`tasks/{id}/events`：保留原有单次执行接口；单任务详情同样含 `timeline`。
- `tasks/{id}/events?before_seq=…`：读取严格早于游标的最近 200 条事件，按时间顺序返回，并带 `has_more`；首次读取最新页使用 `before_seq=9223372036854775807`。与 `after_seq` 互斥。

接口使用独立只读会话，不接受该会话调用管理 RPC。服务仅监听回环地址。

任务链及任务列表/详情支持 `ETag` / `If-None-Match` 条件读取。ETag 由响应内容计算：数据库没有新写入时直接返回 304，不重建投影；有写入（包括验收、任务关联和活动，即使没有新事件或任务更新时间变化）时重新计算，结果与客户端缓存一致仍返回 304，因此其他任务的写入不会让这条任务链或任务的缓存失效。结果文件的删除、恢复、替换或修改也会使相关详情缓存失效。所有条件请求仍先校验只读会话。浏览器仅使用有界内存缓存，认证失败或重新交换会话时清空，不写入持久化存储。

服务端按 SQLite `data_version` 缓存任务链图扫描结果：同一数据版本下，任务链列表、任务链详情和多个标签页共用一次扫描（按筛选条件区分，最多 8 份）；任何数据库写入都会使其失效，旧版本条目随即丢弃。任务摘要、消息与结果文件仍按请求读取。

页面轮询节奏：任务链列表每 5 秒；任务链与选中执行在有成员执行中或验收未完成（`pending`、`awaiting_verification`、`verifying`）时每 2.5 秒，全部结束且验收已定后降为每 15 秒，出现新执行或重新进入验收时恢复。标签页隐藏时不再安排新的轮询，重新可见时立即刷新列表、任务链和选中执行，再按上述节奏继续。

前端 `api.mjs` 处理传输和会话，`state.mjs` 处理请求序列与事件游标，`graph.mjs` 处理关系图，`timeline.mjs` 处理折叠状态，页面与样式分别放在 `app.mjs`、`style.css`。后端 `dashboard_timeline.py` / `dashboard_chains.py` 生成投影。

合成演示（非真实抓取）：`python scripts/synthetic_collaboration_timeline.py`。

## 开发检查

```sh
python -m unittest discover -s tests -p test_activity.py -v
python -m unittest discover -s tests -p test_stream_events.py -v
python -m unittest discover -s tests -p test_dashboard_timeline.py -v
python -m unittest discover -s tests -p 'test_dashboard*.py' -v
python -m unittest discover -s tests -p test_cursor_grokbot.py -v
python -m unittest discover -s tests -p test_muse_timeline.py -v
node --test tests/frontend/*.mjs
python -m unittest discover -s tests
python -m pip wheel . --no-deps
```

前端不依赖第三方 CDN、字体、构建工具或品牌固定名单。修改展示契约时同步检查 API 与页面测试；打包时确认静态文件（含 `timeline.mjs`）包含在 wheel 内。

首版验证记录见 [初版验收](2026-10-24-dashboard-validation.md)，任务链改版见 [任务链验收](2026-10-24-task-chain-validation.md)，统一时间线见 [协作时间线验收](2026-10-24-collaboration-timeline-validation.md)，可靠性与性能改进见 [优化验证](2026-10-25-reliability-dashboard-performance-validation.md)。

工作区隔离、结果读取容错、事件历史分页和验收聚合批量查询见 [第二轮优化验证](2026-10-25-second-optimization-validation.md)。
