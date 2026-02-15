"""
Rule DSL Schema — Pydantic models for AI-generated trading rules.

The AI generates a JSON rule tree. The engine interprets it against live market data.
Any trading idea expressible in logic can be represented.

Example: "Buy when RSI < 30 and last candle is green"
{
  "buy_rules": {
    "operator": "and",
    "conditions": [
      {"type": "rsi_below", "params": {"threshold": 30, "period": 14}},
      {"type": "candle_is_green"}
    ]
  }
}

Example: "Buy every candle"
{
  "buy_rules": {
    "operator": "always"
  }
}

Example: "Buy when price drops 0.5% in 3 candles then bounces"
{
  "buy_rules": {
    "operator": "and",
    "conditions": [
      {"type": "price_dropped_pct", "params": {"pct": 0.5, "lookback": 3}},
      {"type": "candle_is_green"}
    ]
  }
}
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────
# Condition nodes
# ──────────────────────────────────────────────────────────

class Condition(BaseModel):
    """A single atomic condition evaluated against market data."""

    type: str = Field(description="Condition type identifier")
    params: dict[str, Any] = Field(default_factory=dict, description="Parameters for this condition")


class RuleGroup(BaseModel):
    """
    A group of conditions combined with a logical operator.

    operator:
      - "and": ALL conditions must be true
      - "or": ANY condition must be true
      - "not": Inverts the first condition
      - "always": Always true (no conditions needed)
      - "never": Always false
    """

    operator: Literal["and", "or", "not", "always", "never"] = Field(default="and")
    conditions: list[Condition | RuleGroup] = Field(
        default_factory=list,
        description="List of conditions or nested rule groups",
    )


# ──────────────────────────────────────────────────────────
# Full rule set for an agent
# ──────────────────────────────────────────────────────────

class TradingRules(BaseModel):
    """
    Complete trading rules for a systematic agent.

    buy_rules: When to open a long position.
    sell_rules: When to open a short position.
    close_rules: When to close all positions. (optional)
    """

    buy_rules: RuleGroup = Field(
        default_factory=lambda: RuleGroup(operator="never"),
        description="Conditions that trigger a BUY signal",
    )
    sell_rules: RuleGroup = Field(
        default_factory=lambda: RuleGroup(operator="never"),
        description="Conditions that trigger a SELL signal",
    )
    close_rules: RuleGroup | None = Field(
        default=None,
        description="Conditions that trigger closing all positions",
    )


# ──────────────────────────────────────────────────────────
# Available condition types (documentation for the AI)
# ──────────────────────────────────────────────────────────

CONDITION_CATALOG = """
## Price conditions
- "price_above": params {threshold: float} — current price > threshold
- "price_below": params {threshold: float} — current price < threshold
- "price_change_pct_up": params {pct: float, lookback: int} — price rose by ≥pct% over last N candles
- "price_change_pct_down": params {pct: float, lookback: int} — price fell by ≥pct% over last N candles
- "price_dropped_pct": params {pct: float, lookback: int} — price dropped ≥pct% (absolute) in last N candles
- "price_near_high": params {lookback: int, within_pct: float} — price within X% of recent high
- "price_near_low": params {lookback: int, within_pct: float} — price within X% of recent low

## Candle conditions
- "candle_is_green": no params — last candle close > open
- "candle_is_red": no params — last candle close < open
- "candle_body_gt": params {pips: float} — last candle body size > N pips
- "consecutive_green": params {count: int} — last N candles all green
- "consecutive_red": params {count: int} — last N candles all red
- "doji": no params — last candle body < 20% of range (indecision)
- "engulfing_bullish": no params — bullish engulfing pattern
- "engulfing_bearish": no params — bearish engulfing pattern

## Indicator conditions
- "rsi_above": params {threshold: float, period: int=14}
- "rsi_below": params {threshold: float, period: int=14}
- "macd_cross_up": params {fast: int=12, slow: int=26, signal: int=9} — bullish MACD crossover
- "macd_cross_down": params {fast: int=12, slow: int=26, signal: int=9} — bearish MACD crossover
- "macd_positive": params {fast: int=12, slow: int=26, signal: int=9} — MACD histogram > 0
- "macd_negative": params {fast: int=12, slow: int=26, signal: int=9}
- "ema_cross_up": params {fast: int=9, slow: int=21} — fast EMA crosses above slow
- "ema_cross_down": params {fast: int=9, slow: int=21}
- "price_above_ema": params {period: int} — price above EMA
- "price_below_ema": params {period: int}
- "price_above_bb_upper": params {period: int=20, std: float=2.0}
- "price_below_bb_lower": params {period: int=20, std: float=2.0}
- "bb_squeeze": params {period: int=20, std: float=2.0, squeeze_pct: float=1.0} — bands narrowing

## Volume conditions
- "volume_above_avg": params {period: int=20, multiplier: float=1.5}
- "volume_below_avg": params {period: int=20, multiplier: float=0.5}
- "volume_spike": params {period: int=20, multiplier: float=2.0}

## Time / frequency
- "every_candle": no params — always true (for DCA)
- "every_n_candles": params {n: int} — true every Nth candle

## Meta
- "has_no_open_trades": no params — true when agent has 0 open positions
- "has_open_trades": no params — true when agent has ≥1 open position
"""
