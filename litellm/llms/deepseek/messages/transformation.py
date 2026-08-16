"""
DeepSeek Anthropic-compatible messages transformation config.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

import litellm
from litellm.llms.anthropic.common_utils import AnthropicError
from litellm.llms.anthropic.experimental_pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.anthropic_messages.anthropic_response import AnthropicMessagesResponse
from litellm.types.router import GenericLiteLLMParams

_DEEPSEEK_MESSAGES_PATHS = frozenset({"anthropic/v1/messages", "v1/messages"})


class _DeepSeekHistoryValidationError(AnthropicError):
    _litellm_disable_fallbacks = True


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


def _url_with_path(base_url: str, path_segments: tuple[str, ...]) -> str:
    parsed = urlsplit(base_url)
    path = "/" + "/".join(path_segments)
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _complete_messages_url(base_url: str, messages_path: str | None) -> str:
    parsed = urlsplit(base_url)
    if parsed.path.rstrip("/").endswith("/v1/messages"):
        return base_url

    base_segments = _deduplicated_path_segments(parsed.path)
    if messages_path == "anthropic/v1/messages":
        prefix = _without_trailing_segments(base_segments, frozenset({"anthropic", "v1"}))
        return _url_with_path(base_url, prefix + ("anthropic", "v1", "messages"))
    if messages_path == "v1/messages":
        prefix = _without_trailing_segments(base_segments, frozenset({"v1"}))
        return _url_with_path(base_url, prefix + ("v1", "messages"))

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
            has_reasoning = has_reasoning or (isinstance(thinking, str) and bool(thinking.strip()))
        elif block_type in {"tool_use", "server_tool_use"}:
            has_tool_use = True
        elif block_type == "redacted_thinking":
            has_redacted_thinking = True
        transformed_content.append(transformed_block)
    return transformed_content, has_tool_use, has_redacted_thinking, has_reasoning


def _promoted_tool_reasoning_content(content: list[object]) -> list[object] | None:
    tool_index = next(
        (
            index
            for index, block in enumerate(content)
            if isinstance(block, Mapping) and block.get("type") in {"tool_use", "server_tool_use"}
        ),
        -1,
    )
    if tool_index <= 0:
        return None
    leading_blocks = content[:tool_index]
    if not all(
        isinstance(block, Mapping)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and bool(block["text"].strip())
        for block in leading_blocks
    ):
        return None
    if any(isinstance(block, Mapping) and block.get("type") == "text" for block in content[tool_index:]):
        return None
    thinking = "".join(str(block["text"]) for block in leading_blocks if isinstance(block, Mapping))
    return [{"type": "thinking", "thinking": thinking}, *deepcopy(content[tool_index:])]


def _deepseek_history_validation_error(message: str) -> AnthropicError:
    return _DeepSeekHistoryValidationError(message=message, status_code=400)


def _deepseek_history_message(message: dict, promote_tool_reasoning_text: bool = False) -> dict:
    transformed_message = deepcopy(message)
    if transformed_message.get("role") != "assistant":
        return transformed_message

    reasoning_content = _nonempty_reasoning_content(message)
    transformed_message.pop("reasoning_content", None)
    transformed_message.pop("provider_specific_fields", None)
    content = transformed_message.get("content")
    if not isinstance(content, list):
        if reasoning_content is None:
            return transformed_message
        text_block = [{"type": "text", "text": content}] if isinstance(content, str) and content else []
        transformed_message["content"] = [{"type": "thinking", "thinking": reasoning_content}, *text_block]
        return transformed_message

    transformed_content, has_tool_use, has_redacted_thinking, has_reasoning = _unsigned_content_blocks(content)
    if has_tool_use and has_redacted_thinking:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history cannot replay redacted thinking")
    if not has_reasoning and reasoning_content is not None:
        transformed_content = [{"type": "thinking", "thinking": reasoning_content}, *transformed_content]
        has_reasoning = True
    if has_tool_use and not has_reasoning and promote_tool_reasoning_text:
        promoted_content = _promoted_tool_reasoning_content(transformed_content)
        if promoted_content is not None:
            transformed_content = promoted_content
    transformed_message["content"] = transformed_content
    return transformed_message


def _deepseek_history(messages: list[dict], promote_tool_reasoning_text: bool = False) -> list[dict]:
    return [
        _deepseek_history_message(message, promote_tool_reasoning_text=promote_tool_reasoning_text)
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


def _chat_message_has_reasoning(message: Mapping[str, object]) -> bool:
    if _nonempty_reasoning_content(message) is not None:
        return True
    return any(
        isinstance(block, Mapping)
        and block.get("type") == "thinking"
        and isinstance(block.get("thinking"), str)
        and bool(block["thinking"].strip())
        for field in ("content", "thinking_blocks")
        for block in _message_blocks(message, field)
    )


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


def deepseek_history_has_reasoningless_tool_use(messages: list[dict]) -> bool:
    return any(
        message.get("role") == "assistant"
        and _chat_message_has_tool_history(message)
        and not _chat_message_has_reasoning(message)
        for message in messages
    )


def _prepare_deepseek_chat_message(message: dict) -> dict:
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
    if not has_reasoning and reasoning_content is not None:
        thinking_blocks.insert(0, {"type": "thinking", "thinking": reasoning_content})
        has_reasoning = True

    has_tool_history = content_has_tool_use or _chat_message_has_tool_history(transformed_message)
    if has_tool_history and has_redacted_thinking:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history cannot replay redacted thinking")

    transformed_message = _without_reasoning_content_fields(transformed_message)
    if thinking_blocks:
        transformed_message["thinking_blocks"] = thinking_blocks
    else:
        transformed_message.pop("thinking_blocks", None)
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


def prepare_deepseek_chat_history(messages: list[dict]) -> list[dict]:
    prepared_messages = [_prepare_deepseek_chat_message(message) for message in messages]
    return [
        _upgrade_legacy_function_result(
            _upgrade_legacy_function_call(message, message_index),
            _legacy_function_result_call_id(prepared_messages, message_index),
        )
        for message_index, message in enumerate(prepared_messages)
    ]


class DeepSeekAnthropicMessagesConfig(AnthropicMessagesConfig):
    """
    DeepSeek exposes an Anthropic-compatible Messages API at
    https://api.deepseek.com/anthropic.

    It accepts the native Anthropic Messages conversation shape, including
    thinking blocks in assistant history, but rejects Anthropic's explicit
    custom-tool discriminator (`{"type": "custom"}`).
    """

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
        configured_path = optional_params.get("_deepseek_anthropic_messages_path")
        messages_path = (
            configured_path
            if isinstance(configured_path, str) and configured_path in _DEEPSEEK_MESSAGES_PATHS
            else None
        )
        return _complete_messages_url(base_url, messages_path)

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
        request_params = dict(anthropic_messages_optional_request_params)
        request_params.pop("_deepseek_anthropic_messages_path", None)
        disable_tool_thinking = litellm_params.get("_deepseek_anthropic_tool_thinking") == "disabled"
        thinking = request_params.get("thinking")
        thinking_enabled = isinstance(thinking, Mapping) and thinking.get("type") == "enabled"
        transformed_messages = _deepseek_history(
            messages,
            promote_tool_reasoning_text=thinking_enabled and not disable_tool_thinking,
        )
        if disable_tool_thinking and bool(request_params.get("tools")):
            request_params["thinking"] = {"type": "disabled"}
        elif thinking_enabled and deepseek_history_has_reasoningless_tool_use(transformed_messages):
            raise _deepseek_history_validation_error(
                "DeepSeek Anthropic thinking tool history requires non-empty reasoning"
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
        content_blocks = content if isinstance(content, list) else []
        has_thinking = any(
            isinstance(block, Mapping)
            and block.get("type") == "thinking"
            and isinstance(block.get("thinking"), str)
            and bool(block["thinking"].strip())
            for block in content_blocks
        )
        if isinstance(reasoning_content, str) and reasoning_content.strip() and not has_thinking:
            response["content"] = [{"type": "thinking", "thinking": reasoning_content}, *content_blocks]
        elif not has_thinking:
            model_call_details = getattr(logging_obj, "model_call_details", None)
            request_thinking = model_call_details.get("thinking") if isinstance(model_call_details, Mapping) else None
            if isinstance(request_thinking, Mapping) and request_thinking.get("type") == "enabled":
                promoted_content = _promoted_tool_reasoning_content(content_blocks)
                if promoted_content is not None:
                    response["content"] = promoted_content
        return response

    @property
    def max_retry_on_anthropic_messages_http_error(self) -> int:
        return 1

    def should_retry_anthropic_messages_on_http_error(self, e: httpx.HTTPStatusError, litellm_params: dict) -> bool:
        return False
