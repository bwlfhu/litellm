import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Final

import pytest
import pytest_asyncio

from litellm.caching.redis_cache import RedisCache


@pytest_asyncio.fixture
async def budget_counter_redis() -> AsyncGenerator[RedisCache, None]:
    host: Final = os.environ.get("LITELLM_TEST_REDIS_HOST")
    if host is None:
        pytest.skip("Set LITELLM_TEST_REDIS_HOST to an isolated Redis for Lua integration tests")
    namespace: Final = f"budget-generation-test-{uuid.uuid4().hex}"
    cache: Final = RedisCache(
        host=host,
        port=int(os.environ.get("LITELLM_TEST_REDIS_PORT", "6379")),
        password=os.environ.get("LITELLM_TEST_REDIS_PASSWORD"),
        namespace=namespace,
    )
    client: Final = cache.init_async_client()
    try:
        yield cache
    finally:
        async for key in client.scan_iter(match=f"{namespace}:*"):
            await client.delete(key)
        await client.aclose()


@pytest.mark.asyncio
async def test_spend_counter_generation_preserves_concurrent_increments_and_deduplicates_reset(
    budget_counter_redis: RedisCache,
) -> None:
    cache: Final = budget_counter_redis
    key: Final = "spend:key:concurrent"
    await cache.async_set_cache(key=key, value=100.0, ttl=60)
    generation: Final = await cache.async_get_spend_counter_generation(key)
    assert generation is not None
    await cache.async_increment(key=key, value=7.5, refresh_ttl=True)
    results: Final = await asyncio.gather(
        cache.async_subtract_floor_zero(key=key, value=100.0, expected_generation=generation, ttl=60),
        cache.async_increment(key=key, value=2.5, refresh_ttl=True),
        cache.async_subtract_floor_zero(key=key, value=100.0, expected_generation=generation, ttl=60),
    )
    assert (results[0] is None) != (results[2] is None)
    assert await cache.async_get_cache(key=key) == 10.0
    assert await cache.async_get_spend_counter_generation(key) != generation


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_spend", (7.5, 150.0))
@pytest.mark.parametrize("replacement", ("expiry", "delete", "overwrite", "missing-marker"))
async def test_spend_counter_generation_preserves_reseeded_new_window(
    budget_counter_redis: RedisCache,
    replacement: str,
    replacement_spend: float,
) -> None:
    cache: Final = budget_counter_redis
    client: Final = cache.init_async_client()
    key: Final = "spend:key:reseed"
    namespaced: Final = cache.check_and_fix_namespace(key)
    marker: Final = cache.spend_counter_generation_key(namespaced)
    await cache.async_set_cache(key=key, value=100.0, ttl=60)
    generation: Final = await cache.async_get_spend_counter_generation(key)
    assert generation is not None
    if replacement == "expiry":
        await client.pexpire(namespaced, 1)
        await client.pexpire(marker, 1)
        await asyncio.sleep(0.02)
        assert await client.get(namespaced) is None
        await cache.async_set_cache(key=key, value=replacement_spend, nx=True, ttl=60)
    elif replacement == "delete":
        await cache.async_delete_cache(key=key)
        assert await client.get(marker) is None
        await cache.async_increment(key=key, value=replacement_spend, refresh_ttl=True)
    elif replacement == "missing-marker":
        await client.delete(marker)
        await cache.async_set_max(key=key, value=replacement_spend, ttl=60)
    else:
        await cache.async_set_cache(key=key, value=replacement_spend, ttl=60)
    before: Final = await cache.async_get_cache(key=key)
    assert await cache.async_subtract_floor_zero(key=key, value=100.0, expected_generation=generation, ttl=60) is None
    assert await cache.async_get_cache(key=key) == before


@pytest.mark.asyncio
async def test_spend_counter_generation_ttl_tracks_counter_and_nx_does_not_replace_generation(
    budget_counter_redis: RedisCache,
) -> None:
    cache: Final = budget_counter_redis
    client: Final = cache.init_async_client()
    key: Final = "spend:key:ttl"
    namespaced: Final = cache.check_and_fix_namespace(key)
    marker: Final = cache.spend_counter_generation_key(namespaced)
    await cache.async_set_cache(key=key, value=100.0, ttl=2)
    generation: Final = await cache.async_get_spend_counter_generation(key)
    assert generation is not None
    assert await cache.async_set_cache(key=key, value=0.0, nx=True, ttl=1) is False
    assert await cache.async_get_spend_counter_generation(key) == generation
    assert await client.ttl(namespaced) > 1
    await cache.async_increment(key=key, value=7.5, ttl=60, refresh_ttl=True)
    counter_ttl: Final = await client.pttl(namespaced)
    marker_ttl: Final = await client.pttl(marker)
    assert 0 < marker_ttl <= 2000
    assert abs(counter_ttl - marker_ttl) <= 10
    assert await cache.async_set_max(key=key, value=108.0, ttl=3) == 108.0
    assert await cache.async_get_spend_counter_generation(key) == generation
    assert await client.ttl(marker) == await client.ttl(namespaced)
    assert await cache.async_subtract_floor_zero(key=key, value=100.0, expected_generation=generation, ttl=4) == 8.0
    assert await client.ttl(marker) == await client.ttl(namespaced)


@pytest.mark.asyncio
async def test_spend_counter_pending_reset_eventually_expires_despite_active_writers(
    budget_counter_redis: RedisCache,
) -> None:
    cache: Final = budget_counter_redis
    client: Final = cache.init_async_client()
    key: Final = "spend:key:failed-reconcile"
    namespaced: Final = cache.check_and_fix_namespace(key)
    marker: Final = cache.spend_counter_generation_key(namespaced)
    await cache.async_set_cache(key=key, value=100.0, ttl=1)
    assert await cache.async_get_spend_counter_generation(key) is not None
    await asyncio.sleep(0.05)
    await cache.async_increment(key=key, value=7.5, ttl=60, refresh_ttl=True)
    await cache.async_set_max(key=key, value=108.0, ttl=60)
    assert 0 < await client.pttl(namespaced) < 1000
    await asyncio.sleep(1.0)
    assert await client.get(namespaced) is None
    assert await client.get(marker) is None


@pytest.mark.asyncio
async def test_spend_counter_generation_snapshot_missing_counter_does_not_create_zero(
    budget_counter_redis: RedisCache,
) -> None:
    cache: Final = budget_counter_redis
    key: Final = "spend:key:missing"
    assert await cache.async_get_spend_counter_generation(key) is None
    assert (
        await cache.async_subtract_floor_zero(key=key, value=100.0, expected_generation="expired-generation", ttl=60)
        is None
    )
    assert await cache.async_get_cache(key=key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("write_path", ["bulk_increment", "deferred_increment", "lease_refresh"])
@pytest.mark.parametrize("pending", [False, True])
async def test_all_spend_writers_preserve_pending_deadline_and_generation_ttl(
    budget_counter_redis, write_path, pending
):
    from litellm.caching.redis_batch import RedisBatch

    cache = budget_counter_redis
    client = cache.init_async_client()
    key = "spend:key:writer-contract"
    namespaced = cache.check_and_fix_namespace(key)
    marker = cache.spend_counter_generation_key(namespaced)
    await cache.async_set_cache(key=key, value=100.0, ttl=60)
    generation = await cache.async_get_spend_counter_generation(key, prepare_reset=pending)
    await client.pexpire(namespaced, 1000)
    await client.pexpire(marker, 1000)
    for _ in range(3):
        if write_path == "bulk_increment":
            result = await cache.async_increment_pipeline([{"key": key, "increment_value": 2.0, "ttl": 60}])
            assert result is not None and len(result) == 1
        elif write_path == "deferred_increment":
            assert await RedisBatch(cache).increment(key, 2.0, 60) > 100.0
        else:
            assert await cache.async_refresh_ttl(key, 60) is True
    ttl = await client.pttl(namespaced)
    assert 0 < ttl <= 1000 if pending else ttl > 59000
    assert abs(ttl - await client.pttl(marker)) <= 20
    assert await cache.async_get_spend_counter_generation(key, prepare_reset=False) == generation
    assert await cache.async_get_cache(key) == (100.0 if write_path == "lease_refresh" else 106.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_path", ["bulk_set", "bulk_set_with_ttls", "deferred_set"])
async def test_all_bulk_set_and_delete_paths_rotate_generation(budget_counter_redis, write_path):
    from litellm.caching.redis_batch import RedisBatch

    cache = budget_counter_redis
    client = cache.init_async_client()
    key = "spend:key:bulk-set"
    await cache.async_set_cache(key=key, value=100.0, ttl=60)
    old_generation = await cache.async_get_spend_counter_generation(key)
    if write_path == "bulk_set":
        await cache.async_set_cache_pipeline([(key, 7.5)], ttl=60)
    elif write_path == "bulk_set_with_ttls":
        await cache.async_set_cache_pipeline_with_ttls([(key, 7.5, 60)])
    else:
        await RedisBatch(cache).set(key, 7.5, 60)
    new_generation = await cache.async_get_spend_counter_generation(key, prepare_reset=False)
    assert new_generation is not None and new_generation != old_generation
    assert await cache.async_subtract_floor_zero(key, 100.0, old_generation, ttl=60) is None
    assert await cache.async_get_cache(key) == 7.5
    await RedisBatch(cache).delete(key)
    assert await client.get(cache.spend_counter_generation_key(cache.check_and_fix_namespace(key))) is None
    assert await cache.async_get_cache(key) is None
    assert await cache.async_refresh_ttl(key, 60) is False
