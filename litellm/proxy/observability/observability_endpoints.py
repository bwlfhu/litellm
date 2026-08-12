"""Read-only control-plane observability endpoints."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from litellm.proxy._types import UserAPIKeyAuth, user_api_key_has_admin_view
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.observability.routing_stats import RoutingStatsStore

router = APIRouter()


@router.get(
    "/observability/routing-stats",
    tags=["observability"],
)
async def get_routing_stats(
    window: str = Query(default="5m", pattern="^(1m|5m|15m)$"),
    channel: Optional[str] = Query(default=None),
    model_group: Optional[str] = Query(default=None),
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
