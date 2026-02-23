"""IG Markets REST API client — categories, instruments, markets."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from nexow.config import settings

logger = structlog.get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
FORBIDDEN_STATUS = 403


class IGForbiddenError(Exception):
    """Raised when IG returns 403 (e.g. API allowance exceeded)."""

    def __init__(self, message: str = "IG API access forbidden (e.g. allowance exceeded). Try again later."):
        self.message = message
        super().__init__(message)


def _ttl_cache(ttl_seconds: float):
    """In-memory TTL cache to reduce IG API calls."""
    cache: dict[str, tuple[float, Any]] = {}

    def get(key: str) -> Any | None:
        now = time.monotonic()
        if key in cache:
            expiry, val = cache[key]
            if now < expiry:
                return val
            del cache[key]
        return None

    def set(key: str, value: Any) -> None:
        cache[key] = (time.monotonic() + ttl_seconds, value)

    return get, set


_CACHE_MARKET_GET = 90
_CACHE_CATEGORIES = 120
_CACHE_CATEGORY_INSTRUMENTS = 90
_CACHE_PRICES = 45

_cache_market_get, _cache_market_set = _ttl_cache(_CACHE_MARKET_GET)
_cache_cat_get, _cache_cat_set = _ttl_cache(_CACHE_CATEGORIES)
_cache_instr_get, _cache_instr_set = _ttl_cache(_CACHE_CATEGORY_INSTRUMENTS)
_cache_prices_get, _cache_prices_set = _ttl_cache(_CACHE_PRICES)


class IGClient:
    """
    Async HTTP client for the IG Markets REST API.

    Uses session-based auth (POST /session) to obtain CST and X-SECURITY-TOKEN.
    Supports categories, instruments, and markets endpoints.
    """

    GATEWAY_PATH = "/gateway/deal"

    def __init__(self) -> None:
        base = settings.ig_api_url.rstrip("/")
        self._base_url = f"{base}{self.GATEWAY_PATH}"
        self._api_key = settings.ig_api_key
        self._username = settings.ig_username
        self._password = settings.ig_password
        self._cst: str | None = None
        self._x_security_token: str | None = None
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=30.0,
        )

    def _headers(self, version: int = 1) -> dict[str, str]:
        headers: dict[str, str] = {
            "X-IG-API-KEY": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json; charset=UTF-8",
            "Version": str(version),
        }
        if self._cst:
            headers["CST"] = self._cst
        if self._x_security_token:
            headers["X-SECURITY-TOKEN"] = self._x_security_token
        return headers

    async def _ensure_session(self) -> None:
        """Create or refresh session if needed."""
        if self._cst and self._x_security_token:
            return
        if not self._username or not self._password:
            raise ValueError("IG_USERNAME and IG_PASSWORD are required for IG session")
        if not self._api_key:
            raise ValueError("IG_API_TOKEN (API key) is required")

        body = {
            "identifier": self._username,
            "password": self._password,
        }
        resp = await self._http.post(
            "/session",
            headers=self._headers(version=2),
            json=body,
        )
        resp.raise_for_status()
        self._cst = resp.headers.get("cst") or resp.headers.get("CST")
        self._x_security_token = resp.headers.get("x-security-token") or resp.headers.get("X-SECURITY-TOKEN")
        if not self._cst or not self._x_security_token:
            raise RuntimeError("IG session did not return CST and X-SECURITY-TOKEN")
        logger.info("ig_session_created")

    async def _get_with_retry(
        self,
        url: str,
        params: dict[str, str | int] | None = None,
        version: int = 1,
    ) -> httpx.Response:
        await self._ensure_session()
        for attempt in range(MAX_RETRIES + 1):
            resp = await self._http.get(url, headers=self._headers(version=version), params=params or {})
            if resp.status_code == FORBIDDEN_STATUS:
                logger.warning("ig_forbidden", url=url, status=403)
                raise IGForbiddenError(
                    "IG API returned 403 Forbidden. You may have exceeded your API allowance. Try again later."
                )
            if resp.status_code == 401 and attempt < MAX_RETRIES:
                self._cst = None
                self._x_security_token = None
                await self._ensure_session()
                continue
            if resp.status_code not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES:
                resp.raise_for_status()
                return resp
            delay = RETRY_BASE_DELAY * (2**attempt)
            logger.warning("ig_retrying", status=resp.status_code, attempt=attempt + 1, delay=delay, url=url)
            await asyncio.sleep(delay)
        return resp

    # ------------------------------------------------------------------
    # Categories & Instruments
    # ------------------------------------------------------------------

    async def get_categories(self) -> list[dict[str, Any]]:
        """Returns a list of all categories of instruments enabled for the IG account."""
        key = "categories"
        cached = _cache_cat_get(key)
        if cached is not None:
            return cached
        resp = await self._get_with_retry("/categories")
        data = resp.json()
        out = data.get("categories", [])
        _cache_cat_set(key, out)
        return out

    async def get_category_instruments(self, category_id: str) -> list[dict[str, Any]]:
        """Returns all instruments for the given category."""
        key = f"instruments:{category_id}"
        cached = _cache_instr_get(key)
        if cached is not None:
            return cached
        resp = await self._get_with_retry(f"/categories/{category_id}/instruments")
        data = resp.json()
        out = data.get("instruments", [])
        _cache_instr_set(key, out)
        return out

    # ------------------------------------------------------------------
    # Markets
    # ------------------------------------------------------------------

    async def get_markets(self, epics: list[str] | None = None, filter_type: str = "ALL") -> list[dict[str, Any]]:
        """Returns details of the given markets (version 2, up to 50 epics)."""
        if not epics:
            return []
        epics_str = ",".join(epics[:50])
        resp = await self._get_with_retry(
            "/markets",
            params={"epics": epics_str, "filter": filter_type},
            version=2,
        )
        data = resp.json()
        return data.get("marketDetails", [])

    async def get_market(self, epic: str) -> dict[str, Any] | None:
        """Returns details of the given market."""
        key = f"market:{epic}"
        cached = _cache_market_get(key)
        if cached is not None:
            return cached
        resp = await self._get_with_retry(f"/markets/{epic}", version=2)
        data = resp.json()
        _cache_market_set(key, data)
        return data

    async def search_markets(self, search_term: str) -> list[dict[str, Any]]:
        """Returns all markets matching the search term."""
        resp = await self._get_with_retry("/markets", params={"searchTerm": search_term})
        data = resp.json()
        return data.get("markets", [])

    async def get_prices(
        self,
        epic: str,
        resolution: str = "MINUTE",
        num_points: int = 100,
    ) -> list[dict[str, Any]]:
        """Returns historical prices for the given epic (version 2)."""
        key = f"prices:{epic}:{resolution}:{num_points}"
        cached = _cache_prices_get(key)
        if cached is not None:
            return cached
        resp = await self._get_with_retry(
            f"/prices/{epic}/{resolution}/{num_points}",
            version=2,
        )
        data = resp.json()
        out = data.get("prices", [])
        _cache_prices_set(key, out)
        return out

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
