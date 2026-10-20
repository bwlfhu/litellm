# 1.106.0 合入计划与验收记录

分析冻结于 2026-10-20，实施前已核对工作区、远程分支与官方精确 tag

| 用途 | 固定引用 |
| --- | --- |
| 定制差异基线 | 官方 v1.99.1，10f4033437df30b91b5dbf2b64711d0a8683fc52 |
| 当前定制源 | litellm_deepseek_history_sse，29532d2608410044f84ac202503a79a6cd60e693 |
| 实际生产补丁源 | 982f2b9b7a28bec184d0289fbc6c5fb9705c017b，1.99.1-custom |
| 升级目标 | 官方 v1.106.0，955bdaafa8b72b2b6ff728048f272fbc06049f63 |
| 原 mainline | dd8582b0ba3ea8452e6bfaba2cb174a280aebba9，缺少后续线上修复 |
| 实施分支 | litellm_v1106_upgrade，从当前定制源创建 |
| 实施工作区 | /home/allcam/projects/litellm-v1106-upgrade |

原工作区 /home/allcam/projects/litellm 保持原分支与干净状态。补齐浅克隆历史后，真实公共祖先为 947dbbf0298eecd3d226f765d4a4fb7ea3fd2551。常规预演有 572 个冲突文件，含 463 个旧 UI 产物；旧发布线相对该祖先另有 657 个文件差异，不能把它们全部视作本机补丁

实施使用正常双亲合并记录，保留当前定制源和官方 tag 历史。合并结果树以官方 1.106.0 为底，再按 v1.99.1..29532d2608 的 112 项净差异逐项迁移。遇到公共函数改名、Anthropic 模块路径移动和测试目录变更，按新接口改写。官方已覆盖的行为保留官方版本；没有删除本机需求的授权

| 分组 | 必须保留的契约 |
| --- | --- |
| 协议与 DeepSeek | 可信部署上下文、原生 Messages 路径、工具历史、真实推理、omit/placeholder/error、思考开关、图片及工具结果图片 |
| SSE 与流处理 | event/data.type 一致；保活、延迟 HTTP、首帧和流中途错误为 Anthropic 格式；Chat 对照保留 OpenAI 格式；取消清理 |
| Responses 桥 | 工具/推理/文本完整，多步索引与完成快照一致，自定义及 namespace 工具可往返 |
| 路由与观测 | 优先级、affinity、cooldown、回退边界、可信元数据隔离、跨实例统计、Redis 故障显式报错 |
| 预算与计费 | 部署价格及 provider 归属、并发窗口 CAS、Redis generation、冷启动与延迟修复不覆盖新窗口支出 |
| 构建与模型目录 | 采用官方锁文件和 Rust/Maturin 构建，保留镜像/代理配置能力和模型兼容性；不得依赖旧 1.99.1 增量基底 |
| 测试与历史材料 | 测试迁往 tests/unit，保留本机回归和官方覆盖；历史基础设施仅归档 |

合入步骤：冻结引用和备份清单；建立独立工作区；重建官方结果树；逐项迁移补丁及测试；检查语法、导入、接口和差异范围；运行相关回归、静态门禁与隔离 HTTP 验证；冻结候选后由子代理逐组核对；修复审查发现并重跑相关检查；提交推送候选分支，满足源码准入后再推进维护分支

数据库 SQL 源码清单从 157 到 213 个，实际待执行数量依环境而定。proxy extras 升至 0.4.108，MCP 2.x、Langfuse 4.x、Maturin/Rust 都属于验证范围。源码合入、镜像构建、数据库副本迁移和生产上线应分别记录结果。当前生产基线仍见 CURRENT_BASELINE.md，不因本文件产生升级事实

本轮禁止把缺失审查、跳过用例或未运行的生产验收记成通过；业务抓取保持关闭。上线前仍需候选镜像源文件一致性、数据库副本迁移与恢复演练、真实 Harness/模型请求和发布窗口验证

## 实施及独立复核结果

最终源码候选冻结于 `efee6c541fe0add9e6fbf7f9fdab1d330b518185`；核心行为测试快照为 `8433fae985165efd7631c96b2606bb2c98241e09`，之后保留 Azure 官方兜底并完成注解/规则注释收尾，本轮双亲合并为 `121f21de7766800d384cb4027383f7164f272503`。后者的父提交精确为当前定制源 `29532d2608` 和官方 `955bdaafa8`；保留官方历史，可按官方 tag 查看本机净差异

相对官方候选有 141 个路径差异，不能用 112 项旧清单替代最终范围。除原始净定制迁移外，还包括 94 个 GenericLiteLLMParams 构造边界在 25 文件中的适配，其中 21 文件不在原清单；另外 15 个路径包括新版 Redis 批量路径、预算计数重播、工具和 affinity 接口、隔离集成测试、生成快照与本升级记录。141 个路径按 105 个原清单净差异、21 个新增参数适配和 15 个其他适配互斥归类；原清单另有 2 项退役和 5 项沿用官方且无净差异。最终文档归档另计，逐路径归属见最终迁移清单

普通 Anthropic 的两项旧 inline-redacted 输出预期由 1.106.0 官方转换和系统消息 placement 契约替代：任意 inline thinking/redacted 均占用 inline 优先位置，普通转换器不额外发送 inline redacted。可信 DeepSeek 继续保留 usable-inline/sidecar 回退、真实 unsigned thinking 和工具内容规则。原生 Messages 的合法 redacted 透传及普通仅 sidecar 的回放仍保留；不能宣称两个旧普通断言完全等价迁移

| 验证 | 实际结果 |
| --- | --- |
| 扩大的原定制及相邻官方单元回归 | 6,838 passed，0 failed，0 skipped，286 warnings |
| 最后类型边界修改后的受影响回归 | 2,466 passed，0 failed，0 skipped，100 warnings；与上一行重叠，不累加 |
| Azure 官方参数边界恢复 | 29 passed，0 failed，0 skipped，3 warnings；另由子代理实调确认非字典兜底和非法关键字保持原行为 |
| 最终隔离 PostgreSQL 18.6 / Redis 8.0.5 | 30 passed，0 failed，0 skipped，8 warnings；验证真实 SQL CAS、Redis Lua、generation 和并发窗口 |
| 普通 Anthropic placement / DeepSeek 中间确认 | 1,263 passed，0 failed，0 skipped，39 warnings；其相关用例已纳入后续验证 |
| UI | 完整 Next 构建及 OpenAPI/TS API 生成已完成，见外部证据 ui-build.log / api-types-generate.log |
| 完整质量门禁 | 最终源码 efee6c541f 完整 make check 为 PASS；以 v1.106.0 为基线，Ruff、类型、纪律、测试质量、UI 及 API 同步检查均通过，不提高阈值 |
| 三组独立子代理审查 | 协议/SSE、Responses、迁移完整性及路由预算计费逐项审查全部通过，无剩余源码阻断；报告与清单归档在 docs/upgrades/v1.106.0 |

证据目录为 `/home/allcam/projects/litellm-v1106-evidence`，各轮失败记录保留。`regressions-acceptance-files.json` 固定最终扩大回归的测试文件范围，XML 记录用例和跳过数。上述单元验证使用合成输入，不冒充真实模型调用

质量门禁命令为 `BASE_REF=v1.106.0 UV_NO_INSTALL_PROJECT=1 make check`。不安装当前项目的环境开关只避免开发环境自动进入 Maturin 构建，不关闭 lint 或类型检查。官方 1.106.0 已采用按基线规则计数的门禁；原指南提到的 `make lint-budget-update` 在该官方版本不存在，已实际尝试确认，因此保留官方门禁和 cap 原值，并按官方基线验证增量，未制造替代放宽预算

本轮未构建候选容器、未验证 Rust 源码构建、未执行生产数据库副本迁移/恢复、未进行真实 Harness 或模型验收、未上线 1.106.0，也未恢复业务抓取。上线前必须另行完成这些发布验证；通过源码合入不能证明历史 SSE 或 XML 故障已在真实业务恢复

旧 docker/Dockerfile.deepseek-vision 已退役。其固定 1.99.1 基底只覆盖部分模块，不能用于 1.106.0；候选必须从根 Dockerfile 完整构建，使用新版 Rust、依赖、Prisma 和 UI 产物。历史镜像及构建配方仍可由旧提交恢复，生产暂不变更


## 冻结引用与维护分支

最终审核按源码快照 `efee6c541fe0add9e6fbf7f9fdab1d330b518185` 进行，后续提交只归档记录。最终提交包括本文件和 `docs/upgrades/v1.106.0/` 下的迁移清单、三组审查报告和验证摘要；这些文档不改变源码快照的行为

候选分支为 `litellm_v1106_upgrade`。所有门禁和逐项审查通过后，先重新 fetch 核对 `origin/mainline`，验证它是候选祖先，再仅以 fast-forward 推进维护分支 `mainline`，并正常推送两条分支；不 force push。原工作区的 `litellm_deepseek_history_sse` 保持 `29532d2608` 且干净

临时合成 PostgreSQL/Redis 已停止，证据仍保存在本机外部目录，未删除既有失败记录。`CURRENT_BASELINE.md` 按历史生产事实保留，后续发布验收完成后再更新
