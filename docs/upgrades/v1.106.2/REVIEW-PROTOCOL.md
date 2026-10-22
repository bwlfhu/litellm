# v1.106.2 协议与推理历史独立复核

复核时间：2026-10-22 UTC

复核对象为 `/home/allcam/projects/litellm-v11062-upgrade` 合并工作区，定制基线 `origin/mainline=abadbbd2cc`，官方目标 `v1.106.2=49943cd52e`。本复核比较当前文件、定制基线和官方目标三方源码，仅写入外部证据文件，未修改共享源码

## 已发现并由主会话修复的问题

P1：DeepSeek 原生 Messages 的 HTTP 错误处理签名不兼容。官方在 `BaseLLMHTTPHandler._async_post_anthropic_messages_with_http_error_retry` 调用中新增 `request_body=request_body`，并扩展基类 `should_retry_anthropic_messages_on_http_error`，但是原定制 `DeepSeekAnthropicMessagesConfig` 覆盖方法没有接受此参数。结果为任意触发该错误处理路径的 DeepSeek HTTP 失败被 `TypeError: ... got an unexpected keyword argument 'request_body'` 替换，丢失真实上游状态和内容

实际回归首先发现两项失败，分别为 `test_deepseek_messages_error_does_not_retry_blocked_non_protocol_orders` 和 `test_deepseek_signature_error_does_not_retry_while_claude_keeps_retrying`。失败前已有 252 项通过，主会话收到问题后补齐 DeepSeek 覆盖方法的 `request_body: object = None` 参数，同时保持 `return False`，保留 DeepSeek 不重试策略。复核再次运行完整 DeepSeek Messages 文件，259 项全部通过

核对位置：`litellm/llms/custom_httpx/llm_http_handler.py:2177` 和 `litellm/llms/deepseek/messages/transformation.py:1526`。全仓检索该覆盖方法仅发现基类和 DeepSeek 两个定义，未发现其他同类遗漏

## 合并保留情况

Responses 输出合并为单个 `choices[0]` 的官方修复已保留。定制的多个 reasoning item 累积规则继续使用列表，官方 fold 函数适配为接受全部尾部 reasoning item，合并时保留顺序、每项 `id`、每项 `encrypted_content` 和 summary。工具调用继续集中在同一条 choice，文本与 citation 偏移跟随所有 message 合并

实际执行无网络转换检查，输入两个文本 message、四个交错 reasoning item、一个 custom tool call 和一个 function call，输出只有一条 choice，文本、四项有序 reasoning item、四份 opaque encrypted content 和两项工具均完整

Anthropic 有签名空 thinking 的官方保留策略已保留。`strip_empty_content_blocks_from_anthropic_messages` 使用 `is_empty_unsigned_thinking_block`，普通 Anthropic chat 历史转换也继续保留带签名空 thinking。DeepSeek 专用模式继续只保留非空真实推理，可保留无签名真实推理，并拒绝仅空白的 thinking。实际执行同一混合历史的两种转换，普通 Anthropic 仅保留有签名空 thinking，DeepSeek 模式仅保留非空无签名真实推理，结果符合两类上游各自的重放规则

原生 Messages SSE 修复仍在。DeepSeek SSE sanitizer 根据 JSON 内的 `type` 重写 `event:`，保持两者一致，comment keepalive 原样通过。代理的 Messages 异常串流继续使用 Anthropic `error` 事件和对应 JSON `type`，已打开流时不会追加 Chat 的 `[DONE]`。TTFT keepalive 继续使用 SSE comment，不制造消息事件类型

## 验证

`review-protocol-targeted-tests.log`：906 项通过，覆盖 DeepSeek chat、Anthropic SSE wrapper、Responses 转换、prompt factory 与 Anthropic 公共工具

`review-deepseek-messages-tests.log`：修复后 259 项通过，覆盖 DeepSeek 原生 Messages，包括 HTTP 失败、推理历史、工具和 SSE 定制行为

`review-sse-tests.log`：36 项通过，397 项因定向选择未运行，覆盖 keepalive、首字节前失败、半帧中断和原生 Anthropic SDK 对错误事件的解析

最终独立定向测试合计 1,201 项通过，另有三组无网络转换检查通过。没有执行生产请求，不把本复核视为上线验收

## 结论

发现的 HTTP 错误路径回归已经修复并由原有行为回归覆盖。当前重点范围未发现其他阻断问题，Responses 单 choice 修复与多个 reasoning item 定制逻辑兼容，Anthropic 和 DeepSeek 两种不同推理重放策略均保持，Messages SSE 错误和保活行为未回归。生产候选镜像仍需由主会话完成真实请求验证
