"""Portfolio manager — handles multi-instrument signal evaluation."""

from __future__ import annotations

from typing import Any

import structlog

from nexow.strategies.base import BaseStrategy, Signal
from nexow.strategies.bot import BotStrategy
from nexow.strategies.agent import AgentStrategy
from nexow.broker.models import Candle

logger = structlog.get_logger(__name__)


class PortfolioManager:
    """
    Manages a portfolio of instruments for a single bot or agent.

    Handles per-instrument strategy evaluation. Position sizing and
    risk management are NOT part of this layer — bots and agents are
    pure signal providers compared by gross return %.
    """

    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.record_id = record["id"]
        self.config = record.get("config", {})
        self.record_type = record.get("type", "bot")

        portfolio = self.config.get("portfolio", {})
        self.instruments_config: list[dict[str, Any]] = portfolio.get(
            "instruments",
            record.get(
                "instruments",
                [{"instrument": record.get("instrument", "EUR_USD"), "timeframe": "M5"}],
            ),
        )

    @property
    def instruments(self) -> list[str]:
        return [ic["instrument"] for ic in self.instruments_config]

    def get_timeframe(self, instrument: str) -> str:
        for ic in self.instruments_config:
            if ic["instrument"] == instrument:
                return ic.get("timeframe", "M5")
        return "M5"

    def _create_strategy(self, instrument: str) -> BaseStrategy:
        if self.record_type == "agent":
            return AgentStrategy(self.record_id, self.config)
        return BotStrategy(self.record_id, self.config)

    async def evaluate_instrument(
        self,
        instrument: str,
        candles: list[Candle],
        current_price: float,
    ) -> Signal:
        strategy = self._create_strategy(instrument)
        return await strategy.evaluate(candles, current_price)
