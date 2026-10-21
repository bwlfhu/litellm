# 1.106.0 生产发布记录

2026-10-21 已完成从 1.99.1-custom 到 1.106.0-custom 的生产升级，正式入口 `https://autocode.allcam.org` 已恢复

| 身份 | 固定值 |
| --- | --- |
| 官方基线 | `v1.106.0`，`955bdaafa8b72b2b6ff728048f272fbc06049f63` |
| 构建源码 | `f025726ea533d82a94550bf4c911f34184cdd17f` |
| 构建 Job | `litellm-v1106-build-r5-f025726ea5` |
| 生产镜像 | `registry.allcam.org/litellm/litellm:v1.106.0-custom.f025726ea5@sha256:448177134508cc45d41a8831d40455a696d1dec1ecb4923fb0be10f396480d98` |
| 软件包 | LiteLLM `1.106.0`，proxy extras `0.4.108`，Python `3.13.16` |
| 旧镜像 | `registry.allcam.org/litellm/litellm:v1.99.1-custom.982f2b9b7a-anthropic-errors@sha256:810d7b77bcd434897d069d0fd4c32eded541672ebc2595b25a2f3a661b417439` |
| 目标部署 | namespace `litellm`，Deployment `litellm`，四副本 |

源码准入冻结于 `efee6c541f`。之后应用源码不变，仅归档记录并用三个 Dockerfile 提交修复 apk 有界重试、独立阶段代理和 liteadmin 下载包校验。完整镜像包含新构建的 Rust 扩展和 UI，不依赖旧 1.99.1 增量镜像；下载和早期构建失败日志均保留，没有将失败轮次改记为成功

## 发布前验证

最终镜像版本、Prisma CLI/引擎离线可用、Rust 动态库依赖及实际函数调用均通过，78 个定制运行文件 SHA256 与构建源码全部一致。隔离 NetworkPolicy 下公网连接失败，镜像仍能对恢复后的生产克隆完成全部 56 项迁移及两个日志索引构建，213 项迁移账本与 SQL checksum 一致

生产在线自定义格式备份为 1,833,943,934 字节，SHA256 为 `f7a093012ad28285a72418718bca477eab97dfad9d495bfdbe6300796c5462fc`。另有目录格式在线备份和最终停写备份；停写备份含 77 个文件、共 2,473,561,591 字节，每文件 SHA256 记录在私有 `production-final-backup-identity.json`。隔离克隆完整恢复演练成功，用时 774.61 秒，恢复后旧 157 项迁移及业务计数与对应备份一致。克隆备份时为 278 个 key，生产最终停写时为 279 个，两个时点不能混用

候选真实验收覆盖 Messages 简单回答、强制工具调用和历史回放、Chat、Responses、Gemini 共 6 次成功请求，另外 5 条部署线路的 Messages 验收成功。初轮另一部署返回上游 INSUFFICIENT_BALANCE 403，旧生产对该已暂停部署返回 503，保留为现存路由/余额边界，没有修改该路由或将该失败计入通过

隔离 HTTP 故障验证确认延迟 503 和流中断均输出类型一致的 Anthropic error 事件且无 [DONE]，Chat 对照保留 OpenAI 格式。故障脚本在 finally 的子进程终止操作遇到 PermissionError，功能结果有效但脚本整体不能记为退出成功；临时 inspect Pod 在发布后清理

## 生产执行

| UTC 时间 | 操作与实际结果 |
| --- | --- |
| `11:09:15` | 切入口至维护响应，暂停日志清理任务，旧应用缩至零并确认写连接排空 |
| `11:12:27` 起 | 最终停写备份成功，77 个文件均记录大小与 SHA256 |
| `11:22:33` 至 `11:23:18` | 独立生产迁移 Job 成功，56 项迁移和两个额外日志索引完成 |
| 迁移后 | 213 项成功账本及全部 SQL SHA256 匹配，无 invalid index；数量保持 279 key、4 team、82 model、0 budget |
| 切流前 | 新版单副本真实 6 请求成功，扩至四副本，逐一验证版本/Rust/关键源码及零重启 |
| `11:40:44` | 恢复原 Service selector，四个就绪 endpoint，恢复日志清理 CronJob；公网 readiness 返回 200 |
| 发布复核后 | 更正 Pod 模板残留的旧 build-job 注解，同一镜像滚动更新，再验证最终四副本与工具往返 |

维护窗口约 31 分 28 秒。维护服务最初因端口缺少 http 名称短暂无 endpoint，补齐后返回 HTTP 503、Anthropic 错误体和 Retry-After；该短暂入口中断属于本次维护过程，不隐去

生产变更镜像与发布身份注解，恢复后 Deployment spec 的其余配置与发布前一致。数据库更新由单独 Job 执行，应用保持 DISABLE_SCHEMA_UPDATE=true。没有改动模型配置、凭据、预算限额和业务抓取设置

## 线上验收

| 检查 | 实际结果 |
| --- | --- |
| 副本身份 | 最终四个 Pod 全部 Ready，零重启，同镜像 digest；LiteLLM/extras/Rust 一致，构建任务注解正确 |
| 完整定制源码 | 每个 Pod 的 78 个运行文件与冻结源码 SHA256 全匹配 |
| 切流前真实请求 | 6/6 成功，覆盖 Messages、工具调用/回放、Chat、Responses、Gemini |
| 公网入口真实请求 | TLS 校验开启，6/6 成功，覆盖同一协议集合 |
| 逐副本 Messages | 首轮四副本各 3 请求，12/12 成功；更正注解后最终四副本再次各 3 请求，12/12 成功 |
| SSE | 32 次 Messages 的 302 帧 event 等于 JSON data.type，无 [DONE]；Responses 收到 response.completed；Chat 正常结束 |
| 费用持久化 | 36 个不同 call ID 对应 36 条 success 日志，费用均大于零，合计 `0.00473028` |
| 临时 key | finally 删除成功，最终测试 key 数为零，总 key 数恢复至 279 |
| 入口与 UI | readiness 为 healthy/db connected；公网 UI HTML、3 个 JavaScript 和 1 个 CSS 资源均返回 200，类型正确 |

首次容器内公网探针因无法信任入口证书而在创建测试 key 前失败；使用节点已有 CA 证书链重跑后全程验证 TLS 并成功。没有使用 verify=false，也没有更改生产证书或容器配置。该失败记录保留为 `production-ingress-probes.stderr.log`，通过记录为 r2

逐副本即时 key/info 中部分 spend 为零属于验收时点观察，不能单靠该字段声称费用已计入；后续直接核对持久化日志，每次请求均须有非零费用。观察中的两条鉴权 ERROR 来自同一现有 key 已超过原有 300 预算限额，未为发布放宽预算

后续普通业务流量在记录的 20 分钟窗口内出现 4 次 gpt-5.5 Responses 流错误，其中两次消息明确为服务器过载，两次为通用 Response API in-stream error。堆栈进入原生 Responses 的 _maybe_raise_for_error_event，仅在收到 error/response.failed 时触发；没有捕获这些请求的客户端原始帧，不能进一步断言具体根因或宣称它们与升级完全无关。这 4 次业务失败不在 36 次成功验收请求内，未为此改动模型路由或预算

原生产配置未启用 Prometheus callback，认证后的 /metrics 返回 404；本次没有把 Prometheus 检查记为成功，也没有为了验收增加监控配置。已核对 Kubernetes 状态、资源用量、应用日志、真实请求和数据库费用记录

## 独立复核与边界

[发布后子代理复核及问题关闭记录](review-production-rollout.zh-CN.md)逐项确认 213 项迁移和 SQL checksum、四副本身份、78 个运行文件、首轮 24 次真实请求及费用持久化、测试 key 清理和入口恢复。其指出的旧生产基线文档与残留 build-job 注解均在收尾更正；指标 404、超预算拒绝和长会话边界如实保留。另一次独立 CLI 审查的首轮因隔离环境无法启动而没有读到证据，保留为未通过审查；后续使用可读取证据的环境重新审查，不将工具失败记为验收成功

实际 writer 查询功能成功，但临时 Prisma 客户端断连时出现 PermissionError，脚本整体按失败保留。已精确终止该探针的独立 query engine，原应用 engine 保留；探针进程处于退出后的 zombie 状态等待 PID 1 回收。清理后于 2026-10-21T13:14:01Z 再验四副本就绪、零重启、数据库 connected、公网 readiness 200

原有非官方表 LiteLLM_DeepSeekResponsesSession 被保留，启动 schema 检查可能提示 DROP TABLE 建议，但未执行 DROP，不宣称零 drift。迁移前 writer 为数据库对象 owner/superuser；迁移后读权限检查、新版写入费用及管理 key 创建/删除均成功

迁移包含 ShadowEval 列重命名及 JWT issuer 语义变化，不允许旧新版本长期混跑。正式流量恢复后不能仅切旧镜像宣称完整回滚，也不能直接用停写备份覆盖已产生的新写入。发生故障优先保留前向 schema 修复；必要回退须单独评估功能兼容、期间数据和维护窗口

本次以真实 HTTP 模型请求验收，未在用户既有 DeepSeek Harness 会话中复现长历史 XML 溢出，也未完成浏览器登录交互或 24 小时稳定性观察。真实短请求、源码修复和模拟故障验证支持本次升级发布，不能替代所有旧会话恢复的证明。业务抓取保持关闭

[最终脱敏验收摘要](PRODUCTION_VALIDATION.json)记录 36 次成功请求、302 帧 SSE、费用及最终四副本身份，未完成项目显式为 false

原始日志、完整备份、配置快照及请求体保留在权限受限的 `/home/allcam/projects/litellm-v1106-deploy-evidence`，不随源码提交。仓库仅记录脱敏结论与发布身份
