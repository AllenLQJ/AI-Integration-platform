# macOS 本机配置与分阶段验收记录

日期：2026-10-11。初始配置基线为 2a4939a；初始阶段未更改产品代码，后续实际接入和权限修订包含实现修改。本文按阶段保留当时状态，不把早期 Muse 未配置/read_only 或较少测试数解释为当前状态。

当前三个执行器已接入，Muse 使用 delegate。最新完整回归 83 项：82 通过、1 项 Windows ACL 跳过；核心结论见 [Mac 核心验收](2026-10-11-macos-full-acceptance.md)，未验证项及异常恢复见 [补充验收](2026-10-11-macos-resilience-validation.md)。

## 配置

- 项目：`/Users/allen/项目/AI-Integration-platform`。
- 环境：项目 `.venv`，Python 3.12.14，`mcp==1.30.0`。
- 本机使用 `uv venv --python 3.12 .venv`、`uv pip install --python .venv/bin/python -e .`。
- Python 3.12 完整测试仍显式导入 exceptiongroup，另用 `uv pip install --python .venv/bin/python exceptiongroup` 补齐测试依赖。
- 私有数据：`~/.agent-hub`，目录权限 700，config.json 权限 600。
- Codex 0.162.0-alpha.17.2 使用现有 CLI 认证和 read-only/workspace-write 沙箱。
- Claude Code 2.1.296 通过用户设置中的 DeepSeek Anthropic 兼容接口，主模型 `deepseek-flash[1m]`。密钥仅在本机 Claude 用户配置，不复制到 Hub 或仓库。
- 并发：全局 2，每执行器 1；同一工作区串行。
- 初始阶段 Muse 未配置，enabled=false；后续已启用并实测，见下方接入记录。
- agent_hub stdio MCP 已登记 Codex 和 Claude 用户配置，使用项目虚拟环境 Python 绝对路径。原配置备份在私有数据目录。

## 验证结果

- 完整自动回归：62 项，61 通过，1 项 Windows ACL 在 Mac 跳过，38.942 秒。
- uv pip check：31 个包兼容；git diff --check 通过。
- MCP stdio 初始化、八工具发现、list_agents 调用通过；前端关闭后后台仍可健康检查。
- Claude 与 Codex 均通过真实 edit_worktree 文件修改、request_id 幂等提交及原生会话续接。
- 验收使用私有临时 Git 仓库，包含中文与空格路径；原目录 hello.txt 未修改，任务 worktree 保留供审阅。
- Claude/DeepSeek 总控通过 ENABLE_TOOL_SEARCH=false 实际调用 mcp__agent_hub__list_agents，原生会话记录已核对。
- 私有完整任务证据：`~/.agent-hub/mac-acceptance-results.json`。

## 使用

```zsh
cd /Users/allen/项目/AI-Integration-platform
.venv/bin/python -m agent_hub status
ENABLE_TOOL_SEARCH=false claude
```

Codex 需重新加载 MCP 或打开新聊天后使用新登记的八个工具；配置登记不代表当前旧聊天已热加载。Claude 总控使用正常权限授权。

后台由首次调用自动启动，不新增登录启动项；关闭总控不会停止后台。Mac 重启后下次调用再次启动。

显式停止后台：`.venv/bin/python -m agent_hub stop`，会取消在途本地任务，执行前应检查任务。

## 验收边界

本轮完成 Mac 基础配置与两执行器核心闭环，不等同于完整 Mac 第一版验收。真实 Mac 工具子进程取消、超时、崩溃恢复、双执行器并发压力、Codex 总控真实 MCP 调度、Muse、DeepSeek Harness 及 Claude 订阅模式未做本轮实机验证。自动测试覆盖不替代这些真实场景。未提交、推送、合并或删除任务 worktree。

## Muse 接入准备（2026-10-11）

- 独立连接器位于项目 muse-mcp（主仓库忽略），上游基线 965cc2f，已应用 vendor 兼容补丁。
- 独立 Python 3.12 环境和依赖检查通过，连接器 29 项测试通过。
- agent-browser 0.39.0 已安装，入口 ~/.local/bin/agent-browser。
- Hub 已填写连接器绝对路径，保持 enabled=false，待 Chrome 登录与认证验证通过后启用。
- 浏览器认证尚未完成。专用 Chrome 配置在 ~/.agent-hub/muse-browser，调试端口 9223 仅用于本机连接。Cookie 仅保存在 ~/.config/muse-cli/cookies.txt，不纳入仓库。

## Muse 实际接入与复验（2026-10-11）

- 已从专用 Chrome 的 Muse 登录会话导出 Cookie，文件权限 600，目录权限 700；未复制到仓库或日志。
- muse-mcp doctor 验证认证及网关连接成功；Hub Muse 执行器现已 enabled=true。
- 初次真实任务因当前 MCP 对 Any 返回值仅提供 JSON 文本块而失败，尚未发送提示；保留失败记录，不算成功证据。
- Hub 适配器增加 JSON 文本块解析，兼容无 structuredContent 的原始响应、多个历史文本块和单条历史事件；无效 JSON 拒绝处理，原有错误及不确定发送处理保留。
- 增加两项自动测试；完整回归 64 项，63 通过，1 项 Windows ACL 跳过，39.397 秒。
- 修复后真实 Muse 任务成功；续接同一 session_id 正确记住编号。每轮最终标记及两次稳定读取由 Hub 验证。
- 私有证据：~/.agent-hub/muse-mac-acceptance-results.json。诊断期间创建的会话保留，不自动删除。
- 此接入阶段尚未验证实际网页调研/来源链接和复杂浏览器流程。当时 Muse 模式名为 read_only，后续改为 delegate；不能把旧标签理解为只读技术限制。本地取消不代表远端停止。
- 保持专用浏览器可方便更新认证；已导出的 Cookie 可供连接器直接使用，过期后需重新登录导出。未修改 Codex/Claude MCP 注册，仍由同一 Hub 暴露八个工具。

## Muse Mac 本地文件读取探针

- Hub 提交只读提示，仅读取一份无敏感内容的 T7 测试文件；提示未提供随机标记及测试数字。
- 返回随机标记和数字均与本机文件一致，证明该次 Hub 请求可访问本机文件。
- Muse 自述通过配对 Mac 的 files.read 读取；底层调用日志未独立审计。
- 未验证其他本机操作，也不将 Muse read_only 标签解释为技术层面的只读沙箱。

## Muse 客户端退出及写入探针

- 客户端退出前后未检测到 Muse.app 进程；新随机内容文件无法通过 Hub 读取，Muse 报本机连接不可用。
- 客户端重新打开后，用户授权在专用测试目录新建文件；客户端出现写入审批，选择一次性允许后成功。本机字节核对通过。
- Muse 回复中“未触发审批”的自述不准确，以实际客户端审批观察为准。
- Muse read_only 仅为 Hub 执行器模式标签，不能作为本机写入的技术隔离；本轮未增加永久写入许可。

## 权限模式修订与验收

Claude text_only 无工具；read_only 仅 Read/Glob/Grep；edit_worktree 保留文件编辑工具、不开放命令。Muse 仅 delegate，取消只读保证。历史记录保留，旧 Claude 无工具模式续接不扩权，旧 Muse read_only 不允许新续接。工具发现、任务和结果均声明 permissions。

真实账号验收：Claude read_only 返回本机随机文件内容，禁止的新文件未创建；text_only 无法得到随机内容，也未创建文件；Muse delegate 返回指定标记且 permissions.enforced=false，Muse read_only 提交被拒绝。私有证据 ~/.agent-hub/permission-mode-acceptance.json。此次不重新验收已通过且工具集合未改动的 edit_worktree。后台已在无在途任务时重载。

修订后完整回归：70 项，69 通过，1 项 Windows ACL 跳过（39.254 秒）。依赖检查及 git diff --check 通过。

## 后续补充验收

2026-10-11 已进行取消、超时、崩溃恢复、并发、Codex 总控及 Muse 等补充实机验收，并修复两项问题。最新结论以 `2026-10-11-macos-full-acceptance.md` 为准；本文前面的“本轮未验证”描述保留当时范围。
