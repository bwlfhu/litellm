# LiteLLM 1.106.0 生产发布证据只读复核报告

复核范围：逐项读取指定证据并做只读交叉核验。未修改仓库或生产环境，未调用模型 API。报告未包含 secret、key、config、body_base64 或请求正文

## 逐项结论

| 证据 | 结论 | 核验结果与边界 |
|---|---|---|
| `production-migrated-db-checks.json` | 通过，权限为边界 | 213 项迁移全部完成，无未完成或回滚记录。核心计数为 279 key、4 team、82 model、0 budget。无 invalid index，两项请求日志索引均有效。`missing_writer_select` 为空，但仅证明当前核验身份具备表 SELECT，不能证明实际 writer 的 ALTER、INDEX、TRIGGER、REFERENCES、函数执行及新对象 CRUD 权限 |
| `production-final-backup-identity.json` | 通过，停写证明为边界 | 备份任务标记完成，77 个文件全部存在。独立核对大小和 SHA-256，77/77 全部匹配，字节总量一致。维护开始、备份文件时间、迁移执行时间顺序合理，但文件没有直接提供写冻结信号、数据库身份、LSN 或完整 Job 状态，因此“停写期间完成且绑定到目标生产库”仍是间接证明 |
| `production-per-pod-validation.json` | 通过 | 4 个 Pod 均为 1.106.0，Ready，restartCount 为 0，镜像摘要一致，native 检查通过 |
| `production-all-runtime-hashes.json` | 通过 | 4 个 Pod 各核验 78 个文件，全部匹配。与期望哈希及构建上下文再次比对，78/78 一致 |
| `production-live-probes.jsonl` | 通过 | 6 类真实请求均返回 HTTP 200 和 SSE。Messages、工具调用、重放、Chat、Gemini Messages 均无事件不匹配；Responses 流包含 `response.completed` 且状态为 completed |
| `production-ingress-probes-r2.jsonl` | 通过 | 入口路径的 6 类真实请求全部成功，SSE 事件序列一致，无 mismatch，Responses completed 正常 |
| `production-pod-probes-summary.json` | 通过 | 4 个 Pod 的探针进程 exit code 均为 0。对应 Pod 探针文件各自完成 3 类请求 |
| `production-probes-persisted-accounting.json` | 聚合通过，逐请求关联为边界 | 24 次请求、24 条账单记录、24 条均为 success、zero_cost 为 0，request_id 唯一，prompt/completion token 均有值，费用总和约为 0.004440408。除 2 条 Responses 记录外，其余 22 条可通过响应 ID 直接关联；这 2 条 Responses ID 使用不同的 opaque 编码，当前证据不能完成逐字符串关联，但对应持久化账单行存在且为成功正费用记录 |
| `production-final-db-counts.json` | 通过 | 最终为 279 key、4 team、82 model、0 budget、213 migrations，temporary probe key 为 0。与停写前核心计数一致 |
| `production-final-state.json` | 通过 | replicas、ready、available 均为 4，重启数为 0，endpoint ready 数为 4，运行镜像为 1.106.0 |
| `production-traffic-restored.json` | 通过 | Service selector 已恢复，清理 CronJob 未暂停，流量恢复状态明确 |
| `migration-review.zh-CN.md` | 迁移结论通过，兼容与回滚为硬边界 | 56 项缺失迁移已补齐，旧 157 项 checksum 未变，最终 213 项与目标包一致。审查明确记录了 ShadowEval 字段重命名、JWT issuer 语义变化、MCP trigger 及新授权语义带来的旧版本不兼容 |

## 合并核验

迁移账本由停写前 157 项增至 213 项，新增 56 项名称和 checksum 均与审查计划一致。所有迁移均完成且未回滚，迁移日志同时显示两项请求日志索引已并发构建完成，最终没有 invalid index

24 次真实请求由 6 次 live、6 次 ingress 和 4 个 Pod 各 3 次组成。所有请求均成功，账单记录数量与请求数量相等，费用均为正值，测试 key 已删除，最终 temporary probe key 为 0

SSE 证据显示 Messages 类以 `message_stop` 结束，Chat 以 `[DONE]` 结束，Responses 以 `response.completed` 结束。工具调用参数可解析为有效 JSON，未发现事件类型不一致

## 上线阻断判断

**存在证据层面的上线阻断，不能给出无条件 GO**

确定阻断是实际生产 writer 权限没有被证明。`missing_writer_select=[]` 不能替代真实 writer 的对象所有权、DDL、索引、触发器、外键引用、函数执行权限及新对象默认授权核验。审查文件也明确记录生产权限未由复核者验证

停写备份属于条件性阻断。备份文件完整性已通过，但缺少直接的写冻结、数据库身份和备份时点绑定证据。若发布门禁要求“停写期间的可恢复生产备份”必须独立证明，应在放行前补齐

逐请求账单的聚合结论通过，但 2 条 Responses 请求尚未完成响应 ID 到账单行的直接字符串级关联。若要求审计级一对一可追溯，该项也应补证

## 回滚边界

迁移完成后不能把旧 1.99.1 镜像视为全功能安全回退目标。ShadowEval 字段重命名会破坏旧接口，JWT issuer 语义变化可能造成旧版本身份匹配风险，MCP 与新授权语义也不会被旧版本完整识别

新版本产生写入后，应优先保留前向 schema，回退应用并关闭不兼容功能。恢复旧数据库必须先停止全部写入，再制定备份之后的 spend、key、model 等变更回放方案，不能自动执行 down、drop 或直接覆盖恢复

最终判定：**运行状态、迁移完成、索引有效、Pod 健康、SSE 和聚合账单证据均通过；writer 权限及停写备份 provenance 尚未闭环，因此发布证据目前不具备无条件放行资格**