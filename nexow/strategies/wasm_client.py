"""HTTP client for the WASM executor sidecar.

Sends Python strategy code + candle data to the Pyodide sandbox
and returns the trading signal.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from nexow.config import settings

logger = structlog.get_logger(__name__)

# Reusable async client (connection pooling)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.wasm_executor_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
    return _client


async def execute_strategy(
    code: str,
    candles: list[dict[str, Any]],
    current_price: float,
    open_trade_count: int = 0,
    timeout_ms: int = 5000,
) -> str:
    """Execute a Python strategy in the WASM sandbox.

    Returns one of: "buy", "sell", "close", "hold"
    """
    client = _get_client()

    try:
        resp = await client.post(
            "/execute",
            json={
                "code": code,
                "candles": candles,
                "current_price": current_price,
                "open_trade_count": open_trade_count,
                "timeout_ms": timeout_ms,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            logger.warning("wasm_execution_error", error=data["error"])

        action = data.get("action", "hold")
        if action not in ("buy", "sell", "close", "hold"):
            logger.warning("wasm_invalid_action", action=action)
            return "hold"

        return action

    except httpx.HTTPStatusError as e:
        logger.error("wasm_http_error", status=e.response.status_code, detail=str(e))
        return "hold"
    except httpx.ConnectError:
        logger.error("wasm_executor_unavailable", url=settings.wasm_executor_url)
        return "hold"
    except Exception as e:
        logger.error("wasm_client_error", error=str(e))
        return "hold"


async def check_health() -> dict[str, Any]:
    """Check the executor sidecar health."""
    client = _get_client()
    try:
        resp = await client.get("/health")
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
