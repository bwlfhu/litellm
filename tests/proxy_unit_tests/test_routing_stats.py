from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from litellm.caching.redis_cache import RedisCache
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.observability.observability_endpoints import get_routing_stats
from litellm.proxy.observability.routing_stats import RoutingStatsLogger, RoutingStatsStore


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def zrem(self, key, member):
        self.operations.append(("zrem", key, member))

    def hset(self, key, mapping):
        self.operations.append(("hset", key, mapping))

    def hincrby(self, key, field, value):
        self.operations.append(("hincrby", key, field, value))

    def hincrbyfloat(self, key, field, value):
        self.operations.append(("hincrbyfloat", key, field, value))

    def expire(self, key, ttl):
        self.operations.append(("expire", key, ttl))

    def sadd(self, key, value):
        self.operations.append(("sadd", key, value))

    async def execute(self):
        for operation in self.operations:
            method = getattr(self.client, operation[0])
            result = method(*operation[1:])
            if hasattr(result, "__await__"):
                await result


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.zsets = {}
        self.values = {}

    async def eval(self, script, numkeys, active_key, terminal_key, expires_at, attempt_id, ttl):
        if terminal_key in self.values:
            return 0
        self.zsets.setdefault(active_key, {})[attempt_id] = int(expires_at)
        return 1

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    async def zrem(self, key, member):
        self.zsets.setdefault(key, {}).pop(member, None)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({str(k): str(v) for k, v in mapping.items()})

    async def hincrby(self, key, field, value):
        row = self.hashes.setdefault(key, {})
        row[field] = str(int(row.get(field, "0")) + value)

    async def hincrbyfloat(self, key, field, value):
        row = self.hashes.setdefault(key, {})
        row[field] = str(float(row.get(field, "0")) + value)

    async def expire(self, key, ttl):
        return True

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def smembers(self, key):
        return self.sets.get(key, set())

    async def hgetall(self, key):
        return self.hashes.get(key, {})

    async def zremrangebyscore(self, key, minimum, maximum):
        values = self.zsets.setdefault(key, {})
        for member, score in list(values.items()):
            if score <= int(maximum):
                values.pop(member)

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))


def make_store(monkeypatch):
    redis_cache = RedisCache.__new__(RedisCache)
    redis_cache.namespace = None
    redis_cache.check_and_fix_namespace = lambda key: key
    fake_redis = FakeRedis()
    redis_cache.init_async_client = lambda: fake_redis
    return RoutingStatsStore(redis_cache=redis_cache), fake_redis


@pytest.mark.asyncio
async def test_routing_stats_aggregates_attempts_and_active_leases(monkeypatch):
    store, _ = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "ac-mmkg",
        "api_base": "https://code1.mmkg.cloud/v1",
    }
    now = datetime.now(timezone.utc)

    await store.record_start(metadata, "attempt-active")
    await store.record_terminal(metadata, "attempt-success", True, now, now + timedelta(milliseconds=620))
    await store.record_terminal(metadata, "attempt-failure", False, now, now + timedelta(milliseconds=1840))
    items = await store.query(window_minutes=5)

    assert len(items) == 1
    item = items[0]
    assert item["requests"] == 2
    assert item["upstream_attempts"] == 2
    assert item["success"] == 1
    assert item["failure"] == 1
    assert item["active_requests"] == 1
    assert item["latency_p50_ms"] == 1000
    assert item["latency_p95_ms"] == 2000
    assert item["latency_avg_ms"] == 1230.0


@pytest.mark.asyncio
async def test_routing_stats_exposes_first_in_flight_deployment(monkeypatch):
    store, _ = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-in-flight",
        "model_group": "gpt-5.6-sol",
        "channel": "ac-mmkg",
        "api_base": "https://code1.mmkg.cloud/v1",
    }

    await store.record_start(metadata, "attempt-active")

    items = await store.query(window_minutes=1)
    assert len(items) == 1
    assert items[0]["model_id"] == "deployment-in-flight"
    assert items[0]["requests"] == 0
    assert items[0]["active_requests"] == 1
    assert items[0]["latency_p50_ms"] is None
    assert items[0]["latency_p95_ms"] is None


@pytest.mark.asyncio
async def test_routing_stats_terminal_event_is_idempotent(monkeypatch):
    store, _ = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "ac-mmkg",
        "api_base": "https://code1.mmkg.cloud/v1",
    }
    now = datetime.now(timezone.utc)
    await store.record_terminal(metadata, "attempt-1", True, now, now)
    await store.record_terminal(metadata, "attempt-1", True, now, now)

    assert (await store.query(window_minutes=1))[0]["requests"] == 1


@pytest.mark.asyncio
async def test_routing_stats_filters_by_channel_and_model_group(monkeypatch):
    store, _ = make_store(monkeypatch)
    now = datetime.now(timezone.utc)
    ac_mmkg = {
        "model_id": "deployment-ac",
        "model_group": "gpt-5.6-sol",
        "channel": "ac-mmkg",
        "api_base": "https://code1.mmkg.cloud/v1",
    }
    gaccode = {
        "model_id": "deployment-gaccode",
        "model_group": "claude-opus-5",
        "channel": "gaccode",
        "api_base": "https://gaccode.example/v1",
    }
    await store.record_terminal(ac_mmkg, "attempt-ac", True, now, now)
    await store.record_terminal(gaccode, "attempt-gaccode", True, now, now)

    assert [item["model_id"] for item in await store.query(5, channel="ac-mmkg")] == ["deployment-ac"]
    assert [item["model_id"] for item in await store.query(5, model_group="claude-opus-5")] == [
        "deployment-gaccode"
    ]


def _user(role: LitellmUserRoles) -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_role=role)


@pytest.mark.asyncio
async def test_routing_stats_endpoint_requires_admin_view_permission():
    with pytest.raises(HTTPException) as exc_info:
        await get_routing_stats(user_api_key_dict=_user(LitellmUserRoles.INTERNAL_USER))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_routing_stats_endpoint_returns_503_without_coordination_redis(monkeypatch):
    import litellm.proxy.proxy_server as proxy_server

    monkeypatch.setattr(proxy_server, "redis_usage_cache", None)
    with pytest.raises(HTTPException) as exc_info:
        await get_routing_stats(user_api_key_dict=_user(LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY))

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_routing_stats_endpoint_returns_redis_aggregates(monkeypatch):
    import litellm.proxy.proxy_server as proxy_server

    store, _ = make_store(monkeypatch)
    now = datetime.now(timezone.utc)
    await store.record_terminal(
        {
            "model_id": "deployment-1",
            "model_group": "gpt-5.6-sol",
            "channel": "ac-mmkg",
            "api_base": "https://code1.mmkg.cloud/v1",
        },
        "attempt-1",
        True,
        now,
        now,
    )
    monkeypatch.setattr(proxy_server, "redis_usage_cache", store.redis_cache)

    response = await get_routing_stats(
        window="5m", channel="ac-mmkg", user_api_key_dict=_user(LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY)
    )

    assert response["window"] == "5m"
    assert response["items"][0]["model_id"] == "deployment-1"


def test_routing_stats_logger_ignores_unrouted_calls():
    logger = RoutingStatsLogger.__new__(RoutingStatsLogger)
    assert logger._metadata({"litellm_params": {"metadata": {"model_group": "x"}}}) is None


def test_routing_stats_logger_uses_deployment_access_group_as_channel():
    metadata = RoutingStatsLogger._metadata(
        {
            "litellm_params": {
                "metadata": {"model_group": "gpt-5.6-sol", "api_base": "https://code1.mmkg.cloud/v1"},
                "model_info": {"id": "deployment-1", "access_groups": ["ac-mmkg"]},
            }
        }
    )

    assert metadata == {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "ac-mmkg",
        "api_base": "https://code1.mmkg.cloud/v1",
    }
