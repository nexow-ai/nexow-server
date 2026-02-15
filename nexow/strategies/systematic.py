"""Systematic trading agent — powered by the dynamic Rule DSL interpreter."""

from __future__ import annotations

from typing import Any

import structlog

from nexow.strategies.base import AgentStrategy, Signal, SignalType
from nexow.broker.models import Candle
from nexow.rules.interpreter import MarketSnapshot, evaluate_rules

logger = structlog.get_logger(__name__)


class SystematicAgent(AgentStrategy):
    """
    Dynamic rule-based agent.

    The AI generates a JSON rule tree (buy_rules, sell_rules, close_rules).
    This agent feeds live market data into the rule interpreter
    and returns the resulting signal.

    Tracks the last candle time so that rules like "every_candle" or
    "always" only fire ONCE per new candle, not on every engine tick.
    """

    _last_candle_times: dict[str, float] = {}

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.rules = config.get("rules", {})

    async def evaluate(self, candles: list[Candle], current_price: float) -> Signal:
        instrument = candles[0].instrument if candles else "UNKNOWN"

        if not self.rules:
            return Signal(type=SignalType.HOLD, instrument=instrument, reason="No rules configured")

        if len(candles) < 2:
            return Signal(type=SignalType.HOLD, instrument=instrument, reason="Not enough candle data")

        cache_key = f"{self.agent_id}:{instrument}"
        ref_time = candles[-1].time.timestamp()
        prev_time = self._last_candle_times.get(cache_key, 0)

        if ref_time <= prev_time:
            return Signal(type=SignalType.HOLD, instrument=instrument, reason="Waiting for new candle")

        self._last_candle_times[cache_key] = ref_time

        snap = MarketSnapshot(candles, current_price)
        action = evaluate_rules(self.rules, snap)

        signal_map = {
            "buy": SignalType.BUY,
            "sell": SignalType.SELL,
            "close": SignalType.CLOSE,
            "hold": SignalType.HOLD,
        }

        signal_type = signal_map.get(action, SignalType.HOLD)
        reason = self._build_reason(action)

        exit_config = self.config.get("exit", {})
        sl_pct = exit_config.get("stop_loss_pct")
        tp_pct = exit_config.get("take_profit_pct")

        return Signal(
            type=signal_type,
            instrument=instrument,
            confidence=0.8 if signal_type != SignalType.HOLD else 0.0,
            stop_loss_pct=sl_pct if signal_type in (SignalType.BUY, SignalType.SELL) else None,
            take_profit_pct=tp_pct if signal_type in (SignalType.BUY, SignalType.SELL) else None,
            reason=reason,
        )

    def _build_reason(self, action: str) -> str:
        if action == "hold":
            return "No rules triggered"

        rule_key = f"{action}_rules"
        rules = self.rules.get(rule_key, {})
        if not rules:
            return f"Rule matched: {action}"

        conditions = rules.get("conditions", [])
        operator = rules.get("operator", "and")

        if operator == "always":
            return f"Always {action} (every new candle)"

        parts = []
        for c in conditions[:3]:
            ctype = c.get("type", "?")
            params = c.get("params", {})
            param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else ""
            parts.append(f"{ctype}({param_str})" if param_str else ctype)

        joiner = " AND " if operator == "and" else " OR "
        return f"Rule: {joiner.join(parts)}"
