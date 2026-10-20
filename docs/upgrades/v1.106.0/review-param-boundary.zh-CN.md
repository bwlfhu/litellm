# GenericLiteLLMParams 类型边界专项独立审查

最终源码冻结 `efee6c541fe0add9e6fbf7f9fdab1d330b518185`，94处AST已逐项重核；未改源码、未运行heavy gate、未访问生产或发模型请求。转换前依据param-boundary-conversions.json，逐项参数与当前文件哈希见review-param-boundary-ast.json，实际模型对照见review-param-boundary-runtime.json

结论：94处/25文件的转换在当前GenericLiteLLMParams继承与验证器实现下保持业务行为，未发现阻断。get_litellm_params将私有显式参数改从kwargs取值也保持仓库现有关键字调用及可信owner边界

## 参数、顺序及错误边界

对94个before表达式建立AST，逐一与model_validate内部dict的参数AST比较，显式关键字、**展开表达式、展开顺序和字典推导式完全相同。候选没有省略或重复转换。未发现dict局部变量/形参/函数遮蔽；realtime中的一个dict(model_params)是HEAD中原本已有的独立包装，已排除出94计数

对11种不同调用形状用实际Pydantic模型执行前后两种写法，model_dump、model_fields_set、extra、可信context对象身份均相同。另验证6组默认/保留关键字过滤/max_retries/drop_params/3个thinking价格/非法认证header/非法费率输入，成功值或ValidationError的类型、位置、消息均一致

显式关键字在**展开前、后两种重复场景均在模型验证前抛TypeError；None与非字符串key展开仍抛TypeError。错误文字中的调用名会从GenericLiteLLMParams变为dict，异常类别及提前拒绝语义不变，没有采用会静默覆盖重复值的字典字面量合并

## 构造与模型生命周期

实际MRO：GenericLiteLLMParams、CredentialLiteLLMParams、CustomPricingLiteLLMParams、MirroredPricingParams、LiteLLMBaseModel、Pydantic BaseModel。__pydantic_custom_init__为False，__init__就是BaseModel.__init__；继承链没有被跳过的自定义构造副作用。LiteLLMBaseModel在TYPE_CHECKING下的__init__只是静态声明

Generic自身的before模型验证器preprocess_input_data、provider_affinity_header和drop_params字段验证器在两条验证入口中保留；extra=allow与arbitrary_types_allowed配置未改。LiteLLMBaseModel.model_post_init的deferred schema重建由同一Pydantic模型生命周期执行，没有使用model_construct绕开它

## 私有上下文取值

仓库25处get_litellm_params调用均为关键字调用，无依赖已移除末尾私有位置参数的调用。当前main.py仍将_litellm_deployment_protocol_context按关键词传入，kwargs.get取出原对象并放入相同输出key，未构造新对象或将字典标记提升为可信对象

实际对照：可信owner对象身份保留且获准，伪造同形字典虽作为值传递但仍被get_deployment_protocol_context拒绝，缺失值仍为None并拒绝。owner身份检查代码未被删除或放宽。调用点清单见review-get-params-callers.json

## 审查范围补充

这批类型适配涉及25文件，其中21个不在原v1.99.1..29532d2608的112项净补丁清单内。它们是适配新增3个价格字段后的验证边界修改，并非新业务需求；最终迁移范围应把这21个补充文件也记录，避免只列112项却遗漏实际新增改动。逐文件计数及哈希见param-boundary-adaptation-inventory.review.json

|文件|转换数|是否在原112项目标中|
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

本结论是局部类型边界等价性结论；最终候选冻结后仍应核对审查哈希，并以最终门禁及相关endpoint测试记录作为完整准入依据。没有把现有未完成门禁或生产验证记为通过

最终收尾：Azure validate_environment 保留原 isinstance(dict) 兜底与 dict(**...) 展开，非dict仍转空字典，非字符串key仍在Pydantic之前抛TypeError；8组合法/validator输入及3组旧边界独立对照通过，见review-param-boundary-azure-restored.json。litellm_params注解由dict扩大object只准确表达既存接收边界，不改运行时guard。最终94处均为dict展开验证，0处直接绕过原展开表达式；不采用中途direct验证方案。最终静态门禁见make-check-source-final.log，check: PASS；其它端到端与生产边界见最终审查报告
