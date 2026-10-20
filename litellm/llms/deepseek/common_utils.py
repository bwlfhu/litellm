from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import TypeAdapter

import litellm

_MAPPING_ADAPTER: Final = TypeAdapter(Mapping[str, object])
_SEQUENCE_ADAPTER: Final = TypeAdapter(Sequence[object])


def deepseek_mapping(value: object) -> Mapping[str, object] | None:
    return _MAPPING_ADAPTER.validate_python(value) if isinstance(value, Mapping) else None


def deepseek_sequence(value: object) -> Sequence[object]:
    return _SEQUENCE_ADAPTER.validate_python(value) if isinstance(value, list) else ()


def _is_reasoning_placeholder_block(block: object) -> bool:
    mapping: Final = deepseek_mapping(block)
    return mapping is not None and mapping.get("type") == "thinking" and mapping.get("thinking") == " "


def _has_reasoning_placeholder(message: Mapping[str, object]) -> bool:
    if message.get("role") != "assistant":
        return False
    if message.get("reasoning_content") == " ":
        return True
    return any(
        _is_reasoning_placeholder_block(block)
        for block in (*deepseek_sequence(message.get("content")), *deepseek_sequence(message.get("thinking_blocks")))
    )


def warn_missing_reasoning_placeholders(messages: Sequence[Mapping[str, object]]) -> None:
    placeholder_count: Final = sum(1 for message in messages if _has_reasoning_placeholder(message))
    if placeholder_count == 0:
        return
    litellm.verbose_logger.warning(
        "DeepSeek thinking mode: %d assistant message(s) were missing usable reasoning. "
        "A single-space placeholder was injected for each, which can degrade multi-turn response quality. "
        "Preserve reasoning_content or thinking blocks from original responses when building conversation history.",
        placeholder_count,
    )
