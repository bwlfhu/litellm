# 1.106.2 独立安全与发布核对

核对时间：2026-10-22 UTC。仅执行只读源码、公开公告和当前生产依赖查询，本报告不修改源码或生产配置

结论：可以将正式升级目标定为官方 v1.106.2。没有发现正式 v1.107.0 发布；不能将本次升级描述为“1.107.0 修复最新严重 CVE”。1.106.2 确实带入当前生产尚未安装的 aiohttp 安全修复和 yarl 更新，但不能据此宣称整个依赖栈漏洞清零

## 发布身份

独立访问 GitHub release `/repos/BerriAI/litellm/releases/tags/v1.107.0` 和 tag ref `/repos/BerriAI/litellm/git/ref/tags/v1.107.0` 均返回 HTTP 404；PyPI `/pypi/litellm/1.107.0/json` 也返回 HTTP 404

PyPI 项目元数据的 `info.version` 为 1.106.2。存在 `1.107.0.dev1`、`dev2`、`dev3`、`rc1`、`rc2`，它们属于开发或候选版本，不是正式 1.107.0

官方 [v1.106.2 release](https://github.com/BerriAI/litellm/releases/tag/v1.106.2) 于 2026-10-21T10:50:35Z 发布。已保存的 `release.json` 列出 #47742、#47736、#47695；#47736 明确说明稳定分支依赖 CVE 修复，aiohttp 从 3.14.3 更新至 3.14.5，同时声明 diskcache 5.6.3 没有修复版

## 依赖和风险

独立只读查询当前生产 Pod `litellm-7654c87d46-n6nhw` 的 Python 包元数据，得到 LiteLLM 1.106.0、aiohttp 3.14.3、yarl 1.23.0、gunicorn 23.0.0。diskcache 不在生产镜像中，查询抛出 `PackageNotFoundError`；该包是源码 `caching` 可选 extra，不属于当前生产已安装依赖

| 组件 | 当前生产 | 目标 uv.lock | 核对结论 |
| --- | --- | --- | --- |
| aiohttp | 3.14.3 | 3.14.5 | 带入公开安全修复 |
| yarl | 1.23.0 | 1.25.1 | 超过 Aikido 公告的 1.24.0 修复边界 |
| gunicorn | 23.0.0 | 23.0.0 | 未随本次升级修复，不能标记已解决 |
| diskcache | 未安装 | 5.6.3，仅可选依赖 | 锁文件仍含受影响版本，当前镜像没有该包 |

OSV 按精确版本查询 aiohttp 3.14.3 返回以下 8 项；3.14.5 返回空漏洞集合。这表示已检查记录中目标版本不受影响，并非完整镜像扫描或未来漏洞保证

| 公告 | CVE | 描述 | 修复边界 |
| --- | --- | --- | --- |
| GHSA-2g87-pp6c-29x5 | 未分配 | 请求导致内存或 CPU 无界消耗，GitHub reviewed HIGH | 3.14.4 |
| GHSA-494j-54xm-rwp3 | CVE-2026-111063 | shared cookie 未强制 Secure 属性 | 3.14.4 |
| GHSA-5wr8-gcqf-gqf3 | CVE-2026-111064 | 初始请求或 header 缺少超时 | 3.14.4 |
| GHSA-6cjc-53gc-5rqm | CVE-2026-111065 | Max-Age 溢出导致 connector 池耗尽 | 3.14.4 |
| GHSA-9xw5-w5jj-pvcg | CVE-2026-111066 | multipart 请求耗尽文件描述符，GitHub reviewed HIGH | 3.14.4 |
| GHSA-f9xw-jh8m-3mwq | CVE-2026-111748 | absolute-form target 伪造 Request.scheme | 3.14.4，仓库公告受影响 <=3.14.3 |
| GHSA-gx76-c99x-2c96 | CVE-2026-111749 | 子域 cookie 移除既有 cookie 的 Host-Only 范围 | 3.14.4 |
| GHSA-pqxq-w44r-73xw | CVE-2026-111750 | 纯 Python parser 请求走私，GitHub reviewed MODERATE | 3.14.4 |

每项可通过 `https://api.osv.dev/v1/vulns/<GHSA-ID>` 复核。已保存的 `aiohttp_advisories.json` 来自 aiohttp 自身公告，部分公告评分与后续 GitHub reviewed/OSV 不同；最终报告应标明编号与版本范围，避免笼统写成“严重 CVE 已全部修复”

LiteLLM 代理使用的 HTTP 服务栈不是 aiohttp web server，本次只读源码搜索也未发现生产源码启动 aiohttp web server。因此安装受影响 aiohttp 不等于已证明其每一项 server parser 漏洞能通过 LiteLLM 入口利用。客户端 cookie/connector 相关风险则涉及出站 HTTP，仍值得随官方锁文件升级

[AIKIDO-2026-10912](https://intel.aikido.dev/cve/AIKIDO-2026-10912) 描述 yarl <=1.23.0 URL parser host confusion，列出 1.24.0 修复。目标 1.25.1 超过该边界；OSV 对 1.25.1 返回空集合。Aikido 编号不是 CVE 编号

[AIKIDO-2026-10742](https://intel.aikido.dev/cve/AIKIDO-2026-10742) 描述 gunicorn 0.1 至 25.3.0 HTTP framing/request smuggling，列出 26.0.0 修复。官方 1.106.2 仍限制 `gunicorn>=23.0.0,<24.0` 并锁 23.0.0。LiteLLM issue #33405 因 stale 关闭为 `not_planned`，不能把关闭状态解释为已修复。OSV 精确查询 23.0.0 返回空集合，与 Aikido 数据源不同，不应据此否定 Aikido 公告。应在部署报告保留此限制；若实际入口使用 uvicorn/其他服务，则暴露面与 gunicorn HTTP parser 不同，不能仅按安装版本断言生产可利用

[GHSA-w8v5-vhqr-4h9v](https://osv.dev/vulnerability/GHSA-w8v5-vhqr-4h9v) 即 CVE-2025-69872，diskcache <=5.6.3 的默认 pickle 反序列化风险，攻击前提为攻击者可写入缓存目录。OSV 没有 fixed 事件，只有 last_affected 5.6.3。当前生产未安装该包；候选镜像需再次确认不能因误选 extra 引入

## LiteLLM 自身公告

已保存的官方 LiteLLM advisories 最新 critical 为 GHSA-7hp6-4w63-5g45，跨用途复用 salt key 导致提权。公告受影响范围止于 1.104.0rc2 之前的特定旧分支，当前生产 1.106.0 已越过其修复边界，不能把此漏洞写成本次才修复

其他官方 critical CVE 包括 CVE-2026-49468、CVE-2026-42208、CVE-2026-35030，修复边界分别为 1.84.0、1.83.7、1.83.0。OSV 精确查询 LiteLLM 1.106.0 和 1.106.2 都未返回漏洞，不能把查询结果解释为全依赖树审计

## 部署前必须核对的边界

v1.106.0 至 v1.106.2 的 `litellm/proxy/schema.prisma` 和 migrations 没有差异，没有发现此次升级需要数据库 schema 迁移。新增 `db_scripts/strip_vector_store_internal_metadata.py` 为显式 dry-run/`--apply` 的上游 OpenAI vector store 元数据清理工具，不是启动迁移，不应未经实际受影响记录确认就执行删除

候选和生产镜像完成后，应逐 Pod 再核对 aiohttp 3.14.5、yarl 1.25.1、diskcache 未安装，且保留 gunicorn 23.0.0 未修复公告的准确表述。该要求属于镜像身份核验，无需为依赖版本编写镜像结构测试

独立审查结果：发布目标和已知 aiohttp 修复具有证据；无理由为等待尚未正式发布的 1.107.0 而阻止授权升级至 1.106.2。gunicorn 公告必须明确留在风险记录中，不能把此次上线等同于所有安全问题关闭
