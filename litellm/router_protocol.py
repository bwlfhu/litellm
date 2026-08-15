"""Router-authored protocol capabilities for provider-specific request paths."""

from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from inspect import currentframe
from math import isfinite
import asyncio
from typing import Callable, Mapping, cast


class DeploymentReasoningProtocol(StrEnum):
    DEEPSEEK_ANTHROPIC = "deepseek_anthropic"


_PROTOCOL_FIELD = "reasoning_protocol"
_SUFFIX_TOKEN_BUDGET_FIELD = "deepseek_reasoning_suffix_token_budget"
_CONTEXT_TOKEN_BUDGET_FIELD = "deepseek_reasoning_context_token_budget"
_MESSAGES_PATH_FIELD = "deepseek_anthropic_messages_path"
_DEFAULT_DEEPSEEK_ANTHROPIC_MESSAGES_PATH = "anthropic/v1/messages"
_DEEPSEEK_ANTHROPIC_MESSAGES_PATHS = frozenset({_DEFAULT_DEEPSEEK_ANTHROPIC_MESSAGES_PATH, "v1/messages"})
_PROTOCOL_PRIVATE_MODEL_INFO_FIELDS = frozenset(
    {_PROTOCOL_FIELD, _SUFFIX_TOKEN_BUDGET_FIELD, _CONTEXT_TOKEN_BUDGET_FIELD, _MESSAGES_PATH_FIELD}
)


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
    context_token_budget: int = 0
    messages_path: str = _DEFAULT_DEEPSEEK_ANTHROPIC_MESSAGES_PATH

    def is_router_provenanced(self) -> bool:
        return _is_router_provenanced(self)

    def has_provenance(self, provenance: object) -> bool:
        return self._provenance is provenance


def _called_from_router() -> bool:
    frame = currentframe()
    if frame is None or frame.f_back is None or frame.f_back.f_back is None:
        return False
    module_name: object = frame.f_back.f_back.f_globals.get("__name__")
    return module_name == "litellm.router"


class _RouterProtocolContextActivation:
    def __init__(
        self,
        context: object,
        active_context: ContextVar[DeploymentProtocolContext | None],
        active_owner: ContextVar[object | None],
        is_router_provenanced: Callable[[object], bool],
    ) -> None:
        self._context = context
        self._active_context = active_context
        self._active_owner = active_owner
        self._is_router_provenanced = is_router_provenanced
        self._token: Token[DeploymentProtocolContext | None] | None = None
        self._owner_token: Token[object | None] | None = None

    def __enter__(self) -> None:
        if _called_from_router() and self._is_router_provenanced(self._context):
            context = cast(DeploymentProtocolContext, self._context)
            self._token = self._active_context.set(context)
            try:
                owner: object | None = asyncio.current_task()
            except RuntimeError:
                owner = None
            if owner is None:
                owner = object()
            self._owner_token = self._active_owner.set(owner)

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._token is not None:
            self._active_context.reset(self._token)
        if self._owner_token is not None:
            self._active_owner.reset(self._owner_token)


def _protocol_context_runtime() -> tuple[
    Callable[[Mapping[str, object], object, object], DeploymentProtocolContext | None],
    Callable[[object], _RouterProtocolContextActivation],
    Callable[[], DeploymentProtocolContext | None],
    Callable[[object], bool],
]:
    provenance = object()
    active_context: ContextVar[DeploymentProtocolContext | None] = ContextVar(
        "active_router_protocol_context", default=None
    )
    active_owner: ContextVar[object | None] = ContextVar("active_router_protocol_owner", default=None)

    def is_router_provenanced(context: object) -> bool:
        return isinstance(context, DeploymentProtocolContext) and context.has_provenance(provenance)

    def build(
        model_info: Mapping[str, object], deployment_id: object, attempt_id: object
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
            _provenance=provenance,
            context_token_budget=_context_token_budget(model_info),
            messages_path=_messages_path(model_info),
        )

    def activate(context: object) -> _RouterProtocolContextActivation:
        return _RouterProtocolContextActivation(context, active_context, active_owner, is_router_provenanced)

    def active() -> DeploymentProtocolContext | None:
        context = active_context.get()
        owner = active_owner.get()
        if context is None or owner is None:
            return None
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        # Context copied into an executor thread has no asyncio task and is
        # still part of the same Router dispatch. A child asyncio task has a
        # different task identity and must not inherit the capability.
        if current is not None and current is not owner:
            return None
        return context

    return build, activate, active, is_router_provenanced


def _non_negative_rate(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    rate = float(value)
    return rate if isfinite(rate) and rate >= 0 else 0.0


def _build_rate_snapshot(model_info: Mapping[str, object]) -> DeploymentRateSnapshot:
    cache_read_rate = model_info.get(
        "cache_read_input_token_cost",
        model_info.get("cache_read_input_cost_per_token"),
    )
    cache_creation_rate = model_info.get(
        "cache_creation_input_token_cost",
        model_info.get("cache_creation_input_cost_per_token"),
    )
    return DeploymentRateSnapshot(
        input_cost_per_token=_non_negative_rate(model_info.get("input_cost_per_token")),
        output_cost_per_token=_non_negative_rate(model_info.get("output_cost_per_token")),
        cache_read_input_cost_per_token=_non_negative_rate(cache_read_rate),
        cache_creation_input_cost_per_token=_non_negative_rate(cache_creation_rate),
    )


def deployment_rate_snapshot(model_info: Mapping[str, object]) -> DeploymentRateSnapshot:
    return _build_rate_snapshot(model_info)


def _suffix_token_budget(model_info: Mapping[str, object]) -> int:
    configured_budget = model_info.get(_SUFFIX_TOKEN_BUDGET_FIELD)
    if isinstance(configured_budget, int) and not isinstance(configured_budget, bool) and configured_budget >= 0:
        return configured_budget
    context_window = model_info.get("max_input_tokens")
    if isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0:
        return context_window
    return 0


def _context_token_budget(model_info: Mapping[str, object]) -> int:
    configured_budget = model_info.get(_CONTEXT_TOKEN_BUDGET_FIELD)
    if isinstance(configured_budget, int) and not isinstance(configured_budget, bool) and configured_budget >= 0:
        return configured_budget
    context_window = model_info.get("max_input_tokens")
    if isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0:
        return context_window
    return 0


def _messages_path(model_info: Mapping[str, object]) -> str:
    configured_path = model_info.get(_MESSAGES_PATH_FIELD)
    if isinstance(configured_path, str) and configured_path in _DEEPSEEK_ANTHROPIC_MESSAGES_PATHS:
        return configured_path
    return _DEFAULT_DEEPSEEK_ANTHROPIC_MESSAGES_PATH


(
    _build_deployment_protocol_context,
    _activate_router_protocol_context,
    _active_router_protocol_context,
    _is_router_provenanced,
) = _protocol_context_runtime()


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
    if _active_router_protocol_context() is not candidate:
        return None
    if resolve_deployment_protocol(candidate, deployment_id=deployment_id, attempt_id=attempt_id) is None:
        return None
    return candidate


__all__ = [
    "DeploymentProtocolContext",
    "DeploymentRateSnapshot",
    "DeploymentReasoningProtocol",
    "deployment_rate_snapshot",
    "protocol_context_from_kwargs",
    "resolve_deployment_protocol",
    "sanitize_model_info",
]
