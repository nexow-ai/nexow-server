"""Oanda v20 REST API client — market data provider."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import structlog

from nexow.broker.models import Candle
from nexow.config import settings

logger = structlog.get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


class OandaClient:
    """
    Async HTTP client for the Oanda v20 REST API.

    Used as a market data provider: candles, live prices, historical data.
    """

    def __init__(self) -> None:
        self._base_url = settings.oanda_api_url
        self._account_id = settings.oanda_account_id
        self._headers = {
            "Authorization": f"Bearer {settings.oanda_api_token}",
            "Content-Type": "application/json",
        }
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=30.0,
        )

    @property
    def account_url(self) -> str:
        return f"/v3/accounts/{self._account_id}"

    async def _get_with_retry(
        self,
        url: str,
        params: dict,
    ) -> httpx.Response:
        """GET with automatic retry on transient errors (502, 503, 429, etc.)."""
        for attempt in range(MAX_RETRIES + 1):
            resp = await self._http.get(url, params=params)
            if resp.status_code not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
                resp.raise_for_status()
                return resp
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "oanda_retrying",
                status=resp.status_code,
                attempt=attempt + 1,
                delay=delay,
                url=url,
            )
            await asyncio.sleep(delay)
        return resp

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    async def get_candles(
        self,
        instrument: str,
        granularity: str = "M5",
        count: int = 100,
    ) -> list[Candle]:
        """Fetch recent candles for an instrument."""
        url = f"{self.account_url}/instruments/{instrument}/candles"
        params = {"granularity": granularity, "count": count, "price": "M"}
        resp = await self._get_with_retry(url, params)
        data = resp.json()

        candles: list[Candle] = []
        for c in data.get("candles", []):
            if not c.get("complete", False):
                continue
            mid = c["mid"]
            candles.append(
                Candle(
                    instrument=instrument,
                    time=datetime.fromisoformat(c["time"].replace("Z", "+00:00")),
                    open=float(mid["o"]),
                    high=float(mid["h"]),
                    low=float(mid["l"]),
                    close=float(mid["c"]),
                    volume=int(c["volume"]),
                )
            )
        return candles

    async def get_candles_range(
        self,
        instrument: str,
        granularity: str,
        from_time: datetime,
        to_time: datetime,
    ) -> list[Candle]:
        """
        Fetch candles for a date range, paginating in chunks of 5000.

        Oanda v20 allows max 5000 candles per request. We paginate forward
        using ``from`` + ``count`` and stop once we pass ``to_time``.
        """
        url = f"{self.account_url}/instruments/{instrument}/candles"
        all_candles: list[Candle] = []
        cursor = from_time

        while cursor < to_time:
            params: dict[str, str | int] = {
                "granularity": granularity,
                "price": "M",
                "from": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": 5000,
            }
            resp = await self._get_with_retry(url, params)
            data = resp.json()

            raw_candles = data.get("candles", [])
            if not raw_candles:
                break

            batch: list[Candle] = []
            for c in raw_candles:
                if not c.get("complete", False):
                    continue
                mid = c["mid"]
                candle = Candle(
                    instrument=instrument,
                    time=datetime.fromisoformat(c["time"].replace("Z", "+00:00")),
                    open=float(mid["o"]),
                    high=float(mid["h"]),
                    low=float(mid["l"]),
                    close=float(mid["c"]),
                    volume=int(c["volume"]),
                )
                if candle.time.replace(tzinfo=None) > to_time.replace(tzinfo=None):
                    break
                batch.append(candle)

            if not batch:
                break

            all_candles.extend(batch)

            last_time = batch[-1].time.replace(tzinfo=None)
            if last_time <= cursor.replace(tzinfo=None):
                break
            cursor = last_time.replace(tzinfo=from_time.tzinfo)

            if len(raw_candles) < 5000:
                break

            logger.debug(
                "candles_range_page",
                instrument=instrument,
                fetched=len(batch),
                total=len(all_candles),
                cursor=cursor.isoformat(),
            )

        logger.info(
            "candles_range_complete",
            instrument=instrument,
            granularity=granularity,
            total_candles=len(all_candles),
        )
        return all_candles

    async def get_price(self, instrument: str) -> float:
        """Get the current midpoint price for an instrument."""
        url = f"{self.account_url}/pricing"
        params = {"instruments": instrument}
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        price_data = data["prices"][0]
        bid = float(price_data["bids"][0]["price"])
        ask = float(price_data["asks"][0]["price"])
        return (bid + ask) / 2

    async def get_prices(self, instruments: list[str]) -> dict[str, float]:
        """Get current midpoint prices for multiple instruments."""
        url = f"{self.account_url}/pricing"
        params = {"instruments": ",".join(instruments)}
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        prices = {}
        for price_data in data["prices"]:
            instrument = price_data["instrument"]
            bid = float(price_data["bids"][0]["price"])
            ask = float(price_data["asks"][0]["price"])
            prices[instrument] = (bid + ask) / 2

        return prices

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
