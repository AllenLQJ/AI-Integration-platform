# D 盘运行环境迁移

日期：2026-10-10，(timezone omitted)。

## 已完成

- 在 D:\projects\AI-Integration-platform\.venv 使用既有 py310 新建环境，按 requirements-windows.lock 安装依赖，并以 editable 模式安装本项目。
- Python 与 agent_hub 模块路径均位于 D 盘主目录；完整回归 62/62，通过，耗时 53.817 秒；pip check 无破损依赖。
- 备份 Codex config.toml，仅替换 agent_hub 的 command 为 D 盘 Python，args 与 PYTHONUTF8 保留；codex mcp get 已确认新路径。Claude 的已保存测试 MCP 配置也同步并保留备份。Codex 标准配置文件仍留在用户配置目录。
- 确认零活动任务后正常关闭旧后台并从 D 盘启动；旧 PID 40484，新 PID 37112。
- 新 stdio MCP 握手列出八个工具，list_agents 可调用，wait_for_task 的缺失任务探针返回 TASK_NOT_FOUND。38 条历史任务记录保留，私有数据和认证不搬入仓库。
- C 盘旧 .superpowers 中的 11 个验证文件已复制到 D 盘 .superpowers/c-drive-migration-backup，并逐文件核对 SHA-256；该目录不上传。

## 清理状态

C 盘旧开发 worktree 为 C:\Users\your-name\.codex\worktrees\agent-hub-v1\AI-Integration-platform。批量清理命令被自动审批拒绝，因此没有执行删除或终止旧 MCP 前端。使用受管归档工具时，Codex 报告该 worktree 属于另一聊天，当前聊天不能附加或归档。清理需所属聊天执行；当前旧目录和环境仍保留，不能称为已删除。

运行中的共享后台已使用 D 盘环境。其他已打开聊天可能仍持有 C 盘旧 MCP 前端，需重新加载 MCP 或重新打开聊天。私有任务数据库、认证、结果和任务 worktree 有历史价值，保留。

## 发布范围

用户已明确授权将 D 盘 main 上传公开仓库 AllenLQJ/AI-Integration-platform。仅上传已提交源码、测试、文档、依赖锁和 Muse 配套补丁；虚拟环境、认证、私有数据、日志与本地验证备份不上传。
