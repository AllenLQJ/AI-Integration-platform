# Windows 执行器配置与问候验收

日期：2026-10-12。测试基线：`9108081` 加本轮 Windows 兼容性修正。本文随兼容性修正归档；后续整合的回归结果单独记录。

## Claude 超时原因

Claude 用户设置指向 CCR `http://127.0.0.1:3456`。初始检查连接被拒绝，CCR 最新请求日志仍停在 10-10；问候任务未进入网关。CLI 的 `unrecognized_model` 提示不能单独证明模型配置错误。

恢复 CCR 管理服务后，内部网关曾因 5 秒启动握手期限失败。独立启动探针测得核心模块加载约 4.6 秒；后续重新启动成功，网关状态为 running。未修改模型、账号认证或 CCR 的固定握手期限。新 Claude 问候任务实际成功。

## 配置范围

- Codex、Claude、Muse：保留原接入，使用各自私有认证。
- Cursor IDE：`cursor`，`kind=ide`，配置实际 Windows `Cursor.exe` 路径。由 Codex 内置 Windows worker 驱动独立聊天，并通过本机 RPC 领取和回写任务。
- Cursor CLI：`cursor_cli`，使用 D 盘 CLI 安装目录的原生 Node 可执行文件，`args` 指向同目录 `index.js`。`credentials_file` 引用 BDK 官方登录工具保存的私有凭据；不在 Hub 配置中复制密钥。
- Grok Bot：`grokbot`，BDK 0.2.23 登录后创建专用 `Agent Hub Windows Worker`，固定其会话 ID；不复用用户原有聊天。
- 通用 SSH 远端执行器需要明确目标机器、登录与路径，本轮没有虚构远端节点。

## Windows 修正与权限范围

Cursor 原生 Windows CLI 不支持 `--sandbox enabled`。用户明确批准仅 `cursor_cli` 使用官方权限白名单模式，本机配置为 `sandbox=disabled`。默认值仍为 enabled，不自动按平台降级。仍使用 ask 或工作区文件白名单，禁止 Shell、MCP、WebFetch；read_only 禁止 Write。此模式没有 OS 沙箱，不能用问候成功证明完整权限隔离已审计。

新增 Cursor `args` 和 `credentials_file` 配置支持，执行器隔离副本保留启动参数及沙箱选择。显式认证环境覆盖凭据文件；密钥仅在子进程环境中使用。Cursor IDE 持久化记录明确按 UTF-8 读取，修复 Windows 默认编码无法领取中文任务的问题；可执行路径诊断不再仅检查 Mac 应用目录。

IDE worker 在当前 Codex 会话中在线，本轮没有安装常驻无人值守 UI 服务。IDE 编辑、取消、续接与重开定位不由单次问候验收替代。

## 本轮真实成功任务

- Claude：`71f008d527c14f7e8399b2127506366d`。
- Grok Bot：`308635c87c744c60a27d363f4e5ed712`。
- Codex：`b9a84eaf7f8140028388f7256b73bb7f`。
- Cursor IDE：`e67c72f6f3f94b73a4d8116b6de045a8`；已在实际独立界面会话观察完成回答并回写 Hub。
- Cursor CLI：`edbbd88919bf4b438fce4d10021dfc40`。
- Muse：`60d49fb409cc4779ae49617e60e8ef2c`。

失败和超时任务保留在本机私有数据库，不删除记录、不重放旧结果充当新验收。认证文件、完整任务日志和配置备份不进入 Git。

## 自动回归

最终完整回归：259 项，257 通过、2 项 POSIX 专用测试跳过、0 失败，耗时 177.291 秒。Windows 符号链接与 ACL 检查均实际通过。最后定向回归为 Cursor/Grok Bot 27 项和 IDE 桥 6 项，全部通过；独立代码审查无剩余可操作问题。

验证命令为 `.venv\Scripts\python.exe -m unittest discover -s tests -v`。问候实测不替代执行器编辑、自然认证过期、跨机器 SSH 或长期稳定性验收。私有运行配置、认证、数据库和原始日志不随代码上传。

## 六入口重复问候实测

在同日重新提交六个独立任务，六项均 succeeded；Cursor IDE 使用新的独立聊天，由在线 Windows worker 驱动并回写真实回复。

- Cursor CLI：`682f6a2a483e4049b69d32d8a1fe0e37`。
- Cursor IDE：`e3f256e8bb224f4ea58cd4f7606b0eae`。
- Codex CLI：`a7895d9fa6914d5abfde6a7a1b158b03`。
- Claude：`fb866d779cca410ea432c10d52a99df0`。
- Grok Bot：`66c56004cb2f433480c9d0b5feeec472`。
- Muse：`bdf92445e659442690669e06f7d94aa8`。

任务 ID 对应本机私有记录；不构成同会话续接、编辑或取消测试。
