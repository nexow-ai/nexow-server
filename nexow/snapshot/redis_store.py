"""Redis cache layer for market snapshots."""

from __future__ import annotations

import json

import redis.asyncio as aioredis
import structlog

from nexow.config import settings

logger = structlog.get_logger(__name__)

SNAPSHOT_TTL = 120  # 2 minutes (2x M1 interval)
KEY_PREFIX = "nexow:snapshot"


class SnapshotRedisStore:
    """Async Redis store for market snapshots."""

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        logger.info("snapshot_redis_connected")

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()

    def _key(self, instrument: str) -> str:
        return f"{KEY_PREFIX}:{instrument}"

    async def set_snapshot(self, instrument: str, snapshot_json: str) -> None:
        """Cache a snapshot JSON string with TTL."""
        if not self._redis:
            return
        await self._redis.set(self._key(instrument), snapshot_json, ex=SNAPSHOT_TTL)

    async def get_snapshot(self, instrument: str) -> dict | None:
        """Retrieve a cached snapshot as dict, or None if expired/missing."""
        if not self._redis:
            return None
        raw = await self._redis.get(self._key(instrument))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
