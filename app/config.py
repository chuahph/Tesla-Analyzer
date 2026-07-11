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
    # How often /api/sync (the Render/cron path — not the standalone
    # collector above) actually calls Tesla's vehicle_data() for an
    # online-but-idle car, in minutes. Calling /api/sync itself more often
    # than this (an external cron every 1 min is normal and recommended)
    # does NOT force more frequent reads — the endpoint decides for itself,
    # separately from how often it's hit, since a read is itself an
    # activity signal that resets the car's own sleep timer. Lower = more
    # up-to-date data and fewer session-reconstruction gaps, at the cost of
    # more Tesla API calls and a mildly harder time falling asleep on its
    # own. 1.0 matches the cadence this project's own setup guide
    # recommends for the external cron, so a real read happens on (close
    # to) every tick by default.
    sync_poll_interval_min: float = 1.0
    # Minimum odometer movement (km) treated as a real trip rather than
    # jitter — a car nudged while parked, GPS drift, a multi-point turn.
    # Lower it to catch genuinely short moves (e.g. a charger-to-parking-spot
    # shuffle) as logged trips; the trade-off is more exposure to logging a
    # non-trip as a tiny phantom drive. See DRIVE_MIN_KM in app/sync.py.
    drive_min_km: float = 0.1

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
    # Optional URL that GET /api/reports/monthly (also cron-callable via
    # sync_key) POSTs a driving/charging/efficiency summary JSON to (includes
    # a "text" field a Slack/Discord incoming webhook reads directly). Empty
    # = the endpoint is disabled. No internal scheduling — same philosophy as
    # backup_webhook_url: call it on whatever cadence you want the report at
    # (monthly is the intended use, but the endpoint itself is period-agnostic).
    report_webhook_url: str = ""
    # Optional generic event webhook: POSTs a small JSON payload
    # ({event, title, body, timestamp}) for charge-complete, low-battery and
    # drive-complete events — for home automation (Home Assistant, IFTTT,
    # Zapier, n8n, ...) to react to, independent of web push (works even
    # without VAPID configured, and vice versa). Empty = disabled.
    event_webhook_url: str = ""

    # Web push notifications (charge complete, low battery, ...). Generate a
    # keypair once with `python -m app.push_keys` and set both here — empty
    # (either) disables notifications entirely; the subscribe UI stays
    # hidden and /api/push/* returns 404. VAPID requires a contact address
    # (never emailed to you — it's only what a push service could use to
    # reach the app operator if a subscription misbehaves).
    vapid_private_key_pem: str = ""
    vapid_public_key_pem: str = ""
    vapid_subject_email: str = "admin@example.com"
    # Notify when SoC drops to/below this on a synced reading. 0 = disabled.
    low_soc_notify_pct: float = 0.0

    # Analysis parameters
    energy_price_per_kwh: float = 0.90
    # Charging cost by charger type — real bills usually differ far more
    # between AC and DC fast charging than time-of-day does, so these take
    # priority over the flat/ToU rate below for completed charging sessions.
    # Set either to 0 to fall back to flat/ToU pricing for that type instead.
    # Defaults are typical Malaysian *public* charging rates (2026): public
    # AC ≈ RM1.00/kWh (Gentari/JomCharge/ChargEV), public DC ≈ RM1.50/kWh
    # (JomCharge ~1.40, Gentari 1.60–1.80). Set AC lower (e.g. 0.57) if you
    # mostly charge at home on the TNB residential tariff.
    energy_price_ac_kwh: float = 1.00
    energy_price_dc_kwh: float = 1.50
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
    # Petrol-comparison (TCO) inputs: what an equivalent petrol car would have
    # cost to run the same distance, at this price per litre and consumption.
    # Either at 0 disables the comparison entirely (hidden, not a false $0).
    petrol_price_per_liter: float = 0.0
    petrol_l_per_100km: float = 0.0
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
