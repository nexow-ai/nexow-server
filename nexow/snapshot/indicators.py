"""Technical indicator computation using Polars expressions.

All functions take a Polars DataFrame with columns: ts, open, high, low, close, volume.
"""

from __future__ import annotations

import polars as pl

from nexow.snapshot.models import (
    ATRData,
    BollingerData,
    EMACrossData,
    MACDData,
    RSIData,
    RSIZone,
    VolumeData,
)

# ── Core computation ───────────────────────────────────────────────────


def compute_indicators(df: pl.DataFrame) -> pl.DataFrame:
    """Add all indicator columns to the DataFrame in one pass."""
    if df.height < 2:
        return df

    # Cast to float for calculations
    df = df.with_columns(
        pl.col("open").cast(pl.Float64),
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
        pl.col("volume").cast(pl.Float64),
    )

    # ── Price deltas ──
    df = df.with_columns(
        pl.col("close").diff().alias("_delta"),
        pl.col("close").shift(1).alias("_prev_close"),
    )

    # ── RSI (14) ──
    df = df.with_columns(
        pl.col("_delta").clip(lower_bound=0).alias("_gain"),
        (-pl.col("_delta")).clip(lower_bound=0).alias("_loss"),
    )
    df = df.with_columns(
        pl.col("_gain").ewm_mean(span=14, adjust=False).alias("_avg_gain"),
        pl.col("_loss").ewm_mean(span=14, adjust=False).alias("_avg_loss"),
    )
    df = df.with_columns(
        (100.0 - 100.0 / (1.0 + pl.col("_avg_gain") / pl.col("_avg_loss"))).alias("rsi"),
    )

    # ── MACD (12, 26, 9) ──
    df = df.with_columns(
        pl.col("close").ewm_mean(span=12, adjust=False).alias("_ema12"),
        pl.col("close").ewm_mean(span=26, adjust=False).alias("_ema26"),
    )
    df = df.with_columns(
        (pl.col("_ema12") - pl.col("_ema26")).alias("macd_line"),
    )
    df = df.with_columns(
        pl.col("macd_line").ewm_mean(span=9, adjust=False).alias("macd_signal"),
    )
    df = df.with_columns(
        (pl.col("macd_line") - pl.col("macd_signal")).alias("macd_hist"),
    )

    # ── Bollinger Bands (20, 2) ──
    df = df.with_columns(
        pl.col("close").rolling_mean(window_size=20).alias("bb_sma"),
        pl.col("close").rolling_std(window_size=20).alias("bb_std"),
    )
    df = df.with_columns(
        (pl.col("bb_sma") + 2.0 * pl.col("bb_std")).alias("bb_upper"),
        (pl.col("bb_sma") - 2.0 * pl.col("bb_std")).alias("bb_lower"),
    )

    # ── EMA 20 / 50 / 200 ──
    df = df.with_columns(
        pl.col("close").ewm_mean(span=20, adjust=False).alias("ema_20"),
        pl.col("close").ewm_mean(span=50, adjust=False).alias("ema_50"),
    )
    if df.height >= 200:
        df = df.with_columns(
            pl.col("close").ewm_mean(span=200, adjust=False).alias("ema_200"),
        )
    else:
        df = df.with_columns(
            pl.lit(None).cast(pl.Float64).alias("ema_200"),
        )

    # ── ATR (14) ──
    df = df.with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("_prev_close")).abs(),
            (pl.col("low") - pl.col("_prev_close")).abs(),
        ).alias("_tr"),
    )
    df = df.with_columns(
        pl.col("_tr").ewm_mean(span=14, adjust=False).alias("atr"),
    )

    # ── Volume average (20) ──
    df = df.with_columns(
        pl.col("volume").rolling_mean(window_size=20).alias("vol_avg"),
    )

    return df


# ── Extraction helpers ─────────────────────────────────────────────────


def extract_rsi(df: pl.DataFrame) -> RSIData:
    """Extract latest RSI value and zone."""
    val = df["rsi"][-1]
    if val is None:
        return RSIData(value=50.0, zone=RSIZone.neutral)

    if val >= 70:
        zone = RSIZone.overbought
    elif val <= 30:
        zone = RSIZone.oversold
    else:
        zone = RSIZone.neutral

    return RSIData(value=round(val, 1), zone=zone)


def extract_macd(df: pl.DataFrame) -> MACDData:
    """Extract latest MACD histogram, signal, and cross detection."""
    hist = df["macd_hist"][-1] or 0.0
    signal = df["macd_signal"][-1] or 0.0

    cross = None
    if df.height >= 3:
        prev_hist = df["macd_hist"][-2]
        if prev_hist is not None and hist is not None:
            if prev_hist < 0 and hist >= 0:
                cross = "bullish"
            elif prev_hist > 0 and hist <= 0:
                cross = "bearish"
            elif prev_hist < 0 and hist < 0 and hist > prev_hist:
                cross = "bullish_pending"
            elif prev_hist > 0 and hist > 0 and hist < prev_hist:
                cross = "bearish_pending"

    return MACDData(
        histogram=round(hist, 6),
        signal=round(signal, 6),
        cross=cross,
    )


def extract_bollinger(df: pl.DataFrame) -> BollingerData:
    """Extract Bollinger Band position, bandwidth, and squeeze."""
    close = df["close"][-1]
    upper = df["bb_upper"][-1]
    lower = df["bb_lower"][-1]
    sma = df["bb_sma"][-1]

    if upper is None or lower is None or sma is None or upper == lower:
        return BollingerData(position=0.5, bandwidth=0.0, squeeze=False)

    position = (close - lower) / (upper - lower)
    bandwidth = (upper - lower) / sma
    squeeze = bandwidth < 0.002  # Tight bands threshold

    return BollingerData(
        position=round(max(0.0, min(1.0, position)), 2),
        bandwidth=round(bandwidth, 4),
        squeeze=squeeze,
    )


def extract_ema_cross(df: pl.DataFrame) -> EMACrossData:
    """Extract EMA values and cross direction."""
    ema20 = df["ema_20"][-1] or 0.0
    ema50 = df["ema_50"][-1] or 0.0
    ema200 = df["ema_200"][-1]

    cross = "ema20_above_ema50" if ema20 > ema50 else "ema20_below_ema50"

    return EMACrossData(
        ema_20=round(ema20, 5),
        ema_50=round(ema50, 5),
        ema_200=round(ema200, 5) if ema200 is not None else None,
        cross=cross,
    )


def extract_atr(df: pl.DataFrame, pip_size: float = 0.0001) -> ATRData:
    """Extract ATR value, pips, and percentile vs recent history."""
    val = df["atr"][-1]
    if val is None:
        return ATRData(value=0.0, pips=0.0, percentile=50)

    pips = val / pip_size

    # Percentile vs last 100 ATR values
    atr_series = df["atr"].drop_nulls().tail(100)
    if atr_series.len() > 1:
        below = atr_series.filter(atr_series <= val).len()
        percentile = int(below / atr_series.len() * 100)
    else:
        percentile = 50

    return ATRData(
        value=round(val, 6),
        pips=round(pips, 1),
        percentile=percentile,
    )


def extract_volume(df: pl.DataFrame) -> VolumeData:
    """Extract volume ratio vs average and trend."""
    vol = df["volume"][-1] or 0.0
    avg = df["vol_avg"][-1] or 1.0

    ratio = vol / avg if avg > 0 else 1.0

    # Volume trend: compare last 5 vs previous 5
    if df.height >= 10:
        recent = df["volume"].tail(5).mean()
        prior = df["volume"].slice(df.height - 10, 5).mean()
        if recent is not None and prior is not None and prior > 0:
            if recent / prior > 1.15:
                trend = "increasing"
            elif recent / prior < 0.85:
                trend = "decreasing"
            else:
                trend = "stable"
        else:
            trend = "stable"
    else:
        trend = "stable"

    return VolumeData(
        ratio_vs_avg=round(ratio, 2),
        trend=trend,
    )
