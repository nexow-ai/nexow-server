"""
Rule Interpreter — evaluates a JSON rule tree against live market data.

This is the core of the dynamic strategy engine. The AI generates
the rules, this module executes them.

Supports multi-timeframe evaluation: each condition can specify a
"timeframe" param to target a specific MarketSnapshot within a
MultiSnapshot container.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import structlog
import ta

from nexow.broker.models import Candle
from nexow.rules.schema import Condition, RuleGroup, TradingRules

logger = structlog.get_logger(__name__)


class MarketSnapshot:
    """Pre-computed market data available to all condition evaluators."""

    def __init__(self, candles: list[Candle], current_price: float, open_trade_count: int = 0) -> None:
        self.candles = candles
        self.price = current_price
        self.open_trade_count = open_trade_count
        self.tick_count = 0  # incremented externally for every_n_candles

        # Build DataFrame once
        self.df = pd.DataFrame([c.model_dump() for c in candles])
        if not self.df.empty:
            self.df.set_index("time", inplace=True)
            self.df.sort_index(inplace=True)

        self.closes: np.ndarray = self.df["close"].values if not self.df.empty else np.array([])
        self.opens: np.ndarray = self.df["open"].values if not self.df.empty else np.array([])
        self.highs: np.ndarray = self.df["high"].values if not self.df.empty else np.array([])
        self.lows: np.ndarray = self.df["low"].values if not self.df.empty else np.array([])
        self.volumes: np.ndarray = self.df["volume"].values if not self.df.empty else np.array([])

        # Cache computed indicators
        self._cache: dict[str, Any] = {}

    def rsi(self, period: int = 14) -> float | None:
        key = f"rsi_{period}"
        if key not in self._cache:
            if len(self.closes) < period + 1:
                self._cache[key] = None
            else:
                r = ta.momentum.RSIIndicator(pd.Series(self.closes), window=period).rsi()
                self._cache[key] = float(r.iloc[-1]) if not np.isnan(r.iloc[-1]) else None
        return self._cache[key]

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float] | None:
        key = f"macd_{fast}_{slow}_{signal}"
        if key not in self._cache:
            if len(self.closes) < slow + signal:
                self._cache[key] = None
            else:
                m = ta.trend.MACD(pd.Series(self.closes), window_fast=fast, window_slow=slow, window_sign=signal)
                ml = m.macd()
                sl = m.macd_signal()
                self._cache[key] = {
                    "macd": float(ml.iloc[-1]),
                    "signal": float(sl.iloc[-1]),
                    "prev_macd": float(ml.iloc[-2]),
                    "prev_signal": float(sl.iloc[-2]),
                    "histogram": float(ml.iloc[-1] - sl.iloc[-1]),
                }
        return self._cache[key]

    def ema(self, period: int) -> float | None:
        key = f"ema_{period}"
        if key not in self._cache:
            if len(self.closes) < period:
                self._cache[key] = None
            else:
                e = pd.Series(self.closes).ewm(span=period).mean()
                self._cache[key] = float(e.iloc[-1])
        return self._cache[key]

    def ema_prev(self, period: int) -> float | None:
        key = f"ema_prev_{period}"
        if key not in self._cache:
            if len(self.closes) < period + 1:
                self._cache[key] = None
            else:
                e = pd.Series(self.closes).ewm(span=period).mean()
                self._cache[key] = float(e.iloc[-2])
        return self._cache[key]

    def bollinger(self, period: int = 20, std: float = 2.0) -> dict[str, float] | None:
        key = f"bb_{period}_{std}"
        if key not in self._cache:
            if len(self.closes) < period:
                self._cache[key] = None
            else:
                bb = ta.volatility.BollingerBands(pd.Series(self.closes), window=period, window_dev=std)
                self._cache[key] = {
                    "upper": float(bb.bollinger_hband().iloc[-1]),
                    "middle": float(bb.bollinger_mavg().iloc[-1]),
                    "lower": float(bb.bollinger_lband().iloc[-1]),
                    "width": float(bb.bollinger_wband().iloc[-1]),
                }
        return self._cache[key]


# ──────────────────────────────────────────────────────────
# Multi-timeframe snapshot container
# ──────────────────────────────────────────────────────────

class MultiSnapshot:
    """
    Wraps multiple MarketSnapshots keyed by timeframe.

    Each condition can include a "timeframe" param to select which
    snapshot it evaluates against. Conditions without the param
    use the default (primary/fastest) timeframe.
    """

    def __init__(self, snapshots: dict[str, MarketSnapshot], default_tf: str) -> None:
        self.snapshots = snapshots
        self.default_tf = default_tf

    def get(self, timeframe: str | None = None) -> MarketSnapshot:
        """Get the snapshot for a given timeframe, falling back to default."""
        tf = timeframe or self.default_tf
        return self.snapshots.get(tf, self.snapshots[self.default_tf])

    @property
    def price(self) -> float:
        return self.snapshots[self.default_tf].price

    @property
    def open_trade_count(self) -> int:
        return self.snapshots[self.default_tf].open_trade_count


# ──────────────────────────────────────────────────────────
# Condition evaluators
# ──────────────────────────────────────────────────────────

def _eval_condition(condition: Condition, multi: MultiSnapshot) -> bool:
    """Evaluate a single atomic condition.

    The condition's params may include a "timeframe" key to select
    which snapshot to evaluate against. If absent, the default
    (primary) timeframe is used.
    """
    t = condition.type
    p = condition.params

    # Resolve the right snapshot for this condition's timeframe
    snap = multi.get(p.get("timeframe"))

    # ── Price conditions ──
    if t == "price_above":
        return snap.price > p.get("threshold", 0)
    if t == "price_below":
        return snap.price < p.get("threshold", float("inf"))
    if t == "price_change_pct_up":
        lookback = int(p.get("lookback", 3))
        pct = float(p.get("pct", 0.1))
        if len(snap.closes) < lookback + 1:
            return False
        change = (snap.closes[-1] - snap.closes[-(lookback + 1)]) / snap.closes[-(lookback + 1)] * 100
        return change >= pct
    if t == "price_change_pct_down":
        lookback = int(p.get("lookback", 3))
        pct = float(p.get("pct", 0.1))
        if len(snap.closes) < lookback + 1:
            return False
        change = (snap.closes[-1] - snap.closes[-(lookback + 1)]) / snap.closes[-(lookback + 1)] * 100
        return change <= -pct
    if t == "price_dropped_pct":
        lookback = int(p.get("lookback", 3))
        pct = float(p.get("pct", 0.5))
        if len(snap.closes) < lookback + 1:
            return False
        change = abs((snap.closes[-1] - snap.closes[-(lookback + 1)]) / snap.closes[-(lookback + 1)] * 100)
        return change >= pct and snap.closes[-1] < snap.closes[-(lookback + 1)]
    if t == "price_near_high":
        lookback = int(p.get("lookback", 20))
        within_pct = float(p.get("within_pct", 1.0))
        if len(snap.highs) < lookback:
            return False
        recent_high = float(np.max(snap.highs[-lookback:]))
        return (recent_high - snap.price) / recent_high * 100 <= within_pct
    if t == "price_near_low":
        lookback = int(p.get("lookback", 20))
        within_pct = float(p.get("within_pct", 1.0))
        if len(snap.lows) < lookback:
            return False
        recent_low = float(np.min(snap.lows[-lookback:]))
        if recent_low == 0:
            return False
        return (snap.price - recent_low) / recent_low * 100 <= within_pct

    # ── Candle conditions ──
    if t == "candle_is_green":
        return len(snap.closes) > 0 and snap.closes[-1] > snap.opens[-1]
    if t == "candle_is_red":
        return len(snap.closes) > 0 and snap.closes[-1] < snap.opens[-1]
    if t == "candle_body_gt":
        pips = float(p.get("pips", 5))
        if len(snap.closes) == 0:
            return False
        body = abs(snap.closes[-1] - snap.opens[-1])
        pip_value = 0.0001
        if any(x in snap.candles[0].instrument for x in ["JPY"]):
            pip_value = 0.01
        if "XAU" in snap.candles[0].instrument:
            pip_value = 0.1
        return body / pip_value >= pips
    if t == "consecutive_green":
        count = int(p.get("count", 3))
        if len(snap.closes) < count:
            return False
        return all(snap.closes[-(i + 1)] > snap.opens[-(i + 1)] for i in range(count))
    if t == "consecutive_red":
        count = int(p.get("count", 3))
        if len(snap.closes) < count:
            return False
        return all(snap.closes[-(i + 1)] < snap.opens[-(i + 1)] for i in range(count))
    if t == "doji":
        if len(snap.closes) == 0:
            return False
        body = abs(snap.closes[-1] - snap.opens[-1])
        rng = snap.highs[-1] - snap.lows[-1]
        return rng > 0 and body / rng < 0.2
    if t == "engulfing_bullish":
        if len(snap.closes) < 2:
            return False
        prev_red = snap.closes[-2] < snap.opens[-2]
        curr_green = snap.closes[-1] > snap.opens[-1]
        engulfs = snap.closes[-1] > snap.opens[-2] and snap.opens[-1] < snap.closes[-2]
        return prev_red and curr_green and engulfs
    if t == "engulfing_bearish":
        if len(snap.closes) < 2:
            return False
        prev_green = snap.closes[-2] > snap.opens[-2]
        curr_red = snap.closes[-1] < snap.opens[-1]
        engulfs = snap.closes[-1] < snap.opens[-2] and snap.opens[-1] > snap.closes[-2]
        return prev_green and curr_red and engulfs

    # ── Indicator conditions ──
    if t == "rsi_above":
        rsi = snap.rsi(int(p.get("period", 14)))
        return rsi is not None and rsi > float(p.get("threshold", 70))
    if t == "rsi_below":
        rsi = snap.rsi(int(p.get("period", 14)))
        return rsi is not None and rsi < float(p.get("threshold", 30))
    if t == "macd_cross_up":
        m = snap.macd(int(p.get("fast", 12)), int(p.get("slow", 26)), int(p.get("signal", 9)))
        if m is None:
            return False
        return m["prev_macd"] <= m["prev_signal"] and m["macd"] > m["signal"]
    if t == "macd_cross_down":
        m = snap.macd(int(p.get("fast", 12)), int(p.get("slow", 26)), int(p.get("signal", 9)))
        if m is None:
            return False
        return m["prev_macd"] >= m["prev_signal"] and m["macd"] < m["signal"]
    if t == "macd_positive":
        m = snap.macd(int(p.get("fast", 12)), int(p.get("slow", 26)), int(p.get("signal", 9)))
        return m is not None and m["histogram"] > 0
    if t == "macd_negative":
        m = snap.macd(int(p.get("fast", 12)), int(p.get("slow", 26)), int(p.get("signal", 9)))
        return m is not None and m["histogram"] < 0
    if t == "ema_cross_up":
        fast_now = snap.ema(int(p.get("fast", 9)))
        slow_now = snap.ema(int(p.get("slow", 21)))
        fast_prev = snap.ema_prev(int(p.get("fast", 9)))
        slow_prev = snap.ema_prev(int(p.get("slow", 21)))
        if any(v is None for v in [fast_now, slow_now, fast_prev, slow_prev]):
            return False
        return fast_prev <= slow_prev and fast_now > slow_now  # type: ignore
    if t == "ema_cross_down":
        fast_now = snap.ema(int(p.get("fast", 9)))
        slow_now = snap.ema(int(p.get("slow", 21)))
        fast_prev = snap.ema_prev(int(p.get("fast", 9)))
        slow_prev = snap.ema_prev(int(p.get("slow", 21)))
        if any(v is None for v in [fast_now, slow_now, fast_prev, slow_prev]):
            return False
        return fast_prev >= slow_prev and fast_now < slow_now  # type: ignore
    if t == "price_above_ema":
        e = snap.ema(int(p.get("period", 21)))
        return e is not None and snap.price > e
    if t == "price_below_ema":
        e = snap.ema(int(p.get("period", 21)))
        return e is not None and snap.price < e
    if t == "price_above_bb_upper":
        bb = snap.bollinger(int(p.get("period", 20)), float(p.get("std", 2.0)))
        return bb is not None and snap.price > bb["upper"]
    if t == "price_below_bb_lower":
        bb = snap.bollinger(int(p.get("period", 20)), float(p.get("std", 2.0)))
        return bb is not None and snap.price < bb["lower"]
    if t == "bb_squeeze":
        bb = snap.bollinger(int(p.get("period", 20)), float(p.get("std", 2.0)))
        squeeze_pct = float(p.get("squeeze_pct", 1.0))
        return bb is not None and bb["width"] < squeeze_pct / 100

    # ── Volume conditions ──
    if t == "volume_above_avg":
        period = int(p.get("period", 20))
        multiplier = float(p.get("multiplier", 1.5))
        if len(snap.volumes) < period:
            return False
        avg = float(np.mean(snap.volumes[-period:]))
        return avg > 0 and snap.volumes[-1] > avg * multiplier
    if t == "volume_below_avg":
        period = int(p.get("period", 20))
        multiplier = float(p.get("multiplier", 0.5))
        if len(snap.volumes) < period:
            return False
        avg = float(np.mean(snap.volumes[-period:]))
        return avg > 0 and snap.volumes[-1] < avg * multiplier
    if t == "volume_spike":
        period = int(p.get("period", 20))
        multiplier = float(p.get("multiplier", 2.0))
        if len(snap.volumes) < period:
            return False
        avg = float(np.mean(snap.volumes[-period:]))
        return avg > 0 and snap.volumes[-1] > avg * multiplier

    # ── Time / frequency ──
    if t == "every_candle":
        return True
    if t == "every_n_candles":
        n = int(p.get("n", 1))
        return snap.tick_count % n == 0

    # ── Meta (uses the multi-snapshot's global state) ──
    if t == "has_no_open_trades":
        return multi.open_trade_count == 0
    if t == "has_open_trades":
        return multi.open_trade_count > 0

    logger.warning("unknown_condition_type", type=t)
    return False


# ──────────────────────────────────────────────────────────
# Rule group evaluator (recursive)
# ──────────────────────────────────────────────────────────

def eval_rule_group(group: RuleGroup | dict, multi: MultiSnapshot) -> bool:
    """Recursively evaluate a rule group."""
    if isinstance(group, dict):
        group = RuleGroup(**group)

    op = group.operator

    if op == "always":
        return True
    if op == "never":
        return False

    results: list[bool] = []
    for item in group.conditions:
        if isinstance(item, dict):
            # Could be Condition or nested RuleGroup
            if "operator" in item:
                results.append(eval_rule_group(RuleGroup(**item), multi))
            else:
                results.append(_eval_condition(Condition(**item), multi))
        elif isinstance(item, RuleGroup):
            results.append(eval_rule_group(item, multi))
        elif isinstance(item, Condition):
            results.append(_eval_condition(item, multi))

    if not results:
        return op == "always"

    if op == "and":
        return all(results)
    elif op == "or":
        return any(results)
    elif op == "not":
        return not results[0] if results else False

    return False


def evaluate_rules(
    rules: TradingRules | dict,
    snap_or_multi: MarketSnapshot | MultiSnapshot,
) -> str:
    """
    Evaluate the full trading rules and return the action.

    Accepts either a single MarketSnapshot (backwards compatible) or
    a MultiSnapshot for multi-timeframe evaluation.

    Returns: "buy", "sell", "close", or "hold"
    """
    if isinstance(rules, dict):
        rules = TradingRules(**rules)

    # Wrap single snapshot for backwards compatibility
    if isinstance(snap_or_multi, MarketSnapshot):
        snap_or_multi = MultiSnapshot({"default": snap_or_multi}, "default")

    multi = snap_or_multi

    # Check close rules first
    if rules.close_rules and eval_rule_group(rules.close_rules, multi):
        return "close"

    # Check buy rules
    if eval_rule_group(rules.buy_rules, multi):
        return "buy"

    # Check sell rules
    if eval_rule_group(rules.sell_rules, multi):
        return "sell"

    return "hold"
