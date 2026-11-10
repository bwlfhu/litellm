# LiteLLM v1.108.4 生产发布记录

发布日期：2026-11-10

本次将生产从 LiteLLM `v1.106.6` 升级到官方 `v1.108.4`，同时保留 `mainline` 上仍在使用的加密协作、DeepSeek 原生 Messages、Responses、路由、计费、预算和观测定制

| 项目 | 发布值 |
| --- | --- |
| 官方 release commit | `e6f080fb5633aa71ea81b652bd72069aff1f0228` |
| `mainline` 源码 | `acc2805bd23713d0495380b55593e43ac872c875` |
| 镜像 | `registry.allcam.org/litellm/litellm:v1.108.4-custom.acc2805bd2` |
| 镜像 digest | `sha256:76d1cf35674f06a9bf1e2ab289446c7f8145232d04b2eca0961a6bac892267e8` |
| 构建 Job | `litellm/litellm-v1084-build-acc2805bd2` |
| Kubernetes | context `default`，namespace `litellm`，Deployment `litellm` |
| 发布策略 | 4 副本，`maxSurge=1`，`maxUnavailable=0` |
| 数据库迁移 | 独立 v2 resolver Job，213 项到 220 项 |
| 回退镜像 | `v1.106.6-custom.plaintext-collaboration-r2@sha256:4235475a270adc636e4cfab47c1beed6d312180e1cd6da3c97496dcb4e74a6d0` |

升级源码保留 LiteLLM 官方 tag 与内部 `mainline` 两侧 Git 历史。全量镜像使用根 Dockerfile 构建，UI、依赖、Rust 扩展、Prisma 离线引擎及运行时阶段均完成，最终镜像通过 digest 固定。容器内版本为 LiteLLM `1.108.4`、proxy extras `0.4.110`、enterprise `0.1.79.post1`、Python `3.13.18`、MCP `2.3.0`、OpenAI SDK `2.33.0` 和 Pydantic `2.13.4`

升级前创建完整 PostgreSQL 16.14 备份，大小约 3.9 GB。归档通过 `pg_restore --list` 检查，全部备份文件的 SHA-256 校验通过。隔离 PostgreSQL 成功恢复该备份。候选 v2 resolver 在隔离库应用 7 项新增迁移后，220 项 migration checksum 均与发布源码匹配，request-log 索引检查完成。迁移只增加 schema，没有更新或删除已有行

隔离迁移前后，305 个恢复库 key、4 个 team、77 个模型、0 个 budget、578,731 条 SpendLogs 和 `LiteLLM_DeepSeekResponsesSession` 保持不变。原生产镜像连接迁移后的隔离库成功启动，并通过协议兼容测试。用于完整 SpendLogs 逐行排序比对的补充 Job 未完成，因此该项不作为验收通过依据

生产迁移前审计记录 306 个 key、4 个 team、77 个模型、0 个 budget、213 项成功迁移、无失败迁移、无无效索引，以及 604,354 条 SpendLogs。发布验收后记录到 307 个 key，迁移前 306 个 token 身份全部保留。新增 key 没有本次验证 alias，未作删除操作。SpendLogs 和正向费用在验收期间持续增长

生产迁移 Job 仅获取数据库主机、库名和 writer secret，使用 `lock_timeout=10s`、`statement_timeout=600s` 和 v2 resolver。生产代理维持 `DISABLE_SCHEMA_UPDATE=true`。迁移完成后原 4 个副本仍 Ready，随后按 Deployment 的滚动策略替换为新 digest。最终四个 Pod 均 Ready、镜像 digest 一致、重启数为零，公网 readiness 为 `{"status":"healthy","db":"connected"}`

使用线上真实 provider API，升级前和升级后分别验证 Chat、DeepSeek 原生 Messages、Anthropic Messages、Responses、Responses 函数调用及工具结果续聊，以及 Chat、Messages、Responses 流式结束事件。五轮 Responses 全历史回放和 DeepSeek 带推理内容的工具调用往返均成功。新镜像对每个生产 Pod 单独执行 Chat 请求，四次均返回成功。浏览器管理员登录成功，页面错误数为零。线上 `GET /get/latest_release_info` 返回 `v1.108.4`、7 项 fixes 和 1 项 other update，版本提示统计与 release 内容一致。本次验证创建的 key 均按 API 返回值调用 `/key/delete`；期间另有一条无验证 alias 的 key 记录新增，身份无法匹配到本次验证 key，因此保留未动

Git 检查通过 `make check`、`uv lock --check`、Python 编译检查和受影响测试。协议、计费、Responses 桥、路由回退和其他 provider/proxy 测试组分别报告 776、916、2557、1075 和 1096 项通过，测试集有重叠，不累加。路由回退组另有 1 项按现有测试规则跳过。升级前后 Responses `previous_response_id` 的真实上游路径都返回账户能力限制，因此使用完整历史重放验证续聊

回退镜像已在具有新 schema 的隔离数据库上启动并通过同组协议测试。生产数据没有恢复覆盖。本文记录短时发布验收，不代表 24 小时或更长的稳定性观察已完成
