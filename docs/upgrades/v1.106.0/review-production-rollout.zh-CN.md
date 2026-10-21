# 生产发布独立复核与关闭记录

2026-10-21，独立子代理 `audit_production_rollout_final` 对切流后首轮证据逐项核对，返回通过。此轮快照包含 24 次生产真实请求；后续更正构建任务注解又进行了 12 次请求，最终数量单独记录，不能把后续验证冒充初轮审查输入

| 初轮独立检查 | 子代理结论 |
| --- | --- |
| 数据库 | 213 项迁移与源码 SQL checksum 一致 |
| 镜像及运行源码 | 四个新版副本就绪、零重启，每副本 78 文件匹配冻结源码 |
| 入口 | 公网 readiness 和 UI 均为 200 |
| SSE | 独立解析 20 次 Messages 的 186 帧，无 event/type 不一致 |
| Chat / Responses | 流正常结束，Responses completed |
| 工具历史 | 调用和 tool result 回放通过 |
| 费用 | 24 次请求逐条对应持久化记账，全部成功且费用非零 |
| 临时 key | 已清理 |

| 复核发现或边界 | 收尾结果 |
| --- | --- |
| 生产基线仍写旧版本 | CURRENT_BASELINE.md、UPGRADE_V1_106_0.md 及目录说明已同步实际生产事实 |
| Pod 模板 build-job 注解仍指旧构建 | 已更正为 litellm-v1106-build-r5-f025726ea5，同 digest 滚动更新后，最终四副本身份、78 文件哈希及每副本 3 次工具往返重新通过 |
| /metrics 为 404 | 原配置未启用 Prometheus callback，保留检查结果，不记为通过 |
| 两条业务 ERROR | 原有 key 超预算拒绝，未改动预算 |
| 历史会话与长观察 | 未宣称所有历史故障恢复或完成 24 小时观察 |

主代理在更正注解后补充验证最终四副本，12 次新增请求全部成功。最终合计 36 条不同 call ID 均对应数据库 success 记录和非零费用，32 次 Messages 共 302 帧无类型不一致；测试 key 为零，业务数量保持 279/4/82/0。[最终脱敏摘要](PRODUCTION_VALIDATION.json)固定这组结果

另行运行的独立 CLI 复核首轮因 bwrap 隔离环境无法读取文件而未通过，这属于审查工具失败，不是生产故障，也没有记为证据通过。[初次重审](review-production-evidence-initial.zh-CN.md)发现 writer、备份来源和 Responses 账单关联证据缺口。补充实际 Prisma writer 权限、旧 RS 的排空时间及同目标库备份证明、36 个 HTTP call ID 与账单一对一关联后，[独立复核](review-production-evidence-closure.zh-CN.md)确认缺口全部关闭，无剩余发布阻断，原始失败不删除
