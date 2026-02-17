"""Entry point: python -m nexow.worker"""

import asyncio

from nexow.worker.loop import WorkerLoop


async def main() -> None:
    loop = WorkerLoop()
    try:
        await loop.start()
    except KeyboardInterrupt:
        pass
    finally:
        await loop.stop()


if __name__ == "__main__":
    asyncio.run(main())
