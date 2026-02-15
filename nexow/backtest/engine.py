"""
Backtest Engine — walks historical candles bar-by-bar and evaluates
the exact same rule interpreter used for live trading.

Produces trades, equity curve, and summary statistics.
Streams partial equity curve data during simulation for real-time charting.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import structlog

from nexow.broker.models import Candle
from nexow.broker.oanda import OandaClient
from nexow.rules.interpreter import MarketSnapshot, MultiSnapshot, evaluate_rules

logger = structlog.get_logger(__name__)

# Minimum candles needed for indicator warmup (longest indicator lookback)
WARMUP_BARS = 200

# How many equity curve points to stream per progress update (~every 1-2%)
EQUITY_SAMPLE_INTERVAL_PCT = 1


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """A single trade produced during backtesting."""

    instrument: str
    direction: str  # "buy" | "sell"
    entry_price: float
    entry_time: str  # ISO timestamp
    exit_price: float | None = None
    exit_time: str | None = None
    return_pct: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    status: str = "open"


@dataclass
class BacktestStats:
    """Summary statistics from a backtest run."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_return_pct: float = 0.0
    avg_return_pct: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    avg_trade_duration_hours: float = 0.0


@dataclass
class EquityPoint:
    """A single point on the equity curve."""

    time: str
    equity: float


@dataclass
class BacktestResult:
    """Full result of a backtest run."""

    stats: BacktestStats
    trades: list[BacktestTrade]
    equity_curve: list[EquityPoint]


@dataclass
class ProgressUpdate:
    """Streamed progress update during backtest execution."""

    phase: str  # "fetching" | "simulating" | "complete" | "error"
    progress_pct: int  # 0-100
    message: str
    result: BacktestResult | None = None
    # Partial equity curve points streamed during simulation
    equity_curve: list[EquityPoint] = field(default_factory=list)


# ------------------------------------------------------------------
# Backtest engine
# ------------------------------------------------------------------

class BacktestEngine:
    """
    Runs a historical simulation of a systematic agent's rules.

    Reuses the exact same `evaluate_rules()` function from the live engine
    to guarantee parity between backtest and live execution.
    """

    def __init__(self, market: OandaClient | None = None) -> None:
        self.market = market or OandaClient()

    async def run(
        self,
        config: dict[str, Any],
        instruments: list[dict[str, str]],
        exit_config: dict[str, Any],
        period_start: datetime,
        period_end: datetime,
    ) -> AsyncIterator[ProgressUpdate]:
        """
        Run the backtest and yield progress updates.

        Uses an event-driven simulation: each (instrument, timeframe) pair
        produces events at its own candle frequency. Events are merged into
        a single chronological queue so that multi-asset / multi-timeframe
        portfolios are handled correctly.

        Args:
            config: Agent config JSON (must contain "rules" key).
            instruments: List of {"instrument": "EUR_USD", "timeframe": "M5"}.
            exit_config: {"stop_loss_pct": N, "take_profit_pct": M}.
            period_start: Start of backtest period.
            period_end: End of backtest period.
        """
        rules = config.get("rules", {})
        if not rules:
            yield ProgressUpdate(
                phase="error",
                progress_pct=0,
                message="No rules found in agent config",
            )
            return

        sl_pct = exit_config.get("stop_loss_pct")
        tp_pct = exit_config.get("take_profit_pct")

        # ----------------------------------------------------------
        # Phase 1: Fetch historical candles per (instrument, timeframe) pair
        # ----------------------------------------------------------
        pair_candles: dict[str, list[Candle]] = {}  # "EUR_USD:M5" -> [...]
        total_pairs = len(instruments)

        for idx, inst_cfg in enumerate(instruments):
            instrument = inst_cfg["instrument"]
            timeframe = inst_cfg.get("timeframe", "M5")
            pair_key = f"{instrument}:{timeframe}"

            pct = int((idx / total_pairs) * 30)  # 0-30% for fetching
            yield ProgressUpdate(
                phase="fetching",
                progress_pct=pct,
                message=f"Fetching {instrument} ({timeframe}) candles...",
            )

            try:
                candles = await self.market.get_candles_range(
                    instrument=instrument,
                    granularity=timeframe,
                    from_time=period_start,
                    to_time=period_end,
                )
                pair_candles[pair_key] = candles
                logger.info(
                    "backtest_candles_fetched",
                    pair=pair_key,
                    count=len(candles),
                )
            except Exception as e:
                yield ProgressUpdate(
                    phase="error",
                    progress_pct=pct,
                    message=f"Failed to fetch candles for {instrument} ({timeframe}): {e}",
                )
                return

        if not pair_candles:
            yield ProgressUpdate(
                phase="error",
                progress_pct=30,
                message="No candle data available for the selected instruments",
            )
            return

        # Validate warmup for each pair
        for pair_key, candles in pair_candles.items():
            if len(candles) <= WARMUP_BARS:
                yield ProgressUpdate(
                    phase="error",
                    progress_pct=30,
                    message=f"Not enough candles for {pair_key} "
                            f"({len(candles)}, need >{WARMUP_BARS})",
                )
                return

        yield ProgressUpdate(
            phase="fetching",
            progress_pct=30,
            message=f"Fetched candles for {total_pairs} pair(s). Building event queue...",
        )

        # ----------------------------------------------------------
        # Phase 2: Group timeframes per instrument & build event queue
        # ----------------------------------------------------------
        # Map each unique instrument to its list of timeframes
        inst_timeframes: dict[str, list[str]] = {}
        for inst_cfg in instruments:
            inst = inst_cfg["instrument"]
            tf = inst_cfg.get("timeframe", "M5")
            inst_timeframes.setdefault(inst, []).append(tf)

        # Identify primary (fastest = most candles) timeframe per instrument
        primary_tf: dict[str, str] = {}
        for inst, tfs in inst_timeframes.items():
            primary_tf[inst] = max(
                tfs, key=lambda tf: len(pair_candles.get(f"{inst}:{tf}", []))
            )

        # Pre-compute sorted timestamp lists for bisect lookups on non-primary TFs
        candle_times: dict[str, list[datetime]] = {}
        for pair_key, candles in pair_candles.items():
            candle_times[pair_key] = [c.time for c in candles]

        # Build events ONLY from the primary (fastest) timeframe per instrument.
        # Slower timeframes provide context data, not evaluation triggers.
        # Event = (candle_time, instrument, primary_pair_key, candle_index)
        events: list[tuple[datetime, str, str, int]] = []
        for inst, tf in primary_tf.items():
            pair_key = f"{inst}:{tf}"
            candles = pair_candles[pair_key]
            for idx in range(WARMUP_BARS, len(candles)):
                events.append((candles[idx].time, inst, pair_key, idx))

        events.sort(key=lambda e: e[0])
        total_events = len(events)

        if total_events == 0:
            yield ProgressUpdate(
                phase="error",
                progress_pct=30,
                message="No simulation events after warmup",
            )
            return

        logger.info(
            "backtest_event_queue_built",
            total_events=total_events,
            instruments=list(inst_timeframes.keys()),
            primary_tfs=primary_tf,
        )

        # ----------------------------------------------------------
        # Phase 2b: Dry-run diagnostic — sample rules on a few bars
        # ----------------------------------------------------------
        unknown_types: set[str] = set()
        self._collect_unknown_conditions(rules, unknown_types)
        if unknown_types:
            logger.warning("backtest_unknown_conditions", types=list(unknown_types))
            yield ProgressUpdate(
                phase="simulating",
                progress_pct=31,
                message=f"Warning: unknown condition types in rules: {', '.join(sorted(unknown_types))}. "
                        f"These always evaluate to false.",
            )

        # Quick sample: evaluate rules on ~50 evenly-spaced events using
        # full MultiSnapshot to test multi-timeframe conditions.
        sample_step = max(1, total_events // 50)
        sample_signals: dict[str, int] = {"buy": 0, "sell": 0, "close": 0, "hold": 0}
        for si in range(0, total_events, sample_step):
            s_time, s_inst, s_pk, s_ci = events[si]
            multi = self._build_multi_snapshot(
                s_inst, s_pk, s_ci, s_time,
                inst_timeframes, pair_candles, candle_times,
                primary_tf, open_trade_count=0,
            )
            if multi is not None:
                sa = evaluate_rules(rules, multi)
                sample_signals[sa] = sample_signals.get(sa, 0) + 1

        logger.info("backtest_rule_sample", signals=sample_signals)
        if sample_signals["buy"] == 0 and sample_signals["sell"] == 0:
            yield ProgressUpdate(
                phase="simulating",
                progress_pct=32,
                message=f"Warning: rules produced 0 buy/sell signals in a "
                        f"{len(range(0, total_events, sample_step))}-bar sample. "
                        f"The strategy may be too restrictive for this data.",
            )

        # ----------------------------------------------------------
        # Phase 3: Event-driven simulation with MultiSnapshot
        # ----------------------------------------------------------
        closed_trades: list[BacktestTrade] = []
        open_trades: dict[str, BacktestTrade] = {}  # keyed by instrument
        equity_curve: list[EquityPoint] = []
        cumulative_return = 0.0  # realized returns only

        # Track latest price per instrument (for unrealized PnL)
        latest_prices: dict[str, float] = {}

        last_yield_pct = 30
        new_equity_points: list[EquityPoint] = []
        equity_sample_every = max(1, total_events // 300)  # ~300 points

        for event_idx, (event_time, instrument, primary_pair, candle_idx) in enumerate(events):
            primary_candles = pair_candles[primary_pair]
            current_bar = primary_candles[candle_idx]
            current_price = current_bar.close
            bar_time = current_bar.time.isoformat()
            latest_prices[instrument] = current_price

            # Check SL/TP on this instrument's open trade (uses primary TF bar)
            if instrument in open_trades:
                trade = open_trades[instrument]
                hit = self._check_sl_tp(trade, current_bar, sl_pct, tp_pct)
                if hit:
                    closed_trades.append(trade)
                    cumulative_return += trade.return_pct or 0.0
                    del open_trades[instrument]

            # Build MultiSnapshot: one snapshot per timeframe for this instrument
            multi = self._build_multi_snapshot(
                instrument, primary_pair, candle_idx, event_time,
                inst_timeframes, pair_candles, candle_times,
                primary_tf, open_trade_count=len(open_trades),
            )
            if multi is None:
                continue

            action = evaluate_rules(rules, multi)

            if action == "hold":
                pass
            elif action == "close" and instrument in open_trades:
                trade = open_trades[instrument]
                self._close_trade(trade, current_price, bar_time)
                closed_trades.append(trade)
                cumulative_return += trade.return_pct or 0.0
                del open_trades[instrument]
            elif action in ("buy", "sell") and instrument not in open_trades:
                open_trades[instrument] = BacktestTrade(
                    instrument=instrument,
                    direction=action,
                    entry_price=current_price,
                    entry_time=bar_time,
                    stop_loss_pct=sl_pct,
                    take_profit_pct=tp_pct,
                )

            # --- Sample equity curve (realized + unrealized) ---
            if event_idx % equity_sample_every == 0 or event_idx == total_events - 1:
                unrealized = 0.0
                for inst, trade in open_trades.items():
                    price_now = latest_prices.get(inst)
                    if price_now is not None:
                        entry = trade.entry_price
                        if trade.direction == "buy":
                            unrealized += ((price_now - entry) / entry) * 100
                        else:
                            unrealized += ((entry - price_now) / entry) * 100

                total_equity = cumulative_return + unrealized
                point = EquityPoint(
                    time=event_time.isoformat(), equity=round(total_equity, 4),
                )
                equity_curve.append(point)
                new_equity_points.append(point)

            # Yield progress every ~2%
            current_pct = 30 + int((event_idx / total_events) * 65)
            if current_pct >= last_yield_pct + 2:
                last_yield_pct = current_pct
                yield ProgressUpdate(
                    phase="simulating",
                    progress_pct=current_pct,
                    message=f"Event {event_idx + 1:,}/{total_events:,} "
                            f"| {len(closed_trades)} trades | {cumulative_return:+.2f}%",
                    equity_curve=new_equity_points,
                )
                new_equity_points = []

        # ----------------------------------------------------------
        # Phase 4: Close remaining open trades at each pair's last bar
        # ----------------------------------------------------------
        for instrument, trade in list(open_trades.items()):
            # Find the last candle for this instrument across all its pairs
            best_time: datetime | None = None
            best_price = 0.0
            for pair_key, candles in pair_candles.items():
                if pair_key.startswith(f"{instrument}:") and candles:
                    last_bar = candles[-1]
                    if best_time is None or last_bar.time > best_time:
                        best_time = last_bar.time
                        best_price = last_bar.close
            if best_time is not None:
                self._close_trade(trade, best_price, best_time.isoformat())
                closed_trades.append(trade)
                cumulative_return += trade.return_pct or 0.0

        # Final equity point
        if events:
            equity_curve.append(
                EquityPoint(
                    time=events[-1][0].isoformat(),
                    equity=round(cumulative_return, 4),
                )
            )

        # ----------------------------------------------------------
        # Phase 5: Compute statistics
        # ----------------------------------------------------------
        stats = self._compute_stats(closed_trades)

        result = BacktestResult(
            stats=stats,
            trades=closed_trades,
            equity_curve=equity_curve,
        )

        yield ProgressUpdate(
            phase="complete",
            progress_pct=100,
            message=f"Backtest complete: {stats.total_trades} trades, "
                    f"{stats.total_return_pct:+.2f}% return",
            result=result,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_multi_snapshot(
        instrument: str,
        primary_pair: str,
        primary_candle_idx: int,
        event_time: datetime,
        inst_timeframes: dict[str, list[str]],
        pair_candles: dict[str, list[Candle]],
        candle_times: dict[str, list[datetime]],
        primary_tf: dict[str, str],
        open_trade_count: int = 0,
    ) -> MultiSnapshot | None:
        """
        Build a MultiSnapshot containing one MarketSnapshot per timeframe
        for the given instrument at the given event time.

        The primary timeframe uses the exact candle_idx. Non-primary
        timeframes use bisect to find the latest candle at or before
        the event time.
        """
        snapshots: dict[str, MarketSnapshot] = {}
        default_tf = primary_tf[instrument]

        for tf in inst_timeframes[instrument]:
            pair_key = f"{instrument}:{tf}"
            candles = pair_candles.get(pair_key, [])
            if not candles:
                continue

            if pair_key == primary_pair:
                latest_idx = primary_candle_idx
            else:
                # Binary search: latest candle at or before event_time
                times = candle_times.get(pair_key, [])
                latest_idx = bisect_right(times, event_time) - 1

            if latest_idx < 2:
                continue

            start = max(0, latest_idx - WARMUP_BARS + 1)
            window = candles[start : latest_idx + 1]

            if len(window) < 2:
                continue

            snapshots[tf] = MarketSnapshot(
                window, window[-1].close, open_trade_count=open_trade_count,
            )

        if not snapshots or default_tf not in snapshots:
            return None

        return MultiSnapshot(snapshots, default_tf)

    # Known condition types supported by the rule interpreter
    _KNOWN_CONDITIONS = {
        "price_above", "price_below", "price_change_pct_up", "price_change_pct_down",
        "price_dropped_pct", "price_near_high", "price_near_low",
        "candle_is_green", "candle_is_red", "candle_body_gt",
        "consecutive_green", "consecutive_red", "doji",
        "engulfing_bullish", "engulfing_bearish",
        "rsi_above", "rsi_below",
        "macd_cross_up", "macd_cross_down", "macd_positive", "macd_negative",
        "ema_cross_up", "ema_cross_down", "price_above_ema", "price_below_ema",
        "price_above_bb_upper", "price_below_bb_lower", "bb_squeeze",
        "volume_above_avg", "volume_below_avg", "volume_spike",
        "every_candle", "every_n_candles",
        "has_no_open_trades", "has_open_trades",
    }

    @classmethod
    def _collect_unknown_conditions(cls, rules: dict, out: set[str]) -> None:
        """Walk the rule tree and collect any condition types not in the known set."""
        if not isinstance(rules, dict):
            return
        # If this dict looks like a condition (has "type" but no "operator")
        if "type" in rules and "operator" not in rules:
            ctype = rules["type"]
            if ctype not in cls._KNOWN_CONDITIONS:
                out.add(ctype)
            return
        # If it's a rule group, recurse into conditions
        for item in rules.get("conditions", []):
            cls._collect_unknown_conditions(item, out)
        # Top-level rule set: check buy_rules, sell_rules, close_rules
        for key in ("buy_rules", "sell_rules", "close_rules"):
            if key in rules:
                cls._collect_unknown_conditions(rules[key], out)

    @staticmethod
    def _check_sl_tp(
        trade: BacktestTrade,
        bar: Candle,
        sl_pct: float | None,
        tp_pct: float | None,
    ) -> bool:
        """
        Check if SL or TP was hit during this bar using high/low.

        Returns True if the trade was closed (mutates trade in-place).
        """
        if sl_pct is None and tp_pct is None:
            return False

        entry = trade.entry_price
        direction = trade.direction
        bar_time = bar.time.isoformat()

        # For buy trades: adverse = low, favorable = high
        # For sell trades: adverse = high, favorable = low
        if direction == "buy":
            worst_return = ((bar.low - entry) / entry) * 100
            best_return = ((bar.high - entry) / entry) * 100
        else:
            worst_return = ((entry - bar.high) / entry) * 100
            best_return = ((entry - bar.low) / entry) * 100

        # Check SL first (assume SL hit before TP on the same bar)
        if sl_pct is not None and worst_return <= -sl_pct:
            # SL hit — exit at SL level
            if direction == "buy":
                exit_price = entry * (1 - sl_pct / 100)
            else:
                exit_price = entry * (1 + sl_pct / 100)
            trade.exit_price = exit_price
            trade.exit_time = bar_time
            trade.return_pct = -sl_pct
            trade.status = "closed"
            return True

        # Check TP
        if tp_pct is not None and best_return >= tp_pct:
            if direction == "buy":
                exit_price = entry * (1 + tp_pct / 100)
            else:
                exit_price = entry * (1 - tp_pct / 100)
            trade.exit_price = exit_price
            trade.exit_time = bar_time
            trade.return_pct = tp_pct
            trade.status = "closed"
            return True

        return False

    @staticmethod
    def _close_trade(trade: BacktestTrade, exit_price: float, exit_time: str) -> None:
        """Close a trade at a given price and time."""
        entry = trade.entry_price
        if trade.direction == "buy":
            trade.return_pct = ((exit_price - entry) / entry) * 100
        else:
            trade.return_pct = ((entry - exit_price) / entry) * 100
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.status = "closed"

    @staticmethod
    def _compute_stats(trades: list[BacktestTrade]) -> BacktestStats:
        """Compute summary statistics from closed trades."""
        if not trades:
            return BacktestStats()

        returns = [t.return_pct or 0.0 for t in trades]
        winners = [r for r in returns if r > 0]
        losers = [r for r in returns if r < 0]

        total_return = sum(returns)
        avg_return = total_return / len(returns) if returns else 0.0
        win_rate = (len(winners) / len(returns) * 100) if returns else 0.0

        # Max drawdown (from peak equity)
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in returns:
            cumulative += r
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Sharpe ratio (annualized, assuming ~252 trading days)
        if len(returns) > 1:
            std = float(np.std(returns, ddof=1))
            if std > 0:
                sharpe = (avg_return / std) * math.sqrt(252)
            else:
                sharpe = 0.0
        else:
            sharpe = 0.0

        # Profit factor
        gross_profit = sum(winners) if winners else 0.0
        gross_loss = abs(sum(losers)) if losers else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
            float("inf") if gross_profit > 0 else 0.0
        )
        # Cap for JSON serialization
        if profit_factor == float("inf"):
            profit_factor = 999.99

        # Average trade duration
        durations_hours: list[float] = []
        for t in trades:
            if t.entry_time and t.exit_time:
                try:
                    entry_dt = datetime.fromisoformat(t.entry_time)
                    exit_dt = datetime.fromisoformat(t.exit_time)
                    durations_hours.append((exit_dt - entry_dt).total_seconds() / 3600)
                except (ValueError, TypeError):
                    pass
        avg_duration = sum(durations_hours) / len(durations_hours) if durations_hours else 0.0

        return BacktestStats(
            total_trades=len(trades),
            winning_trades=len(winners),
            losing_trades=len(losers),
            win_rate=round(win_rate, 2),
            total_return_pct=round(total_return, 4),
            avg_return_pct=round(avg_return, 4),
            max_drawdown=round(max_dd, 2),
            sharpe_ratio=round(sharpe, 4),
            profit_factor=round(profit_factor, 4),
            best_trade_pct=round(max(returns), 4) if returns else 0.0,
            worst_trade_pct=round(min(returns), 4) if returns else 0.0,
            avg_trade_duration_hours=round(avg_duration, 2),
        )
