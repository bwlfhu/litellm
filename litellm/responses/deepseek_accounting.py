"""Single-owner usage and cost accounting for DeepSeek Responses attempts."""

from dataclasses import dataclass
from typing import Mapping

from pydantic import TypeAdapter, ValidationError

_USAGE_DETAILS_ADAPTER = TypeAdapter(dict[str, object])


@dataclass(frozen=True, slots=True)
class AttemptRateSnapshot:
    input_cost_per_token: float = 0.0
    output_cost_per_token: float = 0.0
    cache_read_input_cost_per_token: float = 0.0
    cache_creation_input_cost_per_token: float = 0.0


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class AttemptAccountingSnapshot:
    model: str
    deployment_id: str
    rates: AttemptRateSnapshot
    usage: NormalizedUsage
    cost: float


@dataclass(frozen=True, slots=True)
class ParentAccounting:
    attempts: tuple[AttemptAccountingSnapshot, ...] = ()

    def add_attempt(self, snapshot: AttemptAccountingSnapshot) -> "ParentAccounting":
        return ParentAccounting(attempts=self.attempts + (snapshot,))

    @property
    def usage(self) -> NormalizedUsage:
        return NormalizedUsage(
            input_tokens=sum(attempt.usage.input_tokens for attempt in self.attempts),
            output_tokens=sum(attempt.usage.output_tokens for attempt in self.attempts),
            cache_read_input_tokens=sum(attempt.usage.cache_read_input_tokens for attempt in self.attempts),
            cache_creation_input_tokens=sum(attempt.usage.cache_creation_input_tokens for attempt in self.attempts),
        )

    @property
    def cost(self) -> float:
        return sum(attempt.cost for attempt in self.attempts)

    def spend_log_summary(self) -> dict[str, object]:
        return {
            "attempt_count": len(self.attempts),
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_input_tokens": self.usage.cache_read_input_tokens,
            "cache_creation_input_tokens": self.usage.cache_creation_input_tokens,
            "total_tokens": self.usage.total_tokens,
            "cost": self.cost,
            "attempts": tuple(
                {
                    "model": attempt.model,
                    "deployment_id": attempt.deployment_id,
                    "input_tokens": attempt.usage.input_tokens,
                    "output_tokens": attempt.usage.output_tokens,
                    "cache_read_input_tokens": attempt.usage.cache_read_input_tokens,
                    "cost": attempt.cost,
                    "rates": {
                        "input_cost_per_token": attempt.rates.input_cost_per_token,
                        "output_cost_per_token": attempt.rates.output_cost_per_token,
                        "cache_read_input_cost_per_token": attempt.rates.cache_read_input_cost_per_token,
                        "cache_creation_input_cost_per_token": attempt.rates.cache_creation_input_cost_per_token,
                    },
                }
                for attempt in self.attempts
            ),
        }


def normalize_deepseek_usage(usage: Mapping[str, object]) -> NormalizedUsage:
    input_tokens = _token_count(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
    output_tokens = _token_count(usage.get("output_tokens", usage.get("completion_tokens", 0)))
    nested_cached_tokens = _cached_tokens_from_details(usage.get("input_tokens_details"))
    cache_read = _token_count(
        usage.get(
            "cache_read_input_tokens",
            usage.get("prompt_cache_hit_tokens", usage.get("cached_tokens", nested_cached_tokens)),
        )
    )
    cache_creation = _token_count(usage.get("cache_creation_input_tokens", 0))
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


def _token_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _cached_tokens_from_details(value: object) -> int:
    try:
        details = _USAGE_DETAILS_ADAPTER.validate_python(value)
    except ValidationError:
        return 0
    return _token_count(details.get("cached_tokens"))


def calculate_attempt_cost(usage: NormalizedUsage, rates: AttemptRateSnapshot) -> float:
    billable_input = max(usage.input_tokens - usage.cache_read_input_tokens - usage.cache_creation_input_tokens, 0)
    return (
        billable_input * rates.input_cost_per_token
        + usage.output_tokens * rates.output_cost_per_token
        + usage.cache_read_input_tokens * rates.cache_read_input_cost_per_token
        + usage.cache_creation_input_tokens * rates.cache_creation_input_cost_per_token
    )


def build_attempt_snapshot(
    *,
    model: str,
    deployment_id: str,
    usage: Mapping[str, object],
    rates: AttemptRateSnapshot,
) -> AttemptAccountingSnapshot:
    normalized = normalize_deepseek_usage(usage)
    return AttemptAccountingSnapshot(
        model=model,
        deployment_id=deployment_id,
        rates=rates,
        usage=normalized,
        cost=calculate_attempt_cost(normalized, rates),
    )


__all__ = [
    "AttemptAccountingSnapshot",
    "AttemptRateSnapshot",
    "NormalizedUsage",
    "ParentAccounting",
    "build_attempt_snapshot",
    "calculate_attempt_cost",
    "normalize_deepseek_usage",
]
