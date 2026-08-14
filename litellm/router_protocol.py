"""Router-authored protocol capabilities for provider-specific request paths."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class DeploymentReasoningProtocol(StrEnum):
    DEEPSEEK_ANTHROPIC = "deepseek_anthropic"


_ROUTER_PROVENANCE = object()
_PROTOCOL_FIELD = "reasoning_protocol"


@dataclass(frozen=True, slots=True)
class DeploymentProtocolContext:
    protocol: DeploymentReasoningProtocol
    deployment_id: str
    attempt_id: str
    _provenance: object


def build_deployment_protocol_context(
    model_info: Mapping[str, object],
    deployment_id: object,
    attempt_id: object,
) -> DeploymentProtocolContext | None:
    protocol = model_info.get(_PROTOCOL_FIELD)
    if protocol != DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC.value:
        return None
    if not isinstance(deployment_id, str) or not deployment_id:
        return None
    if not isinstance(attempt_id, str) or not attempt_id:
        return None
    return DeploymentProtocolContext(
        protocol=DeploymentReasoningProtocol.DEEPSEEK_ANTHROPIC,
        deployment_id=deployment_id,
        attempt_id=attempt_id,
        _provenance=_ROUTER_PROVENANCE,
    )


def resolve_deployment_protocol(
    context: object,
    *,
    deployment_id: object = None,
    attempt_id: object = None,
) -> DeploymentReasoningProtocol | None:
    if not isinstance(context, DeploymentProtocolContext):
        return None
    if context._provenance is not _ROUTER_PROVENANCE:
        return None
    if deployment_id is not None and context.deployment_id != deployment_id:
        return None
    if attempt_id is not None and context.attempt_id != attempt_id:
        return None
    return context.protocol


def sanitize_model_info(model_info: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in model_info.items() if key != _PROTOCOL_FIELD}


def protocol_context_from_kwargs(
    kwargs: Mapping[str, object],
    *,
    deployment_id: object = None,
    attempt_id: object = None,
) -> DeploymentProtocolContext | None:
    candidate = kwargs.get("_litellm_deployment_protocol_context")
    if not isinstance(candidate, DeploymentProtocolContext):
        return None
    if resolve_deployment_protocol(candidate, deployment_id=deployment_id, attempt_id=attempt_id) is None:
        return None
    return candidate


__all__ = [
    "DeploymentProtocolContext",
    "DeploymentReasoningProtocol",
    "build_deployment_protocol_context",
    "protocol_context_from_kwargs",
    "resolve_deployment_protocol",
    "sanitize_model_info",
]
