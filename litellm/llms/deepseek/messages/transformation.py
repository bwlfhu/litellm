"""
DeepSeek Anthropic-compatible messages transformation config.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx

import litellm
from litellm.llms.anthropic.common_utils import AnthropicError, is_anthropic_invalid_thinking_signature_error
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.anthropic_messages.anthropic_response import AnthropicMessagesResponse
from litellm.types.router import GenericLiteLLMParams

_DEEPSEEK_UNSUPPORTED_CONTENT_BLOCK_TYPES: Final = frozenset(
    {
        "image",
        "document",
        "search_result",
        "code_execution_tool_result",
        "mcp_tool_use",
        "mcp_tool_result",
        "container_upload",
    }
)


class _DeepSeekHistoryValidationError(AnthropicError):
    pass


def _deduplicated_path_segments(path: str) -> tuple[str, ...]:
    segments = tuple(segment for segment in path.split("/") if segment)
    return tuple(
        segment
        for index, segment in enumerate(segments)
        if index == 0 or segment not in {"anthropic", "v1"} or segment != segments[index - 1]
    )


def _without_trailing_segments(segments: tuple[str, ...], values: frozenset[str]) -> tuple[str, ...]:
    index = len(segments)
    while index and segments[index - 1] in values:
        index -= 1
    return segments[:index]


def _without_known_messages_suffix(segments: tuple[str, ...]) -> tuple[str, ...]:
    suffixes = (
        ("anthropic", "v1", "messages"),
        ("v1", "messages"),
        ("anthropic", "v1"),
        ("anthropic",),
        ("v1",),
    )
    return next(
        (segments[: -len(suffix)] for suffix in suffixes if segments[-len(suffix) :] == suffix),
        segments,
    )


def _url_with_path(base_url: str, path_segments: tuple[str, ...]) -> str:
    parsed = urlsplit(base_url)
    path = "/" + "/".join(path_segments)
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _complete_messages_url(base_url: str, messages_path: str | None) -> str:
    parsed = urlsplit(base_url)
    base_segments = _deduplicated_path_segments(parsed.path)
    if messages_path == "anthropic/v1/messages":
        prefix = _without_known_messages_suffix(base_segments)
        return _url_with_path(base_url, prefix + ("anthropic", "v1", "messages"))
    if messages_path == "v1/messages":
        prefix = _without_known_messages_suffix(base_segments)
        return _url_with_path(base_url, prefix + ("v1", "messages"))
    if parsed.path.rstrip("/").endswith("/v1/messages"):
        return base_url

    prefix = _without_trailing_segments(base_segments, frozenset({"v1", "beta"}))
    normalized = prefix if prefix[-1:] == ("anthropic",) else prefix + ("anthropic",)
    return _url_with_path(base_url, normalized + ("v1", "messages"))


def _nonempty_reasoning_content(message: Mapping[str, object]) -> str | None:
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content
    provider_specific_fields = message.get("provider_specific_fields")
    if isinstance(provider_specific_fields, Mapping):
        provider_reasoning_content = provider_specific_fields.get("reasoning_content")
        if isinstance(provider_reasoning_content, str) and provider_reasoning_content.strip():
            return provider_reasoning_content
    return None


def _unsigned_content_blocks(content: list[object]) -> tuple[list[object], bool, bool, bool]:
    transformed_content = []
    has_tool_use = False
    has_redacted_thinking = False
    has_reasoning = False
    for block in content:
        if not isinstance(block, Mapping):
            transformed_content.append(deepcopy(block))
            continue
        transformed_block = deepcopy(dict(block))
        block_type = transformed_block.get("type")
        if block_type == "thinking":
            transformed_block.pop("signature", None)
            thinking = transformed_block.get("thinking")
            if not isinstance(thinking, str) or not thinking.strip():
                continue
            has_reasoning = True
        elif block_type in {"tool_use", "server_tool_use"}:
            has_tool_use = True
        elif block_type == "redacted_thinking":
            has_redacted_thinking = True
            continue
        transformed_content.append(transformed_block)
    return transformed_content, has_tool_use, has_redacted_thinking, has_reasoning


def _deepseek_history_validation_error(message: str) -> AnthropicError:
    return _DeepSeekHistoryValidationError(message=message, status_code=400)


def _content_block_tree(block: object, depth: int = 0) -> tuple[tuple[object, int], ...]:
    if not isinstance(block, Mapping) or depth >= 1:
        return ((block, depth),)
    nested_content: Final = block.get("content")
    return (
        (block, depth),
        *(_content_blocks(nested_content, depth=depth + 1) if isinstance(nested_content, list) else ()),
    )


def _content_blocks(content: list[object], depth: int = 0) -> tuple[tuple[object, int], ...]:
    return tuple(nested_block for block in content for nested_block in _content_block_tree(block, depth=depth))


def _validate_deepseek_content_blocks(messages: list[dict], model: str | None = None) -> None:
    unsupported_types: Final = sorted(
        {
            block_type
            for message in messages
            for field in ("content", "thinking_blocks")
            for block, depth in _content_blocks(message.get(field) if isinstance(message.get(field), list) else [])
            if isinstance(block, Mapping)
            for block_type in (block.get("type"),)
            if isinstance(block_type, str)
            and (
                block_type in _DEEPSEEK_UNSUPPORTED_CONTENT_BLOCK_TYPES
                or (block_type == "redacted_thinking" and (message.get("role") != "assistant" or depth > 0))
            )
        }
    )
    if unsupported_types:
        raise litellm.utils.UnsupportedParamsError(
            message=(
                "DeepSeek Anthropic compatibility does not support content block type(s): "
                + ", ".join(unsupported_types)
            ),
            model=model,
            llm_provider="deepseek",
        )


def _sidecar_reasoning_blocks(message: Mapping[str, object]) -> tuple[list[object], bool, bool]:
    raw_thinking_blocks: Final = message.get("thinking_blocks")
    if not isinstance(raw_thinking_blocks, list):
        return [], False, False
    transformed_blocks, _, has_redacted_thinking, has_reasoning = _unsigned_content_blocks(raw_thinking_blocks)
    reasoning_blocks: Final = [
        block
        for block in transformed_blocks
        if isinstance(block, Mapping) and block.get("type") in {"thinking", "redacted_thinking"}
    ]
    return reasoning_blocks, has_redacted_thinking, has_reasoning


def _deepseek_history_message(
    message: dict,
    require_reasoning: bool,
    missing_reasoning: Literal["placeholder"] | None,
) -> dict:
    transformed_message = deepcopy(message)
    if transformed_message.get("role") != "assistant":
        return transformed_message

    reasoning_content: Final = _nonempty_reasoning_content(message)
    transformed_message.pop("reasoning_content", None)
    transformed_message.pop("provider_specific_fields", None)
    transformed_message.pop("thinking_blocks", None)
    sidecar_blocks, sidecar_has_redacted_thinking, sidecar_has_reasoning = _sidecar_reasoning_blocks(message)
    content: Final = transformed_message.get("content")
    if not isinstance(content, list):
        scalar_has_tool_history: Final = _chat_message_has_tool_history(message)
        reasoning_blocks: Final = (
            [{"type": "thinking", "thinking": reasoning_content}]
            if reasoning_content is not None
            else sidecar_blocks
            if sidecar_has_reasoning
            else []
        )
        scalar_has_reasoning: Final = bool(reasoning_blocks)
        if scalar_has_tool_history and sidecar_has_redacted_thinking and require_reasoning and not scalar_has_reasoning:
            raise _deepseek_history_validation_error("DeepSeek Anthropic tool history cannot replay redacted thinking")
        scalar_uses_placeholder: Final = (
            scalar_has_tool_history
            and require_reasoning
            and not scalar_has_reasoning
            and missing_reasoning == "placeholder"
        )
        if scalar_has_tool_history and require_reasoning and not scalar_has_reasoning and not scalar_uses_placeholder:
            raise _deepseek_history_validation_error("DeepSeek Anthropic tool history requires non-empty reasoning")
        replayable_reasoning: Final = (
            [{"type": "thinking", "thinking": " "}] if scalar_uses_placeholder else reasoning_blocks
        )
        text_blocks: Final = [{"type": "text", "text": content}] if isinstance(content, str) and content.strip() else []
        scalar_content: Final = [*replayable_reasoning, *text_blocks]
        if sidecar_has_redacted_thinking and not scalar_content:
            raise _deepseek_history_validation_error("DeepSeek Anthropic history cannot replay redacted-only thinking")
        if scalar_content:
            transformed_message["content"] = scalar_content
        return transformed_message

    transformed_content, has_tool_use, inline_has_redacted_thinking, inline_has_reasoning = _unsigned_content_blocks(
        content
    )
    content_without_inline_reasoning: Final = [
        block for block in transformed_content if not (isinstance(block, Mapping) and block.get("type") == "thinking")
    ]
    selected_content: Final = (
        [{"type": "thinking", "thinking": reasoning_content}, *content_without_inline_reasoning]
        if reasoning_content is not None
        else transformed_content
        if inline_has_reasoning
        else [*sidecar_blocks, *transformed_content]
        if sidecar_has_reasoning
        else transformed_content
    )
    has_reasoning: Final = inline_has_reasoning or sidecar_has_reasoning or reasoning_content is not None
    has_redacted_thinking: Final = inline_has_redacted_thinking or sidecar_has_redacted_thinking
    has_tool_history: Final = has_tool_use or _chat_message_has_tool_history(message)
    if has_tool_history and has_redacted_thinking and require_reasoning and not has_reasoning:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history cannot replay redacted thinking")
    use_placeholder: Final = (
        has_tool_history and require_reasoning and not has_reasoning and missing_reasoning == "placeholder"
    )
    if has_tool_history and require_reasoning and not has_reasoning and not use_placeholder:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history requires non-empty reasoning")
    final_content: Final = (
        [{"type": "thinking", "thinking": " "}, *selected_content] if use_placeholder else selected_content
    )
    if has_redacted_thinking and not final_content:
        raise _deepseek_history_validation_error("DeepSeek Anthropic history cannot replay redacted-only thinking")
    transformed_message["content"] = final_content
    return transformed_message


def _deepseek_history(
    messages: list[dict],
    require_reasoning: bool,
    missing_reasoning: Literal["placeholder"] | None,
) -> list[dict]:
    return [
        _deepseek_history_message(
            message,
            require_reasoning=require_reasoning,
            missing_reasoning=missing_reasoning,
        )
        for message in messages
    ]


def _without_reasoning_content_fields(message: dict) -> dict:
    transformed_message = {key: value for key, value in message.items() if key != "reasoning_content"}
    provider_specific_fields = transformed_message.get("provider_specific_fields")
    if not isinstance(provider_specific_fields, Mapping):
        return transformed_message
    remaining_provider_specific_fields = {
        key: value for key, value in provider_specific_fields.items() if key != "reasoning_content"
    }
    if remaining_provider_specific_fields:
        return {**transformed_message, "provider_specific_fields": remaining_provider_specific_fields}
    return {key: value for key, value in transformed_message.items() if key != "provider_specific_fields"}


def _message_blocks(message: Mapping[str, object], field: str) -> list[object]:
    value = message.get(field)
    return value if isinstance(value, list) else []


def _chat_message_has_tool_history(message: Mapping[str, object]) -> bool:
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and bool(tool_calls):
        return True
    if isinstance(message.get("function_call"), Mapping):
        return True
    return any(
        isinstance(block, Mapping) and block.get("type") in {"tool_use", "server_tool_use"}
        for block in _message_blocks(message, "content")
    )


def _prepare_deepseek_chat_message(
    message: dict,
    require_reasoning: bool,
    missing_reasoning: Literal["placeholder"] | None,
) -> dict:
    transformed_message = deepcopy(message)
    if transformed_message.get("role") != "assistant":
        return transformed_message

    content = transformed_message.get("content")
    if isinstance(content, list):
        transformed_content, content_has_tool_use, content_has_redacted_thinking, content_has_reasoning = (
            _unsigned_content_blocks(content)
        )
        transformed_message["content"] = transformed_content
    else:
        content_has_tool_use = False
        content_has_redacted_thinking = False
        content_has_reasoning = False

    raw_thinking_blocks = transformed_message.get("thinking_blocks")
    if isinstance(raw_thinking_blocks, list):
        thinking_blocks, _, blocks_have_redacted_thinking, blocks_have_reasoning = _unsigned_content_blocks(
            raw_thinking_blocks
        )
    else:
        thinking_blocks = []
        blocks_have_redacted_thinking = False
        blocks_have_reasoning = False

    has_reasoning = content_has_reasoning or blocks_have_reasoning
    has_redacted_thinking = content_has_redacted_thinking or blocks_have_redacted_thinking
    reasoning_content = _nonempty_reasoning_content(message)
    if reasoning_content is not None:
        transformed_message["content"] = (
            [
                block
                for block in transformed_message.get("content", [])
                if not (isinstance(block, Mapping) and block.get("type") == "thinking")
            ]
            if isinstance(transformed_message.get("content"), list)
            else transformed_message.get("content")
        )
        thinking_blocks = [{"type": "thinking", "thinking": reasoning_content}]
        has_reasoning = True

    has_tool_history = content_has_tool_use or _chat_message_has_tool_history(transformed_message)
    if has_tool_history and has_redacted_thinking and require_reasoning and not has_reasoning:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history cannot replay redacted thinking")
    if has_tool_history and require_reasoning and not has_reasoning and missing_reasoning == "placeholder":
        thinking_blocks.insert(0, {"type": "thinking", "thinking": " "})
    elif has_tool_history and require_reasoning and not has_reasoning:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history requires non-empty reasoning")

    transformed_message = _without_reasoning_content_fields(transformed_message)
    if thinking_blocks:
        transformed_message["thinking_blocks"] = thinking_blocks
    else:
        transformed_message.pop("thinking_blocks", None)
    replayable_content: Final = transformed_message.get("content")
    has_visible_content: Final = (
        bool(replayable_content.strip())
        if isinstance(replayable_content, str)
        else bool(replayable_content)
        if isinstance(replayable_content, list)
        else replayable_content is not None
    )
    if has_redacted_thinking and not has_visible_content and not thinking_blocks and not has_tool_history:
        raise _deepseek_history_validation_error("DeepSeek Anthropic history cannot replay redacted-only thinking")
    return transformed_message


def _legacy_function_call_id(message_index: int) -> str:
    return f"legacy_function_call_{message_index}"


def _upgrade_legacy_function_call(message: dict, message_index: int) -> dict:
    function_call = message.get("function_call")
    if message.get("role") != "assistant" or not isinstance(function_call, Mapping):
        return message
    existing_tool_calls = message.get("tool_calls")
    tool_calls = existing_tool_calls if isinstance(existing_tool_calls, list) else []
    return {
        **{key: value for key, value in message.items() if key != "function_call"},
        "tool_calls": [
            *tool_calls,
            {
                "id": _legacy_function_call_id(message_index),
                "type": "function",
                "function": deepcopy(dict(function_call)),
            },
        ],
    }


def _legacy_function_result_call_id(messages: list[dict], message_index: int) -> str | None:
    tool_call_id = messages[message_index].get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return tool_call_id
    for prior_index in range(message_index - 1, -1, -1):
        prior_message = messages[prior_index]
        if prior_message.get("role") == "assistant" and isinstance(prior_message.get("function_call"), Mapping):
            return _legacy_function_call_id(prior_index)
        if prior_message.get("role") in {"user", "tool", "function"}:
            return None
    return None


def _upgrade_legacy_function_result(message: dict, tool_call_id: str | None) -> dict:
    if message.get("role") != "function" or tool_call_id is None:
        return message
    return {**message, "role": "tool", "tool_call_id": tool_call_id}


def prepare_deepseek_chat_history(
    messages: list[dict],
    require_reasoning: bool = True,
    missing_reasoning: Literal["placeholder"] | None = None,
) -> list[dict]:
    _validate_deepseek_content_blocks(messages)
    prepared_messages = [
        _prepare_deepseek_chat_message(
            message,
            require_reasoning=require_reasoning,
            missing_reasoning=missing_reasoning,
        )
        for message in messages
    ]
    return [
        _upgrade_legacy_function_result(
            _upgrade_legacy_function_call(message, message_index),
            _legacy_function_result_call_id(prepared_messages, message_index),
        )
        for message_index, message in enumerate(prepared_messages)
    ]


def _normalized_tool_choice(tool_choice: object) -> object:
    if isinstance(tool_choice, str):
        return {
            "auto": {"type": "auto"},
            "none": {"type": "none"},
            "required": {"type": "any"},
            "any": {"type": "any"},
        }.get(tool_choice, tool_choice)
    if not isinstance(tool_choice, Mapping):
        return deepcopy(tool_choice)
    choice: Final = deepcopy(dict(tool_choice))
    choice_type: Final = choice.get("type")
    if choice_type == "required":
        return {**choice, "type": "any"}
    if choice_type == "function":
        function: Final = choice.get("function")
        if isinstance(function, Mapping) and isinstance(function.get("name"), str):
            return {
                **{key: value for key, value in choice.items() if key != "function"},
                "type": "tool",
                "name": function["name"],
            }
    if choice_type is None and isinstance(choice.get("name"), str):
        return {**choice, "type": "tool"}
    return choice


def _without_adaptive_reasoning_params(request_params: Mapping[str, object]) -> dict:
    without_reasoning_effort: Final = {
        key: deepcopy(value) for key, value in request_params.items() if key != "reasoning_effort"
    }
    raw_output_config: Final = without_reasoning_effort.get("output_config")
    raw_effort: Final = raw_output_config.get("effort") if isinstance(raw_output_config, Mapping) else None
    normalized_effort: Final = (
        {
            "minimal": "low",
            "medium": "high",
            "xhigh": "high",
        }.get(raw_effort, raw_effort)
        if isinstance(raw_effort, str)
        else None
    )
    output_config: Final = (
        {"effort": normalized_effort}
        if isinstance(normalized_effort, str) and normalized_effort in {"low", "high", "max"}
        else None
    )
    without_output_config: Final = {
        key: value for key, value in without_reasoning_effort.items() if key != "output_config"
    }
    normalized_output_config: Final = (
        {
            **without_output_config,
            "output_config": output_config,
        }
        if output_config is not None
        else without_output_config
    )
    raw_thinking: Final = normalized_output_config.get("thinking")
    if not isinstance(raw_thinking, Mapping):
        return normalized_output_config
    thinking_type: Final = raw_thinking.get("type")
    if thinking_type == "disabled":
        return {**normalized_output_config, "thinking": {"type": "disabled"}}
    if thinking_type in {"enabled", "adaptive"}:
        return {**normalized_output_config, "thinking": {"type": "enabled"}}
    return normalized_output_config


def default_deepseek_anthropic_thinking_to_disabled(
    request_params: Mapping[str, object],
) -> dict[str, object]:
    if request_params.get("thinking") is not None:
        return dict(request_params)
    return {**request_params, "thinking": {"type": "disabled"}}


class DeepSeekAnthropicMessagesConfig(AnthropicMessagesConfig):
    """
    DeepSeek exposes an Anthropic-compatible Messages API at
    https://api.deepseek.com/anthropic.

    It accepts the native Anthropic Messages conversation shape, including
    thinking blocks in assistant history, but rejects Anthropic's explicit
    custom-tool discriminator (`{"type": "custom"}`).
    """

    def __init__(
        self,
        messages_path: Literal["anthropic/v1/messages", "v1/messages"] | None = None,
        tool_thinking: Literal["disabled"] | None = None,
        missing_reasoning: Literal["placeholder"] | None = None,
    ) -> None:
        self._messages_path = messages_path
        self._tool_thinking = tool_thinking
        self._missing_reasoning = missing_reasoning

    @property
    def custom_llm_provider(self) -> str | None:
        return "deepseek"

    def should_strip_billing_metadata(self) -> bool:
        return True

    @staticmethod
    def _translate_adaptive_effort_for_non_adaptive_model(
        model: str, optional_params: dict, max_tokens: int | None, custom_llm_provider: str
    ) -> None:
        return

    @staticmethod
    def get_api_key(api_key: str | None = None) -> str | None:
        return api_key or get_secret_str("DEEPSEEK_API_KEY") or litellm.api_key

    @staticmethod
    def get_api_base(api_base: str | None = None) -> str:
        return (
            api_base
            or get_secret_str("DEEPSEEK_ANTHROPIC_API_BASE")
            or get_secret_str("DEEPSEEK_API_BASE")
            or "https://api.deepseek.com/anthropic"
        )

    def validate_anthropic_messages_environment(
        self,
        headers: dict,
        model: str,
        messages: list[Any],
        optional_params: dict,
        litellm_params: dict,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> tuple[dict, str | None]:
        dynamic_api_key = self.get_api_key(api_key=api_key)

        if "x-api-key" not in headers and "authorization" not in headers and dynamic_api_key is not None:
            headers["x-api-key"] = dynamic_api_key

        if "anthropic-version" not in headers:
            headers["anthropic-version"] = "2023-06-01"
        if "content-type" not in headers:
            headers["content-type"] = "application/json"

        headers = self._update_headers_with_anthropic_beta(
            headers=headers,
            optional_params=optional_params,
            custom_llm_provider=self.custom_llm_provider or "deepseek",
        )

        return headers, api_base

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: bool | None = None,
    ) -> str:
        base_url = self.get_api_base(api_base=api_base).rstrip("/")
        return _complete_messages_url(base_url, self._messages_path)

    @staticmethod
    def _sanitize_tools_for_deepseek(tools: Any) -> Any:
        if not isinstance(tools, list):
            return tools

        sanitized_tools = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "custom":
                sanitized_tool = dict(tool)
                sanitized_tool.pop("type", None)
                sanitized_tools.append(sanitized_tool)
            else:
                sanitized_tools.append(tool)
        return sanitized_tools

    def transform_anthropic_messages_request(
        self,
        model: str,
        messages: list[dict],
        anthropic_messages_optional_request_params: dict,
        litellm_params: GenericLiteLLMParams,
        headers: dict,
    ) -> dict:
        _validate_deepseek_content_blocks(messages, model=model)
        normalized_reasoning_params: Final = _without_adaptive_reasoning_params(
            anthropic_messages_optional_request_params
        )
        normalized_tool_choice: Final = _normalized_tool_choice(normalized_reasoning_params.get("tool_choice"))
        request_params_with_tool_choice: Final = {
            **{key: value for key, value in normalized_reasoning_params.items() if key != "tool_choice"},
            **({"tool_choice": normalized_tool_choice} if normalized_tool_choice is not None else {}),
        }
        request_params_with_thinking_default: Final = default_deepseek_anthropic_thinking_to_disabled(
            request_params_with_tool_choice
        )
        should_disable_thinking: Final = self._tool_thinking == "disabled" and bool(
            request_params_with_thinking_default.get("tools")
        )
        request_params: Final = (
            {**request_params_with_thinking_default, "thinking": {"type": "disabled"}}
            if should_disable_thinking
            else request_params_with_thinking_default
        )
        thinking: Final = request_params.get("thinking")
        require_reasoning: Final = isinstance(thinking, Mapping) and thinking.get("type") == "enabled"
        transformed_messages: Final = _deepseek_history(
            messages,
            require_reasoning=require_reasoning,
            missing_reasoning=self._missing_reasoning,
        )
        anthropic_messages_request = super().transform_anthropic_messages_request(
            model=model,
            messages=transformed_messages,
            anthropic_messages_optional_request_params=request_params,
            litellm_params=litellm_params,
            headers=headers,
        )
        if "tools" in anthropic_messages_request:
            anthropic_messages_request["tools"] = self._sanitize_tools_for_deepseek(anthropic_messages_request["tools"])
        return anthropic_messages_request

    def transform_anthropic_messages_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: object,
    ) -> AnthropicMessagesResponse:
        response = super().transform_anthropic_messages_response(
            model=model,
            raw_response=raw_response,
            logging_obj=logging_obj,
        )
        reasoning_content = response.pop("reasoning_content", None)
        provider_specific_fields = response.get("provider_specific_fields")
        if isinstance(provider_specific_fields, Mapping):
            provider_reasoning_content = provider_specific_fields.get("reasoning_content")
            if not isinstance(reasoning_content, str) or not reasoning_content.strip():
                reasoning_content = provider_reasoning_content
            remaining_provider_specific_fields = {
                key: value for key, value in provider_specific_fields.items() if key != "reasoning_content"
            }
            if remaining_provider_specific_fields:
                response["provider_specific_fields"] = remaining_provider_specific_fields
            else:
                response.pop("provider_specific_fields", None)
        content = response.get("content")
        content_blocks = (
            [
                block
                for block in content
                if not (
                    isinstance(block, Mapping)
                    and block.get("type") == "thinking"
                    and (not isinstance(block.get("thinking"), str) or not block["thinking"].strip())
                )
            ]
            if isinstance(content, list)
            else []
        )
        has_thinking = any(
            isinstance(block, Mapping)
            and block.get("type") == "thinking"
            and isinstance(block.get("thinking"), str)
            and bool(block["thinking"].strip())
            for block in content_blocks
        )
        if isinstance(reasoning_content, str) and reasoning_content.strip() and not has_thinking:
            response["content"] = [{"type": "thinking", "thinking": reasoning_content}, *content_blocks]
        elif isinstance(content, list) and content_blocks != content:
            response["content"] = content_blocks
        return response

    @property
    def max_retry_on_anthropic_messages_http_error(self) -> int:
        return 1

    def should_retry_anthropic_messages_on_http_error(self, e: httpx.HTTPStatusError, litellm_params: dict) -> bool:
        return False

    def should_disable_anthropic_messages_fallbacks_on_http_error(self, e: httpx.HTTPStatusError) -> bool:
        return e.response.status_code == 400 and is_anthropic_invalid_thinking_signature_error(e.response.text)
