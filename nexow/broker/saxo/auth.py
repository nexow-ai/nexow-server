"""Saxo Bank OpenAPI — OAuth2 token exchange and refresh."""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from nexow.config import settings

logger = structlog.get_logger(__name__)

TOKEN_PATH = "/token"
AUTHORIZE_PATH = "/authorize"


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}"
    return base64.b64encode(raw.encode()).decode()


async def exchange_code_for_tokens(
    code: str,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """Exchange authorization code for access_token and refresh_token."""
    uri = redirect_uri or settings.saxo_redirect_uri
    if not uri:
        raise ValueError("redirect_uri is required (or set SAXO_REDIRECT_URI)")
    token_url = f"{settings.saxo_auth_url.rstrip('/')}{TOKEN_PATH}"
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": uri,
    }
    auth_header = _basic_auth(settings.saxo_app_key, settings.saxo_app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data=body,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30.0,
        )
    resp.raise_for_status()
    data = resp.json()
    logger.info("saxo_tokens_exchanged", has_refresh="refresh_token" in data)
    return data


async def refresh_access_token(
    refresh_token: str,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """Exchange refresh_token for new access_token and refresh_token."""
    uri = redirect_uri or settings.saxo_redirect_uri
    if not uri:
        raise ValueError("redirect_uri is required (or set SAXO_REDIRECT_URI)")
    token_url = f"{settings.saxo_auth_url.rstrip('/')}{TOKEN_PATH}"
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": uri,
    }
    auth_header = _basic_auth(settings.saxo_app_key, settings.saxo_app_secret)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data=body,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30.0,
        )
    resp.raise_for_status()
    data = resp.json()
    logger.info("saxo_token_refreshed")
    return data


def authorization_url(state: str, redirect_uri: str | None = None) -> str:
    """Build the authorization URL to redirect the user to Saxo login."""
    uri = redirect_uri or settings.saxo_redirect_uri
    if not uri:
        raise ValueError("redirect_uri is required (or set SAXO_REDIRECT_URI)")
    base = settings.saxo_auth_url.rstrip("/")
    params = "&".join(
        [
            "response_type=code",
            f"client_id={settings.saxo_app_key}",
            f"state={quote(state, safe='')}",
            f"redirect_uri={quote(uri, safe='')}",
        ]
    )
    return f"{base}{AUTHORIZE_PATH}?{params}"
