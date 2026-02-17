"""Redis-based claim/lock to prevent duplicate evaluations across workers."""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis


@dataclass(frozen=True)
class ClaimKey:
    agent_id: str
    instrument: str
    timeframe: str
    candle_ts: int

    def redis_key(self) -> str:
        return f"nexow:claim:{self.agent_id}:{self.instrument}:{self.timeframe}:{self.candle_ts}"


class ClaimLock:
    def __init__(self, redis: aioredis.Redis, owner_id: str) -> None:
        self._redis = redis
        self._owner_id = owner_id

    async def try_claim(self, key: ClaimKey, ttl_seconds: int) -> bool:
        # SET key value NX EX ttl
        # Returns True if we acquired the claim.
        result = await self._redis.set(
            key.redis_key(),
            self._owner_id,
            ex=ttl_seconds,
            nx=True,
        )
        return bool(result)

