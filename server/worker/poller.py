"""Market data poller — fetches prices from Oanda and broadcasts via Redis."""

import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis
import structlog

from server.broker.oanda import OandaClient
from server.config import settings

logger = structlog.get_logger(__name__)


class MarketDataPoller:
    """
    Polls Oanda API for live prices and broadcasts them via Redis pub/sub.

    This is the single point of contact with Oanda for live prices.
    Workers subscribe to Redis instead of hitting Oanda directly.
    """

    def __init__(self) -> None:
        self.oanda = OandaClient()
        self.redis_client: aioredis.Redis | None = None
        self.running = False

    async def start(self) -> None:
        self.redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        self.running = True

        logger.info("poller_started", instruments=settings.default_instruments, interval=settings.poll_interval_seconds)

        try:
            while self.running:
                await self._poll_and_publish()
                await asyncio.sleep(settings.poll_interval_seconds)
        except Exception as e:
            logger.error("poller_error", error=str(e))
            raise
        finally:
            await self.stop()

    async def stop(self) -> None:
        self.running = False
        if self.redis_client:
            await self.redis_client.aclose()
        await self.oanda.close()
        logger.info("poller_stopped")

    async def _poll_and_publish(self) -> None:
        try:
            prices = await self.oanda.get_prices(settings.default_instruments)
            message = {"timestamp": datetime.now(timezone.utc).isoformat(), "prices": prices}
            await self.redis_client.publish(settings.redis_channel, json.dumps(message))
            logger.debug("prices_published", instruments=list(prices.keys()), channel=settings.redis_channel)
        except Exception as e:
            logger.error("poll_failed", error=str(e), instruments=settings.default_instruments)
