# v1.106.0 完备性最终独立复核

最终源码 `efee6c541fe0add9e6fbf7f9fdab1d330b518185`，分支 `litellm_v1106_upgrade`，工作区 `/home/allcam/projects/litellm-v1106-upgrade`。只读核对源码与测试，独立运行必要离线对照；未修改源码、未运行重型门禁、未访问生产、未发送模型请求。证据目录 `/home/allcam/projects/litellm-v1106-evidence`

结论：本组既有源码阻断均已关闭，未发现新的合入阻断。完整迁移范围、普通 Anthropic 契约替代和补充适配均已显式记录。本结论支持候选源码合入，不等同镜像、完整数据库迁移、真实 Harness 或生产上线验收

## 合入基线和完整范围

旧官方补丁基线 `10f4033437df30b91b5dbf2b64711d0a8683fc52`（v1.99.1），定制源 `29532d2608410044f84ac202503a79a6cd60e693`，官方目标 `955bdaafa8b72b2b6ff728048f272fbc06049f63`（v1.106.0）。正常双亲合并 `121f21de7766800d384cb4027383f7164f272503` 保留双方历史，结果以官方目标树为底迁移本机净补丁；最终修正为上述 efee6 源码

112项原始来源清单严格等于旧官方基线到定制源的净差异集合，逐项记录来源/目标路径、契约、处置、Git跟踪状态、SHA256、测试保留/移位/替代。`.python-version` 是 ignored 环境文件，不计迁入；固定1.99.1增量镜像配方退役，采用新版官方完整构建链

冻结候选相对官方的141个实际变化路径已全部互斥归类：105个原目标、21个参数边界扩展、15个其它必要适配。原112项另有2项退役与5项目标无净差异；参数适配共25文件，其中4个与原目标重叠，所以不能把112+21+15当实际变化数。全部141路径均被Git跟踪，源码与冻结引用字节一致。最终审查文档另行归档，不混入该冻结源码集合。详见 `review-final-scope-inventory.json` 和最终112项清单

15项补充路径覆盖 RedisBatch、InMemoryCache、预算reseed共享判定、Anthropic原生新入口/server tool流、Responses custom tools及日志副本测试、加密affinity防伪及其测试、两份真实PG/Redis integration、OpenAPI快照和升级计划文档。逐路径契约在scope清单中，不以文件存在代替审查

## 阻断关闭与关键边界

| 项目 | 最终复核 |
| --- | --- |
| Docker APK_REPOSITORY作用域 | builder/runtime使用前声明，采用官方完整Rust/Maturin、Prisma和UI构建链；不使用旧1.99.1增量基底 |
| 官方bulk/deferred/lease绕过预算generation及TTL | increment/set/delete/刷新通过共享原子协议；RedisBatch指令数仍正确，key及generation同槽，内存删除继续_remove_key |
| 预算窗口DB与Redis | 单条SQL按budget_reset_at进行CAS并RETURNING真实提交token，扣除旧窗口金额而保留并发新支出；仅提交成功后协调generation；旧worker/丢失generation不覆盖新窗口 |
| Responses provider cost在日志转换中丢失 | 对象/dict、原terminal/assembled均保留provider成本；日志副本独立且不改client payload |
| provider cost覆盖已算response_cost | 已算成本优先，包括0；provider上报值作fallback；cache_hit仍为0 |
| previous_response_id锁回旧order | 显式order fallback受当前候选约束；user/session pin不能越过所选order；真正encrypted pin优先且每轮回算 |
| 客户端伪造encrypted affinity flag | 请求清理与pre-call双边界清除客户端/上一轮flag，空kwargs字典保持身份；只有实际验证的加密归属可重新设置 |

两段核心Redis Lua与先前已独立审查的第三轮源码逐字一致，DB CAS和Redis窗口协调函数AST一致。独立调用真实key-slot helper覆盖普通、命名空间、既有hash tag、空hash tag与Unicode等5种key；全部key/generation同槽。真实隔离PostgreSQL/Redis最终30项通过且0跳过，见 `budget-real-acceptance.xml`；这不是完整生产数据库副本迁移证明

路由观测的最终不可变聚合改写另做8组旧/新实际函数对照：空数据、仅active、跨bucket聚合、同时间戳选后bucket、延迟溢出、Redis bytes、缺model ID与稀疏分钟；结果完全相同，百分位8格对照也相同。可信Router归属快照、attempt ID容器身份、跨实例幂等、active lease和Redis故障显式503保持。证据 `review-completeness-frozen-budget-stats.json`

计费独立实调6格：BaseModel/dict两载体乘无已算成本/已算0/已算0.125。原terminal与assembled分别为0.0042/0/0.125，cache_hit为0，client payload不变，日志副本独立。环境参数更新继续右侧覆盖旧值、过滤内部可信context且保留其它嵌套值身份，不额外复制校验全部旧dict。证据 `review-completeness-frozen-cost.json`

## 94处参数边界和Anthropic契约

最终94处/25文件均为 `GenericLiteLLMParams.model_validate(dict(...))`，显式关键字、kwargs展开和顺序逐项AST完全一致，0处跳过原展开表达式。实际11种调用形状的值、fields_set、extra、模型/字段验证器与post_init一致；可信owner对象保留身份，伪造同形dict仍被拒绝

Azure保留旧isinstance(dict)兜底和dict(**...)展开：None/list仍转空字典，非字符串key仍先抛TypeError。独立8组合法/validator输入及3组旧边界前后完全一致。最终仅将该参数注解扩大为object，以准确描述既存接收边界并消除静态冗余判定；不采用中途删除guard的direct验证方案。证据 `review-param-boundary-ast.json`、`review-param-boundary-runtime.json`、`review-param-boundary-azure-restored.json`

运行时对照在7811源码完成；efee6相对7811只改该Azure参数注解，去除这一处注解差异后全文件AST相同，其余源码字节相同。桥接证据 `review-final-source-bridge.json`。最终94处AST和141路径哈希已对efee6重核

普通Anthropic两个旧inline-redacted参数场景按1.106.0官方转换/placement契约调整：inline存在时阻止sidecar，普通inline redacted不由定制额外直接发出，signed thinking及可见text保留。对应旧测试改名并保留新期望。可信DeepSeek的unsigned thinking、不可发送inline回退sidecar和去重契约继续保留；native Messages合法redacted透传不经过该Chat factory。此处是本机移植语义对齐，不是上游模型缺陷结论，也不宣称所有旧普通Anthropic行为完全等价。详见 `review-anthropic-placement.md`

## 验证结果和限制

已读取实际最终记录，不累加重叠运行：

| 证据 | 结果 |
| --- | --- |
| regressions-acceptance.xml | 6838通过，0失败/错误/跳过 |
| post-types-final.xml | 2466通过，0失败/错误/跳过 |
| budget-real-acceptance.xml | 隔离真实PG/Redis 30通过，0失败/错误/跳过 |
| azure-final.xml | guard恢复后29通过，0失败/错误/跳过 |
| make-check-source-final.log | 最终efee6完整范围门禁check: PASS；Python lint/类型预算、test-tree、UI及API类型同步均完成 |

门禁按变更范围跳过无改动的tests/e2e检查，该记录不是端到端模型测试通过。旧make-check-release-candidate的UnnecessaryIsInstance红灯已由保留guard并修正参数注解关闭，不能用旧红替代最终实际绿，也不能用历史绿替代最终源码记录

`CURRENT_BASELINE.md` 与定制源逐字相同，生产仍1.99.1-custom；抓取保持关闭。尚未验收候选完整镜像、完整数据库副本schema迁移/恢复演练、真实供应商/Harness端到端、生产发布及监测窗口。因此不宣称线上XML/SSE故障已被本轮升级验证修复
