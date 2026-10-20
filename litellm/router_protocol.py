from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

from pydantic import TypeAdapter

_DeepSeekAnthropicMessagesPath: TypeAlias = Literal["anthropic/v1/messages", "v1/messages"]
_DeepSeekAnthropicToolThinking: TypeAlias = Literal["disabled"]
DeepSeekMissingReasoning: TypeAlias = Literal["placeholder", "omit", "error"]
_PROTOCOL_CONTEXT_OWNER: Final = object()


@dataclass(frozen=True, slots=True)
class _RouterDeploymentProtocolContext:
    protocol: Literal["deepseek_anthropic"]
    messages_path: _DeepSeekAnthropicMessagesPath | None
    owner: object
    tool_thinking: _DeepSeekAnthropicToolThinking | None = None
    missing_reasoning: DeepSeekMissingReasoning | None = None


def build_deployment_protocol_context(model_info: object) -> _RouterDeploymentProtocolContext | None:
    if not isinstance(model_info, Mapping):
        return None
    fields: Final = TypeAdapter(Mapping[str, object]).validate_python(model_info)
    messages_path: Final = fields.get("deepseek_anthropic_messages_path")
    normalized_messages_path: Final = (
        "anthropic/v1/messages"
        if messages_path == "anthropic/v1/messages"
        else "v1/messages"
        if messages_path == "v1/messages"
        else None
    )
    tool_thinking: Final = "disabled" if fields.get("deepseek_anthropic_tool_thinking") == "disabled" else None
    requested_missing_reasoning: Final[object] = fields.get("deepseek_anthropic_missing_reasoning")
    missing_reasoning: Final[DeepSeekMissingReasoning | None] = (
        "placeholder"
        if requested_missing_reasoning == "placeholder"
        else "omit"
        if requested_missing_reasoning == "omit"
        else "error"
        if requested_missing_reasoning == "error"
        else None
    )
    if (
        fields.get("reasoning_protocol") != "deepseek_anthropic"
        and normalized_messages_path is None
        and tool_thinking is None
        and missing_reasoning is None
    ):
        return None
    return _RouterDeploymentProtocolContext(
        protocol="deepseek_anthropic",
        messages_path=normalized_messages_path,
        owner=_PROTOCOL_CONTEXT_OWNER,
        tool_thinking=tool_thinking,
        missing_reasoning=missing_reasoning,
    )


def get_deployment_protocol_context(kwargs: Mapping[str, object]) -> _RouterDeploymentProtocolContext | None:
    context: Final = kwargs.get("_litellm_deployment_protocol_context")
    if isinstance(context, _RouterDeploymentProtocolContext) and context.owner is _PROTOCOL_CONTEXT_OWNER:
        return context
    return None
