import asyncio
import importlib
import os
import sys
import types
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Final

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from litellm.caching.dual_cache import DualCache
from litellm.caching.redis_cache import RedisCache
from litellm.proxy._types import LiteLLM_VerificationToken
from litellm.proxy.common_utils.reset_budget_job import ResetBudgetJob


class MockProxyLogging:
    class MockServiceLogging:
        async def async_service_success_hook(self, **kwargs):
            pass

        async def async_service_failure_hook(self, **kwargs):
            pass

    def __init__(self):
        self.service_logging_obj = self.MockServiceLogging()


class _BudgetResetSQLClient:
    def __init__(self, connection: AsyncConnection):
        self.connection = connection
        self.db = self
        self.after_read: Callable[[], Awaitable[None]] | None = None
        self.after_write: Callable[[], Awaitable[None]] | None = None
        self.after_counter_read: Callable[[], Awaitable[None]] | None = None
        self.before_write: Callable[[], Awaitable[None]] | None = None
        self.reject_write = False
        self.litellm_verificationtoken = self
        self.proxy_server = None

    async def find_unique(self, where):
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute('SELECT spend FROM "LiteLLM_VerificationToken" WHERE token = %s', (where["token"],))
            row = await cursor.fetchone()
        if self.after_counter_read is not None:
            await self.after_counter_read()
        return types.SimpleNamespace(**row) if row is not None else None

    async def get_data(self, **kwargs):
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                'SELECT token, spend, budget_reset_at FROM "LiteLLM_VerificationToken" '
                "WHERE budget_reset_at <= CURRENT_TIMESTAMP"
            )
            rows = await cursor.fetchall()
        if self.after_read is not None:
            await self.after_read()
        return [LiteLLM_VerificationToken(**row, budget_duration="1d") for row in rows]

    async def query_raw(self, query, *args):
        if self.reject_write:
            raise RuntimeError("synthetic DB write rejected")
        if self.before_write is not None:
            await self.before_write()
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query.replace("$1", "%s"), args)
            rows = await cursor.fetchall()
        if self.after_write is not None:
            await self.after_write()
        return rows

    async def spend(self) -> float:
        async with self.connection.cursor() as cursor:
            await cursor.execute('SELECT spend FROM "LiteLLM_VerificationToken" WHERE token = %s', ("synthetic",))
            row = await cursor.fetchone()
        assert row is not None
        return float(row[0])

    async def increment(self, value: float) -> None:
        await self.connection.execute(
            'UPDATE "LiteLLM_VerificationToken" SET spend = spend + %s WHERE token = %s',
            (value, "synthetic"),
        )


@pytest_asyncio.fixture
async def budget_reset_stores(
    monkeypatch,
) -> AsyncGenerator[tuple[_BudgetResetSQLClient, _BudgetResetSQLClient, RedisCache], None]:
    database_url: Final = os.environ.get("LITELLM_TEST_DATABASE_URL")
    redis_host: Final = os.environ.get("LITELLM_TEST_REDIS_HOST")
    if database_url is None or redis_host is None:
        pytest.skip("Set isolated LITELLM_TEST_DATABASE_URL and LITELLM_TEST_REDIS_HOST for budget integration")
    schema: Final = f"budget_reset_test_{uuid.uuid4().hex}"
    connection: Final = await AsyncConnection.connect(database_url, autocommit=True)
    follower_connection: Final = await AsyncConnection.connect(database_url, autocommit=True)
    cache: Final = RedisCache(
        host=redis_host,
        port=int(os.environ.get("LITELLM_TEST_REDIS_PORT", "6379")),
        password=os.environ.get("LITELLM_TEST_REDIS_PASSWORD"),
        namespace=schema,
    )
    redis: Final = cache.init_async_client()
    try:
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        for conn in (connection, follower_connection):
            await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        await connection.execute(
            'CREATE TABLE "LiteLLM_VerificationToken" ('
            "token text PRIMARY KEY, spend double precision, budget_reset_at timestamp)"
        )
        await connection.execute(
            'INSERT INTO "LiteLLM_VerificationToken" VALUES (%s, %s, %s)',
            ("synthetic", 100.0, datetime.now(timezone.utc) - timedelta(minutes=1)),
        )
        await cache.async_set_cache(key="spend:key:synthetic", value=100.0, ttl=60)
        primary: Final = _BudgetResetSQLClient(connection)
        secondary: Final = _BudgetResetSQLClient(follower_connection)
        real_proxy: Final = importlib.import_module("litellm.proxy.proxy_server")
        proxy_module: Final = types.ModuleType("litellm.proxy.proxy_server")
        proxy_module.spend_counter_cache = DualCache(redis_cache=cache)
        proxy_module.user_api_key_cache = DualCache()
        monkeypatch.setattr(real_proxy, "spend_counter_cache", proxy_module.spend_counter_cache)
        monkeypatch.setattr(real_proxy, "prisma_client", primary)
        primary.proxy_server = real_proxy
        monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", proxy_module)
        yield primary, secondary, cache
    finally:
        async for key in redis.scan_iter(match=f"{schema}:*"):
            await redis.delete(key)
        await redis.aclose()
        await connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
        await connection.close()
        await follower_connection.close()


@pytest.mark.asyncio
async def test_key_budget_reset_db_cas_preserves_racing_spend_and_rejects_stale_worker(budget_reset_stores):
    primary, secondary, cache = budget_reset_stores
    follower_read: Final = asyncio.Event()
    release_follower: Final = asyncio.Event()

    async def pause_follower() -> None:
        follower_read.set()
        await release_follower.wait()

    async def write_after_primary_snapshot() -> None:
        await primary.increment(7.5)
        await cache.async_increment(key="spend:key:synthetic", value=7.5, refresh_ttl=True)

    secondary.after_read = pause_follower
    primary.after_read = write_after_primary_snapshot
    first_job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
    stale_job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=secondary)
    follower_task: Final = asyncio.create_task(stale_job.reset_budget_for_litellm_keys())
    try:
        await asyncio.wait_for(follower_read.wait(), timeout=5)
        await first_job.reset_budget_for_litellm_keys()
        assert await primary.spend() == 7.5
        assert await cache.async_get_cache(key="spend:key:synthetic") == 7.5
        await primary.increment(5.0)
        await cache.async_increment(key="spend:key:synthetic", value=5.0, refresh_ttl=True)
    finally:
        release_follower.set()
        await follower_task
    assert await primary.spend() == 12.5
    assert await cache.async_get_cache(key="spend:key:synthetic") == 12.5


@pytest.mark.asyncio
async def test_key_budget_reset_db_commit_then_redis_expiry_and_reseed_preserves_spend(budget_reset_stores):
    primary, _, cache = budget_reset_stores
    key: Final = "spend:key:synthetic"

    async def reseed_after_commit() -> None:
        await primary.increment(7.5)
        client: Final = cache.init_async_client()
        namespaced: Final = cache.check_and_fix_namespace(key)
        await client.pexpire(namespaced, 1)
        await client.pexpire(cache._spend_counter_generation_key(namespaced), 1)
        await asyncio.sleep(0.02)
        assert await cache.async_get_cache(key=key) is None
        await cache.async_set_cache(key=key, value=await primary.spend(), nx=True, ttl=60)

    primary.after_write = reseed_after_commit
    job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
    await job.reset_budget_for_litellm_keys()
    assert await primary.spend() == 7.5
    assert await cache.async_get_cache(key=key) == 7.5


@pytest.mark.asyncio
async def test_key_budget_reset_rejected_db_commit_preserves_redis_spend(budget_reset_stores):
    primary, _, cache = budget_reset_stores
    primary.reject_write = True
    job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
    await job.reset_budget_for_litellm_keys()
    assert await primary.spend() == 100.0
    assert await cache.async_get_cache(key="spend:key:synthetic") == 100.0


class _UnavailableResetRedis:
    def __init__(self, cache: RedisCache, fail_snapshot: bool):
        self.cache = cache
        self.fail_snapshot = fail_snapshot

    async def async_get_spend_counter_generation(self, key: str) -> str | None:
        if self.fail_snapshot:
            raise ConnectionError("synthetic snapshot failure")
        return await self.cache.async_get_spend_counter_generation(key)

    async def async_subtract_floor_zero(self, **kwargs) -> float | None:
        raise ConnectionError("synthetic reconciliation failure")


@pytest.mark.asyncio
async def test_key_budget_reset_unavailable_generation_does_not_advance_db_window(budget_reset_stores):
    primary, _, cache = budget_reset_stores
    from litellm.proxy.proxy_server import spend_counter_cache

    spend_counter_cache.redis_cache = _UnavailableResetRedis(cache, fail_snapshot=True)
    job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
    await job.reset_budget_for_litellm_keys()
    assert await primary.spend() == 100.0
    assert await cache.async_get_cache(key="spend:key:synthetic") == 100.0


@pytest.mark.asyncio
async def test_key_budget_reset_failed_redis_reconciliation_cannot_extend_old_spend_forever(budget_reset_stores):
    primary, _, cache = budget_reset_stores
    from litellm.proxy.proxy_server import spend_counter_cache

    key: Final = "spend:key:synthetic"
    await cache.async_set_cache(key=key, value=100.0, ttl=1)
    spend_counter_cache.redis_cache = _UnavailableResetRedis(cache, fail_snapshot=False)

    async def concurrent_spend() -> None:
        await primary.increment(7.5)
        await cache.async_increment(key=key, value=7.5, refresh_ttl=True)

    primary.after_write = concurrent_spend
    job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
    await job.reset_budget_for_litellm_keys()
    assert await primary.spend() == 7.5
    assert await cache.async_get_cache(key=key) == 107.5
    await cache.async_increment(key=key, value=2.5, refresh_ttl=True)
    await primary.increment(2.5)
    assert await cache.init_async_client().ttl(cache.check_and_fix_namespace(key)) <= 1
    await asyncio.sleep(1.1)
    assert await cache.async_get_cache(key=key) is None
    await cache.async_set_cache(key=key, value=await primary.spend(), nx=True, ttl=60)
    assert await cache.async_get_cache(key=key) == 10.0


@pytest.mark.asyncio
async def test_key_budget_reset_rejects_repair_that_read_old_window_before_reset(budget_reset_stores):
    primary, _, cache = budget_reset_stores
    key: Final = "spend:key:synthetic"
    repair_read: Final = asyncio.Event()
    release_repair: Final = asyncio.Event()

    async def pause_repair() -> None:
        repair_read.set()
        await release_repair.wait()

    primary.after_counter_read = pause_repair
    repair_task: Final = asyncio.create_task(primary.proxy_server._repair_stale_spend_counter(key, 100.0))
    try:
        await asyncio.wait_for(repair_read.wait(), timeout=5)
        job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
        await job.reset_budget_for_litellm_keys()
        await primary.increment(7.5)
        await cache.async_increment(key=key, value=7.5, refresh_ttl=True)
    finally:
        release_repair.set()
        repair_result: Final = await repair_task
    assert repair_result == 7.5
    assert await primary.spend() == 7.5
    assert await cache.async_get_cache(key=key) == 7.5
    assert primary.proxy_server.spend_counter_cache.in_memory_cache.get_cache(key=key) != 100.0


@pytest.mark.asyncio
async def test_key_budget_admission_uses_fresh_window_after_stale_db_floor(budget_reset_stores):
    primary, _, cache = budget_reset_stores
    key: Final = "spend:key:synthetic"
    floor_read: Final = asyncio.Event()
    release_floor: Final = asyncio.Event()

    async def pause_floor() -> None:
        floor_read.set()
        await release_floor.wait()

    await cache.async_set_cache(key=key, value=0.0, ttl=60)
    primary.after_counter_read = pause_floor
    admission: Final = asyncio.create_task(primary.proxy_server.get_current_spend(key, 100.0, max_budget=50.0))
    try:
        await asyncio.wait_for(floor_read.wait(), timeout=5)
        job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
        await job.reset_budget_for_litellm_keys()
        await primary.increment(7.5)
        await cache.async_increment(key=key, value=7.5, refresh_ttl=True)
    finally:
        release_floor.set()
        admitted_spend: Final = await admission
    assert admitted_spend == 7.5
    assert await cache.async_get_cache(key=key) == 7.5


@pytest.mark.asyncio
async def test_key_budget_reset_cold_counter_blocks_late_old_window_reseed(budget_reset_stores):
    from litellm.proxy.db.spend_counter_reseed import SpendCounterReseed

    primary, _, cache = budget_reset_stores
    key: Final = "spend:key:synthetic"
    seed_read: Final = asyncio.Event()
    release_seed: Final = asyncio.Event()

    async def pause_seed() -> None:
        seed_read.set()
        await release_seed.wait()

    await cache.async_delete_cache(key=key)
    primary.after_counter_read = pause_seed
    seed_task: Final = asyncio.create_task(
        SpendCounterReseed.coalesced(
            prisma_client=primary,
            spend_counter_cache=primary.proxy_server.spend_counter_cache,
            counter_key=key,
        )
    )
    try:
        await asyncio.wait_for(seed_read.wait(), timeout=5)
        job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
        await job.reset_budget_for_litellm_keys()
    finally:
        release_seed.set()
        seeded: Final = await seed_task
    assert seeded == 0.0
    await primary.increment(7.5)
    await cache.async_increment(key=key, value=7.5, refresh_ttl=True)
    assert await primary.spend() == 7.5
    assert await cache.async_get_cache(key=key) == 7.5


@pytest.mark.asyncio
async def test_key_budget_reset_counter_expiry_after_snapshot_blocks_late_old_window_reseed(budget_reset_stores):
    from litellm.proxy.db.spend_counter_reseed import SpendCounterReseed

    primary, _, cache = budget_reset_stores
    key: Final = "spend:key:synthetic"
    reset_ready: Final = asyncio.Event()
    release_reset: Final = asyncio.Event()
    seed_ready: Final = asyncio.Event()
    release_seed: Final = asyncio.Event()

    async def pause_reset() -> None:
        reset_ready.set()
        await release_reset.wait()

    async def pause_seed() -> None:
        seed_ready.set()
        await release_seed.wait()

    primary.before_write = pause_reset
    primary.after_counter_read = pause_seed
    job: Final = ResetBudgetJob(proxy_logging_obj=MockProxyLogging(), prisma_client=primary)
    reset_task: Final = asyncio.create_task(job.reset_budget_for_litellm_keys())
    await asyncio.wait_for(reset_ready.wait(), timeout=5)
    redis: Final = cache.init_async_client()
    namespaced: Final = cache.check_and_fix_namespace(key)
    await redis.pexpire(namespaced, 1)
    await redis.pexpire(cache._spend_counter_generation_key(namespaced), 1)
    await asyncio.sleep(0.02)
    seed_task: Final = asyncio.create_task(
        SpendCounterReseed.coalesced(
            prisma_client=primary,
            spend_counter_cache=primary.proxy_server.spend_counter_cache,
            counter_key=key,
        )
    )
    try:
        await asyncio.wait_for(seed_ready.wait(), timeout=5)
        release_reset.set()
        await reset_task
    finally:
        release_reset.set()
        release_seed.set()
        await reset_task
        seeded: Final = await seed_task
    assert seeded == 0.0
    assert await primary.spend() == 0.0
    assert await cache.async_get_cache(key=key) == 0.0
