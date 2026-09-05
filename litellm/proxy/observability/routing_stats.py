"""Real-time deployment routing statistics backed by coordination Redis.

This deliberately does not use spend logs. The proxy records only deployment
attempts that reached a selected deployment, so pre-routing failures are not
attributed to a channel that was never contacted.
"""

import asyncio
import hashlib
import math
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Set
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Final, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from litellm._logging import verbose_proxy_logger
from litellm._uuid import uuid
from litellm.caching.redis_cache import RedisCache
from litellm.integrations.custom_logger import CustomLogger

_PREFIX: Final = "litellm:routing-stats:v2"
_BUCKET_SECONDS: Final = 60
_RETENTION_SECONDS = 20 * 60
_ACTIVE_LEASE_SECONDS = 20 * 60
_LATENCY_BUCKETS_MS: Final = (100, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000, 120000)


RedisValue = str | bytes | int | float
Timestamp = datetime | int | float
Metadata = dict[str, str]


class RoutingStatsPipeline(Protocol):
    def zrem(self, key: str, member: str) -> object: ...

    def hset(self, key: str, mapping: Mapping[str, object]) -> object: ...

    def hincrby(self, key: str, field: str, value: int) -> object: ...

    def hincrbyfloat(self, key: str, field: str, value: int) -> object: ...

    def expire(self, key: str, ttl: int) -> object: ...

    def sadd(self, key: str, value: str) -> object: ...

    def zadd(self, key: str, mapping: Mapping[str, int]) -> object: ...

    def execute(self) -> Awaitable[object]: ...


class RoutingStatsRedis(Protocol):
    def eval(self, script: str, numkeys: int, *args: object) -> Awaitable[object]: ...

    def pipeline(self, *, transaction: bool) -> AbstractAsyncContextManager[RoutingStatsPipeline]: ...

    def smembers(self, key: str) -> Awaitable[Set[RedisValue]]: ...

    def zrangebyscore(self, key: str, minimum: int, maximum: str) -> Awaitable[list[RedisValue]]: ...

    def hgetall(self, key: str) -> Awaitable[Mapping[RedisValue, RedisValue]]: ...

    def zremrangebyscore(self, key: str, minimum: str, maximum: int) -> Awaitable[object]: ...

    def zcard(self, key: str) -> Awaitable[int]: ...


def _decode(value: RedisValue | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _as_int(value: RedisValue | None) -> int:
    try:
        return int(float(_decode(value)))
    except (TypeError, ValueError):
        return 0


def _as_float(value: RedisValue | None) -> float:
    try:
        return float(_decode(value))
    except (TypeError, ValueError):
        return 0.0


def _sanitize_api_base(api_base: object) -> str:
    """Keep only a provider URL's scheme, host, and optional port."""
    if not isinstance(api_base, str) or not api_base:
        return ""
    try:
        parsed: Final = urlsplit(api_base)
        if not parsed.scheme or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc: Final = host if parsed.port is None else f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))
    except ValueError:
        return ""


class RoutingStatsStore:
    """Writes and reads minute-bucketed deployment attempt statistics."""

    def __init__(self, redis_cache: RedisCache) -> None:
        self.redis_cache = redis_cache

    def _async_client(self) -> RoutingStatsRedis:
        """RedisCache deliberately supports both Redis and RedisCluster clients."""
        return cast(RoutingStatsRedis, self.redis_cache.init_async_client())  # pyright: ignore[reportUnknownMemberType]

    def _key(self, *parts: object) -> str:
        return self.redis_cache.check_and_fix_namespace(":".join([_PREFIX, *map(str, parts)]))

    @staticmethod
    def _token(model_id: str) -> str:
        return hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _bucket(timestamp: float | None = None) -> int:
        return int((timestamp if timestamp is not None else time.time()) // _BUCKET_SECONDS)

    def _bucket_key(self, bucket: int, token: str) -> str:
        return self._key("bucket", "{" + token + "}", bucket)

    def _index_key(self, bucket: int) -> str:
        return self._key("index", bucket)

    def _active_key(self, token: str) -> str:
        return self._key("active", "{" + token + "}")

    def _active_metadata_key(self, token: str) -> str:
        return self._key("active-metadata", "{" + token + "}")

    def _active_index_key(self) -> str:
        return self._key("active-index-v2")

    def _terminal_key(self, token: str, attempt_id: str) -> str:
        return self._key("terminal", "{" + token + "}", attempt_id)

    async def record_start(self, metadata: Metadata, attempt_id: str) -> None:
        """Add an expiring active-request lease after a deployment is selected."""
        token: Final = self._token(metadata["model_id"])
        active_key: Final = self._active_key(token)
        terminal_key: Final = self._terminal_key(token, attempt_id)
        client: Final = self._async_client()
        bucket: Final = self._bucket()
        expires_at: Final = int(time.time() + _ACTIVE_LEASE_SECONDS)
        await self._retry(
            lambda: self._write_inventory(
                client,
                metadata,
                token,
                bucket,
                active_expires_at=expires_at,
            )
        )
        # A terminal callback can win a scheduling race with this background task.
        # The marker prevents a completed request from being resurrected as active.
        script: Final = (
            "if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end "
            "redis.call('ZADD', KEYS[1], ARGV[1], ARGV[2]) "
            "redis.call('EXPIRE', KEYS[1], ARGV[3]) return 1"
        )
        await self._retry(
            lambda: client.eval(script, 2, active_key, terminal_key, expires_at, attempt_id, _ACTIVE_LEASE_SECONDS)
        )

    async def record_terminal(
        self,
        metadata: Metadata,
        attempt_id: str,
        succeeded: bool,
        start_time: Timestamp,
        end_time: Timestamp,
    ) -> None:
        """Persist an attempt result. This is called from a detached task."""
        client: Final = self._async_client()
        token: Final = self._token(metadata["model_id"])
        now_ms: Final = int(time.time() * 1000)
        duration_ms: Final = self._duration_ms(start_time=start_time, end_time=end_time)
        bucket: Final = self._bucket()
        bucket_key: Final = self._bucket_key(bucket, token)
        active_key: Final = self._active_key(token)
        terminal_key: Final = self._terminal_key(token, attempt_id)
        latency_field: Final = self._latency_field(duration_ms)
        metric_field: Final = "success" if succeeded else "failure"
        # The three keys share the deployment token's Redis Cluster hash tag.
        # This makes deduplication, active cleanup, and counters atomic.
        script: Final = (
            "if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end "
            "redis.call('SET', KEYS[2], '1', 'EX', ARGV[2]) "
            "redis.call('ZREM', KEYS[1], ARGV[1]) "
            "redis.call('HSET', KEYS[3], 'model_id', ARGV[4], 'model_group', ARGV[5], "
            "'channel', ARGV[6], 'api_base', ARGV[7], 'last_seen_ms', ARGV[8]) "
            "redis.call('HINCRBY', KEYS[3], 'requests', 1) "
            "redis.call('HINCRBY', KEYS[3], ARGV[9], 1) "
            "redis.call('HINCRBYFLOAT', KEYS[3], 'latency_sum_ms', ARGV[10]) "
            "redis.call('HINCRBY', KEYS[3], ARGV[11], 1) "
            "redis.call('EXPIRE', KEYS[3], ARGV[3]) return 1"
        )

        async def _record() -> None:
            # An already-recorded terminal event is expected on duplicate
            # callback delivery. Inventory is still retried if its previous
            # non-atomic write failed after the metric script succeeded.
            await client.eval(
                script,
                3,
                active_key,
                terminal_key,
                bucket_key,
                attempt_id,
                _ACTIVE_LEASE_SECONDS,
                _RETENTION_SECONDS,
                metadata["model_id"],
                metadata["model_group"],
                metadata["channel"],
                metadata.get("api_base", ""),
                now_ms,
                metric_field,
                duration_ms,
                latency_field,
            )
            await self._write_inventory(client, metadata, token, bucket, active_expires_at=None)

        await self._retry(_record)

    async def _write_inventory(
        self,
        client: RoutingStatsRedis,
        metadata: Metadata,
        token: str,
        bucket: int,
        active_expires_at: int | None,
    ) -> None:
        """Index deployment metadata separately from the atomic metric write."""
        now_ms: Final = int(time.time() * 1000)
        mapping: Final = {
            "model_id": metadata["model_id"],
            "model_group": metadata["model_group"],
            "channel": metadata["channel"],
            "api_base": metadata.get("api_base", ""),
            "last_seen_ms": now_ms,
        }
        async with client.pipeline(transaction=False) as pipe:
            bucket_key: Final = self._bucket_key(bucket, token)
            pipe.hset(bucket_key, mapping=mapping)
            pipe.expire(bucket_key, _RETENTION_SECONDS)
            pipe.sadd(self._index_key(bucket), token)
            pipe.expire(self._index_key(bucket), _RETENTION_SECONDS)
            if active_expires_at is not None:
                active_metadata_key: Final = self._active_metadata_key(token)
                pipe.hset(active_metadata_key, mapping=mapping)
                pipe.expire(active_metadata_key, _ACTIVE_LEASE_SECONDS)
                pipe.zadd(self._active_index_key(), {token: active_expires_at})
            await pipe.execute()

    @staticmethod
    async def _retry(operation: Callable[[], Awaitable[object]]) -> object:
        """Retry short Redis blips inside the detached telemetry task."""
        for attempt in range(3):
            try:
                return await operation()
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")

    @staticmethod
    def _duration_ms(start_time: Timestamp, end_time: Timestamp) -> int:
        try:
            if isinstance(start_time, datetime) and isinstance(end_time, datetime):
                return max(0, round((end_time - start_time).total_seconds() * 1000))
            if isinstance(start_time, datetime) or isinstance(end_time, datetime):
                return 0
            return max(0, round((float(end_time) - float(start_time)) * 1000))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _latency_field(duration_ms: int) -> str:
        for upper_bound in _LATENCY_BUCKETS_MS:
            if duration_ms <= upper_bound:
                return f"latency_le_{upper_bound}"
        return "latency_le_inf"

    async def query(
        self,
        window_minutes: int,
        channel: str | None = None,
        model_group: str | None = None,
    ) -> list[dict[str, object]]:
        current_bucket: Final = self._bucket()
        buckets: Final = range(current_bucket - window_minutes + 1, current_bucket + 1)
        client: Final = self._async_client()
        token_sets: Final = await asyncio.gather(*(client.smembers(self._index_key(bucket)) for bucket in buckets))
        tokens: Final = {_decode(token) for token_set in token_sets for token in token_set}
        active_index_key: Final = self._active_index_key()
        now: Final = int(time.time())
        await client.zremrangebyscore(active_index_key, "-inf", now)
        tokens.update(_decode(token) for token in await client.zrangebyscore(active_index_key, now + 1, "+inf"))

        rows: Final = await asyncio.gather(
            *(self._read_token(client=client, token=token, buckets=buckets) for token in tokens)
        )
        response: Final[list[dict[str, object]]] = []
        for row in rows:
            if row is None:
                continue
            if channel is not None and row["channel"] != channel:
                continue
            if model_group is not None and row["model_group"] != model_group:
                continue
            response.append(row)
        return sorted(response, key=lambda item: (item["channel"], item["model_group"], item["model_id"]))

    async def _read_token(
        self, client: RoutingStatsRedis, token: str, buckets: Iterable[int]
    ) -> dict[str, object] | None:
        bucket_list: Final = list(buckets)
        hash_rows: Final = await asyncio.gather(
            *(client.hgetall(self._bucket_key(bucket, token)) for bucket in bucket_list)
        )
        active_metadata: Final = await client.hgetall(self._active_metadata_key(token))
        merged: Final[dict[str, float]] = defaultdict(float)
        latest_metadata: dict[str, str] = {}
        latest_seen = -1
        for hash_row in hash_rows:
            if not hash_row:
                continue
            row = {_decode(key): _decode(value) for key, value in hash_row.items()}
            seen = _as_int(row.get("last_seen_ms"))
            if seen >= latest_seen:
                latest_seen = seen
                latest_metadata = {key: row.get(key, "") for key in ("channel", "model_group", "model_id", "api_base")}
            for field in ("requests", "success", "failure"):
                merged[field] += _as_int(row.get(field))
            merged["latency_sum_ms"] += _as_float(row.get("latency_sum_ms"))
            for field in (f"latency_le_{bound}" for bound in _LATENCY_BUCKETS_MS):
                merged[field] += _as_int(row.get(field))
            merged["latency_le_inf"] += _as_int(row.get("latency_le_inf"))

        active_requests: Final = await self._active_requests(client=client, token=token)
        if not latest_metadata and active_metadata:
            active_row: Final = {_decode(key): _decode(value) for key, value in active_metadata.items()}
            latest_seen = _as_int(active_row.get("last_seen_ms"))
            latest_metadata = {
                key: active_row.get(key, "") for key in ("channel", "model_group", "model_id", "api_base")
            }
        if not latest_metadata.get("model_id") or (not any(hash_rows) and active_requests == 0):
            return None
        requests: Final = int(merged["requests"])
        has_latency_overflow: Final = merged["latency_le_inf"] > 0
        return {
            **latest_metadata,
            "requests": requests,
            "upstream_attempts": requests,
            "success": int(merged["success"]),
            "failure": int(merged["failure"]),
            "upstream_failure": int(merged["failure"]),
            "active_requests": active_requests,
            "latency_p50_ms": None if has_latency_overflow else self._percentile(merged, requests, 0.50),
            "latency_p95_ms": None if has_latency_overflow else self._percentile(merged, requests, 0.95),
            "latency_avg_ms": round(merged["latency_sum_ms"] / requests, 2) if requests else None,
            "latency_max_ms": self._histogram_max(merged) if requests and not has_latency_overflow else None,
            "latency_overflow_count": int(merged["latency_le_inf"]),
            "last_seen": datetime.fromtimestamp(latest_seen / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    async def _active_requests(self, client: RoutingStatsRedis, token: str) -> int:
        active_key: Final = self._active_key(token)
        now: Final = int(time.time())
        await client.zremrangebyscore(active_key, "-inf", now)
        return int(await client.zcard(active_key))

    @staticmethod
    def _percentile(metrics: dict[str, float], count: int, quantile: float) -> int | None:
        if count <= 0:
            return None
        target: Final = max(1, math.ceil(count * quantile))
        running = 0
        for bound in _LATENCY_BUCKETS_MS:
            running += metrics[f"latency_le_{bound}"]
            if running >= target:
                return bound
        return None

    @staticmethod
    def _histogram_max(metrics: dict[str, float]) -> int | None:
        if metrics["latency_le_inf"] > 0:
            return None
        for bound in reversed(_LATENCY_BUCKETS_MS):
            if metrics[f"latency_le_{bound}"] > 0:
                return bound
        return None


class RoutingStatsLogger(CustomLogger):
    """Callback that sends deployment-attempt telemetry to Redis in background tasks."""

    def __init__(self, redis_cache: RedisCache) -> None:
        super().__init__(turn_off_message_logging=True)  # pyright: ignore[reportUnknownMemberType]
        self._store = RoutingStatsStore(redis_cache=redis_cache)

    def update_redis_cache(self, redis_cache: RedisCache) -> None:
        self._store = RoutingStatsStore(redis_cache=redis_cache)

    def log_pre_api_call(self, model: str, messages: list[object], kwargs: dict[str, object]) -> None:
        metadata: Final = self._metadata(kwargs)
        if metadata is None:
            return
        litellm_params_raw: Final = kwargs.get("litellm_params")
        if not isinstance(litellm_params_raw, dict):
            return
        litellm_params: Final = cast(dict[str, object], litellm_params_raw)
        # The input callback runs once per provider handoff. Router retries can
        # select the same deployment and reuse request kwargs, so never reuse a
        # prior attempt identifier based on model id.
        attempt_id: Final = str(uuid.uuid4())
        litellm_params["routing_stats_attempt_id"] = attempt_id
        self._schedule(self._store.record_start(metadata=metadata, attempt_id=attempt_id))

    def log_success_event(
        self, kwargs: dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=True, start_time=start_time, end_time=end_time)

    def log_failure_event(
        self, kwargs: dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=False, start_time=start_time, end_time=end_time)

    async def async_log_success_event(
        self, kwargs: dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=True, start_time=start_time, end_time=end_time)

    async def async_log_failure_event(
        self, kwargs: dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=False, start_time=start_time, end_time=end_time)

    def _record_terminal(
        self, kwargs: dict[str, object], succeeded: bool, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        metadata: Final = self._metadata(kwargs)
        litellm_params_raw: Final = kwargs.get("litellm_params")
        if metadata is None or not isinstance(litellm_params_raw, dict):
            return
        litellm_params: Final = cast(dict[str, object], litellm_params_raw)
        attempt_id: Final = litellm_params.get("routing_stats_attempt_id")
        if not isinstance(attempt_id, str):
            return
        self._schedule(
            self._store.record_terminal(
                metadata=metadata,
                attempt_id=attempt_id,
                succeeded=succeeded,
                start_time=start_time,
                end_time=end_time,
            )
        )

    @staticmethod
    def _metadata(kwargs: dict[str, object]) -> Metadata | None:
        litellm_params_raw: Final = kwargs.get("litellm_params")
        if not isinstance(litellm_params_raw, dict):
            return None
        litellm_params: Final = cast(dict[str, object], litellm_params_raw)
        # Router writes this independent snapshot after each deployment
        # selection. Both metadata containers can otherwise contain caller
        # input: Chat routes via `metadata`, Responses via `litellm_metadata`.
        metadata_raw: Final = litellm_params.get("routing_stats_metadata")
        if not isinstance(metadata_raw, dict):
            return None
        metadata: Final = cast(dict[str, object], metadata_raw)
        model_id: Final = metadata.get("model_id")
        model_group: Final = metadata.get("model_group")
        channel: Final = metadata.get("channel")
        if model_id is None or not isinstance(model_group, str) or not isinstance(channel, str) or not channel:
            return None
        return {
            "model_id": str(model_id),
            "model_group": model_group,
            "channel": channel,
            "api_base": _sanitize_api_base(metadata.get("api_base")),
        }

    @staticmethod
    async def _run_background(coroutine: Coroutine[object, object, None]) -> None:
        try:
            await coroutine
        except Exception as exc:  # noqa: BLE001
            # Telemetry is best-effort; Redis must never affect model traffic.
            verbose_proxy_logger.debug("Routing stats write failed: %s", exc)

    @classmethod
    def _schedule(cls, coroutine: Coroutine[object, object, None]) -> None:
        try:
            asyncio.get_running_loop().create_task(cls._run_background(coroutine))
        except RuntimeError:
            coroutine.close()
        except Exception as exc:  # noqa: BLE001
            verbose_proxy_logger.debug("Routing stats scheduling failed: %s", exc)
            coroutine.close()


_routing_stats_logger: RoutingStatsLogger | None = None  # rebind-ok: initialized once when proxy startup supplies Redis


def initialize_routing_stats(redis_cache: RedisCache | None) -> None:
    """Register one callback instance when shared coordination Redis is available."""
    global _routing_stats_logger
    if redis_cache is None:
        verbose_proxy_logger.warning("Routing stats disabled because coordination Redis is unavailable")
        return
    if _routing_stats_logger is None:
        _routing_stats_logger = RoutingStatsLogger(redis_cache=redis_cache)
        import litellm

        litellm.logging_callback_manager.add_litellm_input_callback(_routing_stats_logger)  # pyright: ignore[reportUnknownMemberType]
        litellm.logging_callback_manager.add_litellm_success_callback(_routing_stats_logger)  # pyright: ignore[reportUnknownMemberType]
        litellm.logging_callback_manager.add_litellm_failure_callback(_routing_stats_logger)  # pyright: ignore[reportUnknownMemberType]
        litellm.logging_callback_manager.add_litellm_async_success_callback(_routing_stats_logger)  # pyright: ignore[reportUnknownMemberType]
        litellm.logging_callback_manager.add_litellm_async_failure_callback(_routing_stats_logger)  # pyright: ignore[reportUnknownMemberType]
    else:
        _routing_stats_logger.update_redis_cache(redis_cache=redis_cache)
