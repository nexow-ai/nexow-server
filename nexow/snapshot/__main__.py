"""Entry point: python -m nexow.snapshot

Runs both background loops in parallel:
- Economic calendar scraper (8x/hour)
- Massive flat files price ingestion (every minute)
"""

import asyncio

import structlog

from nexow.snapshot.calendar_runner import run_calendar_loop
from nexow.snapshot.prices_runner import run_prices_loop

logger = structlog.get_logger(__name__)


async def main() -> None:
    logger.info("snapshot_service_starting")
    try:
        await asyncio.gather(
            run_calendar_loop(),
            run_prices_loop(),
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
