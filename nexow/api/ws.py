"""WebSocket server — real-time price updates, agent logs, notifications."""

from __future__ import annotations

import asyncio
import json

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from nexow.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages active WebSocket connections by channel."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, channel: str) -> None:
        await ws.accept()
        self._connections.setdefault(channel, set()).add(ws)
        logger.info("ws_connected", channel=channel)

    def disconnect(self, ws: WebSocket, channel: str) -> None:
        conns = self._connections.get(channel, set())
        conns.discard(ws)
        if not conns:
            self._connections.pop(channel, None)

    async def broadcast(self, channel: str, data: str) -> None:
        conns = self._connections.get(channel, set())
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)


manager = ConnectionManager()


async def _redis_relay() -> None:
    """Background task: subscribe to Redis and relay to WebSocket clients."""
    try:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(settings.redis_channel)

        logger.info("ws_redis_relay_started", channel=settings.redis_channel)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            await manager.broadcast("prices", message["data"])

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("ws_redis_relay_error", error=str(e))
    finally:
        try:
            await pubsub.unsubscribe(settings.redis_channel)
            await redis_client.aclose()
        except Exception:
            pass


@router.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    """WebSocket endpoint for real-time price updates."""
    await manager.connect(websocket, "prices")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, "prices")


@router.websocket("/ws/agent/{agent_id}/logs")
async def ws_agent_logs(websocket: WebSocket, agent_id: str):
    """WebSocket endpoint for real-time agent log streaming."""
    channel = f"agent:logs:{agent_id}"
    await manager.connect(websocket, channel)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, channel)
