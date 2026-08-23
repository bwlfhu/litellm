"""Read-only control-plane observability endpoints."""

import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Final, Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status

from litellm.proxy._types import UserAPIKeyAuth, user_api_key_has_admin_view
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.observability.routing_stats import RoutingStatsStore
from litellm.router_utils.cooldown_cache import CooldownCache

router = APIRouter()


class _RedisStatusCache(Protocol):
    async def async_batch_get_cache(self, key_list: list[str]) -> Mapping[str, object]: ...

    async def async_get_cache(self, key: str) -> object: ...


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[str, object], value)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: object) -> int | None:
    parsed: Final = _as_float(value)
    return None if parsed is None else int(parsed)


def _format_timestamp(value: object) -> str | None:
    timestamp: Final = _as_float(value)
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _deployment_identity(deployment: Mapping[str, object], model: str | None) -> tuple[str, str] | None:
    model_info_raw: Final = deployment.get("model_info")
    model_info: Final = _as_mapping(model_info_raw) or {}
    deployment_id_raw: Final = model_info.get("id")
    model_group_raw: Final = deployment.get("model_name")
    deployment_id: Final = str(deployment_id_raw) if deployment_id_raw is not None else ""
    model_group: Final = str(model_group_raw) if model_group_raw is not None else ""
    team_public_model_name: Final = model_info.get("team_public_model_name")
    names: Final = frozenset(
        name
        for name in (
            deployment_id,
            model_group,
            team_public_model_name if isinstance(team_public_model_name, str) else None,
        )
        if isinstance(name, str) and name
    )
    if not deployment_id or not model_group or (model is not None and model not in names):
        return None
    return deployment_id, model_group


def _get_status_deployments(llm_router: object, model: str | None) -> tuple[tuple[str, str], ...]:
    get_model_list: Final = getattr(llm_router, "get_model_list", None)
    if not callable(get_model_list):
        return ()
    raw_deployments: Final = get_model_list()
    if not isinstance(raw_deployments, list):
        return ()
    deployments: Final = cast(list[object], raw_deployments)
    identities: Final = tuple(
        identity
        for deployment in deployments
        for deployment_mapping in (_as_mapping(deployment),)
        if deployment_mapping is not None
        for identity in (_deployment_identity(deployment=deployment_mapping, model=model),)
        if identity is not None
    )
    return tuple(dict.fromkeys(identities))


def _build_cooldown_status(value: object, now: float) -> dict[str, object] | None:
    cooldown_value: Final = _as_mapping(value)
    if cooldown_value is None:
        return None
    timestamp: Final = _as_float(cooldown_value.get("timestamp"))
    cooldown_time: Final = _as_float(cooldown_value.get("cooldown_time"))
    if timestamp is None or cooldown_time is None:
        return None
    remaining_seconds: Final = timestamp + cooldown_time - now
    if remaining_seconds <= 0:
        return None
    return {
        "status_code": _as_int(cooldown_value.get("status_code")),
        "message": (
            cooldown_value.get("exception_received")
            if isinstance(cooldown_value.get("exception_received"), str)
            else None
        ),
        "started_at": _format_timestamp(timestamp),
        "until": _format_timestamp(timestamp + cooldown_time),
        "remaining_seconds": round(remaining_seconds, 3),
    }


def _build_health_status(
    value: object,
    now: float,
    staleness_threshold: float,
) -> dict[str, object] | None:
    health_value: Final = _as_mapping(value)
    if health_value is None or health_value.get("is_healthy") is not False:
        return None
    timestamp: Final = _as_float(health_value.get("timestamp"))
    if timestamp is None or now - timestamp >= staleness_threshold:
        return None
    return {
        "reason": health_value.get("reason") if isinstance(health_value.get("reason"), str) else None,
        "checked_at": _format_timestamp(timestamp),
    }


def _build_model_status_item(
    deployment_id: str,
    model_group: str,
    cooldown_value: object,
    health_value: object,
    now: float,
    staleness_threshold: float,
) -> dict[str, object] | None:
    cooldown: Final = _build_cooldown_status(value=cooldown_value, now=now)
    health: Final = _build_health_status(
        value=health_value,
        now=now,
        staleness_threshold=staleness_threshold,
    )
    if cooldown is None and health is None:
        return None
    states: Final = tuple(
        state for state, value in (("cooldown", cooldown), ("health_unhealthy", health)) if value is not None
    )
    return {
        "model_group": model_group,
        "deployment_id": deployment_id,
        "states": states,
        "cooldown": cooldown,
        "health": health,
    }


@router.get(
    "/observability/routing-stats",
    tags=["observability"],
)
async def get_routing_stats(
    window: str = Query(default="5m", pattern="^(1m|5m|15m)$"),
    channel: str | None = Query(default=None),
    model_group: str | None = Query(default=None),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """Return current deployment routing aggregates from coordination Redis."""
    if not user_api_key_has_admin_view(user_api_key_dict):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin view permission required")

    from litellm.proxy.proxy_server import redis_usage_cache

    if redis_usage_cache is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Routing statistics require a configured coordination Redis instance",
        )

    window_minutes = int(window.removesuffix("m"))
    items = await RoutingStatsStore(redis_cache=redis_usage_cache).query(
        window_minutes=window_minutes,
        channel=channel,
        model_group=model_group,
    )
    return {
        "window": window,
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": items,
    }


@router.get(
    "/observability/model-status",
    tags=["observability"],
)
async def get_model_status(
    model: str | None = Query(default=None, min_length=1),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """Return active model exceptions stored in the Router's Redis cache."""
    if not user_api_key_has_admin_view(user_api_key_dict):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin view permission required")

    from litellm.proxy.proxy_server import llm_router, redis_usage_cache

    if llm_router is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model status requires an initialized LiteLLM Router",
        )

    router_redis_cache: Final = getattr(getattr(llm_router, "cache", None), "redis_cache", None)
    redis_cache_value: Final = router_redis_cache if router_redis_cache is not None else redis_usage_cache
    if redis_cache_value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model status requires a configured Router Redis instance",
        )

    deployments: Final = _get_status_deployments(llm_router=llm_router, model=model)
    if not deployments:
        return {
            "source": "redis",
            "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "router_deployments": 0,
            "returned": 0,
            "items": (),
        }
    redis_cache: Final = cast(_RedisStatusCache, redis_cache_value)
    cooldown_keys: Final = [CooldownCache.get_cooldown_cache_key(deployment_id) for deployment_id, _ in deployments]
    health_cache_key: Final = "litellm:health_check:deployment_health_state"
    try:
        cooldown_values_raw: Final = await redis_cache.async_batch_get_cache(key_list=cooldown_keys)
        health_values_raw: Final = await redis_cache.async_get_cache(key=health_cache_key)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to read model status from Redis",
        ) from exc

    cooldown_values: Final = _as_mapping(cooldown_values_raw) or {}
    health_values: Final = _as_mapping(health_values_raw) or {}
    now: Final = time.time()
    health_cache: Final = getattr(getattr(llm_router, "health_state_cache", None), "staleness_threshold", 0.0)
    staleness_threshold: Final = _as_float(health_cache) or 0.0
    items: Final = tuple(
        item
        for deployment_id, model_group in deployments
        for item in (
            _build_model_status_item(
                deployment_id=deployment_id,
                model_group=model_group,
                cooldown_value=cooldown_values.get(CooldownCache.get_cooldown_cache_key(deployment_id)),
                health_value=health_values.get(deployment_id),
                now=now,
                staleness_threshold=staleness_threshold,
            ),
        )
        if item is not None
    )
    return {
        "source": "redis",
        "as_of": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "router_deployments": len(deployments),
        "returned": len(items),
        "items": items,
    }
