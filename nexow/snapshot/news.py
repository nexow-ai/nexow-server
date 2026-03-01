"""News & fundamental context from economic events in DB.

Sentiment is keyword-based (no LLM call) — fast and cheap.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import structlog

from nexow.db.client import SupabaseClient
from nexow.snapshot.models import EconomicEvent, NewsFundamental

logger = structlog.get_logger(__name__)

# Keyword sentiment scoring for economic events
BULLISH_KEYWORDS = {
    "beat", "beats", "surpass", "above", "rise", "rising", "growth",
    "expansion", "surplus", "improve", "strong", "higher", "increase",
    "positive", "upbeat", "hawkish",
}
BEARISH_KEYWORDS = {
    "miss", "misses", "below", "fall", "falling", "decline", "contraction",
    "deficit", "weak", "lower", "decrease", "negative", "downbeat",
    "dovish", "recession", "slowdown",
}

# Impact weight for sentiment
IMPACT_WEIGHT = {"high": 1.0, "medium": 0.5, "low": 0.2, "holiday": 0.0, "none": 0.1}


def _score_event(event: dict) -> float:
    """Score a single event's sentiment from -1 to +1 based on actual vs forecast."""
    actual = (event.get("actual") or "").strip()
    forecast = (event.get("forecast") or "").strip()

    if not actual or not forecast:
        return 0.0

    # Try numeric comparison
    try:
        actual_val = float(actual.replace("%", "").replace("K", "").replace("M", "").replace("B", ""))
        forecast_val = float(forecast.replace("%", "").replace("K", "").replace("M", "").replace("B", ""))
        if forecast_val == 0:
            return 0.0
        diff_pct = (actual_val - forecast_val) / abs(forecast_val)
        return max(-1.0, min(1.0, diff_pct * 2))  # Scale: 50% beat = +1.0
    except (ValueError, TypeError):
        pass

    # Keyword fallback
    text = f"{event.get('event', '')} {actual}".lower()
    bull = sum(1 for w in BULLISH_KEYWORDS if w in text)
    bear = sum(1 for w in BEARISH_KEYWORDS if w in text)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


def get_news_context(
    db: SupabaseClient,
    instrument: str,
    target_date: date | None = None,
) -> NewsFundamental:
    """Build news/fundamental context from economic events in DB."""
    if target_date is None:
        target_date = datetime.now(timezone.utc).date()

    # Get currencies for this instrument (e.g., EUR_USD → EUR, USD)
    currencies = instrument.split("_")

    events_data: list[dict] = []
    for currency in currencies:
        rows = db.get_economic_events(
            target_date=target_date.isoformat(),
            currency=currency,
        )
        events_data.extend(rows)

    # Convert to model and compute sentiment
    events: list[EconomicEvent] = []
    weighted_sentiment = 0.0
    total_weight = 0.0

    for row in events_data:
        impact = row.get("impact", "none")
        ev = EconomicEvent(
            time=row.get("time"),
            currency=row.get("currency", ""),
            event=row.get("event", ""),
            impact=impact,
            forecast=row.get("forecast"),
            previous=row.get("previous"),
            actual=row.get("actual"),
        )
        events.append(ev)

        # Weighted sentiment
        score = _score_event(row)
        weight = IMPACT_WEIGHT.get(impact, 0.1)
        weighted_sentiment += score * weight
        total_weight += weight

    overall = weighted_sentiment / total_weight if total_weight > 0 else 0.0

    return NewsFundamental(
        economic_events=events,
        overall_sentiment=round(max(-1.0, min(1.0, overall)), 2),
    )
