# Muse 长消息发送失败修复与验收

日期：2026-10-12。范围：Codex → Agent Hub MCP → Muse MCP → Noise 网关。此记录说明传输修复，不包含待讨论的知识库开发方案。

## 故障与根因

任务 `2e0f16076bd64b8d952102ce56b64d29` 创建会话 `97928022-5bfe-4c93-b73a-2df41f7705b1` 后，在 muse_send 返回 GATEWAY_ERROR，Hub 保留 DELIVERY_UNKNOWN。会话独立读取一次、复用连接读取三次均无事件。

包含两份文档的请求约 2.9 万字符。JSON、protobuf 包装后旧实现的单个 Noise 明文帧为 98,115 字节；本机 Noise 库上限 65,535 字节。用原失败正文进行离线真实 Noise 握手和加密，旧帧稳定触发 NoiseInvalidMessage，尚未向 WebSocket 写入内容。这解释了新会话为空和连续重试相同内容仍失败。

旧 Muse MCP 把该异常归并为通用 GATEWAY_ERROR，Hub 只保留调用级阶段，丢失状态码和异常类型。此次同时修复诊断。

## 实现与分发

以 Muse 当日网页脚本 `/_next/static/chunks/0jwl3r-yy8po-.js` 的分片实现为依据：ServiceRequest 序列化后按每片 65,489 字节切分，共用随机 chunk_id，按 chunk_index 标号并声明 total_chunks，最多 256 片。整组发送持有发送锁，避免 Noise 状态及分片交错。超出分片数量限制的请求在写入之前拒绝。

接收端在解析 ServiceResponse 之前重组 NoiseTransportFrame，检查片数、索引、片大小、重复片和一致性，限制 16 个待重组消息并检查 30 秒组装期限。此期限在收到新帧时检查，不是独立的后台定时器。

错误返回保留 exception_type、status（存在时）、stage 和 dispatch_attempted；Hub 映射为 muse_stage 并保留原 muse_send 调用阶段。原始异常正文、令牌和 Cookie 不进入这些诊断字段。dispatch_attempted 表示已进入发送步骤，不等于远端确认。发送不确定时不自动重发。

主仓库通过 `vendor/muse-mcp/local-compatibility.patch` 保存修改，基线仍为上游 965cc2f；补丁包含既有 Windows/TLS/网关复用兼容修改和本轮传输修复。忽略的 muse-mcp 本机 checkout 不作为新增子模块或完整源码上传。

## 验证结果

- 原请求离线复现：旧帧 98,115 字节，NoiseInvalidMessage；修复后两片完整解密还原，最大加密后帧为 65,524 字节。
- 短消息隔离 Hub MCP 通过：任务 b2caca776ec7489191b6ed0ea17a1b1b。
- 3.5 万字符长消息隔离 Hub MCP 通过：任务 b4e22653f86345b4bd2c07580c6c96ca，会话 2052148f-0fed-4a22-ae02-1f9575d558bc。历史返回完整 35,376 字符用户消息，以及末尾验收词和最终标记的助手回复。
- 当前 Codex Agent Hub 插件真实长消息通过：任务 3238b2ed14a648e29527f8cf0d309010，会话 e3d72d3f-d267-4794-add7-c025d60c8b8a；返回 MUSE_LONG_CONNECTOR_OK_20261012 和对应 HUB 完成标记，succeeded、execution_uncertain=false、两次稳定读取。
- Muse MCP 自动测试 36 项、Hub Muse 自动测试 36 项，共 72 项通过；覆盖长消息、分片边界、乱序重组、重复片拒绝、超限拒绝、诊断字段和不重发。

可复验命令：

```sh
muse-mcp/.venv/bin/python -m unittest discover -s muse-mcp/tests
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_muse*.py'
# 会真实创建 Muse 会话并发送验收文本
PYTHONPATH=src .venv/bin/python scripts/muse_success_acceptance.py --long
```

运行证据留在本机私有 ~/.agent-hub 目录，不提交正文、认证配置、日志或数据库。以上结果不代表实际调研报告、浏览器工具执行、Windows 或长期稳定性已验收；原调研请求尚未重发。
