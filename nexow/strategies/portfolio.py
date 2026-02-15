"""Portfolio agent — manages multi-instrument signal evaluation."""

from __future__ import annotations

from typing import Any

import structlog

from nexow.strategies.base import AgentStrategy, Signal
from nexow.strategies.discretionary import DiscretionaryAgent
from nexow.strategies.systematic import SystematicAgent
from nexow.broker.models import Candle

logger = structlog.get_logger(__name__)


class PortfolioAgent:
    """
    Manages a portfolio of instruments for a single agent.

    Handles per-instrument strategy evaluation. Position sizing and
    risk management are NOT part of this layer — agents are pure signal
    providers compared by gross return %.
    """

    def __init__(self, agent: dict[str, Any]) -> None:
        self.agent = agent
        self.agent_id = agent["id"]
        self.config = agent.get("config", {})
        self.agent_type = agent.get("type", "bot")

        portfolio = self.config.get("portfolio", {})
        self.instruments_config: list[dict[str, Any]] = portfolio.get(
            "instruments",
            agent.get(
                "instruments",
                [{"instrument": agent.get("instrument", "EUR_USD"), "timeframe": "M5"}],
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

    def _create_strategy(self, instrument: str) -> AgentStrategy:
        if self.agent_type == "agent":
            return DiscretionaryAgent(self.agent_id, self.config)
        return SystematicAgent(self.agent_id, self.config)

    async def evaluate_instrument(
        self,
        instrument: str,
        candles: list[Candle],
        current_price: float,
    ) -> Signal:
        strategy = self._create_strategy(instrument)
        return await strategy.evaluate(candles, current_price)
