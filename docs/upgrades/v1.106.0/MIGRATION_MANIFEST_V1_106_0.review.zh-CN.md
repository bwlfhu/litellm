# 官方1.106.0迁移最终112项清单

基线 10f4033437df30b91b5dbf2b64711d0a8683fc52；定制源 29532d2608410044f84ac202503a79a6cd60e693；官方目标 955bdaafa8b72b2b6ff728048f272fbc06049f63

本清单以Git跟踪状态作为迁入/保留判据，记录当前跟踪文件工作树SHA256；ignored或untracked物理文件不计迁入。最终冻结前须比较哈希，已验证的历史记录不冒充当前全部门禁或生产验收。完整机器可读清单见 port-manifest-final.review.json

源码采用官方目标树加净补丁，正常双亲合并保留两边历史。只有文件名、类型或公共接口变化不构成需求删除；官方已实现或官方协议语义升级时明确记录替代依据

|编号|原路径|目标路径|最终归属|契约与处理依据|
|---|---|---|---|---|
|M001|.python-version|.python-version|retired|采用官方不跟踪 .python-version 的策略；保留官方 pyproject 支持范围、uv.lock 和根 Dockerfile 的 Python 3.13 运行环境；ignored 工作区文件与验证venv 3.13.14仅作环境证据，不宣称版本锁已合入|
|M002|CURRENT_BASELINE.md|CURRENT_BASELINE.md|historical-record-preserved|保留真实生产 1.99.1-custom 状态与最近 SSE 修复证据；源码候选合入不改写为已经上线 1.106.0|
|M003|Dockerfile|Dockerfile|migrated|采用官方完整 Rust/Maturin、UI、Prisma、pgbouncer 构建链，保留 npm/uv/apk 镜像及 Prisma 代理配置；修正 runtime ARG 作用域；不使用旧增量基底|
|M004|Makefile|Makefile|upstream-covered|保留新版官方 Makefile 和 BASE_REF/default_branch.py 机制，替代旧 LINT_BASE 兼容补丁；门禁仍使用两槽位机制|
|M005|UPGRADE_V1_99_1.md|UPGRADE_V1_99_1.md|historical-record-preserved|保留 1.99.1 已完成迁移的历史证据，避免把历史验收误写为本次版本的验证|
|M006|deploy/INFRASTRUCTURE_ARCHIVE.md|deploy/INFRASTRUCTURE_ARCHIVE.md|archived|保留独立基础设施归档边界，本次仅合源码，不部署 Casdoor/FRP/Lucky 等历史材料|
|M007|deploy/casdoor/PDEP_OIDC.md|deploy/casdoor/PDEP_OIDC.md|archived|仅归档保留原 OIDC 接入说明，不执行或宣称本轮部署|
|M008|deploy/casdoor/README.md|deploy/casdoor/README.md|archived|仅归档保留原基础设施说明，不执行或宣称本轮部署|
|M009|deploy/casdoor/bootstrap.sh|deploy/casdoor/bootstrap.sh|archived|仅归档保留原 bootstrap 脚本，不执行基础设施写操作|
|M010|deploy/casdoor/casdoor.yaml|deploy/casdoor/casdoor.yaml|archived|仅归档保留原 Casdoor 清单，不纳入 LiteLLM 本轮发布|
|M011|deploy/casdoor/configure-feishu.sh|deploy/casdoor/configure-feishu.sh|archived|仅归档保留原身份源配置脚本，不执行外部应用写入|
|M012|deploy/casdoor/ingress.yaml|deploy/casdoor/ingress.yaml|archived|仅归档保留原 ingress 清单，不部署|
|M013|deploy/casdoor/namespace.yaml|deploy/casdoor/namespace.yaml|archived|仅归档保留原 namespace 清单，不部署|
|M014|deploy/casdoor/postgres.yaml|deploy/casdoor/postgres.yaml|archived|仅归档保留原基础设施 PostgreSQL 清单，不用于本轮数据库迁移|
|M015|deploy/casdoor/provision-pdep.sh|deploy/casdoor/provision-pdep.sh|archived|仅归档保留原集成配置脚本，不执行|
|M016|docker/Dockerfile.deepseek-vision|docker/Dockerfile.deepseek-vision|retired|退役固定 1.99.1 增量镜像配方，避免旧依赖基底叠加 1.106.0 源码；候选统一从根 Dockerfile 完整构建，历史配方可从旧提交恢复|
|M017|litellm/caching/dual_cache.py|litellm/caching/dual_cache.py|migrated|保留 Redis 回填及本地 increment 的默认内存 TTL，适配官方批量缓存能力|
|M018|litellm/caching/redis_cache.py|litellm/caching/redis_cache.py|migrated|保留 key 预算 generation、CAS 修复所需 Redis 原子协议及 strict 状态读取；补齐官方 bulk/deferred/set/delete/lease 刷新入口，命名改为跨模块共享 public helper|
|M019|litellm/completion_extras/litellm_responses_transformation/transformation.py|litellm/completion_extras/litellm_responses_transformation/transformation.py|migrated|在官方 Responses-to-Chat 桥上保留多项消息、工具输入输出、namespace/custom 工具与推理历史转换，兼容新版 SDK 工具对象|
|M020|litellm/litellm_core_utils/exception_mapping_utils.py|litellm/litellm_core_utils/exception_mapping_utils.py|migrated|异常重新映射保留 _litellm_disable_fallbacks，防止明确本地协议错误经包装后继续跨线路回退|
|M021|litellm/litellm_core_utils/get_litellm_params.py|litellm/litellm_core_utils/get_litellm_params.py|migrated|保留 Router 可信部署上下文的内部参数通道，供提供方选择使用，防止序列化到上游|
|M022|litellm/litellm_core_utils/litellm_logging.py|litellm/litellm_core_utils/litellm_logging.py|migrated|保留真实部署 model_info/provider 定价和预建 logger 同步、Responses usage 非破坏性规范化；新增 provider 上报成本的对象/dict等价读取并保持已算 response_cost 优先|
|M023|litellm/litellm_core_utils/prompt_templates/common_utils.py|litellm/litellm_core_utils/prompt_templates/common_utils.py|upstream-covered|采用官方共享 reasoning helper、加密 reasoning 支持与新版解析接口，覆盖旧 readable thinking flatten；不覆盖回旧重复实现|
|M024|litellm/litellm_core_utils/prompt_templates/factory.py|litellm/litellm_core_utils/prompt_templates/factory.py|migrated|保留可信 DeepSeek 部署的 unsigned thinking、inline/sidecar 去重及不可发送 inline 回退 sidecar；普通 Anthropic 两个旧 inline-redacted 场景按官方 1.106.0 转换/placement 契约调整，恢复 inline 存在优先且不直接发出 inline redacted，详见 review-anthropic-placement.md|
|M025|litellm/llms/anthropic/chat/transformation.py|litellm/llms/anthropic/chat/transformation.py|migrated|保留部署上下文驱动的原生 DeepSeek 兼容、tool-history 策略及思考关闭语义；普通 Anthropic 默认行为沿用官方|
|M026|litellm/llms/anthropic/common_utils.py|litellm/llms/anthropic/common_utils.py|migrated|保留 Anthropic tool-use ID 规范化统计接口，扫描/重写计数用于结构日志，不记录工具正文|
|M027|litellm/llms/anthropic/experimental_pass_through/adapters/handler.py|litellm/llms/anthropic/pass_through/adapters/handler.py|migrated|迁至 pass_through 新目录，保留 thinking_disabled 请求状态和可信上下文跨适配器传递，维护新版官方 handler 状态机|
|M028|litellm/llms/anthropic/experimental_pass_through/adapters/streaming_iterator.py|litellm/llms/anthropic/pass_through/adapters/streaming_iterator.py|migrated|迁至 pass_through 新目录，保留 combined reasoning/text/tool 片段与显式禁用思考过滤；删除未调用旧初始化方法，沿用官方初始化及 server tool 状态机|
|M029|litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py|litellm/llms/anthropic/pass_through/adapters/transformation.py|migrated|迁至 pass_through 新目录，保留 Anthropic-to-Chat 推理/工具转换和显式禁用思考语义|
|M030|litellm/llms/anthropic/experimental_pass_through/messages/handler.py|litellm/llms/anthropic/pass_through/messages/handler.py|migrated|迁至 pass_through 新目录，保留规范化工具结构日志与 MCP 递归可信部署上下文，并在真正提供方边界终止内部控制字段|
|M031|litellm/llms/anthropic/experimental_pass_through/responses_adapters/streaming_iterator.py|litellm/llms/anthropic/pass_through/responses_adapters/streaming_iterator.py|upstream-covered|保留官方新 Responses-to-Messages SSE 实现，已给 unsigned thinking 起始块提供空 signature，且支持 encrypted/redacted reasoning；旧单行修复已覆盖|
|M032|litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py|litellm/llms/anthropic/pass_through/responses_adapters/transformation.py|migrated|保留官方新版 reasoning summary、encrypted/redacted思考解析，迁入本机工具/推理往返需求，删除与官方重复定义的旧 helper|
|M033|litellm/llms/azure_ai/chat/transformation.py|litellm/llms/azure_ai/chat/transformation.py|migrated|采用官方 Azure AI 非 OpenAI 消息字段清理，reasoning_content 已在清理字段中，覆盖旧增补|
|M034|litellm/llms/base_llm/anthropic_messages/transformation.py|litellm/llms/base_llm/anthropic_messages/transformation.py|migrated|保留按提供方选择的 Messages HTTP 错误禁止 fallback 扩展点，默认提供方仍为 False|
|M035|litellm/llms/bedrock/chat/invoke_transformations/amazon_deepseek_transformation.py|litellm/llms/bedrock/chat/invoke_transformations/amazon_deepseek_transformation.py|migrated|保留 Bedrock DeepSeek </think> 跨 chunk 缓冲、同块推理/正文分离及流尾未闭合前缀处理|
|M036|litellm/llms/chatgpt/chat/streaming_utils.py|litellm/llms/chatgpt/chat/streaming_utils.py|migrated|保留 ChatGPT 多工具源 index 映射，避免并行/交错后续 delta 被归入最后一个工具|
|M037|litellm/llms/custom_httpx/llm_http_handler.py|litellm/llms/custom_httpx/llm_http_handler.py|migrated|保留 HTTP 提供方选择、禁回退标记、原生 Messages 工具/思考处理、opt-in timing与结构诊断；适配新版调用链，旧 Rust Anthropic fast-path 已由官方退役|
|M038|litellm/llms/dashscope/cost_calculator.py|litellm/llms/dashscope/cost_calculator.py|migrated|保留 Dashscope DeepSeek thinking 输入/缓存/输出费率，在实际 reasoning tokens 存在时选择 thinking 价格|
|M039|litellm/llms/deepseek/chat/transformation.py|litellm/llms/deepseek/chat/transformation.py|migrated|保留 DeepSeek Chat 默认/显式思考、工具历史真实推理、omit/placeholder/error、强制 tool choice 与 vision/图片历史；适配新版数据类型和共享工具|
|M040|litellm/llms/deepseek/common_utils.py|litellm/llms/deepseek/common_utils.py|migrated|保留缺失推理占位的聚合警告，明确占位策略影响，不对业务正文做日志输出|
|M041|litellm/llms/deepseek/messages/transformation.py|litellm/llms/deepseek/messages/transformation.py|migrated|保留 DeepSeek 原生 Messages 路径矩阵、thinking disabled、历史/图片合法性、并行 tool result、脱敏和 SSE event/data.type 一致；不将原生请求偷偷改成 Chat|
|M042|litellm/llms/fireworks_ai/chat/transformation.py|litellm/llms/fireworks_ai/chat/transformation.py|migrated|沿用新版 Fireworks 支持 reasoning_history 的合法字符串 reasoning_content；继续移除 Anthropic thinking_blocks/非法非字符串值，避免把旧一刀切清理覆盖回新版官方功能|
|M043|litellm/llms/hosted_vllm/chat/transformation.py|litellm/llms/hosted_vllm/chat/transformation.py|upstream-covered|采用官方 vLLM 合法字符串 reasoning_content 回放及非字符串清理，取代旧一刀切移除；thinking_blocks 仍清理，现有新版回归覆盖|
|M044|litellm/llms/openai/responses/transformation.py|litellm/llms/openai/responses/transformation.py|migrated|保留 OpenAI Responses custom_tool_call_input.delta/done 的事件类映射|
|M045|litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py|litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py|migrated|Gemini 工具 schema 转换前复制输入，防止删除 additionalProperties 等步骤污染调用者原始工具定义|
|M046|litellm/main.py|litellm/main.py|migrated|保留 Chat 工具 received/normalized 结构诊断和可信部署上下文传递，内部参数由提供方边界消费|
|M047|litellm/model_prices_and_context_window_backup.json|litellm/model_prices_and_context_window_backup.json|migrated|在官方模型目录基础上保留本机 DeepSeek 别名/vision/上下文输出上限、Dashscope思考价格字段；保持包内backup与主目录一致，未整体覆盖为旧目录|
|M048|litellm/proxy/anthropic_endpoints/endpoints.py|litellm/proxy/anthropic_endpoints/endpoints.py|migrated|保留 Messages 入口协议调用上下文与 Anthropic 错误流编码，适配官方 SSE 帧类型|
|M049|litellm/proxy/common_request_processing.py|litellm/proxy/common_request_processing.py|migrated|保留 keepalive后、首帧、流中、延迟JSON等 Messages typed error及Chat错误格式；补齐 HTTPException及hook替换，保留真实状态和masked错误消息|
|M050|litellm/proxy/common_utils/reset_budget_job.py|litellm/proxy/common_utils/reset_budget_job.py|migrated|保留 key 窗口 budget_reset_at CAS、仅提交成功后协调 Redis generation；以新版 _RowReset.spend_decrement 保留 rollover carried spend|
|M051|litellm/proxy/litellm_pre_call_utils.py|litellm/proxy/litellm_pre_call_utils.py|migrated|保留请求边界内部字段/可信路由遥测隔离，并清除客户端伪造 encrypted affinity flag；沿用官方新增其他安全字段清理|
|M052|litellm/proxy/observability/__init__.py|litellm/proxy/observability/__init__.py|migrated|保留观测包入口，不引入额外运行配置|
|M053|litellm/proxy/observability/observability_endpoints.py|litellm/proxy/observability/observability_endpoints.py|migrated|保留 admin-view 只读 routing-stats/model-status 接口、真实 Redis 来源与故障显式报错，缺失key与读故障区分|
|M054|litellm/proxy/observability/routing_stats.py|litellm/proxy/observability/routing_stats.py|migrated|保留分钟bucket统计、跨实例attempt幂等、active lease、延迟直方图和仅Router生成的可信归属快照，日志不带密钥/正文|
|M055|litellm/proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py|litellm/proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py|migrated|保留原生 Anthropic pass-through成本记录与结构诊断，解析仅在显式启用诊断时执行|
|M056|litellm/proxy/proxy_server.py|litellm/proxy/proxy_server.py|migrated|保留观测router注册/启动、部署cooldown、key支出floor修复与generation保护，融合官方批量支出及多窗口账务实现|
|M057|litellm/proxy/public_endpoints/provider_create_fields.json|litellm/proxy/public_endpoints/provider_create_fields.json|migrated|保留 DeepSeek 创建模型表单默认占位 deepseek-chat，避免误导为 OpenAI 模型|
|M058|litellm/repositories/verification_token_repository.py|litellm/repositories/verification_token_repository.py|migrated|保留 KeyBudgetReset参数对象及单条SQL CAS扣减/RETURNING，避免重复窗口重置和覆盖新支出；适配新版仓储类型|
|M059|litellm/responses/litellm_completion_transformation/streaming_iterator.py|litellm/responses/litellm_completion_transformation/streaming_iterator.py|migrated|保留 Responses 多step输出index、正文/推理/工具事件与完成快照一致；融合官方 custom/namespace、加密reasoning、server tools及同步/异步迭代接口|
|M060|litellm/responses/litellm_completion_transformation/transformation.py|litellm/responses/litellm_completion_transformation/transformation.py|migrated|保留 Chat-to-Responses 工具/推理/文本转换和签名历史，融合官方工具对象、namespace恢复及code_interpreter等事件，保留 incomplete终态|
|M061|litellm/responses/main.py|litellm/responses/main.py|migrated|保留 Responses 单次tool iterable物化、managed工具参数处理及外部ID回放规范化，结构诊断不消耗原始generator|
|M062|litellm/responses/utils.py|litellm/responses/utils.py|migrated|保留 Responses usage dict兼容/非破坏性日志转换、model/replay ID与工具映射，适配官方新usage细节和辅助函数命名|
|M063|litellm/router.py|litellm/router.py|migrated|保留优先级、blocked/cooldown/affinity与回退边界、可信上下文和遥测/定价归属；适配官方选择器并保留显式order fallback|
|M064|litellm/router_protocol.py|litellm/router_protocol.py|migrated|保留 frozen owner-stamped部署协议上下文；只有Router根据部署配置创建的对象可选择DeepSeek模式，用户同形dict/伪造对象不得生效|
|M065|litellm/router_utils/pre_call_checks/deployment_affinity_check.py|litellm/router_utils/pre_call_checks/deployment_affinity_check.py|migrated|保留 user/session affinity只在所选order内命中与清理陈旧pin；显式order fallback的previous_response连续性也受候选约束，真实encrypted pin优先且每轮重算|
|M066|litellm/types/llms/openai.py|litellm/types/llms/openai.py|migrated|保留工具/推理/usage/custom事件所需OpenAI类型字段，遵循新版SDK对象边界和公共类型|
|M067|litellm/types/utils.py|litellm/types/utils.py|migrated|保留thinking价率和ModelInfo字段及内部参数白名单，内部可信上下文与路由遥测不流入提供方参数|
|M068|litellm/utils.py|litellm/utils.py|migrated|保留 tool诊断、单次iterable物化、thinking能力查询和模型元数据价格字段，基于新版公共helper适配|
|M069|model_prices_and_context_window.json|model_prices_and_context_window.json|migrated|主模型目录保留与包内backup相同的本机增补，采用官方新增模型/价格为基础，保持别名vision及token上限契约|
|M070|scripts/pre_commit_lint.sh|scripts/pre_commit_lint.sh|upstream-covered|采用官方pre-commit BASE_REF/default分支解析、范围门禁和两槽位机制，替代旧LINT_BASE补丁，未禁用门禁|
|M071|tests/litellm_utils_tests/test_validate_tool_choice.py|tests/unit/utils/test_validate_tool_choice.py|migrated-tests|工具choice验证与自定义工具形状回归|
|M072|tests/proxy_unit_tests/test_proxy_utils.py|tests/unit/proxy/test_proxy_utils.py|migrated-tests|代理请求清理、内部遥测控制字段和预算/状态相关回归|
|M073|tests/proxy_unit_tests/test_routing_stats.py|tests/unit/proxy/test_routing_stats.py|migrated-tests|跨实例路由统计、active lease、幂等、可信metadata与只读状态接口回归|
|M074|tests/router_unit_tests/test_router_aresponses_streaming_fallback.py|tests/unit/test_router/test_router_aresponses_streaming_fallback.py|migrated-tests|Responses流fallback与partial usage/完成事件兼容回归|
|M075|tests/router_unit_tests/test_router_helper_utils.py|tests/unit/test_router/test_router_helper_utils.py|migrated-tests|Router helper接口/priority/cooldown与新版公共方法适配|
|M076|tests/test_litellm/caching/test_dual_cache.py|tests/unit/caching/test_dual_cache.py|migrated-tests|DualCache默认内存TTL及Redis回填回归|
|M077|tests/test_litellm/caching/test_redis_cache.py|tests/unit/caching/test_redis_cache.py|migrated-tests|Redis generation/strict批量读取与原缓存回归；真实Lua用例迁到integration层|
|M078|tests/test_litellm/completion_extras/litellm_responses_transformation/test_completion_extras_litellm_responses_transformation_transformation.py|tests/unit/completion_extras/litellm_responses_transformation/test_completion_extras_litellm_responses_transformation_transformation.py|migrated-tests|Responses-to-Chat reasoning/custom/namespace工具往返回归|
|M079|tests/test_litellm/litellm_core_utils/prompt_templates/test_litellm_core_utils_prompt_templates_factory.py|tests/unit/litellm_core_utils/prompt_templates/test_litellm_core_utils_prompt_templates_factory.py|migrated-tests|可信 DeepSeek inline/sidecar thinking与推理去重/禁用回归；普通 Anthropic 两个 inline-redacted 参数场景由官方转换/placement 语义替代，保留新名称和期望断言|
|M080|tests/test_litellm/litellm_core_utils/test_litellm_logging.py|tests/unit/litellm_core_utils/test_litellm_logging.py|migrated-tests|部署model_info/provider计价、非破坏性usage、已算成本优先及provider成本实际收费回归|
|M081|tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/test_anthropic_experimental_pass_through_adapters_transformation.py|tests/unit/llms/anthropic/pass_through/adapters/test_anthropic_experimental_pass_through_adapters_transformation.py|migrated-tests|Anthropic-to-Chat reasoning和显式thinking disabled回归|
|M082|tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/test_handler_output_config_passthrough.py|tests/unit/llms/anthropic/pass_through/adapters/test_handler_output_config_passthrough.py|migrated-tests|handler显式/默认思考请求状态及同步/异步输出回归|
|M083|tests/test_litellm/llms/anthropic/experimental_pass_through/adapters/test_streaming_iterator_combined_chunk.py|tests/unit/llms/anthropic/pass_through/adapters/test_streaming_iterator_combined_chunk.py|migrated-tests|同一chunk reasoning/text/tool/finish组合在思考禁用时不丢有效内容|
|M084|tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py|tests/unit/llms/anthropic/pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py|migrated-tests|Messages规范化之后工具形状日志回归|
|M085|tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_mcp_handler.py|tests/unit/llms/anthropic/pass_through/messages/test_mcp_handler.py|migrated-tests|MCP递归可信上下文保留及提供方边界不泄漏回归|
|M086|tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/test_responses_adapters_streaming_iterator.py|tests/unit/llms/anthropic/pass_through/responses_adapters/test_responses_adapters_streaming_iterator.py|migrated-tests|Responses-to-Messages unsigned thinking起始signature回归|
|M087|tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/test_responses_adapters_transformation.py|tests/unit/llms/anthropic/pass_through/responses_adapters/test_responses_adapters_transformation.py|migrated-tests|Responses/Anthropic思考与工具块分组、summary和加密推理往返回归|
|M088|tests/test_litellm/llms/anthropic/test_anthropic_common_utils.py|tests/unit/llms/anthropic/test_anthropic_common_utils.py|migrated-tests|Anthropic工具ID扫描/重写统计及原合法性回归|
|M089|tests/test_litellm/llms/azure_ai/chat/test_azure_ai_transformation.py|tests/unit/llms/azure_ai/chat/test_azure_ai_transformation.py|migrated-tests|Azure消息内部推理字段/图片清理回归|
|M090|tests/test_litellm/llms/bedrock/chat/invoke_transformations/test_amazon_deepseek_transformation.py|tests/unit/llms/bedrock/chat/invoke_transformations/test_amazon_deepseek_transformation.py|migrated-tests|Bedrock </think>任意chunk边界、usage与终止语义回归|
|M091|tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py|tests/unit/llms/custom_httpx/test_llm_http_handler.py|migrated-tests|HTTP provider dispatch和工具/超时/可信上下文回归；退役官方已删的Rust Anthropic fast-path项|
|M092|tests/test_litellm/llms/dashscope/test_dashscope_cost_calculator.py|tests/unit/llms/dashscope/test_dashscope_cost_calculator.py|migrated-tests|Dashscope DeepSeek thinking与非thinking价格选择回归|
|M093|tests/test_litellm/llms/deepseek/chat/test_deepseek_chat_transformation.py|tests/unit/llms/deepseek/chat/test_deepseek_chat_transformation.py|migrated-tests|DeepSeek Chat历史/思考策略/vision/工具choice回归|
|M094|tests/test_litellm/llms/deepseek/messages/test_deepseek_anthropic_messages_transformation.py|tests/unit/llms/deepseek/messages/test_deepseek_anthropic_messages_transformation.py|migrated-tests|DeepSeek原生Messages全路径、历史、图片、thinking、SSE、签名与禁fallback回归|
|M095|tests/test_litellm/llms/fireworks_ai/chat/test_fireworks_ai_chat_transformation.py|tests/unit/llms/fireworks_ai/chat/test_fireworks_ai_chat_transformation.py|migrated-tests|遵循新版Fireworks合法reasoning_content回放及thinking_blocks清理，替代旧一刀切断言|
|M096|tests/test_litellm/llms/hosted_vllm/chat/test_hosted_vllm_chat_transformation.py|tests/unit/llms/hosted_vllm/chat/test_hosted_vllm_chat_transformation.py|migrated-tests|遵循新版vLLM合法字符串reasoning_content回放和非法值清理，替代旧一刀切断言|
|M097|tests/test_litellm/llms/openai/test_use_chat_completions_api_no_leak.py|tests/unit/llms/openai/test_use_chat_completions_api_no_leak.py|migrated-tests|内部routing stats参数不泄漏提供方及Chat API参数兼容回归|
|M098|tests/test_litellm/proxy/common_utils/test_reset_budget_job.py|tests/unit/proxy/common_utils/test_reset_budget_job.py|migrated-tests|预算CAS/rollover与新窗口并发支出回归；真实PG/Redis用例迁往integration/spend|
|M099|tests/test_litellm/proxy/pass_through_endpoints/llm_provider_handlers/test_anthropic_passthrough_logging_handler.py|tests/unit/proxy/pass_through_endpoints/llm_provider_handlers/test_anthropic_passthrough_logging_handler.py|migrated-tests|原生Anthropic成本入payload/response及opt-in结构诊断回归|
|M100|tests/test_litellm/proxy/proxy_server/test_spend_counters.py|tests/unit/proxy/proxy_server/test_spend_counters.py|migrated-tests|新版批量支出、reservation、scope并发与缓存floor回归，融合本机generation契约|
|M101|tests/test_litellm/proxy/test_common_request_processing.py|tests/unit/proxy/test_common_request_processing.py|migrated-tests|Messages保活/首帧/流中/延迟HTTP错误类型与工具流timing回归|
|M102|tests/test_litellm/responses/litellm_completion_transformation/test_litellm_completion_responses.py|tests/unit/responses/litellm_completion_transformation/test_litellm_completion_responses.py|migrated-tests|Chat-to-Responses多step/重复tool index/完成快照/签名历史及官方built-in工具兼容回归|
|M103|tests/test_litellm/responses/test_v197_response_migration_regressions.py|tests/unit/responses/test_v197_response_migration_regressions.py|migrated-tests|namespace工具往返、foreign ID回放、parallel工具终态与隔离cache回归|
|M104|tests/test_litellm/router_utils/pre_call_checks/test_deployment_affinity_check.py|tests/unit/router_utils/pre_call_checks/test_deployment_affinity_check.py|migrated-tests|affinity所选order、previous_response显式fallback及encrypted pin交互回归|
|M105|tests/test_litellm/router_utils/test_fallback_event_handlers.py|tests/unit/router_utils/test_fallback_event_handlers.py|migrated-tests|fallback metadata通道和内部bookkeeping不污染提供方回归|
|M106|tests/test_litellm/router_utils/test_health_check_allowed_fails_integration.py|tests/unit/router_utils/test_health_check_allowed_fails_integration.py|migrated-tests|health check采用部署独立cooldown而非全局值回归|
|M107|tests/test_litellm/test_deepseek_model_metadata.py|tests/unit/test_deepseek_model_metadata.py|migrated-tests|DeepSeek主/备模型目录vision与bare/prefixed一致性，以及迁入的模型token上限覆盖|
|M108|tests/test_litellm/test_router.py|tests/unit/test_router/test_router.py|migrated-tests|真实选中部署model_info同步预建logger/计价及官方Router行为回归|
|M109|tests/test_litellm/test_router_order_fallback.py|tests/unit/test_router_order_fallback.py|migrated-tests|Messages order/跨协议外部fallback与明确本地错误禁fallback回归|
|M110|tests/test_litellm/test_utils.py|tests/unit/test_utils.py|migrated-tests|公共utils与模型能力/价格字段适配；旧DeepSeek上限断言应迁入现有专用metadata测试，避免恢复过时整套价格断言|
|M111|ui/litellm-dashboard/src/app/layout.tsx|ui/litellm-dashboard/src/app/layout.tsx|migrated|保留离线UI构建能力，不依赖next/font/google外网字体下载，沿用官方新版provider及布局结构|
|M112|ui/litellm-dashboard/src/lib/http/schema.d.ts|ui/litellm-dashboard/src/lib/http/schema.d.ts|migrated|按合入后的真实OpenAPI重新生成UI schema类型，包含本机只读观测接口，未保留旧生成物覆盖新版UI|

## 退役与替代说明

- M001 .python-version：采用官方不跟踪 .python-version 的策略；保留官方 pyproject 支持范围、uv.lock 和根 Dockerfile 的 Python 3.13 运行环境；ignored 工作区文件与验证venv 3.13.14仅作环境证据，不宣称版本锁已合入
- M004 Makefile：保留新版官方 Makefile 和 BASE_REF/default_branch.py 机制，替代旧 LINT_BASE 兼容补丁；门禁仍使用两槽位机制
- M016 docker/Dockerfile.deepseek-vision：退役固定 1.99.1 增量镜像配方，避免旧依赖基底叠加 1.106.0 源码；候选统一从根 Dockerfile 完整构建，历史配方可从旧提交恢复
- M023 litellm/litellm_core_utils/prompt_templates/common_utils.py：采用官方共享 reasoning helper、加密 reasoning 支持与新版解析接口，覆盖旧 readable thinking flatten；不覆盖回旧重复实现
- M031 litellm/llms/anthropic/experimental_pass_through/responses_adapters/streaming_iterator.py：保留官方新 Responses-to-Messages SSE 实现，已给 unsigned thinking 起始块提供空 signature，且支持 encrypted/redacted reasoning；旧单行修复已覆盖
- M043 litellm/llms/hosted_vllm/chat/transformation.py：采用官方 vLLM 合法字符串 reasoning_content 回放及非字符串清理，取代旧一刀切移除；thinking_blocks 仍清理，现有新版回归覆盖
- M070 scripts/pre_commit_lint.sh：采用官方pre-commit BASE_REF/default分支解析、范围门禁和两槽位机制，替代旧LINT_BASE补丁，未禁用门禁

## 回归测试迁移与例外

40份旧测试文件已对应新版tests/unit路径；真实PostgreSQL/Redis用例另外移至tests/integration/spend/test_local_budget_generation.py与test_local_budget_reset.py。清单逐项记录旧新增或改动的test函数、保留/移位/替代位置，不仅检查新增函数名

- M077 test_spend_counter_generation_preserves_concurrent_increments_and_deduplicates_reset：relocated-to-integration；tests/integration/spend/test_local_budget_generation.py
- M077 test_spend_counter_generation_preserves_reseeded_new_window：relocated-to-integration；tests/integration/spend/test_local_budget_generation.py
- M077 test_spend_counter_generation_snapshot_missing_counter_does_not_create_zero：relocated-to-integration；tests/integration/spend/test_local_budget_generation.py
- M077 test_spend_counter_generation_ttl_tracks_counter_and_nx_does_not_replace_generation：relocated-to-integration；tests/integration/spend/test_local_budget_generation.py
- M077 test_spend_counter_pending_reset_eventually_expires_despite_active_writers：relocated-to-integration；tests/integration/spend/test_local_budget_generation.py
- M079 test_anthropic_messages_pt_preserves_inline_redacted_thinking_without_sidecar_duplication：superseded-by-official-placement-contract；tests/unit/litellm_core_utils/prompt_templates/test_litellm_core_utils_prompt_templates_factory.py
- M091 test_async_anthropic_messages_handler_logs_rust_provider_response：superseded-or-retired；官方删除 _maybe_rust_anthropic_messages fast-path，保留普通/原生Messages诊断；不恢复不存在的运行路径
- M095 test_transform_messages_helper_strips_thinking_blocks：superseded-or-retired；官方新增reasoning_history要求回放合法字符串，旧全删断言由新版测试替代
- M096 test_hosted_vllm_thinking_blocks_prepended_to_assistant_content：superseded-or-retired；官方vLLM支持合法reasoning_content字符串，新回归同时检验thinking_blocks移除与非法字段类型
- M098 test_key_budget_admission_uses_fresh_window_after_stale_db_floor：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_cold_counter_blocks_late_old_window_reseed：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_counter_expiry_after_snapshot_blocks_late_old_window_reseed：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_db_cas_preserves_racing_spend_and_rejects_stale_worker：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_db_commit_then_redis_expiry_and_reseed_preserves_spend：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_failed_redis_reconciliation_cannot_extend_old_spend_forever：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_rejected_db_commit_preserves_redis_spend：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_rejects_repair_that_read_old_window_before_reset：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M098 test_key_budget_reset_unavailable_generation_does_not_advance_db_window：relocated-to-integration；tests/integration/spend/test_local_budget_reset.py
- M110 test_deepseek_v4_models_in_backup_cost_map：relocated-to-dedicated-metadata-test；tests/unit/test_deepseek_model_metadata.py
- M110 test_deepseek_v4_models_in_cost_map：relocated-to-dedicated-metadata-test；tests/unit/test_deepseek_model_metadata.py

## 类型边界补充适配

94处GenericLiteLLMParams构造改为model_validate(dict(...))，分布25文件，其中21个文件不在原112项净补丁目标中。它们属于新增thinking价格字段的类型边界适配；显式关键字、kwargs展开顺序、重复键提前拒绝、验证器与可信context身份已独立对照。完整文件数、当前Git跟踪状态和哈希列于JSON的parameter_boundary_adaptations；专项证据见review-param-boundary.zh-CN.md

|文件|构造转换数|原112项目标内|
|---|---|---|
|litellm/assistants/main.py|8|补充适配|
|litellm/containers/endpoint_factory.py|1|补充适配|
|litellm/containers/main.py|6|补充适配|
|litellm/evals/main.py|11|补充适配|
|litellm/files/main.py|5|补充适配|
|litellm/fine_tuning/main.py|4|补充适配|
|litellm/google_genai/main.py|2|补充适配|
|litellm/images/main.py|1|补充适配|
|litellm/interactions/agents/main.py|5|补充适配|
|litellm/interactions/main.py|4|补充适配|
|litellm/llms/anthropic/pass_through/messages/handler.py|1|是|
|litellm/llms/azure/image_edit/transformation.py|1|补充适配|
|litellm/llms/azure_ai/agents/transformation.py|1|补充适配|
|litellm/llms/azure_ai/anthropic/messages_transformation.py|1|补充适配|
|litellm/llms/azure_ai/anthropic/transformation.py|1|补充适配|
|litellm/llms/azure_ai/chat/transformation.py|1|是|
|litellm/llms/custom_httpx/llm_http_handler.py|1|是|
|litellm/proxy/vector_store_endpoints/management_endpoints.py|2|补充适配|
|litellm/realtime_api/main.py|5|补充适配|
|litellm/rerank_api/main.py|1|补充适配|
|litellm/responses/main.py|7|是|
|litellm/skills/main.py|4|补充适配|
|litellm/vector_store_files/main.py|6|补充适配|
|litellm/vector_stores/main.py|6|补充适配|
|litellm/videos/main.py|9|补充适配|

## 冻结源码完整差异范围

112原净补丁项包含2退役项与5官方已覆盖/目标无净差异项，因此冻结候选相对官方实际变化105个原目标+21参数扩展+15其它必要适配=141；参数边界25文件中4个已在原112目标中，不重复计数。最终审查文档另行入库不属于该冻结源码差异集合。

下表为原112项目标和21个参数边界扩展文件以外的15个必要适配，141个实际变化路径的互斥归类、冻结/当前哈希与契约见JSON的complete_frozen_scope_inventory。原112项是迁移来源清单，不能拿112+21+15直接当实际文件数

|路径|处理契约|
|---|---|
|UPGRADE_V1_106_0.md|固定基线、源、目标、正常双亲merge及分阶段验收；当前生产仍1.99.1-custom，源码合入不宣称上线|
|litellm/caching/in_memory_cache.py|delete_cache key补str/None返回注解，继续调用_remove_key；TTL和容量清理语义不变|
|litellm/caching/redis_batch.py|官方bulk/deferred increment/set/delete转共享Lua/generation协议，返回指令数仍1，防止绕过窗口和TTL|
|litellm/llms/anthropic/chat/handler.py|bash_code_execution在server_tool_use开始时发出code_interpreter_calls状态，使后续delta/done与官方server tool桥衔接|
|litellm/llms/anthropic/pass_through/messages/utils.py|新prepare_native_messages入口合入工具ID诊断与可信DeepSeek路径；保留官方web search/cache-control处理|
|litellm/proxy/_lazy_openapi_snapshot.json|新增两条只读routing-stats/model-status端点快照，与实际OpenAPI/UI生成物对应|
|litellm/proxy/db/spend_counter_reseed.py|key/team窗口判定由private升为跨模块public helper，保留旧私有alias，选择规则不变|
|litellm/responses/litellm_completion_transformation/custom_tools.py|custom_tool_names接受抽象Set以支持frozenset；输出kwargs类型兼容后续rich payload，不改变function/custom工具分类与序列化|
|litellm/router_utils/pre_call_checks/encrypted_content_affinity_check.py|真实请求kwargs空dict也保持身份，每轮清除旧/伪造_encrypted_content_affinity_pinned，再由服务端已验证加密归属设置|
|tests/integration/spend/test_local_budget_generation.py|原单元层真实Redis Lua契约迁入隔离integration，覆盖TTL、CAS、bulk/deferred/set/delete和lease更新|
|tests/integration/spend/test_local_budget_reset.py|原单元层真实PG/Redis窗口重置迁入隔离integration，覆盖DB CAS、并发/旧worker、generation丢失和拒绝提交不改缓存|
|tests/unit/proxy/test_litellm_pre_call_utils.py|请求清理矩阵新增_encrypted_content_affinity_pinned，防客户端把伪造标志提升为可信；其余差异为import/空行整理|
|tests/unit/responses/litellm_completion_transformation/test_handler.py|现有同步fallback测试采用functions__exec/函数名空间，匹配完整namespace保留契约，不把官方新工具协议还原旧裸名|
|tests/unit/responses/test_streaming_iterator.py|断言assembled日志副本已转chat usage，但原logging事件和client终态仍保留Responses usage，不再依赖原地改写|
|tests/unit/router_utils/pre_call_checks/test_encrypted_content_affinity_check.py|补真实pin回算与伪造/上一轮flag清理回归，覆盖空dict身份保留|

## 官方Anthropic契约调整

普通Anthropic两个inline-redacted旧参数场景改按1.106.0官方转换和placement契约：inline存在时阻止sidecar，普通inline redacted不由定制分支直接发出，signed thinking和可见text继续保留。对应测试改名为test_anthropic_messages_pt_uses_official_inline_redacted_and_sidecar_precedence。可信DeepSeek unsigned thinking、inline不可发送时sidecar回退及去重需求继续保留；native Messages合法redacted透传不经过该Chat factory。详见review-anthropic-placement.md。这是本机移植语义与官方契约对齐，不证明上游模型有缺陷，也不宣称旧普通Anthropic行为完全等价


## 验证与边界

最终regressions-acceptance为6838通过且0失败/错误/跳过；post-types-final为2466通过且0跳过；budget-real-acceptance为隔离PostgreSQL/Redis真实30项通过且0跳过。Azure恢复后另29项通过。历史regressions-final的6089通过/1项缺numpydoc跳过与routing-sse-final的1179通过只作历史证据，不替代最终acceptance，也不把重叠运行累加成互不重复总数

费用优先级已独立实调对象/dict、原terminal/assembled、无已算成本/已算0/已算0.125，金额和缓存0及client payload隔离均正确；证据review-completeness-current-cost.json

最终源码efee6c541fe0add9e6fbf7f9fdab1d330b518185的正式make check已在make-check-source-final.log记录check: PASS；按变更范围跳过无改动的tests/e2e检查，该记录不是模型端到端测试。候选镜像、完整数据库副本和真实Harness/生产发布仍需单独验收。生产CURRENT_BASELINE.md逐字保持定制源版本
