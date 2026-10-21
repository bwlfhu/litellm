# v1.106.0 源码合入验收

最终源码为 `efee6c541fe0add9e6fbf7f9fdab1d330b518185`，官方基线为 `955bdaafa8b72b2b6ff728048f272fbc06049f63`。本目录归档源码准入证据，以及后续独立的 [2026-10-21 生产发布记录](PRODUCTION_RELEASE.zh-CN.md)。生产现已使用 1.106.0-custom，构建源码为 `f025726ea5`

[合入分析与实施记录](../../../UPGRADE_V1_106_0.md)说明工作区、合入方向、结果树基线、双亲历史、适配取舍和发布验证边界

| 记录 | 内容 |
| --- | --- |
| [迁移清单](MIGRATION_MANIFEST_V1_106_0.review.zh-CN.md) | 原 112 项逐项归属、旧测试迁移与明确替代 |
| [机器清单](port-manifest-final.review.json) | 文件哈希、112 项及完整 141 路径分类、额外参数适配与验证依据 |
| [协议审查](review-protocol-final.md) | DeepSeek 原生 Messages/Chat、可信上下文、推理/图片/工具历史、SSE 和 Anthropic placement |
| [Responses 审查](review-responses-final.md) | 工具与事件生命周期、多步/托管工具、namespace、快照和客户端隔离 |
| [完整性审查](review-completeness-final.md) | 全差异范围、路由观测、预算 CAS/Lua、费用优先级和最终引用 |
| [参数边界审查](review-param-boundary.zh-CN.md) | 94 处构造适配，25 文件中 21 个为原清单外必要扩展 |
| [验证摘要](VALIDATION.json) | 测试数、XML 哈希、环境、门禁结果和未验证项目 |
| [完整门禁日志](quality-gate.log) | 最终源码以官方 v1.106.0 为基线执行 make check，结果 PASS |

6,838 项扩大回归通过；最后类型边界变更后的 2,466 项受影响回归通过；隔离 PostgreSQL/Redis 30 项真实测试通过；Azure 恢复官方边界后 29 项回归通过。这些测试集存在重叠，不累加为互不重复的总数，均无失败或跳过。warnings 保留在原始日志，并记录于合入记录

三组审查分别执行独立验证，并逐项关闭已发现的阻断问题。协议及 Responses 的核心行为测试冻结在 `8433fae985`，随后独立确认到 `7811a2dc41` 的五文件规则注释位移 AST 不变及 Azure 官方兜底恢复；最终 `efee6c541f` 仅进一步将该 Azure 入参注解扩大为 object，保留原运行逻辑，由完整性审查和最终门禁确认

源码准入阶段保留官方 Rust/Maturin 构建链，当时尚未执行候选构建、完整生产数据库副本迁移与恢复、真实模型验收或生产发布。这些发布验证已在后续发布阶段另行完成，见生产发布记录；既有 Harness 长会话和所有历史 SSE/XML 故障仍不能由短请求验收推定恢复。业务抓取保持关闭，源码准入阶段临时合成 PostgreSQL/Redis 服务已停止

完整原始日志、XML、独立实调 JSON 和早期失败记录保留在 `/home/allcam/projects/litellm-v1106-evidence`；没有把中间失败或缺失的生产验证改记为通过
