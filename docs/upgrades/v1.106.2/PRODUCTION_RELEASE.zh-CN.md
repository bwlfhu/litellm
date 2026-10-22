# 1.106.2 合入及生产发布记录

2026-10-22 已完成合入、完整镜像构建、候选验证及四副本生产滚动升级。当前运行官方 v1.106.2 加保留的定制修复

## 版本与安全结论

官方 [v1.106.2](https://github.com/BerriAI/litellm/releases/tag/v1.106.2) 发布于 2026-10-21T10:50:35Z。初始核对正式 v1.107.0 的 GitHub release、tag 和 PyPI 精确版本均不存在，只有开发及候选版本。部署前再次查询远端仍没有正式 tag，PyPI 精确版本仍返回 404，GitHub API 再查受到限流。因此选择正式 1.106.2，不部署 1.107.0 预发布版

官方 [#47736](https://github.com/BerriAI/litellm/pull/47736) 将 aiohttp 3.14.3 更新至 3.14.5，yarl 从 1.23.0 更新至 1.25.1。已核对 aiohttp 的八项公告，包括 CVE-2026-111066；目标版本超过 3.14.4 修复边界。生产四个实例均实测安装新版依赖

不能据此宣称所有漏洞清零。原 1.106.0 已不在所核对 LiteLLM 自身 critical 公告的受影响范围，不能说这些漏洞本次才修复。gunicorn 仍为 23.0.0，AIKIDO-2026-10742 声明修复边界为 26.0.0，本次官方版本没有解决。实际进程使用 LiteLLM/uvicorn，未证明 gunicorn parser 在当前公网入口可利用。diskcache 没有安装，但锁文件仍含其可选受影响版本。来源及限制见 [安全审查](REVIEW-SECURITY.md)

## 基线与合并过程

| 用途 | 固定身份 |
| --- | --- |
| 原工作区 | `/home/allcam/projects/litellm`，保留 `litellm_deepseek_history_sse` / `29532d2608` |
| 本次工作区 | `/home/allcam/projects/litellm-v11062-upgrade` |
| 实施分支 | `litellm_v11062_upgrade` |
| 定制基线 | `origin/mainline`，`abadbbd2ccf0f94a845b4cd225f4c5272f991678` |
| 官方公共基线 | `v1.106.0`，`955bdaafa8b72b2b6ff728048f272fbc06049f63` |
| 合入来源 | 官方 `v1.106.2`，`49943cd52ea46aacf8db2e5cb2b890d580fe9bb4` |
| 双亲合并 | `ae62819049383fe9dc2f95fcda4e8fda0c7a450e` |
| 最终构建源码 | `9991444d4d815cd7c0ac0892904001a3eeb75728` |
| 镜像 tag | `v1.106.2-custom.9991444d4d` |
| 镜像 digest | `sha256:eb84297cc6ca38dfc9cdcbc67a280db73cfbe7566f8d9b57875300a19c13cbd2` |

从最新维护分支创建独立工作区，把官方精确 tag 正常 merge 进定制基线。官方两个版本有 213 个文件变化，其中 41 个与定制交叠，12 个冲突逐项解决；没有整体覆盖定制或重写官方历史

Responses 采用官方单 choice 输出，同时保留多个 reasoning item 的顺序、encrypted_content、工具和注释。普通 Anthropic 保留有签名的空 thinking，DeepSeek 保留真实非空 unsigned thinking 并过滤空白占位。原生 Messages 路径、SSE 类型规范化、error/keepalive 和可信部署上下文保持

独立审查发现 DeepSeek 重试覆盖方法遗漏官方新增的 request_body 参数，以及原生工具规范化导入遗漏，已在 fb1a245e45 修复。最终提交删除五个重复价格 key 并重新生成 schema；修复前后 json.loads 实际生效值完全相同，没有改变业务价格

预算 CAS/Lua、Redis generation、部署计费隔离、affinity 和官方费用日志改动已逐项核对。所有 schema.prisma 及 migrations 路径与定制基线完全相同，无需迁移。详见 [协议审查](REVIEW-PROTOCOL.md) 和 [计费路由审查](REVIEW-ACCOUNTING.md)

## 验证与独立核对

| 范围 | 实际结果 |
| --- | --- |
| 121 文件广泛回归 | 15,325 passed、3 failed、2 skipped；失败均为价格重复 key / schema，不改写旧 XML 为全绿 |
| 最后价格修复的受影响回归 | 105 passed，三项失败问题已消除，实际生效价格不变 |
| DeepSeek Messages | 259 passed，包括新版错误处理参数适配 |
| 独立协议检查 | 1,201 passed，另有三组转换边界检查；与其他测试集重叠，不累加 |
| 真实 PostgreSQL batch 集成 | 3 passed、9 deselected |
| 最终源码完整 make check | PASS，Python、类型、纪律、测试质量、UI 和 API 同步检查通过，未放宽门禁 |
| 候选真实请求 | 六次成功，涵盖 Messages、强制工具、工具历史、Chat、Responses、Gemini；六笔正费用落库 |
| 生产逐实例请求 | 四个实例各三次 Messages/工具/历史，共 12 次成功 |
| 生产公网请求 | 六次真实协议请求全部成功，TLS 校验启用 |
| 生产计费 | 18 笔请求对应 18 条记录、18 个唯一 call ID，全部正费用，共 0.00317868 USD，测试 key 为零 |
| UI 和原管理登录 | HTML 200、静态资产 200、现有 admin 登录 200，角色为 proxy_admin，未新增管理员 |
| 独立部署审查 | 备份、源码、镜像、配置、四副本、协议、计费、登录逐项核对，见 [报告](REVIEW-DEPLOYMENT.md) |

最终质量命令为 `BASE_REF=v1.106.2 UV_NO_INSTALL_PROJECT=1 LITELLM_LOCAL_MODEL_COST_MAP=True make check`，日志为 make-check-release.log。价格修复前的门禁记录没有冒充最终检查；早期依赖及旧编辑安装路径失败也保留

Messages 逐帧验证 event 等于 data.type，包含 message_stop，无 Chat [DONE]。工具首轮与历史回放成功；Chat 收到 [DONE]，Responses 收到 response.completed。没有执行用户既有 Harness 长会话重放或完整浏览器操作，不能宣称全部旧 XML/SSE 故障已恢复

首个候选 code1 请求因上游 INSUFFICIENT_BALANCE 返回 403，失败和零费用记录保留。后续成功验收使用其他可用线路，不表示 code1 已恢复或该账户已验证成功

## 构建、备份与部署

根 Dockerfile 完整构建 UI、Python 依赖、Rust 扩展及 Prisma 离线引擎。构建 Job litellm-v11062-release-9991444d4d 于 09:05:25Z 成功，源码归档 SHA256 为 b757a69ca5a33cb091be9a48e1d7cb5a906887eb8faf5ce9c79d842891365a7b。上线使用最终 digest，没有使用探索构建

在线 PostgreSQL 目录备份于 08:12:23Z 完成，100 文件共 2,432,141,798 bytes，独立复算全部 SHA256 一致。本轮未重新执行全量恢复演练，不能把哈希检查称为全量恢复通过

上线前逐项核对 213 migration checksum，业务数量为 279 keys、4 teams、82 models、0 budgets，零 invalid index。没有 schema 改动，不执行迁移或可选向量元数据清理，保持 DISABLE_SCHEMA_UPDATE=true。原有额外 DeepSeekResponsesSession 表保留，不宣称零 schema drift

生产 patch 含 resourceVersion 检查，仅修改镜像及 Deployment/Pod template 发布注解。保留四副本、maxSurge=1、maxUnavailable=0、配置、环境变量、探针、资源、600 秒优雅终止和 Service。四个新副本就绪后旧 Pod 在原宽限期内退出，不承诺任意超长流永不受中断

四实例分别核对最终 imageID、LiteLLM 1.106.2、extras 0.4.108、aiohttp 3.14.5、yarl 1.25.1、78 个定制文件哈希、Rust 实际调用及离线引擎，全部通过。Deployment 除批准镜像和注解外 spec 完全相等，Service spec 完全相等，四副本 Ready、零重启

管理环境凭据登录继续启用，不新增 Proxy Admin，官方提示横幅会继续存在。业务抓取保持关闭。本次候选 Deployment、Service、Secret、专用数据库和测试 key 已清理，共享验证资源保留。原始请求、配置密钥、备份和日志只存权限受限证据目录

## 回滚及观察边界

应用回滚目标为上一版 v1.106.0-custom.f025726ea5，digest sha256:448177134508cc45d41a8831d40455a696d1dec1ecb4923fb0be10f396480d98，同步恢复 source/build 注解。两个版本 schema 相同，不应恢复备份覆盖正常新写入，不能跨越上一轮迁移直接退回 1.99.1

短时观察存在普通业务的上游过载、路由限流、Responses WebSocket 上游握手 401 及通用流错误。WebSocket 握手实现、websockets 15.0.1、httpx 0.28.1 和 httpcore 1.0.9 相对上一版未变；SSE Responses 成功不等于 WebSocket 验证成功。发布请求成功不等于零业务错误，现有证据不足以把全部错误归为上游或认定、排除升级回归。长期稳定性和原始长会话仍需分别观察

重新拉取远程、确认维护分支是升级分支祖先后，正常 fast-forward 推送维护分支及实施分支，不使用 force。当前基线见 [CURRENT_BASELINE.md](../../../CURRENT_BASELINE.md)，原始证据为 `/home/allcam/projects/litellm-v11062-evidence`
