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
    # vehicle_location powers trip start/end places and live speed; the sync
    # degrades gracefully (403 fallback) if the Tesla app doesn't grant it.
    tesla_oauth_scope: str = "openid offline_access vehicle_device_data vehicle_location"
    tesla_oauth_audience: str = "https://fleet-api.prd.na.vn.cloud.tesla.com"

    # Optional passcode protecting the whole app (empty = no login required).
    # Set APP_PASSCODE on a public host so only you can open the dashboard.
    app_passcode: str = ""

    # Optional secret that lets an external cron service trigger /api/sync
    # (as ?key=...) without the passcode cookie, for hands-off logging.
    sync_key: str = ""

    # Analysis parameters
    energy_price_per_kwh: float = 0.90
    currency: str = "RM"
    rated_wh_per_km: float = 150.0
    # When-new full range (km) used as the battery-health 100% reference.
    # 0 = auto-detect from the car's variant badge (e.g. 74D -> 549 km).
    battery_new_range_km: float = 0.0

    @property
    def demo_mode(self) -> bool:
        return not self.tesla_access_token.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
