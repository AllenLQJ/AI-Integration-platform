# Muse MCP 配套修复

上游：https://github.com/kleprevost/muse-mcp
基线：965cc2f（Fix send failures and auth setup guidance）。上游代码遵循其 MIT LICENSE；此目录仅保存本机兼容性补丁，不包含认证数据。

补丁包含 Windows 浏览器认证进程输出处理、临时 TLS 建连恢复以及专用 Muse 网关复用。Hub 的 Muse 执行器需要此配套实现。

在项目根目录执行：

```powershell
git clone https://github.com/kleprevost/muse-mcp.git muse-mcp
git -C muse-mcp checkout 965cc2f
git -C muse-mcp apply ../vendor/muse-mcp/local-compatibility.patch
```

随后按上游 README 创建独立环境并安装依赖，完成自己的认证，在 Hub config.json 中填写实际 Python 路径。不要上传 cookies、令牌或本地配置。

当前补丁对应本机工作区，不代表已由上游合并。
