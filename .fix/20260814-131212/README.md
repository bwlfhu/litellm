# DeepSeek Anthropic thinking 兼容修复方案

## 1. 任务摘要

受影响的是线上将 DeepSeek V4 reasoning deployment 声明为 Anthropic 的渠道。当前请求在 LiteLLM 内部被当成普通 Claude Anthropic deployment 处理；响应中的 DeepSeek `reasoning_content` 被转换成没有 Claude 加密签名的 `thinking` block，随后通用 Anthropic 逻辑为满足 Claude 的协议约束而丢弃该 block。下一次带工具续接时，DeepSeek 收不到上一轮完整 reasoning，返回 400，Router 再尝试低优先级 deployment

目标是增加受 deployment 配置严格控制的 `deepseek_anthropic` reasoning protocol。它继续使用 Anthropic HTTP Messages transport 和 `/v1/messages`，但使用 DeepSeek 的无签名 thinking 规则。Claude 的签名校验和清理逻辑保持不变，普通无工具多轮不强制携带 reasoning

本文件是实施设计，不包含线上 URL、密钥、请求正文或客户配置

## 2. 证据与官方方案核对

### DeepSeek 协议约束

- [Anthropic API 兼容](https://api-docs.deepseek.com/zh-cn/guides/anthropic_api) 支持 `content` 中的 `type: "thinking"`，并支持 `thinking` 与 `output_config.effort`
- [Thinking 模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode) 要求带工具调用时，后续请求完整回传之前 assistant 的 `reasoning_content`，缺失会 400；普通无工具多轮可以不回传
- [Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api) 是无状态接口，不支持依赖 `previous_response_id` 自动取得历史；下一轮必须由调用方提供 reasoning input item。思考控制使用 `reasoning.effort`：`none` 关闭，`low/high/max` 开启，省略时默认 `high`
- [KV cache](https://api-docs.deepseek.com/zh-cn/guides/kv_cache) 按完整前缀命中，保留 reasoning 历史是缓存前缀的一部分，不应在代理层删除

### LiteLLM 官方条目

- [Issue #31439](https://github.com/BerriAI/litellm/issues/31439)：DeepSeek Anthropic-compatible `/v1/messages` 工具多轮缺少 `reasoning_content`
- [PR #31440](https://github.com/BerriAI/litellm/pull/31440)：原始修复，已关闭
- [PR #32110](https://github.com/BerriAI/litellm/pull/32110)：重开版本，补充从 `provider_specific_fields.reasoning_content` 恢复，并增加不变性和多轮测试；截至本任务快照未合并
- [PR #26678](https://github.com/BerriAI/litellm/pull/26678)：DeepSeek OpenAI-compatible Chat 多轮 reasoning 修复；截至本任务快照未合并
- [PR #27425](https://github.com/BerriAI/litellm/pull/27425)：Responses 工具续接时重建 reasoning；截至本任务快照未合并
- [PR #32337](https://github.com/BerriAI/litellm/pull/32337)：讨论 `reasoning_content` 是否应无条件转成 thinking block，提示必须按客户端 thinking 开关判断

这些 PR 不能整体 cherry-pick。#32110 的空格占位会隐藏真实历史丢失；#27425 主要解决 LiteLLM Responses session/history，不能替代 DeepSeek Anthropic wire 编译。移植前应在目标 release tag 上逐文件检查依赖，并用 `git range-diff` 确认只保留必要差异

## 3. 当前代码与真实数据流

当前业务源码基线为 `85b778c2a1`，已包含官方 #28200。实施前必须先用脱敏 route trace 确认线上请求的入口、Router 解析后的 provider、所选 config 和 bridge；不能仅凭模型名或一段 adapter 日志推断整条调用链

### 3.1 路径 A：原生 `/v1/messages`

```text
Anthropic endpoint
-> Router 选择 deployment
-> anthropic messages handler
-> AnthropicMessagesConfig 或 DeepSeekAnthropicMessagesConfig
-> upstream /v1/messages
-> 原样 Anthropic JSON/SSE
-> 客户端回传下一轮历史
```

- [`litellm/llms/deepseek/messages/transformation.py:15`](/home/allcam/projects/litellm/litellm/llms/deepseek/messages/transformation.py:15) 已有 `DeepSeekAnthropicMessagesConfig`，但只负责 URL、header 和 tool 清理
- [`litellm/llms/anthropic/experimental_pass_through/messages/handler.py:537`](/home/allcam/projects/litellm/litellm/llms/anthropic/experimental_pass_through/messages/handler.py:537) 只按 provider 选择 config，不能让仍声明为 `anthropic` 的 deployment 启用 DeepSeek reasoning 语义
- [`litellm/llms/anthropic/experimental_pass_through/messages/transformation.py:494`](/home/allcam/projects/litellm/litellm/llms/anthropic/experimental_pass_through/messages/transformation.py:494) 原样返回上游响应。这条路径的历史所有者是客户端，不要求代理把 reasoning 自动写入 Responses session

### 3.2 路径 B：公共 `/v1/responses`

```text
Responses endpoint
-> Router 选择 deployment
-> responses/main.py 独立解析 provider/config
-> 无 native Responses config 时进入 Responses-to-Chat bridge
-> completion provider transformation
-> upstream
-> ModelResponse/stream 转回 Responses output
-> 可选 LiteLLM session 重建
```

- [`litellm/responses/main.py:955`](/home/allcam/projects/litellm/litellm/responses/main.py:955) 独立解析 provider，并在 [`:1030`](/home/allcam/projects/litellm/litellm/responses/main.py:1030) 选择 Responses config；不会经过路径 A 的 Messages selector
- DeepSeek 当前没有 Python native Responses config，因此会在 [`litellm/responses/main.py:1090`](/home/allcam/projects/litellm/litellm/responses/main.py:1090) 进入 `LiteLLMCompletionTransformationHandler`
- [`litellm/responses/litellm_completion_transformation/handler.py:39`](/home/allcam/projects/litellm/litellm/responses/litellm_completion_transformation/handler.py:39) 先把 Responses input 转成 Chat messages，再调用 `completion`。如果 deployment 被声明为普通 Anthropic，后续仍会触发 Claude 的 signature 规则
- 这条路径的历史所有者是显式 Responses input，或 LiteLLM 已成功持久化并能由 `previous_response_id` 重建的 Responses output/session

### 3.3 路径 C：`/v1/messages` 到 OpenAI Responses adapter

[`litellm/llms/anthropic/experimental_pass_through/messages/handler.py:52`](/home/allcam/projects/litellm/litellm/llms/anthropic/experimental_pass_through/messages/handler.py:52) 当前只把 `openai` provider 路由到 `LiteLLMAnthropicToResponsesAPIAdapter`。其中 [`transformation.py:161`](/home/allcam/projects/litellm/litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py:161) 确实把 thinking 错当成 `output_text`，但这不是当前源码中的默认 DeepSeek 路径

该 adapter 的 reasoning 修复作为独立通用兼容阶段处理。只有 route trace 证明线上 DeepSeek 请求实际经过这里时，它才是本次故障的阻断项

## 4. 设计原则与不变量

1. **两个入口，一个可信 context**：Messages 与 Responses 各自做入口分流，但只消费 Router 在选定 deployment 后生成的 typed protocol context，不直接信任请求 kwargs 中的 `model_info`
2. **客户端字段不能启用**：忽略客户端 `metadata`、header、模型名、`model_info` 和预先传入的内部 kwarg；Router 每次选定 deployment 后重新生成带 provenance 的 protocol context
3. **canonical 只做转换契约**：canonical assistant turn 包含 visible content、可选明文 reasoning 和有序 tool calls，不承担跨请求存储职责
4. **历史所有者按路径确定**：原生 Messages 由客户端回传 content blocks；Responses 由显式 input 或可验证的本地 session history 提供；SpendLog 不是所有路径的隐式真相源
5. **DeepSeek 无签名**：发往 DeepSeek Anthropic 的 thinking block 只包含 `type` 和 `thinking`，不伪造 Claude signature，也不把 reasoning 降级成 text
6. **`redacted_thinking` 不可转换**：DeepSeek 官方明确不支持该 block，且 `data` 不可逆。无工具历史可按明确策略丢弃；tool-associated 历史始终返回不可恢复错误，不能因本轮关闭 thinking 而变为可转换
7. **生成状态与历史义务分离**：`generation_thinking_enabled` 只控制本轮是否生成 thinking；`history_reasoning_required` 由已保留工具会话决定。一旦后者为 true，历史 reasoning 在后续请求中始终必须回放
8. **禁止破坏性模式切换**：已有工具历史时，Messages `thinking.type=disabled` 或 Responses `reasoning.effort=none` 返回稳定冲突 400；不得删除历史 reasoning 后继续请求
9. **工具会话完整性**：按 `tool_use.id <-> tool_result.tool_use_id` 建立调用图，不用 user message 位置推断 turn。工具会话中从首次 tool use 开始的 assistant 历史后缀都必须有非空明文 reasoning，包括不含 tool call 的普通 assistant 节点
10. **顺序与配对**：thinking、visible text、tool call 以及后续 tool result 保持原始顺序；并行 tool calls 属于同一个 assistant 节点，每个 call id 在请求历史中唯一且恰好匹配一个 result
11. **无状态边界**：不向 DeepSeek 上游发送 `previous_response_id`。只有代理已重建完整 history 时才展开为 wire input，否则返回明确 400
12. **单一计费事实源**：provider usage 只归一化一次，所有日志、cost 和 metrics 读取同一个 `Usage`，不得分别累加别名字段

## 5. 实施方案

### 5.1 Trusted protocol context 与传播

在 Router model info 类型中增加内部枚举字段：

```yaml
model_list:
  - model_name: <public-alias>
    litellm_params:
      model: anthropic/<deepseek-model>
      api_base: <provider-base>
    model_info:
      reasoning_protocol: deepseek_anthropic
```

`model_info.reasoning_protocol` 只是 deployment 配置来源，不能直接作为运行时授权。新增私有 Router factory：它在选定 deployment 后读取配置，生成冻结的 `DeploymentProtocolContext(protocol, deployment_id, attempt_id, provenance)`；`provenance` 必须与模块私有 capability object 做 identity 校验。public request 解析阶段无条件丢弃客户端传入的同名 context kwarg，Router factory 随后才写入真正对象。factory 同时生成删除 `reasoning_protocol` 的 `model_info` 副本供既有 kwargs/logging 使用，含该字段的权威 deployment 快照只留在 Router 内部

共享 runtime resolver 只接受 `DeploymentProtocolContext`，校验类型、私有 provenance、当前 deployment id 和 attempt id 后返回 `None | DEEPSEEK_ANTHROPIC`；原有 [`litellm/router.py:3152`](/home/allcam/projects/litellm/litellm/router.py:3152) 的普通 `kwargs["model_info"]` 继续供已有日志/路由功能使用，但不再能启用该协议。两个入口分别消费经过 provenance 校验的 context：

- **Messages consumer**：在按 provider 选 config 之前解析 protocol；命中时选择 `DeepSeekAnthropicMessagesConfig`
- **Responses consumer**：在 `responses/main.py` 选择 native config/Chat bridge 之前解析 protocol；命中时选择 DeepSeek Anthropic Responses bridge strategy，不能依赖 Messages handler 再次选择

Responses consumer 把解析后的不可变 protocol context 显式传入 request transformation、session handler、response transformation 和 streaming iterator。Router 内部持有选中 deployment 的权威快照；fallback 每次由 factory 从新 deployment 的原始 `model_info` 创建新 context，并替换旧对象，禁止合并或继承上一 deployment 的 protocol。direct SDK 调用没有 Router context 时必须解析为 `None`，即使调用方伪造 `model_info.reasoning_protocol` 或内部 kwarg 字典也不能启用

`reasoning_protocol` 是内部路由控制字段，不是 secret，但不能成为客户端可回传并影响路由的状态。实现一个返回副本的共享 model-info 出站 sanitizer，并同时用于 `/model/info`、`/v1/model/info`、`/v2/model/info`，包括 admin/debug 响应；deployment 配置 CRUD 可在受权配置对象中保留该字段，避免编辑时丢失。provider wire、普通 `metadata`、Responses response metadata、`proxy_server_request`、SpendLog 原始 metadata 和用户可见错误不得包含该字段。安全 route trace 可把 resolver 结果作为独立枚举标签记录，不能把整个 `model_info` 写入日志

建议保留 `DeepSeekAnthropicMessagesConfig` 并提取一个可组合的 DeepSeek reasoning codec。`deepseek_anthropic` 不注册为可由客户端直接传入的公开 provider

### 5.2 路径 A：原生 Messages request

在 `DeepSeekAnthropicMessagesConfig.transform_anthropic_messages_request()` 中执行无副作用的 decode、validate、encode：

1. 分别计算两个状态：`generation_thinking_enabled` 由本轮参数决定，`thinking.type == "disabled"` 为 false、enabled 为 true、省略时按 DeepSeek 默认值 true，未知 type 返回参数 400；`history_reasoning_required` 由 canonical history/session 的工具状态决定，与本轮开关无关
2. 每个 assistant message 是独立节点；同一 message 内的 text、thinking 和多个 `tool_use` 不拆分。建立全局 `tool_use.id -> assistant 节点` 索引，再按 `tool_result.tool_use_id` 建边，不使用 user message 作为外层边界
3. `tool_use.id` 或 `tool_result.tool_use_id` 缺失、空白、重复时返回 `tool_history_invalid`；result 找不到 use 时返回 `tool_result_orphaned`；一个 use 对应多个 result 或请求结束时仍无 result 时返回 `tool_history_incomplete`。这些结构校验不受 thinking 开关影响
4. 并行 calls 保持在原 assistant 节点并允许后续一个或多个 user message 中的 results 按 id 回配。混合 text/tool_use 仍是一个节点；连续 assistant tool 节点不合并，前一节点在出现后一节点前仍未完成时按 incomplete 拒绝
5. 图中出现首个 tool use 后，`history_reasoning_required=true`，并生成不可分割的 `ToolAssociatedCanonicalSuffix`：从首个 tool-use assistant 节点开始，包含每个后续 assistant 节点、明文 reasoning、tool_use、对应 tool_result、call-id 图、边界版本和完整性 digest。后缀内每个 assistant 节点都必须有非空明文 reasoning，包括完成 tool result 后产生的普通文本 assistant；仅声明 `tools`、但历史尚无 tool exchange 的首轮不触发历史义务
6. Responses session 一旦出现 tool use，就以原子记录持久化完整的 `ToolAssociatedCanonicalSuffix` 和其完整性信息；`history_reasoning_required` 只是索引/快速拒绝标记，不能作为历史完整性的证明。历史裁剪只能删除该 suffix 之前的前缀，不能删除 suffix 内任一 assistant、reasoning、tool_use 或 tool_result 节点；客户端自持的 Messages 历史只能按其实际回传内容判断，代理不能恢复客户端已删除的旧 tool exchange
7. 当 `history_reasoning_required=true` 时，无论本轮生成开关如何，都先验证 suffix 的版本、边界、digest、首个 tool use、每个 reasoning、tool_use/tool_result 配对和末端 assistant 后缀，再从 Anthropic thinking block、顶层 `reasoning_content` 或 `provider_specific_fields.reasoning_content` 恢复完整历史 reasoning。flag 仍在但 suffix、首个 tool use、任一 reasoning 或任一 tool_result 被裁掉时，直接返回 `reasoning_history_unrecoverable`；缺失或空白但结构仍可定位时返回 `reasoning_history_missing`，只有 redacted data 时返回 `reasoning_history_unrecoverable`
8. `history_reasoning_required=true && generation_thinking_enabled=false` 返回 `reasoning_mode_conflict` 400，不向上游发请求。这样既不丢历史，也不假设 DeepSeek 接受“关闭本轮 thinking 但回放工具 reasoning”的组合
9. 无工具历史且本轮 thinking disabled 时，可以省略普通 assistant 的可选 reasoning，并在 wire 中设置 `thinking.type=disabled`；原始输入对象、visible content 和 tool graph 不得修改
10. 允许请求时，按历史义务重建所需 thinking、text、tool_use block，移除 DeepSeek 不需要的 signature；`output_config.effort` 只调节本轮生成强度，不取消历史回放义务
11. 继续执行现有 custom tool type 清理，不改变 hosted tool type

`provider_specific_fields` 只作为兼容旧 ModelResponse 的恢复来源。恢复后不把该内部副本发送到 provider metadata

### 5.3 路径 A：原生 Messages response 与历史

原生 Messages 不建立隐式服务端会话：

- 非流式响应保持上游 Anthropic content blocks 原样返回；DeepSeek thinking 不要求 signature
- 流式响应保持 thinking delta、text delta、tool delta 和终态 SSE 顺序，由客户端累计并在下一请求原样回传
- logging parser 可以为 usage/observability 派生 canonical reasoning，但该派生值不是下一轮正确性的唯一来源
- 不要求同时写入 `proxy_server_request`、SpendLog 和 Responses session；只有实际消费该数据的 bridge 才持久化标准 Responses output
- Pydantic 和普通 dict 终态都使用安全 getter 读取 usage，避免 `dict object has no attribute usage`

DeepSeek 不会产生受支持的 `redacted_thinking`。如果入站历史来自 Claude fallback：无工具历史可删除整个 redacted block；tool-associated 历史无论本轮 thinking 开关为何都返回 `reasoning_history_unrecoverable`。任何分支都禁止把 `data` 当明文或注入占位符

### 5.4 路径 B：公共 Responses bridge

为 `deepseek_anthropic` 使用独立 bridge strategy，不把 DeepSeek 假装成支持 native Responses 的 provider：

#### 5.4.1 明确 dispatch 边界

在 [`litellm/responses/main.py:1090`](/home/allcam/projects/litellm/litellm/responses/main.py:1090) 进入通用 `LiteLLMCompletionTransformationHandler` 之前增加 protocol strategy 分支。命中 `deepseek_anthropic` 后不得调用 `litellm.completion()`、`litellm.acompletion()`、`litellm.anthropic_messages()` 或 Router public method；这些入口会再次解析 provider/路由，可能重新进入普通 Anthropic Chat signature 逻辑或造成二次 Router attempt

实际调用链固定为：

```text
Responses endpoint
-> Router 选定 deployment 并生成 protocol context
-> DeepSeekAnthropicResponsesBridge.prepare_request()
-> shared session reconstruction + canonical validator
-> _prepare_anthropic_messages_wire_request()
   -> DeepSeekAnthropicMessagesConfig.transform_anthropic_messages_request() exactly once
-> DeepSeekResponsesRawTransport.send(prebuilt_request)
-> configured DeepSeek /v1/messages
-> raw Anthropic JSON/SSE
-> DeepSeekAnthropicResponsesBridge response/stream transformation + final accounting
```

不能把预编译 body 传给现有 [`BaseLLMHTTPHandler.anthropic_messages_handler()`](/home/allcam/projects/litellm/litellm/llms/custom_httpx/llm_http_handler.py:2407)：它会在 [`async_anthropic_messages_handler():2109`](/home/allcam/projects/litellm/litellm/llms/custom_httpx/llm_http_handler.py:2109) 再次调用 config transform，并在非流式响应进入 [`_finalize_anthropic_messages_response():2295`](/home/allcam/projects/litellm/litellm/llms/custom_httpx/llm_http_handler.py:2295)。这会重复编译并把 Anthropic agentic/finalize 生命周期混入 Responses

从当前 handler 提取一个共享的 request-preparation primitive，并定义两种内部、强类型且不可从 public API 选择的 transport strategy：

1. `_prepare_anthropic_messages_wire_request(config: BaseAnthropicMessagesConfig, ...) -> PreparedAnthropicMessagesWireRequest`：复用环境校验、header 过滤、所选 config 的 transform、URL、签名、序列化和 timeout 解析；DeepSeek Path B 传入 `DeepSeekAnthropicMessagesConfig`，原生 Claude 传入选定的普通 Anthropic config。每个 attempt 返回冻结的 prebuilt body bytes/URL/headers/signed body/timeout，单个 attempt 内 config transform 只能发生一次。生成后该 attempt 的 body bytes、headers、签名和 URL 均不可变
2. `ClaudeAnthropicMessagesTransport`：原生 `/v1/messages` 继续保留 Claude 现有的签名恢复策略。签名类 400 可以触发第二次 attempt，但必须从不可变 canonical history 的深拷贝派生一个新的 attempt candidate，只在 candidate 上删除无效 Claude thinking block，再用选定的 Claude config 重新调用 `_prepare_anthropic_messages_wire_request()`；不得原地修改 canonical history、public input 或已发送的 frozen request，也不得把该可变恢复策略暴露给 DeepSeek Path B
3. `DeepSeekResponsesRawTransport`：公共 `/v1/responses` 使用不调用 `raise_for_status()` 且不启用隐式连接重试的专用低层发送路径，只发送一次冻结的 HTTP 请求，并返回强类型 `DeepSeekRawTransportResult`。成功结果包含 `status_code`、不可变 `headers`、`body_bytes` 或异步 `stream` 句柄及唯一 `close()`；400/其他 HTTP 状态作为结果交给 Responses owner，不转换成通用异常。`ConnectError` 和取消分别返回带 `kind`、原始错误、request count 和 close 状态的 typed failure/cancellation result；请求次数固定为一次，响应或 stream 必须由 owner 关闭且只关闭一次。不得调用现有会自动 `raise_for_status()` 或连接重试的 `AsyncHTTPHandler.post()` 默认路径，不得调用 `transform_anthropic_messages_request_on_http_error()`、删除 thinking block、重签名、原地修改 prebuilt request、更新 `logging_obj.model_call_details`、重复 `pre_call` 或写 retry body。该 transport 不调用 `update_from_kwargs`、Anthropic response transform、agentic hooks、fake stream、success/failure hook、Usage、cost 或 SpendLog；上游 400 原样交给 Responses accounting owner

原生 Messages handler 只复用共享的 request preparation，并通过 `ClaudeAnthropicMessagesTransport` 保留现有签名恢复和重新 prepare 行为，再继续执行原有 Anthropic response transform、stream wrapper 和 finalize，保持路径 A 行为。Responses bridge 复用同一 request preparation，但只能调用 `DeepSeekResponsesRawTransport`；每个选定 attempt 由 Responses logging owner 在 raw transport 前执行一次 provider pre-call，并直接解析 raw JSON，不重新执行 Router，也不进入 Anthropic finalize

Responses streaming 必须使用独立的 `DeepSeekAnthropicResponsesSSEDecoder` 纯 SSE decoder。它只负责 Anthropic SSE framing、reasoning/text/tool delta 累计和 Responses event 编码，不得实例化 `BaseAnthropicMessagesStreamingIterator`，不得调用 `PassThroughStreamingHandler` 或 `GLOBAL_PASS_THROUGH_SUCCESS_HANDLER_OBJ`，不得标记 `/v1/messages` pass-through route，也不得执行任何 success/failure logging、Usage、cost 或 SpendLog。完成事件中的累计 reasoning 由 Responses bridge 交给唯一 accounting owner；decoder 本身不保存跨请求 session，也不拥有终态生命周期

wire-level fake transport 必须断言 DeepSeek config transform 和首个 HTTP request 各一次，唯一出站 URL 是已选 deployment 的 `/v1/messages`，body 含所需无签名 thinking，且内部 protocol 字段不在 body/header/metadata 中。streaming 测试还必须对 `BaseAnthropicMessagesStreamingIterator`、`PassThroughStreamingHandler` 和 `GLOBAL_PASS_THROUGH_SUCCESS_HANDLER_OBJ` 设置 fail-fast spy，断言三者均未调用。两组 retry 测试必须互斥：Claude signature 类 400 允许第二次发送，断言第二次 request 来自重新 prepare 的已清理 Claude body、原始 canonical/public input 未改变并最终成功；DeepSeek 400 只返回一次 `DeepSeekRawTransportResult`，断言 request count 为一次、实际 body bytes 与 frozen prepared body 完全一致、config transform 只调用一次、没有 body mutation、重签名、retry logging 或成功 SpendLog。另加 ConnectError 和取消测试，断言均不隐式重试、stream/response close 恰好一次且只有一个 failure lifecycle。若未来启用连接类重试，DeepSeek 测试必须对每个 attempt 断言 body bytes、headers、签名和 URL 完全一致

#### 5.4.2 canonical 转换

1. Responses 只接受 `reasoning.effort` 的 `none | low | high | max`：`none` 映射为 `generation_thinking_enabled=false` 和 Anthropic `thinking.type=disabled`，同时不发送 `output_config.effort`；`low/high/max` 映射为 enabled，并分别写入 `output_config.effort`；省略 reasoning 或 effort 时显式按 enabled + `high` 编译，避免依赖隐式默认
2. 字段类型错误或 effort 为其他值，包括 `medium`、`xhigh` 和空字符串，返回稳定参数 400，不静默降级或套用 Chat Completion 的 effort 映射
3. Responses `reasoning` input item 解码为 pending canonical reasoning，并附着到紧随其后的 assistant message/function call；不得变成 `output_text`
4. 连续 function calls 合并为同一个 assistant 节点，保留 reasoning、call id 和顺序；`function_call_output` 用与路径 A 相同的 id 图 validator 配对
5. session/input 出现工具历史时设置 `history_reasoning_required=true` 并始终恢复、校验 reasoning；若当前 effort 为 `none`，返回 `reasoning_mode_conflict` 400，不删除 reasoning、不调用 raw transport
6. canonical history 最终只由 `DeepSeekAnthropicMessagesConfig` 编译一次 DeepSeek Anthropic `/v1/messages` body，不再经过 Chat transformation 或完整 Anthropic Messages handler
7. 非流式上游 thinking 转成 `ModelResponse.choices[0].message.reasoning_content`，再生成 Responses `reasoning` output item；function call 和 visible message 分别生成标准 output item
8. `DeepSeekAnthropicResponsesSSEDecoder` 累计 reasoning delta 并生成标准 reasoning events；完成时把同一累计结果交给 completed response/logging payload，不能只保存在 decoder 实例中。该 decoder 不得进入 Anthropic pass-through iterator 或 success handler
9. `response.failed`、iterator exception 或不完整终态不得写入伪造的成功 session history；Pydantic/dict terminal response 使用同一安全读取逻辑

#### 5.4.3 sync/async session 与 streaming

把现有 `async_responses_api_session_handler()` 拆成一个共享的 async reconstruction core 和一个纯函数 canonical validator。async bridge 直接 await core；sync bridge 使用仓库现有 [`run_async_function()`](/home/allcam/projects/litellm/litellm/litellm_core_utils/asyncify.py:70) 执行同一个 core，禁止维护第二套 session 拼接逻辑或使用裸 `asyncio.run()`

为 `previous_response_id` 持久化/读取的完整 Responses output 必须包含 reasoning item、完整 `ToolAssociatedCanonicalSuffix` 和版本/digest manifest，并与 response record 原子提交。显式 input 自身已包含完整 canonical history 时不依赖 session；一旦请求需要 response id 补全历史，以下情况在 sync/async 都返回相同的 `reasoning_history_unrecoverable` 400：未配置 session DB、SpendLog 不存在、cold-storage object 缺失/不可读、response id 不存在、已存 output 缺 reasoning、suffix manifest 缺失/不匹配或 suffix 任一节点被裁剪。现有“session 为空时只保留本轮新 input”的宽松分支不能用于 `deepseek_anthropic`

DeepSeek raw transport 只提供 async 网络实现。非流式 sync bridge 用 `run_async_function()` 覆盖从共享 request preparation、`DeepSeekResponsesRawTransport` 到 Responses response transformation 的整个 coroutine，不调用完整 Messages handler。sync streaming 不能在临时 loop 中取得 raw async stream 后关闭 loop；新增专用 sync Responses iterator，由一个 worker thread 持有同一 event loop 直至上游 response/iterator 完成、失败或取消，通过有界 queue 逐 event 转发，并在 close、客户端断开和异常时取消 task、关闭 response 和 join thread。HTTP proxy 的 async endpoint 继续直接消费 DeepSeek raw async stream，不经过该 sync wrapper

sync 与 async 都先调用同一个 canonical `prepare_request()`，再调用同一个 wire preparation、`DeepSeekResponsesRawTransport` 和 Responses response codec。测试必须分别覆盖非流式与流式两轮：第一轮 reasoning 被保存，第二轮由 `previous_response_id` 重建到同一 assistant 节点，最终 wire body 完全一致；并对无 DB、cold storage 缺失和未知 response id 做参数化等价断言

#### 5.4.4 父调用日志与计费生命周期

public `/v1/responses` 外层是该调用唯一的 accounting owner。bridge 持有一个 parent `LiteLLMLoggingObj`、`litellm_call_id` 和每次尝试的 attempt id，不创建嵌套 public call 或第二个 endpoint logging context。每个 provider attempt 可以有一次内部 pre-call 和一次 raw request，但不产生独立 SpendLog、cost hook 或最终 response Usage；Responses bridge 收集每个 attempt 实际产生的 provider usage，在 parent owner 内只归一化一次聚合 `Usage`，只执行一次最终 success/failure hook、aggregate cost calculation 和 parent SpendLog write

非流式成功、首 token 前失败、流中失败和正常 completed 都遵守同一不变量：

```text
one public Responses parent call
= one final success or failure lifecycle
= one parent SpendLog row
= one aggregate cost calculation
= one normalized aggregate response Usage

每个 attempt 只允许一个内部 request/pre-call/usage snapshot。首 token 前 fallback、部分输出后 fallback 和 fallback 失败都把已实际发生且可计费的 attempt usage 纳入 parent aggregate；没有 provider usage 的连接失败不虚构 token/cost。不得把 partial 与 fallback usage 取最大值、重复合并或分别写成独立 SpendLog
```

raw transport 不读取或记录 usage；streaming completed payload、callback payload 和 parent SpendLog 必须引用 Responses bridge 归一化的同一份最终 aggregate Usage 数值。无 fallback 的成功路径只有一个 attempt；fallback 路径允许多个 attempt request/pre-call，但 parent success/failure hook、aggregate cost calculator、SpendLog writer 和 response usage normalization 各只执行一次。测试分别覆盖首 token 前 fallback、部分输出后 fallback 和 fallback 失败，断言没有 child accounting、漏记或重复计费，也不得残留成功 SpendLog 或 Anthropic finalize/agentic hook

需要检查 #27425 的 pending reasoning、并行 function call 和 session reconstruction diff，但只移植与当前类契约兼容的最小部分

### 5.5 路径 C：Messages 到 OpenAI Responses adapter

作为单独通用修复，将 Anthropic `thinking` 映射为 Responses reasoning item，而不是 assistant `output_text`；响应 reasoning item 再映射回 Anthropic thinking block。该修改必须按客户端 thinking 开关隔离，并保留 OpenAI/Azure 的既有行为

此路径不与 DeepSeek protocol selector 共用运行时状态，只复用无副作用的 canonical value 类型。除非 Phase 0 route trace 证明线上故障经过该 adapter，否则不阻塞路径 A 的首个补丁

### 5.6 Usage、缓存与计费

当前 [`Usage`](/home/allcam/projects/litellm/litellm/types/utils.py:1622) 已把 `prompt_cache_hit_tokens` 映射到 `prompt_tokens_details.cached_tokens`，并在 [`:1673`](/home/allcam/projects/litellm/litellm/types/utils.py:1673) 设置 `_cache_read_input_tokens`。实施时以一次 `Usage(...)` 构造作为唯一归一化入口：

| 输入/输出 | 规则 |
| --- | --- |
| Provider `prompt_cache_hit_tokens=N` | 只传给 `Usage` 一次 |
| `prompt_tokens_details.cached_tokens` | 由 `Usage` 派生，必须等于 `N` |
| `_cache_read_input_tokens` / Anthropic `cache_read_input_tokens` | 同一 `N` 的别名视图，不是新增 token |
| SpendLog、Prometheus、cost | 只读标准 Usage，不再加总两个别名 |
| 未缓存 input | `prompt_tokens - cached_tokens - cache_creation_tokens`，下限为 0 |

回归测试必须断言 `cached_tokens == cache_read_input_tokens == N`，并用已知费率验证 cache read 只计费一次。缺少 provider cache-write 字段时不虚构 `cache_creation_input_tokens`

### 5.7 Router retry、fallback 与协议兼容

先区分 retry 和 fallback：400 默认不做同 deployment retry，但非流式外层 Router 仍可能按 order/fallback 配置尝试其他 deployment；Responses 流中的 400 当前明确不触发 mid-stream fallback。协议完整性错误不能依赖 bridge 返回普通 400 来阻止路由，因为 Router 通用 fallback 会捕获普通异常

定义内部可识别的 `DeepSeekProtocolNonFallbackError`（`category=protocol_integrity`、`retry_allowed=false`、`fallback_allowed=false`、public status=400）。bridge 对 `reasoning_history_missing`、`reasoning_history_unrecoverable`、`reasoning_mode_conflict`、`tool_history_invalid`、`tool_result_orphaned` 和 `tool_history_incomplete` 统一抛出该 typed error；公共 endpoint 最外层再将它映射为结构化 400。`async_function_with_fallbacks()`、同步对应路径和其他 retry/fallback 判定点必须在通用异常捕获之前识别该 category，并直接返回 primary error，禁止调用同 model group 的 higher-order deployment 和跨 group fallback。上游 DeepSeek 等价 400 解析后也必须映射到同一 category；只有 rate limit、timeout、5xx 等明确可重试错误沿用 Router fallback policy

错误策略：

- `reasoning_history_missing` / `reasoning_history_unrecoverable` / `reasoning_mode_conflict` / `tool_history_invalid` / `tool_result_orphaned` / `tool_history_incomplete`：本地校验和等价上游 DeepSeek 400 统一映射为 `DeepSeekProtocolNonFallbackError`，再映射为公共 400；不做同 deployment retry，也不调用同 group 或跨 group fallback。校验顺序是 tool graph、历史 suffix 完整性、历史 reasoning、模式冲突，因此 disabled/none 不能掩盖已经损坏的历史
- rate limit、timeout、5xx 等首 token 前错误：沿用 Router fallback policy
- 已输出 reasoning/text 后的错误：只有 continuation input 能保留已输出 reasoning，且目标 protocol 可无损消费时才允许 fallback；否则返回原始 mid-stream error

每个 fallback attempt 都从不可变的原始 public request 重建 canonical input，再根据新 deployment 的 protocol 重新编码。不得复用已经为上一 provider 编译的 wire messages

兼容矩阵：

| 源历史 | fallback 目标 | 是否允许透明续接 |
| --- | --- | --- |
| 完整 DeepSeek 明文 reasoning | DeepSeek Anthropic/OpenAI-compatible | 可以，重新编码后验证 |
| DeepSeek 无签名 thinking | Claude thinking enabled | 不可以，缺少 Claude signature |
| Claude `redacted_thinking` 且属于工具历史 | DeepSeek 任意生成模式 | 不可以，没有可恢复明文 |
| 工具历史需要 reasoning | 目标生成模式 disabled/none | 不可以，返回 `reasoning_mode_conflict` |
| 无工具的可选 reasoning/redacted block | 目标生成模式 disabled/none | 可以丢弃可选 reasoning |
| 无工具普通对话 | 不同 protocol | 可以，按目标重新编码 visible history |

因此 fallback 到 Claude 时不能简单删除 unsigned thinking block；工具续接会丢失必要状态。fallback 失败时保留 primary error、fallback error、deployment id 和阶段，但不记录 reasoning 正文

## 6. 源码与测试落点

| 阶段 | 源码落点 | 主要测试 |
| --- | --- | --- |
| trusted protocol context | Router 私有 context factory/runtime resolver、model-info 出站 sanitizer | provenance、两入口 selector 隔离、model-info/metadata 泄漏测试 |
| canonical thinking/tool graph | generation/history 双状态、reasoning codec/validator | enabled/disabled 冲突、普通 assistant 后缀、call-id 图边界测试 |
| 路径 A request/wire | `messages/handler.py`, `deepseek/messages/transformation.py` | 现有 DeepSeek Messages mapped test |
| 路径 A response/stream | Anthropic Messages response/streaming logging path | 对应 Messages transformation/streaming mapped test |
| 路径 B raw dispatch/accounting | `responses/main.py`、新 DeepSeek Responses bridge、`custom_httpx/llm_http_handler.py` | 单次 prepare/transform/raw HTTP、禁止 Anthropic finalize、单次 accounting 测试 |
| 路径 B session/stream | `responses/litellm_completion_transformation`、专用 sync iterator | sync/async reconstruction、存储错误等价、stream lifecycle 测试 |
| 路径 C generic adapter | Anthropic `responses_adapters` transformation/streaming iterator | 现有 adapter mapped tests |
| Router policy | Router retry/fallback 与 Responses streaming fallback | `test_router_aresponses_streaming_fallback.py` 及 order fallback tests |
| usage/cost | `Usage` 调用点、现有 cost/logging 入口 | DeepSeek usage、Anthropic cost、SpendLog tests |

必须覆盖以下真实链路：

1. **Phase 0 route proof**：分别发 `/v1/messages` 和 `/v1/responses`，断言解析后的 provider、deployment id、protocol 和具体 handler；禁止只断言模型名
2. **原生 Messages 两轮**：第一轮代理返回 thinking + 并行 tool_use；模拟 Claude Code 原样回传 content 和 tool_result；第二轮 wire body 必须含完整无签名 thinking
3. **Messages 生成模式**：无工具历史时 disabled wire 不含 thinking、enabled/省略时开启；未知 type 返回参数 400。完整工具历史切换 disabled 返回 `reasoning_mode_conflict` 且 HTTP 调用为零；缺 reasoning/redacted 时即使 disabled 仍先返回 missing/unrecoverable
4. **Messages 历史错误**：tool-associated assistant 后缀缺明文、空格、只有 redacted data 均返回稳定 400；本轮开关不能绕过；无工具节点可按策略通过；输入对象不变
5. **普通 assistant 后缀**：`assistant(tool_use+reasoning) -> user(tool_result) -> assistant(text without reasoning) -> user` 返回 `reasoning_history_missing`；补齐 reasoning 后 wire 保留两个 assistant 节点
6. **tool id 图边界**：覆盖缺失/重复 call id、孤儿/重复/缺失 result、多个 user result、并行 calls、混合 text/tool_use 和连续 assistant tool 节点；断言错误码与节点分组
7. **公共 Responses raw wire**：对 `completion`、`acompletion`、public/full Messages handler、Anthropic response finalize/agentic hook 和 Router 二次入口设置 fail-fast spy；prepare、config transform、raw HTTP 各命中一次，唯一 wire body 含无签名 thinking
8. **Responses effort 映射**：参数化断言 `none -> disabled`、`low/high/max -> enabled + 同值 output_config.effort`、omitted -> enabled + high；`medium/xhigh`、空值和错误类型返回参数 400。已有完整工具历史加 none 返回 `reasoning_mode_conflict` 且 raw HTTP 为零
9. **父调用 accounting**：一次 Responses parent call 只有一个最终 hook、一个 aggregate Usage normalization、一个 aggregate cost 和一个 parent SpendLog；每个 attempt 仅有一个内部 request/pre-call/usage snapshot，不产生 child accounting。参数化覆盖无 fallback、首 token 前 fallback、部分输出后 fallback 和 fallback 失败，断言没有漏记、重复计费或 Anthropic finalize
10. **公共 Responses 显式历史**：reasoning item、多个 function call、function_call_output 经 bridge 后，DeepSeek wire 顺序与 call id 正确
11. **公共 Responses session**：sync/async 各做两轮，第一轮 output 中 reasoning、完整 `ToolAssociatedCanonicalSuffix`、call-id 图和完整性 digest 被原子持久化；第二轮 `previous_response_id` 重建后的 canonical history 与 wire body 相同
12. **session 失败等价**：sync/async 参数化覆盖无 DB、SpendLog 缺失、cold storage 缺失/不可读、未知 response id、已存 output 缺 reasoning，以及 flag 尚在但首个 tool use、任一 reasoning、tool_use 或 tool_result 被裁掉，断言相同 `reasoning_history_unrecoverable` status/error code；完整显式 input 不误依赖 session
13. **Responses streaming**：async 与 sync 使用同一个纯 SSE decoder，reasoning delta 同时进入对外 events 和 completed/logging payload；对 `BaseAnthropicMessagesStreamingIterator`、`PassThroughStreamingHandler` 和 `GLOBAL_PASS_THROUGH_SUCCESS_HANDLER_OBJ` 设置 fail-fast spy，三者均不得调用，也不得产生 `/v1/messages` pass-through 成功记录；sync iterator 取消/异常会关闭 worker、loop 和 response；failed/incomplete 不产生成功历史
14. **路径 C handler test**：`/v1/messages -> OpenAI Responses adapter` 的 thinking 不再成为 output_text；该测试不伪装成 DeepSeek route
15. **Router/provenance 集成**：本地 `DeepSeekProtocolNonFallbackError` 不重试/不 fallback；测试同 model group 的 higher-order deployment 和跨 group fallback 均未调用；可重试首 token 前错误重新选择并重新编译；新 attempt 的 protocol context 只来自新 deployment；direct SDK 伪造 `model_info` 或内部 kwarg 不能命中 DeepSeek config
16. **raw transport retry 隔离**：模拟 DeepSeek 400、`ConnectError` 和取消，断言使用 typed result/failure、无 `raise_for_status()`、无隐式重试、请求 bytes 不变、response/stream close 恰好一次，未调用 `transform_anthropic_messages_request_on_http_error()`、重签名、retry logging 或 `logging_obj` body mutation；错误由 Responses owner 统一处理且每次只有一个 failure lifecycle
17. **protocol 隔离**：`/model/info`、`/v1/model/info`、`/v2/model/info` 的普通/admin/debug 响应和 provider wire、metadata、proxy request、SpendLog 均不含 `reasoning_protocol` 或 protocol context；客户端同名字段不能启用逻辑
18. **usage/cost**：缓存 alias 相等且只计费一次，SpendLog token/cost 与最终 Usage 一致
19. **Claude 不回归**：没有可信 protocol context 的 Claude deployment 仍执行原签名保护，客户端 metadata/model_info 不能绕过

## 7. 分阶段实施计划

实施必须按以下顺序推进。每一步完成对应门禁后才能进入下一步；任何门禁失败都停留在当前阶段，不通过配置绕过

### Step 0：固定基线并确认真实链路

1. 从目标 release tag 创建修复分支，记录 LiteLLM commit、Python 依赖和部署配置版本
2. 用脱敏 diagnostics 分别请求 `/v1/messages` 和 `/v1/responses`，记录 provider、deployment id、protocol resolver 结果和具体 handler/bridge
3. 保存可复现的最小 fixture：首轮 thinking/tool call、第二轮 tool result、stream/non-stream、上游 400；禁止保存请求正文、reasoning、密钥和敏感 header
4. 对照 #32110、#27425、#26678 做逐文件依赖检查，只标记可移植的最小差异

**Step 0 门禁**：两个 endpoint 的真实调用图与本方案一致，fixture 能在未修改代码上稳定复现 reasoning 丢失或错误重试；否则先修正方案中的源码落点

### Step 1：建立共享协议基础

1. 增加带 provenance 的 Router 私有 `DeploymentProtocolContext` 和两个入口 resolver，先完成 model-info 出站 sanitizer
2. 定义 canonical assistant/history 类型、`generation_thinking_enabled` 与 `history_reasoning_required` 两个状态
3. 实现 tool-use/tool-result call-id 图校验、普通 assistant 后缀 reasoning 校验、完整 `ToolAssociatedCanonicalSuffix` 原子持久化和 `redacted_thinking` 不可恢复错误
4. 固化 Messages thinking 与 Responses effort 的参数解析、错误码和模式冲突规则
5. 先写纯函数和 session 完整性测试，覆盖 disabled/none、普通 assistant 后缀、并行 tool call、孤儿 result、suffix 裁剪和 direct SDK 伪造 context

**Step 1 门禁**：resolver 不能被客户端字段触发；所有历史结构错误、suffix 不完整、缺失 reasoning 和破坏性 disabled/none 切换都在发出 HTTP 请求前得到稳定结果；完整 suffix 与 digest 原子保存/读取，输入对象保持不变

### Step 2：先完成原生 `/v1/messages`（Phase 1）

1. 在 `DeepSeekAnthropicMessagesConfig` 接入共享 codec，编译无签名 thinking block，保留 visible text、tool call 顺序和 provider-specific reasoning 恢复
2. 接入非流式和流式响应的 reasoning 累计，确保客户端能原样回传下一轮历史
3. 增加两轮 wire-level 测试：第一轮返回 thinking + 并行 tool_use，第二轮回传 tool_result，断言完整 thinking 出现在上游 body
4. 增加 Claude signature、无工具多轮、redacted 和 thinking disabled 回归测试

建议提交：`fix(deepseek): validate and replay anthropic reasoning history`、`test(deepseek): cover two-turn anthropic tool reasoning`

**Step 2 门禁**：原生 Messages 两轮通过，DeepSeek 工具续接不再 400；Claude deployment 行为不变；该阶段可独立灰度，不得提前启用 Responses bridge

### Step 3：抽取 Path B 的冻结 wire/raw transport

1. 从现有 Anthropic handler 提取 `_prepare_anthropic_messages_wire_request()`，使每个 attempt 的 config transform、序列化、签名和 URL 只执行一次并返回冻结对象
2. 原生 Messages 继续使用 `ClaudeAnthropicMessagesTransport` 的可变签名恢复；签名类 400 后从 canonical history 深拷贝出 attempt candidate，清理 candidate 上的 Claude thinking，再重新 prepare，不能复用已发送的 frozen request 或修改原始 history
3. 为 Path B 增加独立 `DeepSeekResponsesRawTransport`，只负责发送一次冻结请求和返回 raw response/stream，不进入 Anthropic response finalize 或 accounting hook
4. DeepSeek Path B 使用不自动 `raise_for_status()`、不隐式连接重试的低层发送路径，返回 typed raw result/failure；明确禁止 `transform_anthropic_messages_request_on_http_error()`、删 thinking、重签名和 retry logging
5. 增加互斥测试：Claude signature 类 400 第二次重新 prepare 后成功；DeepSeek 对等 400、`ConnectError` 和取消均只发送一次冻结 body，且无 body mutation、重复 pre-call 或 SpendLog

**Step 3 门禁**：DeepSeek config transform 和首个 HTTP request 各一次，DeepSeek raw transport 不改变请求且失败由调用方接管；Claude signature recovery 第二次请求来自新的 prepare；原生 Messages 既有 handler 测试全部通过

### Step 4：实现公共 `/v1/responses` 非流式桥接

1. 在 `responses/main.py` 的 native config/Chat bridge 选择前接入可信 protocol strategy
2. 通过共享 session reconstruction core 和 canonical validator 生成完整 Anthropic Messages wire；禁止 `completion()`、`acompletion()`、`anthropic_messages()` 和 Router public method 递归进入
3. 完成 `reasoning.effort` 到 Anthropic `thinking/output_config.effort` 映射，以及 Responses reasoning/function call/function output 的顺序编译
4. 统一 sync/async session reconstruction、`previous_response_id` 错误、显式 input 优先级和单次 Responses accounting
5. 增加非流式两轮测试，断言第一轮 reasoning 可持久化，第二轮 wire body 与显式历史重建结果一致

建议提交：`fix(responses): route deepseek anthropic protocol explicitly`、`fix(responses): preserve reasoning through tool sessions`

**Step 4 门禁**：sync/async 非流式两轮均通过；无 fallback 时只有一个 wire request、一个 parent Usage、一个 parent cost 和一个 parent SpendLog；Responses 不经过 Anthropic finalize

### Step 5：实现公共 `/v1/responses` 流式桥接

1. 新增 `DeepSeekAnthropicResponsesSSEDecoder`，只处理 SSE framing、reasoning/text/tool delta 和 Responses event 编码
2. 禁止创建 `BaseAnthropicMessagesStreamingIterator`、`PassThroughStreamingHandler` 或 global pass-through success handler；不得标记 `/v1/messages` route
3. 为 sync streaming 使用持有 event loop 的 worker iterator，覆盖取消、客户端断开、上游 failed/incomplete 和资源关闭
4. 确保 reasoning delta 同时进入对外 event、completed response、session history 和唯一 accounting payload
5. 增加异步/同步流式测试和三个 pass-through 组件的 fail-fast spy

建议提交：`fix(responses): decode deepseek anthropic streams without pass-through logging`、`test(responses): cover deepseek anthropic stream lifecycle`

**Step 5 门禁**：stream completed、首 token 前失败、流中失败和取消路径均只有一个 parent 终态生命周期；fallback attempt 只产生内部 usage snapshot，无重复 success handler、child SpendLog 或 child cost

### Step 6：接入 Router fallback、Usage 和可观测性

1. 在 Router 通用异常捕获前接入 `DeepSeekProtocolNonFallbackError` 分类，覆盖 async/sync、同 group higher-order 和跨 group fallback 判定点
2. fallback 每次从原始 public request 和新 deployment context 重新编译，禁止复用旧 wire body
3. 固化 parent SpendLog 聚合模型：按实际 provider attempt 汇总 Usage/cost 一次，失败连接不虚构 token，partial 与 fallback 不重复合并
4. 统一 `prompt_cache_hit_tokens`、`cached_tokens` 和 `cache_read_input_tokens` 的 Usage 来源，验证只计费一次
5. 验证 model info、metadata、proxy request、SpendLog 和 provider wire 不泄漏 `reasoning_protocol`
6. 增加 fallback、usage/cost、dict/Pydantic terminal response 和 Claude 不回归测试

**Step 6 门禁**：错误分类、fallback 重编译、Usage/cost、SpendLog 和内部字段隔离全部通过；不出现 `dict object has no attribute usage`

### Step 7：验证、灰度与回滚准备

1. 每个逻辑提交分别运行 mapped tests、类型检查、`git diff --check` 和 `make pre-commit`
2. 用本地 proxy curl 做最终证明，只展示 HTTP 状态、event type、usage 摘要和错误阶段，不展示敏感内容
3. 先只对 `/v1/messages` deployment 灰度，再单独对 `/v1/responses` 灰度；两个 endpoint 不同时启用
4. 观察 missing/unrecoverable reasoning、mode conflict、上游 400、fallback skip、stream failed/completed、cache read 和 SpendLog cost
5. 保留按 commit 和 endpoint 独立回滚能力；移除 `reasoning_protocol` 只能关闭新逻辑，不能替代故障恢复验证

只有 Step 0 至 Step 7 全部门禁通过，才允许构建线上 LiteLLM 镜像

## 8. 发布、监控与回滚

按 endpoint 分开灰度：先启用路径 A，再启用路径 B。deployment 配置中的 `model_info.reasoning_protocol` 只由 Router 私有 factory 解析，运行时仅带 provenance 的 protocol context 能开启逻辑。监控 missing/unrecoverable reasoning、reasoning mode conflict 400、fallback/skip 原因、`response.failed`/completed、cache read tokens、SpendLog cost 和 `dict object has no attribute usage`

删除 deployment 的 `reasoning_protocol` 可关闭新逻辑，但旧路径仍可能复现已知 400，因此这只是行为回滚，不代表故障恢复。源码提交保持阶段独立，必要时按 endpoint 回滚；不得改动 Claude deployment 或外部密钥配置

## 9. 完成判定

只有以下条件全部满足才允许构建镜像：两个入口只接受 Router-provenanced protocol context 且公开响应/日志不泄漏内部字段；generation thinking 与 history reasoning obligation 分离，工具历史不能用 disabled/none 绕过；完整 `ToolAssociatedCanonicalSuffix` 与 digest 原子持久化，任一 suffix 节点被裁剪都在预检阶段拒绝；Responses effort 的 none/low/high/max/omitted 映射通过；公共 Responses 只执行一次 config transform 和 DeepSeek raw HTTP，不经过 completion/public/full Messages handler、Anthropic finalize 或二次 Router；DeepSeek raw transport 使用不自动 raise/status retry 的 typed result，400、ConnectError、取消的 close 和 failure lifecycle 正确；Responses streaming 使用纯 SSE decoder 且不触发 Anthropic pass-through iterator、success handler 或 `/v1/messages` accounting；Claude 可在签名恢复时从 canonical 深拷贝重新 prepare，原始 history 不变；`DeepSeekProtocolNonFallbackError` 在 sync/async Router 的同 group 和跨 group fallback 判定点均被拦截；原生 Messages 两轮和 sync/async Responses session 两轮均保留完整 reasoning；call-id 图错误明确失败；每个 Responses parent call 只有一次最终 hook、aggregate Usage、cost 和 parent SpendLog，各 attempt 仅有内部 snapshot；fallback 只在可无损重编译时执行并替换 deployment protocol context；缓存只计费一次；dict/Pydantic、sync/async streaming 和 Claude signature 回归测试通过；仓库不包含线上敏感配置
