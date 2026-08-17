"""
Translates from OpenAI's `/v1/chat/completions` to DeepSeek's `/v1/chat/completions`
"""

from collections.abc import Coroutine, Mapping
from typing import Any, Final, Literal, cast, overload

import litellm
from litellm.litellm_core_utils.prompt_templates.common_utils import (
    handle_messages_with_content_list_to_str_conversion,
)
from litellm.router_protocol import get_deployment_protocol_context
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.openai import AllMessageValues
from litellm.utils import supports_reasoning

from ...openai.chat.gpt_transformation import OpenAIGPTConfig


class _DeepSeekChatHistoryValidationError(litellm.BadRequestError):
    pass


class DeepSeekChatConfig(OpenAIGPTConfig):
    def get_supported_openai_params(self, model: str) -> list:
        """
        DeepSeek reasoner models support thinking parameter.
        """
        params: Final = super().get_supported_openai_params(model)
        params.extend(["thinking", "reasoning_effort"])
        return params

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        """
        Map OpenAI params to DeepSeek params.

        Handles `thinking` and `reasoning_effort` parameters for DeepSeek reasoner models.
        DeepSeek supports `{"type": "enabled"}` and `{"type": "disabled"}` - no budget_tokens
        like Anthropic. `reasoning_effort="none"` is the OpenAI-style way to ask for thinking
        off, so it maps to `{"type": "disabled"}`; any other effort keeps thinking on.

        Reference: https://api-docs.deepseek.com/guides/thinking_mode
        """
        # Let parent handle standard params first
        optional_params = super().map_openai_params(non_default_params, optional_params, model, drop_params)

        # Pop thinking/reasoning_effort from optional_params first (parent may have added them)
        # Then re-add only if valid for DeepSeek
        thinking_value: Final = optional_params.pop("thinking", None)
        reasoning_effort: Final = optional_params.pop("reasoning_effort", None)
        normalized_reasoning_effort: Final = {
            "minimal": "low",
            "medium": "high",
            "xhigh": "high",
        }.get(reasoning_effort, reasoning_effort)

        # Handle thinking parameter - accept both enabled and disabled, ignore budget_tokens
        if isinstance(thinking_value, dict) and thinking_value.get("type") in ("enabled", "disabled"):
            optional_params["thinking"] = {"type": thinking_value["type"]}
            if thinking_value["type"] == "enabled" and normalized_reasoning_effort in {"low", "high", "max"}:
                optional_params["reasoning_effort"] = normalized_reasoning_effort

        # Otherwise fall back to reasoning_effort: "none" disables, anything else enables
        elif reasoning_effort is not None:
            optional_params["thinking"] = {"type": "disabled" if reasoning_effort == "none" else "enabled"}
            if normalized_reasoning_effort in {"low", "high", "max"}:
                optional_params["reasoning_effort"] = normalized_reasoning_effort

        return optional_params

    @staticmethod
    def _message_has_tool_history(message: Mapping[str, object]) -> bool:
        tool_calls: Final = message.get("tool_calls")
        if isinstance(tool_calls, list) and bool(tool_calls):
            return True
        if isinstance(message.get("function_call"), Mapping):
            return True
        content: Final = message.get("content")
        return isinstance(content, list) and any(
            isinstance(block, Mapping) and block.get("type") in {"tool_use", "server_tool_use"} for block in content
        )

    @staticmethod
    def _message_reasoning(message: Mapping[str, object]) -> tuple[str | None, bool]:
        block_lists: Final = tuple(
            block
            for field in ("thinking_blocks", "content")
            for block in (message.get(field) if isinstance(message.get(field), list) else [])
        )
        has_redacted_thinking: Final = any(
            isinstance(block, Mapping) and block.get("type") == "redacted_thinking" for block in block_lists
        )
        raw_reasoning: Final = message.get("reasoning_content")
        if isinstance(raw_reasoning, str) and raw_reasoning.strip():
            return raw_reasoning, has_redacted_thinking
        provider_fields: Final = message.get("provider_specific_fields")
        if isinstance(provider_fields, Mapping):
            stored_reasoning: Final = provider_fields.get("reasoning_content")
            if isinstance(stored_reasoning, str) and stored_reasoning.strip():
                return stored_reasoning, has_redacted_thinking
        reasoning_block: Final = next(
            (
                block.get("thinking")
                for block in block_lists
                if isinstance(block, Mapping)
                and block.get("type") == "thinking"
                and isinstance(block.get("thinking"), str)
                and block["thinking"].strip()
            ),
            None,
        )
        return (reasoning_block if isinstance(reasoning_block, str) else None), has_redacted_thinking

    def _fill_reasoning_content(
        self,
        messages: list[AllMessageValues],
        model: str,
        litellm_params: dict,
        require_reasoning: bool,
    ) -> list[AllMessageValues]:
        protocol_context: Final = get_deployment_protocol_context(litellm_params)
        missing_reasoning: Final = protocol_context.missing_reasoning if protocol_context is not None else None
        result: Final[list[AllMessageValues]] = []
        for message in messages:
            if message.get("role") != "assistant":
                result.append(message)
                continue
            source_message: Final = dict(cast(dict, message))
            reasoning, has_redacted_thinking = self._message_reasoning(source_message)
            raw_content: Final = source_message.get("content")
            content_without_thinking: Final = (
                [
                    block
                    for block in raw_content
                    if not (isinstance(block, Mapping) and block.get("type") in {"thinking", "redacted_thinking"})
                ]
                if isinstance(raw_content, list)
                else raw_content
            )
            sanitized_content: Final = (
                content_without_thinking
                if not isinstance(content_without_thinking, list) or content_without_thinking
                else None
            )
            patched: Final = {
                **{
                    key: sanitized_content if key == "content" else value
                    for key, value in source_message.items()
                    if key not in {"thinking_blocks", "provider_specific_fields", "reasoning_content"}
                },
                **({"reasoning_content": reasoning} if reasoning is not None else {}),
            }
            if not self._message_has_tool_history(patched):
                has_visible_content: Final = (
                    bool(sanitized_content.strip())
                    if isinstance(sanitized_content, str)
                    else bool(sanitized_content)
                    if isinstance(sanitized_content, list)
                    else sanitized_content is not None
                )
                if has_redacted_thinking and reasoning is None and not has_visible_content:
                    raise _DeepSeekChatHistoryValidationError(
                        message="DeepSeek chat history cannot replay redacted-only thinking",
                        model=model,
                        llm_provider="deepseek",
                    )
                result.append(cast(AllMessageValues, patched))
                continue
            if has_redacted_thinking and require_reasoning and reasoning is None:
                raise _DeepSeekChatHistoryValidationError(
                    message="DeepSeek chat tool history cannot replay redacted thinking",
                    model=model,
                    llm_provider="deepseek",
                )
            if reasoning is None and require_reasoning and missing_reasoning != "placeholder":
                raise _DeepSeekChatHistoryValidationError(
                    message="DeepSeek chat tool history requires non-empty reasoning_content",
                    model=model,
                    llm_provider="deepseek",
                )
            if reasoning is None and require_reasoning:
                patched["reasoning_content"] = " "
            result.append(cast(AllMessageValues, patched))
        return result

    @overload
    def _transform_messages(
        self, messages: list[AllMessageValues], model: str, is_async: Literal[True]
    ) -> Coroutine[Any, Any, list[AllMessageValues]]: ...

    @overload
    def _transform_messages(
        self,
        messages: list[AllMessageValues],
        model: str,
        is_async: Literal[False] = False,
    ) -> list[AllMessageValues]: ...

    def _transform_messages(
        self, messages: list[AllMessageValues], model: str, is_async: bool = False
    ) -> list[AllMessageValues] | Coroutine[Any, Any, list[AllMessageValues]]:
        """
        DeepSeek does not support content in list format.
        """
        messages = handle_messages_with_content_list_to_str_conversion(messages)
        if is_async:
            return super()._transform_messages(messages=messages, model=model, is_async=True)
        else:
            return super()._transform_messages(messages=messages, model=model, is_async=False)

    def _thinking_mode_active(self, model: str, optional_params: dict) -> bool:
        """
        Returns True when thinking mode is active for this request.
        """
        thinking: Final = optional_params.get("thinking")
        thinking_type: Final = thinking.get("type") if isinstance(thinking, Mapping) else None
        if thinking_type == "disabled":
            return False
        if thinking_type == "enabled":
            return supports_reasoning(model=model, custom_llm_provider="deepseek")
        model_name: Final = model.rsplit("/", maxsplit=1)[-1].lower()
        return model_name.startswith("deepseek-v4")

    @staticmethod
    def _drop_unsupported_tools(optional_params: dict) -> dict:
        """
        DeepSeek's /chat/completions only accepts tools of type "function".

        Requests bridged from /v1/responses can carry responses-API-native tool
        types (e.g. a Codex CLI tool typed "namespace"); DeepSeek rejects the
        whole request with `unknown variant '<type>', expected 'function'` (issue
        #30722). Drop the unsupported entries so the function tools still go
        through, and drop the now-dangling tool_choice/parallel_tool_calls when
        nothing callable survives.

        When a specific `tool_choice` points at a dropped tool, clear it so the
        sanitized request does not reference a tool DeepSeek will never receive.
        """
        tools: Final = optional_params.get("tools")
        if not isinstance(tools, list) or not tools:
            return optional_params

        def _is_function_tool(tool: object) -> bool:
            return isinstance(tool, dict) and tool.get("type") == "function"

        def _get_function_tool_name(tool: object) -> str | None:
            if not isinstance(tool, dict):
                return None
            function: Final = tool.get("function")
            if not isinstance(function, dict):
                return None
            name: Final = function.get("name")
            return name if isinstance(name, str) else None

        def _tool_choice_matches_function_tool(tool_choice: object, function_tool_names: set[str]) -> bool:
            if not isinstance(tool_choice, dict):
                return True
            if tool_choice.get("type") != "function":
                return False
            function: Final = tool_choice.get("function")
            if not isinstance(function, dict):
                return False
            name: Final = function.get("name")
            return isinstance(name, str) and name in function_tool_names

        function_tools: Final = [tool for tool in tools if _is_function_tool(tool)]
        if len(function_tools) == len(tools):
            return optional_params

        dropped_types: Final = sorted(
            {
                str(tool.get("type")) if isinstance(tool, dict) else type(tool).__name__
                for tool in tools
                if not _is_function_tool(tool)
            }
        )
        litellm.verbose_logger.warning(
            "DeepSeek chat completions only supports function tools; dropping "
            "unsupported tool type(s) %s before sending the request",
            dropped_types,
        )

        cleaned = {k: v for k, v in optional_params.items() if k != "tools"}
        if function_tools:
            function_tool_names: Final = {
                name for tool in function_tools for name in (_get_function_tool_name(tool),) if name is not None
            }
            if not _tool_choice_matches_function_tool(cleaned.get("tool_choice"), function_tool_names):
                cleaned = {k: v for k, v in cleaned.items() if k != "tool_choice"}
            return {**cleaned, "tools": function_tools}
        return {k: v for k, v in cleaned.items() if k not in ("tool_choice", "parallel_tool_calls")}

    def transform_request(
        self,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        """
        Ensures `reasoning_content` is forwarded on assistant messages for
        multi-turn thinking-mode conversations (issue #28045).

        DeepSeek V4 enables thinking by default. Other reasoning models still
        require an explicit thinking parameter.
        """
        optional_params = self._drop_unsupported_tools(optional_params)
        protocol_context: Final = get_deployment_protocol_context(litellm_params)
        if (
            protocol_context is not None
            and protocol_context.tool_thinking == "disabled"
            and optional_params.get("tools")
        ):
            optional_params = {**optional_params, "thinking": {"type": "disabled"}}
        thinking_mode_active: Final = self._thinking_mode_active(model=model, optional_params=optional_params)
        messages = self._fill_reasoning_content(
            messages,
            model=model,
            litellm_params=litellm_params,
            require_reasoning=thinking_mode_active,
        )
        return super().transform_request(
            model=model,
            messages=messages,
            optional_params=optional_params,
            litellm_params=litellm_params,
            headers=headers,
        )

    async def async_transform_request(
        self,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        """
        Async equivalent of transform_request — applies the same reasoning_content
        fix for multi-turn thinking-mode conversations.
        """
        optional_params = self._drop_unsupported_tools(optional_params)
        protocol_context: Final = get_deployment_protocol_context(litellm_params)
        if (
            protocol_context is not None
            and protocol_context.tool_thinking == "disabled"
            and optional_params.get("tools")
        ):
            optional_params = {**optional_params, "thinking": {"type": "disabled"}}
        thinking_mode_active: Final = self._thinking_mode_active(model=model, optional_params=optional_params)
        messages = self._fill_reasoning_content(
            messages,
            model=model,
            litellm_params=litellm_params,
            require_reasoning=thinking_mode_active,
        )
        return await super().async_transform_request(
            model=model,
            messages=messages,
            optional_params=optional_params,
            litellm_params=litellm_params,
            headers=headers,
        )

    def _get_openai_compatible_provider_info(
        self, api_base: str | None, api_key: str | None
    ) -> tuple[str | None, str | None]:
        api_base = api_base or get_secret_str("DEEPSEEK_API_BASE") or "https://api.deepseek.com/beta"
        dynamic_api_key: Final = api_key or get_secret_str("DEEPSEEK_API_KEY")
        return api_base, dynamic_api_key

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: bool | None = None,
    ) -> str:
        """
        If api_base is not provided, use the default DeepSeek /chat/completions endpoint.
        """
        if not api_base:
            api_base = "https://api.deepseek.com/beta"

        if not api_base.endswith("/chat/completions"):
            api_base = f"{api_base}/chat/completions"

        return api_base
