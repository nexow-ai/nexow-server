"""Hybrid price runner — Massive flat files as source of truth, Oanda fills the gap.

Cycle:
1. Check if yesterday's Massive flat file is available
2. If yes: delete Oanda rows for that date → insert flat file (source of truth)
3. Fetch Oanda M1 from last known bar until now → fill the gap
4. Every minute: append latest Oanda M1 bar
5. Next day: repeat from step 1
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog

from nexow.broker.oanda import OandaClient
from nexow.db.client import SupabaseClient
from nexow.snapshot.prices import download_minute_aggs, fetch_oanda_minute_bars

logger = structlog.get_logger(__name__)

INSTRUMENTS = ["EUR_USD"]
BATCH_SIZE = 500


async def _upsert_batch(db: SupabaseClient, rows: list[dict[str, Any]]) -> int:
    """Upsert rows in batches. Returns count."""
    if not rows:
        return 0
    count = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        try:
            db.upsert_forex_prices(batch)
            count += len(batch)
        except Exception as e:
            logger.warning("price_upsert_failed", batch_start=i, error=str(e))
    return count


async def _ingest_flat_file(db: SupabaseClient, instrument: str, target_date: date) -> bool:
    """Download flat file, replace Oanda data for that date. Returns True if successful."""
    rows = await asyncio.to_thread(download_minute_aggs, target_date, instrument)
    if not rows:
        return False

    # Delete provisional Oanda data for this date
    try:
        db.delete_oanda_prices(instrument, target_date.isoformat())
        logger.info("oanda_data_replaced", instrument=instrument, date=target_date.isoformat())
    except Exception as e:
        logger.warning("oanda_delete_failed", instrument=instrument, error=str(e))

    # Insert flat file data
    upserted = await _upsert_batch(db, rows)
    logger.info(
        "flatfile_ingested",
        instrument=instrument,
        date=target_date.isoformat(),
        rows=upserted,
    )
    return True


async def _fill_oanda_gap(db: SupabaseClient, oanda: OandaClient, instrument: str) -> int:
    """Fetch Oanda M1 from the last known bar to now. Returns rows inserted."""
    latest_ts = db.get_latest_price_ts(instrument)

    if latest_ts:
        from_time = datetime.fromisoformat(latest_ts) + timedelta(minutes=1)
    else:
        # No data at all — start from 24h ago
        from_time = datetime.now(timezone.utc) - timedelta(hours=24)

    now = datetime.now(timezone.utc)
    if from_time >= now - timedelta(minutes=1):
        return 0  # Already up to date

    rows = await fetch_oanda_minute_bars(instrument, from_time, oanda=oanda)
    return await _upsert_batch(db, rows)


async def run_prices_loop() -> None:
    """Main loop — every minute, manage flat file ingestion + Oanda fill."""
    db = SupabaseClient()
    oanda = OandaClient()
    flat_file_done: dict[str, date] = {}  # instrument -> last flat file date ingested

    logger.info("prices_runner_started", instruments=INSTRUMENTS)

    while True:
        try:
            now = datetime.now(timezone.utc)
            yesterday = (now - timedelta(days=1)).date()

            for instrument in INSTRUMENTS:
                # Step 1: Try to ingest yesterday's flat file (if not done yet)
                if flat_file_done.get(instrument) != yesterday:
                    success = await _ingest_flat_file(db, instrument, yesterday)
                    if success:
                        flat_file_done[instrument] = yesterday

                # Step 2: Fill gap from last bar to now with Oanda
                filled = await _fill_oanda_gap(db, oanda, instrument)
                if filled:
                    logger.info("oanda_gap_filled", instrument=instrument, rows=filled)

        except Exception as e:
            logger.error("prices_loop_error", error=str(e))

        # Wait 60 seconds
        await asyncio.sleep(60)


async def main() -> None:
    """Entry point: python -m nexow.snapshot.prices_runner"""
    try:
        await run_prices_loop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
