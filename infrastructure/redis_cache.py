"""infrastructure/redis_cache.py — Redis-backed caching for market data and signals."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    aioredis = None


class RedisCache:
    """Async Redis cache for market data, signals, and state.

    Provides TTL-based expiration, JSON serialization, and pub/sub support.
    Falls back to in-memory dict when Redis is unavailable.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        default_ttl_s: int = 300,
        fallback_memory: bool = True,
    ) -> None:
        self._url = url
        self._default_ttl = default_ttl_s
        self._fallback_memory = fallback_memory
        self._client: Optional[aioredis.Redis] = None
        self._mem_store: Dict[str, Any] = {}

    async def connect(self) -> bool:
        if not REDIS_AVAILABLE:
            logger.warning("redis.asyncio not installed. Using in-memory fallback.")
            return False
        try:
            client = aioredis.from_url(self._url, decode_responses=True)
            await client.ping()
            self._client = client
            logger.info("Connected to Redis at %s", self._url)
            return True
        except Exception as e:
            logger.warning("Redis connection failed (%s). Using in-memory fallback.", e)
            self._client = None
            return False

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    async def get(self, key: str) -> Optional[Any]:
        if self._client:
            raw = await self._client.get(key)
            if raw is not None:
                return json.loads(raw)
            return None
        return self._mem_store.get(key)

    async def set(self, key: str, value: Any, ttl_s: Optional[int] = None) -> None:
        ttl = ttl_s if ttl_s is not None else self._default_ttl
        serialized = json.dumps(value)
        if self._client:
            await self._client.setex(key, ttl, serialized)
        elif self._fallback_memory:
            self._mem_store[key] = value

    async def delete(self, key: str) -> None:
        if self._client:
            await self._client.delete(key)
        elif self._fallback_memory:
            self._mem_store.pop(key, None)

    async def exists(self, key: str) -> bool:
        if self._client:
            return bool(await self._client.exists(key))
        return key in self._mem_store

    async def get_many(self, keys: List[str]) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        if self._client:
            values = await self._client.mget(keys)
            for k, v in zip(keys, values):
                if v is not None:
                    results[k] = json.loads(v)
        else:
            for k in keys:
                if k in self._mem_store:
                    results[k] = self._mem_store[k]
        return results

    async def set_many(self, mapping: Dict[str, Any], ttl_s: Optional[int] = None) -> None:
        ttl = ttl_s if ttl_s is not None else self._default_ttl
        if self._client:
            async with self._client.pipeline() as pipe:
                for k, v in mapping.items():
                    await pipe.setex(k, ttl, json.dumps(v))
                await pipe.execute()
        elif self._fallback_memory:
            self._mem_store.update(mapping)

    async def publish(self, channel: str, message: Any) -> int:
        if self._client:
            return await self._client.publish(channel, json.dumps(message))
        return 0

    async def subscribe(self, channel: str) -> Optional[Any]:
        if self._client:
            pubsub = self._client.pubsub()
            await pubsub.subscribe(channel)
            return pubsub
        return None

    async def keys(self, pattern: str = "*") -> List[str]:
        if self._client:
            return await self._client.keys(pattern)
        import fnmatch
        return [k for k in self._mem_store if fnmatch.fnmatch(k, pattern)]

    async def flush(self) -> None:
        if self._client:
            await self._client.flushdb()
        self._mem_store.clear()

    def is_connected(self) -> bool:
        return self._client is not None
