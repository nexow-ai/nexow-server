"""Signal executor — evaluates a single bot or agent and records its signals."""

from __future__ import annotations

from typing import Any

import structlog

from nexow.strategies.base import BaseStrategy, Signal, SignalType
from nexow.strategies.bot import BotStrategy
from nexow.strategies.agent import AgentStrategy
from nexow.broker.models import Candle
from nexow.db.client import SupabaseClient

logger = structlog.get_logger(__name__)


class SignalExecutor:
    """
    Evaluates bot/agent strategies and records entry/exit signals.

    Bots and agents are pure signal providers — they do NOT place broker orders.
    Accounting is return-% based: each trade tracks the percentage change
    from entry to exit.
    """

    def __init__(self, db: SupabaseClient) -> None:
        self.db = db

    def _create_strategy(self, record: dict[str, Any]) -> BaseStrategy:
        record_id = record["id"]
        config = record.get("config", {})
        record_type = record.get("type", "bot")

        if record_type == "agent":
            return AgentStrategy(record_id, config)
        return BotStrategy(record_id, config)

    async def execute(
        self,
        record: dict[str, Any],
        candles: list[Candle],
        current_price: float,
    ) -> None:
        """Run one evaluation cycle for a single bot or agent on one instrument."""
        record_id = record["id"]
        instrument = candles[0].instrument if candles else record.get("instrument", "EUR_USD")
        is_agent = record.get("type") == "agent"

        try:
            strategy = self._create_strategy(record)
            signal: Signal = await strategy.evaluate(candles, current_price)

            logger.info(
                "signal_emitted",
                id=record_id[:8],
                name=record.get("name", "?"),
                type=record.get("type", "bot"),
                instrument=instrument,
                signal=signal.type.value,
                reason=signal.reason,
                confidence=f"{signal.confidence:.2f}",
            )

            # Log each evaluation to agent_logs so the bot console shows it (including hold)
            try:
                log_level = "trade" if signal.type in (SignalType.BUY, SignalType.SELL) else "info"
                self.db.insert_agent_log(
                    record_id,
                    log_level,
                    f"EVAL {signal.type.value.upper()} — {signal.reason}",
                    {"instrument": instrument},
                )
            except Exception as e:
                logger.debug("agent_log_insert_error", id=record_id[:8], error=str(e))

            evaluation_id: str | None = None
            if is_agent:
                evaluation_id = self._record_evaluation(record, signal, instrument)

            if signal.type == SignalType.HOLD:
                return

            if signal.type == SignalType.CLOSE:
                self._close_positions(record_id, instrument, current_price)
                return

            open_trades = self.db.get_open_trades(record_id)
            already_open = any(t["instrument"] == instrument for t in open_trades)
            if already_open:
                logger.debug("trade_skipped_already_open", id=record_id[:8], instrument=instrument)
                return

            trade_record: dict[str, Any] = {
                "agent_id": record_id,
                "instrument": instrument,
                "direction": signal.type.value,
                "entry_price": current_price,
                "status": "open",
                "stop_loss_pct": signal.stop_loss_pct,
                "take_profit_pct": signal.take_profit_pct,
            }
            if evaluation_id:
                trade_record["evaluation_id"] = evaluation_id

            self.db.insert_trade(trade_record)

            logger.info(
                "signal_recorded",
                id=record_id[:8],
                instrument=instrument,
                direction=signal.type.value,
                entry_price=current_price,
                sl_pct=signal.stop_loss_pct,
                tp_pct=signal.take_profit_pct,
            )

        except Exception as e:
            logger.error("execution_error", id=record_id, instrument=instrument, error=str(e))

    def _record_evaluation(self, record: dict[str, Any], signal: Signal, instrument: str) -> str | None:
        """Insert an agent_evaluations row and return its id."""
        meta = signal.metadata
        try:
            row = self.db.insert_evaluation({
                "agent_id": record["id"],
                "instrument": instrument,
                "action": signal.type.value,
                "confidence": signal.confidence,
                "reasoning": signal.reason,
                "technical_summary": meta.get("technical_summary"),
                "sentiment_summary": meta.get("sentiment_summary"),
                "data_sources_used": meta.get("data_sources_used", []),
                "prompt_tokens": meta.get("prompt_tokens", 0),
                "completion_tokens": meta.get("completion_tokens", 0),
                "total_tokens": meta.get("total_tokens", 0),
                "llm_provider": meta.get("llm_provider"),
                "llm_model": meta.get("llm_model"),
                "duration_ms": meta.get("duration_ms"),
            })
            return row.get("id")
        except Exception as e:
            logger.error("evaluation_insert_error", id=record["id"][:8], error=str(e))
            return None

    def _close_positions(self, record_id: str, instrument: str, current_price: float) -> None:
        open_trades = self.db.get_open_trades(record_id)

        for trade in open_trades:
            if trade.get("instrument") != instrument:
                continue

            entry = float(trade["entry_price"])
            direction = trade["direction"]

            if direction == "buy":
                return_pct = ((current_price - entry) / entry) * 100
            else:
                return_pct = ((entry - current_price) / entry) * 100

            self.db.close_trade(trade["id"], current_price, return_pct)

        logger.info("positions_closed", id=record_id[:8], instrument=instrument, price=current_price)
