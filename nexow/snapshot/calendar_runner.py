"""Scheduled runner — scrapes Forex Factory economic calendar 8x per hour.

Trigger minutes: 00, 01, 15, 16, 30, 31, 45, 46
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from nexow.db.client import SupabaseClient
from nexow.snapshot.calendar import scrape_calendar_ocr

logger = structlog.get_logger(__name__)

OCR_MINUTES = frozenset({0, 1, 15, 16, 30, 31, 45, 46})


async def _upsert_events(db: SupabaseClient, events: list[dict]) -> int:
    """Upsert scraped events into Supabase. Returns count of upserted rows."""
    if not events:
        return 0

    count = 0
    for ev in events:
        try:
            db.upsert_economic_event(ev)
            count += 1
        except Exception as e:
            logger.warning("event_upsert_failed", event=ev.get("event", "?"), error=str(e))
    return count


async def run_calendar_loop() -> None:
    """Main loop — OCR scrape at trigger minutes, sleep until next one."""
    db = SupabaseClient()
    already_done: set[tuple[int, int]] = set()

    logger.info("calendar_runner_started", trigger_minutes=sorted(OCR_MINUTES))

    while True:
        try:
            now = datetime.now(timezone.utc)
            key = (now.hour, now.minute)

            if now.minute in OCR_MINUTES and key not in already_done:
                logger.info("calendar_scrape_triggered", hour=now.hour, minute=now.minute)

                events = await scrape_calendar_ocr()
                upserted = await _upsert_events(db, events)

                logger.info(
                    "calendar_scrape_complete",
                    hour=now.hour,
                    minute=now.minute,
                    scraped=len(events),
                    upserted=upserted,
                )

                already_done.add(key)
                already_done = {(h, m) for h, m in already_done if h == now.hour}

        except Exception as e:
            logger.error("calendar_loop_error", error=str(e))

        await asyncio.sleep(30)


async def main() -> None:
    """Entry point: python -m nexow.snapshot.calendar_runner"""
    await run_calendar_loop()


if __name__ == "__main__":
    asyncio.run(main())
