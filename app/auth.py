"""Tesla OAuth 2.0 (Fleet API) helpers for the "Sign in with Tesla" flow.

Full OAuth requires a Tesla developer application (client id/secret) registered
at https://developer.tesla.com. When those credentials are not configured the
dashboard falls back to the access-token paste flow, which works immediately
with a token obtained from a tool such as `tesla_auth`.
"""
from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx

from .config import get_settings

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"


def oauth_configured() -> bool:
    s = get_settings()
    return bool(s.tesla_client_id and s.tesla_client_secret)


def authorize_url(state: str | None = None) -> tuple[str, str]:
    """Return (url, state) for the authorization-code redirect."""
    s = get_settings()
    state = state or secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": s.tesla_client_id,
        "redirect_uri": s.tesla_redirect_uri,
        "scope": s.tesla_oauth_scope,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", state


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for access/refresh tokens."""
    s = get_settings()
    payload = {
        "grant_type": "authorization_code",
        "client_id": s.tesla_client_id,
        "client_secret": s.tesla_client_secret,
        "code": code,
        "redirect_uri": s.tesla_redirect_uri,
        "audience": s.tesla_oauth_audience,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(TOKEN_URL, data=payload)
        resp.raise_for_status()
        return resp.json()


def refresh_tokens(refresh_token: str) -> dict:
    s = get_settings()
    payload = {
        "grant_type": "refresh_token",
        "client_id": s.tesla_client_id,
        "refresh_token": refresh_token,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(TOKEN_URL, data=payload)
        resp.raise_for_status()
        return resp.json()
