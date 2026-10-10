# D 盘运行环境迁移

日期：2026-10-10，Asia/Shanghai。

## 已完成

- 在 D:\My_Files\AI-Integration-platform\.venv 使用既有 py310 新建环境，按 requirements-windows.lock 安装依赖，并以 editable 模式安装本项目。
- Python 与 agent_hub 模块路径均位于 D 盘主目录；完整回归 62/62，通过，耗时 53.817 秒；pip check 无破损依赖。
- 备份 Codex config.toml，仅替换 agent_hub 的 command 为 D 盘 Python，args 与 PYTHONUTF8 保留；codex mcp get 已确认新路径。Claude 的已保存测试 MCP 配置也同步并保留备份。Codex 标准配置文件仍留在用户配置目录。
- 确认零活动任务后正常关闭旧后台并从 D 盘启动；旧 PID 40484，新 PID 37112。
- 新 stdio MCP 握手列出八个工具，list_agents 可调用，wait_for_task 的缺失任务探针返回 TASK_NOT_FOUND。38 条历史任务记录保留，私有数据和认证不搬入仓库。
- C 盘旧 .superpowers 中的 11 个验证文件已复制到 D 盘 .superpowers/c-drive-migration-backup，并逐文件核对 SHA-256；该目录不上传。

## 归档与残留状态

原开发聊天已通过 Codex 受管归档工具保存源码快照，旧 worktree 已从 Git 登记中移除；关闭了旧 MCP 前端。11 个私有验证文件的 D 盘备份已核对。

C 盘旧目录仅剩 `.venv` 的 7 个环境文件及父目录，文件占用阻止完整删除。用户于 2026-10-10 明确决定不再清理，保留这些残留；它们不作为当前运行环境。任务数据库、认证、结果和其他任务 worktree 保留。

本次最终核对：D 盘 Python 和模块路径正确，共享后台健康检查 ok=true；公开 GitHub main 已同步。

## 发布范围

用户已明确授权将 D 盘 main 上传公开仓库 AllenLQJ/AI-Integration-platform。仅上传已提交源码、测试、文档、依赖锁和 Muse 配套补丁；虚拟环境、认证、私有数据、日志与本地验证备份不上传。
