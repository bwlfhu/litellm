# v1.106.2 独立部署核对

核对时间：2026-10-22 UTC；核对源码为 `9991444d4d815cd7c0ac0892904001a3eeb75728`，定制基线 `abadbbd2cc`，官方目标 v1.106.2。本核对只读源码、证据及 Kubernetes 对象，没有修改生产或候选资源、配置、数据库、价格或密钥

## 部署前准入结论

本次候选及生产升级 patch 未发现阻断项，可以按已授权方案执行无数据库迁移的滚动升级。此结论仅为部署准入，生产最终验收仍须在切换后读回四个新 Pod、真实请求、持久化计费和登录配置

| 核对项 | 独立核对结果 |
| --- | --- |
| 最终构建身份 | 构建 Job `litellm-v11062-release-9991444d4d` 于 09:05:25Z 成功，使用固定 digest 的 Kaniko；构建身份记录源码为 `9991444d4d815cd7c0ac0892904001a3eeb75728` |
| 源码归档 | 独立计算 `release-source.tar` SHA256，等于构建记录 `b757a69ca5a33cb091be9a48e1d7cb5a906887eb8faf5ce9c79d842891365a7b` |
| 候选运行镜像 | Deployment 一副本就绪，固定镜像 `registry.allcam.org/litellm/litellm:v1.106.2-custom.9991444d4d@sha256:eb84297cc6ca38dfc9cdcbc67a280db73cfbe7566f8d9b57875300a19c13cbd2` |
| 定制文件 | 独立将 78 项 manifest SHA256 与最终工作区源码比较，再在候选容器读取 78 文件计算，全部匹配 |
| 候选依赖 | 已有运行证据确认 LiteLLM 1.106.2、extras 0.4.108、aiohttp 3.14.5、yarl 1.25.1、gunicorn 23.0.0、diskcache 未安装，Rust 实际函数调用和离线 Prisma engine 检查通过，UI index 存在 |
| 候选真实请求 | `candidate-probe-direct.jsonl` 六次 HTTP 200，包含原生 DeepSeek Messages、强制工具调用、工具历史重放、Chat、Responses、Gemini Messages；Messages event/data type 无错配，Chat/Responses 终止断言通过，临时 key 已删除 |
| 候选计费 | `candidate-accounting.json` 中六条成功记录各有正费用，另有一条失败费用为零；临时 key 剩余数为零。不能将请求结束瞬间的 key/info 汇总费用等同于最终异步持久化结果 |
| 上游限制 | 首次 `candidate-probe.jsonl` 为 code1 上游余额不足导致的 403，不能删去或描述为该路由验证通过。后续六次成功验证使用其他正常线路，不表示 code1 余额问题恢复 |
| 最终代码准入 | `make-check-release.log` 结尾 `check: PASS`，basedpyright 0 errors / 0 warnings / 0 notes。完整广泛回归初次有三项价格目录失败，已按其他独立报告修复并通过受影响的 105 项，不应把原 XML 改称全绿 |
| 数据库迁移 | 独立枚举全部 251 个 schema.prisma 或 migrations 路径，`abadbbd2cc..9991444d4d` 差异为空；正式 v1.106.0..v1.106.2 同样无新增迁移。准备脚本逐项核对生产已成功执行的 213 migration checksum，全匹配，零 invalid index |
| 数据库基线 | 即时准入读取 279 keys、4 teams、82 models、0 budgets；上线普通业务仍会持续写入费用，因此费用计数不要求静态相等 |
| 数据库备份 | `litellm-v11062-online-backup` Job 于 08:12:23Z 成功；独立读取并验证 100 文件的大小及全部 SHA256，合计 2,432,141,798 字节，零不匹配，包含 toc.dat。未执行完整恢复演练，不能写为已验证全量恢复 |
| 当前生产 | 升级前四个旧版 Pod 全部 Ready、零重启，镜像均为旧版 digest 448177134508cc45d41a8831d40455a696d1dec1ecb4923fb0be10f396480d98 |
| 保留管理登录 | 生产 UI_USERNAME/UI_PASSWORD 未设置，PROXY_MASTER_KEY 存在，配置 `disable_env_credential_login` 未启用。新版 `login_utils.py`、`ui_sso.py` 与定制基线相同，仍支持默认 admin + master key；未创建管理账号或关闭现有入口 |
| 升级方式 | `production-upgrade-patch.json` 含 resourceVersion test，实际写入路径仅 metadata annotations、template annotations 及首个容器 image。没有改 replicas/env/envFrom/config mount/service selector/resources/probes/args；保留 `DISABLE_SCHEMA_UPDATE=true` |
| 可用性策略 | 保留 replicas=4、maxSurge=1、maxUnavailable=0、原 startup/readiness/liveness probes、terminationGracePeriodSeconds=600。该策略保障副本准入，不等同于保证任意既有超长流永不中断 |
| 回滚准备 | 生产部署及 template 新 rollback-image 指向刚才的 1.106.0 完整 digest，旧镜像目前四个 Pod 真实运行可用。没有 schema 变更，出现新版本应用回归应先只回滚镜像及身份注解，无需恢复数据库备份，也不能为了回滚覆盖上线后的正常业务写入 |

## 后续验收及准确表述

生产发布后必须确认四副本全部更新为最终 digest，所有 Pod 的 LiteLLM/依赖版本及 78 项文件匹配，默认管理登录仍可用，UI HTML/资产及公网协议入口可用，真实请求的费用持久化且临时 key 已清理。需比较新旧 Deployment 除授权镜像和身份注解之外的 spec，以及 Service selector，防止声明与实际不一致

本次升级可以说明 aiohttp 已检查的安全问题随官方依赖更新获得修复，不能说明所有漏洞清零。gunicorn 23.0.0 公告限制保留于 `REVIEW-SECURITY.md`；实际进程使用 LiteLLM/uvicorn，不能仅凭安装版本认定该 HTTP parser 在公网可利用

候选启动脚本 `launch-candidate.py` 仍记有已删除的旧 scripts ConfigMap 挂载，本次实际 Deployment 已由主会话移除并就绪。若复用该脚本需先清理该挂载；不影响当前最终镜像或生产 patch，不应将此脚本原样用于下次候选启动

## 滚动更新后独立读回

09:54 至 09:57 UTC 独立读取生产资源及新 Pod，确认四个新 Pod `litellm-7c544f69bb-{8gpqv,97td7,tlnk2,xmhxv}` 全部 Ready、零重启，实际 imageID 均为最终 digest `eb84297cc6ca38dfc9cdcbc67a280db73cfbe7566f8d9b57875300a19c13cbd2`。Deployment updated/ready/available 均为 4，generation 113 已被控制器观察，Progressing 为 NewReplicaSetAvailable

我在四个新 Pod 各自读取 78 个运行文件并计算 SHA256，均与最终源码匹配，各自安装 LiteLLM 1.106.2、extras 0.4.108、aiohttp 3.14.5、yarl 1.25.1。生产配置文件在所有新 Pod 的 SHA256 均为 `e683aedccb22e773cd3bcf1ed9e16fbb9478eb172b90ed3522944fad2ac73455`，与旧 Pod 读回相同；UI_USERNAME/UI_PASSWORD 仍未设置，master key 环境变量仍存在，`disable_env_credential_login=false`，`DISABLE_SCHEMA_UPDATE=true`

独立比较即时升级前及升级后 Deployment spec，排除批准的镜像和 template 注解后完全相等；Service spec 亦完全相等。部署及 Pod template 的 source/build/rollback 注解正确，回滚指向原 1.106.0 digest

主会话的四份逐 Pod 真实请求 JSONL 各含 Messages、强制工具和工具历史重放三次 HTTP 200，共 12 次，mismatches 均为空，临时 key 删除均成功。独立 SQL 读回生产为 213 migrations、零未完成或回滚迁移、279 keys、4 teams、82 models、0 budgets、零 invalid index，发布测试 key 剩余为零

初次读回期间仍有一个旧 Pod 在终止宽限期内，已非 Ready，四个新副本均已可用，属于保留 600 秒优雅终止的结果。10:08 UTC 再次确认旧 Pod 全部退出，当前恰好四个新 Pod，全部 Ready、零重启、没有终止中的 Pod

## 最终验收核对

`production-ingress-probes.jsonl` 记录公网入口新增六次成功请求，包括 Messages、工具调用、工具历史、Chat、Responses 和 Gemini Messages，均为 HTTP 200、无 event/data type 错配，临时 key 删除成功。因此本次生产验收为逐 Pod 12 次加公网 6 次，共 18 次成功，候选的六次成功属于另一组证据，不混入生产次数

`production-ui-login-check.json` 记录 UI HTML 200、静态资产 200、现有 admin 登录 200 且角色为 proxy_admin，保留环境凭据登录，没有创建新的 admin，TLS 证书验证启用

独立从全部五份生产请求原始证据提取 call ID、Messages ID 和 Responses ID，再只读查询实际生产 SpendLogs，返回恰好 18 条、18 个不同 call ID、全部 status=success、全部有正费用及 token，费用合计 0.00317868 USD，与主会话 `production-accounting-summary.json` 一致。发布测试 key 剩余为零，不存在本组成功请求的重复费用记录

最终部署核对结论：当前生产的四个可用副本已经运行本次最终 1.106.2 定制镜像，运行源码、配置保留、协议请求、UI/原管理登录及费用持久化均有验证证据，未发现需要回滚的发布阻断项。此结论不宣称 code1 上游余额问题已恢复、不宣称全部依赖安全问题清零，也不把短请求验证扩展为所有旧 Harness 长会话或 XML 溢出案例已经完全解决

## 发布后普通业务异常边界

按要求继续读取四份 `litellm-7c544f69bb-*-startup.private.log`，共 36 处 Traceback，四 Pod 分别 8、8、8、12。它们不是 36 个独立业务故障，重复日志与异常包装不能直接当作请求数或错误率

| Traceback 所处路径 | 记录数 | 已能确认及尚不能确认的范围 |
| --- | --- | --- |
| Responses WebSocket 握手 | 20 | 终端异常均为 `websockets.exceptions.InvalidStatus`，后端握手返回 HTTP 401。失败发生于出站 `websockets.connect`，可以确认后端拒绝握手；不能仅凭 401 判断是上游鉴权规则、线路/凭据选择、协议兼容还是本机发送行为造成 |
| Responses 流错误和 fallback 包装 | 10 | `_maybe_raise_for_error_event` 将收到的 error/response.failed 映射为 `MidStreamFallbackError`，含 upstream_error、过载、HTTP/2 stream failed、temporarily unavailable 等错误。日志同时打印 InternalServerError 是包装关系，不能重复计数。已有数据证明流中收到错误，尚不足以逐例断言网络、本机与上游的最终根因 |
| HTTP 响应错误 | 3 | `MaskedHTTPStatusError` 显示出站 `/v1/responses` 返回 HTTP 403，说明确有失败的业务路径。这三条生产日志不能直接套用首次候选账户余额不足的结论 |
| 路由选择失败 | 3 | `RouterRateLimitError` 具体内容为 no deployments available。可以确认路由当时无可选部署，可能是 cooldown/阻断/其他健康筛选；该类名不等同于已证明用户或上游超过配额，不能笼统写为正常限流 |

独立 AST 比较定制基线和最终源码，`async_responses_websocket`、OpenAI Responses 的 `validate_environment`/`supports_native_websocket`、流的 `_maybe_raise_for_error_event`/`_map_error_event_exception` 完全相同。主会话另核对 websockets/httpx/httpcore/uvicorn 锁定版本未变。旧 1.106.0 的四份 `production-final-observe-litellm-7654c87d46-*.log` 已出现同类 MidStreamFallbackError/InternalServerError，其中包括 overloaded；因此这些类名本身不是新版本回归证据

但是旧观察样本没有这组 WebSocket 401 的可比请求，且新发布的 18 次验收只覆盖 SSE/HTTP，没有执行 WebSocket 成功验收。因此当前既无明确证据证明本次升级引入本机回归，也不能排除路由、鉴权或其他未覆盖场景的本机问题，不应将全部异常归因上游或宣布线上零业务错误。是否存在版本回归需要相同线路、凭据、模型和请求的旧/新版本 WebSocket 对照，当前没有执行该扩展实验

只读 SQL 也确认正常业务时窗内有 failure 费用日志，发布请求全部成功不能外推为全部业务请求成功。短窗口流量构成不同，本报告不以总量变化推断错误率提升、下降或升级收益

## 待提交文档复核

已只读复核 `CURRENT_BASELINE.md`、`docs/upgrades/v1.106.2/PRODUCTION_RELEASE.zh-CN.md` 及 `VALIDATION.json`。版本/源码/digest、四副本状态、18 次生产验收及费用、213 无新增迁移、备份未恢复演练、管理登录保留、旧失败测试记录及 gunicorn 限制，与已核对证据一致

文档明确保留 Responses WebSocket 401 原因未定、SSE 成功不代表 WebSocket 验证通过，未声称全部业务异常属于上游。建议将概括性的“路由限流”更精确地写为“路由无可用部署”，以匹配这三条生产 Traceback。未发现需要阻止本次提交的事实性问题；后续若补写长期稳定性或 WebSocket 已恢复结论，必须新增相应证据
