from collections.abc import Mapping, Sequence
from typing import Final

import litellm


def _is_reasoning_placeholder_block(block: object) -> bool:
    return isinstance(block, Mapping) and block.get("type") == "thinking" and block.get("thinking") == " "


def _has_reasoning_placeholder(message: Mapping[str, object]) -> bool:
    if message.get("role") != "assistant":
        return False
    if message.get("reasoning_content") == " ":
        return True
    return any(
        _is_reasoning_placeholder_block(block)
        for field in ("content", "thinking_blocks")
        for blocks in (message.get(field),)
        if isinstance(blocks, list)
        for block in blocks
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
