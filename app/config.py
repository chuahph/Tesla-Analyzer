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
    tesla_oauth_scope: str = (
        "openid offline_access vehicle_device_data vehicle_location vehicle_cmds"
    )
    tesla_oauth_audience: str = "https://fleet-api.prd.na.vn.cloud.tesla.com"

    # Optional passcode protecting the whole app (empty = no login required).
    # Set APP_PASSCODE on a public host so only you can open the dashboard.
    app_passcode: str = ""

    # Optional secret that lets an external cron service trigger /api/sync
    # (as ?key=...) without the passcode cookie, for hands-off logging.
    sync_key: str = ""
    # Optional URL that GET /api/backup (also cron-callable via sync_key)
    # POSTs a full-history export ZIP to. Empty = the endpoint is disabled.
    # Point it at your own upload endpoint, a cloud-storage presigned PUT
    # URL, or a relay that forwards to email/Slack/Discord.
    backup_webhook_url: str = ""

    # Analysis parameters
    energy_price_per_kwh: float = 0.90
    # Optional time-of-use pricing: when both are set (> 0), driving/charging
    # cost uses the peak or off-peak rate for each timestamp instead of the
    # flat price above. 0 = disabled (flat rate everywhere).
    energy_price_peak_kwh: float = 0.0
    energy_price_offpeak_kwh: float = 0.0
    tariff_peak_start_hour: int = 8
    tariff_peak_end_hour: int = 22
    # Malaysian residential TOU tariffs (e.g. TNB) typically treat the whole
    # weekend as off-peak regardless of hour.
    tariff_weekend_offpeak: bool = True
    currency: str = "RM"
    rated_wh_per_km: float = 150.0
    # When-new full range (km) used as the battery-health 100% reference.
    # 0 = auto-detect from the car's variant badge (e.g. 74D -> 549 km).
    battery_new_range_km: float = 0.0
    # Usable pack capacity (kWh), the 100%->0% energy that turns a drive's
    # range/SoC delta into kWh. 0 = auto: the measured charge EMA, seeded from
    # the car's variant spec. Set this to your car's known usable figure (e.g.
    # a Long Range Model 3 is ~78 kWh new) to override, if the auto value looks
    # off against the car's own energy screen.
    battery_capacity_kwh: float = 0.0

    @property
    def demo_mode(self) -> bool:
        return not self.tesla_access_token.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
