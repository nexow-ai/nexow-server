"""HTTP client for the WASM sandbox sidecar.

Sends Python strategy code + candle data to the Pyodide sandbox
and returns the trading signal.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from server.config import settings

logger = structlog.get_logger(__name__)

# Reusable async client (connection pooling)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.sandbox_url,
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
        logger.error("sandbox_unavailable", url=settings.sandbox_url)
        return "hold"
    except Exception as e:
        logger.error("wasm_client_error", error=str(e))
        return "hold"


def _generate_sample_candles(count: int = 50) -> list[dict[str, Any]]:
    """Generate deterministic sample OHLCV candles for dry-run validation.

    Produces a zigzag price pattern with enough data points for common
    indicators (RSI-14, SMA-20, Bollinger-20, etc.).
    """
    candles = []
    base = 1.10000
    for i in range(count):
        offset = ((i % 10) - 5) * 0.001
        o = round(base + offset, 5)
        c = round(o + 0.0005, 5)
        h = round(max(o, c) + 0.001, 5)
        l = round(min(o, c) - 0.001, 5)
        candles.append(
            {
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000 + i * 10,
                "time": f"2024-01-{(i // 24) + 1:02d}T{i % 24:02d}:00:00",
            }
        )
    return candles


async def dry_run_strategy(code: str) -> tuple[str, str | None]:
    """Execute strategy code against sample data to verify it runs.

    Returns ``(action, error)`` — *error* is ``None`` when the code
    executed successfully.  If the sandbox sidecar is unreachable the
    dry-run is silently skipped (returns ``("hold", None)``).
    """
    sample_candles = _generate_sample_candles(50)
    current_price = sample_candles[-1]["close"]

    client = _get_client()
    try:
        resp = await client.post(
            "/execute",
            json={
                "code": code,
                "candles": sample_candles,
                "current_price": current_price,
                "open_trade_count": 0,
                "timeout_ms": 5000,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        error = data.get("error")
        action = data.get("action", "hold")
        return action, error

    except httpx.ConnectError:
        logger.warning("dry_run_skipped_sandbox_unavailable")
        return "hold", None
    except Exception as e:
        return "hold", str(e)


async def check_health() -> dict[str, Any]:
    """Check the sandbox sidecar health."""
    client = _get_client()
    try:
        resp = await client.get("/health")
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
