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

## 3. 当前代码差异

当前分支基于 `v1.94.0` 之后的本地提交 `85b778c2a1`，并已经包含官方 #28200 的基础实现

- [`litellm/llms/deepseek/messages/transformation.py:15`](/home/allcam/projects/litellm/litellm/llms/deepseek/messages/transformation.py:15) 已有 `DeepSeekAnthropicMessagesConfig`，负责 `/anthropic/v1/messages` URL、DeepSeek header 和 custom tool 清理，但直接继承通用 Anthropic request transformation，没有把 canonical `reasoning_content` 编译为无签名 thinking block
- [`litellm/utils.py:8380`](/home/allcam/projects/litellm/litellm/utils.py:8380) 只在 `custom_llm_provider=deepseek` 时选择该 config；deployment 仍声明为 `anthropic` 时会选择普通 Anthropic config
- [`litellm/llms/anthropic/experimental_pass_through/messages/handler.py:537`](/home/allcam/projects/litellm/litellm/llms/anthropic/experimental_pass_through/messages/handler.py:537) 先按 provider 选 config，之后才决定是否走 Responses，当前没有基于可信 `model_info` 的 reasoning protocol override
- [`litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py:57`](/home/allcam/projects/litellm/litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py:57) 将 assistant thinking 当作 Responses `output_text`，这会损坏 reasoning 语义；响应转换约 [`:398`](/home/allcam/projects/litellm/litellm/llms/anthropic/experimental_pass_through/responses_adapters/transformation.py:398) 只生成 Anthropic block，没有回填 canonical history
- [`litellm/llms/deepseek/chat/transformation.py:62`](/home/allcam/projects/litellm/litellm/llms/deepseek/chat/transformation.py:62) 已有 OpenAI Chat 路径的 reasoning 恢复和空格占位。该行为不能直接复制到 Anthropic protocol；工具历史缺 reasoning 时本任务要求明确失败
- 通用 Anthropic Chat 在 [`litellm/llms/anthropic/chat/transformation.py:1797`](/home/allcam/projects/litellm/litellm/llms/anthropic/chat/transformation.py:1797) 会根据 Claude thinking block 规则丢弃或保留 thinking。该逻辑必须只对普通 Claude 生效

## 4. 设计原则与不变量

1. **启用边界**：只读取 Router 注入的 deployment `model_info.reasoning_protocol == "deepseek_anthropic"`。不读取客户端 `metadata`、模型名猜测、请求 header 或任意用户可控字段。未知值按现有逻辑处理并记录配置错误
2. **transport 与 protocol 分离**：复用 Anthropic HTTP/header/tool transport；DeepSeek reasoning 编译放在 `DeepSeekAnthropicMessagesConfig` 或其单一共享 helper 内，不修改全局 Claude transformation
3. **canonical history**：内部统一表达 assistant 为可见 text、可选 `reasoning_content`、有序 tool calls。Anthropic thinking block、OpenAI 顶层 `reasoning_content`、Responses reasoning item 都先解码到该表达，再按目标线路编码
4. **无签名规则**：DeepSeek Anthropic 请求的 thinking block 只发 `type` 和 `thinking`；不得伪造 Claude `signature`，不得把 DeepSeek 文本放到普通 `text` 以绕过校验
5. **历史完整性**：assistant 有 tool call 时，`reasoning_content` 必须是原始非空内容。能从 `provider_specific_fields.reasoning_content` 恢复就恢复；仍缺失则在代理侧返回结构化 400 和稳定错误码，不注入空格、不静默继续。无工具普通多轮不强制
6. **顺序与配对**：一个 assistant turn 的 wire 顺序为 thinking、visible text、tool_use；下一 user turn 的 tool_result 按 `tool_use_id` 映射到 `function_call_output`。多个并行 tool call 保持原顺序和一一配对
7. **状态边界**：DeepSeek Responses 不发送 `previous_response_id` 作为上游状态机制。若 LiteLLM 本地 session 能解析完整历史，可在代理侧展开后发送；没有历史且客户端未带 reasoning input item 时，明确返回无法恢复的 400。稳定 session id 本身不能凭空恢复推理
8. **可观察性**：保留原始 provider 错误、fallback 错误和 request id；不把内部 `reasoning_protocol` 放进 provider 可见 `metadata`

## 5. 实施方案

### 5.1 Deployment 选择与类型

在 model info 类型中增加可选的、内部使用的 `reasoning_protocol` 字段，并在 Anthropic Messages handler 增加一个纯函数选择器：

```yaml
model_list:
  - model_name: <public-alias>
    litellm_params:
      model: anthropic/<deepseek-model>
      api_base: <provider-base>
    model_info:
      reasoning_protocol: deepseek_anthropic
```

实际配置应由受信任的 Router deployment 管理；示例中的 alias、URL 和模型名均为占位符。选择优先级为：精确 protocol override，其次显式 `deepseek` provider，再次现有 Anthropic/OpenAI-like 路径。protocol override 必须强制走原生 Messages config，避免落入 `LiteLLMAnthropicToResponsesAPIAdapter`

建议保留现有 `DeepSeekAnthropicMessagesConfig` 类名，扩展其 reasoning 能力；`deepseek_anthropic` 是内部 protocol 值，不新增可由客户端任意指定的公开 provider。这样兼容已有 `deepseek/...` 调用，也满足受信 deployment 的隔离要求

### 5.2 原生 Anthropic Messages request

在 [`DeepSeekAnthropicMessagesConfig.transform_anthropic_messages_request()`](/home/allcam/projects/litellm/litellm/llms/deepseek/messages/transformation.py:113) 前后增加单一编译步骤：

1. 从 assistant content blocks、`reasoning_content`、`provider_specific_fields` 提取 canonical reasoning；输入对象不得原地修改
2. 已有 `thinking` block 时保留文本，删除/忽略其 Claude `signature` 字段；不要重复添加相同 reasoning
3. 只有 canonical reasoning 存在时，按 thinking、text、tool_use 重建 assistant content；普通 text/string 仍保持 Anthropic 合法形状
4. assistant 具有 tool_use/tool_calls 且 thinking 开启时，缺 reasoning 立即返回现有公共 BadRequest 约定；错误中包含 assistant turn 索引、缺少字段和“需要回传原始 reasoning”的修复提示，但不包含推理正文
5. thinking 未启用或没有 tool call 时，不因为缺 reasoning 添加占位符；保留客户端原始 thinking 参数和 `output_config.effort`
6. 继续执行现有 custom tool `type` 清理，且不得改动 hosted tool type

如果响应对象只把 reasoning 放在 `provider_specific_fields`，恢复后应移除该内部副本，避免同一 reasoning 被重复发送或泄漏到 provider metadata

### 5.3 原生 Anthropic Messages response 与 streaming

- 非流式响应：识别 `thinking`/`redacted_thinking`，把可见 thinking 文本聚合到 canonical `reasoning_content`，同时保留 Anthropic content block 给 Messages 调用方；DeepSeek block 的 signature 为 absent/None，不进行 Claude signature 验证
- 流式响应：在 `thinking_delta` 到达时同时累计 canonical reasoning 和对外 Anthropic SSE block；收到 tool_use 后仍保留同一 assistant turn 的 reasoning。`message_delta`/终态 usage 到达前不能丢弃累计值
- 终态解析须接受 Pydantic response 和普通 `dict`，usage 读取使用安全 getter；不能出现 `dict object has no attribute usage`
- `response.failed` 在首 token 前应生成可回退的 provider error；中途失败要保留已发事件、原始错误和 request id，不能伪造成功终态

### 5.4 Responses bridge 与 `/v1/responses`

参考 #27425 的最小逻辑，抽出/复用 Responses history helper，不把 thinking 当 text：

- Anthropic -> Responses：`thinking` 变成 `type: "reasoning"` input item 或 pending reasoning，随后 assistant text 变成 `message`，tool_use 变成 `function_call`；连续 reasoning 与多个 function call 的顺序保持不变
- Responses -> canonical：`reasoning` output/input item 提取 summary/content，附着到对应 assistant message 或 function call；不要把 reasoning 文本写成 `output_text`
- `function_call_output` 续接前，验证对应 assistant function call 已带 reasoning；缺失时对启用 protocol 的 DeepSeek 返回显式 400
- 流式 bridge 处理 `response.reasoning_summary_text.delta`、`response.output_item.added/done`、`response.failed`、`response.completed`；初始化 iterator 的 block/index 状态，保留原始中途异常和 fallback 能力
- 对 public `/v1/responses` 的 DeepSeek OpenAI-compatible 路径，使用同一 canonical helper 编译顶层 `reasoning_content`，不能只修原生 Messages。DeepSeek 无状态时不把 `previous_response_id` 发送给上游；只有 LiteLLM 已解析出完整历史时才展开发送
- 原有 Claude、OpenAI、Azure 行为必须由 protocol gate 隔离；没有 `reasoning_protocol` 的 deployment 不改变当前 adapter 选择

### 5.5 Usage、缓存与计费

在 DeepSeek Anthropic response 和 Responses bridge 各自增加字段别名归一化：

| Provider 字段 | LiteLLM 标准字段 | 要求 |
| --- | --- | --- |
| `prompt_cache_hit_tokens` | `prompt_tokens_details.cached_tokens` | 数值化并保留 |
| `prompt_cache_hit_tokens` | `cache_read_input_tokens` | Anthropic usage 与 SpendLog 同步 |
| `prompt_cache_miss_tokens` 或等价写入字段 | `cache_creation_input_tokens` | 有字段才写入，不虚构 |
| 普通 input/output 字段 | `prompt_tokens`/`completion_tokens`/`total_tokens` | 不重复计算缓存 token |

计算 cost 时遵循现有 Anthropic 规则：缓存读 token 不再次计入未缓存 input；既有 model pricing 缺项时保持标准 fallback，不把 provider 原始 usage 丢到日志。SpendLog、Prometheus 和最终 `ModelResponse` 三处都要使用同一个归一化结果

### 5.6 Router fallback 与错误语义

- 首 token 前的 DeepSeek 400：Router 可以按既有 fallback policy 选择下一 deployment；原始错误作为 primary cause 保留，fallback 失败时返回包含两者的现有错误结构
- 已发出任意 token 后的流式错误：不能把已输出内容重放成另一 provider 的新流；保留原始 mid-stream error event/日志，并按现有 Router 能力决定是否尝试 fallback
- fallback deployment 重新读取自己的 `model_info.reasoning_protocol`，不能沿用失败 deployment 的 config 或内部 metadata
- 记录 protocol、deployment id、fallback order、错误阶段和 usage 摘要；禁止记录 reasoning 正文和敏感 header

## 6. 预计源码与测试落点

实现时保持 `tests/test_litellm/` 与源码镜像关系，优先扩展现有测试文件

| 逻辑 | 源码落点 | 回归测试 |
| --- | --- | --- |
| protocol selector、DeepSeek wire 编译 | `litellm/llms/anthropic/experimental_pass_through/messages/handler.py`, `litellm/llms/deepseek/messages/transformation.py` | `tests/test_litellm/llms/deepseek/messages/test_deepseek_anthropic_messages_transformation.py` |
| Messages 流式 reasoning/终态/usage | 现有 Anthropic handler 与 response transformation | 对应 `tests/test_litellm/llms/anthropic/...` mapped tests |
| Anthropic <-> Responses bridge | `litellm/llms/anthropic/experimental_pass_through/responses_adapters/{transformation,streaming_iterator}.py` | `tests/test_litellm/llms/anthropic/experimental_pass_through/responses_adapters/test_responses_adapters_transformation.py` 及 streaming mapped test |
| Responses input/session reasoning | `litellm/responses/litellm_completion_transformation/{transformation,session_handler}.py` | `tests/test_litellm/responses/litellm_completion_transformation/test_reasoning_content_transformation.py`, `test_session_handler.py` |
| Router 首 token/中途 fallback | 现有 Router streaming fallback 路径 | `tests/router_unit_tests/test_router_aresponses_streaming_fallback.py` 及现有 responses fallback tests |
| dict/Pydantic 终态、SpendLog/cost | 现有 responses logging/cost 归一化入口 | `tests/test_litellm/responses/test_no_duplicate_spend_logs.py`, Anthropic cost/dict safety mapped tests |

必须有行为断言的测试：

1. **选择隔离**：同一个 `deepseek-v4-*` 模型，只有 deployment `model_info.reasoning_protocol` 才选择 DeepSeek config；客户端 metadata、模型名相似度和普通 Anthropic deployment 都不能触发
2. **wire-level**：注入 HTTP transport，断言请求 URL 为 `/v1/messages`，assistant tool turn 的 body 含 `{type: "thinking", thinking: "..."}`，不含 `signature`，并保持 thinking/text/tool_use 顺序
3. **历史规则**：provider_specific_fields 可恢复；已有 reasoning 不被覆盖；缺 reasoning 的 tool turn 返回 400；无工具普通多轮和 thinking disabled 不注入空格；输入消息对象不变
4. **Responses**：reasoning item 不再成为 output_text；reasoning、function_call、function_call_output 顺序和 call id 正确；无状态请求缺历史时显式失败；Pydantic 和 dict 终态均通过
5. **streaming**：reasoning delta、text delta、tool delta、completed usage 顺序稳定；首 token 前 failed 可 fallback；中途 failed 保留原始错误和已发事件
6. **缓存/计费**：`prompt_cache_hit_tokens` 同时映射到 cached_tokens 与 `cache_read_input_tokens`，SpendLog/cost 不为零且不重复计算
7. **Claude 不回归**：带无效/缺失 signature 的普通 Claude 请求仍执行原有保护策略；不受 DeepSeek protocol 分支影响

## 7. 分阶段提交与验证

建议拆成可回滚的逻辑提交：

1. `fix(deepseek): preserve reasoning in anthropic messages`：selector、canonical 提取、无签名 wire 编译和显式缺失错误
2. `fix(responses): replay reasoning items for deepseek protocol`：Responses bridge/session、流式状态和无状态约束
3. `fix(usage): normalize deepseek cache token fields`：usage、SpendLog、cost 的统一归一化
4. `test(responses): cover deepseek reasoning fallback and cache usage`：完整回归矩阵和 dict/Pydantic 终态测试

每个提交前检查：

- 基于当前 release tag 检查 #32110/#27425/#26678 的上下文依赖，不能合入其无关重构
- 运行对应 mapped unit tests、类型检查和 `make pre-commit`；源码提交前不将 `.env`、密钥、线上配置或请求日志纳入 staged files
- 用 `git diff --check` 和 `git range-diff <release-tag>...HEAD` 审核补丁边界
- 端到端验证使用脱敏环境变量和可控 provider mock；真实线上证明应通过本地 proxy 的 `/v1/responses`、`stream=true` curl，输出只展示状态、事件类型、usage 摘要和错误阶段

## 8. 发布、监控与回滚

先只给一个受影响的 DeepSeek deployment 增加 `reasoning_protocol`，确认 wire body、工具多轮成功率、首 token 前/中途错误和 fallback 率，再扩大到同类 deployment。监控至少包含：DeepSeek 400（缺 reasoning）、fallback 次数及最终失败率、`response.failed`/`response.completed` 比例、cache read token、SpendLog cost、`dict object has no attribute usage` 搜索结果

回滚优先删除 deployment 的 `reasoning_protocol` 字段使其恢复旧路由；若旧路由仍会触发已知 400，则回滚对应补丁提交。任何回滚都不得改动 Claude deployment 或外部密钥配置

## 9. 完成判定

只有以下条件全部满足才允许构建镜像：受信 deployment 的 `/v1/messages` 和 `/v1/responses` 流式/非流式请求均能保留 thinking；工具多轮完整回传 reasoning；缺失 reasoning 明确失败而非空格掩盖；首 token 前、中途 fallback 保留原始错误；dict/Pydantic 终态、usage、缓存和 SpendLog 正常；Claude 现有 signature 保护测试通过；未向仓库提交任何线上敏感配置
