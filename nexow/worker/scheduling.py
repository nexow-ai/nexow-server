"""Scheduling helpers for worker execution."""

from __future__ import annotations


_SCHEDULE_TO_GRANULARITY: dict[str, str] = {
    # Oanda granularity strings
    "m1": "M1",
    "m5": "M5",
    "m15": "M15",
    "m30": "M30",
    "h1": "H1",
    "h4": "H4",
    "d": "D",
    # Friendly aliases
    "1m": "M1",
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "hourly": "H1",
    "1h": "H1",
    "4h": "H4",
    "daily": "D",
    "1d": "D",
}


def normalize_granularity(value: str | None) -> str | None:
    """Convert schedule/timeframe strings into Oanda granularity values.

    Returns None when the value is empty or unrecognized.
    """
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    key = v.lower()
    if key in _SCHEDULE_TO_GRANULARITY:
        return _SCHEDULE_TO_GRANULARITY[key]
    # If user already provided an Oanda granularity, accept it.
    if v in {"M1", "M5", "M15", "M30", "H1", "H4", "D"}:
        return v
    return None


def granularity_seconds(granularity: str) -> int:
    """Rough seconds per candle for a given granularity."""
    return {
        "M1": 60,
        "M5": 5 * 60,
        "M15": 15 * 60,
        "M30": 30 * 60,
        "H1": 60 * 60,
        "H4": 4 * 60 * 60,
        "D": 24 * 60 * 60,
    }.get(granularity, 5 * 60)

