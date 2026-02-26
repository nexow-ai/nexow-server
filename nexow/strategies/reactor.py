"""Reactor strategy — score-based trade signals from snapshot analyses.

Aggregation is aligned to candle boundaries: for an H1 reactor, the
"current candle" score is the average of M1 analyses whose timestamps
fall within [candle_start, candle_start + candle_duration).  This
matches the Domain Breakdown chart on the frontend.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from nexow.broker.models import Candle
from nexow.db.client import SupabaseClient
from nexow.strategies.base import Signal, SignalType
from nexow.worker.scheduling import granularity_seconds

logger = structlog.get_logger(__name__)

WEIGHT_KEYS = [
    ("weight_technical", "ai_technical"),
    ("weight_momentum", "ai_momentum"),
    ("weight_fundamental", "ai_fundamental"),
    ("weight_structure", "ai_structure"),
    ("weight_session", "ai_session"),
]

# ATR multiplier for stop loss distance
ATR_MULTIPLIER = 1.5
ATR_PERIOD = 14


def _compute_atr(candles: list[Candle], period: int = ATR_PERIOD) -> float:
    """Compute Average True Range from candles."""
    if len(candles) < 2:
        return 0.0

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    if not true_ranges:
        return 0.0

    # Use the last `period` values
    recent = true_ranges[-period:]
    return sum(recent) / len(recent)


def _weighted_score(analyses: list[dict[str, Any]], config: dict[str, Any]) -> float:
    """Compute the weighted average of domain scores using user weights.

    Scores in forex_prices_1m ai_* columns are -1..+1. We normalize to 0..1 for
    comparison with the confidence threshold (which lives in 0..1 space).
    """
    if not analyses:
        return 0.0

    total = 0.0
    for weight_key, score_key in WEIGHT_KEYS:
        weight = float(config.get(weight_key, 0))
        # Average this domain's score across all M1 analyses in the candle
        scores = [float(a.get(score_key, 0)) for a in analyses]
        avg_raw = sum(scores) / len(scores)  # -1..+1
        avg_normalized = (avg_raw + 1) / 2  # 0..1
        total += avg_normalized * weight

    return total


class ReactorStrategy:
    """Evaluate snapshot analyses against user reactor config.

    Rising-edge detection: triggers a signal only when the weighted
    score crosses above the confidence threshold (previous candle was
    below, current candle is at or above).

    Analyses are grouped by candle boundaries (aligned to the config
    timeframe), not by "last N rows".
    """

    def __init__(self, config: dict[str, Any], db: SupabaseClient) -> None:
        self.config = config
        self.db = db

    def evaluate(
        self,
        candles: list[Candle],
        current_price: float,
    ) -> Signal:
        """Evaluate and return a trading signal."""
        instrument = self.config["instrument"]
        timeframe = self.config.get("timeframe", "H1")
        threshold = float(self.config.get("confidence_threshold", 0.6))
        candle_secs = granularity_seconds(timeframe)

        if len(candles) < 2:
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                reason="Need at least 2 candles",
            )

        # Candle boundaries from the actual cached candles
        #   candles[-1] = last closed candle  (current)
        #   candles[-2] = the one before      (previous)
        current_start = candles[-1].time
        previous_start = candles[-2].time
        delta = timedelta(seconds=candle_secs)

        # Fetch M1 analyses covering both candle windows
        from_ts = previous_start.isoformat()
        to_ts = (current_start + delta).isoformat()
        analyses = self.db.get_analyses_in_range(instrument, from_ts, to_ts)

        # Split analyses into the two candle buckets
        current_end = current_start + delta
        current_analyses = [
            a for a in analyses
            if current_start <= _parse_ts(a["ts"]) < current_end
        ]
        previous_analyses = [
            a for a in analyses
            if previous_start <= _parse_ts(a["ts"]) < current_start
        ]

        if not current_analyses:
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                reason=f"No analyses for current candle ({current_start.isoformat()})",
            )

        current_score = _weighted_score(current_analyses, self.config)
        previous_score = _weighted_score(previous_analyses, self.config) if previous_analyses else 0.0

        # Rising edge: prev < threshold AND current >= threshold
        if not (previous_score < threshold <= current_score):
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                confidence=current_score,
                reason=f"No edge: prev={previous_score:.3f} curr={current_score:.3f} thr={threshold:.2f}",
            )

        # Direction from the majority of current-candle analyses
        direction = self._resolve_direction(current_analyses)
        if direction == "hold":
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                confidence=current_score,
                reason="LLM direction is hold despite confluence",
            )

        # trades_per_day check
        max_trades = int(self.config.get("trades_per_day", 3))
        today_count = self.db.count_reactor_trades_today(self.config["id"])
        if today_count >= max_trades:
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                confidence=current_score,
                reason=f"Daily limit reached ({today_count}/{max_trades})",
            )

        # Already open check
        open_trades = self.db.get_reactor_open_trades(self.config["id"])
        if any(t["instrument"] == instrument for t in open_trades):
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                confidence=current_score,
                reason="Already have an open trade on this instrument",
            )

        # SL from ATR, capped by user risk
        atr = _compute_atr(candles)
        if atr <= 0:
            return Signal(
                type=SignalType.HOLD,
                instrument=instrument,
                confidence=current_score,
                reason="Cannot compute ATR (insufficient candle data)",
            )

        sl_distance = atr * ATR_MULTIPLIER
        sl_pct = (sl_distance / current_price) * 100

        # Cap SL by user risk config (in percentage mode)
        if self.config.get("risk_mode") == "percentage":
            risk_cap = float(self.config.get("risk_value", 1.0))
            sl_pct = min(sl_pct, risk_cap)

        # TP = SL × reward_ratio
        reward_ratio = float(self.config.get("reward_ratio", 2.0))
        tp_pct = sl_pct * reward_ratio

        signal_type = SignalType.BUY if direction == "buy" else SignalType.SELL

        return Signal(
            type=signal_type,
            instrument=instrument,
            confidence=current_score,
            stop_loss_pct=round(sl_pct, 4),
            take_profit_pct=round(tp_pct, 4),
            reason=(
                f"Confluence edge: {previous_score:.3f} → {current_score:.3f} "
                f"(threshold {threshold:.2f}), ATR SL={sl_pct:.4f}% TP={tp_pct:.4f}%"
            ),
            metadata={
                "previous_score": round(previous_score, 4),
                "current_score": round(current_score, 4),
                "candle_start": current_start.isoformat(),
                "m1_count_current": len(current_analyses),
                "m1_count_previous": len(previous_analyses),
                "atr": round(atr, 8),
                "sl_distance": round(sl_distance, 8),
            },
        )

    def _resolve_direction(self, analyses: list[dict[str, Any]]) -> str:
        """Pick direction from the majority vote of recent analyses."""
        buy_count = sum(1 for a in analyses if a.get("ai_direction") == "buy")
        sell_count = sum(1 for a in analyses if a.get("ai_direction") == "sell")

        if buy_count > sell_count:
            return "buy"
        if sell_count > buy_count:
            return "sell"
        return "hold"


def _parse_ts(value: str | datetime) -> datetime:
    """Parse a timestamp string (or pass through a datetime) to a tz-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    # ISO format from Supabase (e.g. "2025-01-15T14:00:00+00:00")
    text = str(value)
    # Handle "+00:00" and "Z" suffixes
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)
