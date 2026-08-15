from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


_DeepSeekAnthropicMessagesPath = Literal["anthropic/v1/messages", "v1/messages"]


@dataclass(frozen=True, slots=True)
class _RouterDeploymentProtocolContext:
    protocol: Literal["deepseek_anthropic"]
    messages_path: _DeepSeekAnthropicMessagesPath | None


def _build_deployment_protocol_context(model_info: object) -> _RouterDeploymentProtocolContext | None:
    if not isinstance(model_info, Mapping):
        return None
    if model_info.get("reasoning_protocol") != "deepseek_anthropic":
        return None
    messages_path = model_info.get("deepseek_anthropic_messages_path")
    if messages_path == "anthropic/v1/messages":
        return _RouterDeploymentProtocolContext(protocol="deepseek_anthropic", messages_path="anthropic/v1/messages")
    if messages_path == "v1/messages":
        return _RouterDeploymentProtocolContext(protocol="deepseek_anthropic", messages_path="v1/messages")
    return _RouterDeploymentProtocolContext(protocol="deepseek_anthropic", messages_path=None)


def get_deployment_protocol_context(kwargs: Mapping[str, object]) -> _RouterDeploymentProtocolContext | None:
    context = kwargs.get("_litellm_deployment_protocol_context")
    return context if isinstance(context, _RouterDeploymentProtocolContext) else None
