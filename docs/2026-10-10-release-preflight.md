# 主线集成与发布准备

日期：2026-10-10，Asia/Shanghai。

## 本地集成

feature/agent-hub-v1 已快进合并 main，包含 Windows 第一版、Muse 恢复和 wait_for_task。主目录为 D:\My_Files\AI-Integration-platform。主目录原文档已备份到命名 stash `pre-main-integration original docs 2026-10-10`，整合后的版本作为当前文档；不删除备份。

后续已迁移至 D 盘虚拟环境并切换共享后台，详见 `2026-10-10-d-drive-migration.md`；C 盘旧 worktree 已归档，用户决定保留残留环境文件。日常 Muse 仓库及其未提交 cli.py 修改保留。

## 文档和验证

README、开发进度、第一版验收、等待设计与实施计划已按当前源码同步。旧交接和各阶段验收加历史标记，保留实际日期与失败证据。

开发分支当前源码完整回归 62/62 通过（93.993 秒），pip check 无破损依赖。主线目录再次完整回归 62/62 通过（56.863 秒），pip check 无破损依赖，git diff --check 通过；Markdown 相对链接检查无缺失。源码与已通过测试的开发分支一致。

当前八个 MCP 工具；edit_worktree 允许未跟踪文件但不复制，使用已提交 HEAD 基线。Muse 配套补丁在 vendor/muse-mcp，已验证可应用到上游 965cc2f。

## 公开范围

目标：AllenLQJ/AI-Integration-platform，公开仓库，用户已选择名称与可见性，公开上传已获用户明确授权。只上传 main 的已提交源码、测试、脚本、文档、依赖锁和配套补丁。

.tools、Muse 克隆、虚拟环境、cookies、认证配置、私有运行数据库与日志不纳入。根目录三个未跟踪认证/诊断脚本不上传，本地保留。文档保留测试任务 ID 和历史本机路径，均不是认证材料；Mac、DeepSeek Harness、Claude 订阅及 Muse 远端取消仍未验收。

本轮扫描发布文件及 main 提交历史未发现匹配的真实密钥模式或超过 10 MB 的文件；此项为模式扫描与范围核对。
