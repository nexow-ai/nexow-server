"""Market structure analysis: trends, support/resistance, market phase.

Works on Polars DataFrames with indicator columns already computed.
"""

from __future__ import annotations

import polars as pl

from nexow.snapshot.models import MarketPhase, TimeframeTrend, TrendDirection


# ── Timeframe aggregation ──────────────────────────────────────────────

TIMEFRAME_MINUTES: dict[str, str] = {
    "M15": "15m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}


def aggregate_to_timeframe(df_m1: pl.DataFrame, tf: str) -> pl.DataFrame:
    """Aggregate M1 bars to a higher timeframe using group_by_dynamic."""
    every = TIMEFRAME_MINUTES.get(tf)
    if every is None:
        raise ValueError(f"Unknown timeframe: {tf}")

    if df_m1.height == 0:
        return df_m1

    df = df_m1.sort("ts")

    return df.group_by_dynamic("ts", every=every).agg(
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    )


# ── Trend detection ───────────────────────────────────────────────────


def detect_trend(df: pl.DataFrame) -> TimeframeTrend:
    """Detect trend from EMA 20/50 crossover and price position.

    Requires columns: close, ema_20, ema_50.
    """
    if df.height < 2 or "ema_20" not in df.columns:
        return TimeframeTrend(timeframe="?", direction=TrendDirection.neutral, strength=0.5)

    ema20 = df["ema_20"][-1]
    ema50 = df["ema_50"][-1]
    close = df["close"][-1]

    if ema20 is None or ema50 is None or close is None:
        return TimeframeTrend(timeframe="?", direction=TrendDirection.neutral, strength=0.5)

    # Direction from EMA alignment
    if ema20 > ema50:
        direction = TrendDirection.bullish
    elif ema20 < ema50:
        direction = TrendDirection.bearish
    else:
        direction = TrendDirection.neutral

    # Strength: how far apart EMAs are relative to price
    spread = abs(ema20 - ema50) / close if close > 0 else 0
    # Normalize: 0.001 spread ≈ 0.5 strength, 0.005+ ≈ 1.0
    strength = min(1.0, spread / 0.005)

    # Boost if price confirms (above both EMAs for bullish, below for bearish)
    if direction == TrendDirection.bullish and close > ema20:
        strength = min(1.0, strength + 0.15)
    elif direction == TrendDirection.bearish and close < ema20:
        strength = min(1.0, strength + 0.15)

    return TimeframeTrend(
        timeframe="?",  # Caller sets this
        direction=direction,
        strength=round(strength, 2),
    )


def compute_multi_tf_trends(
    df_m1: pl.DataFrame,
    timeframes: list[str] | None = None,
) -> list[TimeframeTrend]:
    """Compute trend for each timeframe by aggregating M1 data."""
    from nexow.snapshot.indicators import compute_indicators

    if timeframes is None:
        timeframes = ["M15", "H1", "H4", "D1"]

    trends: list[TimeframeTrend] = []
    for tf in timeframes:
        df_tf = aggregate_to_timeframe(df_m1, tf)
        if df_tf.height < 50:
            trends.append(TimeframeTrend(
                timeframe=tf,
                direction=TrendDirection.neutral,
                strength=0.5,
            ))
            continue

        df_tf = compute_indicators(df_tf)
        trend = detect_trend(df_tf)
        trend.timeframe = tf
        trends.append(trend)

    return trends


# ── Support / Resistance ──────────────────────────────────────────────


def detect_support_resistance(
    df: pl.DataFrame,
    lookback: int = 200,
    window: int = 5,
    cluster_pips: float = 10.0,
    pip_size: float = 0.0001,
    max_levels: int = 3,
) -> tuple[list[float], list[float]]:
    """Detect S/R levels from swing highs/lows with clustering.

    Returns (support_levels, resistance_levels) sorted by proximity to current price.
    """
    df_tail = df.tail(lookback)
    if df_tail.height < window * 2 + 1:
        return [], []

    highs = df_tail["high"].to_list()
    lows = df_tail["low"].to_list()
    close = df_tail["close"][-1]

    swing_highs: list[float] = []
    swing_lows: list[float] = []

    for i in range(window, len(highs) - window):
        # Swing high: highest in window
        if highs[i] == max(highs[i - window : i + window + 1]):
            swing_highs.append(highs[i])
        # Swing low: lowest in window
        if lows[i] == min(lows[i - window : i + window + 1]):
            swing_lows.append(lows[i])

    # Cluster nearby levels
    threshold = cluster_pips * pip_size
    resistances = _cluster_levels(swing_highs, threshold)
    supports = _cluster_levels(swing_lows, threshold)

    # Filter: supports below price, resistances above price
    supports = sorted([s for s in supports if s < close], key=lambda x: close - x)
    resistances = sorted([r for r in resistances if r > close], key=lambda x: x - close)

    return (
        [round(s, 5) for s in supports[:max_levels]],
        [round(r, 5) for r in resistances[:max_levels]],
    )


def _cluster_levels(levels: list[float], threshold: float) -> list[float]:
    """Cluster nearby price levels into averages."""
    if not levels:
        return []

    sorted_levels = sorted(levels)
    clusters: list[list[float]] = [[sorted_levels[0]]]

    for level in sorted_levels[1:]:
        if level - clusters[-1][-1] <= threshold:
            clusters[-1].append(level)
        else:
            clusters.append([level])

    # Return average of each cluster, weighted by count (more touches = stronger)
    return [sum(c) / len(c) for c in clusters if len(c) >= 2]


# ── Market phase ──────────────────────────────────────────────────────


def detect_market_phase(
    df: pl.DataFrame,
    trend: TrendDirection,
) -> MarketPhase:
    """Classify market phase from indicators + trend.

    Requires columns: atr, bb_upper, bb_lower, bb_sma, close.
    """
    if df.height < 20:
        return MarketPhase.consolidation

    # BB bandwidth
    upper = df["bb_upper"][-1]
    lower = df["bb_lower"][-1]
    sma = df["bb_sma"][-1]
    close = df["close"][-1]

    if upper is None or lower is None or sma is None or sma == 0:
        return MarketPhase.consolidation

    bandwidth = (upper - lower) / sma

    # ATR percentile
    atr_series = df["atr"].drop_nulls().tail(100)
    atr_current = df["atr"][-1]
    if atr_current is not None and atr_series.len() > 1:
        atr_pct = atr_series.filter(atr_series <= atr_current).len() / atr_series.len()
    else:
        atr_pct = 0.5

    # BB position
    bb_pos = (close - lower) / (upper - lower) if upper != lower else 0.5

    # ── Classification ──
    # Breakout: price outside bands with high volatility
    if (bb_pos > 0.95 or bb_pos < 0.05) and atr_pct > 0.7:
        return MarketPhase.breakout

    # Trending: clear direction with above-average volatility
    if trend != TrendDirection.neutral and atr_pct > 0.4 and bandwidth > 0.003:
        return MarketPhase.trending

    # Consolidation: low volatility, tight bands
    if bandwidth < 0.002 or atr_pct < 0.3:
        return MarketPhase.consolidation

    # Reversal: price at extreme with weakening momentum
    if df.height >= 5:
        recent_atr = df["atr"].tail(5).mean()
        prior_atr = df["atr"].slice(max(0, df.height - 10), 5).mean()
        if recent_atr is not None and prior_atr is not None and prior_atr > 0:
            if recent_atr / prior_atr < 0.8 and (bb_pos > 0.85 or bb_pos < 0.15):
                return MarketPhase.reversal

    return MarketPhase.consolidation
