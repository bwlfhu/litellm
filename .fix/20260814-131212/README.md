# DeepSeek Anthropic thinking 兼容修复方案

## 1. 任务摘要

受影响的是线上将 DeepSeek V4 reasoning deployment 声明为 Anthropic 的渠道。当前请求在 LiteLLM 内部被当成普通 Claude Anthropic deployment 处理；响应中的 DeepSeek `reasoning_content` 被转换成没有 Claude 加密签名的 `thinking` block，随后通用 Anthropic 逻辑为满足 Claude 的协议约束而丢弃该 block。下一次带工具续接时，DeepSeek 收不到上一轮完整 reasoning，返回 400，Router 再尝试低优先级 deployment

目标是增加受 deployment 配置严格控制的 `deepseek_anthropic` reasoning protocol。它继续使用 Anthropic HTTP Messages transport 和 `/v1/messages`，但使用 DeepSeek 的无签名 thinking 规则。Claude 的签名校验和清理逻辑保持不变，普通无工具多轮不强制携带 reasoning

本文件是实施设计，不包含线上 URL、密钥、请求正文或客户配置

## 2. 证据与官方方案核对

### DeepSeek 协议约束

- [Anthropic API 兼容](https://api-docs.deepseek.com/zh-cn/guides/anthropic_api) 支持 `content` 中的 `type: "thinking"`，并支持 `thinking` 与 `output_config.effort`
- [Thinking 模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode) 要求带工具调用时，后续请求完整回传之前 assistant 的 `reasoning_content`，缺失会 400；普通无工具多轮可以不回传
- [Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api) 是无状态接口，不支持依赖 `previous_response_id` 自动取得历史；下一轮必须由调用方提供 reasoning input item
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

1. **两个入口，一个可信 resolver**：Messages 与 Responses 各自做路由决策，但共同调用只接受 Router-authored `model_info` 的 typed resolver，避免两套字段解析漂移
2. **客户端字段不能启用**：忽略客户端 `metadata`、header、模型名和预先传入的内部 kwarg；Router 每次选定 deployment 后重新生成 protocol context
3. **canonical 只做转换契约**：canonical assistant turn 包含 visible content、可选明文 reasoning 和有序 tool calls，不承担跨请求存储职责
4. **历史所有者按路径确定**：原生 Messages 由客户端回传 content blocks；Responses 由显式 input 或可验证的本地 session history 提供；SpendLog 不是所有路径的隐式真相源
5. **DeepSeek 无签名**：发往 DeepSeek Anthropic 的 thinking block 只包含 `type` 和 `thinking`，不伪造 Claude signature，也不把 reasoning 降级成 text
6. **`redacted_thinking` 不可转换**：DeepSeek 官方明确不支持该 block，且 `data` 不可逆。无工具历史可按明确策略丢弃；工具相关历史必须返回不可恢复错误
7. **工具会话完整性**：按 `tool_use.id <-> tool_result.tool_use_id` 建立调用图，不用 user message 位置推断 turn。每个含 tool call 的 assistant 节点都必须有非空明文 reasoning
8. **顺序与配对**：thinking、visible text、tool call 以及后续 tool result 保持原始顺序；并行 tool calls 属于同一个 assistant 节点，每个 call id 在请求历史中唯一且恰好匹配一个 result
9. **无状态边界**：不向 DeepSeek 上游发送 `previous_response_id`。只有代理已重建完整 history 时才展开为 wire input，否则返回明确 400
10. **单一计费事实源**：provider usage 只归一化一次，所有日志、cost 和 metrics 读取同一个 `Usage`，不得分别累加别名字段

## 5. 实施方案

### 5.1 Trusted protocol resolver 与传播

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

共享 resolver 只接受 `model_info: object`，返回 `None | DEEPSEEK_ANTHROPIC`。Router 已在 [`litellm/router.py:3152`](/home/allcam/projects/litellm/litellm/router.py:3152) 把所选 deployment 的 model info 写入 `kwargs["model_info"]`，两个入口分别消费：

- **Messages consumer**：在按 provider 选 config 之前解析 protocol；命中时选择 `DeepSeekAnthropicMessagesConfig`
- **Responses consumer**：在 `responses/main.py` 选择 native config/Chat bridge 之前解析 protocol；命中时选择 DeepSeek Anthropic Responses bridge strategy，不能依赖 Messages handler 再次选择

Responses consumer 把解析后的不可变 protocol context 显式传入 request transformation、session handler、response transformation 和 streaming iterator。Router 内部持有选中 deployment 的权威快照；fallback 每次从新 deployment 的原始 `model_info` 重新调用 resolver，并以新快照替换旧 context，禁止合并或继承上一 deployment 的 protocol

`reasoning_protocol` 是内部路由控制字段，不是 secret，但不能成为客户端可回传并影响路由的状态。实现一个返回副本的共享 model-info 出站 sanitizer，并同时用于 `/model/info`、`/v1/model/info`、`/v2/model/info`，包括 admin/debug 响应；deployment 配置 CRUD 可在受权配置对象中保留该字段，避免编辑时丢失。provider wire、普通 `metadata`、Responses response metadata、`proxy_server_request`、SpendLog 原始 metadata 和用户可见错误不得包含该字段。安全 route trace 可把 resolver 结果作为独立枚举标签记录，不能把整个 `model_info` 写入日志

建议保留 `DeepSeekAnthropicMessagesConfig` 并提取一个可组合的 DeepSeek reasoning codec。`deepseek_anthropic` 不注册为可由客户端直接传入的公开 provider

### 5.2 路径 A：原生 Messages request

在 `DeepSeekAnthropicMessagesConfig.transform_anthropic_messages_request()` 中执行无副作用的 decode、validate、encode：

1. 从 Anthropic thinking block、顶层 `reasoning_content` 或 `provider_specific_fields.reasoning_content` 解码明文 reasoning；已有真实值优先，不重复写入
2. 每个 assistant message 是独立节点；同一 message 内的 text、thinking 和多个 `tool_use` 不拆分。建立全局 `tool_use.id -> assistant 节点` 索引，再按 `tool_result.tool_use_id` 建边，不使用 user message 作为外层边界
3. `tool_use.id` 或 `tool_result.tool_use_id` 缺失、空白、重复时返回 `tool_history_invalid`；result 找不到 use 时返回 `tool_result_orphaned`；一个 use 对应多个 result 或请求结束时仍无 result 时返回 `tool_history_incomplete`
4. 并行 calls 保持在原 assistant 节点并允许后续一个或多个 user message 中的 results 按 id 回配。混合 text/tool_use 仍是一个节点；连续 assistant tool 节点不合并，前一节点在出现后一节点前仍未完成时按 incomplete 拒绝
5. 对图中每个含 tool use 的 assistant 节点验证非空明文 reasoning；普通 assistant 节点和仅仅声明了 `tools`、但尚无 tool exchange 的首轮请求不强制。校验结果附着到 assistant 节点和对应边，不扩大到任意 user message 边界
6. 明文完整时重建 thinking、text、tool_use block，移除 DeepSeek 不需要的 signature；输入对象不得原地修改
7. 缺失明文或只有空格时返回稳定的 `reasoning_history_missing` 400，不注入占位符
8. 继续执行现有 custom tool type 清理，不改变 hosted tool type、`thinking` 和 `output_config.effort`

`provider_specific_fields` 只作为兼容旧 ModelResponse 的恢复来源。恢复后不把该内部副本发送到 provider metadata

### 5.3 路径 A：原生 Messages response 与历史

原生 Messages 不建立隐式服务端会话：

- 非流式响应保持上游 Anthropic content blocks 原样返回；DeepSeek thinking 不要求 signature
- 流式响应保持 thinking delta、text delta、tool delta 和终态 SSE 顺序，由客户端累计并在下一请求原样回传
- logging parser 可以为 usage/observability 派生 canonical reasoning，但该派生值不是下一轮正确性的唯一来源
- 不要求同时写入 `proxy_server_request`、SpendLog 和 Responses session；只有实际消费该数据的 bridge 才持久化标准 Responses output
- Pydantic 和普通 dict 终态都使用安全 getter 读取 usage，避免 `dict object has no attribute usage`

DeepSeek 不会产生受支持的 `redacted_thinking`。如果入站历史来自 Claude fallback：无工具 turn 可删除整个 redacted block；tool-associated turn 返回 `reasoning_history_unrecoverable`，禁止把 `data` 当明文或注入占位符

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
-> DeepSeekAnthropicMessagesConfig.transform_anthropic_messages_request()
-> BaseLLMHTTPHandler.anthropic_messages_handler(_is_async=True)
-> configured DeepSeek /v1/messages
-> DeepSeekAnthropicResponsesBridge response/stream transformation
```

`prepare_request()` 输出新的 messages、Messages optional params、所选 `DeepSeekAnthropicMessagesConfig`、`GenericLiteLLMParams` 和当前 call 的 logging object。bridge 把这些值直接传给 [`BaseLLMHTTPHandler.anthropic_messages_handler()`](/home/allcam/projects/litellm/litellm/llms/custom_httpx/llm_http_handler.py:2407)，由现有环境校验、header、URL、签名、HTTP 错误映射和 logging 生命周期完成 wire 调用；不重新执行 Router。wire-level fake transport 必须断言唯一出站 URL 是已选 deployment 的 `/v1/messages`，body 含无签名 thinking，且 `reasoning_protocol` 不在 body/header/metadata 中

#### 5.4.2 canonical 转换

1. Responses `reasoning` input item 解码为 pending canonical reasoning，并附着到紧随其后的 assistant message/function call；不得变成 `output_text`
2. 连续 function calls 合并为同一个 assistant 节点，保留 reasoning、call id 和顺序；`function_call_output` 用与路径 A 相同的 id 图 validator 配对
3. canonical history 最终只由 `DeepSeekAnthropicMessagesConfig` 编译成 DeepSeek Anthropic `/v1/messages` body，不再经过 Chat transformation
4. 非流式上游 thinking 转成 `ModelResponse.choices[0].message.reasoning_content`，再生成 Responses `reasoning` output item；function call 和 visible message 分别生成标准 output item
5. streaming iterator 累计 reasoning delta 并生成标准 reasoning events；完成时把同一累计结果交给 completed response/logging payload，不能只保存在 SSE wrapper 实例中
6. `response.failed`、iterator exception 或不完整终态不得写入伪造的成功 session history；Pydantic/dict terminal response 使用同一安全读取逻辑

#### 5.4.3 sync/async session 与 streaming

把现有 `async_responses_api_session_handler()` 拆成一个共享的 async reconstruction core 和一个纯函数 canonical validator。async bridge 直接 await core；sync bridge 使用仓库现有 [`run_async_function()`](/home/allcam/projects/litellm/litellm/litellm_core_utils/asyncify.py:70) 执行同一个 core，禁止维护第二套 session 拼接逻辑或使用裸 `asyncio.run()`

为 `previous_response_id` 持久化/读取的完整 Responses output 必须包含 reasoning item。显式 input 自身已包含完整 canonical history 时不依赖 session；一旦请求需要 response id 补全历史，以下情况在 sync/async 都返回相同的 `reasoning_history_unrecoverable` 400：未配置 session DB、SpendLog 不存在、cold-storage object 缺失/不可读、response id 不存在、已存 output 缺 reasoning。现有“session 为空时只保留本轮新 input”的宽松分支不能用于 `deepseek_anthropic`

当前低层 [`anthropic_messages_handler()`](/home/allcam/projects/litellm/litellm/llms/custom_httpx/llm_http_handler.py:2407) 只接受 `_is_async=True`，同步分支会抛错。非流式 sync bridge 用 `run_async_function()` 覆盖从低层 HTTP call 到完整 response transformation 的整个 coroutine。sync streaming 不能在临时 loop 中取得 async iterator 后关闭 loop；新增专用 sync Responses iterator，由一个 worker thread 持有同一 event loop 直至上游 iterator 完成/失败/取消，通过有界 queue 逐 event 转发，并在 close、客户端断开和异常时取消 task、关闭 response 和 join thread。HTTP proxy 的 async endpoint 继续直接消费 async iterator，不经过该 sync wrapper

sync 与 async 都先调用同一个 `prepare_request()`，再调用同一个低层 Messages transport 和 response codec。测试必须分别覆盖非流式与流式两轮：第一轮 reasoning 被保存，第二轮由 `previous_response_id` 重建到同一 assistant 节点，最终 wire body 完全一致；并对无 DB、cold storage 缺失和未知 response id 做参数化等价断言

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

先区分 retry 和 fallback：400 默认不做同 deployment retry，但非流式外层 Router 仍可能按 order/fallback 配置尝试其他 deployment；Responses 流中的 400 当前明确不触发 mid-stream fallback

错误策略：

- `reasoning_history_missing` / `reasoning_history_unrecoverable` / `tool_history_invalid` / `tool_result_orphaned` / `tool_history_incomplete`：本地校验和等价上游 DeepSeek 400 统一映射为公共 400；不做同 deployment retry，默认也不做透明跨协议 fallback
- rate limit、timeout、5xx 等首 token 前错误：沿用 Router fallback policy
- 已输出 reasoning/text 后的错误：只有 continuation input 能保留已输出 reasoning，且目标 protocol 可无损消费时才允许 fallback；否则返回原始 mid-stream error

每个 fallback attempt 都从不可变的原始 public request 重建 canonical input，再根据新 deployment 的 protocol 重新编码。不得复用已经为上一 provider 编译的 wire messages

兼容矩阵：

| 源历史 | fallback 目标 | 是否允许透明续接 |
| --- | --- | --- |
| 完整 DeepSeek 明文 reasoning | DeepSeek Anthropic/OpenAI-compatible | 可以，重新编码后验证 |
| DeepSeek 无签名 thinking | Claude thinking | 不可以，缺少 Claude signature |
| Claude `redacted_thinking` | DeepSeek | 不可以，没有可恢复明文 |
| 无工具普通对话 | 不同 protocol | 可以，按目标重新编码 visible history |

因此 fallback 到 Claude 时不能简单删除 unsigned thinking block；工具续接会丢失必要状态。fallback 失败时保留 primary error、fallback error、deployment id 和阶段，但不记录 reasoning 正文

## 6. 源码与测试落点

| 阶段 | 源码落点 | 主要测试 |
| --- | --- | --- |
| shared trusted resolver | Router model info type、共享 protocol resolver、model-info 出站 sanitizer | 两入口 selector 隔离、model-info/metadata 泄漏测试 |
| canonical tool graph | 共享 reasoning codec/validator | call id 缺失、重复、孤儿、未完成、并行和连续 assistant 节点测试 |
| 路径 A request/wire | `messages/handler.py`, `deepseek/messages/transformation.py` | 现有 DeepSeek Messages mapped test |
| 路径 A response/stream | Anthropic Messages response/streaming logging path | 对应 Messages transformation/streaming mapped test |
| 路径 B direct dispatch | `responses/main.py`、新 DeepSeek Responses bridge、`custom_httpx/llm_http_handler.py` | 禁止 completion/public Messages、单次低层 dispatch、wire body 测试 |
| 路径 B session/stream | `responses/litellm_completion_transformation`、专用 sync iterator | sync/async reconstruction、存储错误等价、stream lifecycle 测试 |
| 路径 C generic adapter | Anthropic `responses_adapters` transformation/streaming iterator | 现有 adapter mapped tests |
| Router policy | Router retry/fallback 与 Responses streaming fallback | `test_router_aresponses_streaming_fallback.py` 及 order fallback tests |
| usage/cost | `Usage` 调用点、现有 cost/logging 入口 | DeepSeek usage、Anthropic cost、SpendLog tests |

必须覆盖以下真实链路：

1. **Phase 0 route proof**：分别发 `/v1/messages` 和 `/v1/responses`，断言解析后的 provider、deployment id、protocol 和具体 handler；禁止只断言模型名
2. **原生 Messages 两轮**：第一轮代理返回 thinking + 并行 tool_use；模拟 Claude Code 原样回传 content 和 tool_result；第二轮 wire body 必须含完整无签名 thinking
3. **Messages 历史错误**：tool-associated assistant 节点缺明文、空格、只有 redacted data 均返回稳定 400；无工具节点可按策略通过；输入对象不变
4. **tool id 图边界**：覆盖缺失/重复 call id、孤儿/重复/缺失 result、多个 user result、并行 calls、混合 text/tool_use 和连续 assistant tool 节点；断言错误码与节点分组
5. **公共 Responses direct wire**：对 `completion`、`acompletion`、public Messages 和 Router 二次入口设置 fail-fast spy；请求只能命中一次低层 Messages handler，唯一 wire body 含无签名 thinking
6. **公共 Responses 显式历史**：reasoning item、多个 function call、function_call_output 经 bridge 后，DeepSeek wire 顺序与 call id 正确
7. **公共 Responses session**：sync/async 各做两轮，第一轮 output 中 reasoning 被持久化；第二轮 `previous_response_id` 重建后的 canonical history 与 wire body 相同
8. **session 失败等价**：sync/async 参数化覆盖无 DB、SpendLog 缺失、cold storage 缺失/不可读、未知 response id 和已存 output 缺 reasoning，断言相同 status/error code；完整显式 input 不误依赖 session
9. **Responses streaming**：async 与 sync reasoning delta 同时进入对外 events 和 completed/logging payload；sync iterator 取消/异常会关闭 worker、loop 和 response；failed/incomplete 不产生成功历史
10. **路径 C handler test**：`/v1/messages -> OpenAI Responses adapter` 的 thinking 不再成为 output_text；该测试不伪装成 DeepSeek route
11. **Router 集成**：本地 reasoning/tool-history 400 不重试/不透明 fallback；可重试首 token 前错误重新选择并重新编译；不兼容 Claude fallback 被跳过；fallback 失败保留原始错误；新 attempt 的 protocol 只来自新 deployment
12. **protocol 隔离**：`/model/info`、`/v1/model/info`、`/v2/model/info` 的普通/admin/debug 响应和 provider wire、metadata、proxy request、SpendLog 均不含 `reasoning_protocol`；客户端同名字段不能启用逻辑
13. **usage/cost**：缓存 alias 相等且只计费一次，SpendLog token/cost 与最终 Usage 一致
14. **Claude 不回归**：没有 protocol 的 Claude deployment 仍执行原签名保护，客户端 metadata 不能绕过

## 7. 分阶段提交与验证

### Phase 0：确认线上真实拓扑

增加临时或既有安全 diagnostics，确认两个 endpoint 的 provider、config/bridge、deployment id 和 protocol。日志不得包含请求正文、reasoning、key 或敏感 header。若故障链与当前源码拓扑不符，先确认运行版本和 deployment 配置再修改源码

### Phase 1：原生 `/v1/messages`

- `fix(deepseek): validate and replay anthropic reasoning history`
- `test(deepseek): cover two-turn anthropic tool reasoning`

只实现路径 A、redacted policy、会话 validator 和 wire test。通过后可以单独灰度给只使用 `/v1/messages` 的 deployment

### Phase 2：公共 `/v1/responses`

- `fix(responses): route deepseek anthropic protocol explicitly`
- `fix(responses): preserve reasoning through tool sessions`
- `test(responses): cover deepseek reasoning session and fallback`

实现独立入口 selector、低层 Messages direct dispatch、共享 sync/async session reconstruction 和专用 sync streaming 生命周期。Phase 1 通过不代表 Phase 2 可以上线

### Phase 3：通用 Messages-to-Responses adapter

仅在独立回归需求或 route proof 证明其属于线上故障链时实施，避免把 OpenAI adapter 重构混入 DeepSeek 首个补丁

每个提交前运行 mapped tests、类型检查和 `make pre-commit`，再用 `git range-diff <release-tag>...HEAD` 检查 #32110/#27425/#26678 的最小移植边界。真实证明使用本地 proxy curl，只展示状态、event type、usage 摘要和错误阶段

## 8. 发布、监控与回滚

按 endpoint 分开灰度：先启用路径 A，再启用路径 B。每个 deployment 只由 `model_info.reasoning_protocol` 开启，并监控 missing/unrecoverable reasoning 400、fallback/skip 原因、`response.failed`/completed、cache read tokens、SpendLog cost 和 `dict object has no attribute usage`

删除 deployment 的 `reasoning_protocol` 可关闭新逻辑，但旧路径仍可能复现已知 400，因此这只是行为回滚，不代表故障恢复。源码提交保持阶段独立，必要时按 endpoint 回滚；不得改动 Claude deployment 或外部密钥配置

## 9. 完成判定

只有以下条件全部满足才允许构建镜像：两个入口都从受信 model info 解析 protocol 且公开响应/日志不泄漏该字段；公共 Responses 不经过 completion/public Messages/二次 Router；原生 Messages 两轮和 sync/async Responses session 两轮均保留完整 reasoning；call-id 图的缺失、重复、孤儿和未完成状态明确失败；fallback 只在可无损重编译时执行并替换 deployment protocol context；缓存只计费一次；dict/Pydantic、sync/async streaming、SpendLog 和 Claude signature 回归测试通过；仓库不包含线上敏感配置
