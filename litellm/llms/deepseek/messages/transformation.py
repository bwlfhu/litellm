"""
DeepSeek Anthropic-compatible messages transformation config.
"""

import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Final, Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

import litellm
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.litellm_core_utils.prompt_templates.factory import (
    convert_to_anthropic_tool_invoke,
    convert_to_anthropic_tool_result,
)
from litellm.llms.anthropic.common_utils import AnthropicError, is_anthropic_invalid_thinking_signature_error
from litellm.llms.anthropic.experimental_pass_through.messages.streaming_iterator import (
    aclose_if_supported,
)
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
_DEEPSEEK_INTERNAL_REASONING_FIELDS: Final = frozenset(
    {
        "provider_specific_fields",
        "reasoning",
        "reasoning_content",
        "reasoning_items",
        "signature",
        "thought_signature",
        "thinking",
        "thinking_blocks",
    }
)
_DEEPSEEK_TOOL_USE_BLOCK_FIELDS: Final = ("type", "id", "name", "input")
_DEEPSEEK_RESPONSE_TOOL_USE_BLOCK_FIELDS: Final = (*_DEEPSEEK_TOOL_USE_BLOCK_FIELDS, "caller")
_DEEPSEEK_TOOL_USE_BLOCK_TYPES: Final = frozenset({"server_tool_use", "tool_use"})
_DEEPSEEK_LEGACY_REASONING_MODEL_PREFIXES: Final = ("deepseek-v3", "deepseek-reasoner")
_DEEPSEEK_OUTPUT_EFFORTS: Final = frozenset(("low", "high", "max"))
_DEEPSEEK_ENABLED_THINKING_TYPES: Final = frozenset(("enabled", "adaptive"))


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


def _sanitize_deepseek_content_block(block: object) -> object:
    if not isinstance(block, Mapping):
        return deepcopy(block)
    block_type: Final = block.get("type")
    if block_type in _DEEPSEEK_TOOL_USE_BLOCK_TYPES:
        return {  # mutable-ok: provider content blocks require concrete JSON objects
            key: deepcopy(block[key]) for key in _DEEPSEEK_TOOL_USE_BLOCK_FIELDS if key in block
        }
    return {  # mutable-ok: provider content blocks require concrete JSON objects
        key: _sanitize_deepseek_content_blocks(value)
        if key == "content" and isinstance(value, list)
        else deepcopy(value)
        for key, value in block.items()
        if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS
    }


def _sanitize_deepseek_content_blocks(content: Sequence[object]) -> Sequence[object]:
    return [  # mutable-ok: provider content requires a concrete JSON array
        _sanitize_deepseek_content_block(block)
        for block in content
        if not (isinstance(block, Mapping) and block.get("type") in ("redacted_thinking", "thinking"))
    ]


def _sanitize_deepseek_response_content_block(block: object, thinking_disabled: bool) -> object | None:
    if not isinstance(block, Mapping):
        return deepcopy(block)
    block_type: Final = block.get("type")
    if block_type == "thinking":
        thinking: Final = block.get("thinking")
        signature: Final = block.get("signature")
        return (
            {  # mutable-ok: Anthropic response blocks require concrete JSON objects
                "type": "thinking",
                "thinking": thinking,
                **(
                    {"signature": signature}  # mutable-ok: Anthropic signatures are JSON objects
                    if isinstance(signature, str)
                    else {}  # mutable-ok: optional JSON fields are merged from a concrete object
                ),
            }
            if not thinking_disabled and isinstance(thinking, str) and thinking.strip()
            else None
        )
    if block_type == "redacted_thinking":
        if thinking_disabled:
            return None
        data: Final = block.get("data")
        return (
            {  # mutable-ok: Anthropic response blocks require concrete JSON objects
                "type": "redacted_thinking",
                "data": deepcopy(data),
            }
            if isinstance(data, str) and data
            else {"type": "redacted_thinking"}  # mutable-ok: Anthropic response blocks are JSON objects
        )
    if block_type in _DEEPSEEK_TOOL_USE_BLOCK_TYPES:
        return {  # mutable-ok: Anthropic tool blocks require concrete JSON objects
            key: deepcopy(block[key]) for key in _DEEPSEEK_RESPONSE_TOOL_USE_BLOCK_FIELDS if key in block
        }
    return {  # mutable-ok: Anthropic response blocks require concrete JSON objects
        key: _sanitize_deepseek_response_content_blocks(value, thinking_disabled=thinking_disabled)
        if key == "content" and isinstance(value, list)
        else deepcopy(value)
        for key, value in block.items()
        if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS
    }


def _sanitize_deepseek_response_content_blocks(
    content: Sequence[object], thinking_disabled: bool
) -> list[object]:  # mutable-ok: Anthropic responses expose content as a JSON array
    return [  # mutable-ok: Anthropic responses expose content as a JSON array
        sanitized_block
        for block in content
        if (sanitized_block := _sanitize_deepseek_response_content_block(block, thinking_disabled)) is not None
    ]


def _sanitize_deepseek_stream_payload(payload: object, thinking_disabled: bool) -> object | None:
    if isinstance(payload, list):
        return tuple(
            sanitized_value
            for value in payload
            if (sanitized_value := _sanitize_deepseek_stream_payload(value, thinking_disabled)) is not None
        )
    if not isinstance(payload, Mapping):
        return deepcopy(payload)
    event_type: Final = payload.get("type")
    if event_type in ("thinking", "redacted_thinking") or event_type in _DEEPSEEK_TOOL_USE_BLOCK_TYPES:
        return _sanitize_deepseek_response_content_block(payload, thinking_disabled=thinking_disabled)
    if event_type == "content_block_start":
        content_block: Final = payload.get("content_block")
        if isinstance(content_block, Mapping):
            block_type: Final = content_block.get("type")
            thinking: Final = content_block.get("thinking")
            signature: Final = content_block.get("signature")
            sanitized_block: Final = (
                {"type": "text", "text": ""}  # mutable-ok: SSE content blocks require concrete JSON objects
                if thinking_disabled and block_type in ("thinking", "redacted_thinking")
                else {  # mutable-ok: SSE content blocks require concrete JSON objects
                    "type": "thinking",
                    "thinking": thinking if isinstance(thinking, str) else "",
                    **(
                        {"signature": signature}  # mutable-ok: Anthropic signatures are JSON objects
                        if isinstance(signature, str)
                        else {}  # mutable-ok: optional JSON fields are merged from a concrete object
                    ),
                }
                if block_type == "thinking"
                else _sanitize_deepseek_response_content_block(
                    content_block,
                    thinking_disabled=thinking_disabled,
                )
            )
            return (
                {  # mutable-ok: SSE events require concrete JSON objects
                    **{  # mutable-ok: SSE event fields require concrete JSON objects
                        key: deepcopy(value)
                        for key, value in payload.items()
                        if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS and key != "content_block"
                    },
                    "content_block": sanitized_block,
                }
                if sanitized_block is not None
                else None
            )
    if event_type == "content_block_delta":
        delta: Final = payload.get("delta")
        if isinstance(delta, Mapping) and delta.get("type") in ("thinking_delta", "signature_delta"):
            if thinking_disabled:
                return None
            delta_type: Final = delta.get("type")
            return {  # mutable-ok: SSE deltas require concrete JSON objects
                **{  # mutable-ok: SSE event fields require concrete JSON objects
                    key: deepcopy(value)
                    for key, value in payload.items()
                    if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS and key != "delta"
                },
                "delta": {  # mutable-ok: SSE deltas require concrete JSON objects
                    key: deepcopy(value)
                    for key, value in delta.items()
                    if key == "type"
                    or (delta_type == "thinking_delta" and key == "thinking")
                    or (delta_type == "signature_delta" and key == "signature")
                },
            }
    return {  # mutable-ok: SSE events require concrete JSON objects
        key: _sanitize_deepseek_stream_payload(value, thinking_disabled=thinking_disabled)
        for key, value in payload.items()
        if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS
    }


def _sanitize_deepseek_sse_event(event: bytes, thinking_disabled: bool) -> bytes | None:
    lines: Final = event.splitlines()
    data_lines: Final = tuple(line[5:].lstrip() for line in lines if line.startswith(b"data:"))
    if not data_lines:
        return event
    try:
        payload: Final = json.loads(b"\n".join(data_lines))
    except (TypeError, ValueError):
        return event
    sanitized_payload: Final = _sanitize_deepseek_stream_payload(
        payload,
        thinking_disabled=thinking_disabled,
    )
    if sanitized_payload is None:
        return None
    non_data_lines: Final = tuple(line for line in lines if line and not line.startswith(b"data:"))
    preserved_lines: Final = (
        non_data_lines
        if any(line.startswith(b"event:") for line in non_data_lines)
        else (b"event: message", *non_data_lines)
    )
    sanitized_data: Final = b"data: " + json.dumps(sanitized_payload, separators=(",", ":")).encode()
    return b"\n".join((*preserved_lines, sanitized_data)) + b"\n\n"


def _split_deepseek_sse_events(pending: Sequence[bytes], chunk: bytes) -> tuple[bytes, Sequence[bytes]]:
    events: Final = b"".join((*pending, chunk)).replace(b"\r\n", b"\n").split(b"\n\n")
    return (events[-1] if events else b""), tuple(events[:-1])


async def _sanitize_deepseek_messages_stream(
    completion_stream: AsyncIterator[bytes],
    thinking_disabled: bool,
) -> AsyncIterator[bytes]:
    pending: Final[list[bytes]] = []  # mutable-ok: incremental SSE parsing needs a retained byte buffer
    try:
        async for chunk in completion_stream:
            remainder, events = _split_deepseek_sse_events(pending, chunk)
            pending.clear()
            pending.append(remainder)
            for event in events:
                if (sanitized_event := _sanitize_deepseek_sse_event(event + b"\n\n", thinking_disabled)) is not None:
                    yield sanitized_event
        if pending and pending[0] and (trailing_event := _sanitize_deepseek_sse_event(pending[0], thinking_disabled)):
            yield trailing_event
    finally:
        await aclose_if_supported(completion_stream)


def _without_reasoning_content_fields(message: Mapping[str, object]) -> Mapping[str, object]:
    return {  # mutable-ok: provider messages require concrete JSON objects
        key: deepcopy(value) for key, value in message.items() if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS
    }


def _sanitize_non_assistant_deepseek_message(message: Mapping[str, object]) -> Mapping[str, object]:
    transformed_message: Final = _without_reasoning_content_fields(message)
    content: Final = transformed_message.get("content")
    if not isinstance(content, list):
        return transformed_message
    return {  # mutable-ok: provider messages require concrete JSON objects
        **transformed_message,
        "content": _sanitize_deepseek_content_blocks(content),
    }


def _is_valid_deepseek_tool_call(tool_call: object) -> bool:
    if not isinstance(tool_call, Mapping) or tool_call.get("type", "function") != "function":
        return False
    tool_call_id: Final = tool_call.get("id")
    if tool_call_id is not None and (not isinstance(tool_call_id, str) or not tool_call_id):
        return False
    function: Final = tool_call.get("function")
    if not isinstance(function, Mapping):
        return False
    name: Final = function.get("name")
    arguments: Final = function.get("arguments")
    return isinstance(name, str) and bool(name) and (arguments is None or isinstance(arguments, str))


def _deepseek_tool_call_blocks(tool_calls: Sequence[object], message_index: int) -> Sequence[object]:
    if not all(_is_valid_deepseek_tool_call(tool_call) for tool_call in tool_calls):
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool_calls contain invalid entries")
    normalized_tool_calls: Final = [  # mutable-ok: shared Anthropic converter requires a concrete list
        {  # mutable-ok: shared Anthropic converter requires OpenAI tool-call objects
            "id": tool_call.get("id") or f"legacy_tool_call_{message_index}_{call_index}",
            "type": "function",
            "function": deepcopy(tool_call.get("function")),
        }
        for call_index, tool_call in enumerate(tool_calls)
        if isinstance(tool_call, Mapping)
    ]
    try:
        converted_blocks: Final = convert_to_anthropic_tool_invoke(normalized_tool_calls)
    except Exception as exc:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool_calls contain invalid arguments") from exc
    return [  # mutable-ok: provider content requires a concrete JSON array
        {  # mutable-ok: provider tool_use blocks require concrete JSON objects
            **deepcopy(block),
            "id": normalized_tool_calls[block_index]["id"],
        }
        for block_index, block in enumerate(converted_blocks)
    ]


def _as_deepseek_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[str, object], value)  # cast-ok: provider payload objects have JSON string keys


def _matching_deepseek_tool_call(tool_calls: Sequence[object], tool_use_id: object) -> Mapping[str, object] | None:
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        return None
    matching_tool_calls: Final = tuple(
        typed_tool_call
        for tool_call in tool_calls
        if (typed_tool_call := _as_deepseek_mapping(tool_call)) is not None and typed_tool_call.get("id") == tool_use_id
    )
    if len(matching_tool_calls) != 1 or matching_tool_calls[0].get("type", "function") != "function":
        return None
    return matching_tool_calls[0]


def _deepseek_tool_call_input(tool_call: Mapping[str, object], message_index: int) -> Mapping[str, object] | None:
    matching_blocks: Final = _deepseek_tool_call_blocks((tool_call,), message_index)
    if len(matching_blocks) != 1 or (matching_block := _as_deepseek_mapping(matching_blocks[0])) is None:
        return None
    return _as_deepseek_mapping(matching_block.get("input"))


def _repair_deepseek_tool_use_block(block: object, tool_calls: Sequence[object], message_index: int) -> object:
    typed_block: Final = _as_deepseek_mapping(block)
    if typed_block is None:
        return deepcopy(block)
    if typed_block.get("type") != "tool_use":
        return deepcopy(typed_block)
    missing_fields: Final = frozenset(field for field in ("name", "input") if field not in typed_block)
    if not missing_fields:
        return deepcopy(typed_block)
    matching_tool_call: Final = _matching_deepseek_tool_call(tool_calls, typed_block.get("id"))
    if matching_tool_call is None:
        return deepcopy(typed_block)
    typed_function: Final = _as_deepseek_mapping(matching_tool_call.get("function"))
    matching_name: Final = typed_function.get("name") if typed_function is not None else None
    matching_input: Final = (
        _deepseek_tool_call_input(matching_tool_call, message_index) if "input" in missing_fields else None
    )
    original_items: Final = tuple((key, deepcopy(value)) for key, value in typed_block.items())
    repaired_items: Final = (
        *(
            (("name", matching_name),)
            if "name" in missing_fields and isinstance(matching_name, str) and matching_name.strip()
            else ()
        ),
        *((("input", deepcopy(matching_input)),) if "input" in missing_fields and matching_input is not None else ()),
    )
    return {  # mutable-ok: provider tool_use blocks require concrete JSON objects
        key: value for key, value in (*original_items, *repaired_items)
    }


def _normalize_deepseek_assistant_tool_history(
    message: Mapping[str, object], message_index: int
) -> Mapping[str, object]:
    raw_tool_calls: Final = message.get("tool_calls")
    raw_function_call: Final = message.get("function_call")
    if raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool_calls must be a list")
    if raw_function_call is not None and not isinstance(raw_function_call, Mapping):
        raise _deepseek_history_validation_error("DeepSeek Anthropic function_call must be an object")
    legacy_tool_call: Final = (
        {  # mutable-ok: shared Anthropic converter requires an OpenAI tool-call object
            "id": f"legacy_function_call_{message_index}",
            "type": "function",
            "function": deepcopy(
                dict(raw_function_call)  # mutable-ok: converter requires a concrete function-call object
            ),
        }
        if isinstance(raw_function_call, Mapping)
        else None
    )
    tool_calls: Final = (
        (*raw_tool_calls, legacy_tool_call)
        if isinstance(raw_tool_calls, list) and legacy_tool_call is not None
        else tuple(raw_tool_calls)
        if isinstance(raw_tool_calls, list)
        else (legacy_tool_call,)
        if legacy_tool_call is not None
        else ()
    )
    transformed_message: Final = {  # mutable-ok: provider messages require concrete JSON objects
        key: deepcopy(value) for key, value in message.items() if key not in ("tool_calls", "function_call")
    }
    if not tool_calls:
        return transformed_message
    raw_content: Final = transformed_message.get("content")
    content_blocks: Final = (
        tuple(_repair_deepseek_tool_use_block(block, tool_calls, message_index) for block in raw_content)
        if isinstance(raw_content, list)
        else ({"type": "text", "text": raw_content},)  # mutable-ok: provider text block is a JSON object
        if isinstance(raw_content, str) and raw_content.strip()
        else ()
    )
    existing_tool_ids: Final = frozenset(
        block.get("id")
        for block in content_blocks
        if isinstance(block, Mapping) and block.get("type") in _DEEPSEEK_TOOL_USE_BLOCK_TYPES
    )
    new_tool_calls: Final = tuple(
        tool_call
        for tool_call in tool_calls
        if not (isinstance(tool_call, Mapping) and tool_call.get("id") in existing_tool_ids)
    )
    converted_tool_blocks: Final = _deepseek_tool_call_blocks(new_tool_calls, message_index)
    return {  # mutable-ok: provider messages require concrete JSON objects
        **transformed_message,
        "content": [  # mutable-ok: provider content requires a concrete JSON array
            *content_blocks,
            *converted_tool_blocks,
        ],
    }


def _normalize_deepseek_tool_result(
    message: Mapping[str, object],
    messages: Sequence[Mapping[str, object]],
    message_index: int,
) -> Mapping[str, object]:
    role: Final = message.get("role")
    if role not in ("tool", "function"):
        return deepcopy(message)
    explicit_tool_call_id: Final = message.get("tool_call_id")
    if role == "tool" and (not isinstance(explicit_tool_call_id, str) or not explicit_tool_call_id):
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool result is missing tool_call_id")
    tool_call_id: Final = _legacy_function_result_call_id(messages, message_index)
    if tool_call_id is None:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool result is missing tool_call_id")
    result_message: Final = {  # mutable-ok: shared Anthropic converter requires a concrete tool-result object
        "role": "tool" if role == "tool" else "function",
        "tool_call_id": tool_call_id,
        "content": deepcopy(message.get("content", "")),
    }
    try:
        result_block: Final = convert_to_anthropic_tool_result(result_message)
    except Exception as exc:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool result is invalid") from exc
    return {  # mutable-ok: provider messages require concrete JSON objects
        "role": "user",
        "content": [  # mutable-ok: provider content requires a concrete JSON array
            _sanitize_deepseek_content_block(
                {  # mutable-ok: provider tool_result blocks require concrete JSON objects
                    **result_block,
                    "tool_use_id": tool_call_id,
                }
            )
        ],
    }


def _normalize_deepseek_native_tool_history(
    messages: Sequence[Mapping[str, object]],
) -> Sequence[Mapping[str, object]]:
    return tuple(
        _normalize_deepseek_tool_result(message, messages, message_index)
        if message.get("role") in ("tool", "function")
        else _normalize_deepseek_assistant_tool_history(message, message_index)
        if message.get("role") == "assistant"
        else deepcopy(message)
        for message_index, message in enumerate(messages)
    )


def _unsigned_content_block(block: object) -> tuple[object | None, bool, bool, bool]:
    if not isinstance(block, Mapping):
        return deepcopy(block), False, False, False
    block_type: Final = block.get("type")
    if block_type == "thinking":
        thinking: Final = block.get("thinking")
        return (
            (
                {  # mutable-ok: provider thinking history requires a concrete JSON object
                    "type": "thinking",
                    "thinking": thinking,
                },
                False,
                False,
                True,
            )
            if isinstance(thinking, str) and thinking.strip()
            else (None, False, False, False)
        )
    if block_type == "redacted_thinking":
        return None, False, True, False
    return _sanitize_deepseek_content_block(block), block_type in _DEEPSEEK_TOOL_USE_BLOCK_TYPES, False, False


def _unsigned_content_blocks(content: list[object]) -> tuple[list[object], bool, bool, bool]:
    block_results: Final = tuple(_unsigned_content_block(block) for block in content)
    return (
        [  # mutable-ok: provider content history requires a concrete JSON array
            sanitized for sanitized, _, _, _ in block_results if sanitized is not None
        ],
        any(has_tool_use for _, has_tool_use, _, _ in block_results),
        any(has_redacted_thinking for _, _, has_redacted_thinking, _ in block_results),
        any(has_reasoning for _, _, _, has_reasoning in block_results),
    )


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


def _content_blocks(content: Sequence[object], depth: int = 0) -> tuple[tuple[object, int], ...]:
    return tuple(nested_block for block in content for nested_block in _content_block_tree(block, depth=depth))


def _validate_deepseek_content_blocks(messages: Sequence[Mapping[str, object]], model: str | None = None) -> None:
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


def _invalid_deepseek_tool_use_field(block: object) -> str | None:
    typed_block: Final = _as_deepseek_mapping(block)
    if typed_block is None:
        return None
    if typed_block.get("type") not in _DEEPSEEK_TOOL_USE_BLOCK_TYPES:
        return None
    tool_use_id: Final = typed_block.get("id")
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        return "id"
    name: Final = typed_block.get("name")
    if not isinstance(name, str) or not name.strip():
        return "name"
    return None if isinstance(typed_block.get("input"), Mapping) else "input"


def _validate_deepseek_tool_use_blocks(messages: Sequence[Mapping[str, object]]) -> None:
    invalid_fields: Final = tuple(
        invalid_field
        for message in messages
        for field in ("content", "thinking_blocks")
        for block, _ in _content_blocks(_message_blocks(message, field))
        if (invalid_field := _invalid_deepseek_tool_use_field(block)) is not None
    )
    if invalid_fields:
        raise _deepseek_history_validation_error(
            f"DeepSeek Anthropic tool_use block is missing or has invalid {invalid_fields[0]}"
        )


def _sidecar_reasoning_blocks(message: Mapping[str, object]) -> tuple[Sequence[object], bool, bool]:
    raw_thinking_blocks: Final = message.get("thinking_blocks")
    if not isinstance(raw_thinking_blocks, list):
        return (), False, False
    transformed_blocks, _, has_redacted_thinking, has_reasoning = _unsigned_content_blocks(raw_thinking_blocks)
    reasoning_blocks: Final = tuple(
        block
        for block in transformed_blocks
        if isinstance(block, Mapping) and block.get("type") in {"thinking", "redacted_thinking"}
    )
    return reasoning_blocks, has_redacted_thinking, has_reasoning


def _deepseek_history_message(
    message: Mapping[str, object],
    require_reasoning: bool,
    missing_reasoning: Literal["placeholder"] | None,
) -> dict:
    transformed_message: Final = deepcopy(
        dict(message)  # mutable-ok: history normalization requires a private working copy
    )
    if transformed_message.get("role") != "assistant":
        return _sanitize_non_assistant_deepseek_message(transformed_message)

    reasoning_content: Final = _nonempty_reasoning_content(message)
    for field in _DEEPSEEK_INTERNAL_REASONING_FIELDS:
        transformed_message.pop(field, None)
    transformed_message.pop("provider_specific_fields", None)
    transformed_message.pop("thinking_blocks", None)
    sidecar_blocks, sidecar_has_redacted_thinking, sidecar_has_reasoning = _sidecar_reasoning_blocks(message)
    content: Final = transformed_message.get("content")
    if not isinstance(content, list):
        scalar_has_tool_history: Final = _chat_message_has_tool_history(message)
        reasoning_blocks: Final = (
            [{"type": "thinking", "thinking": reasoning_content}]
            if require_reasoning and reasoning_content is not None
            else sidecar_blocks
            if require_reasoning and sidecar_has_reasoning
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
        if sidecar_has_redacted_thinking and not scalar_content and not scalar_has_tool_history:
            raise _deepseek_history_validation_error("DeepSeek Anthropic history cannot replay redacted-only thinking")
        if not scalar_content and not scalar_has_tool_history:
            raise _deepseek_history_validation_error("DeepSeek Anthropic history has no replayable assistant content")
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
        content_without_inline_reasoning
        if not require_reasoning
        else [  # mutable-ok: provider content requires a concrete JSON array
            {  # mutable-ok: provider thinking block is a JSON object
                "type": "thinking",
                "thinking": reasoning_content,
            },
            *content_without_inline_reasoning,
        ]
        if reasoning_content is not None
        else transformed_content
        if inline_has_reasoning
        else [*sidecar_blocks, *transformed_content]
        if sidecar_has_reasoning
        else transformed_content
    )
    has_reasoning: Final = require_reasoning and (
        inline_has_reasoning or sidecar_has_reasoning or reasoning_content is not None
    )
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
    if has_redacted_thinking and not final_content and not has_tool_history:
        raise _deepseek_history_validation_error("DeepSeek Anthropic history cannot replay redacted-only thinking")
    if not final_content and not has_tool_history:
        raise _deepseek_history_validation_error("DeepSeek Anthropic history has no replayable assistant content")
    transformed_message["content"] = final_content
    return transformed_message


def _deepseek_history(
    messages: Sequence[Mapping[str, object]],
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


def _message_blocks(message: Mapping[str, object], field: str) -> Sequence[object]:
    value = message.get(field)
    return value if isinstance(value, list) else ()


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


def deepseek_messages_have_tool_history(messages: Iterable[Mapping[str, object]]) -> bool:
    return any(_chat_message_has_tool_history(message) for message in messages)


def _prepare_deepseek_chat_message(
    message: dict,
    require_reasoning: bool,
    missing_reasoning: Literal["placeholder"] | None,
) -> dict:
    transformed_message = deepcopy(message)
    if transformed_message.get("role") != "assistant":
        return _sanitize_non_assistant_deepseek_message(transformed_message)

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
    if require_reasoning and reasoning_content is not None:
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

    content_for_request: Final = transformed_message.get("content")
    if not require_reasoning and isinstance(content_for_request, list):
        transformed_message["content"] = [  # mutable-ok: provider content requires a concrete JSON array
            block
            for block in content_for_request
            if not (isinstance(block, Mapping) and block.get("type") == "thinking")
        ]
    thinking_blocks_for_request: Final = thinking_blocks if require_reasoning else ()
    has_reasoning_for_request: Final = require_reasoning and has_reasoning

    has_tool_history = content_has_tool_use or _chat_message_has_tool_history(transformed_message)
    if has_tool_history and has_redacted_thinking and require_reasoning and not has_reasoning_for_request:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history cannot replay redacted thinking")
    use_placeholder: Final = (
        has_tool_history and require_reasoning and not has_reasoning_for_request and missing_reasoning == "placeholder"
    )
    if has_tool_history and require_reasoning and not has_reasoning_for_request and not use_placeholder:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history requires non-empty reasoning")
    final_thinking_blocks: Final = (
        [  # mutable-ok: provider thinking history requires a concrete JSON array
            {"type": "thinking", "thinking": " "},  # mutable-ok: provider thinking block is a JSON object
            *thinking_blocks_for_request,
        ]
        if use_placeholder
        else thinking_blocks_for_request
    )

    transformed_message = _without_reasoning_content_fields(transformed_message)
    if final_thinking_blocks:
        transformed_message["thinking_blocks"] = final_thinking_blocks
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
    if has_redacted_thinking and not has_visible_content and not final_thinking_blocks and not has_tool_history:
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


def _matching_legacy_function_result_call_id(
    message: Mapping[str, object], result_name: object, message_index: int
) -> str | None:
    function_call: Final = message.get("function_call")
    if isinstance(function_call, Mapping):
        function_name: Final = function_call.get("name")
        if not isinstance(result_name, str) or function_name == result_name:
            return _legacy_function_call_id(message_index)
    tool_calls: Final = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    return next(
        (
            tool_id
            for tool_call in tool_calls
            if isinstance(tool_call, Mapping)
            for function in (tool_call.get("function"),)
            if isinstance(function, Mapping)
            if not isinstance(result_name, str) or function.get("name") == result_name
            for tool_id in (tool_call.get("id"),)
            if isinstance(tool_id, str) and tool_id
        ),
        None,
    )


def _legacy_function_result_call_id(messages: Sequence[Mapping[str, object]], message_index: int) -> str | None:
    tool_call_id = messages[message_index].get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return tool_call_id
    result_name: Final = messages[message_index].get("name")
    for prior_index, prior_message in ((index, messages[index]) for index in range(message_index - 1, -1, -1)):
        if (
            prior_message.get("role") == "assistant"
            and (matched_id := _matching_legacy_function_result_call_id(prior_message, result_name, prior_index))
            is not None
        ):
            return matched_id
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


def _normalize_deepseek_reasoning_effort(effort: object) -> str | None:
    if not isinstance(effort, str):
        return None
    match effort:
        case "minimal":
            return "low"
        case "medium" | "xhigh":
            return "high"
        case _:
            return effort


def _without_adaptive_reasoning_params(request_params: Mapping[str, object]) -> dict:
    raw_reasoning_effort: Final = request_params.get("reasoning_effort")
    normalized_reasoning_effort: Final = _normalize_deepseek_reasoning_effort(raw_reasoning_effort)
    without_reasoning_effort: Final = {
        key: deepcopy(value) for key, value in request_params.items() if key != "reasoning_effort"
    }
    raw_output_config: Final = without_reasoning_effort.get("output_config")
    raw_effort: Final = raw_output_config.get("effort") if isinstance(raw_output_config, Mapping) else None
    normalized_effort: Final = _normalize_deepseek_reasoning_effort(raw_effort)
    output_config_from_request: Final = (
        {"effort": normalized_effort} if normalized_effort in _DEEPSEEK_OUTPUT_EFFORTS else None
    )
    output_config_from_reasoning_effort: Final = (
        {"effort": normalized_reasoning_effort}
        if normalized_reasoning_effort in _DEEPSEEK_OUTPUT_EFFORTS and output_config_from_request is None
        else None
    )
    output_config: Final = output_config_from_request or output_config_from_reasoning_effort
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
        if normalized_reasoning_effort == "none":
            return {
                **without_output_config,
                "thinking": {"type": "disabled"},
            }
        if normalized_reasoning_effort in _DEEPSEEK_OUTPUT_EFFORTS:
            return {  # mutable-ok: provider request parameters require concrete JSON objects
                **normalized_output_config,
                "thinking": {"type": "enabled"},
            }
        return normalized_output_config
    thinking_type: Final = raw_thinking.get("type")
    if thinking_type == "disabled":
        return {
            **without_output_config,
            "thinking": {"type": "disabled"},
        }
    if thinking_type in _DEEPSEEK_ENABLED_THINKING_TYPES:
        return {  # mutable-ok: provider request parameters require concrete JSON objects
            **normalized_output_config,
            "thinking": {"type": "enabled"},
        }
    return normalized_output_config


def default_deepseek_anthropic_thinking_to_enabled(
    request_params: Mapping[str, object],
    model: str,
) -> Mapping[str, object]:
    if request_params.get("thinking") is not None:
        return request_params
    normalized_model: Final = model.rsplit("/", maxsplit=1)[-1].lower()
    if normalized_model.startswith(_DEEPSEEK_LEGACY_REASONING_MODEL_PREFIXES):
        return request_params
    return {  # mutable-ok: provider request requires concrete JSON containers
        **request_params,
        "thinking": {"type": "enabled"},
    }


def omit_false_stream_for_deepseek_anthropic(
    request_params: Mapping[str, object],
) -> dict[str, object]:
    return {key: value for key, value in request_params.items() if key != "stream" or value is not False}


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
        normalized_messages: Final = _normalize_deepseek_native_tool_history(messages)
        _validate_deepseek_content_blocks(normalized_messages, model=model)
        normalized_reasoning_params: Final = _without_adaptive_reasoning_params(
            anthropic_messages_optional_request_params,
        )
        normalized_tool_choice: Final = _normalized_tool_choice(normalized_reasoning_params.get("tool_choice"))
        request_params_with_tool_choice: Final = {
            **{key: value for key, value in normalized_reasoning_params.items() if key != "tool_choice"},
            **({"tool_choice": normalized_tool_choice} if normalized_tool_choice is not None else {}),
        }
        request_params_with_thinking_default: Final = omit_false_stream_for_deepseek_anthropic(
            default_deepseek_anthropic_thinking_to_enabled(
                request_params_with_tool_choice,
                model=model,
            )
        )
        request_params: Final = request_params_with_thinking_default
        thinking: Final = request_params.get("thinking")
        require_reasoning: Final = isinstance(thinking, Mapping) and thinking.get("type") == "enabled"
        transformed_messages: Final = _deepseek_history(
            normalized_messages,
            require_reasoning=require_reasoning,
            missing_reasoning=self._missing_reasoning or "placeholder",
        )
        _validate_deepseek_tool_use_blocks(transformed_messages)
        anthropic_messages_request: Final = super().transform_anthropic_messages_request(
            model=model,
            messages=transformed_messages,
            anthropic_messages_optional_request_params=request_params,
            litellm_params=litellm_params,
            headers=headers,
        )
        normalized_anthropic_messages_request: Final = omit_false_stream_for_deepseek_anthropic(
            anthropic_messages_request
        )
        if "tools" not in normalized_anthropic_messages_request:
            return normalized_anthropic_messages_request
        return {
            **normalized_anthropic_messages_request,
            "tools": self._sanitize_tools_for_deepseek(normalized_anthropic_messages_request["tools"]),
        }

    def get_async_streaming_response_iterator(
        self,
        model: str,
        httpx_response: httpx.Response,
        request_body: dict,
        litellm_logging_obj: LiteLLMLoggingObj,
    ) -> AsyncIterator[bytes]:
        completion_stream: Final[AsyncIterator[bytes]] = super().get_async_streaming_response_iterator(
            model=model,
            httpx_response=httpx_response,
            request_body=request_body,
            litellm_logging_obj=litellm_logging_obj,
        )
        thinking: Final[object] = request_body.get("thinking")
        thinking_disabled: Final = isinstance(thinking, Mapping) and thinking.get("type") == "disabled"
        return _sanitize_deepseek_messages_stream(completion_stream, thinking_disabled=thinking_disabled)

    def transform_anthropic_messages_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> AnthropicMessagesResponse:
        raw_response_data: Final[Mapping[str, object]] = super().transform_anthropic_messages_response(
            model=model,
            raw_response=raw_response,
            logging_obj=logging_obj,
        )
        model_call_details: Final = getattr(logging_obj, "model_call_details", None)
        request_thinking: Final = (
            model_call_details.get("thinking") if isinstance(model_call_details, Mapping) else None
        )
        thinking_enabled: Final = isinstance(request_thinking, Mapping) and request_thinking.get("type") == "enabled"
        thinking_disabled: Final = isinstance(request_thinking, Mapping) and request_thinking.get("type") == "disabled"
        reasoning_content: Final = raw_response_data.get("reasoning_content")
        provider_specific_fields: Final = raw_response_data.get("provider_specific_fields")
        provider_reasoning_content: Final = (
            provider_specific_fields.get("reasoning_content") if isinstance(provider_specific_fields, Mapping) else None
        )
        normalized_reasoning_content: Final = (
            reasoning_content
            if isinstance(reasoning_content, str) and reasoning_content.strip()
            else provider_reasoning_content
        )
        response: Final = {
            key: value
            for key, value in raw_response_data.items()
            if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS and key != "provider_specific_fields"
        }
        content: Final = response.get("content")
        content_blocks: Final = (
            _sanitize_deepseek_response_content_blocks(content, thinking_disabled=thinking_disabled)
            if isinstance(content, list)
            else ()
        )
        has_thinking: Final = any(
            isinstance(block, Mapping)
            and block.get("type") == "thinking"
            and isinstance(block.get("thinking"), str)
            and bool(block.get("thinking", "").strip())
            for block in content_blocks
        )
        if (
            thinking_enabled
            and isinstance(normalized_reasoning_content, str)
            and normalized_reasoning_content.strip()
            and not has_thinking
        ):
            response["content"] = [
                {"type": "thinking", "thinking": normalized_reasoning_content},
                *content_blocks,
            ]
        elif isinstance(content, list) and content_blocks != content:
            response["content"] = content_blocks
        return cast(  # cast-ok: sanitized provider JSON preserves the Anthropic response schema
            AnthropicMessagesResponse,
            response,
        )

    @property
    def max_retry_on_anthropic_messages_http_error(self) -> int:
        return 1

    def should_retry_anthropic_messages_on_http_error(self, e: httpx.HTTPStatusError, litellm_params: dict) -> bool:
        return False

    def should_disable_anthropic_messages_fallbacks_on_http_error(self, e: httpx.HTTPStatusError) -> bool:
        return e.response.status_code == 400 and is_anthropic_invalid_thinking_signature_error(e.response.text)
