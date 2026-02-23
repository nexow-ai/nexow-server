"""FastAPI application — main entry point for the API server."""

import asyncio
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nexow.api.routes import health, data, agents, bots, backtest, labs, markets
from nexow.api.ws import router as ws_router, _redis_relay
from nexow.config import settings

logger = structlog.get_logger(__name__)

relay_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle — start WS relay on startup.

    NOTE: Market data polling runs as a separate process (`python -m nexow.poller`)
    so you can scale API replicas on Fly.io without duplicating pollers.
    """
    global relay_task

    logger.info("nexow_server_starting", environment=settings.environment)

    relay_task = asyncio.create_task(_redis_relay())

    logger.info("nexow_server_started", port=settings.port)
    yield

    logger.info("nexow_server_stopping")
    if relay_task:
        relay_task.cancel()
        try:
            await relay_task
        except asyncio.CancelledError:
            pass
    logger.info("nexow_server_stopped")


app = FastAPI(
    title="Nexow Server",
    description="Nexow trading platform — API server",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(data.router)
app.include_router(markets.router)
app.include_router(bots.router)
app.include_router(agents.router)
app.include_router(backtest.router)
app.include_router(labs.router)
app.include_router(ws_router)
