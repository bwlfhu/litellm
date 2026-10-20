最终协议独立审查，最终引用 7811a2dc41e9a14a121752268e093a194f458830

结论：本次逐项代码审查与独立本地边界复测未发现新增的协议阻断缺陷，前两轮发现 P-01、P-02、P-03 均保持闭环。类型整理保留了有效 JSON 输入的协议行为；普通 Anthropic inline/sidecar 优先级是一项明确采用官方规则的兼容性取舍，不能表述为所有旧行为完全等价

工作区为 /home/allcam/projects/litellm-v1106-upgrade，本轮重点比较 fe58fb69ad6fafdce1471c636062e4d2c865b416 与冻结提交。官方基线 v1.106.0 为 955bdaafa8b72b2b6ff728048f272fbc06049f63，旧定制基线为 29532d2608410044f84ac202503a79a6cd60e693。本轮只读源码，未提交、未访问生产、未发送真实模型请求，也未重复运行重型门禁。复测后确认 litellm/ 与 tests/ 相对冻结提交没有工作树差异；主线程当时仅在修改 UPGRADE_V1_106_0.md

| 核对项 | 最终核对结果与证据 |
| --- | --- |
| 1. 原生 Messages 与 Chat 路由 | messages/handler.py 从可信部署上下文或显式 DeepSeek provider 选择 DeepSeekAnthropicMessagesConfig，调用原生 Messages HTTP 路径；Chat 仍使用独立 Chat 配置。路径归一化改写与原算法等价，没有因为类型整理把原生 Messages 转成 Chat。已读取最终套件中的 mock transport、Router dispatch、路径组合和独立 Chat 测试 |
| 2. 可信上下文隔离 | router_protocol.py 的 dataclass 仍 frozen+slots，owner 必须与模块私有令牌同一 identity；字典、任意对象均不能成为可信上下文。Router:4212 起先从服务端部署 raw_model_info 建立上下文，每次先清理旧上下文再按目标重建，并在客户端凭据覆盖前完成，防止客户端 model_info 抬升协议权限。native handler:506 起在构建 GenericLiteLLMParams 前移除内部字段，MCP 仅显式转交可信对象。独立复测伪造字典、伪造对象和跨部署清理全部通过 |
| 3. 工具历史、工具输入与图片 | messages/transformation.py:471 仍先验证原始 function_call 类型，再建立 typed mapping；非法字符串/list/int/bool 四格均返回 400。deepseek_sequence:16 运行时仍只接收 list，没有因 Sequence 注解扩大协议形状。工具调用 ID 匹配、legacy function 关联、结果相邻合并、原生块补全及校验顺序保留。TypeAdapter(Mapping[str, object]) 是浅层读取边界，tool input 内名为 thinking/reasoning_content 的用户字段保持不透明。最终 2466 项套件包含 user image、24 格 nested tool result image、非法位置图片、tool input 字段保留及工具 ID/顺序用例 |
| 4. 真实思考历史与缺失策略 | _prepare_deepseek_chat_message:987 的函数式重写保留 scalar content、inline/sidecar、真实 reasoning_content 的选择顺序；disabled 时不回放思考，enabled 时优先保留真实推理。missing_reasoning 的 omit/error/placeholder 仍只由可信部署策略控制，未因类型整理恢复无条件空格补造；最终套件保留 omit 场景及真实 sidecar 场景 |
| 5. thinking disabled 与 effort | _without_adaptive_reasoning_params:1202 仍让显式 disabled 优先并移除冲突 effort；none 映射 disabled，minimal/medium/xhigh 等归一化保留。Chat transform 使用独立 protocol_thinking 局部量保存协议控制，disabled 在末端保留原生格式。请求和响应净化仍按 disabled 过滤 thinking/signature，并保留用户工具输入，最终套件含 disabled、effort、first-turn、非 v4 默认行为与流/非流用例。这里证明本机映射行为，不能据此断定真实上游是否遵守 |
| 6. SSE event/type、拆包与关闭 | _sanitize_deepseek_sse_event:307 继续从 JSON 顶层 type 重写 event，data-only 帧补齐类型；增量 CRLF/分片缓存与 finally aclose 不变，没有提前收集整条流。最终套件包含四格 data-only、两格 event mismatch、disabled/native stream 字段净化和工具输入保留。独立提前退出关闭源流测试通过。combined splitter 仍按 content 后 finish 拆分，并仅让 finish 保留 usage；对可选 Delta 字段设置 None 不生成思考负载，两个独立 finish 清理测试通过 |
| 7. HTTPException 与 typed error | common_request_processing.py:1778 提取的 _post_call_failure_exception 保留 hook 返回/抛出替换异常的规则。共享流 serialize_http_errors 默认 False，只有 Messages async_sse_data_generator:4140 显式启用。独立复测源直接抛、hook 返回、hook 抛出三来源×首帧前/流中六格全部通过：首帧前返回真实 429/403 JSONResponse；流中输出官方 AnthropicErrorSseFrame、顶层 type:error，并且替换后不泄漏原始异常文本。独立补测通用默认路径三来源仍重抛 HTTPException，默认 OpenAI error 仍为 data:{error:...} 和 [DONE] |
| 8. 首帧、keepalive 与 Chat 兼容 | 最终接受套件实际包含首字节前 JSON 错误、keepalive 后 typed error、晚到错误、取消与上游关闭、部分 SSE 帧 seal 等具名用例。_post_call_failure_exception 的重构没有扩大 HTTP 序列化开关到 Chat；proxy_server.py 的实际 Chat generator 未在本轮改变。本轮边界复测与代码审查没有把 keepalive 后错误错误改回 HTTP 响应 |
| 9. 普通 Anthropic placement 取舍 | factory.py:2767 起在 preserve_unsigned_thinking_blocks=False 时采用官方“任意 inline thinking/redacted 占位”规则，并删除普通 inline redacted 的额外输出分支，与未改动的官方 mid_conversation_system.py 保持一致。可信 DeepSeek=True 时仍以非空可用 inline thinking 占位，允许真实 sidecar 回退。原两项 placement 失败在最终 6838/2466 两份 XML 中均存在且通过；两个普通 inline-redacted 预期已具名更新为官方结果，七格可信 DeepSeek 合约未删除。原生 Messages redacted 和普通 sidecar-only 的有效回放未被该取舍覆盖 |

历史发现的最终关闭状态：P-01 的 Messages HTTPException typed error 已由六格独立复测再次确认；P-02 的 Responses 重复方法保持删除，保留官方 encrypted reasoning/redacted 路径；P-03 的无调用初始化方法保持删除，官方 __next__/__anext__ 初始化状态机继续保留

本轮独立复测为 review-protocol-final-boundaries.xml：16 passed，0 failed/error/skipped，3 warnings，日志耗时 1.54 秒。三条 warning 为 Pydantic 对 TypedDict ReadOnly 不保证运行时不可变的提示，不是协议失败；代码仍显式管理不可变与可变边界。额外 review-protocol-final-generic.py/JSON/log 记录通用默认流三来源重抛及默认 OpenAI 帧格式检查，均通过，过程中没有调用真实提供商

另已解析主线程最终证据而非只引用总数：regressions-acceptance.xml 为 6838 tests、0 failures/errors/skips；post-types-final.xml 为 2466 tests、0 failures/errors/skips。两组及本轮复测有重叠，不能相加为唯一测试数。最初独立脚本曾把 _sse_error_frames 的 tuple 返回值误当 str，修正脚本断言后完成通过，不属于产品缺陷

验证边界：本报告是冻结代码候选的本地协议审查结论。生产部署、真实 DeepSeek Harness/密钥故障重放、所有旧 XML 会话恢复和真实上游 thinking 行为均未在本轮验证；不能凭本报告归责上游，也不能宣称 SSE mismatch 或 XML 溢出的全部现场已消失。完整 make check 与官方基线门禁由主线程单独记录，本报告不代替其最终结果

最终引用衔接核对：上述实际运行的独立边界测试针对 8433fae985165efd7631c96b2606bb2c98241e09；本次只读追加核对到 7811a2dc41e9a14a121752268e093a194f458830，没有重跑测试。git diff 仅六个文件发生变化，其中 routing_stats.py、Responses streaming_iterator.py、router.py、types/llms/openai.py、utils.py 均为注释位置与相应排版调整，分别用 ast.parse/ast.dump(include_attributes=False) 对比后 AST 完全一致。独立核对结果保存于 review-protocol-final-ref-link.json，因此 Router 可信上下文、Responses 流处理和前述 DeepSeek/Anthropic/SSE 结论可衔接到最终引用

六文件中唯一非 AST 等价项为 llms/azure_ai/chat/transformation.py:140：GenericLiteLLMParams.model_validate 的输入改为 dict(**(litellm_params if isinstance(litellm_params, dict) else {}))。已直接读取官方 v1.106.0 同函数，其原实现为 GenericLiteLLMParams(**(litellm_params if isinstance(litellm_params, dict) else {}))，本次恢复其非 dict 输入用空字典兜底、非字符串 kwargs key 抛错的既有边界，未改动本报告负责的 DeepSeek Messages/Chat、Anthropic placement、可信上下文或 SSE 运行逻辑。已独立读取 azure-final.xml，确认为 29 tests、0 failures/errors/skips，未把该结果当成本审查另行运行。最终协议结论维持，生产、真实 Harness 与全部历史 XML 的验证边界维持
