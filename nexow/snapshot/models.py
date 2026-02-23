"""Pydantic models for the Market Snapshot sent to LLM agents."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────

class TrendDirection(str, Enum):
    bullish = "bullish"
    bearish = "bearish"
    neutral = "neutral"


class MarketPhase(str, Enum):
    trending = "trending"
    consolidation = "consolidation"
    breakout = "breakout"
    reversal = "reversal"


class VolatilityState(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    extreme = "extreme"


class ForexSession(str, Enum):
    asian = "asian"
    london = "london"
    new_york = "new_york"
    pacific = "pacific"
    closed = "closed"


class RSIZone(str, Enum):
    overbought = "overbought"
    neutral = "neutral"
    oversold = "oversold"


# ── Sub-models ─────────────────────────────────────────────────────────

class TimeframeTrend(BaseModel):
    timeframe: str
    direction: TrendDirection
    strength: float = Field(ge=0, le=1)


class RSIData(BaseModel):
    value: float
    zone: RSIZone
    divergence: str | None = None


class MACDData(BaseModel):
    histogram: float
    signal: float
    cross: str | None = None  # "bullish", "bearish", "bullish_pending", "bearish_pending"


class BollingerData(BaseModel):
    position: float  # 0=lower band, 1=upper band
    bandwidth: float
    squeeze: bool


class EMACrossData(BaseModel):
    ema_20: float
    ema_50: float
    ema_200: float | None = None
    cross: str | None = None  # "ema20_above_ema50", "ema20_below_ema50"


class ATRData(BaseModel):
    value: float
    pips: float
    percentile: int = Field(ge=0, le=100)


class VolumeData(BaseModel):
    ratio_vs_avg: float
    trend: str  # "increasing", "decreasing", "stable"


class EconomicEvent(BaseModel):
    time: str | None = None
    currency: str
    event: str
    impact: str
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None


class CorrelationPair(BaseModel):
    pair: str
    correlation_30d: float
    divergence_alert: bool = False


# ── Section models ─────────────────────────────────────────────────────

class PriceStructure(BaseModel):
    bid: float
    ask: float
    spread_pips: float
    trends: list[TimeframeTrend]
    market_phase: MarketPhase
    support_levels: list[float]
    resistance_levels: list[float]
    position_in_range: float = Field(ge=0, le=1)


class TechnicalIndicators(BaseModel):
    rsi: RSIData
    macd: MACDData
    bollinger: BollingerData
    ema: EMACrossData
    atr: ATRData
    volume: VolumeData


class MomentumVolatility(BaseModel):
    price_change_1h: float
    price_change_4h: float
    price_change_1d: float
    price_change_1w: float | None = None
    consecutive_candles: dict[str, Any]  # {"direction": "bullish", "count": 3}
    volatility_state: VolatilityState
    session_range_pips: float
    avg_daily_range_pips: float


class SessionContext(BaseModel):
    current_session: ForexSession
    time_in_session: str
    session_open_price: float | None = None
    day_of_week: str
    hour_utc: int
    minutes_to_next_h1_close: int


class NewsFundamental(BaseModel):
    economic_events: list[EconomicEvent]
    overall_sentiment: float = Field(ge=-1, le=1)


# ── Top-level snapshot ─────────────────────────────────────────────────

class MarketSnapshot(BaseModel):
    instrument: str
    timestamp: str

    price_structure: PriceStructure
    technical_indicators: TechnicalIndicators
    momentum_volatility: MomentumVolatility
    session_context: SessionContext
    news_fundamental: NewsFundamental
    correlations: list[CorrelationPair]
