"""Market data poller entry point.

Run with: `python -m nexow.poller`
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from nexow.worker.poller import MarketDataPoller

logger = structlog.get_logger(__name__)


async def _run() -> None:
    poller = MarketDataPoller()

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # Some runtimes don't support signal handlers (e.g. Windows).
            pass

    task = asyncio.create_task(poller.start())
    await stop_event.wait()

    logger.info("poller_stop_requested")
    await poller.stop()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()

