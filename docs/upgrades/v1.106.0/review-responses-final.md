# Responses、Chat、Messages 桥接最终审查

最终源码引用：`7811a2dc41e9a14a121752268e093a194f458830`

独立行为验证快照：`8433fae985165efd7631c96b2606bb2c98241e09`

结论：本次审查范围内通过，旧 R1 至 R5 和后续 hosted web_search 接线、输出排序问题全部保持关闭，未发现新增阻断。类型与公开接口调整没有破坏本轮核验的协议行为

## 独立验证与证据

审查代理独立运行三个核心文件，317 passed、零跳过：`review-responses-final-tests.log`。覆盖 completion Responses 转换、streaming iterator 全部用例及 handler，包括新增官方 empty-leading、tool-only、annotation、signature、additional_tools 等测试

审查代理独立运行日志组装、费用和客户端终态隔离最小集，19 passed、519 deselected：`review-responses-final-logging-tests.log`

额外使用 `git archive 8433fae... litellm` 导出的固定源码运行 24 个补充案例，185 个检查全部通过，避免共享工作树的后续注释整理影响结论

| 证据文件 | 独立核验内容 |
| --- | --- |
| review-responses-final-native.json | 8 个原生 hosted code/search × 正文前后 × sync/async，76 项检查 |
| review-responses-final-boundaries.json | 11 个 namespace、输入重放浅拷贝、非 list 身份和日志隔离案例，83 项检查 |
| review-responses-final-late.json | final-only、unknown-late 和普通同名客户端工具，3 个案例、16 项检查 |
| review-responses-final-length.json | role-only 首帧后文本到 length 终态的 sync/async，2 个案例、10 项检查 |

主线程的 `regressions-acceptance.log` 6838 passed 和 `post-types-final.log` 2466 passed 已读取，属于主线程验证结果，未重复计为本代理运行。本代理没有运行完整 make check，其最终状态由主线程单独报告

## 旧问题逐项闭合

| 编号 | 最终判断 |
| --- | --- |
| R1 工具事件重复、arguments 重复及 item_id 不匹配 | 关闭。function/custom/namespace 四种工具仍只有一对 added/done，参数不重复，所有引用保持同一 item id |
| R2 服务端执行结果重复及中途变更类型 | 关闭。原生可信首帧标记使 hosted code 从 added 到 done/terminal 都为 code_interpreter_call；代码、stdout、container 路由信息保留，done 数据与 terminal 对应项一致 |
| R2 final-only 与未知晚到结果 | 关闭。final-only 直接生成完整 hosted 生命周期；已发普通 function 的未知晚到结果继续保持 function 类型，把完整执行结果保存在 provider_specific_fields，不追加重复项 |
| R3 SDK Sequence/Mapping 丢失 | 关闭。tuple namespace 成员、MappingProxyType schema、properties/required 均保留；False、空 examples 保留，None 字段按原规则省略；调用者 schema 没有被修改 |
| R4 多步索引与生命周期 | 关闭。多步 sync/async、思考和工具组合通过；原生 code/search 与正文两种顺序的 terminal.output[event.output_index] 均对应原 id/type，done 与 terminal 完整相等 |
| R5 incomplete 终态序号 | 关闭。length 终态在 sync/async 中均为 response.incomplete，全部序号存在且严格递增，文本和输出项保留 |
| hosted web_search 接线和排序 | 关闭。原生 query/source 保留，无普通 function 副本，added/done/terminal 保持相同索引和类型 |

## 本轮重点调整的判断

### additional_tools 的命名适配合理

出站 Chat 工具统一使用 `functions__exec`，与同一 namespace 中的 `functions__wait` 及既有 canonical 命名规则一致，可以避免不同 namespace 的同名 custom 工具冲突。原官方测试期待短名 exec，反映的是官方旧的 custom 展开方式，不是本地应继续保留的线协议约束

这项测试适配保留了 exec 包装参数 schema 含 content 的断言，async 语义测试也保留并通过：返回时恢复 custom_tool_call、短名 exec 和原始 input `ls`。其他 canonical/短名恢复、歧义名称、顶层碰撞、custom/function 同短名隔离的测试也在本轮 317 项中通过，因此不是仅改名称让测试变绿

### 空首帧处理没有发现副作用

移除 role-only 打开 message、恢复 `_delta_carries_output` 门控，与官方工具流契约一致：空文本、空 reasoning、只有 role 或 choices 为空的首帧不提前占用 message output_index，tool-only 的函数输出可以位于 index 0

包含真实文本、annotation 或签名 thinking 的首个有效块仍会打开对应项，reasoning/text/tool 的顺序正确。完全空答案在结束时仍保持既有空 message 的收口行为，未被误改成 tool-only。相关同步异步官方用例全部通过

### 类型与公开 helper 调整保持了有效输入行为

动态 extra payload 改为 model_validate、不可变 namespace 构造、公开 helper alias 和输入 overload 没有在本轮有效输入边界中丢失字段。输入恢复和归一化均返回新 list、新顶层 dict，嵌套内容保持浅拷贝语义；原输入未修改，str、None、tuple、普通对象继续原样返回。旧私有 helper alias 与新公开 helper 指向相同函数

### 日志组装和客户端终态保持隔离

dict 和 Pydantic 两种客户端终态均未被日志归一化修改，hidden metadata 也保持原值。日志副本正确生成 prompt/completion usage，保留 cached/reasoning token details 和上游 reported cost；已经计算的本地费用 0 和 0.125 均优先于上游 0.0042，不因零值而错误回退

## 最终边界

最终源码冻结后的 5 个文件仅有规则说明注释位置调整，已读取 `final-comment-only-ast.json`。本审查范围内的 streaming_iterator 另用 ast.dump 独立对照，确认与 8433fae 的 AST 完全相同

最终引用衔接已再次只读核对：`git diff 8433fae..7811a2d` 只有 6 个文件。其中 routing_stats.py、streaming_iterator.py、router.py、types/llms/openai.py、utils.py 这 5 个文件的 AST 均由本代理独立对照确认相同，证据为 `review-responses-final-ref-check.json`。另一个文件 azure_ai/chat/transformation.py 恢复官方 v1.106.0 的参数边界：非 dict 输入按空 kwargs 处理，dict 中非法 kwargs key 仍由字典解包报错；本次 4 行变化不涉及 Responses 桥运行逻辑

已读取 `azure-final.log` 的 29 passed 记录，这是主线程的官方 Azure 回归验证。本次最终引用衔接没有重复运行测试；Responses 的审查结论可以从 8433fae 衔接到 7811a2d。最终完整 make check 的运行状态仍由主线程报告

本审查使用合成协议事件和真实转换器、迭代器及日志组装器，未访问生产、未发送真实模型请求，也未操作实际 Harness 客户端。不能把该审查扩大解释为全部旧 XML 溢出、上游网络错误或所有线上长会话均已恢复

未知外部流晚到的服务端执行结果保存在 provider_specific_fields，保持先前已经承诺的 function 生命周期；可信原生 server_tool_use 则从首帧起提供 hosted 类型，两条兼容路径的表达有意不同

没有修改共享源码、提交或部署
