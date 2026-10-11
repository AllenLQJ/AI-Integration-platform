# Cursor IDE 本机安装记录

日期：2026-10-11。用户要求与 Grok Bot 远端取消需求同时推进。

## 已完成

- 从 [Cursor 官方下载页](https://cursor.com/download) 的 Mac ARM64 链接下载并安装至 `/Applications/Cursor.app`。
- 版本 `3.24.12`，ARM64，应用 Bundle ID 为 `com.todesktop.230313mzl4w4u92`。
- DMG 大小 `295402051` 字节；`hdiutil verify` 验证镜像校验通过。
- DMG SHA256：`c5ed7193bfb906e2aae3f3f281c62cb979efd1b662c73524a15f1c4448734f5f`，用于留存此次安装包指纹，并非与厂商公布的 SHA256 比对。
- 安装前签名检查通过：Developer ID Application: Hilary Stout，TeamIdentifier `VDXQ22DGB9`；Apple 公证评估 accepted / Notarized Developer ID。
- 安装后重新检查应用签名；成功启动原生 Cursor Agents 窗口并显示官方 Log In / Sign Up 页面。
- 既有 Cursor CLI 与 IDE 分别安装，IDE 的 bin/cursor --version 返回 `3.24.12` 与 arm64。
- 用户 `~/.cursor/mcp.json` 已有 agent_hub 注册，指向本机项目虚拟环境 Python，args 为 `-m agent_hub mcp`；CLI 先前已发现八工具并完成真实总控验收。
- 用户完成 IDE 独立登录；原生设置窗口已显示账号 Allen Lan 与 Log Out，确认 IDE 已登录。
- IDE 的 Customize → MCPs 已显示 Connected 下的 agent_hub，`8 tools enabled`，确认桌面入口已连接并发现八个工具。

## 待确认

桌面 IDE 的登录、MCP 连接和八个工具展示已确认，IDE 发起任务的实际调用尚未独立验收。安装和工具展示不代替 IDE 内实际派发验收。

安装包、下载日志和私有安装记录保存在 `~/.agent-hub/cursor-ide-install-20261011/`。镜像已卸载，应用从 /Applications 启动。没有关闭 Gatekeeper 或修改应用签名。

项目本轮代码和文档仍在本地，未提交或推送 GitHub。
