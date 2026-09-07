"""Redis cache with transparent degradation.

Cache is an optimisation, never a correctness requirement: if Redis is down the
service keeps working and reports the dependency as degraded. A tiny in-process
TTL cache covers single-node development so behaviour is identical.
"""

from __future__ import annotations

import time
from typing import Any

import orjson

from ..core.config import RedisSettings, get_settings
from ..core.logging import get_logger
from ..core.metrics import CACHE_OPS_TOTAL

logger = get_logger(__name__)


class InMemoryCache:
    """Bounded TTL cache used when Redis is disabled or unreachable."""

    def __init__(self, max_entries: int = 4096) -> None:
        self._store: dict[str, tuple[float, bytes]] = {}
        self._max_entries = max_entries

    async def get(self, key: str) -> bytes | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        if len(self._store) >= self._max_entries:
            self._evict()
        self._store[key] = (time.time() + ttl_seconds, value)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self._store if k.startswith(prefix)]
        for key in keys:
            self._store.pop(key, None)
        return len(keys)

    async def incr_window(self, key: str, window_seconds: int) -> tuple[int, int]:
        """Fixed-window counter returning (count, seconds_until_reset)."""
        now = int(time.time())
        bucket_start = now - (now % window_seconds)
        bucket_key = key + ":" + str(bucket_start)
        current = self._store.get(bucket_key)
        count = 1
        if current is not None and current[0] > now:
            count = int(orjson.loads(current[1]).get("n", 0)) + 1
        self._store[bucket_key] = (
            float(bucket_start + window_seconds),
            orjson.dumps({"n": count}),
        )
        return count, max(1, bucket_start + window_seconds - now)

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, (exp, _) in self._store.items() if exp < now]
        for key in expired:
            self._store.pop(key, None)
        if len(self._store) >= self._max_entries:
            oldest = sorted(self._store.items(), key=lambda item: item[1][0])[
                : self._max_entries // 4
            ]
            for key, _ in oldest:
                self._store.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._store.clear()


class RedisCache:
    """Redis-backed cache and rate-limit counter."""

    def __init__(self, settings: RedisSettings) -> None:
        self._settings = settings
        self._client: Any = None

    async def connect(self) -> bool:
        """Open the pool; returns False instead of raising when unavailable."""
        try:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._settings.url,
                max_connections=self._settings.max_connections,
                socket_timeout=self._settings.socket_timeout_seconds,
                socket_connect_timeout=self._settings.socket_connect_timeout_seconds,
                decode_responses=False,
            )
            await self._client.ping()
            logger.info("redis_connected", url=self._redacted_url())
            return True
        except Exception as exc:
            logger.warning("redis_unavailable", error=str(exc))
            self._client = None
            return False

    def _redacted_url(self) -> str:
        url = self._settings.url
        if "@" in url:
            scheme, rest = url.split("://", 1)
            return scheme + "://***@" + rest.split("@", 1)[1]
        return url

    def _key(self, key: str) -> str:
        return self._settings.key_prefix + ":" + key

    async def get(self, key: str) -> bytes | None:
        if self._client is None:
            return None
        try:
            value: bytes | None = await self._client.get(self._key(key))
            CACHE_OPS_TOTAL.labels(result="hit" if value else "miss").inc()
            return value
        except Exception as exc:
            CACHE_OPS_TOTAL.labels(result="error").inc()
            logger.warning("redis_get_failed", key=key, error=str(exc))
            return None

    async def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        if self._client is None or ttl_seconds <= 0:
            return
        try:
            await self._client.set(self._key(key), value, ex=ttl_seconds)
            CACHE_OPS_TOTAL.labels(result="set").inc()
        except Exception as exc:
            CACHE_OPS_TOTAL.labels(result="error").inc()
            logger.warning("redis_set_failed", key=key, error=str(exc))

    async def delete(self, key: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.delete(self._key(key))
        except Exception as exc:
            logger.warning("redis_delete_failed", key=key, error=str(exc))

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key under a prefix using SCAN, never KEYS."""
        if self._client is None:
            return 0
        deleted = 0
        try:
            pattern = self._key(prefix) + "*"
            async for key in self._client.scan_iter(match=pattern, count=500):
                await self._client.delete(key)
                deleted += 1
        except Exception as exc:
            logger.warning("redis_scan_failed", prefix=prefix, error=str(exc))
        return deleted

    async def incr_window(self, key: str, window_seconds: int) -> tuple[int, int]:
        """Atomic fixed-window counter returning (count, seconds_until_reset)."""
        if self._client is None:
            return 1, window_seconds
        now = int(time.time())
        bucket_start = now - (now % window_seconds)
        bucket_key = self._key(key + ":" + str(bucket_start))
        try:
            pipe = self._client.pipeline()
            pipe.incr(bucket_key)
            pipe.expire(bucket_key, window_seconds + 1)
            count, _ = await pipe.execute()
            return int(count), max(1, bucket_start + window_seconds - now)
        except Exception as exc:
            logger.warning("redis_incr_failed", key=key, error=str(exc))
            return 1, window_seconds

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover - best effort shutdown
                logger.debug("redis_close_failed")
            self._client = None


class CacheService:
    """JSON-oriented facade over whichever backend is healthy."""

    def __init__(self, backend: RedisCache | InMemoryCache, default_ttl: int = 300) -> None:
        self._backend = backend
        self._default_ttl = default_ttl

    @property
    def backend_name(self) -> str:
        return "redis" if isinstance(self._backend, RedisCache) else "memory"

    async def get_json(self, key: str) -> Any | None:
        raw = await self._backend.get(key)
        if raw is None:
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            logger.warning("cache_decode_failed", key=key)
            await self._backend.delete(key)
            return None

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        await self._backend.set(key, orjson.dumps(value), ttl_seconds or self._default_ttl)

    async def invalidate_prefix(self, prefix: str) -> int:
        return await self._backend.delete_prefix(prefix)

    async def rate_limit(
        self, key: str, limit: int, window_seconds: int = 60
    ) -> tuple[bool, int, int]:
        """Return (allowed, remaining, retry_after_seconds)."""
        count, retry_after = await self._backend.incr_window(key, window_seconds)
        remaining = max(0, limit - count)
        return count <= limit, remaining, retry_after

    async def health(self) -> dict[str, Any]:
        reachable = await self._backend.ping()
        return {"backend": self.backend_name, "status": "ok" if reachable else "degraded"}

    async def close(self) -> None:
        await self._backend.close()


async def build_cache(settings: RedisSettings | None = None) -> CacheService:
    """Create a cache service, preferring Redis and falling back to memory."""
    cfg = settings or get_settings().redis
    if cfg.enabled:
        redis_cache = RedisCache(cfg)
        if await redis_cache.connect():
            return CacheService(redis_cache, cfg.cache_ttl_seconds)
    logger.info("cache_using_in_memory_backend")
    return CacheService(InMemoryCache(), cfg.cache_ttl_seconds)
