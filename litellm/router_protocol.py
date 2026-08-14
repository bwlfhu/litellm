"""Router-authored protocol capabilities for provider-specific request paths."""

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from inspect import currentframe
from math import isfinite
from typing import Mapping


class DeploymentReasoningProtocol(StrEnum):
    DEEPSEEK_ANTHROPIC = "deepseek_anthropic"


_ROUTER_PROVENANCE = object()
_PROTOCOL_FIELD = "reasoning_protocol"
_SUFFIX_TOKEN_BUDGET_FIELD = "deepseek_reasoning_suffix_token_budget"
_PROTOCOL_PRIVATE_MODEL_INFO_FIELDS = frozenset({_PROTOCOL_FIELD, _SUFFIX_TOKEN_BUDGET_FIELD})


@dataclass(frozen=True, slots=True)
class DeploymentRateSnapshot:
    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    cache_read_input_cost_per_token: float = 0.0
    cache_creation_input_cost_per_token: float = 0.0


@dataclass(frozen=True, slots=True)
class DeploymentProtocolContext:
    protocol: DeploymentReasoningProtocol
    deployment_id: str
    attempt_id: str
    suffix_token_budget: int
    rate_snapshot: DeploymentRateSnapshot
    _provenance: object

    def is_router_provenanced(self) -> bool:
        return self._provenance is _ROUTER_PROVENANCE


def _called_from_router() -> bool:
    frame = currentframe()
    while frame is not None:
        module_name: object = frame.f_globals.get("__name__")
        if module_name == "litellm.router":
            return True
        frame = frame.f_back
    return False


_ACTIVE_ROUTER_PROTOCOL_CONTEXT: ContextVar[DeploymentProtocolContext | None] = ContextVar(
    "active_router_protocol_context", default=None
)


def _non_negative_rate(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    rate = float(value)
    return rate if isfinite(rate) and rate >= 0 else 0.0


def _build_rate_snapshot(model_info: Mapping[str, object]) -> DeploymentRateSnapshot:
    return DeploymentRateSnapshot(
        input_cost_per_token=_non_negative_rate(model_info.get("input_cost_per_token")),
        output_cost_per_token=_non_negative_rate(model_info.get("output_cost_per_token")),
        cache_read_input_cost_per_token=_non_negative_rate(model_info.get("cache_read_input_cost_per_token")),
        cache_creation_input_cost_per_token=_non_negative_rate(model_info.get("cache_creation_input_cost_per_token")),
    )


def _suffix_token_budget(model_info: Mapping[str, object]) -> int:
    configured_budget = model_info.get(_SUFFIX_TOKEN_BUDGET_FIELD)
    if isinstance(configured_budget, int) and not isinstance(configured_budget, bool) and configured_budget >= 0:
        return configured_budget
    context_window = model_info.get("max_input_tokens")
    if isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0:
        return context_window
    return 0


def _build_deployment_protocol_context(
    model_info: Mapping[str, object],
    deployment_id: object,
    attempt_id: object,
) -> DeploymentProtocolContext | None:
    if not _called_from_router():
        return None
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
        suffix_token_budget=_suffix_token_budget(model_info),
        rate_snapshot=_build_rate_snapshot(model_info),
        _provenance=_ROUTER_PROVENANCE,
    )


@contextmanager
def _activate_router_protocol_context(context: object) -> Generator[None, None, None]:
    if (
        not _called_from_router()
        or not isinstance(context, DeploymentProtocolContext)
        or not context.is_router_provenanced()
    ):
        yield
        return
    token = _ACTIVE_ROUTER_PROTOCOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_ROUTER_PROTOCOL_CONTEXT.reset(token)


def resolve_deployment_protocol(
    context: object,
    *,
    deployment_id: object = None,
    attempt_id: object = None,
) -> DeploymentReasoningProtocol | None:
    if not isinstance(context, DeploymentProtocolContext):
        return None
    if not context.is_router_provenanced():
        return None
    if deployment_id is not None and context.deployment_id != deployment_id:
        return None
    if attempt_id is not None and context.attempt_id != attempt_id:
        return None
    return context.protocol


def sanitize_model_info(model_info: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in model_info.items() if key not in _PROTOCOL_PRIVATE_MODEL_INFO_FIELDS}


def protocol_context_from_kwargs(
    kwargs: Mapping[str, object],
    *,
    deployment_id: object = None,
    attempt_id: object = None,
) -> DeploymentProtocolContext | None:
    candidate = kwargs.get("_litellm_deployment_protocol_context")
    if not isinstance(candidate, DeploymentProtocolContext):
        return None
    if _ACTIVE_ROUTER_PROTOCOL_CONTEXT.get() is not candidate:
        return None
    if resolve_deployment_protocol(candidate, deployment_id=deployment_id, attempt_id=attempt_id) is None:
        return None
    return candidate


__all__ = [
    "DeploymentProtocolContext",
    "DeploymentRateSnapshot",
    "DeploymentReasoningProtocol",
    "protocol_context_from_kwargs",
    "resolve_deployment_protocol",
    "sanitize_model_info",
]
