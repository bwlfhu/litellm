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
from datetime import datetime, timezone
from collections.abc import Awaitable, Coroutine, Mapping, Set
from contextlib import AbstractAsyncContextManager
from typing import Dict, Iterable, Optional, Protocol, Union, cast

from litellm._logging import verbose_proxy_logger
from litellm._uuid import uuid
from litellm.caching.redis_cache import RedisCache
from litellm.integrations.custom_logger import CustomLogger

_PREFIX = "litellm:routing-stats:v1"
_BUCKET_SECONDS = 60
_RETENTION_SECONDS = 20 * 60
_ACTIVE_LEASE_SECONDS = 20 * 60
_LATENCY_BUCKETS_MS = (100, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000, 120000)


RedisValue = Union[str, bytes, int, float]
Timestamp = Union[datetime, int, float]
Metadata = Dict[str, str]


class RoutingStatsPipeline(Protocol):
    def zrem(self, key: str, member: str) -> object: ...

    def hset(self, key: str, mapping: Mapping[str, object]) -> object: ...

    def hincrby(self, key: str, field: str, value: int) -> object: ...

    def hincrbyfloat(self, key: str, field: str, value: int) -> object: ...

    def expire(self, key: str, ttl: int) -> object: ...

    def sadd(self, key: str, value: str) -> object: ...

    def execute(self) -> Awaitable[object]: ...


class RoutingStatsRedis(Protocol):
    def eval(self, script: str, numkeys: int, *args: object) -> Awaitable[object]: ...

    def set(self, key: str, value: str, *, ex: int, nx: bool) -> Awaitable[object]: ...

    def pipeline(self, *, transaction: bool) -> AbstractAsyncContextManager[RoutingStatsPipeline]: ...

    def smembers(self, key: str) -> Awaitable[Set[RedisValue]]: ...

    def hgetall(self, key: str) -> Awaitable[Mapping[RedisValue, RedisValue]]: ...

    def zremrangebyscore(self, key: str, minimum: str, maximum: int) -> Awaitable[object]: ...

    def zcard(self, key: str) -> Awaitable[int]: ...


def _decode(value: Optional[RedisValue]) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _as_int(value: Optional[RedisValue]) -> int:
    try:
        return int(float(_decode(value)))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Optional[RedisValue]) -> float:
    try:
        return float(_decode(value))
    except (TypeError, ValueError):
        return 0.0


class RoutingStatsStore:
    """Writes and reads minute-bucketed deployment attempt statistics."""

    def __init__(self, redis_cache: RedisCache):
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
    def _bucket(timestamp: Optional[float] = None) -> int:
        return int((timestamp if timestamp is not None else time.time()) // _BUCKET_SECONDS)

    def _bucket_key(self, bucket: int, token: str) -> str:
        return self._key("bucket", bucket, token)

    def _index_key(self, bucket: int) -> str:
        return self._key("index", bucket)

    def _active_key(self, token: str) -> str:
        return self._key("active", "{" + token + "}")

    def _terminal_key(self, token: str, attempt_id: str) -> str:
        return self._key("terminal", "{" + token + "}", attempt_id)

    async def record_start(self, metadata: Metadata, attempt_id: str) -> None:
        """Publish deployment metadata and add an expiring active-request lease."""
        token = self._token(metadata["model_id"])
        active_key = self._active_key(token)
        terminal_key = self._terminal_key(token, attempt_id)
        client = self._async_client()
        now_ms = int(time.time() * 1000)
        bucket = self._bucket()
        bucket_key = self._bucket_key(bucket, token)
        index_key = self._index_key(bucket)
        mapping = {
            "model_id": metadata["model_id"],
            "model_group": metadata["model_group"],
            "channel": metadata["channel"],
            "api_base": metadata.get("api_base", ""),
            "last_seen_ms": now_ms,
        }

        # Index the selected deployment before it completes. This makes an
        # in-flight request visible even when it is the first request for it.
        async with client.pipeline(transaction=False) as pipe:
            pipe.hset(bucket_key, mapping=mapping)
            pipe.expire(bucket_key, _RETENTION_SECONDS)
            pipe.sadd(index_key, token)
            pipe.expire(index_key, _RETENTION_SECONDS)
            await pipe.execute()

        expires_at = int(time.time() + _ACTIVE_LEASE_SECONDS)
        # A terminal callback can win a scheduling race with this background task.
        # The marker prevents a completed request from being resurrected as active.
        script = (
            "if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end "
            "redis.call('ZADD', KEYS[1], ARGV[1], ARGV[2]) "
            "redis.call('EXPIRE', KEYS[1], ARGV[3]) return 1"
        )
        await client.eval(
            script, 2, active_key, terminal_key, expires_at, attempt_id, _ACTIVE_LEASE_SECONDS
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
        client = self._async_client()
        token = self._token(metadata["model_id"])
        terminal_key = self._terminal_key(token, attempt_id)
        # Callback chains may include more than one terminal path. Count an
        # attempt once, and also leave a marker for a lagging start task.
        first_terminal = await client.set(terminal_key, "1", ex=_ACTIVE_LEASE_SECONDS, nx=True)
        if not first_terminal:
            return

        now_ms = int(time.time() * 1000)
        duration_ms = self._duration_ms(start_time=start_time, end_time=end_time)
        bucket = self._bucket()
        bucket_key = self._bucket_key(bucket, token)
        index_key = self._index_key(bucket)
        active_key = self._active_key(token)
        latency_field = self._latency_field(duration_ms)
        mapping = {
            "model_id": metadata["model_id"],
            "model_group": metadata["model_group"],
            "channel": metadata["channel"],
            "api_base": metadata.get("api_base", ""),
            "last_seen_ms": now_ms,
        }

        async with client.pipeline(transaction=False) as pipe:
            pipe.zrem(active_key, attempt_id)
            pipe.hset(bucket_key, mapping=mapping)
            pipe.hincrby(bucket_key, "requests", 1)
            pipe.hincrby(bucket_key, "success" if succeeded else "failure", 1)
            pipe.hincrbyfloat(bucket_key, "latency_sum_ms", duration_ms)
            pipe.hincrby(bucket_key, latency_field, 1)
            pipe.expire(bucket_key, _RETENTION_SECONDS)
            pipe.sadd(index_key, token)
            pipe.expire(index_key, _RETENTION_SECONDS)
            await pipe.execute()

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
        channel: Optional[str] = None,
        model_group: Optional[str] = None,
    ) -> list[dict[str, object]]:
        current_bucket = self._bucket()
        buckets = range(current_bucket - window_minutes + 1, current_bucket + 1)
        client = self._async_client()
        token_sets = await asyncio.gather(
            *(client.smembers(self._index_key(bucket)) for bucket in buckets)
        )
        tokens = {_decode(token) for token_set in token_sets for token in token_set}

        rows = await asyncio.gather(
            *(self._read_token(client=client, token=token, buckets=buckets) for token in tokens)
        )
        response: list[dict[str, object]] = []
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
    ) -> Optional[dict[str, object]]:
        bucket_list = list(buckets)
        hash_rows = await asyncio.gather(
            *(client.hgetall(self._bucket_key(bucket, token)) for bucket in bucket_list)
        )
        merged: dict[str, float] = defaultdict(float)
        latest_metadata: dict[str, str] = {}
        latest_seen = -1
        for hash_row in hash_rows:
            if not hash_row:
                continue
            row = {
                _decode(key): _decode(value) for key, value in hash_row.items()
            }
            seen = _as_int(row.get("last_seen_ms"))
            if seen >= latest_seen:
                latest_seen = seen
                latest_metadata = {
                    key: row.get(key, "") for key in ("channel", "model_group", "model_id", "api_base")
                }
            for field in ("requests", "success", "failure"):
                merged[field] += _as_int(row.get(field))
            merged["latency_sum_ms"] += _as_float(row.get("latency_sum_ms"))
            for field in (f"latency_le_{bound}" for bound in _LATENCY_BUCKETS_MS):
                merged[field] += _as_int(row.get(field))
            merged["latency_le_inf"] += _as_int(row.get("latency_le_inf"))

        if not latest_metadata.get("model_id"):
            return None
        active_requests = await self._active_requests(client=client, token=token)
        requests = int(merged["requests"])
        return {
            **latest_metadata,
            "requests": requests,
            "upstream_attempts": requests,
            "success": int(merged["success"]),
            "failure": int(merged["failure"]),
            "upstream_failure": int(merged["failure"]),
            "active_requests": active_requests,
            "latency_p50_ms": self._percentile(merged, requests, 0.50),
            "latency_p95_ms": self._percentile(merged, requests, 0.95),
            "latency_avg_ms": round(merged["latency_sum_ms"] / requests, 2) if requests else None,
            "latency_max_ms": self._histogram_max(merged) if requests else None,
            "last_seen": datetime.fromtimestamp(latest_seen / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    async def _active_requests(self, client: RoutingStatsRedis, token: str) -> int:
        active_key = self._active_key(token)
        now = int(time.time())
        await client.zremrangebyscore(active_key, "-inf", now)
        return int(await client.zcard(active_key))

    @staticmethod
    def _percentile(metrics: dict[str, float], count: int, quantile: float) -> Optional[int]:
        if count <= 0:
            return None
        target = max(1, math.ceil(count * quantile))
        running = 0
        for bound in _LATENCY_BUCKETS_MS:
            running += metrics[f"latency_le_{bound}"]
            if running >= target:
                return bound
        return _LATENCY_BUCKETS_MS[-1]

    @staticmethod
    def _histogram_max(metrics: dict[str, float]) -> int:
        for bound in reversed(_LATENCY_BUCKETS_MS):
            if metrics[f"latency_le_{bound}"] > 0:
                return bound
        return _LATENCY_BUCKETS_MS[-1]


class RoutingStatsLogger(CustomLogger):
    """Callback that sends deployment-attempt telemetry to Redis in background tasks."""

    def __init__(self, redis_cache: RedisCache):
        super().__init__(turn_off_message_logging=True)  # pyright: ignore[reportUnknownMemberType]
        self._store = RoutingStatsStore(redis_cache=redis_cache)

    def update_redis_cache(self, redis_cache: RedisCache) -> None:
        self._store = RoutingStatsStore(redis_cache=redis_cache)

    def log_pre_api_call(self, model: str, messages: list[object], kwargs: Dict[str, object]) -> None:
        metadata = self._metadata(kwargs)
        if metadata is None:
            return
        litellm_params_raw = kwargs.get("litellm_params")
        if not isinstance(litellm_params_raw, dict):
            return
        litellm_params = cast(Dict[str, object], litellm_params_raw)
        current_model_id = litellm_params.get("routing_stats_attempt_model_id")
        attempt_id = litellm_params.get("routing_stats_attempt_id")
        if current_model_id != metadata["model_id"] or not isinstance(attempt_id, str):
            attempt_id = str(uuid.uuid4())
            litellm_params["routing_stats_attempt_id"] = attempt_id
            litellm_params["routing_stats_attempt_model_id"] = metadata["model_id"]
        self._schedule(self._store.record_start(metadata=metadata, attempt_id=attempt_id))

    def log_success_event(
        self, kwargs: Dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=True, start_time=start_time, end_time=end_time)

    def log_failure_event(
        self, kwargs: Dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=False, start_time=start_time, end_time=end_time)

    async def async_log_success_event(
        self, kwargs: Dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=True, start_time=start_time, end_time=end_time)

    async def async_log_failure_event(
        self, kwargs: Dict[str, object], response_obj: object, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        self._record_terminal(kwargs=kwargs, succeeded=False, start_time=start_time, end_time=end_time)

    def _record_terminal(
        self, kwargs: Dict[str, object], succeeded: bool, start_time: Timestamp, end_time: Timestamp
    ) -> None:
        metadata = self._metadata(kwargs)
        litellm_params_raw = kwargs.get("litellm_params")
        if metadata is None or not isinstance(litellm_params_raw, dict):
            return
        litellm_params = cast(Dict[str, object], litellm_params_raw)
        attempt_id = litellm_params.get("routing_stats_attempt_id")
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
    def _metadata(kwargs: Dict[str, object]) -> Optional[Metadata]:
        litellm_params_raw = kwargs.get("litellm_params")
        if not isinstance(litellm_params_raw, dict):
            return None
        litellm_params = cast(Dict[str, object], litellm_params_raw)
        model_info_raw = litellm_params.get("model_info")
        metadata_raw = litellm_params.get("metadata")
        if not isinstance(model_info_raw, dict) or not isinstance(metadata_raw, dict):
            return None
        model_info = cast(Dict[str, object], model_info_raw)
        metadata = cast(Dict[str, object], metadata_raw)
        model_id = model_info.get("id")
        model_group = metadata.get("model_group")
        access_groups = model_info.get("access_groups")
        if model_id is None or not isinstance(model_group, str) or not isinstance(access_groups, list) or not access_groups:
            return None
        channel: object = cast(object, access_groups[0])
        if not isinstance(channel, str) or not channel:
            return None
        api_base = metadata.get("api_base")
        return {
            "model_id": str(model_id),
            "model_group": model_group,
            "channel": channel,
            "api_base": api_base if isinstance(api_base, str) else "",
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


_routing_stats_logger: Optional[RoutingStatsLogger] = None


def initialize_routing_stats(redis_cache: Optional[RedisCache]) -> None:
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
