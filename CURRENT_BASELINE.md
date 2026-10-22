# Current Production Baseline

2026-10-22 已完成生产升级至官方 v1.106.2 加定制修复。`mainline` 为长期维护分支，功能修改使用短期分支和独立工作区

| 项目 | 当前生产值 |
| --- | --- |
| 官方版本 | `v1.106.2`，`49943cd52ea46aacf8db2e5cb2b890d580fe9bb4` |
| 构建源码 | `9991444d4d815cd7c0ac0892904001a3eeb75728` |
| 镜像 | `v1.106.2-custom.9991444d4d` |
| 镜像 digest | `sha256:eb84297cc6ca38dfc9cdcbc67a280db73cfbe7566f8d9b57875300a19c13cbd2` |
| LiteLLM / proxy extras | `1.106.2` / `0.4.108` |
| Python / aiohttp / yarl | `3.13.16` / `3.14.5` / `1.25.1` |
| 生产部署 | `litellm/litellm`，4 个副本就绪、零重启 |
| 数据库 | PostgreSQL 16.14，213 项成功迁移，全部 SQL SHA256 匹配，本次无 schema 变更 |
| 管理登录 | 保留默认 admin 与 master key 回退，没有新增管理员或关闭环境凭据登录 |

镜像从根 Dockerfile 完整构建，包含依赖、Rust 扩展、Prisma 离线引擎和 UI。四个实例逐一验证版本、Rust 实际调用、78 个定制运行文件 SHA256，全部与固定构建源码一致。历史 `docker/Dockerfile.deepseek-vision` 已退役

本次使用四副本滚动升级，`maxSurge=1`、`maxUnavailable=0`，保留原配置、环境变量、探针、资源和 Service。`DISABLE_SCHEMA_UPDATE=true`，没有执行数据库迁移或可选向量元数据清理。原有额外表 `LiteLLM_DeepSeekResponsesSession` 保留，不宣称零 schema drift

生产验收包括逐副本 Messages、工具调用和历史回放共 12 次，以及公网 Messages、工具、历史、Chat、Responses、Gemini 共 6 次，全部成功。Messages 的 SSE event 与 data.type 一致，Responses 收到 response.completed，Chat 保留 [DONE]。18 笔成功费用逐条持久化，call ID 无重复，测试 key 已清理。UI、静态资产及现有管理员 HTTP 登录均成功，未执行完整浏览器交互或既有 Harness 长会话重放

DeepSeek 原生 Messages、真实推理和工具历史、图片、Responses 桥、路由优先级、affinity、跨实例观测、预算窗口 CAS/Lua 和部署计费定制保留。缺失推理不补造空格，Anthropic 流中异常输出同名 error 事件，不附加 Chat [DONE]。业务抓取保持关闭

协议归因沿用 2026-10-16 更正：直连 Messages 的 `thinking: {type: "disabled"}` 对照有效，旧 Chat 请求不能用于证明上游 Messages 关闭思考无效。短请求成功不能证明所有旧 XML/SSE 故障已恢复

首次候选 code1 请求因上游余额不足返回 403，失败证据保留。成功验收使用其他可用线路，不表示该上游账户已恢复。发布观察还包含业务的过载、路由限流、Responses WebSocket 上游握手 401 及通用流错误，不能宣称线上零错误。WebSocket 握手实现和相关 websockets/httpx 版本未变，仍不能仅凭这些日志认定或排除升级回归

aiohttp 已更新到 3.14.5，包含已核对的安全修复，yarl 更新到 1.25.1。gunicorn 仍为 23.0.0，相关公告限制尚未解决；实际代理使用 LiteLLM/uvicorn，安装版本本身不等于已证明公网可利用。diskcache 没有安装。本次不宣称全依赖漏洞清零，正式 1.107.0 在本次核对时尚未发布

最终源码完整 `make check` 通过。广泛回归初次 15,325 passed、3 failed、2 skipped，三项价格目录问题修复后受影响 105 项通过，旧失败记录保留。三组源码审查和独立部署核对完成，重叠测试集不累加

本次在线数据库备份完成，100 个文件全部哈希核对，未在本轮重新执行全量恢复演练。相对上一版 1.106.0 没有 schema 变更，应用回退使用原 1.106.0 镜像及对应身份注解，不覆盖上线后的业务数据。不能跨过上一轮迁移直接退回 1.99.1

详细结果见 [本次发布记录](docs/upgrades/v1.106.2/PRODUCTION_RELEASE.zh-CN.md)。上一轮迁移历史见 [1.106.0 发布记录](docs/upgrades/v1.106.0/PRODUCTION_RELEASE.zh-CN.md)，原始私有证据位于 `/home/allcam/projects/litellm-v11062-evidence`。短时验收与长期稳定性观察分开记录
