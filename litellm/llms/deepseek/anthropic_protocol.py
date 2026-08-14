"""Canonical DeepSeek Anthropic reasoning history validation."""

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from json import dumps
from math import ceil
from typing import Mapping, Sequence


class DeepSeekProtocolNonFallbackError(ValueError):
    def __init__(self, code: str):
        self.code = code
        self.status_code = 400
        self.retry_allowed = False
        self.fallback_allowed = False
        super().__init__(code)


class DeepSeekProtocolError(DeepSeekProtocolNonFallbackError):
    pass


class DeepSeekUpstreamError(ValueError):
    def __init__(self, category: str, status_code: int | None):
        self.category = category
        self.status_code = status_code
        self.retry_allowed = True
        self.fallback_allowed = True
        super().__init__(category)


@dataclass(frozen=True, slots=True)
class ToolAssociatedCanonicalSuffix:
    messages: tuple[dict[str, object], ...]
    call_ids: tuple[str, ...]
    digest: str
    token_count: int
    version: int = 1


@dataclass(frozen=True, slots=True)
class CanonicalHistory:
    messages: tuple[dict[str, object], ...]
    generation_thinking_enabled: bool
    history_reasoning_required: bool
    suffix: ToolAssociatedCanonicalSuffix | None


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _message_content(message: Mapping[str, object]) -> tuple[object, ...]:
    content = message.get("content")
    if isinstance(content, list):
        return tuple(content)
    if isinstance(content, str):
        return (content,)
    return ()


def _restored_thinking(message: Mapping[str, object]) -> str | None:
    for field in ("reasoning_content",):
        recovered = _non_empty_string(message.get(field))
        if recovered is not None:
            return recovered
    provider_fields = message.get("provider_specific_fields")
    if isinstance(provider_fields, Mapping):
        recovered = _non_empty_string(provider_fields.get("reasoning_content"))
        if recovered is not None:
            return recovered
    for block in _message_content(message):
        if isinstance(block, Mapping) and block.get("type") == "thinking":
            recovered = _non_empty_string(block.get("thinking"))
            if recovered is not None:
                return recovered
    return None


def _has_redacted_thinking(message: Mapping[str, object]) -> bool:
    return any(
        isinstance(block, Mapping) and block.get("type") == "redacted_thinking"
        for block in _message_content(message)
    )


def _tool_use_ids(message: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for block in _message_content(message):
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        call_id = _non_empty_string(block.get("id"))
        if call_id is None:
            raise DeepSeekProtocolError("tool_history_invalid")
        values.append(call_id)
    return tuple(values)


def _tool_result_ids(message: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for block in _message_content(message):
        if not isinstance(block, Mapping) or block.get("type") != "tool_result":
            continue
        call_id = _non_empty_string(block.get("tool_use_id"))
        if call_id is None:
            raise DeepSeekProtocolError("tool_history_invalid")
        values.append(call_id)
    return tuple(values)


def _copy_message_for_wire(message: Mapping[str, object], *, keep_thinking: bool) -> dict[str, object]:
    copied = deepcopy(dict(message))
    copied.pop("reasoning_content", None)
    copied.pop("provider_specific_fields", None)
    content = copied.get("content")
    if not isinstance(content, list):
        return copied
    restored = _restored_thinking(message)
    wire_content = [
        {key: deepcopy(value) for key, value in block.items() if key != "signature"}
        if isinstance(block, Mapping) and block.get("type") == "thinking"
        else deepcopy(block)
        for block in content
        if not (isinstance(block, Mapping) and block.get("type") == "redacted_thinking")
    ]
    has_thinking = any(isinstance(block, Mapping) and block.get("type") == "thinking" for block in wire_content)
    if keep_thinking and restored is not None and not has_thinking:
        wire_content.insert(0, {"type": "thinking", "thinking": restored})
    if not keep_thinking:
        wire_content = [
            block
            for block in wire_content
            if not (isinstance(block, Mapping) and block.get("type") == "thinking")
        ]
    copied["content"] = wire_content
    return copied


def _suffix_digest(messages: Sequence[dict[str, object]], call_ids: Sequence[str]) -> str:
    encoded = dumps(
        {"version": 1, "messages": messages, "call_ids": call_ids},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return sha256(encoded).hexdigest()


def _suffix_token_count(messages: Sequence[dict[str, object]]) -> int:
    encoded = dumps(messages, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    return ceil(len(encoded) / 4)


def _generation_thinking_enabled(thinking: object) -> bool:
    if thinking is None:
        return True
    if not isinstance(thinking, Mapping):
        raise DeepSeekProtocolError("reasoning_mode_invalid")
    mode = thinking.get("type")
    if mode == "enabled":
        return True
    if mode == "disabled":
        return False
    raise DeepSeekProtocolError("reasoning_mode_invalid")


def _validate_manifest(suffix: ToolAssociatedCanonicalSuffix, manifest: Mapping[str, object]) -> None:
    if manifest.get("version") != suffix.version:
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")
    if manifest.get("digest") != suffix.digest:
        raise DeepSeekProtocolError("reasoning_history_unrecoverable")


def _register_tool_uses(
    message: Mapping[str, object],
    index: int,
    tool_uses: dict[str, int],
    pending_calls: set[str],
) -> int | None:
    call_ids = _tool_use_ids(message)
    if pending_calls and call_ids:
        raise DeepSeekProtocolError("tool_history_incomplete")
    for call_id in call_ids:
        if call_id in tool_uses:
            raise DeepSeekProtocolError("tool_history_invalid")
        tool_uses[call_id] = index
        pending_calls.add(call_id)
    return index if call_ids else None


def _register_tool_results(
    message: Mapping[str, object],
    tool_uses: Mapping[str, int],
    tool_results: set[str],
    pending_calls: set[str],
) -> None:
    for call_id in _tool_result_ids(message):
        if call_id not in tool_uses:
            raise DeepSeekProtocolError("tool_result_orphaned")
        if call_id in tool_results:
            raise DeepSeekProtocolError("tool_history_incomplete")
        tool_results.add(call_id)
        pending_calls.discard(call_id)


def _collect_tool_graph(
    messages: Sequence[dict[str, object]], *, allow_pending_calls: bool = False
) -> tuple[int | None, tuple[str, ...]]:
    tool_uses: dict[str, int] = {}
    tool_results: set[str] = set()
    pending_calls: set[str] = set()
    first_tool_use_index: int | None = None
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            first_use = _register_tool_uses(message, index, tool_uses, pending_calls)
            if first_tool_use_index is None and first_use is not None:
                first_tool_use_index = first_use
        if message.get("role") == "user":
            _register_tool_results(message, tool_uses, tool_results, pending_calls)
    if set(tool_uses) != tool_results and not allow_pending_calls:
        raise DeepSeekProtocolError("tool_history_incomplete")
    return first_tool_use_index, tuple(tool_uses)


def _compile_without_tool_history(
    messages: Sequence[dict[str, object]], generation_thinking_enabled: bool
) -> CanonicalHistory:
    wire_messages = tuple(
        _copy_message_for_wire(message, keep_thinking=generation_thinking_enabled) for message in messages
    )
    return CanonicalHistory(
        messages=wire_messages,
        generation_thinking_enabled=generation_thinking_enabled,
        history_reasoning_required=False,
        suffix=None,
    )


def _compile_tool_suffix(
    messages: Sequence[dict[str, object]],
    first_tool_use_index: int,
    call_ids: tuple[str, ...],
    manifest: Mapping[str, object] | None,
    max_suffix_tokens: int | None,
) -> ToolAssociatedCanonicalSuffix:
    suffix_source = messages[first_tool_use_index:]
    for message in suffix_source:
        if message.get("role") != "assistant":
            continue
        if _has_redacted_thinking(message):
            raise DeepSeekProtocolError("reasoning_history_unrecoverable")
        if _restored_thinking(message) is None:
            raise DeepSeekProtocolError("reasoning_history_missing")
    suffix_messages = tuple(_copy_message_for_wire(message, keep_thinking=True) for message in suffix_source)
    suffix = ToolAssociatedCanonicalSuffix(
        messages=tuple(deepcopy(message) for message in suffix_messages),
        call_ids=call_ids,
        digest=_suffix_digest(suffix_messages, call_ids),
        token_count=_suffix_token_count(suffix_messages),
    )
    if manifest is not None:
        _validate_manifest(suffix, manifest)
    if max_suffix_tokens is not None and suffix.token_count > max_suffix_tokens:
        raise DeepSeekProtocolError("reasoning_history_context_exhausted")
    return suffix


def compile_deepseek_anthropic_history(
    messages: Sequence[Mapping[str, object]],
    thinking: object = None,
    *,
    max_suffix_tokens: int | None = None,
    manifest: Mapping[str, object] | None = None,
) -> CanonicalHistory:
    generation_thinking_enabled = _generation_thinking_enabled(thinking)
    source_messages = tuple(deepcopy(dict(message)) for message in messages)
    first_tool_use_index, call_ids = _collect_tool_graph(source_messages)
    if first_tool_use_index is None:
        return _compile_without_tool_history(source_messages, generation_thinking_enabled)
    suffix = _compile_tool_suffix(source_messages, first_tool_use_index, call_ids, manifest, max_suffix_tokens)
    if not generation_thinking_enabled:
        raise DeepSeekProtocolError("reasoning_mode_conflict")
    prefix = tuple(
        _copy_message_for_wire(message, keep_thinking=generation_thinking_enabled)
        for message in source_messages[:first_tool_use_index]
    )
    return CanonicalHistory(
        messages=prefix + suffix.messages,
        generation_thinking_enabled=True,
        history_reasoning_required=True,
        suffix=suffix,
    )


def deepseek_anthropic_session_manifest(messages: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    source_messages = tuple(deepcopy(dict(message)) for message in messages)
    first_tool_use_index, call_ids = _collect_tool_graph(source_messages, allow_pending_calls=True)
    if first_tool_use_index is None:
        return None
    suffix = _compile_tool_suffix(source_messages, first_tool_use_index, call_ids, None, None)
    return {
        "version": suffix.version,
        "digest": suffix.digest,
        "call_ids": list(suffix.call_ids),
        "token_count": suffix.token_count,
    }


__all__ = [
    "CanonicalHistory",
    "DeepSeekProtocolError",
    "DeepSeekProtocolNonFallbackError",
    "DeepSeekUpstreamError",
    "ToolAssociatedCanonicalSuffix",
    "compile_deepseek_anthropic_history",
    "deepseek_anthropic_session_manifest",
]
