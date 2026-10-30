# LiteLLM v1.106.6 生产发布记录

发布日期：2026-10-30

本次从生产 `v1.106.2` 升级到官方 `v1.106.6`，保留现有 DeepSeek、Anthropic、SSE、路由、计费和部署定制。官方 tag commit 为 `61c6ab78393ef45683dd7d963d2a315bd845e20b`，合入后的 `mainline` 提交为 `c801e360824e184700c4a3f2608ad06e8eb68510`

| 项目 | 发布值 |
| --- | --- |
| 镜像 | `registry.allcam.org/litellm/litellm:v1.106.6-custom.c801e36082` |
| 镜像 digest | `sha256:31df7ce48abf00a3ce2a742019bef8f4fdc7f40567e2fe4c98dcdd766b8d3219` |
| 构建 Job | `registry/litellm-build-c801e36082-v2`，1 次成功 |
| Kubernetes | context `default`，namespace `litellm`，Deployment `litellm` |
| 发布策略 | 4 副本，`maxSurge=1`，`maxUnavailable=0` |
| 数据库迁移 | 未执行，`DISABLE_SCHEMA_UPDATE=true` |
| 回退镜像 | `v1.106.2-custom.9991444d4d@sha256:eb84297cc6ca38dfc9cdcbc67a280db73cfbe7566f8d9b57875300a19c13cbd2` |

构建使用官方 Kaniko `v1.23.2-debug` 固定 digest，从 GitHub 的不可变提交归档构建。构建保留生产所需的 NPM、UV、APK 和代理参数，构建日志显示 UI、Python 依赖、Rust 扩展、Prisma 离线引擎和运行时镜像均成功完成，最终 digest 已写入 Deployment 并由 4 个 Pod 一致使用

代码门禁包括 `uv lock --check`、`python -m compileall -q litellm enterprise`，以及三组受影响测试。代理组 954 项通过，OpenTelemetry 和 GCS/Langfuse 组 498 项通过，DeepSeek/Anthropic 组 423 项通过，补充回归 2 项通过。验证环境最初因旧版 `mcp`、OpenTelemetry 和 Langfuse 收集失败，切换到锁文件要求的 `mcp 2.2.0`、OpenTelemetry `1.45.1` 和 Langfuse `4.18.0` 后复测通过

发布后 4 个 Pod 均 Ready，重启次数均为零，Deployment `4/4`，readiness 返回 `{"status":"healthy","db":"connected"}`。容器内版本核对为 LiteLLM `1.106.6`、MCP `2.2.0`、OpenTelemetry `1.45.1`、boto3 `1.43.117`、certifi `2026.10.19` 和 grpcio `1.84.1`，关键类型 `CacheableResult` 和 `ReadableLogRecord` 可导入

公网 `https://autocode.allcam.org` 验证了 readiness、模型列表、Chat Completions、Anthropic Messages 和 Responses。Messages 返回 `end_turn`，Responses 返回 `completed`，Responses 工具调用返回 `function_call` 及 `get_status({"value":"current"})`。Chat 工具调用在一个推理模型上返回模型能力限制的 400，Messages 强制工具选择也返回上游不支持该模式的 400，这两项已用 Responses 工具路径验证成功，属于模型能力边界而非服务发布失败

发布后数据库 `LiteLLM_SpendLogs` 持续出现成功的 `anthropic_messages`、`aresponses` 和 `acompletion` 记录，均有正向 spend。UI 页面和静态资产返回 200，现有 Service、Ingress、环境变量、探针、资源限制和管理登录配置保持不变。未执行完整浏览器交互、管理员登录流程、长时间稳定性观察或数据库恢复演练

回退使用发布前镜像及 Deployment 注解中的 rollback image，不执行数据库回退或覆盖上线后的业务数据
