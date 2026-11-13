# Current Production Baseline

2026-11-10 已完成生产升级至官方 LiteLLM `v1.108.4`，保留现有定制并验证数据库、Chat、Messages、Responses、工具调用和管理 UI

2026-11-13 完成 DeepSeek Messages 交错块索引修复，代码已进入 mainline。最新镜像在原生产镜像上仅替换 `inline_thinking.py`，保留现有依赖、静态 UI 和数据库 schema。本次修复、复审与发布证据见 [交错块修复记录](docs/upgrades/v1.108.4/OPEN_BLOCK_FIX.zh-CN.md)。上轮 thinking 修复见 [审查修复记录](docs/upgrades/v1.108.4/THINKING_REVIEW_FIX.zh-CN.md)，下文全量升级与数据库迁移验证来自 2026-11-10

| 项目 | 当前生产值 |
| --- | --- |
| 官方版本 | `v1.108.4`，`e6f080fb5633aa71ea81b652bd72069aff1f0228` |
| `mainline` 运行时代码 | `0a53dbe8f8c16532b0ea62972b9ee35c593924b9` |
| 镜像 | `registry.allcam.org/litellm/litellm:v1.108.4-open-block.0a53dbe8f8` |
| 镜像 digest | `sha256:7e93fe8c7f563e53fbe29ecfe498b4c2b6b36e38275faeebf656785e4ccde624` |
| LiteLLM / proxy extras / enterprise | `1.108.4` / `0.4.110` / `0.1.79.post1` |
| Python / MCP | `3.13.18` / `2.3.0` |
| Kubernetes | `litellm/litellm`，4 个副本 Ready，零重启 |
| 数据库 | PostgreSQL 16.14，220 项成功迁移，无失败迁移或无效索引 |
| 应用启动迁移 | `DISABLE_SCHEMA_UPDATE=true`，迁移通过独立 Job 执行 |
| 本次补丁回退镜像 | `v1.108.4-thinking-reviewed.0e42eb7bc2@sha256:3dcb78d10130df42fb79ee94deea0a6752b8f8b540945e1460095dbd37fd2e8a` |

基础镜像由根 Dockerfile 完整构建，含 Python 依赖、Rust 扩展、Prisma 离线引擎和重新构建的 UI。thinking 修复继承这些内容。四个 Pod 使用同一镜像 digest。发布采用 `maxSurge=1`、`maxUnavailable=0`

迁移前创建了完整 PostgreSQL 备份，大小约 3.9 GB，`pg_restore --list` 和每个备份文件的 SHA-256 校验均通过。备份保存在部署主机的升级证据目录，未加入仓库

生产数据库从 213 项迁移增加到 220 项，新增的 7 项迁移均为 schema 变更，未包含行数据重写。所有 220 个已完成迁移的 SQL checksum 与候选源码一致。隔离库从生产备份恢复成功，候选迁移后 key、team、model 行数和身份指纹未变，SpendLogs 数量保持 578,731，定制表 `LiteLLM_DeepSeekResponsesSession` 保留

生产迁移前有 306 个 key、4 个 team、77 个模型和 0 个 budget。发布验收期间 key 表新增 1 条记录，306 个原有 token 身份全部保留。新增记录没有本次验证使用的 key alias，因此保留未删除。生产 SpendLogs 从迁移前的 604,354 条持续增长；验收后的 `acompletion`、`anthropic_messages` 和 `aresponses` 成功记录均包含正向费用

升级前后均使用线上最新模型进行了真实 API 验收。Chat、DeepSeek 原生 Messages、Anthropic Messages、Responses、Responses 函数调用和工具结果续聊，以及三种流式协议都成功。五轮 Responses 历史回放和 DeepSeek 推理工具往返成功。每个生产 Pod 都单独完成 Chat 请求，浏览器实际登录 Admin UI 成功且没有页面异常

仓库 `make check`、`uv lock --check` 和 `python -m compileall -q litellm enterprise` 通过。严格 lint、类型纪律、测试质量、OpenAPI 文档、UI lint 和 API 类型同步均通过。受影响测试组报告 776、916、2557、1075 和 1096 项通过，另有 1 项按测试规则跳过；测试集有重叠，不累加。候选镜像在迁移后的隔离数据库上启动正常，原生产镜像也在该 schema 上通过了回退兼容验证

真实上游不支持该账户使用 `previous_response_id` 的 HTTP 请求模式，因此续聊验收按客户端实际方式回传完整历史。此前短时健康状态和本次验收结果不能代表长期稳定性，发布后的观察与已完成验收分开记录

详细结果见 [v1.108.4 生产发布记录](docs/upgrades/v1.108.4/PRODUCTION_RELEASE.zh-CN.md)。上一版记录见 [v1.106.6 生产发布记录](docs/upgrades/v1.106.6/PRODUCTION_RELEASE.zh-CN.md)
