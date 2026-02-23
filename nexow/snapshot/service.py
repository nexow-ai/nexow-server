"""SnapshotService — orchestrates market snapshot computation and caching.

Lifecycle:
1. start() — loads historical M1 data from DB into Polars, connects Redis
2. on_new_bar(instrument, bar) — appends bar, recomputes, caches
3. get_snapshot(instrument) — reads from Redis
4. stop() — cleanup
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl
import structlog

from nexow.db.client import SupabaseClient
from nexow.snapshot.indicators import (
    compute_indicators,
    extract_atr,
    extract_bollinger,
    extract_ema_cross,
    extract_macd,
    extract_rsi,
    extract_volume,
)
from nexow.snapshot.models import (
    MarketSnapshot,
    MomentumVolatility,
    PriceStructure,
    TechnicalIndicators,
    VolatilityState,
)
from nexow.snapshot.news import get_news_context
from nexow.snapshot.redis_store import SnapshotRedisStore
from nexow.snapshot.session import get_session_context
from nexow.snapshot.structure import (
    compute_multi_tf_trends,
    detect_market_phase,
    detect_support_resistance,
)

logger = structlog.get_logger(__name__)

# Pip size per instrument family
PIP_SIZES: dict[str, float] = {
    "JPY": 0.01,  # USD_JPY, EUR_JPY, etc.
    "XAU": 0.01,  # Gold
    "DEFAULT": 0.0001,
}

# How many days of M1 data to load on startup
STARTUP_LOOKBACK_DAYS = 30
MAX_BARS_IN_MEMORY = 50_000  # ~35 days of M1 data


def _pip_size(instrument: str) -> float:
    for key, size in PIP_SIZES.items():
        if key in instrument:
            return size
    return PIP_SIZES["DEFAULT"]


class SnapshotService:
    """Pre-computes and caches market snapshots for all active instruments."""

    def __init__(self, db: SupabaseClient | None = None) -> None:
        self.db = db or SupabaseClient()
        self.store = SnapshotRedisStore()
        self._dataframes: dict[str, pl.DataFrame] = {}  # instrument -> M1 DataFrame
        self._last_ts: dict[str, str] = {}  # instrument -> last processed timestamp

    async def start(self, instruments: list[str] | None = None) -> None:
        """Load historical data and connect Redis."""
        await self.store.connect()

        if instruments is None:
            instruments = ["EUR_USD"]

        for instrument in instruments:
            await self._load_history(instrument)

        logger.info("snapshot_service_started", instruments=instruments)

    async def stop(self) -> None:
        """Cleanup."""
        await self.store.close()
        logger.info("snapshot_service_stopped")

    async def _load_history(self, instrument: str) -> None:
        """Load M1 bars from DB into a Polars DataFrame."""
        from_ts = (datetime.now(timezone.utc) - timedelta(days=STARTUP_LOOKBACK_DAYS)).isoformat()

        logger.info("snapshot_loading_history", instrument=instrument, from_ts=from_ts)

        rows = await asyncio.to_thread(self.db.get_forex_prices_bulk, instrument, from_ts)

        if not rows:
            logger.warning("snapshot_no_history", instrument=instrument)
            self._dataframes[instrument] = pl.DataFrame(
                schema={"ts": pl.Datetime("us", "UTC"), "open": pl.Float64, "high": pl.Float64,
                        "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64}
            )
            return

        df = pl.DataFrame(rows)
        df = df.with_columns(
            pl.col("ts").str.to_datetime(time_zone="UTC").alias("ts"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
        ).sort("ts")

        # Trim to max
        if df.height > MAX_BARS_IN_MEMORY:
            df = df.tail(MAX_BARS_IN_MEMORY)

        self._dataframes[instrument] = df
        self._last_ts[instrument] = str(df["ts"][-1])

        logger.info("snapshot_history_loaded", instrument=instrument, bars=df.height)

    async def on_new_bar(self, instrument: str, bar: dict[str, Any]) -> None:
        """Append a new M1 bar and recompute the snapshot."""
        df = self._dataframes.get(instrument)
        if df is None:
            return

        # Build new row
        new_row = pl.DataFrame({
            "ts": [pl.Series([bar["ts"]]).str.to_datetime(time_zone="UTC")[0]],
            "open": [float(bar["open"])],
            "high": [float(bar["high"])],
            "low": [float(bar["low"])],
            "close": [float(bar["close"])],
            "volume": [float(bar.get("volume", 0))],
        })

        # Check if this bar is newer than last
        bar_ts = str(new_row["ts"][0])
        if self._last_ts.get(instrument) == bar_ts:
            return  # Already processed

        # Append and trim
        df = pl.concat([df, new_row])
        if df.height > MAX_BARS_IN_MEMORY:
            df = df.tail(MAX_BARS_IN_MEMORY)

        self._dataframes[instrument] = df
        self._last_ts[instrument] = bar_ts

        # Recompute
        await self.refresh_snapshot(instrument)

    async def refresh_snapshot(self, instrument: str) -> None:
        """Recompute and cache the snapshot for an instrument."""
        df = self._dataframes.get(instrument)
        if df is None or df.height < 50:
            logger.debug("snapshot_skip_not_enough_data", instrument=instrument, bars=df.height if df is not None else 0)
            return

        try:
            snapshot = await asyncio.to_thread(self._build_snapshot, instrument, df)
            snapshot_json = snapshot.model_dump_json()
            await self.store.set_snapshot(instrument, snapshot_json)
            logger.info("snapshot_refreshed", instrument=instrument)
        except Exception as e:
            logger.error("snapshot_build_failed", instrument=instrument, error=str(e))

    def _build_snapshot(self, instrument: str, df: pl.DataFrame) -> MarketSnapshot:
        """Synchronous snapshot assembly — runs in thread."""
        now = datetime.now(timezone.utc)
        pip_size = _pip_size(instrument)

        # ── Compute indicators on M1 ──
        df_ind = compute_indicators(df)

        # ── Technical Indicators ──
        tech = TechnicalIndicators(
            rsi=extract_rsi(df_ind),
            macd=extract_macd(df_ind),
            bollinger=extract_bollinger(df_ind),
            ema=extract_ema_cross(df_ind),
            atr=extract_atr(df_ind, pip_size),
            volume=extract_volume(df_ind),
        )

        # ── Multi-TF Trends ──
        trends = compute_multi_tf_trends(df)

        # ── Support / Resistance ──
        supports, resistances = detect_support_resistance(df_ind, pip_size=pip_size)

        # ── Market Phase ──
        primary_trend = trends[0].direction if trends else "neutral"
        phase = detect_market_phase(df_ind, primary_trend)

        # ── Price Structure ──
        close = float(df["close"][-1])
        spread = pip_size * 2  # Approximate spread
        bid = close - spread / 2
        ask = close + spread / 2

        # Position in range
        if supports and resistances:
            nearest_s = supports[0]
            nearest_r = resistances[0]
            pos_range = (close - nearest_s) / (nearest_r - nearest_s) if nearest_r != nearest_s else 0.5
        else:
            pos_range = 0.5

        price_struct = PriceStructure(
            bid=round(bid, 5),
            ask=round(ask, 5),
            spread_pips=round(spread / pip_size, 1),
            trends=trends,
            market_phase=phase,
            support_levels=supports,
            resistance_levels=resistances,
            position_in_range=round(max(0.0, min(1.0, pos_range)), 2),
        )

        # ── Momentum / Volatility ──
        momentum = self._compute_momentum(df, df_ind, pip_size)

        # ── Session Context ──
        session = get_session_context(now)
        # Try to set session open price
        session_start_map = {"asian": 0, "london": 7, "new_york": 12, "pacific": 21}
        start_hour = session_start_map.get(session.current_session.value)
        if start_hour is not None:
            session_open_ts = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            if session_open_ts > now:
                session_open_ts -= timedelta(days=1)
            mask = df["ts"] >= session_open_ts
            session_bars = df.filter(mask)
            if session_bars.height > 0:
                session.session_open_price = round(float(session_bars["open"][0]), 5)

        # ── News ──
        news = get_news_context(self.db, instrument)

        # ── Assemble ──
        return MarketSnapshot(
            instrument=instrument,
            timestamp=now.isoformat(),
            price_structure=price_struct,
            technical_indicators=tech,
            momentum_volatility=momentum,
            session_context=session,
            news_fundamental=news,
            correlations=[],  # Needs multi-instrument data
        )

    def _compute_momentum(
        self, df: pl.DataFrame, df_ind: pl.DataFrame, pip_size: float,
    ) -> MomentumVolatility:
        """Compute momentum and volatility metrics."""
        close = float(df["close"][-1])

        def pct_change(n_bars: int) -> float:
            if df.height > n_bars:
                old = float(df["close"][-1 - n_bars])
                return round((close - old) / old * 100, 2) if old else 0.0
            return 0.0

        # Price changes
        change_1h = pct_change(60)
        change_4h = pct_change(240)
        change_1d = pct_change(1440)
        change_1w = pct_change(7200) if df.height > 7200 else None

        # Consecutive candles in same direction
        closes = df["close"].tail(20).to_list()
        count = 0
        if len(closes) >= 2:
            direction = "bullish" if closes[-1] > closes[-2] else "bearish"
            for i in range(len(closes) - 1, 0, -1):
                if (direction == "bullish" and closes[i] > closes[i - 1]) or \
                   (direction == "bearish" and closes[i] < closes[i - 1]):
                    count += 1
                else:
                    break
        else:
            direction = "neutral"

        # Volatility state from ATR percentile
        atr_data = extract_atr(df_ind, pip_size)
        if atr_data.percentile >= 85:
            vol_state = VolatilityState.extreme
        elif atr_data.percentile >= 65:
            vol_state = VolatilityState.high
        elif atr_data.percentile >= 35:
            vol_state = VolatilityState.normal
        else:
            vol_state = VolatilityState.low

        # Session range (today's high - low in pips)
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        today_bars = df.filter(pl.col("ts") >= today_start)
        if today_bars.height > 0:
            session_range = (float(today_bars["high"].max()) - float(today_bars["low"].min())) / pip_size
        else:
            session_range = 0.0

        # Average daily range (last 5 trading days)
        if df.height >= 7200:  # 5 days of M1
            daily_ranges: list[float] = []
            for i in range(5):
                start = df.height - 1440 * (i + 1)
                end = df.height - 1440 * i
                if start >= 0:
                    chunk = df.slice(start, min(1440, end - start))
                    dr = (float(chunk["high"].max()) - float(chunk["low"].min())) / pip_size
                    daily_ranges.append(dr)
            avg_daily = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 0.0
        else:
            avg_daily = session_range  # Fallback

        return MomentumVolatility(
            price_change_1h=change_1h,
            price_change_4h=change_4h,
            price_change_1d=change_1d,
            price_change_1w=change_1w,
            consecutive_candles={"direction": direction, "count": count},
            volatility_state=vol_state,
            session_range_pips=round(session_range, 1),
            avg_daily_range_pips=round(avg_daily, 1),
        )

    async def get_snapshot(self, instrument: str) -> dict | None:
        """Read snapshot from Redis cache."""
        return await self.store.get_snapshot(instrument)
