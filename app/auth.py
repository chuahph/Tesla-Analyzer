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


def authorize_url(redirect_uri: str, state: str | None = None) -> tuple[str, str]:
    """Return (url, state) for the authorization-code redirect."""
    s = get_settings()
    state = state or secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": s.tesla_client_id,
        "redirect_uri": redirect_uri,
        "scope": s.tesla_oauth_scope,
        "state": state,
        # Without this Tesla silently reuses the user's previous consent, so a
        # newly added scope (e.g. vehicle_location) is never actually granted.
        "prompt_missing_scopes": "true",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", state


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for access/refresh tokens."""
    s = get_settings()
    payload = {
        "grant_type": "authorization_code",
        "client_id": s.tesla_client_id,
        "client_secret": s.tesla_client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
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
    if s.tesla_client_secret:
        payload["client_secret"] = s.tesla_client_secret
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(TOKEN_URL, data=payload)
        resp.raise_for_status()
        return resp.json()


def partner_token() -> str:
    """Machine token for partner-level calls (client_credentials grant)."""
    s = get_settings()
    scope = " ".join(
        p for p in s.tesla_oauth_scope.split() if p != "offline_access"
    )
    payload = {
        "grant_type": "client_credentials",
        "client_id": s.tesla_client_id,
        "client_secret": s.tesla_client_secret,
        "scope": scope,
        "audience": s.tesla_oauth_audience,
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(TOKEN_URL, data=payload)
        resp.raise_for_status()
        return resp.json()["access_token"]


def register_partner(domain: str) -> dict:
    """Register this app's domain with Tesla (one-time Fleet API requirement).

    Tesla fetches https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem
    during this call, which the app serves itself.
    """
    s = get_settings()
    token = partner_token()
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{s.tesla_oauth_audience}/api/1/partner_accounts",
            json={"domain": domain},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()
