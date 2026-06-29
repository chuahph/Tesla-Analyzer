"""Thin Tesla Owner/Fleet API client.

Only the read-only endpoints needed for analytics are implemented. When no
access token is configured the app never instantiates this client and uses
generated sample data instead (see ``sample_data.py``).
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings


class TeslaClient:
    def __init__(self, access_token: str | None = None, base_url: str | None = None):
        settings = get_settings()
        self.access_token = access_token or settings.tesla_access_token
        self.base_url = (base_url or settings.tesla_api_base_url).rstrip("/")
        if not self.access_token:
            raise ValueError("TESLA_ACCESS_TOKEN is required for live mode")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json().get("response", {})

    def list_vehicles(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{self.base_url}/api/1/vehicles", headers=self._headers()
            )
            resp.raise_for_status()
            return resp.json().get("response", [])

    def vehicle_data(self, vehicle_id: str | int) -> dict[str, Any]:
        """Full snapshot: charge_state, drive_state, climate_state, vehicle_state."""
        return self._get(f"/api/1/vehicles/{vehicle_id}/vehicle_data")
