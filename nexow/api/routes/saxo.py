"""Saxo Bank OpenAPI — auth callback, onboarding (CM), and trading/portfolio proxies."""

import base64
import json

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from nexow.broker.saxo import (
    SaxoClient,
    SaxoClientError,
    authorization_url,
    exchange_code_for_tokens,
)
from nexow.config import settings

def _frontend_redirect_base() -> str:
    return settings.frontend_url.rstrip("/")


def _resolve_saxo_redirect_uri(
    redirect_uri: str | None = None,
    label: str | None = None,
    *,
    host: str | None = None,
) -> str | None:
    """Resolve redirect_uri from explicit value, label, or localhost fallback."""
    if redirect_uri:
        return redirect_uri
    if label == "development" and getattr(settings, "saxo_redirect_uri_development", ""):
        return settings.saxo_redirect_uri_development
    # When backend is called from localhost and no label/uri given, use development redirect if set
    dev_uri = getattr(settings, "saxo_redirect_uri_development", "") or ""
    if dev_uri and host and (host.startswith("localhost") or host.startswith("127.0.0.1")):
        return dev_uri
    return None


def _encode_state(csrf: str, redirect_uri: str) -> str:
    """Encode csrf + redirect_uri so callback can use same redirect_uri for token exchange."""
    payload = {"csrf": csrf, "ru": redirect_uri}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _decode_state(state: str) -> tuple[str, str | None]:
    """Decode state; returns (csrf, redirect_uri or None)."""
    try:
        padded = state + "=" * (4 - len(state) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        if isinstance(data, dict) and "csrf" in data:
            return data["csrf"], data.get("ru")
    except Exception:
        pass
    return state, None


# Saxo ref/v1/instruments expects these AssetType values; map UI names to API values.
_SAXO_ASSET_TYPE_MAP = {
    "Future": "ContractFutures",
    "Forward": "FxForwards",
    "CfdOnCommodity": "CfdOnFutures",
}

# 403 from Saxo on SIM = no access to this asset type (e.g. stocks). User must connect SIM to Live.
_SAXO_403_MESSAGE = (
    "Stock and other non-FX data on SIM require linking your SIM account to a Live account. "
    "In developer.saxo: Open API → Apps → Live apps → sign in with your funded Live account. "
    "See https://openapi.help.saxo/hc/en-us/articles/4416934146449"
)

router = APIRouter(prefix="/saxo", tags=["saxo"])

_saxo_client: SaxoClient | None = None


def get_saxo() -> SaxoClient:
    global _saxo_client
    if _saxo_client is None:
        _saxo_client = SaxoClient()
        if settings.saxo_access_token:
            _saxo_client.set_access_token_only(settings.saxo_access_token)
    return _saxo_client


# --- Auth (Phase 0) ---

@router.get("/auth/url")
async def saxo_auth_url(
    request: Request,
    state: str = Query(..., description="State for CSRF"),
    redirect_uri: str | None = Query(None, alias="redirectUri"),
    label: str | None = Query(None, description="Use a named redirect, e.g. development -> SAXO_REDIRECT_URI_DEVELOPMENT"),
):
    """Return the Saxo authorization URL to redirect the user to."""
    if not settings.saxo_app_key:
        raise HTTPException(503, detail="Saxo integration not configured (SAXO_APP_KEY)")
    host = (request.headers.get("host") or "").split(":")[0]
    resolved = _resolve_saxo_redirect_uri(redirect_uri=redirect_uri, label=label, host=host)
    uri = resolved or settings.saxo_redirect_uri
    if not uri:
        raise HTTPException(400, detail="redirect_uri required (set SAXO_REDIRECT_URI or pass redirectUri/label=development with SAXO_REDIRECT_URI_DEVELOPMENT)")
    try:
        state_encoded = _encode_state(state, uri)
        url = authorization_url(state_encoded, redirect_uri=uri)
        return {"url": url, "redirect_uri": uri}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@router.get("/auth/callback")
async def saxo_auth_callback(
    code: str = Query(...),
    state: str = Query(...),
    redirect_uri: str | None = Query(None, alias="redirectUri"),
    frontend_redirect: str | None = Query(None, alias="frontendRedirect"),
):
    """Exchange code for tokens and store; redirect to frontend with state."""
    if not settings.saxo_app_key or not settings.saxo_app_secret:
        raise HTTPException(503, detail="Saxo integration not configured")
    csrf, uri_from_state = _decode_state(state)
    exchange_uri = redirect_uri or uri_from_state or settings.saxo_redirect_uri
    try:
        data = await exchange_code_for_tokens(code, redirect_uri=exchange_uri)
    except Exception as e:
        raise HTTPException(400, detail=f"Token exchange failed: {e}")
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in", 1200)
    if not access:
        raise HTTPException(400, detail="No access_token in response")
    client = get_saxo()
    client.set_platform_tokens(access, refresh_token=refresh, expires_in=expires_in)
    base = (frontend_redirect or _frontend_redirect_base()).rstrip("/")
    return RedirectResponse(url=f"{base}/saxo?state={csrf}&saxo=ok")


@router.post("/auth/tokens")
async def saxo_set_tokens(
    access_token: str = Form(...),
    refresh_token: str | None = Form(None),
    expires_in: int = Form(1200),
):
    """Set platform tokens (e.g. after manual OAuth or for dev with 24h token)."""
    client = get_saxo()
    if refresh_token:
        client.set_platform_tokens(access_token, refresh_token=refresh_token, expires_in=expires_in)
    else:
        client.set_access_token_only(access_token)
    return {"status": "ok"}


@router.get("/me")
async def saxo_me():
    """Validate token and return current user (port/v1/users/me)."""
    try:
        client = get_saxo()
        data = await client.get_me()
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))

# --- Client Management / Onboarding (Phase 1) ---

@router.get("/onboarding/options")
async def saxo_onboarding_options():
    """GET signup form options (dropdowns, etc.)."""
    try:
        client = get_saxo()
        data = await client.cm_get_options()
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.post("/onboarding/signups")
async def saxo_create_signup(body: dict):
    """Create a new signup (onboarding). Returns ClientId, ClientKey, SignupId."""
    try:
        client = get_saxo()
        data = await client.cm_create_signup(body)
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.post("/onboarding/signups/{signup_id}/attachments")
async def saxo_upload_attachment(
    signup_id: str,
    document_type: str = Form(..., alias="documentType"),
    file: UploadFile = File(...),
):
    """Upload a document for a signup (e.g. ID, proof of residency)."""
    try:
        content = await file.read()
        client = get_saxo()
        result = await client.cm_upload_attachment(
            signup_id,
            document_type=document_type,
            file_content=content,
            content_type=file.content_type or "application/octet-stream",
        )
        return result or {"status": "ok"}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.put("/onboarding/signups/{signup_id}/complete")
async def saxo_complete_application(signup_id: str):
    """Mark signup application as complete."""
    try:
        client = get_saxo()
        result = await client.cm_complete_application(signup_id)
        return result or {"status": "ok"}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/onboarding/status/{client_key}")
async def saxo_onboarding_status(client_key: str):
    """Get onboarding status for a client (by ClientKey)."""
    try:
        client = get_saxo()
        data = await client.cm_get_status(client_key)
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.post("/onboarding/verification/{client_key}")
async def saxo_initiate_verification(client_key: str):
    """Initiate verification (e.g. redirect to vendor)."""
    try:
        client = get_saxo()
        data = await client.cm_initiate_verification(client_key)
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/onboarding/pdf/{client_key}")
async def saxo_onboarding_pdf(client_key: str):
    """Download onboarding PDF for client."""
    try:
        client = get_saxo()
        pdf_bytes = await client.cm_get_onboarding_pdf(client_key)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="onboarding_{client_key}.pdf"'},
        )
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))

# --- Reference Data (Phase 2) ---

@router.get("/instruments/details/{uic}/{asset_type}")
async def saxo_instrument_details(uic: int, asset_type: str):
    """Get detailed instrument info by Uic and AssetType. Declared before /instruments so path matches."""
    try:
        client = get_saxo()
        data = await client.ref_instrument_details(uic, asset_type)
        return data
    except SaxoClientError as e:
        detail = _SAXO_403_MESSAGE if e.status_code == 403 else str(e)
        raise HTTPException(e.status_code or 502, detail=detail)


@router.get("/instruments")
async def saxo_instruments(
    account_key: str | None = Query(None, alias="accountKey"),
    asset_types: str | None = Query(None, alias="assetTypes"),
    keywords: str | None = Query(None),
    exchange_id: str | None = Query(None, alias="exchangeId"),
    top: int | None = Query(None, ge=1, le=1000),
    skip: int | None = Query(None, ge=0),
):
    """List instruments (reference data). Supports OData $top/$skip, AssetTypes, Keywords, ExchangeId."""
    try:
        client = get_saxo()
        params: dict[str, str | int] = {}
        if account_key:
            params["AccountKey"] = account_key
        if asset_types:
            api_asset_type = _SAXO_ASSET_TYPE_MAP.get(asset_types, asset_types)
            params["AssetTypes"] = api_asset_type
        if keywords:
            params["Keywords"] = keywords
        if exchange_id:
            params["ExchangeId"] = exchange_id
        effective_top = top
        if effective_top is None:
            effective_top = 100
        if not asset_types:
            effective_top = min(effective_top, 100)
        params["$top"] = effective_top
        if skip is not None:
            params["$skip"] = skip
        data = await client.ref_instruments(params)
        return {"instruments": data}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/exchanges")
async def saxo_exchanges():
    """List exchanges (reference data)."""
    try:
        client = get_saxo()
        data = await client.ref_exchanges()
        return {"exchanges": data}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/charts/debug")
async def saxo_charts_debug(
    uic: int = Query(21, alias="Uic", description="e.g. 21 for EUR/USD"),
    asset_type: str = Query("FxSpot", alias="AssetType"),
    horizon: int = Query(60, alias="Horizon", ge=1, le=43200),
    count: int = Query(24, alias="Count", ge=1, le=500),
    path: str = Query("chart/v3/charts", description="chart/v3/charts or chart/v1/charts"),
):
    """Return raw Saxo chart API response for debugging (no parsing)."""
    try:
        client = get_saxo()
        raw = await client.chart_data_raw(uic, asset_type, horizon_minutes=horizon, count=count, path=path)
        return raw
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/charts")
async def saxo_charts(
    uic: int = Query(..., alias="Uic"),
    asset_type: str = Query(..., alias="AssetType"),
    horizon: int = Query(60, alias="Horizon", ge=1, le=43200),
    count: int = Query(100, ge=1, le=500),
):
    """Get historical chart data (OHLC). Horizon in minutes."""
    try:
        client = get_saxo()
        data = await client.chart_data(uic, asset_type, horizon_minutes=horizon, count=count)
        return {"data": data}
    except SaxoClientError as e:
        detail = _SAXO_403_MESSAGE if e.status_code == 403 else str(e)
        raise HTTPException(e.status_code or 502, detail=detail)


@router.get("/infoprices")
async def saxo_infoprices(
    uic: int = Query(..., alias="Uic"),
    asset_type: str = Query(..., alias="AssetType"),
):
    """Get current quote (bid/ask) for instrument."""
    try:
        client = get_saxo()
        data = await client.infoprices(uic, asset_type)
        return data
    except SaxoClientError as e:
        detail = _SAXO_403_MESSAGE if e.status_code == 403 else str(e)
        raise HTTPException(e.status_code or 502, detail=detail)

# --- Portfolio (Phase 2) ---

@router.get("/balances")
async def saxo_balances(account_key: str | None = Query(None, alias="accountKey")):
    """Get balances for the authenticated user."""
    try:
        client = get_saxo()
        params = {}
        if account_key:
            params["AccountKey"] = account_key
        data = await client.port_balances(params or None)
        return {"balances": data}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/positions")
async def saxo_positions(account_key: str | None = Query(None, alias="accountKey")):
    """Get positions for the authenticated user."""
    try:
        client = get_saxo()
        params = {}
        if account_key:
            params["AccountKey"] = account_key
        data = await client.port_positions(params or None)
        return {"positions": data}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/clients")
async def saxo_clients():
    """Get clients under the authenticated user (port/v1/clients)."""
    try:
        client = get_saxo()
        data = await client.port_clients()
        return {"clients": data}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/accounts")
async def saxo_accounts(
    client_key: str | None = Query(None, alias="clientKey"),
    include_sub_accounts: bool | None = Query(None, alias="includeSubAccounts"),
):
    """List accounts for the authenticated user (port/v1/accounts/me)."""
    try:
        client = get_saxo()
        params: dict[str, str | bool] = {}
        if client_key:
            params["ClientKey"] = client_key
        if include_sub_accounts is not None:
            params["IncludeSubAccounts"] = include_sub_accounts
        data = await client.port_accounts_me(params or None)
        return {"accounts": data}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.get("/accounts/{account_key}")
async def saxo_account(account_key: str):
    """Get a single account by AccountKey."""
    try:
        client = get_saxo()
        data = await client.port_account(account_key)
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.patch("/accounts/{account_key}")
async def saxo_update_account(account_key: str, body: dict):
    """Update account (e.g. DisplayName)."""
    try:
        client = get_saxo()
        data = await client.port_update_account(account_key, body)
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.post("/accounts")
async def saxo_create_account(
    body: dict | None = None,
    choice_of_account: str | int | None = Query(None, alias="choiceOfAccount"),
):
    """Create an additional account for your client (same login). Uses Saxo cm/v2/accounts. Sends ClientKey and ChoiceOfAccount at root (Saxo expects Path '')."""
    try:
        client = get_saxo()
        payload = dict(body or {})
        client_key = payload.get("ClientKey")
        if not client_key:
            me = await client.get_me()
            client_key = me.get("ClientKey")
            if not client_key:
                raise HTTPException(
                    400,
                    detail="Could not determine ClientKey (port/v1/users/me did not return it). Pass ClientKey in the request body.",
                )
        choice_val = payload.get("ChoiceOfAccount") or payload.get("choiceOfAccount")
        if choice_val is None and choice_of_account is not None:
            choice_val = choice_of_account
        if choice_val is None:
            choice_val = 0
        request_body = {
            "ClientKey": client_key,
            "ChoiceOfAccount": choice_val,
        }
        data = await client.cm_create_account(body=request_body)
        return data
    except SaxoClientError as e:
        detail = str(e)
        if e.body and isinstance(e.body, dict):
            msg = (
                e.body.get("Message")
                or e.body.get("message")
                or e.body.get("error_description")
            )
            if msg:
                detail = f"{detail}; {msg}"
            if e.status_code == 400:
                detail = f"{detail} Body: {json.dumps(e.body)}"
        if e.status_code == 404:
            detail = (
                "Create account API (cm/v2/accounts) is not available for your Saxo "
                "environment or app (404). Open additional accounts at saxobank.com or in the Saxo app."
            )
        raise HTTPException(e.status_code or 502, detail=detail)


# --- Trading (Phase 2) ---

@router.get("/orders")
async def saxo_orders(account_key: str | None = Query(None, alias="accountKey")):
    """Get open orders."""
    try:
        client = get_saxo()
        params = {}
        if account_key:
            params["AccountKey"] = account_key
        data = await client.trade_orders(params or None)
        return {"orders": data}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.post("/orders")
async def saxo_place_order(body: dict):
    """Place an order."""
    try:
        client = get_saxo()
        data = await client.trade_order(body)
        return data
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))


@router.delete("/orders/{order_id}")
async def saxo_cancel_order(order_id: str):
    """Cancel an order."""
    try:
        client = get_saxo()
        await client.trade_cancel_order(order_id)
        return {"status": "ok"}
    except SaxoClientError as e:
        raise HTTPException(e.status_code or 502, detail=str(e))
