"""Forex session detection — pure datetime calculation, no external data."""

from __future__ import annotations

from datetime import datetime, timezone

from nexow.snapshot.models import ForexSession, SessionContext

# Session hours (UTC)
# Sydney/Pacific : 21:00 – 06:00 UTC
# Tokyo/Asian    : 00:00 – 09:00 UTC
# London         : 07:00 – 16:00 UTC
# New York       : 12:00 – 21:00 UTC

SESSIONS = [
    (ForexSession.london,   7, 16),
    (ForexSession.new_york, 12, 21),
    (ForexSession.asian,    0, 9),
    (ForexSession.pacific,  21, 24),  # wraps around midnight
]

DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _get_active_session(hour: int, weekday: int) -> ForexSession:
    """Determine the dominant forex session for a given UTC hour."""
    # Weekend
    if weekday >= 5:  # Saturday/Sunday
        return ForexSession.closed

    # Friday after NY close
    if weekday == 4 and hour >= 21:
        return ForexSession.closed

    # Overlap London+NY is categorized as NY (most volume)
    if 12 <= hour < 16:
        return ForexSession.new_york
    if 7 <= hour < 12:
        return ForexSession.london
    if 16 <= hour < 21:
        return ForexSession.new_york
    if 0 <= hour < 7:
        return ForexSession.asian
    if 21 <= hour < 24:
        return ForexSession.pacific

    return ForexSession.closed


def _session_start_hour(session: ForexSession) -> int:
    """Get the start hour (UTC) of a session."""
    mapping = {
        ForexSession.asian: 0,
        ForexSession.london: 7,
        ForexSession.new_york: 12,
        ForexSession.pacific: 21,
    }
    return mapping.get(session, 0)


def get_session_context(now: datetime | None = None) -> SessionContext:
    """Build session context from current time."""
    if now is None:
        now = datetime.now(timezone.utc)

    hour = now.hour
    minute = now.minute
    weekday = now.weekday()

    session = _get_active_session(hour, weekday)

    # Time in session
    start_h = _session_start_hour(session)
    if session == ForexSession.closed:
        time_in = "0h 0m"
    else:
        elapsed_h = hour - start_h
        if elapsed_h < 0:
            elapsed_h += 24
        time_in = f"{elapsed_h}h {minute}m"

    # Minutes to next H1 candle close
    mins_to_h1 = 60 - minute if minute > 0 else 0

    return SessionContext(
        current_session=session,
        time_in_session=time_in,
        session_open_price=None,  # Set by service from price data
        day_of_week=DAY_NAMES[weekday],
        hour_utc=hour,
        minutes_to_next_h1_close=mins_to_h1,
    )
