"""Entry point: python -m server.worker"""

import asyncio

from server.worker.loop import WorkerLoop


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
