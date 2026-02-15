"""Abstract base class for all trading agent strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel

from nexow.broker.models import Candle


class SignalType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"
    HOLD = "hold"


class Signal(BaseModel):
    """
    A trading signal emitted by an agent.

    Agents are pure signal providers — they emit entry/exit signals
    with optional percentage-based stop-loss and take-profit levels.
    """

    type: SignalType
    instrument: str
    confidence: float = 1.0
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    reason: str = ""


class AgentStrategy(ABC):
    """
    Abstract interface every agent must implement.

    Agents receive candle data and their config,
    and return a Signal indicating what action to take.
    """

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.config = config

    @abstractmethod
    async def evaluate(self, candles: list[Candle], current_price: float) -> Signal:
        """Evaluate market data and return a trading signal."""
        ...

    def get_param(self, key: str, default: Any = None) -> Any:
        """Helper to safely read a config parameter."""
        return self.config.get(key, default)
