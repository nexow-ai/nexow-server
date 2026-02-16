"""Per-tick market data cache — fetch once, share across all agents."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from server.broker.models import Candle
from server.broker.oanda import OandaClient

logger = structlog.get_logger(__name__)


class MarketCache:
    """
    Deduplicates market data fetches within a single tick.

    Before evaluating agents, the loop calls prefetch() with every
    (instrument, timeframe) pair needed across all active agents.
    Each unique pair is fetched exactly once. Agents then read from
    the cache with zero I/O.

    Live prices are also updated in the background by the Redis
    subscriber, so even the batch price fetch is often a no-op.
    """

    def __init__(self, market: OandaClient) -> None:
        self._market = market
        self._candles: dict[str, list[Candle]] = {}
        self._prices: dict[str, float] = {}

    async def prefetch(self, instrument_configs: list[dict[str, Any]]) -> None:
        """Batch-fetch all unique candles and prices for this tick."""
        unique_candle_keys: set[tuple[str, str]] = set()
        unique_instruments: set[str] = set()

        for cfg in instrument_configs:
            inst = cfg["instrument"]
            tf = cfg.get("timeframe", "M5")
            unique_candle_keys.add((inst, tf))
            unique_instruments.add(inst)

        candle_tasks: list[tuple[str, asyncio.Task]] = []
        for inst, tf in unique_candle_keys:
            key = f"{inst}:{tf}"
            if key not in self._candles:
                task = asyncio.ensure_future(self._market.get_candles(instrument=inst, granularity=tf))
                candle_tasks.append((key, task))

        if candle_tasks:
            results = await asyncio.gather(*(t for _, t in candle_tasks), return_exceptions=True)
            for (key, _), result in zip(candle_tasks, results):
                if isinstance(result, Exception):
                    logger.warning("candle_fetch_failed", key=key, error=str(result))
                else:
                    self._candles[key] = result

        missing_prices = [i for i in unique_instruments if i not in self._prices]
        if missing_prices:
            try:
                batch = await self._market.get_prices(missing_prices)
                self._prices.update(batch)
            except Exception as exc:
                logger.warning("batch_price_failed", error=str(exc))
                price_tasks = {inst: asyncio.ensure_future(self._market.get_price(inst)) for inst in missing_prices}
                results = await asyncio.gather(*price_tasks.values(), return_exceptions=True)
                for inst, result in zip(price_tasks, results):
                    if not isinstance(result, Exception):
                        self._prices[inst] = result

        logger.debug("cache_prefetched", candle_keys=len(self._candles), price_keys=len(self._prices),
                      oanda_candle_calls=len(candle_tasks), oanda_price_calls=len(missing_prices))

    def update_prices(self, prices: dict[str, float]) -> None:
        """Update live prices from Redis pub/sub."""
        self._prices.update(prices)

    def get_candles(self, instrument: str, timeframe: str) -> list[Candle] | None:
        return self._candles.get(f"{instrument}:{timeframe}")

    def get_price(self, instrument: str) -> float | None:
        return self._prices.get(instrument)

    def clear_candles(self) -> None:
        self._candles.clear()

    def clear_all(self) -> None:
        self._candles.clear()
        self._prices.clear()
