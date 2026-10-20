"""
DeepSeek Anthropic-compatible messages transformation config.
"""

import json
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from itertools import chain, groupby
from typing import (
    Final,
    Literal,
    cast,  # noqa: TID251  # shared Anthropic converters require SDK types after explicit history validation
)
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import TypeAdapter

import litellm
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.litellm_core_utils.prompt_templates.factory import (
    convert_to_anthropic_tool_invoke,
    convert_to_anthropic_tool_result,
)
from litellm.litellm_core_utils.prompt_templates.mid_conversation_system import message_field
from litellm.llms.anthropic.common_utils import AnthropicError
from litellm.llms.anthropic.pass_through.messages.streaming_iterator import (
    aclose_if_supported,
)
from litellm.llms.anthropic.pass_through.messages.transformation import (
    AnthropicMessagesConfig,
)
from litellm.llms.deepseek.common_utils import deepseek_mapping, deepseek_sequence, warn_missing_reasoning_placeholders
from litellm.router_protocol import DeepSeekMissingReasoning
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.anthropic_messages.anthropic_response import AnthropicMessagesResponse
from litellm.types.llms.openai import (
    ChatCompletionAssistantToolCall,
    ChatCompletionFunctionMessage,
    ChatCompletionToolMessage,
)
from litellm.types.router import GenericLiteLLMParams

_DEEPSEEK_MAPPING_ADAPTER: Final = TypeAdapter(Mapping[str, object])
_DEEPSEEK_SEQUENCE_ADAPTER: Final = TypeAdapter(Sequence[object])

_DEEPSEEK_UNSUPPORTED_CONTENT_BLOCK_TYPES: Final = frozenset(
    {
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


def _is_invalid_thinking_signature_error(error_text: str) -> bool:
    lower: Final = error_text.lower()
    return "thinking" in lower and "signature" in lower and ("invalid" in lower or "valid string" in lower)


class _DeepSeekHistoryValidationError(AnthropicError):
    pass


def _deduplicated_path_segments(path: str) -> tuple[str, ...]:
    segments: Final = tuple(segment for segment in path.split("/") if segment)
    return tuple(
        segment
        for index, segment in enumerate(segments)
        if index == 0 or segment not in {"anthropic", "v1"} or segment != segments[index - 1]
    )


def _without_trailing_segments(segments: tuple[str, ...], values: frozenset[str]) -> tuple[str, ...]:
    last_retained: Final = next(
        (index + 1 for index in range(len(segments) - 1, -1, -1) if segments[index] not in values), 0
    )
    return segments[:last_retained]


def _without_known_messages_suffix(segments: tuple[str, ...]) -> tuple[str, ...]:
    suffixes: Final = (
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
    parsed: Final = urlsplit(base_url)
    path: Final = "/" + "/".join(path_segments)
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _complete_messages_url(base_url: str, messages_path: str | None) -> str:
    parsed: Final = urlsplit(base_url)
    base_segments: Final = _deduplicated_path_segments(parsed.path)
    if messages_path == "anthropic/v1/messages":
        anthropic_prefix: Final = _without_known_messages_suffix(base_segments)
        return _url_with_path(base_url, anthropic_prefix + ("anthropic", "v1", "messages"))
    if messages_path == "v1/messages":
        messages_prefix: Final = _without_known_messages_suffix(base_segments)
        return _url_with_path(base_url, messages_prefix + ("v1", "messages"))
    if parsed.path.rstrip("/").endswith("/v1/messages"):
        return base_url

    prefix: Final = _without_trailing_segments(base_segments, frozenset({"v1", "beta"}))
    normalized: Final = prefix if prefix[-1:] == ("anthropic",) else prefix + ("anthropic",)
    return _url_with_path(base_url, normalized + ("v1", "messages"))


def _nonempty_reasoning_content(message: Mapping[str, object]) -> str | None:
    reasoning_content: Final = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        return reasoning_content
    provider_specific_fields: Final = deepseek_mapping(message.get("provider_specific_fields"))
    if provider_specific_fields is not None:
        provider_reasoning_content: Final = provider_specific_fields.get("reasoning_content")
        if isinstance(provider_reasoning_content, str) and provider_reasoning_content.strip():
            return provider_reasoning_content
    return None


def _sanitize_deepseek_content_block(raw_block: object) -> object:
    if not isinstance(raw_block, Mapping):
        return deepcopy(raw_block)
    block: Final = _DEEPSEEK_MAPPING_ADAPTER.validate_python(raw_block)
    block_type: Final = block.get("type")
    if block_type in _DEEPSEEK_TOOL_USE_BLOCK_TYPES:
        return {key: deepcopy(block[key]) for key in _DEEPSEEK_TOOL_USE_BLOCK_FIELDS if key in block}
    return {
        key: _sanitize_deepseek_content_blocks(value)
        if key == "content" and isinstance(value, list)
        else deepcopy(value)
        for key, value in block.items()
        if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS
    }


def _is_nonempty_deepseek_thinking_block(block: object) -> bool:
    fields: Final = deepseek_mapping(block)
    if fields is None or fields.get("type") != "thinking":
        return False
    thinking: Final = fields.get("thinking")
    return isinstance(thinking, str) and bool(thinking.strip())


def _sanitize_deepseek_content_blocks(content: Sequence[object]) -> Sequence[object]:
    return [
        _sanitize_deepseek_content_block(block)
        for block in content
        if not (
            (fields := deepseek_mapping(block)) is not None and fields.get("type") in ("redacted_thinking", "thinking")
        )
    ]


def _sanitize_deepseek_response_content_block(raw_block: object, thinking_disabled: bool) -> object | None:
    if not isinstance(raw_block, Mapping):
        return deepcopy(raw_block)
    block: Final = _DEEPSEEK_MAPPING_ADAPTER.validate_python(raw_block)
    block_type: Final = block.get("type")
    if block_type == "thinking":
        thinking: Final = block.get("thinking")
        signature: Final = block.get("signature")
        return (
            {
                "type": "thinking",
                "thinking": thinking,
                **({"signature": signature} if isinstance(signature, str) else {}),
            }
            if not thinking_disabled and isinstance(thinking, str) and thinking.strip()
            else None
        )
    if block_type == "redacted_thinking":
        if thinking_disabled:
            return None
        data: Final = block.get("data")
        return (
            {
                "type": "redacted_thinking",
                "data": deepcopy(data),
            }
            if isinstance(data, str) and data
            else {"type": "redacted_thinking"}
        )
    if block_type in _DEEPSEEK_TOOL_USE_BLOCK_TYPES:
        return {key: deepcopy(block[key]) for key in _DEEPSEEK_RESPONSE_TOOL_USE_BLOCK_FIELDS if key in block}
    return {
        key: _sanitize_deepseek_response_content_blocks(value, thinking_disabled=thinking_disabled)
        if key == "content" and isinstance(value, list)
        else deepcopy(value)
        for key, value in block.items()
        if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS
    }


def _sanitize_deepseek_response_content_blocks(
    content: Sequence[object], thinking_disabled: bool
) -> list[object]:  # mutable-ok: Anthropic responses expose content as a JSON array
    return [
        sanitized_block
        for block in content
        if (sanitized_block := _sanitize_deepseek_response_content_block(block, thinking_disabled)) is not None
    ]


def _sanitize_deepseek_stream_payload(raw_payload: object, thinking_disabled: bool) -> object | None:
    if isinstance(raw_payload, list):
        return tuple(
            sanitized_value
            for value in _DEEPSEEK_SEQUENCE_ADAPTER.validate_python(raw_payload)
            if (sanitized_value := _sanitize_deepseek_stream_payload(value, thinking_disabled)) is not None
        )
    if not isinstance(raw_payload, Mapping):
        return deepcopy(raw_payload)
    payload: Final = _DEEPSEEK_MAPPING_ADAPTER.validate_python(raw_payload)
    event_type: Final = payload.get("type")
    if event_type in ("thinking", "redacted_thinking") or event_type in _DEEPSEEK_TOOL_USE_BLOCK_TYPES:
        return _sanitize_deepseek_response_content_block(payload, thinking_disabled=thinking_disabled)
    if event_type == "content_block_start":
        content_block: Final = deepseek_mapping(payload.get("content_block"))
        if content_block is not None:
            block_type: Final = content_block.get("type")
            thinking: Final = content_block.get("thinking")
            signature: Final = content_block.get("signature")
            sanitized_block: Final = (
                {"type": "text", "text": ""}
                if thinking_disabled and block_type in ("thinking", "redacted_thinking")
                else {
                    "type": "thinking",
                    "thinking": thinking if isinstance(thinking, str) else "",
                    **({"signature": signature} if isinstance(signature, str) else {}),
                }
                if block_type == "thinking"
                else _sanitize_deepseek_response_content_block(
                    content_block,
                    thinking_disabled=thinking_disabled,
                )
            )
            return (
                {
                    **{
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
        delta: Final = deepseek_mapping(payload.get("delta"))
        if delta is not None and delta.get("type") in ("thinking_delta", "signature_delta"):
            if thinking_disabled:
                return None
            delta_type: Final = delta.get("type")
            return {
                **{
                    key: deepcopy(value)
                    for key, value in payload.items()
                    if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS and key != "delta"
                },
                "delta": {
                    key: deepcopy(value)
                    for key, value in delta.items()
                    if key == "type"
                    or (delta_type == "thinking_delta" and key == "thinking")
                    or (delta_type == "signature_delta" and key == "signature")
                },
            }
    return {
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
        payload: Final[object] = json.loads(b"\n".join(data_lines))
    except (TypeError, ValueError):
        return event
    sanitized_payload: Final = _sanitize_deepseek_stream_payload(
        payload,
        thinking_disabled=thinking_disabled,
    )
    if sanitized_payload is None:
        return None
    non_data_lines: Final = tuple(
        line for line in lines if line and not line.startswith(b"data:") and not line.startswith(b"event:")
    )
    payload_object: Final = _as_deepseek_mapping(sanitized_payload)
    event_type: Final = payload_object.get("type", "message") if payload_object is not None else "message"
    preserved_lines: Final = (f"event: {event_type}".encode(), *non_data_lines)
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
    return {key: deepcopy(value) for key, value in message.items() if key not in _DEEPSEEK_INTERNAL_REASONING_FIELDS}


def _sanitize_non_assistant_deepseek_message(message: Mapping[str, object]) -> Mapping[str, object]:
    transformed_message: Final = _without_reasoning_content_fields(message)
    content: Final = transformed_message.get("content")
    if not isinstance(content, list):
        return transformed_message
    return {
        **transformed_message,
        "content": _sanitize_deepseek_content_blocks(content),
    }


def _is_valid_deepseek_tool_call(tool_call: object) -> bool:
    fields: Final = deepseek_mapping(tool_call)
    if fields is None or fields.get("type", "function") != "function":
        return False
    tool_call_id: Final = fields.get("id")
    if tool_call_id is not None and (not isinstance(tool_call_id, str) or not tool_call_id):
        return False
    function: Final = deepseek_mapping(fields.get("function"))
    if not function is not None:
        return False
    name: Final = function.get("name")
    arguments: Final = function.get("arguments")
    return isinstance(name, str) and bool(name) and (arguments is None or isinstance(arguments, str))


def _deepseek_tool_call_blocks(tool_calls: Sequence[object], message_index: int) -> Sequence[object]:
    if not all(_is_valid_deepseek_tool_call(tool_call) for tool_call in tool_calls):
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool_calls contain invalid entries")
    normalized_tool_calls: Final = [
        {
            "id": fields.get("id") or f"legacy_tool_call_{message_index}_{call_index}",
            "type": "function",
            "function": deepcopy(fields.get("function")),
        }
        for call_index, tool_call in enumerate(tool_calls)
        if (fields := deepseek_mapping(tool_call)) is not None
    ]
    try:
        converted_blocks: Final = convert_to_anthropic_tool_invoke(
            cast(  # cast-ok: tool call roles, names, IDs and argument types are validated above
                list[ChatCompletionAssistantToolCall],
                normalized_tool_calls,
            )
        )
    except Exception as exc:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool_calls contain invalid arguments") from exc
    return [
        {
            **deepcopy(block),
            "id": normalized_tool_calls[block_index]["id"],
        }
        for block_index, block in enumerate(converted_blocks)
    ]


def _as_deepseek_mapping(raw_value: object) -> Mapping[str, object] | None:
    if not isinstance(raw_value, Mapping):
        return None
    return deepseek_mapping(raw_value)


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
    typed_function: Final = deepseek_mapping(_as_deepseek_mapping(matching_tool_call.get("function")))
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
    return {key: value for key, value in (*original_items, *repaired_items)}


def _normalize_deepseek_assistant_tool_history(
    message: Mapping[str, object], message_index: int
) -> Mapping[str, object]:
    raw_tool_calls: Final = message.get("tool_calls")
    raw_function_call: Final[object] = message.get("function_call")
    if raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool_calls must be a list")
    if raw_function_call is not None and not isinstance(raw_function_call, Mapping):
        raise _deepseek_history_validation_error("DeepSeek Anthropic function_call must be an object")
    function_call_fields: Final = deepseek_mapping(raw_function_call)
    legacy_tool_call: Final = (
        {
            "id": f"legacy_function_call_{message_index}",
            "type": "function",
            "function": deepcopy(dict(function_call_fields)),
        }
        if function_call_fields is not None
        else None
    )
    tool_calls: Final = (
        (*deepseek_sequence(raw_tool_calls), legacy_tool_call)
        if isinstance(raw_tool_calls, list) and legacy_tool_call is not None
        else tuple(deepseek_sequence(raw_tool_calls))
        if isinstance(raw_tool_calls, list)
        else (legacy_tool_call,)
        if legacy_tool_call is not None
        else ()
    )
    transformed_message: Final = {
        key: deepcopy(value) for key, value in message.items() if key not in ("tool_calls", "function_call")
    }
    if not tool_calls:
        return transformed_message
    raw_content: Final = transformed_message.get("content")
    content_blocks: Final = (
        tuple(
            _repair_deepseek_tool_use_block(block, tool_calls, message_index)
            for block in deepseek_sequence(raw_content)
        )
        if isinstance(raw_content, list)
        else ({"type": "text", "text": raw_content},)
        if isinstance(raw_content, str) and raw_content.strip()
        else ()
    )
    existing_tool_ids: Final = frozenset(
        fields.get("id")
        for block in content_blocks
        if (fields := deepseek_mapping(block)) is not None and fields.get("type") in _DEEPSEEK_TOOL_USE_BLOCK_TYPES
    )
    new_tool_calls: Final = tuple(
        tool_call
        for tool_call in tool_calls
        if not ((fields := deepseek_mapping(tool_call)) is not None and fields.get("id") in existing_tool_ids)
    )
    converted_tool_blocks: Final = _deepseek_tool_call_blocks(new_tool_calls, message_index)
    return {
        **transformed_message,
        "content": [
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
    result_message: Final = {
        "role": "tool" if role == "tool" else "function",
        "tool_call_id": tool_call_id,
        "content": deepcopy(message.get("content", "")),
    }
    try:
        result_block: Final = convert_to_anthropic_tool_result(
            cast(  # cast-ok: role and call ID are normalized before shared content validation
                ChatCompletionToolMessage | ChatCompletionFunctionMessage,
                result_message,
            )
        )
    except Exception as exc:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool result is invalid") from exc
    return {
        "role": "user",
        "content": [
            _sanitize_deepseek_content_block(
                {
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


def _is_deepseek_tool_result_message(message: Mapping[str, object]) -> bool:
    content: Final = _message_blocks(message, "content")
    return (
        message.get("role") == "user"
        and bool(content)
        and all(
            (typed_block := _as_deepseek_mapping(block)) is not None and typed_block.get("type") == "tool_result"
            for block in content
        )
    )


def _merged_deepseek_tool_result_group(messages: Iterable[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    group: Final = tuple(messages)
    if len(group) < 2:
        return group
    return (
        {
            **group[0],
            "content": list(chain.from_iterable(_message_blocks(message, "content") for message in group)),
        },
    )


def _merge_deepseek_tool_result_messages(
    messages: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        chain.from_iterable(
            _merged_deepseek_tool_result_group(group) if is_result else tuple(group)
            for is_result, group in groupby(messages, _is_deepseek_tool_result_message)
        )
    )


def _deepseek_tool_history_ids(
    message: Mapping[str, object], block_type: Literal["tool_use", "tool_result"], message_index: int
) -> tuple[str, ...]:
    field: Final = "id" if block_type == "tool_use" else "tool_use_id"
    raw_ids: Final = tuple(
        typed_block.get(field)
        for block in _message_blocks(message, "content")
        if (typed_block := _as_deepseek_mapping(block)) is not None and typed_block.get("type") == block_type
    )
    if any(not isinstance(call_id, str) or not call_id.strip() for call_id in raw_ids):
        raise _deepseek_history_validation_error(
            f"DeepSeek Anthropic messages[{message_index}] {block_type} has invalid {field}"
        )
    call_ids: Final = tuple(call_id for call_id in raw_ids if isinstance(call_id, str))
    if len(call_ids) != len(frozenset(call_ids)):
        raise _deepseek_history_validation_error(
            f"DeepSeek Anthropic messages[{message_index}] contains duplicate {block_type} ids"
        )
    return call_ids


def _validate_deepseek_tool_history(messages: Sequence[Mapping[str, object]]) -> None:
    for index, message in enumerate(messages):
        _validate_deepseek_tool_history_message(messages, message, index)


def _validate_deepseek_tool_history_message(
    messages: Sequence[Mapping[str, object]], message: Mapping[str, object], index: int
) -> None:
    calls: Final = _deepseek_tool_history_ids(message, "tool_use", index)
    results: Final = _deepseek_tool_history_ids(message, "tool_result", index)
    if calls and message.get("role") != "assistant":
        raise _deepseek_history_validation_error(
            f"DeepSeek Anthropic messages[{index}] tool_use requires assistant role"
        )
    following: Final = messages[index + 1] if index + 1 < len(messages) else None
    following_results: Final = frozenset(
        _deepseek_tool_history_ids(following, "tool_result", index + 1)
        if following is not None and following.get("role") == "user"
        else ()
    )
    missing: Final = tuple(call_id for call_id in calls if call_id not in following_results)
    if missing:
        raise _deepseek_history_validation_error(
            f"DeepSeek Anthropic messages[{index}] tool_use ids require tool_result blocks in the immediately "
            f"following user message: {', '.join(missing)}"
        )
    if not results:
        return
    previous: Final = messages[index - 1] if index > 0 else None
    previous_calls: Final = frozenset(
        _deepseek_tool_history_ids(previous, "tool_use", index - 1)
        if previous is not None and previous.get("role") == "assistant"
        else ()
    )
    unmatched: Final = tuple(call_id for call_id in results if call_id not in previous_calls)
    if message.get("role") != "user" or unmatched:
        raise _deepseek_history_validation_error(
            f"DeepSeek Anthropic messages[{index}] tool_result ids have no immediately preceding "
            f"assistant tool_use: {', '.join(unmatched or results)}"
        )
    content: Final = _message_blocks(message, "content")
    if any(
        (typed_block := _as_deepseek_mapping(block)) is None or typed_block.get("type") != "tool_result"
        for block in content[: len(results)]
    ):
        raise _deepseek_history_validation_error(
            f"DeepSeek Anthropic messages[{index}] tool_result blocks must appear before other content"
        )


def _unsigned_content_block(raw_block: object) -> tuple[object | None, bool, bool, bool]:
    if not isinstance(raw_block, Mapping):
        return deepcopy(raw_block), False, False, False
    block: Final = _DEEPSEEK_MAPPING_ADAPTER.validate_python(raw_block)
    block_type: Final = block.get("type")
    if block_type == "thinking":
        thinking: Final = block.get("thinking")
        return (
            (
                {
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


def _unsigned_content_blocks(content: Sequence[object]) -> tuple[Sequence[object], bool, bool, bool]:
    block_results: Final = tuple(_unsigned_content_block(block) for block in content)
    return (
        [sanitized for sanitized, _, _, _ in block_results if sanitized is not None],
        any(has_tool_use for _, has_tool_use, _, _ in block_results),
        any(has_redacted_thinking for _, _, has_redacted_thinking, _ in block_results),
        any(has_reasoning for _, _, _, has_reasoning in block_results),
    )


def _deepseek_history_validation_error(message: str) -> AnthropicError:
    return _DeepSeekHistoryValidationError(message=message, status_code=400)


def _content_block_tree(
    block: object, depth: int = 0, parent_type: str | None = None
) -> tuple[tuple[object, int, str | None], ...]:
    typed_block: Final = _as_deepseek_mapping(block)
    if typed_block is None or depth >= 1:
        return ((block, depth, parent_type),)
    nested_content: Final = typed_block.get("content")
    block_type: Final = typed_block.get("type") if typed_block is not None else None
    return (
        (block, depth, parent_type),
        *(
            _content_blocks(
                nested_content, depth=depth + 1, parent_type=block_type if isinstance(block_type, str) else None
            )
            if isinstance(nested_content, list)
            else ()
        ),
    )


def _content_blocks(
    content: Sequence[object], depth: int = 0, parent_type: str | None = None
) -> tuple[tuple[object, int, str | None], ...]:
    return tuple(
        chain.from_iterable(_content_block_tree(block, depth=depth, parent_type=parent_type) for block in content)
    )


def _message_content_tree(message: Mapping[str, object]) -> Iterator[tuple[str, object, int, str | None]]:
    for field in ("content", "thinking_blocks"):
        for block, depth, parent in _content_blocks(_message_blocks(message, field)):
            yield field, block, depth, parent


def _unsupported_deepseek_block_types(messages: Sequence[Mapping[str, object]]) -> Iterator[str]:
    for message in messages:
        for field, raw_block, depth, parent_type in _message_content_tree(message):
            block = _as_deepseek_mapping(raw_block)
            block_type = block.get("type") if block is not None else None
            if not isinstance(block_type, str):
                continue
            if (
                block_type in _DEEPSEEK_UNSUPPORTED_CONTENT_BLOCK_TYPES
                or (
                    block_type == "image"
                    and (
                        message.get("role") != "user"
                        or field != "content"
                        or (depth > 0 and parent_type != "tool_result")
                    )
                )
                or (block_type == "redacted_thinking" and (message.get("role") != "assistant" or depth > 0))
            ):
                yield block_type


def _validate_deepseek_content_blocks(messages: Sequence[Mapping[str, object]], model: str | None = None) -> None:
    unsupported_types: Final = sorted(frozenset(_unsupported_deepseek_block_types(messages)))
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
    blocks: Final = chain.from_iterable(_message_content_tree(message) for message in messages)
    invalid_fields: Final = tuple(filter(None, (_invalid_deepseek_tool_use_field(block) for _, block, _, _ in blocks)))
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
        if (fields := deepseek_mapping(block)) is not None and fields.get("type") in {"thinking", "redacted_thinking"}
    )
    return reasoning_blocks, has_redacted_thinking, has_reasoning


def _deepseek_history_message(
    message: Mapping[str, object],
    require_reasoning: bool,
    missing_reasoning: DeepSeekMissingReasoning | None,
) -> Mapping[str, object]:
    transformed_message: Final = deepcopy(dict(message))
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
        if (
            scalar_has_tool_history
            and require_reasoning
            and not scalar_has_reasoning
            and not scalar_uses_placeholder
            and missing_reasoning != "omit"
        ):
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
        block
        for block in transformed_content
        if not ((fields := deepseek_mapping(block)) is not None and fields.get("type") == "thinking")
    ]
    selected_content: Final = (
        content_without_inline_reasoning
        if not require_reasoning
        else [
            {
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
    if (
        has_tool_history
        and require_reasoning
        and not has_reasoning
        and not use_placeholder
        and missing_reasoning != "omit"
    ):
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
    missing_reasoning: DeepSeekMissingReasoning | None,
) -> Sequence[Mapping[str, object]]:
    transformed: Final = [
        _deepseek_history_message(
            message,
            require_reasoning=require_reasoning,
            missing_reasoning=missing_reasoning,
        )
        for message in messages
    ]
    warn_missing_reasoning_placeholders(transformed)
    return transformed


def _message_blocks(message: Mapping[str, object], field: str) -> Sequence[object]:
    value: Final = message.get(field)
    return deepseek_sequence(value)


def _chat_message_has_tool_history(message: Mapping[str, object]) -> bool:
    tool_calls: Final = message.get("tool_calls")
    if isinstance(tool_calls, list) and bool(tool_calls):
        return True
    if isinstance(message.get("function_call"), Mapping):
        return True
    return any(
        isinstance(block, Mapping) and message_field(block, "type") in {"tool_use", "server_tool_use"}
        for block in _message_blocks(message, "content")
    )


def deepseek_messages_have_tool_history(messages: Iterable[Mapping[str, object]]) -> bool:
    return any(_chat_message_has_tool_history(message) for message in messages)


def _prepare_deepseek_chat_message(
    message: Mapping[str, object],
    require_reasoning: bool,
    missing_reasoning: DeepSeekMissingReasoning | None,
) -> Mapping[str, object]:
    copied_message: Final = deepcopy(dict(message))
    if copied_message.get("role") != "assistant":
        return _sanitize_non_assistant_deepseek_message(copied_message)
    content: Final = copied_message.get("content")
    transformed_content, content_has_tool_use, content_has_redacted_thinking, content_has_reasoning = (
        _unsigned_content_blocks(_DEEPSEEK_SEQUENCE_ADAPTER.validate_python(content))
        if isinstance(content, list)
        else ((), False, False, False)
    )
    raw_thinking_blocks: Final = copied_message.get("thinking_blocks")
    thinking_blocks, _, blocks_have_redacted_thinking, blocks_have_reasoning = (
        _unsigned_content_blocks(_DEEPSEEK_SEQUENCE_ADAPTER.validate_python(raw_thinking_blocks))
        if isinstance(raw_thinking_blocks, list)
        else ((), False, False, False)
    )
    reasoning_content: Final = _nonempty_reasoning_content(message)
    prefer_reasoning_content: Final = require_reasoning and reasoning_content is not None
    selected_thinking: Final = (
        [{"type": "thinking", "thinking": reasoning_content}] if prefer_reasoning_content else thinking_blocks
    )
    selected_content: Final = (
        [
            block
            for block in transformed_content
            if not ((fields := deepseek_mapping(block)) is not None and fields.get("type") == "thinking")
        ]
        if prefer_reasoning_content or not require_reasoning
        else transformed_content
    )
    normalized_message: Final = {
        **copied_message,
        **({"content": selected_content} if isinstance(content, list) else {}),
    }
    has_reasoning: Final = content_has_reasoning or blocks_have_reasoning or prefer_reasoning_content
    has_redacted_thinking: Final = content_has_redacted_thinking or blocks_have_redacted_thinking
    thinking_blocks_for_request: Final = selected_thinking if require_reasoning else ()
    has_reasoning_for_request: Final = require_reasoning and has_reasoning
    has_tool_history: Final = content_has_tool_use or _chat_message_has_tool_history(normalized_message)
    if has_tool_history and has_redacted_thinking and require_reasoning and not has_reasoning_for_request:
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history cannot replay redacted thinking")
    use_placeholder: Final = (
        has_tool_history and require_reasoning and not has_reasoning_for_request and missing_reasoning == "placeholder"
    )
    if (
        has_tool_history
        and require_reasoning
        and not has_reasoning_for_request
        and not use_placeholder
        and missing_reasoning != "omit"
    ):
        raise _deepseek_history_validation_error("DeepSeek Anthropic tool history requires non-empty reasoning")
    final_thinking_blocks: Final = (
        [{"type": "thinking", "thinking": " "}, *thinking_blocks_for_request]
        if use_placeholder
        else thinking_blocks_for_request
    )
    cleaned_message: Final = _without_reasoning_content_fields(normalized_message)
    transformed_message: Final = {
        **{key: value for key, value in cleaned_message.items() if key != "thinking_blocks"},
        **({"thinking_blocks": final_thinking_blocks} if final_thinking_blocks else {}),
    }
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


def _upgrade_legacy_function_call(message: Mapping[str, object], message_index: int) -> Mapping[str, object]:
    function_call: Final = deepseek_mapping(message.get("function_call"))
    if message.get("role") != "assistant" or not function_call is not None:
        return message
    existing_tool_calls: Final = message.get("tool_calls")
    tool_calls: Final = deepseek_sequence(existing_tool_calls)
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
    function_call: Final = deepseek_mapping(message.get("function_call"))
    if function_call is not None:
        function_name: Final = function_call.get("name")
        if not isinstance(result_name, str) or function_name == result_name:
            return _legacy_function_call_id(message_index)
    tool_calls: Final = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    return next(filter(None, (_legacy_tool_call_result_id(tool_call, result_name) for tool_call in tool_calls)), None)


def _legacy_tool_call_result_id(raw_tool_call: object, result_name: object) -> str | None:
    tool_call: Final = _as_deepseek_mapping(raw_tool_call)
    if tool_call is None:
        return None
    function: Final = deepseek_mapping(tool_call.get("function"))
    if function is None or (isinstance(result_name, str) and function.get("name") != result_name):
        return None
    tool_id: Final = tool_call.get("id")
    return tool_id if isinstance(tool_id, str) and tool_id else None


def _legacy_function_result_call_id(messages: Sequence[Mapping[str, object]], message_index: int) -> str | None:
    tool_call_id: Final = messages[message_index].get("tool_call_id")
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


def _upgrade_legacy_function_result(message: Mapping[str, object], tool_call_id: str | None) -> Mapping[str, object]:
    if message.get("role") != "function" or tool_call_id is None:
        return message
    return {**message, "role": "tool", "tool_call_id": tool_call_id}


def prepare_deepseek_chat_history(
    messages: Sequence[Mapping[str, object]],
    require_reasoning: bool = True,
    missing_reasoning: DeepSeekMissingReasoning | None = None,
) -> Sequence[Mapping[str, object]]:
    _validate_deepseek_content_blocks(messages)
    prepared_messages: Final = [
        _prepare_deepseek_chat_message(
            message,
            require_reasoning=require_reasoning,
            missing_reasoning=missing_reasoning,
        )
        for message in messages
    ]
    upgraded: Final = [
        _upgrade_legacy_function_result(
            _upgrade_legacy_function_call(message, message_index),
            _legacy_function_result_call_id(prepared_messages, message_index),
        )
        for message_index, message in enumerate(prepared_messages)
    ]
    warn_missing_reasoning_placeholders(upgraded)
    return upgraded


def _normalized_tool_choice(raw_tool_choice: object) -> object:
    if isinstance(raw_tool_choice, str):
        return {
            "auto": {"type": "auto"},
            "none": {"type": "none"},
            "required": {"type": "any"},
            "any": {"type": "any"},
        }.get(raw_tool_choice, raw_tool_choice)
    if not isinstance(raw_tool_choice, Mapping):
        return deepcopy(raw_tool_choice)
    tool_choice: Final = _DEEPSEEK_MAPPING_ADAPTER.validate_python(raw_tool_choice)
    choice: Final = deepcopy(dict(tool_choice))
    choice_type: Final = choice.get("type")
    if choice_type == "required":
        return {**choice, "type": "any"}
    if choice_type == "function":
        function: Final = deepseek_mapping(choice.get("function"))
        if function is not None and isinstance(function.get("name"), str):
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


def _without_adaptive_reasoning_params(request_params: Mapping[str, object]) -> Mapping[str, object]:
    raw_reasoning_effort: Final = request_params.get("reasoning_effort")
    normalized_reasoning_effort: Final = _normalize_deepseek_reasoning_effort(raw_reasoning_effort)
    without_reasoning_effort: Final = {
        key: deepcopy(value) for key, value in request_params.items() if key != "reasoning_effort"
    }
    raw_output_config: Final = deepseek_mapping(without_reasoning_effort.get("output_config"))
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
            return {
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
        return {
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
    return {
        **request_params,
        "thinking": {"type": "enabled"},
    }


def _normalize_tool_choice_for_thinking(request_params: Mapping[str, object]) -> Mapping[str, object]:
    thinking: Final = request_params.get("thinking")
    if not (isinstance(thinking, Mapping) and message_field(thinking, "type") == "enabled"):
        return request_params
    tool_choice: Final = request_params.get("tool_choice")
    if not isinstance(tool_choice, Mapping):
        return request_params
    choice_type: Final = message_field(tool_choice, "type")
    if choice_type in ("auto", "none"):
        return request_params
    if choice_type in ("any", "tool"):
        return {**request_params, "tool_choice": {"type": "auto"}}
    return request_params


def omit_false_stream_for_deepseek_anthropic(
    request_params: Mapping[str, object],
) -> Mapping[str, object]:
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
        missing_reasoning: DeepSeekMissingReasoning | None = None,
    ) -> None:
        self._messages_path = messages_path
        self._tool_thinking = tool_thinking
        self._missing_reasoning: DeepSeekMissingReasoning | None = missing_reasoning

    @property
    def custom_llm_provider(self) -> str | None:
        return "deepseek"

    def should_strip_billing_metadata(self) -> bool:
        return True

    @staticmethod
    def _translate_adaptive_effort_for_non_adaptive_model(
        model: str, optional_params: Mapping[str, object], max_tokens: int | None, custom_llm_provider: str
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
        headers: dict[str, str],
        model: str,
        messages: Sequence[Mapping[str, object]],
        optional_params: Mapping[str, object],
        litellm_params: Mapping[str, object],
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> tuple[dict[str, str], str | None]:  # mutable-ok: provider environment interface returns mutable HTTP headers
        dynamic_api_key: Final = self.get_api_key(api_key=api_key)

        if "x-api-key" not in headers and "authorization" not in headers and dynamic_api_key is not None:
            headers["x-api-key"] = dynamic_api_key

        if "anthropic-version" not in headers:
            headers["anthropic-version"] = "2023-06-01"
        if "content-type" not in headers:
            headers["content-type"] = "application/json"

        updated_headers: Final = self._update_headers_with_anthropic_beta(
            headers=headers,
            optional_params=dict(optional_params),
            custom_llm_provider=self.custom_llm_provider or "deepseek",
            messages=[dict(message) for message in messages],
        )

        return TypeAdapter(dict[str, str]).validate_python(updated_headers), api_base

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: Mapping[str, object],
        litellm_params: Mapping[str, object],
        stream: bool | None = None,
    ) -> str:
        base_url = self.get_api_base(api_base=api_base).rstrip("/")
        return _complete_messages_url(base_url, self._messages_path)

    @staticmethod
    def _sanitize_tools_for_deepseek(tools: object) -> object:
        if not isinstance(tools, list):
            return tools
        return [
            {key: value for key, value in mapped.items() if key != "type"}
            if (mapped := deepseek_mapping(tool)) is not None and mapped.get("type") == "custom"
            else tool
            for tool in _DEEPSEEK_SEQUENCE_ADAPTER.validate_python(tools)
        ]

    def transform_anthropic_messages_request(
        self,
        model: str,
        messages: Sequence[Mapping[str, object]],
        anthropic_messages_optional_request_params: Mapping[str, object],
        litellm_params: GenericLiteLLMParams,
        headers: dict[str, str],
    ) -> dict[str, object]:  # mutable-ok: provider request interface returns mutable JSON dictionaries
        _validate_deepseek_content_blocks(messages, model=model)
        normalized_messages: Final = _merge_deepseek_tool_result_messages(
            _normalize_deepseek_native_tool_history(messages)
        )
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
        request_params: Final = _normalize_tool_choice_for_thinking(request_params_with_thinking_default)
        thinking: Final = deepseek_mapping(request_params.get("thinking"))
        require_reasoning: Final = thinking is not None and thinking.get("type") == "enabled"
        transformed_messages: Final = _deepseek_history(
            normalized_messages,
            require_reasoning=require_reasoning,
            missing_reasoning=self._missing_reasoning or "placeholder",
        )
        _validate_deepseek_tool_use_blocks(transformed_messages)
        _validate_deepseek_tool_history(transformed_messages)
        raw_anthropic_messages_request: Final[object] = super().transform_anthropic_messages_request(
            model=model,
            messages=[dict(message) for message in transformed_messages],
            anthropic_messages_optional_request_params=dict(request_params),
            litellm_params=litellm_params,
            headers=headers,
        )
        anthropic_messages_request: Final = TypeAdapter(dict[str, object]).validate_python(
            raw_anthropic_messages_request
        )
        normalized_anthropic_messages_request: Final = omit_false_stream_for_deepseek_anthropic(
            {**anthropic_messages_request, **({"thinking": thinking} if thinking is not None else {})}
        )
        if "tools" not in normalized_anthropic_messages_request:
            return dict(normalized_anthropic_messages_request)
        return {
            **normalized_anthropic_messages_request,
            "tools": self._sanitize_tools_for_deepseek(normalized_anthropic_messages_request["tools"]),
        }

    def get_async_streaming_response_iterator(
        self,
        model: str,
        httpx_response: httpx.Response,
        request_body: Mapping[str, object],
        litellm_logging_obj: LiteLLMLoggingObj,
    ) -> AsyncIterator[bytes]:
        completion_stream: Final[AsyncIterator[bytes]] = super().get_async_streaming_response_iterator(
            model=model,
            httpx_response=httpx_response,
            request_body=dict(request_body),
            litellm_logging_obj=litellm_logging_obj,
        )
        thinking: Final = deepseek_mapping(request_body.get("thinking"))
        thinking_disabled: Final = thinking is not None and thinking.get("type") == "disabled"
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
        model_call_details: Final = deepseek_mapping(getattr(logging_obj, "model_call_details", None))
        request_thinking: Final = (
            deepseek_mapping(model_call_details.get("thinking")) if model_call_details is not None else None
        )
        thinking_enabled: Final = request_thinking is not None and request_thinking.get("type") == "enabled"
        thinking_disabled: Final = request_thinking is not None and request_thinking.get("type") == "disabled"
        reasoning_content: Final = raw_response_data.get("reasoning_content")
        provider_specific_fields: Final = deepseek_mapping(raw_response_data.get("provider_specific_fields"))
        provider_reasoning_content: Final = (
            provider_specific_fields.get("reasoning_content") if provider_specific_fields is not None else None
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
        has_thinking: Final = any(_is_nonempty_deepseek_thinking_block(block) for block in content_blocks)
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

    def should_retry_anthropic_messages_on_http_error(
        self, e: httpx.HTTPStatusError, litellm_params: Mapping[str, object]
    ) -> bool:
        return False

    def should_disable_anthropic_messages_fallbacks_on_http_error(self, e: httpx.HTTPStatusError) -> bool:
        return e.response.status_code == 400 and _is_invalid_thinking_signature_error(e.response.text)
