"""Pydantic models for market data."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Candle(BaseModel):
    """OHLCV candle."""

    instrument: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
