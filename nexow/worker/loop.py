"""Main worker loop — concurrent bot/agent evaluation with market data caching."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import redis.asyncio as aioredis
import structlog

from nexow.ai.bot_factory import generate_bot
from nexow.ai.factory import generate_agent
from nexow.broker.oanda import OandaClient
from nexow.config import settings
from nexow.db.client import SupabaseClient
from nexow.strategies.base import SignalType
from nexow.strategies.portfolio import PortfolioManager
from nexow.strategies.reactor import ReactorStrategy
from nexow.worker.cache import MarketCache
from nexow.worker.claim_lock import ClaimKey, ClaimLock
from nexow.worker.executor import SignalExecutor
from nexow.worker.scheduling import granularity_seconds, normalize_granularity

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
        self.executor = SignalExecutor(self.db)
        self.cache = MarketCache(self.market)
        self._running = False
        self._eval_semaphore = asyncio.Semaphore(settings.max_concurrent_evaluations)

        self._pending_task: asyncio.Task | None = None
        self._price_sub_task: asyncio.Task | None = None
        self._claim_redis: aioredis.Redis | None = None
        self._claim_lock: ClaimLock | None = None
        self._last_eval_candle_ts: dict[str, float] = {}
        self._worker_id = os.getenv("FLY_MACHINE_ID") or str(uuid.uuid4())

    async def start(self) -> None:
        self._running = True

        self._claim_redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        self._claim_lock = ClaimLock(self._claim_redis, owner_id=self._worker_id)

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

        if self._claim_redis:
            try:
                await self._claim_redis.aclose()
            except Exception:
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

            agent_type = agent.get("type", "bot")
            logger.info("generating_config", agent_id=agent["id"], type=agent_type, prompt=prompt[:80])
            try:
                if agent_type == "agent":
                    result = await generate_agent(prompt)
                else:
                    result = await generate_bot(prompt)

                config = result.config
                # For bots with strategy_code, store it inside config
                if hasattr(result, "strategy_code") and result.strategy_code:
                    config["strategy_code"] = result.strategy_code

                portfolio = config.get("portfolio", {})
                instruments = portfolio.get("instruments", [])

                update_data: dict = {
                    "name": result.name,
                    "description": result.description,
                    "type": agent_type,
                    "config": config,
                    "status": "active",
                }
                if instruments:
                    update_data["instruments"] = instruments
                    update_data["instrument"] = instruments[0].get("instrument", "EUR_USD")
                    update_data["timeframe"] = instruments[0].get("timeframe", "M5")

                self.db.update_agent_config(agent["id"], config)
                self.db.client.table("agents").update(update_data).eq("id", agent["id"]).execute()
                logger.info("config_generated", agent_id=agent["id"], type=agent_type, name=result.name)
            except Exception as e:
                logger.error("config_generation_failed", agent_id=agent["id"], type=agent_type, error=str(e))

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

        # Build all (instrument, timeframe) pairs we need to evaluate.
        eval_targets: list[tuple[dict, str, str]] = []
        all_instrument_configs: list[dict[str, Any]] = []

        # ----- Agents / Bots -----
        agents = self.db.get_active_agents()
        for agent in agents:
            try:
                portfolio = PortfolioManager(agent)
                for inst_cfg in portfolio.instruments_config:
                    instrument = inst_cfg["instrument"]
                    timeframe = self._effective_timeframe(agent, inst_cfg)
                    eval_targets.append((agent, instrument, timeframe))
                    all_instrument_configs.append({"instrument": instrument, "timeframe": timeframe})
            except Exception as e:
                logger.error("portfolio_init_error", agent_id=agent.get("id", "?"), error=str(e))

        # ----- Reactor configs -----
        reactor_configs = self.db.get_active_reactor_configs()
        reactor_targets: list[tuple[dict, str, str]] = []
        for rc in reactor_configs:
            instrument = rc.get("instrument", "EUR_USD")
            timeframe = normalize_granularity(rc.get("timeframe", "H1")) or "H1"
            reactor_targets.append((rc, instrument, timeframe))
            all_instrument_configs.append({"instrument": instrument, "timeframe": timeframe})

        if not eval_targets and not reactor_targets:
            return

        await self.cache.prefetch(all_instrument_configs)

        # Evaluate agents
        if eval_targets:
            tasks = [self._evaluate_one(agent, instrument, timeframe) for agent, instrument, timeframe in eval_targets]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for (agent, instrument, timeframe), result in zip(eval_targets, results):
                if isinstance(result, Exception):
                    logger.error(
                        "agent_eval_unhandled",
                        agent_id=agent.get("id", "?"),
                        instrument=instrument,
                        timeframe=timeframe,
                        error=str(result),
                    )

        # Evaluate reactors
        if reactor_targets:
            reactor_tasks = [
                self._evaluate_reactor(rc, instrument, timeframe)
                for rc, instrument, timeframe in reactor_targets
            ]
            reactor_results = await asyncio.gather(*reactor_tasks, return_exceptions=True)
            for (rc, instrument, timeframe), result in zip(reactor_targets, reactor_results):
                if isinstance(result, Exception):
                    logger.error(
                        "reactor_eval_unhandled",
                        reactor_id=rc.get("id", "?")[:8],
                        instrument=instrument,
                        error=str(result),
                    )

        self.cache.clear_candles()
        logger.debug("tick_complete", active_agents=len(agents), reactors=len(reactor_configs))

    def _effective_timeframe(self, agent: dict, inst_cfg: dict[str, Any]) -> str:
        """Choose which candle timeframe drives evaluation for this agent/bot.

        - Bots: use the instrument timeframe in the portfolio config.
        - Agents: use `evaluation_schedule` (top-level or config) when present,
          otherwise use the instrument timeframe.
        """
        tf = inst_cfg.get("timeframe", "M5")
        if agent.get("type") == "agent":
            schedule = agent.get("evaluation_schedule") or agent.get("config", {}).get("evaluation_schedule")
            schedule_tf = normalize_granularity(schedule)
            if schedule_tf:
                return schedule_tf
        return normalize_granularity(tf) or tf

    async def _evaluate_one(self, agent: dict, instrument: str, timeframe: str) -> None:
        async with self._eval_semaphore:
            candles = self.cache.get_candles(instrument, timeframe)
            price = self.cache.get_price(instrument)

            if candles is None or price is None or not candles:
                logger.debug(
                    "cache_miss_skipping",
                    agent_id=agent["id"][:8],
                    instrument=instrument,
                    timeframe=timeframe,
                )
                return

            latest_ts = candles[-1].time.timestamp()
            last_key = f"{agent['id']}:{instrument}:{timeframe}"
            last_seen = self._last_eval_candle_ts.get(last_key, 0.0)
            if latest_ts <= last_seen:
                return

            # Cross-worker claim/lock: one evaluation per candle per agent.
            if not self._claim_lock:
                return
            ttl = max(settings.claim_lock_min_ttl_seconds, granularity_seconds(timeframe))
            claim = ClaimKey(
                agent_id=agent["id"],
                instrument=instrument,
                timeframe=timeframe,
                candle_ts=int(latest_ts),
            )
            if not await self._claim_lock.try_claim(claim, ttl_seconds=ttl):
                return

            await self.executor.execute(agent, candles, price)
            self._last_eval_candle_ts[last_key] = latest_ts

    async def _evaluate_reactor(self, config: dict, instrument: str, timeframe: str) -> None:
        """Evaluate a single reactor config against snapshot analyses."""
        async with self._eval_semaphore:
            candles = self.cache.get_candles(instrument, timeframe)
            price = self.cache.get_price(instrument)

            if candles is None or price is None or not candles:
                return

            # Same candle-dedup logic as agents
            latest_ts = candles[-1].time.timestamp()
            last_key = f"reactor:{config['id']}:{instrument}:{timeframe}"
            last_seen = self._last_eval_candle_ts.get(last_key, 0.0)
            if latest_ts <= last_seen:
                return

            # Cross-worker claim lock
            if not self._claim_lock:
                return
            ttl = max(settings.claim_lock_min_ttl_seconds, granularity_seconds(timeframe))
            claim = ClaimKey(
                agent_id=f"reactor:{config['id']}",
                instrument=instrument,
                timeframe=timeframe,
                candle_ts=int(latest_ts),
            )
            if not await self._claim_lock.try_claim(claim, ttl_seconds=ttl):
                return

            strategy = ReactorStrategy(config, self.db)
            signal = strategy.evaluate(candles, price)

            logger.info(
                "reactor_signal",
                reactor_id=config["id"][:8],
                instrument=instrument,
                signal=signal.type.value,
                confidence=f"{signal.confidence:.3f}",
                reason=signal.reason,
            )

            self._last_eval_candle_ts[last_key] = latest_ts

            if signal.type in (SignalType.BUY, SignalType.SELL):
                trade_record = {
                    "reactor_config_id": config["id"],
                    "instrument": instrument,
                    "direction": signal.type.value,
                    "entry_price": price,
                    "status": "open",
                    "stop_loss_pct": signal.stop_loss_pct,
                    "take_profit_pct": signal.take_profit_pct,
                }
                self.db.insert_reactor_trade(trade_record)
                logger.info(
                    "reactor_trade_opened",
                    reactor_id=config["id"][:8],
                    instrument=instrument,
                    direction=signal.type.value,
                    entry_price=price,
                    sl_pct=signal.stop_loss_pct,
                    tp_pct=signal.take_profit_pct,
                )

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
