# Muse MCP 配套修复

上游：https://github.com/kleprevost/muse-mcp
基线：965cc2f（Fix send failures and auth setup guidance）。上游代码遵循其 MIT LICENSE；此目录仅保存本机兼容性补丁，不包含认证数据。

补丁包含 Windows 浏览器认证进程输出处理、临时 TLS 建连恢复以及专用 Muse 网关复用、Noise 长消息分片与接收重组、发送阶段脱敏诊断及相关回归测试。Hub 的 Muse 执行器需要此配套实现。

在项目根目录执行：

```powershell
git clone https://github.com/kleprevost/muse-mcp.git muse-mcp
git -C muse-mcp checkout 965cc2f
git -C muse-mcp apply ../vendor/muse-mcp/local-compatibility.patch
```

随后按上游 README 创建独立环境并安装依赖，完成自己的认证，在 Hub config.json 中填写实际 Python 路径。不要上传 cookies、令牌或本地配置。

## Windows 认证与验证

在 Chrome 登录 Muse 并保持 `https://muse.ai/` 标签页打开。在 `chrome://inspect/#remote-debugging` 启用远程调试，连接时由用户处理 Chrome 的授权弹窗。

项目根目录的 [Authenticate-Muse.ps1](../../Authenticate-Muse.ps1) 使用独立浏览器会话导出认证，再调用 [verify_muse.py](../../verify_muse.py) 检查 MCP 状态；默认不会发送消息。运行前需安装上述 Muse 独立环境，以及本地浏览器工具：

```powershell
npm install --prefix .tools agent-browser@0.38.2
.\Authenticate-Muse.ps1
# 如需代理，显式传入自己的地址；省略时沿用已有环境变量
.\Authenticate-Muse.ps1 -ProxyUrl 'http://127.0.0.1:PORT'
```

认证命令的 Python 模块入口是 `python -m muse_mcp.cli auth export`。`python -m muse_mcp` 只启动等待 stdio 输入的 MCP 服务，不处理 `auth export` 参数，不能用来刷新登录。独立运行 `python verify_muse.py` 只检查状态；加 `--send` 才会发送一条真实测试消息。

2026-10-21 Windows 本机已验证导出成功、MCP `ok=true` / `authenticated=true`，并收到真实测试回复“连接成功”。此项仅覆盖本轮认证与消息收发。

同日发布复测中，一次重复导出未取得 `hatch_sess`，上游工具拒绝覆盖已有 Cookie，脚本以错误退出；随后独立状态检查仍为 `ok=true` / `authenticated=true`。再次运行认证脚本后导出及状态检查通过，并收到新一轮真实回复“复测成功”。长期稳定性尚未验证，不能把已有认证有效当作新一轮导出成功。

## 补丁更新记录

当前补丁对应本机工作区，不代表已由上游合并。

2026-10-12 修复长内容超过单个 Noise 帧限制导致的发送失败。每片有效载荷上限 65,489 字节，最多 256 片；长请求按协议分片，长响应重组后解析。Hub 不自动重发发送不确定的请求。根因和真实验证见 [修复记录](../../docs/2026-10-12-muse-long-message-fix.md)。

更新已有安装时，不要在已经打过旧补丁的 checkout 上重复应用累计补丁；保留原 checkout 和认证配置，用新的 965cc2f checkout 应用本补丁、验证后再切换配置路径。
