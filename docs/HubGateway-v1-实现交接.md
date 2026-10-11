# HubGateway v1 实现交接

日期：2026-10-11。实现版本：0.2.1，契约版本：1.0。根据 MediaFlow 最新服务端约定继续实现，复用原接口。当前 MCP 共 11 工具（包括同期增加的 resume_task），schema 见 `HubGateway-v1-tools.json`。

## 已实现接口

- get_capabilities：只读查询版本、稳定 host_id、执行器模式、能力及证据，不派发模型任务。
- submit_task：可选 context，完整 JSON 持久化并参与幂等哈希；不传或 null 保持旧哈希兼容。无素材时 payload.input_host_id=null；非空附件仍拒绝。
- get_result：保留原始 state、answer、artifacts.logs，附 media_artifacts、gateway_state、execution_uncertain、errors 和 contract_version。
- get_artifact：只返回服务端登记、绑定 task_id 且哈希未变化的文件；同主机 local_file，不接受客户端任意路径，不返回 Base64。
- waiting_for_user/cancelling 的 gateway_state 为 running；timed_out/interrupted/delivery_unknown/cancelled 为 failed，保留原始状态供客户端核对。不确定执行不自动重发；超时包含 TASK_TIMED_OUT。

## 已验证能力与模型身份

文本审核：Claude text_only，实际 CLI modelUsage 为 deepseek-flash[1m]。任务 `921171a6f80044a4877a3189348fed15` 返回完整审核 JSON，通过 MediaFlow REVIEW_SCHEMA 及 fingerprint/policy_version 一致性检查。仅证明一个清晰请求的文本审核协议，不证明视觉审核或广泛审核质量。

图片：用户明确同意将 `muse:auto` 约定为 Muse 默认图片路由。model_identity.selection=default_route，upstream_model=null；Muse 上游未披露底层模型名称。verified 仅代表这一默认路由的有界图片生成/取回能力，不能解释为任何具体图片模型已验证。

任务 `079fd10f94894064a79e03f8b7a07de7` 只派发一次：白底红色圆形、无文字、无参考图的 PNG。Muse 返回 completed 回复及结构化 delta.presentation(kind=image)，真实原图 1600×1600、145025 字节，实际解码与视觉检查通过。SHA-256：`8d541619640a18c40cf41b08343c6203843b22909a363bbc0ddb36c95a4f5e0f`。

产物 ID：`artifact-85835d25a22d57c390ba8bac998c4dbf`。由旧后台派发的原任务，使用新版适配器完成授权成品恢复取回并登记，没有重复生成。新版本正常任务会在返回成功前自动执行同一取回逻辑；首次新版完整生成周期仍可由 MediaFlow 实际业务验收。

当前 verified 图片 profile 仅声明 count=1、format=png，input_transports=[]、output_mime_types=[image/png]。不声明宽高/比例控制；一次产物尺寸不证明任意指定尺寸支持。公开证据及 profile 见 `HubGateway-v1-verified-profiles.json`。

## Muse 附件取回

只识别本任务序号范围中的结构化 image presentation，核对 missing、byte_len、mime、sandbox://workspace/imagine_media 文件身份和 original 取回地址。普通正文网址、external_link、缩略图和来源网页不登记成品。只有远端会话 completed 且完成回复/附件连续稳定读取后才取回。

实测 VM HTTPS 原图无法直接连接；使用 Muse 已认证的 Noise 网关读取 presentation.original 对应的 `/media/raw/workspace/imagine_media/…` 路径。专用只读 helper 在 Muse 配置的 Python 环境中连接，核对所属 VM、路径、大小，写入 Hub 私有目录；Hub 执行 PNG/JPEG 实际解码和 SHA-256 登记。URL 仅在 worker 内存使用，不进入结果、事件、传输日志或交接。helper 输入只含 VM 标识和媒体路径，不含 URL 或认证数据。

文件授权根目录：`/Users/your-name/.agent-hub/media-artifacts`。客户端配置 same_host=true、trusted_artifact_roots=[该目录]。每文件上限 64 MiB，禁止符号链接及越界；缺失返回 ARTIFACT_NOT_FOUND，内容变化返回 ARTIFACT_EXPIRED。客户端仍须独立验证大小、哈希、签名/解码并原子归档。

视频、输入附件、参考图/编辑、JPEG 默认路由实测均未完成，不登记 verified；视频请求在派发前拒绝。图片通过后，视频另做独立适配与 MP4 验收。不会自动修改 MediaFlow 业务源代码、自动发布或发送交接消息。

## 验证和加载

```zsh
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m unittest discover -s tests -q
```

2026-10-11 最新完整回归：156 项，155 通过、1 项 Windows ACL 跳过，耗时 63.357 秒。新增测试涵盖结构化附件识别、来源链接隔离、任务序号边界、授权地址/路径、格式/大小、原图取回失败、实际解码、证据不含 URL、指定模型/视频在派发前拒绝。既有进程测试产生 ResourceWarning，无测试失败。

使用 MediaFlow 的实际 MCPGateway、verified_capability、validate_parameters 和 ArtifactCollector 通过 stdio 读取能力、原任务结果和附件，并在隔离临时目录完成签名/解码、大小、SHA-256 与原子归档校验；哈希与 Hub 一致。该检查不创建 MediaFlow 工作流、不重新生成、不构成新版完整业务生成周期验收。

确认共享后台没有 queued/running/waiting_for_user/cancelling 任务后，已重载至 0.2.1。客户端刷新 MCP 连接取得最新工具 schema。Windows 尚未重新实机回归。

MediaFlow 当前 normalize_task 尚不识别同期新增的原始 waiting_for_user 状态；其客户端需另行扩展人工等待兼容，不能把 UNKNOWN_STATE 当作未派发后重新生成。正常图片链路及原任务取回不依赖该状态。
