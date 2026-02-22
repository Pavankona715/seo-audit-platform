"""
Redis client factory with connection pooling and health checks.
"""

from typing import Annotated

import redis.asyncio as aioredis
import structlog
from fastapi import Depends

from app.core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

_redis_pool: aioredis.ConnectionPool | None = None


def _get_pool() -> aioredis.ConnectionPool:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.ConnectionPool.from_url(
            str(settings.REDIS_DSN),
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=5.0,
            retry_on_timeout=True,
            health_check_interval=30,
            decode_responses=True,
        )
    return _redis_pool


async def get_redis_client() -> aioredis.Redis:
    """Get a Redis client from the connection pool."""
    return aioredis.Redis(connection_pool=_get_pool())


async def get_redis() -> aioredis.Redis:
    """FastAPI dependency for Redis client."""
    return await get_redis_client()


RedisClient = Annotated[aioredis.Redis, Depends(get_redis)]


class CacheManager:
    """High-level cache operations with namespacing and TTL management."""

    def __init__(self, redis: aioredis.Redis, namespace: str = "seo"):
        self.redis = redis
        self.namespace = namespace

    def _key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    async def get(self, key: str) -> str | None:
        return await self.redis.get(self._key(key))

    async def set(self, key: str, value: str, ttl: int = 3600) -> None:
        await self.redis.setex(self._key(key), ttl, value)

    async def delete(self, key: str) -> None:
        await self.redis.delete(self._key(key))

    async def exists(self, key: str) -> bool:
        return bool(await self.redis.exists(self._key(key)))

    async def increment(self, key: str, amount: int = 1, ttl: int = 60) -> int:
        pipe = self.redis.pipeline()
        full_key = self._key(key)
        pipe.incr(full_key, amount)
        pipe.expire(full_key, ttl)
        results = await pipe.execute()
        return results[0]

    async def set_hash(self, key: str, mapping: dict, ttl: int = 3600) -> None:
        full_key = self._key(key)
        await self.redis.hset(full_key, mapping=mapping)
        await self.redis.expire(full_key, ttl)

    async def get_hash(self, key: str) -> dict:
        return await self.redis.hgetall(self._key(key))
