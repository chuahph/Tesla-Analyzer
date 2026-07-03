"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    database_url: str = "sqlite:///./data/tesla_analyzer.db"

    # Tesla API credentials. When the access token is empty the app runs in
    # DEMO mode and serves generated sample data instead of calling Tesla.
    tesla_access_token: str = ""
    tesla_refresh_token: str = ""
    tesla_api_base_url: str = "https://owner-api.teslamotors.com"
    poll_interval_seconds: int = 60

    # Tesla OAuth (Fleet API). Required only for the "Sign in with Tesla" button;
    # the access-token paste flow and demo/import modes do not need these.
    tesla_client_id: str = ""
    tesla_client_secret: str = ""
    tesla_redirect_uri: str = "http://localhost:8000/api/link/oauth/callback"
    tesla_oauth_scope: str = "openid offline_access vehicle_device_data"
    tesla_oauth_audience: str = "https://fleet-api.prd.na.vn.cloud.tesla.com"

    # Optional passcode protecting the whole app (empty = no login required).
    # Set APP_PASSCODE on a public host so only you can open the dashboard.
    app_passcode: str = ""

    # Analysis parameters
    energy_price_per_kwh: float = 0.90
    currency: str = "RM"
    rated_wh_per_km: float = 150.0

    @property
    def demo_mode(self) -> bool:
        return not self.tesla_access_token.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
