"""Saxo Bank OpenAPI — REST client for CM, Portfolio, Trading, Reference Data."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from nexow.broker.saxo.auth import refresh_access_token
from nexow.config import settings

logger = structlog.get_logger(__name__)

REFRESH_BUFFER_SECONDS = 60


class SaxoClientError(Exception):
    """Saxo API error (4xx/5xx or API-level error)."""

    def __init__(self, message: str, status_code: int | None = None, body: dict | None = None):
        self.status_code = status_code
        self.body = body
        super().__init__(message)


class SaxoClient:
    """
    Async HTTP client for Saxo Bank OpenAPI.

    Supports Client Management (onboarding), Reference Data, Portfolio, and Trading.
    Uses platform-level tokens (set via set_platform_tokens after OAuth callback).
    """

    def __init__(self) -> None:
        self._base_url = settings.saxo_base_url.rstrip("/")
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)

    def set_platform_tokens(
        self,
        access_token: str,
        refresh_token: str | None = None,
        expires_in: int = 1200,
    ) -> None:
        """Set tokens (e.g. after OAuth callback). expires_in in seconds from Saxo."""
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = time.monotonic() + max(0, expires_in - REFRESH_BUFFER_SECONDS)

    def set_access_token_only(self, access_token: str) -> None:
        """Use a single access token (e.g. 24h dev token) without refresh."""
        self._access_token = access_token
        self._refresh_token = None
        self._expires_at = float("inf")

    async def _ensure_valid_token(self) -> str:
        """Return valid access token, refreshing if needed."""
        async with self._lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            if self._refresh_token:
                data = await refresh_access_token(self._refresh_token)
                self._access_token = data["access_token"]
                self._expires_at = time.monotonic() + max(
                    0, data.get("expires_in", 1200) - REFRESH_BUFFER_SECONDS
                )
                if "refresh_token" in data:
                    self._refresh_token = data["refresh_token"]
                return self._access_token
        raise SaxoClientError(
            "No valid Saxo token. Complete OAuth flow or set SAXO_ACCESS_TOKEN for dev."
        )

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: Any = None,
    ) -> dict[str, Any] | list[Any] | None:
        """Send request to OpenAPI. Path is e.g. 'port/v1/users/me' (no leading slash)."""
        token = await self._ensure_valid_token()
        path = path.lstrip("/")
        headers = self._headers(token)
        try:
            resp = await self._http.request(
                method,
                path,
                params=params,
                json=json,
                data=data,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.exception("saxo_request_error", path=path, error=str(e))
            raise SaxoClientError(str(e))
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = None
            raise SaxoClientError(
                f"Saxo API error: {resp.status_code}",
                status_code=resp.status_code,
                body=body,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- Root / session (Phase 0) ---

    async def get_me(self) -> dict[str, Any]:
        """Validate token and get current user info (Root/Port)."""
        out = await self._request("GET", "port/v1/users/me")
        return out or {}

    # --- Client Management (Phase 1) ---

    async def cm_create_signup(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /cm/v1/signups — create signup, returns ClientId, ClientKey, SignupId."""
        out = await self._request("POST", "cm/v1/signups", json=body)
        return out or {}

    async def cm_upload_attachment(
        self,
        signup_id: str,
        document_type: str,
        file_content: bytes,
        content_type: str = "application/pdf",
    ) -> dict[str, Any] | None:
        """POST /cm/v1/signups/attachments/{signupId} — upload document."""
        token = await self._ensure_valid_token()
        url = f"{self._base_url}/cm/v1/signups/attachments/{signup_id}"
        headers = self._headers(token)
        # Saxo often expects multipart for attachments
        files = {"file": (f"doc_{document_type}", file_content, content_type)}
        try:
            resp = await self._http.post(url, headers=headers, files=files)
        except httpx.HTTPError as e:
            raise SaxoClientError(str(e))
        if resp.status_code >= 400:
            raise SaxoClientError(
                f"Saxo API error: {resp.status_code}",
                status_code=resp.status_code,
                body=resp.json() if resp.content else None,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def cm_complete_application(self, signup_id: str) -> dict[str, Any] | None:
        """PUT /cm/v1/signups/completeapplication/{signupId}."""
        return await self._request("PUT", f"cm/v1/signups/completeapplication/{signup_id}")

    async def cm_get_status(self, client_key: str) -> dict[str, Any]:
        """GET /cm/v1/signups/status/{clientKey} — onboarding status."""
        out = await self._request("GET", f"cm/v1/signups/status/{client_key}")
        return out or {}

    async def cm_initiate_verification(self, client_key: str) -> dict[str, Any]:
        """POST /cm/v1/signups/verification/initiate/{clientKey}."""
        out = await self._request("POST", f"cm/v1/signups/verification/initiate/{client_key}")
        return out or {}

    async def cm_get_onboarding_pdf(self, client_key: str) -> bytes:
        """GET /cm/v1/signups/onboardingpdf/{clientKey} — returns PDF bytes."""
        token = await self._ensure_valid_token()
        url = f"{self._base_url}/cm/v1/signups/onboardingpdf/{client_key}"
        resp = await self._http.get(url, headers=self._headers(token))
        if resp.status_code >= 400:
            raise SaxoClientError(
                f"Saxo API error: {resp.status_code}",
                status_code=resp.status_code,
            )
        return resp.content

    async def cm_get_options(self) -> dict[str, Any]:
        """GET /cm/v1/signups/options — dropdown/options for signup forms."""
        out = await self._request("GET", "cm/v1/signups/options")
        return out or {}

    async def cm_create_account(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /cm/v2/accounts — create an additional account for the existing client. Body must have accountCreateRequest with at least ClientKey."""
        out = await self._request("POST", "cm/v2/accounts", json=body)
        return out or {}

    # --- Reference Data (Phase 2) ---

    async def ref_instruments(self, params: dict[str, Any] | None = None) -> list[Any]:
        """Reference data: instruments (optional params: AccountKey, AssetTypes, etc.)."""
        out = await self._request("GET", "ref/v1/instruments", params=params or {})
        if isinstance(out, list):
            return out
        return out.get("Data", []) if isinstance(out, dict) else []

    async def ref_exchanges(self) -> list[Any]:
        """Reference data: exchanges."""
        out = await self._request("GET", "ref/v1/exchanges")
        if isinstance(out, list):
            return out
        return out.get("Data", []) if isinstance(out, dict) else []

    async def ref_instrument_details(self, uic: int, asset_type: str) -> dict[str, Any]:
        """GET ref/v1/instruments/details/{Uic}/{AssetType} — detailed instrument info."""
        out = await self._request(
            "GET", f"ref/v1/instruments/details/{uic}/{asset_type}"
        )
        return out or {}

    async def chart_data(
        self,
        uic: int,
        asset_type: str,
        horizon_minutes: int = 60,
        count: int = 100,
    ) -> list[Any]:
        """GET chart historical OHLC. Tries chart/v3/charts then chart/v1/charts. Returns [] on 404."""
        base_params: dict[str, Any] = {
            "Uic": uic,
            "AssetType": asset_type,
            "Horizon": horizon_minutes,
            "Count": count,
        }
        for path, params in (
            ("chart/v3/charts", {**base_params, "FieldGroups": "Data"}),
            ("chart/v1/charts", base_params),
        ):
            try:
                out = await self._request("GET", path, params=params)
                if isinstance(out, list):
                    return out
                data = out.get("Data", []) if isinstance(out, dict) else []
                if data:
                    return data
                if out:
                    logger.warning(
                        "saxo_chart_empty",
                        path=path,
                        uic=uic,
                        asset_type=asset_type,
                        response_keys=list(out.keys()) if isinstance(out, dict) else None,
                    )
            except SaxoClientError as e:
                if e.status_code == 404:
                    continue
                raise
        return []

    async def chart_data_raw(
        self,
        uic: int,
        asset_type: str,
        horizon_minutes: int = 60,
        count: int = 100,
        path: str = "chart/v3/charts",
    ) -> dict[str, Any]:
        """Same as chart_data but returns the raw Saxo response (for debugging)."""
        params: dict[str, Any] = {
            "Uic": uic,
            "AssetType": asset_type,
            "Horizon": horizon_minutes,
            "Count": count,
        }
        if path == "chart/v3/charts":
            params["FieldGroups"] = "Data"
        out = await self._request("GET", path, params=params)
        if isinstance(out, dict):
            return out
        return {"Data": out} if isinstance(out, list) else {}

    async def infoprices(self, uic: int, asset_type: str) -> dict[str, Any]:
        """GET trade/v1/infoprices — current quote for instrument. Returns {} on 404 only."""
        params = {"Uic": uic, "AssetType": asset_type}
        try:
            out = await self._request("GET", "trade/v1/infoprices", params=params)
            return out or {}
        except SaxoClientError as e:
            if e.status_code == 404:
                return {}
            raise

    # --- Portfolio (Phase 2) ---

    async def port_accounts_me(
        self, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """GET port/v1/accounts/me — accounts for the logged-in user."""
        out = await self._request("GET", "port/v1/accounts/me", params=params or {})
        if isinstance(out, list):
            return out
        return out.get("Data", []) if isinstance(out, dict) else []

    async def port_account(self, account_key: str) -> dict[str, Any]:
        """GET port/v1/accounts/{AccountKey} — single account details."""
        out = await self._request("GET", f"port/v1/accounts/{account_key}")
        return out or {}

    async def port_update_account(
        self, account_key: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH port/v1/accounts/{AccountKey} — update account (e.g. DisplayName)."""
        out = await self._request(
            "PATCH", f"port/v1/accounts/{account_key}", json=body
        )
        return out or {}

    async def port_balances(self, params: dict[str, Any] | None = None) -> list[Any]:
        """GET port/v1/balances — balances for the authenticated user."""
        out = await self._request("GET", "port/v1/balances", params=params or {})
        if isinstance(out, list):
            return out
        return out.get("Data", []) if isinstance(out, dict) else []

    async def port_positions(self, params: dict[str, Any] | None = None) -> list[Any]:
        """GET port/v1/positions — positions for the authenticated user."""
        out = await self._request("GET", "port/v1/positions", params=params or {})
        if isinstance(out, list):
            return out
        return out.get("Data", []) if isinstance(out, dict) else []

    async def port_clients(self) -> list[Any]:
        """GET port/v1/clients — clients under the user."""
        out = await self._request("GET", "port/v1/clients")
        if isinstance(out, list):
            return out
        return out.get("Data", []) if isinstance(out, dict) else []

    # --- Trading (Phase 2) ---

    async def trade_orders(self, params: dict[str, Any] | None = None) -> list[Any]:
        """GET trade/v1/orders — open orders."""
        out = await self._request("GET", "trade/v1/orders", params=params or {})
        if isinstance(out, list):
            return out
        return out.get("Data", []) if isinstance(out, dict) else []

    async def trade_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST trade/v1/orders — place order."""
        out = await self._request("POST", "trade/v1/orders", json=body)
        return out or {}

    async def trade_cancel_order(self, order_id: str) -> None:
        """DELETE trade/v1/orders/{orderId}."""
        await self._request("DELETE", f"trade/v1/orders/{order_id}")

    async def close(self) -> None:
        """Close HTTP client."""
        await self._http.aclose()
