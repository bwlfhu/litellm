from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from litellm.caching.redis_cache import RedisCache
from litellm.utils import Rules, function_setup
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.observability.observability_endpoints import get_routing_stats, router as observability_router
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

    def zadd(self, key, mapping):
        self.operations.append(("zadd", key, mapping))

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

    async def eval(self, script, numkeys, *args):
        if numkeys == 2:
            active_key, terminal_key, expires_at, attempt_id, _ttl = args
            if terminal_key in self.values:
                return 0
            self.zsets.setdefault(active_key, {})[attempt_id] = int(expires_at)
            return 1

        active_key, terminal_key, bucket_key, attempt_id, _lease_ttl, _retention_ttl, *fields = args
        if terminal_key in self.values:
            return 0
        self.values[terminal_key] = "1"
        self.zsets.setdefault(active_key, {}).pop(attempt_id, None)
        model_id, model_group, channel, api_base, last_seen_ms, metric_field, duration_ms, latency_field = fields
        await self.hset(
            bucket_key,
            {
                "model_id": model_id,
                "model_group": model_group,
                "channel": channel,
                "api_base": api_base,
                "last_seen_ms": last_seen_ms,
            },
        )
        await self.hincrby(bucket_key, "requests", 1)
        await self.hincrby(bucket_key, metric_field, 1)
        await self.hincrbyfloat(bucket_key, "latency_sum_ms", duration_ms)
        await self.hincrby(bucket_key, latency_field, 1)
        return 1

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

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update({str(member): int(score) for member, score in mapping.items()})

    async def smembers(self, key):
        return self.sets.get(key, set())

    async def zrangebyscore(self, key, minimum, maximum):
        lower = int(minimum)
        return [member for member, score in self.zsets.get(key, {}).items() if score >= lower]

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
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    now = datetime.now(timezone.utc)

    await store.record_start(metadata, "attempt-primarytive")
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
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
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
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    now = datetime.now(timezone.utc)
    await store.record_terminal(metadata, "attempt-1", True, now, now)
    await store.record_terminal(metadata, "attempt-1", True, now, now)

    assert (await store.query(window_minutes=1))[0]["requests"] == 1


@pytest.mark.asyncio
async def test_routing_stats_counts_same_deployment_retries(monkeypatch):
    store, _ = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    now = datetime.now(timezone.utc)

    await store.record_start(metadata, "retry-1")
    await store.record_terminal(metadata, "retry-1", False, now, now)
    await store.record_start(metadata, "retry-2")
    await store.record_terminal(metadata, "retry-2", True, now, now)

    item = (await store.query(window_minutes=1))[0]
    assert item["requests"] == 2
    assert item["success"] == 1
    assert item["failure"] == 1
    assert item["active_requests"] == 0


@pytest.mark.asyncio
async def test_routing_stats_keeps_active_request_visible_across_minute_boundary(monkeypatch):
    store, fake_redis = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-in-flight",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    monkeypatch.setattr(store, "_bucket", lambda timestamp=None: 100)
    await store.record_start(metadata, "attempt-primarytive")
    monkeypatch.setattr(store, "_bucket", lambda timestamp=None: 101)

    item = (await store.query(window_minutes=1))[0]
    assert item["model_id"] == "deployment-in-flight"
    assert item["requests"] == 0
    assert item["active_requests"] == 1
    assert fake_redis.zsets[store._active_index_key()]


@pytest.mark.asyncio
async def test_routing_stats_prunes_expired_active_index_tokens(monkeypatch):
    store, fake_redis = make_store(monkeypatch)
    stale_token = store._token("removed-deployment")
    fake_redis.zsets[store._active_index_key()] = {stale_token: 0}
    monkeypatch.setattr("litellm.proxy.observability.routing_stats.time.time", lambda: 1)

    assert await store.query(window_minutes=1) == []
    assert stale_token not in fake_redis.zsets[store._active_index_key()]


@pytest.mark.asyncio
async def test_routing_stats_retries_failed_redis_terminal_write(monkeypatch):
    store, fake_redis = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    now = datetime.now(timezone.utc)
    original_eval = fake_redis.eval
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("transient")
        return await original_eval(*args, **kwargs)

    fake_redis.eval = AsyncMock(side_effect=fail_once)
    monkeypatch.setattr("litellm.proxy.observability.routing_stats.asyncio.sleep", AsyncMock())

    await store.record_terminal(metadata, "attempt-1", True, now, now)

    assert fake_redis.eval.await_count == 2
    assert (await store.query(window_minutes=1))[0]["requests"] == 1


@pytest.mark.asyncio
async def test_routing_stats_marks_latency_overflow_without_underreporting(monkeypatch):
    store, _ = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    now = datetime.now(timezone.utc)
    await store.record_terminal(metadata, "attempt-1", True, now, now + timedelta(seconds=121))

    item = (await store.query(window_minutes=1))[0]
    assert item["latency_p50_ms"] is None
    assert item["latency_p95_ms"] is None
    assert item["latency_max_ms"] is None
    assert item["latency_overflow_count"] == 1


@pytest.mark.asyncio
async def test_routing_stats_treats_all_percentiles_as_unknown_with_mixed_overflow_samples(monkeypatch):
    store, _ = make_store(monkeypatch)
    metadata = {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    now = datetime.now(timezone.utc)
    for attempt in range(20):
        await store.record_terminal(metadata, f"fast-{attempt}", True, now, now + timedelta(milliseconds=100))
    await store.record_terminal(metadata, "slow", True, now, now + timedelta(seconds=121))

    item = (await store.query(window_minutes=1))[0]
    assert item["latency_p50_ms"] is None
    assert item["latency_p95_ms"] is None
    assert item["latency_max_ms"] is None
    assert item["latency_overflow_count"] == 1


def test_routing_stats_snapshot_reaches_callback_payload_without_reaching_provider_kwargs():
    snapshot = {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    kwargs = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hello"}],
        "litellm_call_id": "routing-stats-test",
        "_litellm_routing_stats_metadata": snapshot,
    }

    logging_obj, provider_kwargs = function_setup(
        original_function="acompletion",
        rules_obj=Rules(),
        start_time=datetime.now(timezone.utc),
        **kwargs,
    )

    assert provider_kwargs.get("_litellm_routing_stats_metadata") is None
    assert logging_obj.model_call_details["litellm_params"]["routing_stats_metadata"] == snapshot


@pytest.mark.asyncio
async def test_routing_stats_filters_by_channel_and_model_group(monkeypatch):
    store, _ = make_store(monkeypatch)
    now = datetime.now(timezone.utc)
    primary_channel = {
        "model_id": "deployment-primary",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test/v1",
    }
    gaccode = {
        "model_id": "deployment-gaccode",
        "model_group": "claude-opus-5",
        "channel": "gaccode",
        "api_base": "https://gaccode.example/v1",
    }
    await store.record_terminal(primary_channel, "attempt-primary", True, now, now)
    await store.record_terminal(gaccode, "attempt-gaccode", True, now, now)

    assert [item["model_id"] for item in await store.query(5, channel="primary-channel")] == ["deployment-primary"]
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
            "channel": "primary-channel",
            "api_base": "https://routing.example.test/v1",
        },
        "attempt-1",
        True,
        now,
        now,
    )
    monkeypatch.setattr(proxy_server, "redis_usage_cache", store.redis_cache)

    response = await get_routing_stats(
        window="5m",
        channel="primary-channel",
        model_group=None,
        user_api_key_dict=_user(LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY),
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
                "routing_stats_metadata": {
                    "model_id": "deployment-1",
                    "model_group": "gpt-5.6-sol",
                    "channel": "primary-channel",
                    "api_base": "https://routing.example.test/v1",
                }
            }
        }
    )

    assert metadata == {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test",
    }


def test_routing_stats_logger_supports_responses_litellm_metadata_and_sanitizes_url():
    metadata = RoutingStatsLogger._metadata(
        {
            "litellm_params": {
                "routing_stats_metadata": {
                    "model_id": "deployment-1",
                    "model_group": "gpt-5.6-sol",
                    "channel": "primary-channel",
                    "api_base": "https://user:password@routing.example.test/v1?key=secret#fragment",
                }
            }
        }
    )

    assert metadata == {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test",
    }


def test_routing_stats_logger_rejects_responses_requester_metadata_spoofing():
    metadata = RoutingStatsLogger._metadata(
        {
            "litellm_params": {
                "metadata": {
                    "model_group": "caller-spoofed",
                    "api_base": "https://caller.invalid/v1?key=secret",
                },
                "litellm_metadata": {
                    "model_group": "gpt-5.6-sol",
                    "model_info": {"id": "deployment-1", "access_groups": ["primary-channel"]},
                    "api_base": "https://routing.example.test/v1",
                },
                "routing_stats_metadata": {
                    "model_id": "deployment-1",
                    "model_group": "gpt-5.6-sol",
                    "channel": "primary-channel",
                    "api_base": "https://routing.example.test/v1",
                },
            }
        }
    )

    assert metadata == {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test",
    }


def test_routing_stats_logger_rejects_chat_requester_litellm_metadata_spoofing():
    metadata = RoutingStatsLogger._metadata(
        {
            "litellm_params": {
                "metadata": {
                    "model_group": "gpt-5.6-sol",
                    "model_info": {"id": "deployment-1", "access_groups": ["primary-channel"]},
                    "api_base": "https://routing.example.test/v1",
                },
                "litellm_metadata": {
                    "model_group": "caller-spoofed",
                    "api_base": "https://caller.invalid/v1?key=secret",
                },
                "routing_stats_metadata": {
                    "model_id": "deployment-1",
                    "model_group": "gpt-5.6-sol",
                    "channel": "primary-channel",
                    "api_base": "https://routing.example.test/v1",
                },
            }
        }
    )

    assert metadata == {
        "model_id": "deployment-1",
        "model_group": "gpt-5.6-sol",
        "channel": "primary-channel",
        "api_base": "https://routing.example.test",
    }


def test_routing_stats_logger_creates_distinct_ids_for_same_deployment_retry(monkeypatch):
    logger = RoutingStatsLogger.__new__(RoutingStatsLogger)

    class StoreStub:
        async def record_start(self, **kwargs):
            return None

    logger._store = StoreStub()
    scheduled = []
    monkeypatch.setattr(logger, "_schedule", lambda coroutine: (coroutine.close(), scheduled.append(coroutine)))
    kwargs = {
        "litellm_params": {
            "routing_stats_metadata": {
                "model_id": "deployment-1",
                "model_group": "gpt-5.6-sol",
                "channel": "primary-channel",
                "api_base": "https://routing.example.test/v1",
            },
        }
    }
    monkeypatch.setattr("litellm.proxy.observability.routing_stats.uuid.uuid4", lambda: "attempt-one")
    logger.log_pre_api_call("model", [], kwargs)
    monkeypatch.setattr("litellm.proxy.observability.routing_stats.uuid.uuid4", lambda: "attempt-two")
    logger.log_pre_api_call("model", [], kwargs)

    assert kwargs["litellm_params"]["routing_stats_attempt_id"] == "attempt-two"
    assert len(scheduled) == 2


def test_routing_stats_uses_one_redis_cluster_slot_per_terminal_attempt(monkeypatch):
    store, _ = make_store(monkeypatch)
    token = store._token("deployment-1")

    assert "{" + token + "}" in store._active_key(token)
    assert "{" + token + "}" in store._terminal_key(token, "attempt-1")
    assert "{" + token + "}" in store._bucket_key(1, token)


def test_routing_stats_asgi_validates_window_parameter(monkeypatch):
    import litellm.proxy.proxy_server as proxy_server

    store, _ = make_store(monkeypatch)
    monkeypatch.setattr(proxy_server, "redis_usage_cache", store.redis_cache)
    app = FastAPI()
    app.include_router(observability_router)
    app.dependency_overrides[user_api_key_auth] = lambda: _user(LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY)

    with TestClient(app) as client:
        assert client.get("/observability/routing-stats?window=1m").status_code == 200
        assert client.get("/observability/routing-stats?window=2m").status_code == 422
