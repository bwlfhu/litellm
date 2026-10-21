# Current Production Baseline

2026-10-21 已完成生产升级。`mainline` 为长期维护分支，功能修改使用短期 `litellm_<task>` 分支和独立工作区

| 项目 | 当前生产值 |
| --- | --- |
| 官方版本 | `v1.106.0`，`955bdaafa8b72b2b6ff728048f272fbc06049f63` |
| 构建源码 | `f025726ea533d82a94550bf4c911f34184cdd17f` |
| 应用源码验收快照 | `efee6c541fe0add9e6fbf7f9fdab1d330b518185`；后续仅构建修复与记录 |
| 镜像 | `registry.allcam.org/litellm/litellm:v1.106.0-custom.f025726ea5@sha256:448177134508cc45d41a8831d40455a696d1dec1ecb4923fb0be10f396480d98` |
| LiteLLM / proxy extras | `1.106.0` / `0.4.108` |
| 生产 Python | `3.13.16` |
| 生产部署 | `litellm/litellm`，4 个副本就绪、零重启 |
| 数据库 | PostgreSQL 16.14，213 项成功迁移，全部 SQL SHA256 匹配 |
| 正式入口恢复 | `2026-10-21T11:40:44Z`，`https://autocode.allcam.org` |

镜像从根 Dockerfile 完整构建，包含当前依赖、Rust 扩展、Prisma 离线引擎和 UI。四个实例逐一验证版本、Rust 实际调用、78 个定制运行文件 SHA256，全部与固定构建源码一致。历史 `docker/Dockerfile.deepseek-vision` 已退役，不能用于当前版本

生产先排空全部旧副本，再执行 56 项待迁移和两个额外日志索引构建，应用继续保持 `DISABLE_SCHEMA_UPDATE=true`。迁移前后均为 279 个 key、4 个团队、82 个模型、0 个独立预算记录，无无效索引，应用数据库角色为对象所有者及 superuser。生产仍保留原有额外表 `LiteLLM_DeepSeekResponsesSession`，启动检查会提示它不在官方 schema 中；没有执行 DROP，不宣称零 schema drift

生产验收包括切流前及公网入口各 6 次真实协议请求，以及逐副本 Messages/工具调用/历史回放，详情和最终计数见发布记录。Messages 的 SSE event 与 data.type 全部一致，Responses 收到 response.completed，Chat 保留 OpenAI 流结束格式。已逐条核对费用持久化和临时测试 key 清理。公网健康检查和 UI 静态页面均返回 200；未执行浏览器登录或用户现有 Harness 长会话回放，不能据此宣称所有历史 XML/SSE 故障均已恢复

DeepSeek 原生 Messages、可信部署上下文、真实推理/工具历史、图片与工具结果图片、Responses 桥、路由优先级、affinity、跨实例观测、预算窗口 CAS/Lua 和部署计费定制已迁移。缺失历史推理的 `omit` 策略继续生效，不补造空格。Anthropic 保活及流中异常输出顶层 type=error 的同名事件，不附加 OpenAI [DONE]；普通 Chat 继续使用 OpenAI 错误格式。业务抓取保持关闭

协议归因继续沿用 2026-10-16 的更正：直连原生 Messages 的 thinking.type=disabled 对照有效；此前使用 Chat 请求得出的“上游 Messages 关闭思考无效”结论已撤回。历史客户端错误帧未完整捕获，不能把经过模拟验证的错误序列化缺陷或连接超时一概归为上游协议问题

发布后普通业务还观察到 gpt-5.5 Responses 的过载及通用流错误，详见发布记录。这些失败与成功验收分开记录，当前证据没有证明具体通用错误根因或升级回归

源码准入包括 6,838 项扩大回归、其后受影响回归、真实 PostgreSQL/Redis 集成、完整 make check 和三组独立审查，详见 [合入记录](UPGRADE_V1_106_0.md)。这些测试集有重叠，不累加。生产短时观察与 24 小时稳定性观察分开记录，不将未完成的长观察记为通过

停写前后完整备份和隔离恢复演练已完成，原始证据保留在权限受限的 `/home/allcam/projects/litellm-v1106-deploy-evidence`。迁移包含 ShadowEval 列重命名和 JWT issuer 语义变动，恢复写入后不能仅换回旧镜像宣称完整回滚，也不能直接覆盖数据库丢弃新写入。优先保留前向 schema 排障，回退须单独核对应用与数据兼容性

本次发布过程、已知边界和脱敏验收见 [生产发布记录](docs/upgrades/v1.106.0/PRODUCTION_RELEASE.zh-CN.md)。旧版本发布记录保留在 [UPGRADE_V1_99_1.md](UPGRADE_V1_99_1.md)，历史基础设施材料仍见 [归档说明](deploy/INFRASTRUCTURE_ARCHIVE.md)，不属于本次发布
