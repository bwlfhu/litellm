# Current Production Baseline

2026-10-30 已完成生产升级至官方 v1.106.6 加定制修复。`mainline` 为长期维护分支，功能修改使用短期分支和独立工作区

| 项目 | 当前生产值 |
| --- | --- |
| 官方版本 | `v1.106.6`，`61c6ab78393ef45683dd7d963d2a315bd845e20b` |
| 构建源码 | `c801e360824e184700c4a3f2608ad06e8eb68510` |
| 镜像 | `v1.106.6-custom.c801e36082` |
| 镜像 digest | `sha256:31df7ce48abf00a3ce2a742019bef8f4fdc7f40567e2fe4c98dcdd766b8d3219` |
| LiteLLM / proxy extras | `1.106.6` / `0.4.108` |
| Python / mcp / OpenTelemetry | `3.13.17` / `2.2.0` / `1.45.1` |
| 生产部署 | `litellm/litellm`，4 个副本就绪、零重启 |
| 数据库 | PostgreSQL 16.14，213 项成功迁移，全部 SQL SHA256 匹配，本次无 schema 变更 |
| 管理登录 | 保留默认 admin 与 master key 回退，没有新增管理员或关闭环境凭据登录 |

镜像从根 Dockerfile 完整构建，包含依赖、Rust 扩展、Prisma 离线引擎和 UI。四个实例使用同一镜像 digest，容器内版本和关键依赖已核对。历史 `docker/Dockerfile.deepseek-vision` 已退役

本次使用四副本滚动升级，`maxSurge=1`、`maxUnavailable=0`，保留原配置、环境变量、探针、资源和 Service。`DISABLE_SCHEMA_UPDATE=true`，没有执行数据库迁移或可选向量元数据清理。原有额外表 `LiteLLM_DeepSeekResponsesSession` 保留，不宣称零 schema drift。生产 Pod 使用同一镜像 digest，发布后均为 Ready 且重启次数为零

生产验收包括公网 readiness、模型列表、Chat、Anthropic Messages、Responses 及 Responses 工具调用。Responses 返回 `function_call`，Messages 返回 `end_turn`，Chat 和 Responses 均成功。发布后 `LiteLLM_SpendLogs` 持续写入 `anthropic_messages`、`aresponses` 和 `acompletion` 成功记录并包含正向费用。UI 页面和静态资产返回 200，未执行完整浏览器交互或管理员登录流程，也未执行既有 Harness 长会话重放

DeepSeek 原生 Messages、真实推理和工具历史、图片、Responses 桥、路由优先级、affinity、跨实例观测、预算窗口 CAS/Lua 和部署计费定制保留。缺失推理不补造空格，Anthropic 流中异常输出同名 error 事件，不附加 Chat [DONE]。业务抓取保持关闭

协议归因沿用 2026-10-16 更正：直连 Messages 的 `thinking: {type: "disabled"}` 对照有效，旧 Chat 请求不能用于证明上游 Messages 关闭思考无效。短请求成功不能证明所有旧 XML/SSE 故障已恢复

首次候选 code1 请求因上游余额不足返回 403，失败证据保留。成功验收使用其他可用线路，不表示该上游账户已恢复。发布观察还包含业务的过载、路由无可用部署、Responses WebSocket 上游握手 401 及通用流错误，不能宣称线上零错误。WebSocket 握手实现和相关 websockets/httpx 版本未变，仍不能仅凭这些日志认定或排除升级回归

本次依赖验证包含 `mcp 2.2.0`、OpenTelemetry `1.45.1`、`boto3 1.43.117`、`certifi 2026.10.19` 和 `grpcio 1.84.1`。gunicorn 仍为 23.0.0，相关公告限制尚未解决；实际代理使用 LiteLLM/uvicorn，安装版本本身不等于已证明公网可利用。diskcache 没有安装。本次不宣称全依赖漏洞清零

源码通过 `uv lock --check`、Python 语法编译检查，以及重点受影响单元测试和 2 个补充回归测试。OpenTelemetry 组 498 项、提供商组 423 项、代理组 954 项和补充回归 2 项均通过，环境首次使用旧依赖造成的收集错误已在锁定版本环境复测排除。三组源码审查和独立部署核对完成，重叠测试集不累加

本次没有执行数据库迁移，未在本轮重新执行全量恢复演练。相对上一版 1.106.2 没有 schema 变更，应用回退使用原 1.106.2 镜像及对应身份注解，不覆盖上线后的业务数据。不能跨过上一轮迁移直接退回 1.99.1

详细结果见 [本次发布记录](docs/upgrades/v1.106.6/PRODUCTION_RELEASE.zh-CN.md)。上一轮发布历史见 [1.106.2 发布记录](docs/upgrades/v1.106.2/PRODUCTION_RELEASE.zh-CN.md)。短时验收与长期稳定性观察分开记录
