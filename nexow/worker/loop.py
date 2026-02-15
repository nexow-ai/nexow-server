"""Main worker loop — concurrent agent evaluation with market data caching."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import redis.asyncio as aioredis
import structlog

from nexow.ai.factory import generate_strategy
from nexow.broker.oanda import OandaClient
from nexow.config import settings
from nexow.db.client import SupabaseClient
from nexow.strategies.portfolio import PortfolioAgent
from nexow.worker.cache import MarketCache
from nexow.worker.executor import AgentExecutor

logger = structlog.get_logger(__name__)


class WorkerLoop:
    """
    High-throughput agent execution engine.

    Background tasks (never block the tick loop):
      - _pending_loop   : AI config generation
      - _subscribe_prices : Redis price updates

    Tick loop (every N seconds):
      1. Sync SL/TP using cached prices
      2. Collect all (instrument, tf) pairs from agents
      3. Batch-prefetch candles + prices (deduped)
      4. Evaluate all agents concurrently
    """

    def __init__(self) -> None:
        self.db = SupabaseClient()
        self.market = OandaClient()
        self.executor = AgentExecutor(self.db)
        self.cache = MarketCache(self.market)
        self._running = False
        self._eval_semaphore = asyncio.Semaphore(settings.max_concurrent_evaluations)

        self._pending_task: asyncio.Task | None = None
        self._price_sub_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True

        self._pending_task = asyncio.create_task(self._pending_loop())
        self._price_sub_task = asyncio.create_task(self._subscribe_prices())

        logger.info("worker_started", tick_interval=settings.tick_interval_seconds,
                     max_concurrent=settings.max_concurrent_evaluations)

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("tick_error", error=str(e))
            await asyncio.sleep(settings.tick_interval_seconds)

    async def stop(self) -> None:
        logger.info("worker_stopping")
        self._running = False

        for task in (self._pending_task, self._price_sub_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        await self.market.close()
        logger.info("worker_stopped")

    # ------------------------------------------------------------------
    # Background: AI config generation
    # ------------------------------------------------------------------

    async def _pending_loop(self) -> None:
        while self._running:
            try:
                await self._process_pending()
            except Exception as e:
                logger.error("pending_loop_error", error=str(e))
            await asyncio.sleep(settings.pending_check_interval_seconds)

    async def _process_pending(self) -> None:
        try:
            pending = self.db.get_pending_agents()
        except Exception:
            return

        for agent in pending:
            prompt = agent.get("prompt", "")
            if not prompt:
                continue

            logger.info("generating_agent_config", agent_id=agent["id"], prompt=prompt[:80])
            try:
                result = await generate_strategy(prompt)
                config = result.config
                portfolio = config.get("portfolio", {})
                instruments = portfolio.get("instruments", [])

                update_data: dict = {
                    "name": result.name,
                    "description": result.description,
                    "type": result.agent_type.value if hasattr(result.agent_type, "value") else result.agent_type,
                    "config": config,
                    "status": "active",
                }
                if instruments:
                    update_data["instruments"] = instruments
                    update_data["instrument"] = instruments[0].get("instrument", "EUR_USD")
                    update_data["timeframe"] = instruments[0].get("timeframe", "M5")

                self.db.update_agent_config(agent["id"], config)
                self.db.client.table("agents").update(update_data).eq("id", agent["id"]).execute()
                logger.info("agent_activated", agent_id=agent["id"], name=result.name)
            except Exception as e:
                logger.error("agent_generation_failed", agent_id=agent["id"], error=str(e))

    # ------------------------------------------------------------------
    # Background: Redis price subscriber
    # ------------------------------------------------------------------

    async def _subscribe_prices(self) -> None:
        try:
            redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(settings.redis_channel)
            logger.info("redis_price_subscriber_started", channel=settings.redis_channel)

            async for message in pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    prices = data.get("prices", {})
                    if prices:
                        self.cache.update_prices(prices)
                except Exception as e:
                    logger.debug("redis_price_parse_error", error=str(e))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("redis_subscriber_error", error=str(e))
        finally:
            try:
                await pubsub.unsubscribe(settings.redis_channel)
                await redis_client.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Main tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        await self._sync_trades()

        agents = self.db.get_active_agents()
        if not agents:
            return

        all_instrument_configs: list[dict[str, Any]] = []
        agent_portfolios: list[tuple[dict, PortfolioAgent]] = []

        for agent in agents:
            try:
                portfolio = PortfolioAgent(agent)
                agent_portfolios.append((agent, portfolio))
                all_instrument_configs.extend(portfolio.instruments_config)
            except Exception as e:
                logger.error("portfolio_init_error", agent_id=agent.get("id", "?"), error=str(e))

        await self.cache.prefetch(all_instrument_configs)

        tasks = [self._evaluate_agent(agent, portfolio) for agent, portfolio in agent_portfolios]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (agent, _), result in zip(agent_portfolios, results):
            if isinstance(result, Exception):
                logger.error("agent_eval_unhandled", agent_id=agent.get("id", "?"), error=str(result))

        self.cache.clear_candles()
        logger.debug("tick_complete", active_agents=len(agents), evaluated=len(tasks))

    async def _evaluate_agent(self, agent: dict, portfolio: PortfolioAgent) -> None:
        async with self._eval_semaphore:
            for inst_config in portfolio.instruments_config:
                instrument = inst_config["instrument"]
                timeframe = inst_config.get("timeframe", "M5")

                schedule = agent.get("evaluation_schedule", "every_tick")
                if schedule != "every_tick" and agent.get("type") == "agent":
                    continue

                candles = self.cache.get_candles(instrument, timeframe)
                price = self.cache.get_price(instrument)

                if candles is None or price is None:
                    logger.warning("cache_miss_skipping", agent_id=agent["id"][:8],
                                   instrument=instrument, timeframe=timeframe)
                    continue

                await self.executor.execute(agent, candles, price)

    # ------------------------------------------------------------------
    # SL/TP sync
    # ------------------------------------------------------------------

    async def _sync_trades(self) -> None:
        try:
            open_trades = self.db.get_all_open_trades()
            if not open_trades:
                return

            by_instrument: dict[str, list[dict]] = {}
            for trade in open_trades:
                by_instrument.setdefault(trade["instrument"], []).append(trade)

            missing = [i for i in by_instrument if self.cache.get_price(i) is None]
            if missing:
                try:
                    self.cache.update_prices(await self.market.get_prices(missing))
                except Exception:
                    pass

            for inst, trades in by_instrument.items():
                price = self.cache.get_price(inst)
                if price is None:
                    continue

                for trade in trades:
                    sl_pct = float(trade["stop_loss_pct"]) if trade.get("stop_loss_pct") else None
                    tp_pct = float(trade["take_profit_pct"]) if trade.get("take_profit_pct") else None
                    if sl_pct is None and tp_pct is None:
                        continue

                    entry = float(trade["entry_price"])
                    direction = trade["direction"]
                    current_return = ((price - entry) / entry) * 100 if direction == "buy" else ((entry - price) / entry) * 100

                    exit_triggered = False
                    exit_reason = ""
                    if sl_pct is not None and current_return <= -sl_pct:
                        exit_triggered, exit_reason = True, "SL"
                    if tp_pct is not None and current_return >= tp_pct:
                        exit_triggered, exit_reason = True, "TP"

                    if exit_triggered:
                        self.db.close_trade(trade["id"], price, current_return)
                        logger.info("trade_sl_tp_hit", trade_id=trade["id"][:8], instrument=inst,
                                    exit_price=price, return_pct=round(current_return, 4), reason=exit_reason)

        except Exception as e:
            logger.debug("trade_sync_error", error=str(e))
