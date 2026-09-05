from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

_DeepSeekAnthropicMessagesPath = Literal["anthropic/v1/messages", "v1/messages"]
_DeepSeekAnthropicToolThinking = Literal["disabled"]
_DeepSeekAnthropicMissingReasoning = Literal["placeholder"]
_PROTOCOL_CONTEXT_OWNER: Final = object()


@dataclass(frozen=True, slots=True)
class _RouterDeploymentProtocolContext:
    protocol: Literal["deepseek_anthropic"]
    messages_path: _DeepSeekAnthropicMessagesPath | None
    _owner: object
    tool_thinking: _DeepSeekAnthropicToolThinking | None = None
    missing_reasoning: _DeepSeekAnthropicMissingReasoning | None = None


def _build_deployment_protocol_context(model_info: object) -> _RouterDeploymentProtocolContext | None:
    if not isinstance(model_info, Mapping):
        return None
    messages_path: Final = model_info.get("deepseek_anthropic_messages_path")
    normalized_messages_path: Final = (
        "anthropic/v1/messages"
        if messages_path == "anthropic/v1/messages"
        else "v1/messages"
        if messages_path == "v1/messages"
        else None
    )
    tool_thinking: Final = "disabled" if model_info.get("deepseek_anthropic_tool_thinking") == "disabled" else None
    missing_reasoning: Final = (
        "placeholder" if model_info.get("deepseek_anthropic_missing_reasoning") == "placeholder" else None
    )
    if (
        model_info.get("reasoning_protocol") != "deepseek_anthropic"
        and normalized_messages_path is None
        and tool_thinking is None
        and missing_reasoning is None
    ):
        return None
    return _RouterDeploymentProtocolContext(
        protocol="deepseek_anthropic",
        messages_path=normalized_messages_path,
        _owner=_PROTOCOL_CONTEXT_OWNER,
        tool_thinking=tool_thinking,
        missing_reasoning=missing_reasoning,
    )


def get_deployment_protocol_context(kwargs: Mapping[str, object]) -> _RouterDeploymentProtocolContext | None:
    context: Final = kwargs.get("_litellm_deployment_protocol_context")
    if isinstance(context, _RouterDeploymentProtocolContext) and context._owner is _PROTOCOL_CONTEXT_OWNER:
        return context
    return None
