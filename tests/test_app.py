"""App-level tests: passcode gate boundaries and the Tesla partner key path."""
import json
import pytest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.main import app

PEM_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"


@pytest.fixture(autouse=True)
def _db_ready():
    """Create the schema before every test. Tests that drive the app through
    TestClient get this from its startup hook, but the ones that reach for
    SessionLocal directly (the alert tests) don't — so without this they pass
    only when some earlier test in the file happened to build the tables, and
    fail when run alone or first. Mirrors test_multicar.py."""
    from app.database import init_db

    init_db()
    yield


def test_open_paths_with_passcode_set():
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = "secret123"
    try:
        with TestClient(app) as client:
            # Tesla must reach the partner key; hosts must reach health.
            pem = client.get(PEM_PATH)
            assert pem.status_code == 200
            assert "BEGIN PUBLIC KEY" in pem.text
            assert client.get("/api/health").status_code == 200
            # Everything else stays locked.
            assert client.get("/api/summary").status_code == 401
            resp = client.get("/", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/login"
            # Correct passcode unlocks.
            login = client.post(
                "/login", data={"passcode": "secret123"}, follow_redirects=False
            )
            assert login.status_code == 303
            assert client.get("/").status_code == 200
    finally:
        settings.app_passcode = old


def test_linked_vehicle_preferred_and_demo_purged(seeded):
    from app import services, state
    from app.api.routes import _first_vehicle
    from app.models import Vehicle

    # Demo data exists; a real linked vehicle arrives.
    real = Vehicle(vin="LRW3F7EK3RC309372", name="My Model 3", model="Model 3")
    seeded.add(real)
    seeded.commit()
    state.put(seeded, state.LINKED_VIN_KEY, real.vin)

    assert _first_vehicle(seeded).vin == real.vin  # linked wins over demo

    services.purge_demo(seeded)
    vins = [v.vin for v in seeded.query(Vehicle).all()]
    assert vins == [real.vin]  # demo vehicle and its data are gone


def test_sync_key_lets_cron_through_the_gate():
    settings = get_settings()
    old_pc, old_sk = settings.app_passcode, settings.sync_key
    settings.app_passcode = "secret123"
    settings.sync_key = "cron-key-42"
    try:
        with TestClient(app) as client:
            # No key / wrong key -> locked.
            assert client.get("/api/sync").status_code == 401
            assert client.get("/api/sync?key=nope").status_code == 401
            assert client.post("/api/sync").status_code == 401
            # Correct key passes the gate (400 = reached the endpoint, no
            # linked account in the test database).
            resp = client.get("/api/sync?key=cron-key-42")
            assert resp.status_code == 400
            assert "link" in resp.json()["detail"].lower()
            # The key opens /api/sync and /api/backup (both cron-callable), not
            # arbitrary other endpoints.
            assert client.get("/api/summary?key=cron-key-42").status_code == 401
            assert client.get("/api/export/csv?key=cron-key-42").status_code == 401
            # 400 here means it passed the gate and reached the endpoint (no
            # BACKUP_WEBHOOK_URL configured in this test).
            assert client.get("/api/backup?key=cron-key-42").status_code == 400
            # Alerts check is also cron-key callable, via GET or POST; the key
            # opens the gate and the endpoint returns 200 (nothing to alert on
            # in the empty test database).
            assert client.get("/api/alerts/check").status_code == 401
            assert client.get("/api/alerts/check?key=cron-key-42").status_code == 200
            assert client.post("/api/alerts/check?key=cron-key-42").status_code == 200
    finally:
        settings.app_passcode, settings.sync_key = old_pc, old_sk


def test_backup_requires_webhook_url_and_posts_the_export():
    settings = get_settings()
    old_pc, old_url = settings.app_passcode, settings.backup_webhook_url
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            # No webhook configured -> a clear 400, not a silent no-op.
            settings.backup_webhook_url = ""
            resp = client.get("/api/backup")
            assert resp.status_code == 400
            assert "BACKUP_WEBHOOK_URL" in resp.json()["detail"]

            # Configured -> POSTs the export ZIP to it.
            settings.backup_webhook_url = "https://example.invalid/upload"
            sent = {}

            def fake_post(url, content=None, headers=None, timeout=None):
                sent["url"], sent["content"], sent["headers"] = url, content, headers
                import httpx as _httpx
                return _httpx.Response(200, request=_httpx.Request("POST", url))

            import app.api.routes as routes_mod
            orig_post = routes_mod.httpx.post
            routes_mod.httpx.post = fake_post
            try:
                resp = client.get("/api/backup")
            finally:
                routes_mod.httpx.post = orig_post

            assert resp.status_code == 200
            body = resp.json()
            assert body["sent"] is True
            assert body["bytes"] > 0
            assert sent["url"] == "https://example.invalid/upload"
            assert sent["headers"]["Content-Type"] == "application/zip"
            # The posted bytes are a real, re-importable export.
            from app.importer import parse_upload
            drives, charges = parse_upload("backup.zip", sent["content"])
            assert len(drives) == body["drives"]
            assert len(charges) == body["charges"]
    finally:
        settings.app_passcode, settings.backup_webhook_url = old_pc, old_url


def test_backup_surfaces_webhook_delivery_failure():
    settings = get_settings()
    old_pc, old_url = settings.app_passcode, settings.backup_webhook_url
    settings.app_passcode = ""
    settings.backup_webhook_url = "https://example.invalid/upload"
    try:
        with TestClient(app) as client:  # startup seeds demo data
            def failing_post(url, content=None, headers=None, timeout=None):
                import httpx as _httpx
                raise _httpx.ConnectError("connection refused", request=_httpx.Request("POST", url))

            import app.api.routes as routes_mod
            orig_post = routes_mod.httpx.post
            routes_mod.httpx.post = failing_post
            try:
                resp = client.get("/api/backup")
            finally:
                routes_mod.httpx.post = orig_post

            assert resp.status_code == 502
            assert "webhook" in resp.json()["detail"].lower()
    finally:
        settings.app_passcode, settings.backup_webhook_url = old_pc, old_url


def test_monthly_report_requires_webhook_url_and_posts_summary():
    settings = get_settings()
    old_pc, old_url = settings.app_passcode, settings.report_webhook_url
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            # No webhook configured -> a clear 400, not a silent no-op.
            settings.report_webhook_url = ""
            resp = client.get("/api/reports/monthly")
            assert resp.status_code == 400
            assert "REPORT_WEBHOOK_URL" in resp.json()["detail"]

            # Configured -> POSTs a JSON summary to it.
            settings.report_webhook_url = "https://example.invalid/report"
            sent = {}

            def fake_post(url, json=None, timeout=None):
                sent["url"], sent["json"] = url, json
                import httpx as _httpx
                return _httpx.Response(200, request=_httpx.Request("POST", url))

            import app.api.routes as routes_mod
            orig_post = routes_mod.httpx.post
            routes_mod.httpx.post = fake_post
            try:
                resp = client.get("/api/reports/monthly?days=30")
            finally:
                routes_mod.httpx.post = orig_post

            assert resp.status_code == 200
            body = resp.json()
            assert body["sent"] is True and body["period_days"] == 30
            assert sent["url"] == "https://example.invalid/report"
            payload = sent["json"]
            assert payload["period_days"] == 30
            assert "text" in payload and isinstance(payload["text"], str)
            assert "km" in payload["text"]  # demo data has drives in the last 30 days
            assert payload["driving"]["available"] is True
            assert payload["charging"]["available"] in (True, False)
    finally:
        settings.app_passcode, settings.report_webhook_url = old_pc, old_url


def test_monthly_report_surfaces_webhook_delivery_failure():
    settings = get_settings()
    old_pc, old_url = settings.app_passcode, settings.report_webhook_url
    settings.app_passcode = ""
    settings.report_webhook_url = "https://example.invalid/report"
    try:
        with TestClient(app) as client:  # startup seeds demo data
            def failing_post(url, json=None, timeout=None):
                import httpx as _httpx
                raise _httpx.ConnectError("connection refused", request=_httpx.Request("POST", url))

            import app.api.routes as routes_mod
            orig_post = routes_mod.httpx.post
            routes_mod.httpx.post = failing_post
            try:
                resp = client.get("/api/reports/monthly")
            finally:
                routes_mod.httpx.post = orig_post

            assert resp.status_code == 502
            assert "webhook" in resp.json()["detail"].lower()
    finally:
        settings.app_passcode, settings.report_webhook_url = old_pc, old_url


def test_monthly_report_cron_callable_via_sync_key():
    """Same passcode-bypass mechanism as /api/sync and /api/backup."""
    settings = get_settings()
    old_pc, old_key, old_url = settings.app_passcode, settings.sync_key, settings.report_webhook_url
    settings.app_passcode = "secret123"
    settings.sync_key = "crontoken"
    settings.report_webhook_url = "https://example.invalid/report"
    try:
        with TestClient(app) as client:
            # No key -> blocked by the passcode gate.
            assert client.get("/api/reports/monthly").status_code == 401

            def fake_post(url, json=None, timeout=None):
                import httpx as _httpx
                return _httpx.Response(200, request=_httpx.Request("POST", url))

            import app.api.routes as routes_mod
            orig_post = routes_mod.httpx.post
            routes_mod.httpx.post = fake_post
            try:
                resp = client.get("/api/reports/monthly?key=crontoken")
            finally:
                routes_mod.httpx.post = orig_post
            assert resp.status_code == 200
    finally:
        settings.app_passcode, settings.sync_key, settings.report_webhook_url = old_pc, old_key, old_url


def test_push_endpoints_404_when_not_configured():
    settings = get_settings()
    old_pc = settings.app_passcode
    old_priv, old_pub = settings.vapid_private_key_pem, settings.vapid_public_key_pem
    settings.app_passcode = ""
    settings.vapid_private_key_pem = settings.vapid_public_key_pem = ""
    try:
        with TestClient(app) as client:
            assert client.get("/api/push/vapid-public-key").status_code == 404
            resp = client.post("/api/push/subscribe", json={
                "endpoint": "https://push.example.com/x",
                "keys": {"p256dh": "a", "auth": "b"},
            })
            assert resp.status_code == 404
            # The test-notification endpoint is likewise a 404 when no channel
            # at all is configured (nothing to deliver to). It is deliberately
            # not gated on push alone — Telegram and the webhook keep it alive.
            assert client.post("/api/push/test").status_code == 404
            assert client.get("/api/push/test").status_code == 404
    finally:
        settings.app_passcode = old_pc
        settings.vapid_private_key_pem, settings.vapid_public_key_pem = old_priv, old_pub


def test_push_subscribe_and_unsubscribe_round_trip():
    from webpush.vapid import VAPID

    from app.database import SessionLocal
    from app.models import PushSubscription

    settings = get_settings()
    old_pc = settings.app_passcode
    old_priv, old_pub = settings.vapid_private_key_pem, settings.vapid_public_key_pem
    settings.app_passcode = ""
    priv, pub, appkey = VAPID.generate_keys()
    settings.vapid_private_key_pem = priv.decode().strip().replace("\n", "\\n")
    settings.vapid_public_key_pem = pub.decode().strip().replace("\n", "\\n")
    try:
        with TestClient(app) as client:
            resp = client.get("/api/push/vapid-public-key")
            assert resp.status_code == 200
            assert resp.json()["key"] == appkey

            sub_body = {
                "endpoint": "https://push.example.com/round-trip",
                "keys": {"p256dh": "fake-p256dh", "auth": "fake-auth"},
            }
            assert client.post("/api/push/subscribe", json=sub_body).status_code == 200
            with SessionLocal() as s:
                assert s.query(PushSubscription).filter(
                    PushSubscription.endpoint == sub_body["endpoint"]).count() == 1

            # Malformed payload -> 400, not a silent no-op.
            resp = client.post("/api/push/subscribe", json={"endpoint": "https://x"})
            assert resp.status_code == 400

            assert client.post("/api/push/unsubscribe", json={
                "endpoint": sub_body["endpoint"]}).status_code == 200
            with SessionLocal() as s:
                assert s.query(PushSubscription).filter(
                    PushSubscription.endpoint == sub_body["endpoint"]).count() == 0
    finally:
        settings.app_passcode = old_pc
        settings.vapid_private_key_pem, settings.vapid_public_key_pem = old_priv, old_pub
        from app.database import SessionLocal as SL
        from app.models import PushSubscription as PS
        with SL() as s:
            s.query(PS).delete()
            s.commit()


def test_health_reports_build_info():
    with TestClient(app) as client:
        body = client.get("/api/health").json()
        assert "build" in body
        assert set(body["build"]) == {"sha", "time"}


def test_summary_since_charge_window():
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            full = client.get("/api/summary?days=365").json()
            since = client.get("/api/summary?days=365&since_charge=1").json()
            assert since["window_label"] == "since last charge"
            assert "battery" in full  # health section always present
            # The window starts at the last charge, so it holds a subset of drives
            # and no completed charging sessions from before it.
            full_drives = full["driving"].get("total_drives", 0)
            since_drives = since["driving"].get("total_drives", 0) if since["driving"]["available"] else 0
            assert since_drives <= full_drives
            since_charges = since["charging"].get("total_sessions", 0) if since["charging"]["available"] else 0
            assert since_charges <= 1  # at most a charge that started after the last one ended
            # The window's own boundary charge is otherwise invisible in every
            # list above (it ends right at "since"), so it's surfaced separately —
            # in every window, not just since_charge, so the format/context is
            # consistent regardless of which window is picked.
            lc = since["last_charge"]
            assert lc is not None
            assert set(lc) == {
                "id", "start_time", "end_time", "energy_added_kwh", "start_soc",
                "end_soc", "cost", "charge_type", "location", "location_raw",
                "rate_per_kwh", "is_free", "used_since_kwh", "source", "battery_kwh_at_end",
            }
            assert lc["used_since_kwh"] >= 0
            # Energy Charged/AC-DC Energy/Charging Cost (and Driving Cost,
            # gated on the same chg.available in the frontend) must still
            # populate in the since-charge view, based on that one boundary
            # charge — not go blank just because no NEW charge happened yet.
            assert since["charging"]["available"] is True
            assert since["charging"]["total_sessions"] == 1
            assert since["charging"]["total_energy_kwh"] == round(lc["energy_added_kwh"], 1)
            assert since["charging"]["total_cost"] == round(lc["cost"], 2)
            assert lc["end_time"] <= since["generated_at"]
            assert full["last_charge"] == lc  # same last charge regardless of window
    finally:
        settings.app_passcode = old


def test_last_charge_used_since_kwh_sums_drives_after_it_independent_of_window():
    """used_since_kwh (in last_charge_summary) is the kWh used after the
    last charge ended — computed fresh regardless of which window/days
    param the request happens to carry, same as last_charge itself."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            from app.database import SessionLocal
            from app.models import Charge, Drive, Vehicle

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-NETBATT", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                charge = Charge(
                    vehicle_id=v.id,
                    start_time=datetime(2025, 6, 1, 22, 0), end_time=datetime(2025, 6, 2, 2, 0),
                    duration_min=240, start_soc=40, end_soc=90, energy_added_kwh=30.0,
                    charge_type="AC", max_power_kw=7, location="Home", cost=27.0,
                )
                s.add(charge)
                s.commit()
                # Two drives after the charge, one before it (must be excluded).
                s.add(Drive(
                    vehicle_id=v.id,
                    start_time=datetime(2025, 6, 1, 10, 0), end_time=datetime(2025, 6, 1, 10, 30),
                    distance_km=10, duration_min=30, start_soc=50, end_soc=48,
                    energy_used_kwh=99.0, avg_speed_kmh=20, max_speed_kmh=40, outside_temp_c=28,
                ))
                s.add(Drive(
                    vehicle_id=v.id,
                    start_time=datetime(2025, 6, 3, 8, 0), end_time=datetime(2025, 6, 3, 8, 30),
                    distance_km=15, duration_min=30, start_soc=90, end_soc=85,
                    energy_used_kwh=5.5, avg_speed_kmh=30, max_speed_kmh=50, outside_temp_c=28,
                ))
                s.add(Drive(
                    vehicle_id=v.id,
                    start_time=datetime(2025, 6, 4, 8, 0), end_time=datetime(2025, 6, 4, 8, 30),
                    distance_km=12, duration_min=30, start_soc=85, end_soc=81,
                    energy_used_kwh=4.5, avg_speed_kmh=30, max_speed_kmh=50, outside_temp_c=28,
                ))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-NETBATT"})
            try:
                body_wide = client.get("/api/summary?days=365").json()
                body_narrow = client.get("/api/summary?days=1").json()
                assert body_wide["last_charge"]["used_since_kwh"] == 10.0    # 5.5 + 4.5, not 99
                assert body_narrow["last_charge"]["used_since_kwh"] == 10.0  # same regardless of window
            finally:
                # This vehicle and its rows persist in the shared test DB —
                # restore the active pointer and delete them so later tests
                # (e.g. clear-drives, which isn't vehicle-scoped) aren't
                # thrown off by an extra car's data left behind.
                client.post("/api/active-vehicle", json={"vin": "DEMO0SAMPLE0000001"})
                with SessionLocal() as s:
                    s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                    s.query(Charge).filter(Charge.vehicle_id == v.id).delete()
                    s.query(Vehicle).filter(Vehicle.id == v.id).delete()
                    s.commit()
    finally:
        settings.app_passcode = old


def test_summary_reports_battery_balance():
    """battery_balance always reports the window's raw kWh used; % is only
    included for the since-charge window (a plain days-based window can span
    several charge/discharge cycles, with no single well-defined "used out
    of how much" to divide by) and is computed against the full
    degradation-adjusted pack (full_charge_kwh) — the same basis as every
    other %-of-battery figure in the app, so they're directly comparable."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            body = client.get("/api/summary?days=365").json()
            bal = body["battery_balance"]
            assert set(bal) == {
                "full_charge_kwh", "charged_kwh", "used_kwh", "used_pct", "current_soc_pct",
                "trip_kwh", "vampire_kwh", "vampire_hours", "vampire_gaps",
                "vampire_longest_hours", "vampire_longest_start", "vampire_longest_end",
                "vampire_longest_inducer",
            }
            assert bal["charged_kwh"] >= 0
            assert bal["used_kwh"] >= 0
            assert bal["full_charge_kwh"] > 0
            assert bal["used_pct"] is None
            # trip_kwh + vampire_kwh always sums back to used_kwh exactly.
            assert round(bal["trip_kwh"] + bal["vampire_kwh"], 1) == round(bal["used_kwh"], 1)
            if bal["current_soc_pct"] is not None:
                assert 0 <= bal["current_soc_pct"] <= 100
            # The single longest gap is present iff there was at least one.
            if bal["vampire_gaps"] > 0:
                assert bal["vampire_longest_hours"] is not None
                assert bal["vampire_longest_hours"] <= bal["vampire_hours"]
                assert bal["vampire_longest_start"] is not None
                assert bal["vampire_longest_end"] is not None
            else:
                assert bal["vampire_longest_hours"] is None
                assert bal["vampire_longest_inducer"] is None

            since_body = client.get("/api/summary?since_charge=true").json()
            since_bal = since_body["battery_balance"]
            if since_bal["full_charge_kwh"] > 0:
                assert since_bal["used_pct"] is not None
                assert round(since_bal["used_pct"], 1) == round(
                    since_bal["used_kwh"] / since_bal["full_charge_kwh"] * 100.0, 1)
    finally:
        settings.app_passcode = old


def test_since_charge_battery_used_anchors_to_actual_soc_not_bottom_up_estimate():
    """Reported live: "Balance 89% batt and used 11.7%, total exceed 100%".
    The since-charge window's used_kwh/used_pct must equal the real drop
    (last charge's end SoC minus the latest reading), not the bottom-up sum
    of each trip's own max(measured kWh, integer SoC drop) — that per-trip
    "take the larger" rule exists to rescue one trip with a range-reading
    gap from being undercounted, but always taking the larger is a
    one-directional bias that drifts the summed total past what the pack's
    own SoC already reports directly. Here every trip's measured kWh is
    deliberately made smaller than its own integer SoC drop, so the
    bottom-up total (were it still used) would overshoot the true 5%/3.5kWh
    drop implied by 100% -> 95%."""
    settings = get_settings()
    old_pc, old_cap = settings.app_passcode, settings.battery_capacity_kwh
    settings.app_passcode = ""
    settings.battery_capacity_kwh = 70.0
    try:
        with TestClient(app) as client:  # startup seeds demo data
            from app.database import SessionLocal
            from app.models import BatteryReading, Charge, Drive, Vehicle

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-SOCTRUTH", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                s.add(Charge(
                    vehicle_id=v.id,
                    start_time=datetime(2026, 7, 1, 7, 30), end_time=datetime(2026, 7, 1, 8, 0),
                    duration_min=30, start_soc=80, end_soc=100, energy_added_kwh=14.0,
                    charge_type="AC", max_power_kw=7, location="Home", cost=12.6,
                ))
                # Three back-to-back trips, boundaries touching (no measurable
                # idle drop between/before any of them, so vampire_kwh == 0)
                # and each trip's measured energy well under its own 2-point
                # SoC drop, forcing _trip_kwh()'s max() to pick the integer
                # estimate every time: bottom-up total = 3 * 1.4 = 4.2 kWh.
                for i, (start, end, ssoc, esoc) in enumerate([
                    (datetime(2026, 7, 1, 8, 10), datetime(2026, 7, 1, 8, 20), 100, 98),
                    (datetime(2026, 7, 1, 8, 30), datetime(2026, 7, 1, 8, 40), 98, 96),
                    (datetime(2026, 7, 1, 8, 50), datetime(2026, 7, 1, 9, 0), 96, 94),
                ]):
                    s.add(Drive(
                        vehicle_id=v.id, start_time=start, end_time=end,
                        distance_km=5, duration_min=10, start_soc=ssoc, end_soc=esoc,
                        energy_used_kwh=0.5, avg_speed_kmh=30, max_speed_kmh=50, outside_temp_c=28,
                    ))
                # The real, ground-truth current SoC: 100% -> 95% = 5% = 3.5
                # kWh of a 70 kWh pack — less than the 4.2 kWh bottom-up sum.
                s.add(BatteryReading(
                    vehicle_id=v.id, ts=datetime(2026, 7, 1, 9, 10),
                    soc=95, range_km=350.0, odo_km=1000.0,
                ))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-SOCTRUTH"})
            try:
                bal = client.get("/api/summary?since_charge=true").json()["battery_balance"]
                assert bal["current_soc_pct"] == 95.0
                assert bal["used_kwh"] == 3.5  # ground truth, not the 4.2 bottom-up sum
                assert bal["used_pct"] == 5.0
                assert round(bal["trip_kwh"] + bal["vampire_kwh"], 1) == round(bal["used_kwh"], 1)
                assert round(100.0 - bal["current_soc_pct"], 1) == bal["used_pct"]
            finally:
                client.post("/api/active-vehicle", json={"vin": "DEMO0SAMPLE0000001"})
                with SessionLocal() as s:
                    s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                    s.query(Charge).filter(Charge.vehicle_id == v.id).delete()
                    s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
                    s.query(Vehicle).filter(Vehicle.id == v.id).delete()
                    s.commit()
    finally:
        settings.app_passcode = old_pc
        settings.battery_capacity_kwh = old_cap


def test_since_charge_driving_cost_anchors_to_ground_truth_not_bottom_up_estimate():
    """Reported live: Driving Cost showed RM 0.246/km while Charging Cost
    showed RM 23.67/100km (RM 0.2367/km) from the very same charge — Driving
    Cost was still pricing driving_analysis.analyze()'s own bottom-up
    total_energy_used_kwh (the same one-directional max() bias as
    Battery Used, see the SOCTRUTH test above), not the ground-truth SoC
    delta. Same fixture shape: three trips whose measured kWh is
    deliberately under their own integer SoC drop, so the bottom-up total
    (4.2 kWh) overshoots the true 3.5 kWh drop — Driving Cost must be priced
    off the smaller, correct total instead."""
    settings = get_settings()
    old_pc, old_cap = settings.app_passcode, settings.battery_capacity_kwh
    settings.app_passcode = ""
    settings.battery_capacity_kwh = 70.0
    try:
        with TestClient(app) as client:  # startup seeds demo data
            from app.database import SessionLocal
            from app.models import BatteryReading, Charge, Drive, Vehicle

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-COSTTRUTH", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                # 12.6 / 14.0 = RM 0.9/kWh — the flat rate every since-charge
                # trip (and the recomputed Driving Cost) should be priced at.
                s.add(Charge(
                    vehicle_id=v.id,
                    start_time=datetime(2026, 7, 1, 7, 30), end_time=datetime(2026, 7, 1, 8, 0),
                    duration_min=30, start_soc=80, end_soc=100, energy_added_kwh=14.0,
                    charge_type="AC", max_power_kw=7, location="Home", cost=12.6,
                ))
                for start, end, ssoc, esoc in [
                    (datetime(2026, 7, 1, 8, 10), datetime(2026, 7, 1, 8, 20), 100, 98),
                    (datetime(2026, 7, 1, 8, 30), datetime(2026, 7, 1, 8, 40), 98, 96),
                    (datetime(2026, 7, 1, 8, 50), datetime(2026, 7, 1, 9, 0), 96, 94),
                ]:
                    s.add(Drive(
                        vehicle_id=v.id, start_time=start, end_time=end,
                        distance_km=5, duration_min=10, start_soc=ssoc, end_soc=esoc,
                        energy_used_kwh=0.5, avg_speed_kmh=30, max_speed_kmh=50, outside_temp_c=28,
                    ))
                s.add(BatteryReading(
                    vehicle_id=v.id, ts=datetime(2026, 7, 1, 9, 10),
                    soc=95, range_km=350.0, odo_km=1000.0,
                ))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-COSTTRUTH"})
            try:
                body = client.get("/api/summary?since_charge=true").json()
                bal, drv, chg = body["battery_balance"], body["driving"], body["charging"]
                assert bal["used_kwh"] == 3.5  # ground truth (ratified by the SOCTRUTH test)
                # 3.5 kWh * RM 0.9/kWh = RM 3.15 — not 4.2 kWh's RM 3.78.
                assert drv["total_cost"] == 3.15
                assert drv["cost_per_km"] == round(3.15 / 15.0, 3)
                # Driving Cost's implied per-kWh rate now matches Charging
                # Cost's (the whole point) — both trace back to the same
                # RM 0.9/kWh the charge was actually paid at.
                assert round(drv["total_cost"] / bal["used_kwh"], 3) == round(chg["total_cost"] / chg["total_energy_kwh"], 3)
            finally:
                client.post("/api/active-vehicle", json={"vin": "DEMO0SAMPLE0000001"})
                with SessionLocal() as s:
                    s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                    s.query(Charge).filter(Charge.vehicle_id == v.id).delete()
                    s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
                    s.query(Vehicle).filter(Vehicle.id == v.id).delete()
                    s.commit()
    finally:
        settings.app_passcode = old_pc
        settings.battery_capacity_kwh = old_cap


def test_recent_trips_capped_at_5_for_any_window_but_show_more_raises_it():
    """Reported live: "why all 12th July trip missing" -- with a charge
    cycle spanning more than 5 drives, recent_trips only ever showed the 5
    most recent, so every earlier trip that cycle silently disappeared from
    the list even though the window's own aggregate KPIs still covered all
    of them. First fix made since-charge windows list every trip
    unconditionally; a follow-up request unified this instead -- every
    window (since-charge included) caps at 5 by default, and trips_limit
    (the "Show more" button) raises it the same way everywhere, rather than
    since-charge being a special uncapped case."""
    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            from datetime import timedelta

            from app.database import SessionLocal
            from app.models import Charge, Drive, Vehicle

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-ALLTRIPS", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                s.add(Charge(
                    vehicle_id=v.id,
                    start_time=datetime(2026, 7, 1, 7, 30), end_time=datetime(2026, 7, 1, 8, 0),
                    duration_min=30, start_soc=60, end_soc=100, energy_added_kwh=28.0,
                    charge_type="AC", max_power_kw=7, location="Home", cost=25.2,
                ))
                for i in range(7):  # more than the old 5-trip cap
                    start = datetime(2026, 7, 1, 9, 0) + timedelta(hours=i)
                    s.add(Drive(
                        vehicle_id=v.id, start_time=start, end_time=start + timedelta(minutes=10),
                        distance_km=5, duration_min=10, start_soc=90 - i, end_soc=89 - i,
                        energy_used_kwh=0.5, avg_speed_kmh=30, max_speed_kmh=50, outside_temp_c=28,
                    ))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-ALLTRIPS"})
            try:
                since = client.get("/api/summary?since_charge=true").json()
                assert since["driving"]["total_drives"] == 7
                assert len(since["driving"]["recent_trips"]) == 5  # capped, same as any window

                days = client.get("/api/summary?days=90").json()
                assert days["driving"]["total_drives"] == 7
                assert len(days["driving"]["recent_trips"]) == 5  # plain window, same cap

                # "Show more" button: trips_limit raises the cap, for either
                # window shape.
                more = client.get("/api/summary?days=90&trips_limit=10").json()
                assert len(more["driving"]["recent_trips"]) == 7  # all there are, under the raised cap

                since_more = client.get("/api/summary?since_charge=true&trips_limit=10").json()
                assert len(since_more["driving"]["recent_trips"]) == 7
            finally:
                client.post("/api/active-vehicle", json={"vin": "DEMO0SAMPLE0000001"})
                with SessionLocal() as s:
                    s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                    s.query(Charge).filter(Charge.vehicle_id == v.id).delete()
                    s.query(Vehicle).filter(Vehicle.id == v.id).delete()
                    s.commit()
    finally:
        settings.app_passcode = old_pc


def test_idle_inducer_detects_sentry_and_climate_but_never_a_negative():
    """_idle_inducer() only ever reports a POSITIVE detection from
    BatteryReading rows actually logged inside the gap — a reading showing
    both off, or no reading at all, must never be reported as a confirmed
    "nothing was running" (sync stops polling once the car sleeps, so most
    of a real gap is unobserved)."""
    from app.api.routes import _idle_inducer
    from app.database import SessionLocal
    from app.models import BatteryReading, Vehicle

    with SessionLocal() as s:
        v = Vehicle(vin="TESTVIN-INDUCER", name="Test", model="Model 3")
        s.add(v)
        s.commit()
        gap_start, gap_end = "2026-07-01T08:00", "2026-07-01T20:00"

        # No readings at all in range -> no claim either way.
        assert _idle_inducer(s, v.id, gap_start, gap_end) is None

        # A reading with both off, still inside the gap -> no claim (can't
        # rule out a later toggle the app never saw).
        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 8, 5),
                              soc=80, range_km=300, sentry_mode=False, climate_on=False))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) is None

        # A reading outside the gap showing Sentry on doesn't count.
        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 21, 0),
                              soc=79, range_km=298, sentry_mode=True, climate_on=False))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) is None

        # Sentry on, inside the gap -> positive detection.
        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 8, 10),
                              soc=80, range_km=300, sentry_mode=True, climate_on=False))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) == "Sentry Mode (maybe)"

        # Climate also seen on inside the gap -> both mentioned.
        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 9, 0),
                              soc=79, range_km=299, sentry_mode=False, climate_on=True))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) == "Sentry Mode & climate (maybe)"

        s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
        s.query(Vehicle).filter(Vehicle.id == v.id).delete()
        s.commit()


def test_idle_inducer_prefers_cabin_overheat_protection_over_generic_climate():
    """Cabin overheat protection is a specific reason climate_on went true —
    when a reading shows it actively cooling, the label names that
    specifically instead of also/separately claiming generic "climate was
    on" for the same underlying HVAC activity. cabin_overheat_protection
    ("Off"/"On"/"FanOnly") alone is just the car's *setting* — most owners
    leave it "On" permanently as a safety default — so it must NOT be
    reported as a drain cause unless cabin_overheat_protection_actively_
    cooling actually confirms it ran (reported live: a user's idle gap was
    labelled "cabin overheat protection was on" purely because the setting
    was left enabled, not because it ever activated)."""
    from app.api.routes import _idle_inducer
    from app.database import SessionLocal
    from app.models import BatteryReading, Vehicle

    with SessionLocal() as s:
        v = Vehicle(vin="TESTVIN-COP", name="Test", model="Model 3")
        s.add(v)
        s.commit()
        gap_start, gap_end = "2026-07-01T08:00", "2026-07-01T20:00"

        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 12, 0),
                              soc=80, range_km=300, climate_on=True,
                              cabin_overheat_protection="On",
                              cabin_overheat_protection_actively_cooling=True))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) == "cabin overheat protection (maybe)"

        # FanOnly still counts as active, as long as it's really cooling.
        s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 12, 0),
                              soc=80, range_km=300, climate_on=True,
                              cabin_overheat_protection="FanOnly",
                              cabin_overheat_protection_actively_cooling=True))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) == "cabin overheat protection (maybe)"

        # The setting is "On" (as it almost always is) but it never actually
        # triggered this gap -> must NOT claim COP; falls back to the
        # generic label since climate_on is still true from something else.
        s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 12, 0),
                              soc=80, range_km=300, climate_on=True,
                              cabin_overheat_protection="On",
                              cabin_overheat_protection_actively_cooling=False))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) == "climate (maybe)"

        # Setting Off, and never actively cooling -> no COP claim either way.
        s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
        s.add(BatteryReading(vehicle_id=v.id, ts=datetime(2026, 7, 1, 12, 0),
                              soc=80, range_km=300, climate_on=True,
                              cabin_overheat_protection="Off",
                              cabin_overheat_protection_actively_cooling=False))
        s.commit()
        assert _idle_inducer(s, v.id, gap_start, gap_end) == "climate (maybe)"

        s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
        s.query(Vehicle).filter(Vehicle.id == v.id).delete()
        s.commit()
        s.query(Vehicle).filter(Vehicle.id == v.id).delete()
        s.commit()


def test_place_label_prefers_specific_feature_over_broad_district():
    """The label should name the actual spot (POI/street) rather than the
    broader neighbourhood the old zoom-16 logic settled on, and area should
    be the coarser district it sits in (the route-grouping key)."""
    from app.api.routes import _label_from_geocode

    # A named POI at the point wins over the surrounding suburb; area is the
    # coarser suburb it sits in.
    assert _label_from_geocode({
        "name": "Queensbay Mall",
        "address": {"building": "Queensbay Mall", "suburb": "Bayan Lepas",
                    "city": "George Town", "neighbourhood": "Bayan Mutiara"},
    }) == ("Queensbay Mall, Bayan Lepas", "Bayan Lepas")

    # No POI: the street (with house number) beats the neighbourhood for the
    # specific part; the area falls to the city when no suburb is present.
    assert _label_from_geocode({
        "address": {"house_number": "12", "road": "Lebuh Tunku Kudin",
                    "neighbourhood": "Bayan Mutiara", "city": "George Town"},
    }) == ("12 Lebuh Tunku Kudin, George Town", "George Town")

    # Falls back gracefully when only coarse fields exist, and never repeats
    # the same word on both sides of the comma.
    assert _label_from_geocode({
        "address": {"suburb": "George Town", "city": "George Town"},
    }) == ("George Town", "George Town")
    assert _label_from_geocode({}) == ("", "")


def test_google_geocode_label_prefers_poi_over_result_order():
    """Google doesn't reliably return the POI result first, unlike Nominatim
    — a point_of_interest/establishment result must be found explicitly
    rather than trusting results[0]."""
    from app.api.routes import _label_from_google_geocode

    assert _label_from_google_geocode({
        "results": [
            {  # A broader street_address result Google happened to list first.
                "types": ["street_address"],
                "address_components": [
                    {"long_name": "1", "types": ["street_number"]},
                    {"long_name": "Persiaran Gurney", "types": ["route"]},
                    {"long_name": "George Town", "types": ["locality"]},
                ],
            },
            {  # The actual POI at this point, listed second.
                "types": ["point_of_interest", "establishment"],
                "address_components": [
                    {"long_name": "Queensbay Mall", "types": ["point_of_interest", "establishment"]},
                    {"long_name": "Bayan Lepas", "types": ["sublocality"]},
                    {"long_name": "George Town", "types": ["locality"]},
                ],
            },
        ],
    }) == ("Queensbay Mall, Bayan Lepas", "Bayan Lepas")

    # No POI result: falls back to street number + route, area from locality.
    assert _label_from_google_geocode({
        "results": [{
            "types": ["street_address"],
            "address_components": [
                {"long_name": "12", "types": ["street_number"]},
                {"long_name": "Lebuh Tunku Kudin", "types": ["route"]},
                {"long_name": "George Town", "types": ["locality"]},
            ],
        }],
    }) == ("12 Lebuh Tunku Kudin, George Town", "George Town")

    assert _label_from_google_geocode({}) == ("", "")
    assert _label_from_google_geocode({"results": []}) == ("", "")


def test_place_and_area_prefers_google_when_configured_falls_back_on_miss(monkeypatch):
    """google_maps_api_key set -> Google is tried first; a failed/empty
    Google lookup still falls back to Nominatim rather than giving up."""
    from app.api import routes

    settings = get_settings()
    old = settings.google_maps_api_key
    settings.google_maps_api_key = "test-key"
    routes._PLACE_CACHE.clear()
    try:
        monkeypatch.setattr(routes, "_google_reverse_geocode", lambda lat, lon, key: ("Queensbay Mall, Bayan Lepas", "Bayan Lepas"))
        assert routes._place_and_area("5.3300, 100.3000") == ("Queensbay Mall, Bayan Lepas", "Bayan Lepas")

        routes._PLACE_CACHE.clear()
        monkeypatch.setattr(routes, "_google_reverse_geocode", lambda lat, lon, key: None)
        monkeypatch.setattr(
            routes.httpx, "get",
            lambda *a, **k: type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"address": {"road": "Lebuh Tunku Kudin", "city": "George Town"}},
            })(),
        )
        assert routes._place_and_area("5.4100, 100.3200") == ("Lebuh Tunku Kudin, George Town", "George Town")
    finally:
        settings.google_maps_api_key = old
        routes._PLACE_CACHE.clear()


def test_place_and_area_reuses_a_nearby_cached_name(monkeypatch):
    """A parked car's reported position drifts a few metres between polls, so
    the coords a trip arrives at and the ones the next trip departs from are
    rarely identical. Those must resolve to the SAME name — looking the second
    one up again can return a different nearby business, which is what left one
    McDonald's stop labelled two ways across consecutive trips."""
    from app.api import routes

    settings = get_settings()
    old = settings.google_maps_api_key
    settings.google_maps_api_key = "test-key"
    routes._PLACE_CACHE.clear()
    calls = []

    def fake_geocode(lat, lon, key):
        calls.append((lat, lon))
        # Whatever the geocoder would say the *second* time is deliberately
        # different — the point is that it never gets asked.
        return (f"Business #{len(calls)}", f"Area #{len(calls)}")

    try:
        monkeypatch.setattr(routes, "_google_reverse_geocode", fake_geocode)
        arrival = routes._place_and_area("5.38120, 100.30210")
        assert arrival == ("Business #1", "Area #1")

        # ~15 m away: same spot, so the cached name is reused and no second
        # lookup happens.
        departure = routes._place_and_area("5.38133, 100.30212")
        assert departure == arrival
        assert len(calls) == 1

        # ~1 km away is a genuinely different place — it must still look up.
        other = routes._place_and_area("5.39100, 100.30210")
        assert other == ("Business #2", "Area #2")
        assert len(calls) == 2
    finally:
        settings.google_maps_api_key = old
        routes._PLACE_CACHE.clear()


def test_place_and_area_passes_through_invalid_coords():
    """No network call for an empty/malformed coordinate string — both label
    and area fall back to the raw input untouched."""
    from app.api.routes import _place, _place_and_area

    assert _place_and_area("") == ("", "")
    assert _place_and_area("not-coords") == ("not-coords", "not-coords")
    assert _place("") == ""


def test_summary_narrative_gated_the_same_as_week_compare():
    """The narrative only makes sense for a plain days-based window (a
    natural "period before" exists); since_charge/current_drive windows
    have no such period, so it's omitted rather than comparing against
    something arbitrary."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            wide = client.get("/api/summary?days=365").json()
            assert wide["narrative"] is not None
            assert isinstance(wide["narrative"], list) and wide["narrative"]
            assert "km" in wide["narrative"][0]

            narrow = client.get("/api/summary?days=7").json()   # < 14 days
            assert narrow["narrative"] is None

            since = client.get("/api/summary?days=365&since_charge=1").json()
            assert since["narrative"] is None
    finally:
        settings.app_passcode = old


def test_monthly_report_includes_narrative():
    settings = get_settings()
    old_pc, old_url = settings.app_passcode, settings.report_webhook_url
    settings.app_passcode = ""
    settings.report_webhook_url = "https://example.invalid/report"
    try:
        with TestClient(app) as client:  # startup seeds demo data
            sent = {}

            def fake_post(url, json=None, timeout=None):
                sent["json"] = json
                import httpx as _httpx
                return _httpx.Response(200, request=_httpx.Request("POST", url))

            import app.api.routes as routes_mod
            orig_post = routes_mod.httpx.post
            routes_mod.httpx.post = fake_post
            try:
                resp = client.get("/api/reports/monthly?days=30")
            finally:
                routes_mod.httpx.post = orig_post

            assert resp.status_code == 200
            payload = sent["json"]
            assert isinstance(payload["narrative"], list) and payload["narrative"]
            assert "📝" in payload["text"]
    finally:
        settings.app_passcode, settings.report_webhook_url = old_pc, old_url


def test_summary_reports_week_compare_and_costs():
    """A wide window includes the rolling week-over-week compare (or null when
    a week is empty) plus driving/charging cost figures."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            body = client.get("/api/summary?days=365").json()
            assert "week_compare" in body
            wc = body["week_compare"]
            if wc is not None:  # demo data spans recent weeks, so usually present
                assert set(wc) == {"this", "last"}
                assert wc["this"]["distance_km"] >= 0
            drv, chg = body["driving"], body["charging"]
            assert drv["total_cost"] is not None      # tariff configured by default
            assert drv["cost_per_km"] is not None
            assert "insights" in drv
            assert round(chg["ac_cost"] + chg["dc_cost"], 1) == round(chg["total_cost"], 1)
            # Narrow "since charge" windows skip the compare rather than
            # sending a misleading partial week.
            since = client.get("/api/summary?days=365&since_charge=1").json()
            assert since["week_compare"] is None
    finally:
        settings.app_passcode = old


def test_trip_cost_uses_last_charge_rate_not_configured_tariff():
    """Driving/trip cost is priced at what was actually paid for the most
    recent charge (cost ÷ energy_added_kwh) — what's really powering every
    subsequent trip — not the app's configured flat/ToU tariff."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            from app.database import SessionLocal
            from app.models import Charge, Drive, Vehicle

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-LASTRATE", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                # RM 2.00/kWh — deliberately far from the app's configured
                # flat tariff (0.90 by default) so the two are easy to tell apart.
                s.add(Charge(
                    vehicle_id=v.id,
                    start_time=datetime(2026, 7, 1, 22, 0), end_time=datetime(2026, 7, 2, 0, 0),
                    duration_min=120, start_soc=40, end_soc=90, energy_added_kwh=20.0,
                    charge_type="AC", max_power_kw=7, location="Home", cost=40.0,
                ))
                s.add(Drive(
                    vehicle_id=v.id,
                    start_time=datetime(2026, 7, 2, 8, 0), end_time=datetime(2026, 7, 2, 8, 30),
                    distance_km=20, duration_min=30, start_soc=90, end_soc=85,
                    energy_used_kwh=4.0, avg_speed_kmh=40, max_speed_kmh=60, outside_temp_c=28,
                ))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-LASTRATE"})
            try:
                body = client.get("/api/summary?days=365").json()
                trip = body["driving"]["recent_trips"][0]
                assert trip["cost"] == round(4.0 * 2.00, 2)  # 8.00, at the charge's own rate
                assert body["driving"]["total_cost"] == round(4.0 * 2.00, 2)
            finally:
                client.post("/api/active-vehicle", json={"vin": "DEMO0SAMPLE0000001"})
                with SessionLocal() as s:
                    s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                    s.query(Charge).filter(Charge.vehicle_id == v.id).delete()
                    s.query(Vehicle).filter(Vehicle.id == v.id).delete()
                    s.commit()
    finally:
        settings.app_passcode = old


def test_petrol_comparison_hidden_unless_configured_then_reflects_settings():
    settings = get_settings()
    old_pc = settings.app_passcode
    old_price, old_l100 = settings.petrol_price_per_liter, settings.petrol_l_per_100km
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            # Disabled by default (both 0) -> no assumed "average car" figure.
            settings.petrol_price_per_liter = 0.0
            settings.petrol_l_per_100km = 0.0
            body = client.get("/api/summary?days=365").json()
            assert body["petrol_comparison"] is None

            settings.petrol_price_per_liter = 2.05
            settings.petrol_l_per_100km = 7.0
            body = client.get("/api/summary?days=365").json()
            pc = body["petrol_comparison"]
            assert pc is not None
            distance_km = body["driving"]["total_distance_km"]
            expected_petrol_cost = round(distance_km / 100.0 * 7.0 * 2.05, 2)
            assert pc["petrol_cost"] == expected_petrol_cost
            assert pc["distance_km"] == distance_km
            ev_cost = body["driving"]["total_cost"]
            assert pc["ev_cost"] == ev_cost
            assert pc["savings"] == round(expected_petrol_cost - ev_cost, 2)
    finally:
        settings.app_passcode = old_pc
        settings.petrol_price_per_liter, settings.petrol_l_per_100km = old_price, old_l100


def test_charge_cost_uses_time_of_use_pricing_at_write_time():
    """A charge session logged through _process_vehicle is re-priced at its
    own start time under configured TOU rates, not the flat default."""
    from types import SimpleNamespace

    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import Charge, Vehicle

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_ac_kwh=0.0, energy_price_dc_kwh=0.0,
        energy_price_peak_kwh=1.20,
        energy_price_offpeak_kwh=0.45, tariff_peak_start_hour=8,
        tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0, sentry_drain_notify_pct=0.0,
        intrusion_notify=False,
        drive_min_km=0.5,
    )

    def vehicle_data(vin, ts, odo_mi, soc, added_kwh, charging, lat=None, lon=None):
        return {
            "vin": vin, "display_name": "Test",
            "vehicle_config": {},
            "vehicle_state": {"odometer": odo_mi, "is_user_present": True, "locked": False},
            "drive_state": {"timestamp": ts * 1000, "shift_state": "P", "speed": 0,
                            "latitude": lat, "longitude": lon},
            "charge_state": {
                "battery_level": soc, "battery_range": 200.0,
                "charging_state": "Charging" if charging else "Complete",
                "charger_power": 7.0 if charging else 0.0,
                "charge_energy_added": added_kwh,
            },
            "climate_state": {"outside_temp": 25.0},
        }

    with SessionLocal() as s:
        v = Vehicle(vin="TESTVIN-TOU", name="Test", model="Model 3")
        s.add(v)
        # This test is about ToU pricing specifically, not about which source a
        # charge defaults to when nothing else matches — pin it explicitly so
        # it stays independent of that default (currently "home").
        from app import state
        state.put(s, state.DEFAULT_PRICE_SOURCE_KEY, "public")
        s.commit()

        # Monday 2pm MYT (peak, per the settings above): start charging.
        # Built as a UTC epoch (MYT is UTC+8, no DST) so the result doesn't
        # depend on the test runner's own local timezone.
        import calendar
        from datetime import datetime as _dt, timedelta as _td

        base_ts = calendar.timegm((_dt(2026, 7, 6, 14, 0, 0) - _td(hours=8)).timetuple())
        d1 = vehicle_data("TESTVIN-TOU", base_ts, 1000.0, 40, 0.0, True)
        _process_vehicle(s, d1, {"vin": "TESTVIN-TOU"}, settings)
        s.commit()
        # 10 minutes later, charging stops with 5 kWh added.
        d2 = vehicle_data("TESTVIN-TOU", base_ts + 600, 1000.0, 47, 5.0, False)
        _process_vehicle(s, d2, {"vin": "TESTVIN-TOU"}, settings)
        s.commit()

        charge = s.query(Charge).filter(Charge.vehicle_id == v.id).first()
        assert charge is not None
        assert charge.energy_added_kwh == 5.0
        assert charge.cost == round(5.0 * 1.20, 2)   # peak rate, not the flat 0.90


def test_charge_cost_uses_ac_dc_rate_at_write_time():
    """AC/DC rates win over ToU (and the flat rate) when configured — real
    bills differ far more by charger type than by time of day."""
    from types import SimpleNamespace

    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import Charge, Vehicle

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_ac_kwh=0.90, energy_price_dc_kwh=1.13,
        # ToU also configured, to prove AC/DC wins over it too.
        energy_price_peak_kwh=1.20, energy_price_offpeak_kwh=0.45,
        tariff_peak_start_hour=8, tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0, sentry_drain_notify_pct=0.0,
        intrusion_notify=False,
        drive_min_km=0.5,
    )

    def vehicle_data(vin, ts, odo_mi, soc, added_kwh, charging, fast=False):
        return {
            "vin": vin, "display_name": "Test",
            "vehicle_config": {},
            "vehicle_state": {"odometer": odo_mi, "is_user_present": True, "locked": False},
            "drive_state": {"timestamp": ts * 1000, "shift_state": "P", "speed": 0,
                            "latitude": None, "longitude": None},
            "charge_state": {
                "battery_level": soc, "battery_range": 200.0,
                "charging_state": "Charging" if charging else "Complete",
                "charger_power": 150.0 if fast else 7.0,
                "charge_energy_added": added_kwh,
                "fast_charger_present": fast,
            },
            "climate_state": {"outside_temp": 25.0},
        }

    with SessionLocal() as s:
        v = Vehicle(vin="TESTVIN-ACDC", name="Test", model="Model 3")
        s.add(v)
        s.commit()

        import calendar
        from datetime import datetime as _dt, timedelta as _td

        base_ts = calendar.timegm((_dt(2026, 7, 6, 14, 0, 0) - _td(hours=8)).timetuple())

        # AC session: home charger, 5 kWh added.
        d1 = vehicle_data("TESTVIN-ACDC", base_ts, 1000.0, 40, 0.0, True)
        _process_vehicle(s, d1, {"vin": "TESTVIN-ACDC"}, settings)
        s.commit()
        d2 = vehicle_data("TESTVIN-ACDC", base_ts + 600, 1000.0, 47, 5.0, False)
        _process_vehicle(s, d2, {"vin": "TESTVIN-ACDC"}, settings)
        s.commit()

        # DC fast-charge session: 10 kWh added, an hour later. Odometer stays
        # put (parked between sessions) — any movement here would register as
        # a whole-gap drive, which isn't what this test is about.
        d3 = vehicle_data("TESTVIN-ACDC", base_ts + 3600, 1000.0, 50, 0.0, True, fast=True)
        _process_vehicle(s, d3, {"vin": "TESTVIN-ACDC"}, settings)
        s.commit()
        d4 = vehicle_data("TESTVIN-ACDC", base_ts + 4200, 1000.0, 63, 10.0, False, fast=True)
        _process_vehicle(s, d4, {"vin": "TESTVIN-ACDC"}, settings)
        s.commit()

        charges = s.query(Charge).filter(Charge.vehicle_id == v.id).order_by(Charge.start_time).all()
        assert len(charges) == 2
        assert charges[0].charge_type == "AC"
        assert charges[0].cost == round(5.0 * 0.90, 2)
        assert charges[1].charge_type == "DC"
        assert charges[1].cost == round(10.0 * 1.13, 2)


def test_drive_complete_fires_event_webhook_but_not_push(monkeypatch):
    """Logging a drive through _process_vehicle fires the generic event
    webhook (for home-automation consumers) without going through the push
    channel — a push alert per every single drive would be unwanted noise
    for anyone who already has charge/low-battery push enabled."""
    from types import SimpleNamespace

    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import Vehicle

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_ac_kwh=0.0, energy_price_dc_kwh=0.0,
        energy_price_peak_kwh=0.0,
        energy_price_offpeak_kwh=0.0, tariff_peak_start_hour=8,
        tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0, sentry_drain_notify_pct=0.0,
        intrusion_notify=False,
        drive_min_km=0.5,
    )

    def vehicle_data(vin, ts, odo_mi, soc, shift, speed=0):
        return {
            "vin": vin, "display_name": "Test",
            "vehicle_config": {},
            "vehicle_state": {"odometer": odo_mi, "is_user_present": True, "locked": shift == "P"},
            "drive_state": {"timestamp": ts * 1000, "shift_state": shift, "speed": speed,
                            "latitude": None, "longitude": None},
            "charge_state": {"battery_level": soc, "battery_range": 200.0,
                             "charging_state": "Complete", "charger_power": 0.0,
                             "charge_energy_added": 0.0},
            "climate_state": {"outside_temp": 25.0},
        }

    webhook_calls = []
    push_calls = []
    monkeypatch.setattr(
        "app.api.routes.notifications.fire_webhook",
        lambda event, title, body: webhook_calls.append((event, title, body)),
    )
    monkeypatch.setattr(
        "app.api.routes.notifications.notify",
        lambda *a, **k: push_calls.append((a, k)),
    )

    try:
        with SessionLocal() as s:
            v = Vehicle(vin="TESTVIN-DRIVE", name="Test", model="Model 3")
            s.add(v)
            s.commit()

            base_ts = 1_760_000_000
            d1 = vehicle_data("TESTVIN-DRIVE", base_ts, 1000.0, 80, "P")
            _process_vehicle(s, d1, {"vin": "TESTVIN-DRIVE"}, settings)
            s.commit()
            d2 = vehicle_data("TESTVIN-DRIVE", base_ts + 600, 1000.0, 80, "D", speed=40)
            _process_vehicle(s, d2, {"vin": "TESTVIN-DRIVE"}, settings)
            s.commit()
            d3 = vehicle_data("TESTVIN-DRIVE", base_ts + 1800, 1010.0, 75, "P")
            _process_vehicle(s, d3, {"vin": "TESTVIN-DRIVE"}, settings)
            s.commit()

        assert any(c[0] == "drive-complete" for c in webhook_calls)
        assert push_calls == []   # drive completion never goes through the push channel
    finally:
        # This vehicle's drive rows aren't scoped out of other tests'
        # global counts (e.g. clear-drives) — remove them so this test
        # doesn't pollute the shared demo DB for the rest of the suite.
        with SessionLocal() as s:
            from app.models import Drive as _Drive

            leftover = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-DRIVE").first()
            if leftover:
                s.query(_Drive).filter(_Drive.vehicle_id == leftover.id).delete()
                s.delete(leftover)
                s.commit()


def test_sentry_drain_alert_fires_once_per_parked_episode(monkeypatch):
    """While parked with Sentry on, the live drain alert fires once the drop
    since parking crosses the threshold — then stays quiet for that episode,
    and re-arms after the car drives off."""
    from types import SimpleNamespace

    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import BatteryReading, Vehicle

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_ac_kwh=0.0, energy_price_dc_kwh=0.0,
        energy_price_peak_kwh=0.0, energy_price_offpeak_kwh=0.0, tariff_peak_start_hour=8,
        tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0,
        sentry_drain_notify_pct=2.0, intrusion_notify=False, drive_min_km=0.5,
    )

    def vdata(ts, odo_mi, soc, shift, sentry, speed=0):
        return {
            "vin": "TESTVIN-SENTRY", "display_name": "Test", "vehicle_config": {},
            "vehicle_state": {"odometer": odo_mi, "is_user_present": shift != "P",
                              "locked": shift == "P", "sentry_mode": sentry},
            "drive_state": {"timestamp": ts * 1000, "shift_state": shift, "speed": speed,
                            "latitude": None, "longitude": None},
            "charge_state": {"battery_level": soc, "battery_range": 200.0,
                             "charging_state": "Disconnected", "charger_power": 0.0,
                             "charge_energy_added": 0.0},
            "climate_state": {"outside_temp": 25.0},
        }

    pushes = []
    monkeypatch.setattr("app.api.routes.notifications.notify",
                        lambda session, title, body, tag=None: pushes.append((title, tag)))

    t = 1_760_500_000
    try:
        with SessionLocal() as s:
            s.add(Vehicle(vin="TESTVIN-SENTRY", name="Test", model="Model 3"))
            s.commit()

            def tick(dt, soc, shift, sentry):
                _process_vehicle(s, vdata(t + dt, 2000.0, soc, shift, sentry),
                                 {"vin": "TESTVIN-SENTRY"}, settings)
                s.commit()

            tick(0, 80, "P", True)      # parked, Sentry on -> anchor at 80%
            tick(600, 79, "P", True)    # -1% -> under threshold, quiet
            tick(1200, 77, "P", True)   # -3% -> fires once
            tick(1800, 76, "P", True)   # already fired this episode -> quiet
            sentry_pushes = [p for p in pushes if p[1] == "sentry-drain"]
            assert len(sentry_pushes) == 1

            tick(2400, 76, "D", True, )  # drove off -> episode resets
            tick(3000, 76, "P", True)    # new park -> new anchor at 76%
            tick(3600, 73, "P", True)    # -3% again -> fires for the new episode
            assert len([p for p in pushes if p[1] == "sentry-drain"]) == 2
    finally:
        with SessionLocal() as s:
            from app.models import Drive as _Drive
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-SENTRY").first()
            if v:
                s.query(_Drive).filter(_Drive.vehicle_id == v.id).delete()
                s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_display_state_flicker_forces_a_battery_reading(monkeypatch):
    """A watched state changing must write its own BatteryReading row even
    with SoC unmoved.

    Sentry arming is the case that matters: it happens as the car parks,
    before it sleeps and polling stops seeing it, and SoC will not have moved
    a whole point by then. Keying the write on SoC alone would drop the one
    sample that decides how the whole parked gap is priced — see
    driving.gap_sentry_state."""
    from types import SimpleNamespace

    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import BatteryReading, Vehicle

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_ac_kwh=0.0, energy_price_dc_kwh=0.0,
        energy_price_peak_kwh=0.0, energy_price_offpeak_kwh=0.0, tariff_peak_start_hour=8,
        tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0,
        sentry_drain_notify_pct=0.0, intrusion_notify=False, drive_min_km=0.5,
    )

    def vdata(ts, sentry):
        return {
            "vin": "TESTVIN-DISPLAY", "display_name": "Test", "vehicle_config": {},
            "vehicle_state": {"odometer": 2000.0, "is_user_present": False,
                              "locked": True, "sentry_mode": sentry},
            "drive_state": {"timestamp": ts * 1000, "shift_state": "P", "speed": 0,
                            "latitude": None, "longitude": None},
            "charge_state": {"battery_level": 80, "battery_range": 200.0,
                             "charging_state": "Disconnected", "charger_power": 0.0,
                             "charge_energy_added": 0.0},
            "climate_state": {"outside_temp": 25.0},
        }

    t = 1_760_700_000
    try:
        with SessionLocal() as s:
            v = Vehicle(vin="TESTVIN-DISPLAY", name="Test", model="Model 3")
            s.add(v)
            s.commit()
            vid = v.id

            def tick(dt, sentry):
                _process_vehicle(s, vdata(t + dt, sentry),
                                 {"vin": "TESTVIN-DISPLAY"}, settings)
                s.commit()

            def rows():
                return s.query(BatteryReading).filter(
                    BatteryReading.vehicle_id == vid).order_by(BatteryReading.ts).all()

            tick(0, True)      # first reading
            tick(60, True)     # unchanged, SoC unmoved -> no new row
            before = len(rows())
            tick(120, False)   # Sentry went off (SoC identical) -> must log
            after = rows()
            assert len(after) == before + 1
            assert after[-1].sentry_mode is False
    finally:
        with SessionLocal() as s:
            from app.models import Drive as _Drive
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-DISPLAY").first()
            if v:
                s.query(_Drive).filter(_Drive.vehicle_id == v.id).delete()
                s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_repair_moves_a_boundary_and_the_figures_that_follow_from_it(monkeypatch):
    """Undoing the reverted place-split's damage: it credited an arriving trip
    1.311 km it never drove and starved the departing one by the same amount.
    Moving the boundary back has to carry energy with it — at each trip's own
    Wh/km, which is the exact inverse of how the bad figure was made — and
    hand the stretch back to the trip that really drove it."""
    from datetime import datetime, timedelta

    from app.api.routes import repair_trip_boundary
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    t0 = datetime(2026, 8, 2, 19, 19)
    try:
        with SessionLocal() as s:
            s.add(Vehicle(vin="TESTVIN-REPAIR", name="Test", model="Model 3"))
            s.commit()
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-REPAIR").first()
            # The corrupted pair, as the live rows read.
            closed = Drive(vehicle_id=v.id, start_time=t0,
                           end_time=t0 + timedelta(minutes=8),
                           distance_km=5.4, duration_min=8.0,
                           start_soc=35.0, end_soc=34.0, energy_used_kwh=0.74,
                           start_odo_km=28908.673, end_odo_km=28914.093,
                           start_location="7-Eleven", end_location="Home")
            opened = Drive(vehicle_id=v.id, start_time=t0 + timedelta(hours=11),
                           end_time=t0 + timedelta(hours=11, minutes=22),
                           distance_km=8.0, duration_min=22.0,
                           start_soc=34.0, end_soc=32.0, energy_used_kwh=1.19,
                           start_odo_km=28914.093, end_odo_km=28922.049,
                           start_recovered_km=0.515,
                           start_location="Home", end_location="Office")
            s.add_all([closed, opened])
            s.commit()
            cid, oid = closed.id, opened.id

            preview = repair_trip_boundary(
                closed_id=cid, open_id=oid, boundary_odo_km=28912.782,
                closed_end_time=None, closed_end_coords=None,
                apply=False, session=s)
            assert preview["delta_km"] == -1.311
            assert preview["closed"]["distance_km"] == [5.4, 4.1]
            assert preview["open"]["distance_km"] == [8.0, 9.3]
            # Energy follows distance at each trip's own Wh/km, which returns
            # the arriving trip to what it measured before the fault.
            assert preview["closed"]["energy_kwh"] == [0.74, 0.56]
            assert preview["open"]["energy_kwh"] == [1.19, 1.38]
            s.expire_all()
            assert s.get(Drive, cid).distance_km == 5.4      # dry run wrote nothing

            repair_trip_boundary(
                closed_id=cid, open_id=oid, boundary_odo_km=28912.782,
                closed_end_time="2026-08-02T19:25",
                closed_end_coords="5.3427, 100.3106", apply=True, session=s)
            s.expire_all()
            c2, o2 = s.get(Drive, cid), s.get(Drive, oid)
            assert (c2.distance_km, c2.end_odo_km) == (4.1, 28912.782)
            assert (o2.distance_km, o2.start_odo_km) == (9.3, 28912.782)
            assert c2.duration_min == 6.0                    # end time restored
            # The stretch handed back arrived with no reading of its own.
            assert o2.start_recovered_km == 1.826
            assert o2.avg_speed_kmh == round(9.3 / (22.0 / 60.0), 1)
            # The fault restamped the arrival with the place it matched, so a
            # trip home read "-> QBM". Coordinates are what the label derives
            # from, so restoring them puts the name back.
            assert c2.end_coords == "5.3427, 100.3106"

            # A boundary that would leave a trip with no distance at all is
            # refused rather than written as a negative.
            with pytest.raises(Exception):
                repair_trip_boundary(closed_id=cid, open_id=oid,
                                     boundary_odo_km=28900.0, closed_end_time=None,
                                     closed_end_coords=None, apply=False, session=s)
    finally:
        with SessionLocal() as s:
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-REPAIR").first()
            if v:
                s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_backfill_repairs_origins_the_odometer_can_prove(monkeypatch):
    """Trips recorded before 10e8423 name wherever the network came back, not
    where the car set off. The odometer is what makes them repairable: a
    recovered departure starting on the previous trip's closing reading began
    exactly where that trip ended. Dry run by default — a repair that rewrites
    history unasked is worse than the wrong location."""
    from datetime import datetime, timedelta

    from app.api import routes as routes_mod
    from app.api.routes import backfill_start_locations
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    t0 = datetime(2026, 7, 1, 8, 0)

    def drive(n, start_odo, end_odo, recovered, start_coords, end_coords):
        return Drive(
            vehicle_id=None, start_time=t0 + timedelta(hours=n),
            end_time=t0 + timedelta(hours=n, minutes=30),
            distance_km=end_odo - start_odo, duration_min=30.0,
            start_soc=80.0, end_soc=78.0,
            start_odo_km=start_odo, end_odo_km=end_odo,
            start_recovered_km=recovered,
            start_coords=start_coords, start_location=f"place<{start_coords}>",
            start_area=f"area<{start_coords}>",
            end_coords=end_coords, end_location=f"place<{end_coords}>",
            end_area=f"area<{end_coords}>")

    try:
        with SessionLocal() as s:
            s.add(Vehicle(vin="TESTVIN-BACKFILL", name="Test", model="Model 3"))
            s.commit()
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-BACKFILL").first()
            monkeypatch.setattr(routes_mod, "_first_vehicle", lambda _s: v)

            rows = [
                # Ends at home.
                drive(0, 100.0, 110.0, 0.0, "5.30, 100.30", "5.3430, 100.3107"),
                # Recovered departure: odometer says it left home, coords say
                # it started on the highway. The repairable case.
                drive(2, 110.0, 121.0, 1.579, "5.3494, 100.3095", "5.40, 100.32"),
                # Recovered, but the odometer does NOT hand over — a gap means
                # something else happened between them. Must be left alone.
                drive(4, 125.0, 130.0, 0.5, "5.41, 100.33", "5.42, 100.34"),
                # No recovery at all: its own start reading is the truth.
                drive(6, 130.0, 140.0, 0.0, "5.42, 100.34", "5.43, 100.35"),
            ]
            for r in rows:
                r.vehicle_id = v.id
                s.add(r)
            s.commit()

            preview = backfill_start_locations(apply=False, session=s)
            assert preview["would_change"] == 1
            only = preview["changes"][0]
            assert only["from"]["coords"] == "5.3494, 100.3095"
            assert only["to"]["coords"] == "5.3430, 100.3107"
            # Nothing written on a dry run.
            s.expire_all()
            still = s.query(Drive).filter(Drive.start_odo_km == 110.0).one()
            assert still.start_coords == "5.3494, 100.3095"

            applied = backfill_start_locations(apply=True, session=s)
            assert applied["changed"] == 1
            s.expire_all()
            fixed = s.query(Drive).filter(Drive.start_odo_km == 110.0).one()
            assert fixed.start_coords == "5.3430, 100.3107"
            assert fixed.start_location == "place<5.3430, 100.3107>"
            assert fixed.start_area == "area<5.3430, 100.3107>"
            # The non-contiguous one is untouched.
            untouched = s.query(Drive).filter(Drive.start_odo_km == 125.0).one()
            assert untouched.start_coords == "5.41, 100.33"

            # And it's idempotent — a second run finds nothing left to do.
            assert backfill_start_locations(apply=False, session=s)["would_change"] == 0
    finally:
        with SessionLocal() as s:
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-BACKFILL").first()
            if v:
                s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_intrusion_alert_fires_once_per_opening(monkeypatch):
    """A door opening while the car sits parked with Sentry armed and nobody
    aboard fires once, stays quiet while it's still open, and re-arms once
    everything is shut again. An opening with someone aboard (or with Sentry
    off) is ordinary use and must stay silent."""
    from types import SimpleNamespace

    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import BatteryReading, Vehicle

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_ac_kwh=0.0, energy_price_dc_kwh=0.0,
        energy_price_peak_kwh=0.0, energy_price_offpeak_kwh=0.0, tariff_peak_start_hour=8,
        tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0,
        sentry_drain_notify_pct=0.0, intrusion_notify=True, drive_min_km=0.5,
    )

    def vdata(ts, sentry, door_open, user_present=False, locked=True):
        return {
            "vin": "TESTVIN-INTRUDE", "display_name": "Test", "vehicle_config": {},
            "vehicle_state": {"odometer": 2000.0, "is_user_present": user_present,
                              "locked": locked, "sentry_mode": sentry,
                              "df": 1 if door_open else 0, "dr": 0, "pf": 0, "pr": 0,
                              "ft": 0, "rt": 0},
            "drive_state": {"timestamp": ts * 1000, "shift_state": "P", "speed": 0,
                            "latitude": None, "longitude": None},
            "charge_state": {"battery_level": 80, "battery_range": 200.0,
                             "charging_state": "Disconnected", "charger_power": 0.0,
                             "charge_energy_added": 0.0},
            "climate_state": {"outside_temp": 25.0},
        }

    pushes = []
    monkeypatch.setattr("app.api.routes.notifications.notify",
                        lambda session, title, body, tag=None: pushes.append((title, tag)))

    def fired():
        return len([p for p in pushes if p[1] == "intrusion"])

    t = 1_760_600_000
    try:
        with SessionLocal() as s:
            s.add(Vehicle(vin="TESTVIN-INTRUDE", name="Test", model="Model 3"))
            s.commit()

            def tick(dt, sentry, door_open, user_present=False, locked=True):
                _process_vehicle(s, vdata(t + dt, sentry, door_open, user_present, locked),
                                 {"vin": "TESTVIN-INTRUDE"}, settings)
                s.commit()

            tick(0, True, False)          # parked, armed, all shut -> quiet
            assert fired() == 0
            tick(60, True, True)          # door opens -> fires once
            assert fired() == 1
            tick(120, True, True)         # still open -> no repeat
            assert fired() == 1
            tick(180, True, False)        # shut again -> re-arms
            tick(240, True, True)         # a separate opening -> fires again
            assert fired() == 2

            # Someone aboard is ordinary use, whatever else is true.
            tick(300, True, False)
            tick(360, True, True, True)   # door open but someone is aboard
            assert fired() == 2

            # Sentry off but still LOCKED must now arm — it's being locked that
            # makes an opening anomalous, not Sentry, and requiring Sentry left
            # a car parked locked without it silently unwatched.
            tick(420, False, False)
            tick(480, False, True)
            assert fired() == 3

            # Neither Sentry nor locked: the car was left open on purpose.
            tick(540, False, False, locked=False)
            tick(600, False, True, locked=False)
            assert fired() == 3

            # Every one of those openings is now on record, not just pushed.
            # The alert alone left no trace once dismissed, which is exactly
            # why "when did a real event happen?" kept blocking the
            # Sentry-visibility question (see SecurityEvent).
            from app.models import SecurityEvent
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-INTRUDE").first()
            rows = s.query(SecurityEvent).filter(
                SecurityEvent.vehicle_id == v.id).order_by(SecurityEvent.ts).all()
            assert len(rows) == 3                  # one per fired alert, no more
            assert [r.kind for r in rows] == ["door", "door", "door"]
            assert [r.sentry_mode for r in rows] == [True, True, False]
            assert all(r.locked for r in rows)
    finally:
        with SessionLocal() as s:
            from app.models import Drive as _Drive
            from app.models import SecurityEvent as _Sec
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-INTRUDE").first()
            if v:
                s.query(_Drive).filter(_Drive.vehicle_id == v.id).delete()
                s.query(_Sec).filter(_Sec.vehicle_id == v.id).delete()
                s.query(BatteryReading).filter(BatteryReading.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_plan_route_resolves_destination_to_distance(monkeypatch):
    """The trip-planner route lookup geocodes a typed destination and returns
    the distance from the car's last parked spot, without any live network —
    the geocode/distance helpers are stubbed here."""
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    monkeypatch.setattr("app.api.routes._forward_geocode",
                        lambda q: (5.42, 100.33, f"Resolved: {q}"))
    seen = {}

    def fake_distance(origin, dest, depart_epoch=None):
        seen["depart_epoch"] = depart_epoch
        # Only quote a traffic speed when a departure was actually asked for,
        # mirroring Google (duration_in_traffic needs a future departure_time).
        return (12.3, "driving", 28.0 if depart_epoch else None)

    monkeypatch.setattr("app.api.routes._driving_distance_km", fake_distance)

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    drive_id = None
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                v = s.query(Vehicle).order_by(Vehicle.id).first()
                d = Drive(vehicle_id=v.id, start_time=datetime.now(), end_time=datetime.now(),
                          distance_km=5.0, end_coords="5.40, 100.30", end_location="Office")
                s.add(d); s.commit(); drive_id = d.id

            resp = client.get("/api/plan/route?to=KLCC")
            assert resp.status_code == 200
            body = resp.json()
            assert body["km"] == 12.3
            assert body["method"] == "driving"
            assert body["origin_label"] == "Office"       # from the last drive
            assert body["dest_label"] == "Resolved: KLCC"
            # No departure asked for -> no traffic call, no traffic figure.
            assert seen["depart_epoch"] is None
            assert body["traffic_kmh"] is None

            # With a departure, Google is asked for its traffic prediction and
            # the predicted speed comes back for the planner to price.
            resp = client.get("/api/plan/route?to=KLCC&depart=17:30")
            assert resp.status_code == 200
            assert resp.json()["traffic_kmh"] == 28.0
            # An HH:MM already past today must resolve to the future, since
            # Google rejects a departure_time in the past.
            assert seen["depart_epoch"] > datetime.now().timestamp()

            # A malformed time is ignored rather than failing the lookup.
            resp = client.get("/api/plan/route?to=KLCC&depart=nonsense")
            assert resp.status_code == 200
            assert seen["depart_epoch"] is None

            # A destination that geocodes to nothing -> 404.
            monkeypatch.setattr("app.api.routes._forward_geocode", lambda q: None)
            assert client.get("/api/plan/route?to=zzz").status_code == 404
    finally:
        settings.app_passcode = old
        if drive_id is not None:
            with SessionLocal() as s:
                d = s.get(Drive, drive_id)
                if d:
                    s.delete(d); s.commit()


def test_clear_drives_keeps_charges_and_respects_gate():
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = "secret123"
    try:
        with TestClient(app) as client:  # startup seeds demo data
            # Locked without the passcode cookie.
            assert client.post("/api/data/clear-drives").status_code == 401
            client.post("/login", data={"passcode": "secret123"})
            before = client.get("/api/summary?days=730").json()
            resp = client.post("/api/data/clear-drives")
            assert resp.status_code == 200
            assert resp.json()["deleted_drives"] == before["driving"]["total_drives"]
            after = client.get("/api/summary?days=730").json()
            assert after["driving"]["available"] is False       # trips gone
            assert after["charging"]["total_sessions"] == before["charging"]["total_sessions"]
    finally:
        settings.app_passcode = old
        # Re-seed the demo data so later tests see the usual dataset.
        from app import services
        from app.database import SessionLocal

        with SessionLocal() as s:
            services._wipe(s)
        from app.collector import seed_demo_if_empty

        seed_demo_if_empty()


def test_delete_selected_drives_by_id():
    from app.database import SessionLocal
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            with SessionLocal() as s:
                ids = [d.id for d in s.query(Drive).order_by(Drive.id).limit(3).all()]
                total = s.query(Drive).count()
            resp = client.post("/api/data/delete-drives", json={"ids": ids})
            assert resp.status_code == 200
            assert resp.json()["deleted_drives"] == len(ids)
            with SessionLocal() as s:
                assert s.query(Drive).count() == total - len(ids)
                assert not s.query(Drive).filter(Drive.id.in_(ids)).count()
            # Empty / no ids deletes nothing.
            assert client.post("/api/data/delete-drives", json={"ids": []}).json()["deleted_drives"] == 0
    finally:
        settings.app_passcode = old


def test_reset_tags_clears_every_trip_tag_and_respects_gate():
    """Bulk 'reset tags' clears the Work/Personal tag on every trip back to
    untagged, leaves everything else about each trip untouched, and sits
    behind the passcode gate like every other data-mutating endpoint."""
    from app.database import SessionLocal
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = "secret123"
    try:
        with TestClient(app) as client:  # startup seeds demo data
            with SessionLocal() as s:
                rows = s.query(Drive).order_by(Drive.id).limit(3).all()
                ids = [d.id for d in rows]
                for d in rows:
                    d.tag = "work"
                s.commit()
                orig_distances = {d.id: d.distance_km for d in rows}

            assert client.post("/api/data/reset-tags").status_code == 401
            client.post("/login", data={"passcode": "secret123"})

            resp = client.post("/api/data/reset-tags")
            assert resp.status_code == 200
            assert resp.json()["reset_tags"] == 3

            with SessionLocal() as s:
                for i in ids:
                    d = s.get(Drive, i)
                    assert d.tag == ""                              # cleared
                    assert d.distance_km == orig_distances[i]       # everything else kept

            # Nothing left to clear the second time.
            assert client.post("/api/data/reset-tags").json()["reset_tags"] == 0
    finally:
        settings.app_passcode = old


def test_auto_tag_overwrites_every_trip_from_current_places():
    """Auto-tag sets every trip's Work/Personal tag by matching its
    coordinates against the Office/Home Place right now: Office -> work,
    Home -> personal, neither -> untagged. Places are the single source of
    truth, so this overwrites a manually-set tag that disagrees (including
    resetting one to untagged when the trip matches neither place) — but
    leaves a trip alone (not counted as "changed") when its tag already
    agrees with the current Place match."""
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    ids = {}
    try:
        with TestClient(app) as client:  # startup seeds demo data
            # "My Office", not "Office": the match is by word containment
            # (any Place whose name contains the word office/home), and this
            # exercises that; the exact-named "Home" covers the plain case.
            assert client.post("/api/places", json={
                "name": "My Office", "lat": 5.4000, "lon": 100.4000, "radius_km": 0.2,
            }).status_code == 200
            assert client.post("/api/places", json={
                "name": "Home", "lat": 5.3300, "lon": 100.3000, "radius_km": 0.2,
            }).status_code == 200

            with SessionLocal() as s:
                vehicle_id = s.query(Vehicle).order_by(Vehicle.id).first().id
                office_untagged = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.4001, 100.4001", end_coords="",
                )
                office_already_work = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.4002, 100.4002", end_coords="",
                    tag="work",  # already agrees with the Place -- not a "change"
                )
                office_wrongly_personal = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.4003, 100.4003", end_coords="",
                    tag="personal",  # set by hand -- Office wins and overwrites it
                )
                home_trip = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.3301, 100.3001", end_coords="",
                )
                neither_but_tagged = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.9000, 100.9000", end_coords="",
                    tag="work",  # matches no Place -- reset back to untagged
                )
                s.add_all([office_untagged, office_already_work, office_wrongly_personal,
                          home_trip, neither_but_tagged])
                s.commit()
                ids = {
                    "office_untagged": office_untagged.id, "office_already_work": office_already_work.id,
                    "office_wrongly_personal": office_wrongly_personal.id, "home": home_trip.id,
                    "neither_but_tagged": neither_but_tagged.id,
                }

            resp = client.post("/api/data/auto-tag")
            assert resp.status_code == 200
            # >= 3, not ==: this also sweeps the seeded demo dataset, which
            # may itself have coordinates/tags that shift too -- the precise
            # per-trip behaviour below is what actually matters.
            assert resp.json()["changed"] >= 3   # office_untagged, office_wrongly_personal, neither_but_tagged
            # Both qualifying Places were found ("My Office" counts via word
            # containment) -- the UI uses these to explain a 0-changed run.
            assert resp.json()["office_place"] is True
            assert resp.json()["home_place"] is True

            with SessionLocal() as s:
                assert s.get(Drive, ids["office_untagged"]).tag == "work"
                assert s.get(Drive, ids["office_already_work"]).tag == "work"
                assert s.get(Drive, ids["office_wrongly_personal"]).tag == "work"   # overwritten
                assert s.get(Drive, ids["home"]).tag == "personal"
                assert s.get(Drive, ids["neither_but_tagged"]).tag == ""            # reset

            # Nothing left to change the second time.
            assert client.post("/api/data/auto-tag").json()["changed"] == 0
    finally:
        settings.app_passcode = old
        with SessionLocal() as s:
            real_ids = [i for i in ids.values() if i is not None]
            if real_ids:
                s.query(Drive).filter(Drive.id.in_(real_ids)).delete(synchronize_session=False)
                s.commit()
            from app.models import Place
            s.query(Place).delete()
            s.commit()
        from app import services
        from app.database import SessionLocal as SL
        with SL() as s:
            services._wipe(s)
        from app.collector import seed_demo_if_empty
        seed_demo_if_empty()


def test_clear_charges_keeps_drives_and_respects_gate():
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = "secret123"
    try:
        with TestClient(app) as client:  # startup seeds demo data
            # Locked without the passcode cookie.
            assert client.post("/api/data/clear-charges").status_code == 401
            client.post("/login", data={"passcode": "secret123"})
            before = client.get("/api/summary?days=730").json()
            resp = client.post("/api/data/clear-charges")
            assert resp.status_code == 200
            assert resp.json()["deleted_charges"] == before["charging"]["total_sessions"]
            after = client.get("/api/summary?days=730").json()
            assert after["charging"]["available"] is False       # charges gone
            assert after["driving"]["total_drives"] == before["driving"]["total_drives"]
    finally:
        settings.app_passcode = old
        # Re-seed the demo data so later tests see the usual dataset.
        from app import services
        from app.database import SessionLocal

        with SessionLocal() as s:
            services._wipe(s)
        from app.collector import seed_demo_if_empty

        seed_demo_if_empty()


def test_delete_selected_charges_by_id():
    from app.database import SessionLocal
    from app.models import Charge

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            with SessionLocal() as s:
                ids = [c.id for c in s.query(Charge).order_by(Charge.id).limit(3).all()]
                total = s.query(Charge).count()
            resp = client.post("/api/data/delete-charges", json={"ids": ids})
            assert resp.status_code == 200
            assert resp.json()["deleted_charges"] == len(ids)
            with SessionLocal() as s:
                assert s.query(Charge).count() == total - len(ids)
                assert not s.query(Charge).filter(Charge.id.in_(ids)).count()
            # Empty / no ids deletes nothing.
            assert client.post("/api/data/delete-charges", json={"ids": []}).json()["deleted_charges"] == 0
    finally:
        settings.app_passcode = old
        from app import services
        from app.database import SessionLocal as SL
        with SL() as s:
            services._wipe(s)
        from app.collector import seed_demo_if_empty
        seed_demo_if_empty()


def test_tag_drive_endpoint():
    from app.database import SessionLocal
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            with SessionLocal() as s:
                drive_id = s.query(Drive).order_by(Drive.id).first().id

            resp = client.post("/api/data/tag-drive", json={"id": drive_id, "tag": "work"})
            assert resp.status_code == 200
            assert resp.json() == {"id": drive_id, "tag": "work"}
            with SessionLocal() as s:
                assert s.get(Drive, drive_id).tag == "work"

            # Clearing (empty tag) works too.
            client.post("/api/data/tag-drive", json={"id": drive_id, "tag": ""})
            with SessionLocal() as s:
                assert s.get(Drive, drive_id).tag == ""

            # Unknown id -> 404, not a silent no-op.
            assert client.post("/api/data/tag-drive", json={"id": 9_999_999, "tag": "work"}).status_code == 404
            # Missing id -> 400.
            assert client.post("/api/data/tag-drive", json={"tag": "work"}).status_code == 400
    finally:
        settings.app_passcode = old


def test_set_drive_cost_endpoint():
    from app.database import SessionLocal
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            with SessionLocal() as s:
                drive_id = s.query(Drive).order_by(Drive.id).first().id
                assert s.get(Drive, drive_id).cost_override is None

            resp = client.post("/api/data/set-drive-cost", json={"id": drive_id, "cost": 4.5})
            assert resp.status_code == 200
            assert resp.json() == {"id": drive_id, "cost_override": 4.5}
            with SessionLocal() as s:
                assert s.get(Drive, drive_id).cost_override == 4.5

            # Clearing (cost omitted/null) reverts to automatic pricing.
            client.post("/api/data/set-drive-cost", json={"id": drive_id, "cost": None})
            with SessionLocal() as s:
                assert s.get(Drive, drive_id).cost_override is None

            # Validation: negative and non-numeric costs are rejected.
            assert client.post(
                "/api/data/set-drive-cost", json={"id": drive_id, "cost": -1}).status_code == 400
            assert client.post(
                "/api/data/set-drive-cost", json={"id": drive_id, "cost": "abc"}).status_code == 400
            # Unknown id -> 404, not a silent no-op.
            assert client.post(
                "/api/data/set-drive-cost", json={"id": 9_999_999, "cost": 1.0}).status_code == 404
            # Missing id -> 400.
            assert client.post("/api/data/set-drive-cost", json={"cost": 1.0}).status_code == 400
    finally:
        settings.app_passcode = old


def test_edit_drive_endpoint():
    """Manually correcting a trip's start/end time (a no-signal park/departure
    the sync-time estimate still got wrong) must recompute duration/avg speed
    from the new times, while leaving distance/energy exactly as recorded --
    those come from the odometer/SoC readings at the trip's edges, not the
    clock. Also covers the validation paths: end <= start, unknown id, and
    an edit that would overlap another logged trip."""
    from app.database import SessionLocal
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            with SessionLocal() as s:
                # Demo data isn't guaranteed gap-free between every pair, so
                # pick a trip with a real gap on both sides -- shrinking its
                # own window by 2 minutes then can't newly overlap anything.
                rows = s.query(Drive).order_by(Drive.start_time).all()
                drive = next(
                    d for i, d in enumerate(rows)
                    if 0 < i < len(rows) - 1
                    and rows[i - 1].end_time < d.start_time
                    and d.end_time < rows[i + 1].start_time
                )
                nxt_start = next(r.start_time for r in rows if r.start_time > drive.end_time)
                drive_id = drive.id
                orig_distance = drive.distance_km
                orig_energy = drive.energy_used_kwh
                # Shrink the window from the start side by 2 minutes -- still
                # fully inside the original span, so this can't newly overlap
                # anything that didn't already overlap the original trip.
                new_start = (drive.start_time + timedelta(minutes=2)).isoformat(timespec="minutes")
                end_iso = drive.end_time.isoformat(timespec="minutes")

            resp = client.post("/api/data/edit-drive",
                                json={"id": drive_id, "start_time": new_start, "end_time": end_iso})
            assert resp.status_code == 200
            body = resp.json()
            assert body["start_time"] == new_start
            with SessionLocal() as s:
                d = s.get(Drive, drive_id)
                assert d.start_time.isoformat(timespec="minutes") == new_start
                # Distance/energy untouched -- they come from the odometer/SoC
                # readings, not the clock.
                assert d.distance_km == orig_distance
                assert d.energy_used_kwh == orig_energy
                # Duration/avg speed recalculated from the new (shorter) span.
                expected_min = round((d.end_time - d.start_time).total_seconds() / 60.0, 1)
                assert d.duration_min == expected_min
                assert d.avg_speed_kmh == round(d.distance_km / (expected_min / 60.0), 1)

            # End <= start is rejected.
            bad = client.post("/api/data/edit-drive",
                               json={"id": drive_id, "start_time": end_iso, "end_time": new_start})
            assert bad.status_code == 400

            # Unknown id -> 404.
            assert client.post("/api/data/edit-drive",
                                json={"id": 9_999_999, "start_time": new_start}).status_code == 404
            # Missing id -> 400.
            assert client.post("/api/data/edit-drive", json={"start_time": new_start}).status_code == 400
            # Neither field given -> 400.
            assert client.post("/api/data/edit-drive", json={"id": drive_id}).status_code == 400

            # Stretching a trip's end out to swallow the next trip's start
            # is rejected as an overlap, not silently corrupting both rows.
            overlap_end = (nxt_start + timedelta(minutes=5)).isoformat(timespec="minutes")
            resp = client.post("/api/data/edit-drive", json={"id": drive_id, "end_time": overlap_end})
            assert resp.status_code == 400
    finally:
        settings.app_passcode = old


def test_places_crud_and_geofenced_labeling():
    """Defining a named place (a) is usable going forward via _place_and_area
    and (b) retroactively relabels already-logged trips whose stored coords
    fall inside its radius, without touching trips elsewhere."""
    from app.database import SessionLocal
    from app.models import Drive, Place, Vehicle

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            assert client.get("/api/places").json() == []

            with SessionLocal() as s:
                vehicle_id = s.query(Vehicle).order_by(Vehicle.id).first().id
                near = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.3300, 100.3000", end_coords="",
                    start_location="Some Street", start_area="Some Street",
                )
                far = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.5000, 100.5000", end_coords="",
                    start_location="Far Street", start_area="Far Street",
                )
                s.add_all([near, far])
                s.commit()
                near_id, far_id = near.id, far.id

            resp = client.post("/api/places", json={
                "name": "Home", "lat": 5.3301, "lon": 100.3001, "radius_km": 0.2,
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["name"] == "Home"
            assert body["relabeled"] == 1     # only the nearby trip

            with SessionLocal() as s:
                assert s.get(Drive, near_id).start_location == "Home"
                assert s.get(Drive, near_id).start_area == "Home"
                assert s.get(Drive, far_id).start_location == "Far Street"  # untouched

            places = client.get("/api/places").json()
            assert len(places) == 1 and places[0]["name"] == "Home"

            # A coordinate inside the geofence resolves to the place name
            # without any network geocode.
            from app.api.routes import _place_and_area
            with SessionLocal() as s:
                label, area = _place_and_area("5.3300, 100.3000", s)
            assert label == "Home" and area == "Home"

            place_id = places[0]["id"]
            assert client.delete(f"/api/places/{place_id}").status_code == 200
            assert client.get("/api/places").json() == []
            assert client.delete(f"/api/places/{place_id}").status_code == 404

            # Validation.
            assert client.post("/api/places", json={"lat": 1.0, "lon": 2.0}).status_code == 400
            assert client.post("/api/places", json={"name": "X", "lat": "nope", "lon": 2.0}).status_code == 400
    finally:
        settings.app_passcode = old


def test_relabel_selected_drives_refreshes_stale_labels_only(monkeypatch):
    """Reset locations (selected ids) re-runs the normal lookup from each
    trip's stored raw coordinates and overwrites the stale label — a trip
    with no stored coords (logged before that column existed) is left alone
    since there's nothing to look it up from. Also clears the Work/Personal
    tag on any trip it actually relabels (a location reset is a clean slate
    for the trip's place-derived identity), but a skipped trip keeps its tag
    too, same as it keeps its stale location."""
    from app.api import routes
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    stale_id = no_coords_id = None
    try:
        with TestClient(app) as client:  # startup seeds demo data
            with SessionLocal() as s:
                vehicle_id = s.query(Vehicle).order_by(Vehicle.id).first().id
                stale = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=5.0, start_coords="5.9000, 100.9000", end_coords="",
                    start_location="Stale Old Label", start_area="Stale Old Label",
                    tag="work",
                )
                no_coords = Drive(
                    vehicle_id=vehicle_id, start_time=datetime.now(), end_time=datetime.now(),
                    distance_km=3.0, start_coords="", end_coords="",
                    start_location="Untouchable", start_area="Untouchable",
                    tag="personal",
                )
                s.add_all([stale, no_coords])
                s.commit()
                stale_id, no_coords_id = stale.id, no_coords.id

            routes._PLACE_CACHE.clear()
            monkeypatch.setattr(
                routes.httpx, "get",
                lambda *a, **k: type("R", (), {
                    "raise_for_status": lambda self: None,
                    "json": lambda self: {"address": {"road": "Fresh Road", "city": "Freshville"}},
                })(),
            )

            resp = client.post("/api/data/relabel-drives", json={"ids": [stale_id, no_coords_id]})
            assert resp.status_code == 200
            assert resp.json() == {"relabeled": 1, "skipped": 1}

            with SessionLocal() as s:
                assert s.get(Drive, stale_id).start_location == "Fresh Road, Freshville"
                assert s.get(Drive, stale_id).tag == ""                          # tag cleared too
                assert s.get(Drive, no_coords_id).start_location == "Untouchable"  # nothing to look up from
                assert s.get(Drive, no_coords_id).tag == "personal"              # skipped -> tag kept too

            assert client.post("/api/data/relabel-drives", json={"ids": []}).json() == \
                {"relabeled": 0, "skipped": 0}
    finally:
        settings.app_passcode = old
        with SessionLocal() as s:
            ids = [i for i in (stale_id, no_coords_id) if i is not None]
            if ids:
                s.query(Drive).filter(Drive.id.in_(ids)).delete(synchronize_session=False)
                s.commit()


def test_relabel_all_drives_endpoint_gated():
    """Bulk 'reset all' sits behind the passcode gate like every other
    data-mutating endpoint, and always returns the relabeled/skipped shape."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = "secret123"
    try:
        with TestClient(app) as client:
            assert client.post("/api/data/relabel-all-drives").status_code == 401
            client.post("/login", data={"passcode": "secret123"})
            resp = client.post("/api/data/relabel-all-drives")
            assert resp.status_code == 200
            assert set(resp.json()) == {"relabeled", "skipped"}
    finally:
        settings.app_passcode = old


def test_manual_charge_logs_a_historical_session_additively():
    """A manually-logged charge is inserted for the active vehicle without
    touching any other data — the safe alternative to /api/import (which
    wipes and replaces everything) for backfilling one missed session."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            # This test is about the AC/DC rate math specifically, not about
            # which source a charge defaults to when nothing else matches —
            # pin it explicitly so it stays independent of that default
            # (currently "home").
            client.post("/api/pricing-prefs", json={"rates": {}, "default_source": "public"})

            before = client.get("/api/summary?days=730").json()
            before_drives = before["driving"]["total_drives"]
            before_sessions = before["charging"]["total_sessions"]

            resp = client.post("/api/charges/manual", json={
                "start_time": "2025-01-01T22:00:00", "end_time": "2025-01-02T05:00:00",
                "energy_added_kwh": 30.0, "charge_type": "AC",
                "start_soc": 40, "end_soc": 90, "location": "Home",
            })
            assert resp.status_code == 200
            charge_id = resp.json()["id"]

            after = client.get("/api/summary?days=730").json()
            assert after["driving"]["total_drives"] == before_drives   # untouched
            assert after["charging"]["total_sessions"] == before_sessions + 1

            from app.database import SessionLocal
            from app.models import Charge
            with SessionLocal() as s:
                c = s.get(Charge, charge_id)
                assert c.energy_added_kwh == 30.0
                assert c.duration_min == 420.0
                # Auto-computed from the configured AC rate (charge_type: "AC" above).
                assert c.cost == round(30.0 * settings.energy_price_ac_kwh, 2)
                s.delete(c)   # tidy up so this doesn't skew later tests' totals
                s.commit()

            # A DC session auto-costs at the DC rate instead.
            resp_dc = client.post("/api/charges/manual", json={
                "start_time": "2025-01-05T12:00:00", "end_time": "2025-01-05T12:30:00",
                "energy_added_kwh": 20.0, "charge_type": "DC",
                "start_soc": 20, "end_soc": 60, "location": "Supercharger",
            })
            assert resp_dc.status_code == 200
            with SessionLocal() as s:
                c_dc = s.get(Charge, resp_dc.json()["id"])
                assert c_dc.cost == round(20.0 * settings.energy_price_dc_kwh, 2)
                s.delete(c_dc)
                s.commit()

            # is_free (e.g. a Tesla Destination Charger) overrides the auto
            # rate AND an explicit cost — no telemetry field distinguishes
            # these from a paid AC charger, so it's a manual flag.
            resp_free = client.post("/api/charges/manual", json={
                "start_time": "2025-01-10T18:00:00", "end_time": "2025-01-10T20:00:00",
                "energy_added_kwh": 15.0, "charge_type": "AC", "is_free": True,
                "cost": 99.0, "location": "Hotel Destination Charger",
            })
            assert resp_free.status_code == 200
            with SessionLocal() as s:
                c_free = s.get(Charge, resp_free.json()["id"])
                assert c_free.is_free is True
                assert c_free.cost == 0.0
                s.delete(c_free)
                s.commit()

            # Validation.
            assert client.post("/api/charges/manual", json={
                "end_time": "2025-01-02T05:00:00", "energy_added_kwh": 10,
            }).status_code == 400   # missing start_time
            assert client.post("/api/charges/manual", json={
                "start_time": "2025-01-02T05:00:00", "end_time": "2025-01-01T22:00:00",
                "energy_added_kwh": 10,
            }).status_code == 400   # end before start
            assert client.post("/api/charges/manual", json={
                "start_time": "2025-01-01T22:00:00", "end_time": "2025-01-02T05:00:00",
                "energy_added_kwh": 0,
            }).status_code == 400   # zero energy
            assert client.post("/api/charges/manual", json={
                "start_time": "2025-01-01T22:00:00", "end_time": "2025-01-02T05:00:00",
                "energy_added_kwh": 10, "charge_type": "GAS",
            }).status_code == 400   # invalid charge_type

            # An explicit cost overrides the tariff-computed one.
            resp2 = client.post("/api/charges/manual", json={
                "start_time": "2025-02-01T22:00:00", "end_time": "2025-02-02T05:00:00",
                "energy_added_kwh": 10.0, "cost": 4.5,
            })
            assert resp2.status_code == 200
            with SessionLocal() as s:
                c2 = s.get(Charge, resp2.json()["id"])
                assert c2.cost == 4.5
                s.delete(c2)
                s.commit()
    finally:
        settings.app_passcode = old


def test_edit_charge_rate_recalculates_cost():
    """A session priced differently from the configured AC/DC default (a
    promo rate, a pricier one-off public charger, ...) can be fixed by
    supplying its actual per-kWh rate — the new cost is energy * that rate,
    overriding whatever the sync/manual-entry auto-calc originally set."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            resp = client.post("/api/charges/manual", json={
                "start_time": "2025-03-01T20:00:00", "end_time": "2025-03-01T22:00:00",
                "energy_added_kwh": 10.0, "charge_type": "AC",
            })
            charge_id = resp.json()["id"]

            from app.database import SessionLocal
            from app.models import Charge

            # A promo rate of 0.5/kWh instead of the AC default.
            edit = client.post("/api/charges/edit-rate", json={
                "id": charge_id, "price_per_kwh": 0.5,
            })
            assert edit.status_code == 200
            assert edit.json() == {"id": charge_id, "cost": 5.0, "is_free": False, "source": None}
            with SessionLocal() as s:
                c = s.get(Charge, charge_id)
                assert c.cost == 5.0
                assert c.is_free is False

            # The dashboard's 🏠 quick-rate button passes a source, which
            # persists so the selected-icon indicator survives rate changes.
            edit_home = client.post("/api/charges/edit-rate", json={
                "id": charge_id, "price_per_kwh": 0.44, "source": "home",
            })
            assert edit_home.status_code == 200
            assert edit_home.json()["source"] == "home"
            with SessionLocal() as s:
                c = s.get(Charge, charge_id)
                assert c.price_source == "home"

            # "other" (the dashboard's 🏷️ Others button — a fully custom
            # rate, not one of the three configured presets) is also valid.
            edit_other = client.post("/api/charges/edit-rate", json={
                "id": charge_id, "price_per_kwh": 0.62, "source": "other",
            })
            assert edit_other.status_code == 200
            assert edit_other.json()["source"] == "other"

            # An invalid source is rejected outright.
            assert client.post("/api/charges/edit-rate", json={
                "id": charge_id, "price_per_kwh": 0.5, "source": "garage",
            }).status_code == 400

            # 0 doubles as marking it free.
            edit_free = client.post("/api/charges/edit-rate", json={
                "id": charge_id, "price_per_kwh": 0,
            })
            assert edit_free.status_code == 200
            with SessionLocal() as s:
                c = s.get(Charge, charge_id)
                assert c.cost == 0.0
                assert c.is_free is True
                s.delete(c)
                s.commit()

            # Validation.
            assert client.post("/api/charges/edit-rate", json={
                "id": 999999, "price_per_kwh": 1.0,
            }).status_code == 404   # unknown charge
            assert client.post("/api/charges/edit-rate", json={
                "price_per_kwh": 1.0,
            }).status_code == 400   # missing id
            assert client.post("/api/charges/edit-rate", json={
                "id": charge_id, "price_per_kwh": -1,
            }).status_code == 400   # negative rate
    finally:
        settings.app_passcode = old


def test_edit_charge_location_renames_one_or_every_session_there():
    """The geocoder names a charger after whatever it resolved at the time —
    the shop next door, or bare coordinates when it resolved nothing. Renaming
    fixes the label (and, on request, every session sharing it) without
    touching what the session cost."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    from app.database import SessionLocal
    from app.models import Charge

    def _add(client, day, location):
        return client.post("/api/charges/manual", json={
            "start_time": f"2025-04-0{day}T20:00:00", "end_time": f"2025-04-0{day}T22:00:00",
            "energy_added_kwh": 10.0, "charge_type": "AC", "location": location,
        }).json()["id"]

    try:
        with TestClient(app) as client:
            # Two sessions at the same badly-named spot, one elsewhere, and
            # one the geocoder never resolved at all.
            a = _add(client, 1, "Kedai Runcit Ali")
            b = _add(client, 2, "Kedai Runcit Ali")
            other = _add(client, 3, "Office")
            blank = _add(client, 4, "")
            with SessionLocal() as s:
                cost_before = s.get(Charge, a).cost

            # Renaming just this one leaves its twin alone.
            resp = client.post("/api/charges/edit-location", json={
                "id": a, "location": "Sunway Pyramid DC",
            })
            assert resp.status_code == 200
            assert resp.json() == {"id": a, "location": "Sunway Pyramid DC", "updated": 1}
            with SessionLocal() as s:
                assert s.get(Charge, a).location == "Sunway Pyramid DC"
                assert s.get(Charge, b).location == "Kedai Runcit Ali"
                # A rename is not a repricing.
                assert s.get(Charge, a).cost == cost_before

            # apply_all sweeps up every session still carrying the old label,
            # and nothing else.
            resp = client.post("/api/charges/edit-location", json={
                "id": b, "location": "Sunway Pyramid DC", "apply_all": True,
            })
            assert resp.status_code == 200
            assert resp.json()["updated"] == 1   # only b still had the old name
            with SessionLocal() as s:
                assert s.get(Charge, b).location == "Sunway Pyramid DC"
                assert s.get(Charge, other).location == "Office"

            # Both now share a label, so apply_all from either renames both.
            resp = client.post("/api/charges/edit-location", json={
                "id": a, "location": "Pyramid Supercharger", "apply_all": True,
            })
            assert resp.json()["updated"] == 2
            with SessionLocal() as s:
                assert s.get(Charge, a).location == "Pyramid Supercharger"
                assert s.get(Charge, b).location == "Pyramid Supercharger"

            # A blank label isn't something sessions have "in common" — naming
            # an unresolved charge must not drag every other unnamed one along.
            blank2 = _add(client, 5, "")
            resp = client.post("/api/charges/edit-location", json={
                "id": blank, "location": "Back lane", "apply_all": True,
            })
            assert resp.json()["updated"] == 1
            with SessionLocal() as s:
                assert s.get(Charge, blank).location == "Back lane"
                assert s.get(Charge, blank2).location == ""

            # Validation: a name is required (clearing one would throw away
            # the raw coordinates, the only geographic record a Charge keeps).
            assert client.post("/api/charges/edit-location", json={
                "id": a, "location": "   ",
            }).status_code == 400
            assert client.post("/api/charges/edit-location", json={
                "location": "Home",
            }).status_code == 400   # missing id
            assert client.post("/api/charges/edit-location", json={
                "id": 999999, "location": "Home",
            }).status_code == 404   # unknown charge

            with SessionLocal() as s:
                for cid in (a, b, other, blank, blank2):
                    s.delete(s.get(Charge, cid))
                s.commit()
    finally:
        settings.app_passcode = old


def test_recent_charges_carry_the_stored_location_alongside_the_shown_one():
    """The Recent Charges rows expose location_raw so the rename button can
    tell a charge's own label from one inferred for it from a nearby trip —
    only the former can be bulk-renamed across sessions."""
    from app.analysis import charging as charging_analysis
    from app.models import Charge

    charge = Charge(
        vehicle_id=1,
        start_time=datetime(2025, 4, 1, 20, 0), end_time=datetime(2025, 4, 1, 22, 0),
        duration_min=120.0, start_soc=40.0, end_soc=80.0, energy_added_kwh=28.0,
        charge_type="AC", max_power_kw=7.0, location="3.1234, 101.5678", cost=25.0,
    )
    stats = charging_analysis.analyze([charge], drives=[])
    row = stats["recent_charges"][0]
    assert row["location_raw"] == "3.1234, 101.5678"
    assert row["location"] == "3.1234, 101.5678"   # nothing to infer from


def test_pricing_prefs_home_defaults_before_first_save():
    """Before the Rates page has ever been saved, Home defaults to AC RM0.90
    / DC RM1.13 per kWh — not the generic TNB-ToU-average placeholder."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    from app import state
    from app.database import SessionLocal

    keys = (
        state.PRICE_PUBLIC_AC_KEY, state.PRICE_PUBLIC_DC_KEY,
        state.PRICE_HOME_AC_KEY, state.PRICE_HOME_DC_KEY,
        state.PRICE_OFFICE_AC_KEY, state.PRICE_OFFICE_DC_KEY,
        state.DEFAULT_PRICE_SOURCE_KEY, state.PRICE_UPDATED_AT_KEY,
    )
    try:
        with SessionLocal() as s:
            state.delete(s, *keys)
        with TestClient(app) as client:  # startup seeds demo data
            rates = client.get("/api/pricing-prefs").json()["rates"]
            assert rates["home_ac"] == 0.90
            assert rates["home_dc"] == 1.13
    finally:
        settings.app_passcode = old
        with SessionLocal() as s:
            state.delete(s, *keys)


def test_pricing_prefs_updated_at_tracks_last_save():
    """No live TNB/public-charger rate feed exists to auto-refresh from, so
    the Rates page shows when the numbers were last saved instead — None
    until the first save, then today's date, persisting across a fresh
    GET."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    from app import state
    from app.database import SessionLocal

    keys = (
        state.PRICE_PUBLIC_AC_KEY, state.PRICE_PUBLIC_DC_KEY,
        state.PRICE_HOME_AC_KEY, state.PRICE_HOME_DC_KEY,
        state.PRICE_OFFICE_AC_KEY, state.PRICE_OFFICE_DC_KEY,
        state.DEFAULT_PRICE_SOURCE_KEY, state.PRICE_UPDATED_AT_KEY,
    )
    try:
        with SessionLocal() as s:
            state.delete(s, *keys)
        with TestClient(app) as client:  # startup seeds demo data
            before = client.get("/api/pricing-prefs").json()
            assert before["updated_at"] is None

            resp = client.post("/api/pricing-prefs", json={
                "rates": {
                    "public_ac": 1.0, "public_dc": 1.5,
                    "home_ac": 0.44, "home_dc": 0.44,
                    "office_ac": 0.57, "office_dc": 0.57,
                },
                "default_source": "public",
            })
            assert resp.status_code == 200
            # The owner's date, not the server's. date.today() reads the host
            # zone, so this assertion only held for the 16 hours a day the two
            # happened to agree — and the value under test is exactly what the
            # Rates page shows the owner, which must be their date.
            from app.sync import now_local
            today = now_local().date().isoformat()
            assert resp.json()["updated_at"] == today
            assert client.get("/api/pricing-prefs").json()["updated_at"] == today
    finally:
        settings.app_passcode = old
        with SessionLocal() as s:
            state.delete(s, *keys)


def test_service_crud_and_due_status():
    """Logging a service record (a) persists and lists back, (b) feeds the
    due/overdue projection, and (c) can be deleted."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            body = client.get("/api/service").json()
            assert body["records"] == []
            assert all(r["status"] == "unknown" for r in body["due"])
            assert "Tire Rotation" in body["types"]

            resp = client.post("/api/service", json={
                "type": "Tire Rotation", "date": "2026-01-01T00:00:00",
                "odo_km": 15000, "cost": 40.0, "notes": "Front to back",
            })
            assert resp.status_code == 200
            record_id = resp.json()["id"]

            body = client.get("/api/service").json()
            assert len(body["records"]) == 1
            assert body["records"][0]["type"] == "Tire Rotation"
            assert body["records"][0]["cost"] == 40.0
            rotation = next(r for r in body["due"] if r["type"] == "Tire Rotation")
            assert rotation["status"] != "unknown"
            assert rotation["due_odo_km"] == 25000.0

            # Validation.
            assert client.post("/api/service", json={"odo_km": 1}).status_code == 400
            assert client.post("/api/service", json={"type": "X", "date": "not-a-date"}).status_code == 400

            assert client.delete(f"/api/service/{record_id}").status_code == 200
            assert client.get("/api/service").json()["records"] == []
            assert client.delete(f"/api/service/{record_id}").status_code == 404
    finally:
        settings.app_passcode = old


def test_live_eta_projects_distance_time_and_soc_to_nearest_place():
    """A live drive's ETA/projected SoC picks the nearest named place not
    already reached, and returns nothing when the car is already there or no
    place is defined at all."""
    from app.api.routes import _live_eta
    from app.database import SessionLocal
    from app.models import Place

    with SessionLocal() as s:
        s.add(Place(name="Office", lat=5.4000, lon=100.4000, radius_km=0.15,
                     created_at=datetime.now()))
        s.add(Place(name="Home", lat=5.3300, lon=100.3000, radius_km=0.15,
                     created_at=datetime.now()))
        s.commit()

        snap = {"lat": 5.3350, "lon": 100.3050}  # ~600 m from Home, outside its radius
        live = {"soc": 70.0, "avg_speed_kmh": 40.0, "driving_wh_per_km": 150.0}
        eta = _live_eta(s, snap, live, capacity_kwh=60.0)
        assert eta is not None
        assert eta["place"] == "Home"          # nearer than Office
        assert eta["distance_km"] < 1.0
        assert eta["eta_min"] >= 0
        assert eta["projected_soc"] is not None and eta["projected_soc"] <= 70.0

        # Already inside Home's radius -> Home excluded, Office (far) picked instead.
        snap_at_home = {"lat": 5.3300, "lon": 100.3000}
        eta2 = _live_eta(s, snap_at_home, live, capacity_kwh=60.0)
        assert eta2 is not None and eta2["place"] == "Office"

        # No GPS on the snapshot -> no projection possible.
        assert _live_eta(s, {"lat": None, "lon": None}, live, 60.0) is None

    # No places defined at all -> nothing to project toward.
    with SessionLocal() as s:
        for p in s.query(Place).all():
            s.delete(p)
        s.commit()
        assert _live_eta(s, {"lat": 5.33, "lon": 100.30}, live, 60.0) is None


def test_summary_current_drive_falls_back_to_last_drive():
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data, no open trip
            body = client.get("/api/summary?current_drive=1").json()
            assert body["window_label"] == "last drive"
            assert body["live_trip"] is None
            # The window is anchored at the newest drive: exactly one drive in it.
            assert body["driving"]["total_drives"] == 1
            trip = body["driving"]["recent_trips"][0]
            assert "end_time" in trip and "avg_speed_kmh" in trip
            # km per 1% battery is reported alongside the other driving stats.
            full = client.get("/api/summary?days=365").json()
            assert full["driving"]["km_per_soc_pct"] > 0
            # The export honours the same window.
            resp = client.get("/api/export/csv?current_drive=1")
            assert "current-drive" in resp.headers["content-disposition"]
    finally:
        settings.app_passcode = old


def test_export_csv_round_trips_through_importer():
    from app.importer import parse_upload

    with TestClient(app) as client:  # startup seeds demo data
        resp = client.get("/api/export/csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "attachment" in resp.headers["content-disposition"]
        drives, charges = parse_upload("export.zip", resp.content)
        summary = client.get("/api/summary?days=730").json()
        assert len(drives) == summary["driving"]["total_drives"]
        assert len(charges) == summary["charging"]["total_sessions"]
        # Windowed export contains a strict subset and labels the filename.
        resp7 = client.get("/api/export/csv?days=7")
        d7, c7 = parse_upload("export7.zip", resp7.content)
        assert len(d7) < len(drives)
        assert "7d" in resp7.headers["content-disposition"]
        respsc = client.get("/api/export/csv?since_charge=1")
        assert "since-charge" in respsc.headers["content-disposition"]


def test_export_zip_ships_analysis_sheets_without_breaking_reimport():
    """The full export carries every dashboard section as its own sheet under
    analysis/, while drives.csv/charges.csv stay the re-importable pair — the
    derived sheets must not be parsed back in (recent-trips.csv restates the
    same drives, so importing it too would double every trip)."""
    import zipfile
    from io import BytesIO

    from app.api.routes import EXPORT_SECTIONS
    from app.importer import parse_upload

    with TestClient(app) as client:  # startup seeds demo data
        resp = client.get("/api/export/csv?days=30")
        assert resp.status_code == 200
        names = set(zipfile.ZipFile(BytesIO(resp.content)).namelist())
        assert {"drives.csv", "charges.csv"} <= names
        # Every section ships, namespaced so it can't collide with the raw pair.
        for _key, (stem, _label) in EXPORT_SECTIONS.items():
            assert f"analysis/{stem}.csv" in names
        # Re-import still sees only the raw rows, not the derived sheets.
        drives, charges = parse_upload("export.zip", resp.content)
        summary = client.get("/api/summary?days=30").json()
        assert len(drives) == summary["driving"]["total_drives"]


def test_export_section_returns_one_csv_for_a_known_section():
    with TestClient(app) as client:
        resp = client.get("/api/export/section?name=trips&days=30")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "recent-trips-30d.csv" in resp.headers["content-disposition"]
        assert resp.text.splitlines()[0].startswith("start_time,end_time,route")

        # Window params flow through to the filename.
        assert "since-charge" in client.get(
            "/api/export/section?name=kpis&since_charge=1"
        ).headers["content-disposition"]
        # Unknown sections are a clean 404, not a stack trace.
        assert client.get("/api/export/section?name=nope").status_code == 404


def test_no_passcode_means_open():
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 200
            assert client.get(PEM_PATH).status_code == 200
    finally:
        settings.app_passcode = old


def test_departure_pace_is_per_place_and_survives_an_edit():
    """A place's departure pace is set from outside evidence (the car's own
    Trips screen), so the two ways to lose it both matter: the editor must not
    silently clear it by omission, and the geofence lookup must find it from
    the parked coordinates alone."""
    from app.database import SessionLocal
    from app.models import Place

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            client.post("/api/places", json={
                "name": "Home", "lat": 5.3301, "lon": 100.3001, "radius_km": 0.2,
            })
            assert client.get("/api/places").json()[0]["departure_pace_kmh"] == 0.0

            # Unknown place names fail loudly rather than setting nothing.
            assert client.get("/api/set-departure-pace",
                              params={"place": "Nowhere", "kmh": 45}).status_code == 404
            # A typo'd unit (m/s, or mph read as km/h) is caught, not stored.
            assert client.get("/api/set-departure-pace",
                              params={"place": "Home", "kmh": 450}).status_code == 400

            body = client.get("/api/set-departure-pace",
                              params={"place": "home", "kmh": 45}).json()  # name is case-insensitive
            assert body["place"] == "Home" and body["departure_pace_kmh"] == 45.0
            assert body["was"] == 0.0

            # Moving the geofence from the places editor — which posts no pace
            # field at all — must leave the setting alone.
            client.post("/api/places", json={
                "name": "Home", "lat": 5.3302, "lon": 100.3002, "radius_km": 0.25,
            })
            assert client.get("/api/places").json()[0]["departure_pace_kmh"] == 45.0

            # And the sync path finds it from coordinates inside the fence.
            from app.api.routes import _place_departure_pace
            with SessionLocal() as s:
                inside = {"lat": 5.3301, "lon": 100.3001}
                outside = {"lat": 5.5000, "lon": 100.5000}
                assert _place_departure_pace(s, inside) == 45.0
                assert _place_departure_pace(s, outside) is None
                assert _place_departure_pace(s, None) is None
                assert _place_departure_pace(s, {"lat": None, "lon": None}) is None

            # 0 is how you go back to the global default.
            assert client.get("/api/set-departure-pace",
                              params={"place": "Home", "kmh": 0}).json()[
                                  "departure_pace_kmh"] == 0.0
            with SessionLocal() as s:
                assert _place_departure_pace(s, {"lat": 5.3301, "lon": 100.3001}) is None
                s.query(Place).delete()
                s.commit()
    finally:
        settings.app_passcode = old


def test_drive_boundary_reports_both_facing_edges():
    """A boundary question is about a PAIR of trips, so the endpoint has to
    carry the previous trip's closing edge alongside this one's opening — the
    browser-side diagnostics never did, which is why a misattributed handover
    could not be checked without the car's own trip meter."""
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                # Its own vehicle: the endpoint scopes neighbours by
                # vehicle_id, so this isolates the chain from the seeded demo
                # history and from whatever other tests have logged.
                car = Vehicle(vin="BOUNDARY-TEST", name="Boundary", model="Tesla")
                s.add(car)
                s.flush()
                vid = car.id
                base = datetime(2026, 8, 17, 9, 0)
                rows = []
                for i, (odo_a, odo_b) in enumerate(
                        [(100.0, 110.0), (110.0, 121.0), (121.0, 130.0)]):
                    d = Drive(
                        vehicle_id=vid,
                        start_time=base + timedelta(hours=2 * i),
                        end_time=base + timedelta(hours=2 * i, minutes=20),
                        distance_km=odo_b - odo_a, duration_min=20.0,
                        start_odo_km=odo_a, end_odo_km=odo_b,
                        end_gap_sec=900.0, start_recovered_km=6.4,
                    )
                    s.add(d)
                    rows.append(d)
                s.commit()
                ids = [d.id for d in rows]

            body = client.get(f"/api/drives/{ids[1]}/boundary").json()
            assert body["trip"]["id"] == ids[1]
            assert body["previous"]["id"] == ids[0]
            assert body["next"]["id"] == ids[2]
            # Conserved ground reads zero on both sides — the case the note
            # exists to warn about, since the instrumentation beside it
            # (a 15-min stale close, 6.4 km recovered) is the actual tell.
            assert body["handover_km"] == {"previous_to_trip": 0.0, "trip_to_next": 0.0}
            assert body["previous"]["end_gap_sec"] == 900.0
            assert body["trip"]["start_recovered_km"] == 6.4

            # The end of the chain has one neighbour, not an error. (These
            # rows sit after the seeded demo history, so only the far side is
            # genuinely empty.)
            last = client.get(f"/api/drives/{ids[2]}/boundary").json()
            assert last["next"] is None
            assert last["handover_km"]["trip_to_next"] is None

            assert client.get("/api/drives/999999/boundary").status_code == 404

            with SessionLocal() as s:
                s.query(Drive).filter(Drive.vehicle_id == vid).delete(
                    synchronize_session=False)
                s.query(Vehicle).filter(Vehicle.id == vid).delete(
                    synchronize_session=False)
                s.commit()
    finally:
        settings.app_passcode = old


def test_standby_evidence_exposes_the_quantum_and_the_clipping():
    """The standby rate is subtracted from real trip energy, so both ways the
    fit can lie have to be visible: SoC readings that went UP being scored as
    zero rather than negative, and gaps whose whole drop is one rounding step."""
    from app import state
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                car = Vehicle(vin="STANDBY-TEST", name="Standby", model="Tesla",
                              battery_capacity_kwh=68.4)
                s.add(car)
                s.flush()
                vid = car.id
                # The endpoint reads the car the dashboard follows, so point
                # that at this one rather than wiping the seeded history.
                state.put(s, state.ACTIVE_VIN_KEY, "STANDBY-TEST")
                base = datetime(2026, 8, 17, 9, 0)
                # Four drives 12 h apart, so every gap between them is one
                # overnight park. Their SoCs give three gaps: a whole point
                # dropped, another whole point, then one where the reading
                # came back a point HIGHER than it went in.
                pairs = [(81.0, 80.0), (79.0, 78.0), (77.0, 76.0), (77.0, 76.0)]
                for i, (start_soc, end_soc) in enumerate(pairs):
                    s.add(Drive(
                        vehicle_id=vid,
                        start_time=base + timedelta(hours=12 * i),
                        end_time=base + timedelta(hours=12 * i, minutes=30),
                        distance_km=10.0, duration_min=30.0,
                        start_soc=start_soc, end_soc=end_soc,
                        start_odo_km=100.0 * i, end_odo_km=100.0 * i + 10,
                    ))
                s.commit()

            body = client.get("/api/standby-evidence").json()
            # The rise is counted as a gap and flagged, not silently dropped.
            assert body["clipped"] == 1
            # ...and the two rates straddle it, which is the whole point of
            # reporting both: one clips that gap to zero, the other doesn't.
            assert body["rate_kw"] > body["rate_kw_unclipped"]
            assert any(g["points"] < 0 for g in body["gaps"])
            # A gap of exactly one point is the quantum, and the endpoint says
            # how long a point would have to spread to look like this rate.
            assert body["soc_point_kwh"] == pytest.approx(
                body["capacity_kwh"] / 100.0, rel=0.01)
            assert body["hours_per_point_at_this_rate"] > 0

            with SessionLocal() as s:
                state.delete(s, state.ACTIVE_VIN_KEY)
                s.query(Drive).filter(Drive.vehicle_id == vid).delete(
                    synchronize_session=False)
                s.query(Vehicle).filter(Vehicle.id == vid).delete(
                    synchronize_session=False)
                s.commit()
    finally:
        settings.app_passcode = old


def test_self_check_separates_what_can_be_fixed_from_what_cannot():
    """The point of this endpoint is the sorting, not the finding. A trip whose
    arrival is still estimated because nothing ever saw the car parked is not
    work; a stop recorded short of a reading that exists is."""
    from app import state
    from app.database import SessionLocal
    from app.models import BatteryReading, Drive, Vehicle

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                car = Vehicle(vin="SELFCHECK-TEST", name="Check", model="Tesla",
                              battery_capacity_kwh=68.4)
                s.add(car)
                s.flush()
                vid = car.id
                state.put(s, state.ACTIVE_VIN_KEY, "SELFCHECK-TEST")
                base = datetime.now() - timedelta(days=2)
                seen_short = Drive(
                    vehicle_id=vid, start_time=base,
                    end_time=base + timedelta(minutes=20),
                    distance_km=10.0, duration_min=20.0, energy_used_kwh=1.5,
                    start_soc=80.0, end_soc=78.0,
                    start_odo_km=100.0, end_odo_km=110.0,
                    start_location="A", end_location="Home")
                nxt = Drive(
                    vehicle_id=vid, start_time=base + timedelta(hours=10),
                    end_time=base + timedelta(hours=10, minutes=20),
                    distance_km=9.6, duration_min=20.0, energy_used_kwh=1.4,
                    start_soc=78.0, end_soc=76.0,
                    start_odo_km=110.0, end_odo_km=119.6,
                    start_location="Home", end_location="B",
                    end_est_km=0.24)          # arrival nothing ever measured
                s.add_all([seen_short, nxt])
                # A poll that saw the car resting 0.4 km past the first trip's
                # recorded stop — evidence, so this one IS work.
                s.add(BatteryReading(
                    vehicle_id=vid, ts=base + timedelta(hours=1),
                    soc=78.0, range_km=300.0, odo_km=110.4))
                s.commit()
                ids = (seen_short.id, nxt.id)

            body = client.get("/api/self-check").json()
            assert body["verdict"].startswith("1 item")
            assert [f["drive_id"] for f in body["actionable"]] == [ids[0]]
            assert "repair-arrivals" in body["actionable"][0]["fix"]

            # The unverified arrival is reported, but not as something to do.
            est = [f for f in body["inherent"]
                   if f["what"] == "arrival distance still estimated"]
            assert [f["drive_id"] for f in est] == [ids[1]]
            assert all("fix" not in f for f in body["inherent"])

            with SessionLocal() as s:
                state.delete(s, state.ACTIVE_VIN_KEY)
                s.query(BatteryReading).filter(
                    BatteryReading.vehicle_id == vid).delete(synchronize_session=False)
                s.query(Drive).filter(Drive.vehicle_id == vid).delete(
                    synchronize_session=False)
                s.query(Vehicle).filter(Vehicle.id == vid).delete(
                    synchronize_session=False)
                s.commit()
    finally:
        settings.app_passcode = old


def test_repair_arrivals_refuses_a_move_made_during_the_park():
    """Ground appearing after a stop the car was SEEN sitting at is a second
    movement, not an arrival tail — an arrival cannot resume once the car has
    parked. Measured, trip 422: the readings put the car 0.35 km past its
    recorded stop while the car's own trip meter agreed with the recorded
    figure exactly, so the 0.35 km happened during the three-hour park."""
    from app import state
    from app.database import SessionLocal
    from app.models import BatteryReading, Drive, Vehicle

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                car = Vehicle(vin="PARKMOVE-TEST", name="Parkmove", model="Tesla",
                              battery_capacity_kwh=68.4)
                s.add(car)
                s.flush()
                vid = car.id
                state.put(s, state.ACTIVE_VIN_KEY, "PARKMOVE-TEST")
                base = datetime.now() - timedelta(days=2)
                arrive = Drive(
                    vehicle_id=vid, start_time=base,
                    end_time=base + timedelta(minutes=20),
                    distance_km=10.0, duration_min=20.0, energy_used_kwh=1.5,
                    start_soc=80.0, end_soc=78.0,
                    start_odo_km=100.0, end_odo_km=110.0,
                    start_location="A", end_location="Car park")
                depart = Drive(
                    vehicle_id=vid, start_time=base + timedelta(hours=4),
                    end_time=base + timedelta(hours=4, minutes=20),
                    distance_km=9.6, duration_min=20.0, energy_used_kwh=1.4,
                    start_soc=78.0, end_soc=76.0,
                    start_odo_km=110.0, end_odo_km=119.6,
                    start_location="Car park", end_location="B")
                s.add_all([arrive, depart])
                # Parked at the recorded stop for an hour, THEN 0.35 further.
                s.add(BatteryReading(vehicle_id=vid, ts=base + timedelta(hours=1),
                                     soc=78.0, range_km=300.0, odo_km=110.0))
                s.add(BatteryReading(vehicle_id=vid, ts=base + timedelta(hours=3),
                                     soc=78.0, range_km=299.0, odo_km=110.35))
                s.commit()
                arrive_id = arrive.id

            plan = client.get("/api/repair-arrivals").json()
            assert plan["repaired"] == 0 and plan["needs_a_human"] == 1
            refused = [m for m in plan["manual"] if m["drive_id"] == arrive_id]
            assert len(refused) == 1
            assert "move during the park" in refused[0]["why"]
            assert "repair-missing-trip" in refused[0]["run"]

            # ...and it is reported as something nothing here can settle,
            # rather than as work with a one-click fix.
            check = client.get("/api/self-check").json()
            assert check["verdict"] == "Nothing to do."
            moved = [f for f in check["inherent"] if f.get("drive_id") == arrive_id]
            assert len(moved) == 1 and "a move during the park" in moved[0]["why"]

            with SessionLocal() as s:
                state.delete(s, state.ACTIVE_VIN_KEY)
                s.query(BatteryReading).filter(
                    BatteryReading.vehicle_id == vid).delete(synchronize_session=False)
                s.query(Drive).filter(Drive.vehicle_id == vid).delete(
                    synchronize_session=False)
                s.query(Vehicle).filter(Vehicle.id == vid).delete(
                    synchronize_session=False)
                s.commit()
    finally:
        settings.app_passcode = old


def test_every_analyze_call_fits_the_parked_rate_from_the_whole_history():
    """A standby rate describes the car, not the window. The commit that said
    so patched only the two call sites that had prompted it, leaving the
    comparison periods and the export to fit their own — so the same park
    could report one figure on the dashboard and another beside it."""
    import inspect
    import re

    from app.api import routes

    src = inspect.getsource(routes)
    # `analyze()` with nothing between the brackets is prose about the
    # function in a comment, not a call to it.
    calls = [m.end() for m in re.finditer(r"driving_analysis\.analyze\(", src)
             if src[m.end()] != ")"]
    assert len(calls) >= 5
    for at in calls:
        depth, i = 1, at
        while depth:                     # to this call's closing bracket
            depth += {"(": 1, ")": -1}.get(src[i], 0)
            i += 1
        assert "vampire_rate_history" in src[at:i], (
            f"analyze() at offset {at} fits its rate from its own window:\n"
            f"{src[at - 40:i]}")


def test_set_parked_draw_records_a_reading_the_fit_cannot_take(monkeypatch):
    """One setting per place, from the car's own Park screen — the same shape
    as the departure pace, and for the same reason: a real quantity this app
    has no instrument for."""
    from app.database import SessionLocal
    from app.models import Place

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            client.post("/api/places", json={
                "name": "Home", "lat": 5.3301, "lon": 100.3001, "radius_km": 0.2,
            })
            assert client.get("/api/places").json()[0]["parked_draw_w"] == 0.0

            assert client.get("/api/set-parked-draw",
                              params={"place": "Nowhere", "w": 14}).status_code == 422
            assert client.get("/api/set-parked-draw",
                              params={"place": "Nowhere", "watts": 14}).status_code == 404
            # kW typed where watts were asked for.
            assert client.get("/api/set-parked-draw",
                              params={"place": "Home", "watts": 0.014}).status_code == 400

            body = client.get("/api/set-parked-draw",
                              params={"place": "home", "watts": 14}).json()
            assert body["place"] == "Home" and body["parked_draw_w"] == 14.0
            assert body["was"] == 0.0

            # The places editor posts no draw field, and must not clear it.
            client.post("/api/places", json={
                "name": "Home", "lat": 5.3302, "lon": 100.3002, "radius_km": 0.25,
            })
            assert client.get("/api/places").json()[0]["parked_draw_w"] == 14.0

            from app.api.routes import _place_parked_rates
            with SessionLocal() as s:
                assert _place_parked_rates(s) == {"Home": 0.014}

            assert client.get("/api/set-parked-draw",
                              params={"place": "Home", "watts": 0}).json()[
                                  "parked_draw_w"] == 0.0
            with SessionLocal() as s:
                assert _place_parked_rates(s) == {}
                s.query(Place).delete()
                s.commit()
    finally:
        settings.app_passcode = old


def test_the_trip_side_and_the_gap_side_price_a_park_at_the_same_rate():
    """vampire_drain adds a park's drain to the gap; the sync-time correction
    subtracts the same minutes from the trip they were taken out of. Two rates
    leak at the boundary, and once a place could be told its own draw the leak
    stopped being second-order: 99.6 parked minutes at the blended 78 W on one
    side and Home's 14 on the other is 0.1 kWh going missing from one trip."""
    from app.database import SessionLocal
    from app.models import Place

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            client.post("/api/places", json={
                "name": "Home", "lat": 5.3301, "lon": 100.3001, "radius_km": 0.2})
            client.get("/api/set-parked-draw", params={"place": "Home", "watts": 14})

            from app.api.routes import _parked_rate_kw_for, _place_parked_rates
            with SessionLocal() as s:
                told = _parked_rate_kw_for(s, "Home", [], [], 68.6)
                # Both consumers resolve through the same helper, so this IS
                # the rate each of them uses.
                assert told == pytest.approx(0.014)
                assert _place_parked_rates(s)["Home"] == pytest.approx(0.014)
                # An unknown place falls through to the fit, not to the setting.
                assert _parked_rate_kw_for(s, "Nowhere", [], [], 68.6) != told
                s.query(Place).delete()
                s.commit()
    finally:
        settings.app_passcode = old


def test_partner_registration_is_recorded_per_domain(monkeypatch):
    """Moving the app to a new domain must re-register it with Tesla.

    Registration is Tesla fetching the partner key FROM a domain, so a flag
    that only remembers "some domain was registered" makes the move silently
    skip the step — and it surfaces much later, as a car refusing to pair
    against a domain Tesla was never told about.
    """
    from app import auth as auth_mod
    from app.api import routes as routes_mod

    settings = get_settings()
    old_pc, old_id, old_secret = (
        settings.app_passcode, settings.tesla_client_id, settings.tesla_client_secret)
    settings.app_passcode = ""
    settings.tesla_client_id = "id"
    settings.tesla_client_secret = "secret"

    registered: list[str] = []
    monkeypatch.setattr(auth_mod, "register_partner",
                        lambda domain: registered.append(domain) or {})
    monkeypatch.setattr(routes_mod.auth, "register_partner",
                        lambda domain: registered.append(domain) or {})
    monkeypatch.setattr(routes_mod.auth, "authorize_url",
                        lambda uri, state=None: ("https://auth.tesla.com/x", "s"))
    try:
        with TestClient(app) as client:
            for host in ("first.example", "first.example", "second.example"):
                client.get("/api/link/oauth/start", follow_redirects=False,
                           headers={"host": host})
        # Twice from the same host registers once; a new host registers again.
        assert registered == ["first.example", "second.example"]
    finally:
        (settings.app_passcode, settings.tesla_client_id,
         settings.tesla_client_secret) = old_pc, old_id, old_secret


def test_telemetry_ingest_records_what_arrived():
    """The bridge's batch is stored verbatim and readable back.

    Nothing is derived from it on purpose: Tesla does not document the units
    of Odometer or EnergyRemaining, and this app's accuracy history is a list
    of figures that were quietly scaled wrong. So the contract here is only
    that what the car sent is what comes back.
    """
    settings = get_settings()
    old_pc, old_sk = settings.app_passcode, settings.sync_key
    settings.app_passcode = "secret123"
    settings.sync_key = "cronkey"
    try:
        with TestClient(app) as client:
            batch = {"records": [{
                "vin": "5YJ3TEST",
                "createdAt": "2026-09-07T14:00:00Z",
                "data": [
                    {"key": "Odometer", "value": {"stringValue": "12345.6"}},
                    {"key": "EnergyRemaining", "value": {"doubleValue": 51.2}},
                    {"key": "Gear", "value": {"shiftStateValue": "ShiftStateD"}},
                    # A shape nobody anticipated must survive, not be dropped.
                    {"key": "Location",
                     "value": {"locationValue": {"latitude": 5.3, "longitude": 100.3}}},
                ],
            }]}
            # No key: refused.
            assert client.post("/api/telemetry", json=batch).status_code == 401
            posted = client.post("/api/telemetry?key=cronkey", json=batch)
            assert posted.status_code == 200
            assert posted.json()["accepted"] == 1

            client.post("/login", data={"passcode": "secret123"})
            seen = client.get("/api/telemetry/recent").json()
            assert seen["seen"]["records"] == 1
            # Every key the car sent is listed, with its latest value.
            assert seen["fields"]["Odometer"]["sample"] == "12345.6"
            assert seen["fields"]["EnergyRemaining"]["sample"] == 51.2
            assert seen["fields"]["Gear"]["sample"] == "ShiftStateD"
            assert seen["fields"]["Location"]["sample"] == {
                "latitude": 5.3, "longitude": 100.3}
            # The original is no longer stored beside the flat form — it
            # doubled the one blob written on every batch, and the only thing
            # it carried that the flat form does not is the replay flag,
            # which is carried on its own.
            assert seen["records"][-1]["resend"] is False
            assert "raw" not in seen["records"][-1]

            # "invalid" is the car saying it has no reading, not a reading of
            # true — and it must not erase what the car last did tell us.
            client.post("/api/telemetry?key=cronkey", json={"records": [{
                "vin": "5YJ3TEST",
                "createdAt": "2026-09-07T14:00:30Z",
                "data": [{"key": "Gear", "value": {"invalid": True}},
                         {"key": "Soc", "value": {"doubleValue": 33.0}}],
            }]})
            after = client.get("/api/telemetry/recent").json()
            assert after["snapshot"]["5YJ3TEST"]["shift"] == "D"
            assert after["snapshot"]["5YJ3TEST"]["soc"] == 33.0

            assert client.get("/api/telemetry/recent?keys_only=true").json()[
                "records"] == []

            # The composite maps to a snapshot in the units the app uses.
            # Two records, each carrying only what changed, must together
            # describe the car — that is the whole reason for accumulating.
            client.post("/api/telemetry?key=cronkey", json={"records": [{
                "vin": "5YJ3TEST",
                "createdAt": "2026-09-07T14:01:00Z",
                "data": [{"key": "Soc", "value": {"doubleValue": 40.0}}],
            }]})
            snap = client.get("/api/telemetry/recent").json()["snapshot"]["5YJ3TEST"]
            assert round(snap["odo_km"]) == 19868   # 12345.6 miles, from batch 1
            assert snap["soc"] == 40.0              # from batch 2
            assert snap["shift"] == "D"             # still remembered
    finally:
        settings.app_passcode, settings.sync_key = old_pc, old_sk


def test_fleet_token_needs_the_key_and_withholds_the_refresh_token(monkeypatch):
    """The receiver box gets an access token; it never gets the refresh token.

    That asymmetry is the whole point: what leaks if the sync key leaks then
    expires on its own in hours, rather than lasting until someone notices.
    """
    from app import state as state_mod
    from app.api import routes as routes_mod
    from app.database import SessionLocal

    settings = get_settings()
    old_pc, old_sk = settings.app_passcode, settings.sync_key
    settings.app_passcode = "secret123"
    settings.sync_key = "cronkey"
    with SessionLocal() as s:
        state_mod.put(s, state_mod.TOKEN_KEY, "access-abc")
        state_mod.put(s, state_mod.REFRESH_KEY, "refresh-xyz")
    monkeypatch.setattr(routes_mod.auth, "oauth_configured", lambda: False)
    try:
        with TestClient(app) as client:
            assert client.get("/api/fleet-token").status_code == 401
            body = client.get("/api/fleet-token?key=cronkey").json()
            assert body["access_token"] == "access-abc"
            assert "refresh_token" not in body
            assert "vin" in body and "base_url" in body
    finally:
        settings.app_passcode, settings.sync_key = old_pc, old_sk
        with SessionLocal() as s:
            state_mod.delete(s, state_mod.TOKEN_KEY, state_mod.REFRESH_KEY)


def test_telemetry_shadow_trip_appears_and_is_promoted_at_once():
    """A stream of records becomes a shadow trip AND the drive row for it.

    The unit tests cover the machine; this covers the wiring — that records
    arriving over the wire drive it, and that a journey which has ended is on
    the dashboard immediately rather than at the next cron tick.

    That used to be the opposite assertion, and rightly: promotion ran only
    from /api/sync, which was invisible at a one-minute cron. At thirty
    minutes it is not — a trip that ended fourteen minutes ago was simply
    missing — and at the four-hourly watchdog this is heading for it would be
    absent for most of the day.
    """
    from datetime import timezone

    from app.database import SessionLocal
    from app.models import Drive

    settings = get_settings()
    old_pc, old_sk = settings.app_passcode, settings.sync_key
    settings.app_passcode = "secret123"
    settings.sync_key = "cronkey"

    base = datetime.now(timezone.utc) - timedelta(hours=2)

    def rec(offset_s, **fields):
        stamp = (base + timedelta(seconds=offset_s)).isoformat().replace(
            "+00:00", "Z")
        return {"vin": "SHADOW1", "createdAt": stamp,
                "data": [{"key": k, "value": {"doubleValue": v}}
                         if isinstance(v, (int, float)) else
                         {"key": k, "value": {"shiftStateValue": v}}
                         for k, v in fields.items()]}

    try:
        with TestClient(app) as client:
            # Counted after startup: entering TestClient seeds demo data, so
            # a count taken before it would be measuring the seeder.
            with SessionLocal() as s:
                before = s.query(Drive).count()
            client.post("/api/telemetry?key=cronkey", json={"records": [
                rec(0, Odometer=100.0, EnergyRemaining=30.0, Soc=50.0,
                    Gear="ShiftStateD", VehicleSpeed=20.0),
                rec(600, Odometer=106.0, EnergyRemaining=28.5, Soc=48.0,
                    VehicleSpeed=25.0),
                rec(660, Gear="ShiftStateP", VehicleSpeed=0.0),
                # Somebody gets out — that is what makes it an arrival rather
                # than a pause, and settles it on the short window.
                {"vin": "SHADOW1",
                 "createdAt": (base + timedelta(seconds=670)).isoformat().replace(
                     "+00:00", "Z"),
                 "data": [{"key": "DoorState",
                           "value": {"doorValue": {"DriverFront": True}}}]},
                rec(900, VehicleSpeed=0.0),
            ]})
            client.post("/login", data={"passcode": "secret123"})
            body = client.get("/api/telemetry/compare?days=1").json()

        assert body["telemetry_trips"] >= 1
        trip = body["trips"][-1]["telemetry"]
        assert round(trip["km"], 1) == 9.7        # 6 miles
        assert trip["kwh"] == 1.5                 # measured, not derived
        # And the history has it, written by the ingest rather than the cron.
        with SessionLocal() as s:
            assert s.query(Drive).count() == before + 1
            row = s.scalars(select(Drive).order_by(Drive.id.desc())).first()
            assert row.source == "telemetry"
            assert round(row.distance_km, 1) == 9.7
    finally:
        settings.app_passcode, settings.sync_key = old_pc, old_sk
        # Promotion now writes on ingest, so this test has a side effect on
        # the shared database that it did not have before. Cleaned up, or
        # every later test counting drives inherits it.
        with SessionLocal() as s:
            extra = s.scalars(
                select(Drive).where(Drive.source == "telemetry",
                                    Drive.polled_km.is_(None))).all()
            for r in extra:
                s.delete(r)
            s.commit()


def test_sentry_escalation_raises_an_alert_once(monkeypatch):
    """Aware and Panic mean the car noticed something; Armed does not.

    Polling could never tell these apart — vehicle_data reports a bare on/off
    boolean, which is why this app's own note says the alarm state is not
    visible in the API. It is visible in the stream, as a transition.
    """
    from app.api import routes as routes_mod

    settings = get_settings()
    old_sk = settings.sync_key
    settings.sync_key = "cronkey"
    alerts: list[tuple] = []
    monkeypatch.setattr(routes_mod.notifications, "notify",
                        lambda s, title, body, tag=None: alerts.append((title, body, tag)))

    def sentry(state, at):
        return {"vin": "SENTRY1", "createdAt": at,
                "data": [{"key": "SentryMode",
                          "value": {"sentryModeStateValue": state}}]}

    try:
        with TestClient(app) as client:
            client.post("/api/telemetry?key=cronkey", json={"records": [
                sentry("SentryModeStateArmed", "2026-09-08T10:00:00Z"),
                sentry("SentryModeStateAware", "2026-09-08T10:01:00Z"),
                # Flicker: someone walks past again inside the cooldown.
                sentry("SentryModeStateArmed", "2026-09-08T10:01:30Z"),
                sentry("SentryModeStatePanic", "2026-09-08T10:02:00Z"),
            ]})
        # Armed is the car minding its own business, and the cooldown keeps a
        # flicker from becoming six messages — but the alarm is not a flicker.
        # Someone triggering Aware and then setting the alarm off two minutes
        # later is the sequence that matters most, and the quiet period earned
        # by the first must not swallow the second.
        assert [a[0] for a in alerts] == ["Sentry", "Sentry: alarm"], alerts
        assert all(a[2] == "sentry" for a in alerts)
        assert "moved near the car" in alerts[0][1]
        assert "ALARM went off" in alerts[1][1]
    finally:
        settings.sync_key = old_sk


def test_push_test_lives_on_telegram_alone():
    """Telegram-only setups must be able to check that alerts arrive.

    The endpoint used to require VAPID push, which left someone whose only
    channel is Telegram with no way to test it before an alarm depended on it.
    """
    settings = get_settings()
    old_pc, old_tok, old_chat = (settings.app_passcode,
                                 settings.telegram_bot_token, settings.telegram_chat_id)
    old_priv, old_pub = settings.vapid_private_key_pem, settings.vapid_public_key_pem
    settings.app_passcode = ""
    settings.vapid_private_key_pem = settings.vapid_public_key_pem = ""
    settings.telegram_bot_token, settings.telegram_chat_id = "123:ABC", "456"
    try:
        with TestClient(app) as client:
            with mock.patch("app.notifications.httpx.post") as post:
                post.return_value = SimpleNamespace(status_code=200)
                body = client.get("/api/push/test").json()
            assert body["channels_configured"] == {
                "push": False, "telegram": True, "webhook": False}
            assert post.call_args.args[0].endswith("/sendMessage")
    finally:
        settings.app_passcode = old_pc
        settings.telegram_bot_token, settings.telegram_chat_id = old_tok, old_chat
        settings.vapid_private_key_pem, settings.vapid_public_key_pem = old_priv, old_pub


def test_telegram_chat_id_lookup():
    """Reads the chat ID off getUpdates so nobody has to type a token into
    Safari — one autocorrected character there returns a bare 404."""
    settings = get_settings()
    old_pc, old_tok = settings.app_passcode, settings.telegram_bot_token
    settings.app_passcode = ""
    try:
        settings.telegram_bot_token = ""
        with TestClient(app) as client:
            assert client.get("/api/telegram/chat-id").status_code == 404

            settings.telegram_bot_token = "123:ABC"

            def _replies(*payloads):
                """getUpdates first, then getMe — one canned reply each."""
                return [SimpleNamespace(json=lambda p=p: p) for p in payloads]

            me = {"ok": True, "result": {"username": "eV_Tesla_Analyzer_bot"}}
            with mock.patch("app.api.routes.httpx.get") as get:
                get.side_effect = _replies({
                    "ok": True,
                    "result": [
                        {"message": {"chat": {"id": 987654321, "type": "private",
                                              "first_name": "Ph"}, "text": "hi"}},
                        # A second message from the same chat must not appear twice.
                        {"message": {"chat": {"id": 987654321, "type": "private",
                                              "first_name": "Ph"}, "text": "hi again"}},
                    ],
                }, me)
                body = client.get("/api/telegram/chat-id").json()
            assert body["chats"] == [
                {"chat_id": 987654321, "type": "private", "name": "Ph"}]
            assert body["bot"] == "@eV_Tesla_Analyzer_bot"

            # No messages yet is a normal state, not an error: it means the
            # bot has not been spoken to — or that the token belongs to a
            # different bot than the one being messaged, which is why the
            # hint has to name the bot rather than just say "your bot".
            with mock.patch("app.api.routes.httpx.get") as get:
                get.side_effect = _replies({"ok": True, "result": []}, me)
                body = client.get("/api/telegram/chat-id").json()
            assert body["chats"] == [] and "tap Start" in body["hint"]
            assert "@eV_Tesla_Analyzer_bot" in body["hint"]

            # getMe failing is a lost diagnostic, not a lost answer.
            with mock.patch("app.api.routes.httpx.get") as get:
                get.side_effect = [
                    SimpleNamespace(json=lambda: {
                        "ok": True,
                        "result": [{"message": {"chat": {"id": 5, "type": "private",
                                                         "first_name": "Ph"}}}],
                    }),
                    RuntimeError("network"),
                ]
                body = client.get("/api/telegram/chat-id").json()
            assert body["chats"][0]["chat_id"] == 5 and body["bot"] == ""

            # A token Telegram rejects must not be reported as a mistyped URL:
            # this URL is built in code, so the token is the only suspect.
            with mock.patch("app.api.routes.httpx.get") as get:
                get.return_value = SimpleNamespace(json=lambda: {
                    "ok": False, "error_code": 404, "description": "Not Found"})
                resp = client.get("/api/telegram/chat-id")
            assert resp.status_code == 502
            assert "BotFather" in resp.json()["detail"]
    finally:
        settings.app_passcode, settings.telegram_bot_token = old_pc, old_tok


def test_script_shortcuts_are_reachable_without_the_passcode():
    """The receiver box is set up by typing a command into Google's
    SSH-in-browser, which does not paste on iOS — so the URL has to be short,
    and curl there carries no passcode cookie."""
    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = "secret"
    try:
        with TestClient(app) as client:
            for path, script in (("/vm", "vm.sh"), ("/car", "car.sh")):
                resp = client.get(path, follow_redirects=False)
                assert resp.status_code == 302, path
                assert resp.headers["location"].endswith("/" + script)
                # Not behind the gate: a redirect to /login would hand curl
                # an HTML page and bash would try to run it.
                assert "/login" not in resp.headers["location"]
    finally:
        settings.app_passcode = old_pc


def test_compare_survives_an_unreadable_shadow_trip():
    """A record the store cannot read is a named skip, not a 500.

    Shadow trips are written by a state machine that has changed shape several
    times while the car kept streaming, so the store can hold records from more
    than one version of it. "How did that drive go" should not answer with a
    server error because one old record is missing a key.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    previous = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    try:
        now = sync_mod.now_local()
        start, end = now - timedelta(minutes=20), now - timedelta(minutes=2)
        good = {
            "start_ts": start.timestamp(), "end_ts": end.timestamp(),
            "start_time": start.isoformat(timespec="seconds"),
            "end_time": end.isoformat(timespec="seconds"),
            "distance_km": 10.4, "duration_min": 18.0, "energy_kwh": 1.52,
            "wh_per_km": 146.2, "start_odo_km": 12000.0, "end_odo_km": 12010.4,
        }
        state.put(sess, state.TELEMETRY_TRIPS_KEY,
                  _json.dumps([good, {"start_time": "broken-row"}]))
        sess.commit()

        with TestClient(app) as client:
            body = client.get("/api/telemetry/compare").json()
        assert body["telemetry_trips"] == 1          # the good one still reported
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["why"].startswith("KeyError")
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, previous or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_unhandled_failure_says_what_broke():
    """A 500 must name the exception and where it happened.

    The only person who runs this app is on a phone, where reading the host's
    log viewer is not realistic — so a blank "Internal Server Error" means the
    next step is guesswork and another deploy.
    """
    @app.get("/api/_boom_for_test")
    def _boom():
        raise ValueError("kaboom")

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/api/_boom_for_test")
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "ValueError" and body["detail"] == "kaboom"
        assert any("test_app.py" in frame for frame in body["where"])
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes
            if getattr(r, "path", None) != "/api/_boom_for_test"
        ]


def test_percentile_rejects_a_percentage():
    """0.5 is the median; 50 is a bug that hides until the second data point.

    percentile indexes at (len-1) x pct, so 50 lands inside the list while the
    list holds one element and runs off the end once a second arrives. That is
    how six such calls reached production in the telemetry comparison and only
    failed after a second shadow trip was recorded.
    """
    from app.analysis import percentile

    assert percentile([2.0, 4.0], 0.5) == 3.0
    with pytest.raises(ValueError, match="fraction"):
        percentile([2.0, 4.0], 50)


def test_no_caller_passes_percentile_a_percentage():
    """Repo-wide, because the failure surfaces far from the call that is wrong."""
    import re
    from pathlib import Path

    offenders = []
    for path in Path("app").rglob("*.py"):
        for num, line in enumerate(path.read_text().splitlines(), 1):
            for arg in re.findall(r"percentile\([^()]*,\s*([0-9.]+)\s*\)", line):
                if float(arg) > 1.0:
                    offenders.append(f"{path}:{num}: {arg}")
    assert not offenders, "percentile takes a fraction: " + "; ".join(offenders)


def _snap(ts, **kw):
    base = {"ts": ts, "odo_km": 100.0, "soc": 80.0, "energy_kwh": 55.0,
            "speed_kmh": 0.0, "shift": "P", "charging": False,
            "seat_occupied": True, "doors_open": False, "lat": 5.3, "lon": 100.3}
    base.update(kw)
    return base


def test_shadow_trip_closes_after_the_car_sleeps():
    """A parked Tesla stops streaming, so nothing arrives to close the trip.

    advance_shadow only runs on an incoming record. Left at that, the last
    drive of the day stays open until the next one — which reads, from
    outside, as telemetry having missed the journey altogether.
    """
    from app import sync as sync_mod

    shadow: dict = {}
    t = 1_000_000.0
    # Drive, then stop. The stop is the last thing the car ever says.
    sync_mod.advance_shadow(shadow, _snap(t, speed_kmh=40.0, shift="D"))
    sync_mod.advance_shadow(shadow, _snap(t + 600, odo_km=110.0, energy_kwh=53.0,
                                          speed_kmh=40.0, shift="D"))
    last = _snap(t + 660, odo_km=111.0, energy_kwh=52.8,
                 seat_occupied=False, doors_open=True)
    assert sync_mod.advance_shadow(shadow, last) is None
    assert shadow.get("open")                      # still open, as it should be

    # Silence. Too soon to act: a live stream must keep ownership of the
    # decision, because it knows more than the clock does.
    assert sync_mod.settle_shadow(shadow, t + 700) is None
    # Quiet long enough, and past the settle window.
    done = sync_mod.settle_shadow(shadow, t + 660 + 400)
    assert done is not None
    # The trip ends when the car stopped, not when this noticed.
    assert done["end_ts"] == t + 660
    assert done["distance_km"] == pytest.approx(11.0, abs=0.01)
    assert not shadow.get("open")                  # and the machine is clear


def test_shadow_trip_closes_when_the_stream_dies_mid_drive():
    """No stationary snapshot ever arrived — close at the last motion seen."""
    from app import sync as sync_mod

    shadow: dict = {}
    t = 2_000_000.0
    sync_mod.advance_shadow(shadow, _snap(t, speed_kmh=40.0, shift="D"))
    sync_mod.advance_shadow(shadow, _snap(t + 300, odo_km=105.0, energy_kwh=54.0,
                                          speed_kmh=40.0, shift="D"))
    assert sync_mod.settle_shadow(shadow, t + 400) is None      # still driving
    done = sync_mod.settle_shadow(shadow, t + 300 + 700)
    assert done is not None and done["end_ts"] == t + 300


def test_late_arrival_reading_extends_the_closed_trip():
    """A car parked out of coverage replays its arrival after the trip closed.

    The readings that measure where the journey truly ended are transmitted —
    just late. Discarding them loses the same tail on every trip that ends in
    the same underground carpark, which is a bias, not noise.
    """
    from app import sync as sync_mod

    shadow: dict = {}
    t = 3_000_000.0
    sync_mod.advance_shadow(shadow, _snap(t, odo_km=100.0, energy_kwh=55.0,
                                          speed_kmh=40.0, shift="D"))
    sync_mod.advance_shadow(shadow, _snap(t + 600, odo_km=110.0, energy_kwh=53.0,
                                          speed_kmh=40.0, shift="D"))
    # Signal dies here; this stale odometer is what the trip closes on.
    sync_mod.advance_shadow(shadow, _snap(t + 660, odo_km=110.7, energy_kwh=52.9,
                                          seat_occupied=False, doors_open=True))
    trip = sync_mod.settle_shadow(shadow, t + 660 + 400)
    assert trip["distance_km"] == pytest.approx(10.7, abs=0.001)

    # Coverage returns; the car replays what it measured while parked.
    late = _snap(t + 700, odo_km=111.0, energy_kwh=52.8, soc=76.0)
    assert sync_mod.amend_closed_trip(trip, late) is True
    assert trip["distance_km"] == pytest.approx(11.0, abs=0.001)
    assert trip["end_odo_km"] == pytest.approx(111.0, abs=0.001)
    assert trip["tail_amended_km"] == pytest.approx(0.3, abs=0.001)
    # The recovered 0.3 km costs something, but not the raw EnergyRemaining
    # delta: that reading was taken while the car sat there drawing power,
    # and subtracting it charges the journey for standby it did not spend
    # driving. Added at the trip's own Wh/km, the same way recover_sleep_gap
    # adds it, so the two recovery paths agree rather than one measuring
    # standby and the other inferring propulsion.
    assert trip["energy_kwh"] == pytest.approx(2.1 + 0.3 * 0.19626, abs=0.002)
    assert trip["wh_per_km"] == pytest.approx(196.3, abs=0.5)
    assert trip["soc_end"] == 76.0
    # Time is untouched: the car stopped when it stopped.
    assert trip["end_ts"] == t + 660
    assert trip["duration_min"] == pytest.approx(11.0, abs=0.1)

    # Applying it again from an even later reading refines the same trip
    # rather than compounding: distance is recomputed from the bracket, and
    # the energy added is only for the ground gained SINCE — 0.1 km here,
    # not the 0.4 km the trip has now recovered in total. The same reading
    # twice gains nothing and is refused outright.
    assert sync_mod.amend_closed_trip(trip, _snap(t + 720, odo_km=111.1,
                                                  energy_kwh=52.8)) is True
    assert trip["distance_km"] == pytest.approx(11.1, abs=0.001)
    assert trip["energy_kwh"] == pytest.approx(2.179, abs=0.002)
    assert trip["wh_per_km"] == pytest.approx(196.3, abs=0.5)


def test_late_arrival_reading_is_refused_when_it_cannot_be_the_arrival():
    from app import sync as sync_mod

    shadow: dict = {}
    t = 4_000_000.0
    sync_mod.advance_shadow(shadow, _snap(t, odo_km=100.0, energy_kwh=55.0,
                                          speed_kmh=40.0, shift="D"))
    sync_mod.advance_shadow(shadow, _snap(t + 600, odo_km=110.0, energy_kwh=53.0,
                                          speed_kmh=40.0, shift="D"))
    sync_mod.advance_shadow(shadow, _snap(t + 660, odo_km=110.7, energy_kwh=52.9,
                                          seat_occupied=False, doors_open=True))
    trip = sync_mod.settle_shadow(shadow, t + 660 + 400)
    base = dict(trip)

    # Before the end: stale in the ordinary sense.
    assert not sync_mod.amend_closed_trip(trip, _snap(t + 600, odo_km=111.0))
    # Long after: belongs to whatever the car did next.
    assert not sync_mod.amend_closed_trip(trip, _snap(t + 660 + 5000, odo_km=111.0))
    # Too far: not an arrival tail, a journey.
    assert not sync_mod.amend_closed_trip(trip, _snap(t + 700, odo_km=118.0))
    # Backwards, and unchanged.
    assert not sync_mod.amend_closed_trip(trip, _snap(t + 700, odo_km=110.0))
    assert not sync_mod.amend_closed_trip(trip, _snap(t + 700, odo_km=110.7))
    # Still moving: this is a trip, not an arrival.
    assert not sync_mod.amend_closed_trip(
        trip, _snap(t + 700, odo_km=111.0, speed_kmh=30.0, shift="D"))
    assert trip == base


def test_enum_fields_are_read_not_coerced():
    """Tesla sends these as enum strings, and bool() of one is not a reading.

    bool("ChargePortLatchDisengaged") is True, exactly like
    bool("ChargePortLatchEngaged") — so a latch coerced this way can never
    report itself open. Same trap for HvacPower, whose value is a state name
    rather than the number the field's old name implied.
    """
    from app import sync as sync_mod

    def snap(**fields):
        return sync_mod.snapshot_from_telemetry(fields, 1_000_000.0)

    assert snap(ChargePortLatch="ChargePortLatchEngaged")["charge_port_latched"] is True
    assert snap(ChargePortLatch="ChargePortLatchDisengaged")["charge_port_latched"] is False
    assert snap(ChargePortLatch="ChargePortLatchBlocking")["charge_port_latched"] is False
    # A value the car could not supply is unknown, not a confident False.
    assert snap(ChargePortLatch="ChargePortLatchSNA")["charge_port_latched"] is None
    assert snap()["charge_port_latched"] is None

    assert snap(HvacPower="HvacPowerStateOn")["climate_on"] is True
    assert snap(HvacPower="HvacPowerStatePrecondition")["climate_on"] is True
    assert snap(HvacPower="HvacPowerStateOverheatProtect")["climate_on"] is True
    assert snap(HvacPower="HvacPowerStateOff")["climate_on"] is False
    assert snap()["climate_on"] is None

    # CenterDisplay is streamed and kept as the car's own word for the state.
    # The center_display_state column that used to sit beside it held Tesla's
    # POLLED integer code on an undocumented scale, so no streamed value could
    # be turned into one without inventing a mapping — the column is gone and
    # the enum, which needs no mapping, is what remains.
    s = snap(CenterDisplay="DisplayStateDriving")
    assert s["display_state_raw"] == "DisplayStateDriving"
    assert "center_display_state" not in s


def test_late_reading_never_reaches_past_the_newest_trip():
    """A reading that adds nothing to the newest trip must not fall through.

    Two contiguous fragments of one journey, 31118.637 -> 31119.297 ->
    31119.946. A reading at the end of the second was refused by it (no gain)
    and then accepted by the first, which extended to 31119.946 and counted
    the second fragment's 0.649 km a second time. An older trip's end is
    bounded by the start of the trip after it; only the newest is a candidate.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc, old_key = settings.app_passcode, settings.sync_key
    settings.app_passcode = ""
    sess = SessionLocal()
    prev_trips = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    prev_shadow = state.get(sess, state.TELEMETRY_SHADOW_KEY)
    vin = "TESTVIN0000000001"
    try:
        base = 1_788_800_000.0
        earlier = {"vin": vin, "end_ts": base, "start_odo_km": 31118.637,
                   "end_odo_km": 31119.297, "distance_km": 0.66,
                   "duration_min": 0.9, "start_energy_kwh": 55.0,
                   "end_energy_kwh": 54.62}
        newest = {"vin": vin, "end_ts": base + 64, "start_odo_km": 31119.297,
                  "end_odo_km": 31119.946, "distance_km": 0.649,
                  "duration_min": 1.0, "start_energy_kwh": 54.62,
                  "end_energy_kwh": 54.54}
        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([earlier, newest]))
        state.put(sess, state.TELEMETRY_SHADOW_KEY, _json.dumps({vin: {}}))
        sess.commit()

        # A stationary reading at the newest trip's own ending: adds nothing.
        with TestClient(app) as client:
            resp = client.post("/api/telemetry", json={"records": [{
                "vin": vin,
                # base + 124s: inside the fifteen-minute tail window of both
                # trips, so only the newest-trip rule can keep it out.
                "createdAt": "2026-09-07T16:55:24.000000000Z",
                # 19337.08601 miles is 31119.946 km: exactly where the newest
                # trip ended, so it offers that trip nothing and the older one
                # everything — which is the trap.
                "data": [{"key": "Odometer",
                          "value": {"doubleValue": 19337.08601041421}},
                         {"key": "VehicleSpeed", "value": {"doubleValue": 0}},
                         {"key": "Gear", "value": {"stringValue": "ShiftStateP"}}],
            }]})
        assert resp.status_code == 200

        stored = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
        by_start = {round(t["start_odo_km"], 3): t for t in stored}
        # The earlier fragment is untouched: it must not reach into the newer.
        assert by_start[31118.637]["end_odo_km"] == 31119.297
        assert by_start[31118.637]["distance_km"] == 0.66
        assert "tail_amended_km" not in by_start[31118.637]
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev_trips or "[]")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, prev_shadow or "{}")
        sess.commit()
        sess.close()
        settings.app_passcode, settings.sync_key = old_pc, old_key


def test_ingest_keeps_the_field_composite_when_no_trip_is_running():
    """The per-vehicle composite must survive a record that starts no trip.

    A telemetry message carries only what changed, so the composite is the
    only thing that ever describes the whole car. A local named `latest` in
    the tail-recovery branch shadowed it, and the save below wrote null over
    every vehicle's fields on any ingest where that branch ran — which is
    every ingest while the car sits parked.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_LATEST_KEY)
    try:
        with TestClient(app) as client:
            client.post("/api/telemetry", json={"records": [{
                "vin": "COMPOSITE1",
                "createdAt": "2026-09-08T10:00:00Z",
                "data": [{"key": "Soc", "value": {"doubleValue": 77.0}},
                         {"key": "Gear", "value": {"stringValue": "ShiftStateP"}}],
            }]})
            # A second, disjoint field: it must join the first, not replace it.
            client.post("/api/telemetry", json={"records": [{
                "vin": "COMPOSITE1",
                "createdAt": "2026-09-08T10:00:30Z",
                "data": [{"key": "OutsideTemp", "value": {"doubleValue": 32.0}}],
            }]})

        stored = _json.loads(state.get(SessionLocal(), state.TELEMETRY_LATEST_KEY))
        assert stored is not None, "the composite was overwritten with null"
        assert stored["COMPOSITE1"]["Soc"] == 77.0
        assert stored["COMPOSITE1"]["OutsideTemp"] == 32.0
    finally:
        state.put(sess, state.TELEMETRY_LATEST_KEY, prev or "{}")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_ingest_recovers_from_a_corrupt_state_row():
    """A store holding the literal null must not keep rejecting the bridge.

    json.loads("null") is None, so a single bad write left every subsequent
    ingest raising AttributeError — the receiver's posts failing, and the
    records in them lost, long after the bug that wrote it was fixed. Guarding
    only the write is not enough when the damage is already on disk.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in (
        state.TELEMETRY_LATEST_KEY, state.TELEMETRY_SHADOW_KEY,
        state.TELEMETRY_TRIPS_KEY, state.TELEMETRY_SEEN_KEY)}
    try:
        for key in prev:
            state.put(sess, key, "null")
        sess.commit()

        with TestClient(app) as client:
            resp = client.post("/api/telemetry", json={"records": [{
                "vin": "CORRUPT1",
                "createdAt": "2026-09-08T10:00:00Z",
                "data": [{"key": "Soc", "value": {"doubleValue": 60.0}}],
            }]})
        assert resp.status_code == 200, resp.text
        stored = _json.loads(state.get(SessionLocal(), state.TELEMETRY_LATEST_KEY))
        assert stored["CORRUPT1"]["Soc"] == 60.0
    finally:
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_settle_does_not_close_a_live_trip():
    """The settle clock must share the record timestamps' time base.

    Record timestamps are true epochs from _telemetry_ts. now_local() is naive
    MYT wall-clock, and .timestamp() on a naive value reads it in the server's
    timezone — eight hours out on the deployed host. Every call then saw 28,800
    seconds of silence and closed whatever trip was open, so a cron ticking
    each minute chopped one evening's driving into eleven one-minute fragments.
    """
    import json as _json
    import time as _time

    from app.database import SessionLocal
    from app.api import routes as routes_mod
    from app import state

    sess = SessionLocal()
    prev_shadow = state.get(sess, state.TELEMETRY_SHADOW_KEY)
    prev_trips = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    vin = "LIVETRIP00000001"
    try:
        now = _time.time()
        driving = {"ts": now - 5, "odo_km": 120.0, "energy_kwh": 50.0,
                   "speed_kmh": 40.0, "shift": "D", "soc": 70.0,
                   "charging": False, "seat_occupied": True, "doors_open": False}
        shadow = {"open": dict(driving, ts=now - 600, odo_km=110.0,
                               energy_kwh=52.0),
                  "last": driving, "seen_ts": now - 5, "max_speed_kmh": 60.0}
        state.put(sess, state.TELEMETRY_SHADOW_KEY, _json.dumps({vin: shadow}))
        state.put(sess, state.TELEMETRY_TRIPS_KEY, "[]")
        sess.commit()

        assert routes_mod._settle_shadows(SessionLocal()) == 0

        after = _json.loads(state.get(SessionLocal(), state.TELEMETRY_SHADOW_KEY))
        assert after[vin].get("open"), "a live trip was closed"
        assert _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY)) == []
    finally:
        state.put(sess, state.TELEMETRY_SHADOW_KEY, prev_shadow or "{}")
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev_trips or "[]")
        sess.commit()
        sess.close()


def test_drop_trips_previews_before_it_deletes():
    """Shadow trips are derived, so a bug in the closing code writes trips that
    never happened. Nothing recomputes them, so they need removing by hand —
    and a hand-run delete should show its work before doing it."""
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    try:
        def trip(hh, mm, km):
            at = datetime(2026, 9, 8, hh, mm)
            return {"start_ts": at.timestamp() - 8 * 3600, "end_ts": at.timestamp() - 8 * 3600 + 600,
                    "start_time": at.isoformat(timespec="seconds"),
                    "end_time": at.isoformat(timespec="seconds"),
                    "distance_km": km, "duration_min": 10.0}

        good, frag1, frag2 = trip(17, 9, 10.89), trip(20, 24, 0.325), trip(20, 25, 0.857)
        unreadable = {"start_time": "no start_ts here"}
        state.put(sess, state.TELEMETRY_TRIPS_KEY,
                  _json.dumps([good, frag1, frag2, unreadable]))
        sess.commit()

        with TestClient(app) as client:
            body = client.get("/api/telemetry/drop-trips?since=2026-09-08T19:00").json()
            assert body["would_drop"] == 2 and body["would_keep"] == 2
            # Nothing changed yet: a preview that deletes is not a preview.
            assert len(_json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))) == 4

            body = client.get(
                "/api/telemetry/drop-trips?since=2026-09-08T19:00&apply=true").json()
            assert body["dropped"] == 2

        left = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
        assert [t.get("distance_km") for t in left] == [10.89, None]
        # A trip whose start cannot be read is kept, not swept up: it has not
        # been shown to be on the wrong side of the boundary.
        assert any(t.get("start_time") == "no start_ts here" for t in left)

        with TestClient(app) as client:
            assert client.get("/api/telemetry/drop-trips?since=not-a-date").status_code == 422
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_no_trip_without_a_real_odometer():
    """A composite that has not yet seen an Odometer record reads 0.0.

    Odometer streams every 30 seconds and VehicleSpeed every 10, so after any
    reset of the store the first driving snapshot arrives before the first
    odometer one. Opening a trip there measures from zero and closes against
    the real reading: a single journey of 31,127 km, which looks exactly like
    data and poisons every median it touches.
    """
    from app import sync as sync_mod

    shadow: dict = {}
    t = 1_788_900_000.0
    moving_only = sync_mod.snapshot_from_telemetry({"VehicleSpeed": 14.0}, t)
    assert moving_only["odo_km"] == 0.0 and sync_mod.is_driving(moving_only)
    sync_mod.advance_shadow(shadow, moving_only)
    assert not shadow.get("open"), "opened a trip with no odometer to measure from"

    # It opens as soon as there is something to measure from, and the trip
    # that results is the real one.
    sync_mod.advance_shadow(shadow, sync_mod.snapshot_from_telemetry(
        {"VehicleSpeed": 14.0, "Odometer": 19341.0}, t + 30))
    assert shadow["open"]["odo_km"] == pytest.approx(31126.245, abs=0.01)
    sync_mod.advance_shadow(shadow, sync_mod.snapshot_from_telemetry(
        {"VehicleSpeed": 0.0, "Gear": "ShiftStateP", "Odometer": 19341.4,
         "DriverSeatOccupied": False}, t + 60))
    done = sync_mod.settle_shadow(shadow, t + 760)
    assert done["distance_km"] == pytest.approx(0.644, abs=0.002)


def test_absurd_distance_is_refused_rather_than_recorded():
    """A boundary that went wrong should leave a gap, not a plausible record.

    A missing trip is visible as missing. A trip carrying an impossible number
    is indistinguishable from a real one until someone reads it closely.
    """
    from app import sync as sync_mod

    shadow = {
        "open": {"ts": 1_788_900_000.0, "odo_km": 5.0, "energy_kwh": 55.0,
                 "soc": 80.0, "lat": 5.3, "lon": 100.3},
        "max_speed_kmh": 60.0,
    }
    end = {"ts": 1_788_900_600.0, "odo_km": 31127.0, "soc": 79.0,
           "energy_kwh": 54.0}
    assert sync_mod._shadow_close(shadow, end) is None


def _tele_record(vin, ts, resend=False, **kv):
    """One telemetry record in the wire shape the bridge posts."""
    from datetime import timezone as _tz

    keys = {"odo": "Odometer", "mph": "VehicleSpeed", "gear": "Gear",
            "kwh": "EnergyRemaining", "soc": "Soc", "seat": "DriverSeatOccupied"}
    data = []
    for short, value in kv.items():
        key = keys[short]
        wrap = ({"stringValue": value} if key == "Gear"
                else {"booleanValue": value} if key == "DriverSeatOccupied"
                else {"doubleValue": value})
        data.append({"key": key, "value": wrap})
    stamp = datetime.fromtimestamp(ts, _tz.utc).isoformat().replace("+00:00", "Z")
    return {"vin": vin, "createdAt": stamp, "data": data, "isResend": resend}


def test_a_charging_session_reaches_the_charges_endpoint_through_the_ingest():
    """The three meters, posted the way the receiver posts them.

    The unit tests drive the charge machine directly. This drives it through
    the HTTP path, because that is where the wiring can be wrong — the sleep
    gap recovery was correct in isolation and never fired once in production,
    for exactly that reason.
    """
    import time as _time
    from datetime import timezone

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in
            (state.TELEMETRY_CHARGES_KEY, state.TELEMETRY_CHARGE_SHADOW_KEY,
             state.TELEMETRY_LATEST_KEY)}
    vin = "CHARGER000000001"
    try:
        for key in prev:
            state.put(sess, key, "")
        sess.commit()

        t = _time.time() - 3000
        with TestClient(app) as client:
            def post(ts, **fields):
                keys = {"ac": "ACChargingEnergyIn", "dc": "DCChargingEnergyIn",
                        "kwh": "EnergyRemaining", "soc": "Soc",
                        "kw": "ACChargingPower", "st": "DetailedChargeState"}
                data = [{"key": keys[k],
                         "value": ({"stringValue": v} if k == "st"
                                   else {"doubleValue": v})}
                        for k, v in fields.items()]
                stamp = datetime.fromtimestamp(ts, timezone.utc).isoformat(
                    ).replace("+00:00", "Z")
                r = client.post("/api/telemetry", json={"records": [
                    {"vin": vin, "createdAt": stamp, "data": data}]})
                assert r.status_code == 200, r.text

            # The real figures off the AC session of 10 September.
            post(t, st="DetailedChargeStateCharging", ac=4.715, dc=4.480,
                 kwh=44.44, soc=60.53, kw=7.5)
            post(t + 120, ac=5.092, dc=4.840, kwh=44.74, soc=61.06)
            post(t + 240, ac=5.218, dc=4.960, kwh=44.88, soc=61.20, kw=7.6)
            post(t + 300, st="DetailedChargeStateDisconnected")

            body = client.get("/api/telemetry/charges").json()

        assert body["charges"] == 1, body
        row = body["recent"][0]
        assert row["kwh_wall"] == pytest.approx(0.503, abs=0.001)
        assert row["kwh_pack_meter"] == pytest.approx(0.480, abs=0.001)
        assert row["kwh_pack_level"] == pytest.approx(0.440, abs=0.001)
        assert row["meters_agree_pct"] == pytest.approx(95.4, abs=0.5)
        assert row["pack_vs_wall_pct"] == pytest.approx(87.5, abs=0.5)
        assert row["peak_kw"] == pytest.approx(7.6)
        assert row["vin"] == vin
    finally:
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_recovering_gaps_on_stored_trips_previews_before_it_writes():
    """Trips closed before the correction existed can still be paid back.

    And must not be paid twice. The preview runs the same function on the
    same objects, so the guard that matters is that a preview writes nothing
    and an apply run over an already-corrected store finds no gap left.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    try:
        now = sync_mod.now_local()

        def trip(mins_ago, start_odo, end_odo, ended_on):
            at = now - timedelta(minutes=mins_ago)
            end = at + timedelta(minutes=10)
            return {"vin": "BACKFILL00000001",
                    "start_ts": at.replace(tzinfo=sync_mod.MYT).timestamp(),
                    "end_ts": end.replace(tzinfo=sync_mod.MYT).timestamp(),
                    "start_time": at.isoformat(timespec="seconds"),
                    "end_time": end.isoformat(timespec="seconds"),
                    "distance_km": round(end_odo - start_odo, 3),
                    "duration_min": 10.0, "energy_kwh": 0.88,
                    "wh_per_km": round(0.88 * 1000.0 / (end_odo - start_odo), 1),
                    "start_odo_km": start_odo, "end_odo_km": end_odo,
                    "ended_on": ended_on}

        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([
            trip(120, 31158.066, 31161.861, "stream_lost"),
            trip(60, 31162.199, 31166.0, "exit"),
        ]))
        sess.commit()

        with TestClient(app) as client:
            preview = client.get("/api/telemetry/recover-gaps").json()
            assert preview["would_recover"] == 1, preview
            assert preview["trips"][0]["recovered_km"] == pytest.approx(0.338, abs=0.002)
            # Preview means preview.
            stored = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
            assert stored[0]["distance_km"] == 3.795

            done = client.get("/api/telemetry/recover-gaps?apply=true").json()
            assert done["recovered"] == 1
            stored = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
            assert stored[0]["distance_km"] == pytest.approx(4.133, abs=0.002)

            # Run it again: the trips now meet, so there is nothing to take.
            again = client.get("/api/telemetry/recover-gaps?apply=true").json()
            assert again["recovered"] == 0
            stored = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
            assert stored[0]["distance_km"] == pytest.approx(4.133, abs=0.002)
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_the_next_morning_s_departure_pays_back_last_night_s_arrival():
    """Two journeys through the real ingest path, and the metres between them.

    The car parks underground, loses signal before it can send ShiftStateP,
    and sleeps — so unlike a blackout it drives through, nothing is replayed
    and that arrival is never transmitted at all. The odometer does not care:
    it counts up, so the next morning's opening reading measures the ground
    the lost arrival covered.

    Driven through the HTTP path rather than the state machine because the
    wiring is where this can go wrong — the last time a trip was corrected
    from a neighbouring one it reached past the newest trip and counted a
    whole journey twice.
    """
    import json as _json
    import time as _time

    from app.database import SessionLocal
    from app.api import routes as routes_mod
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev_trips = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    prev_shadow = state.get(sess, state.TELEMETRY_SHADOW_KEY)
    vin = "SLEEPGAP00000001"
    mi = sync_mod.MILES_TO_KM
    lost_km = 0.338                       # the real one, off trip 535
    try:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, "[]")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, "{}")
        sess.commit()

        # Far enough back that the morning is a night away from the night —
        # beyond SHADOW_TAIL_SEC, so the late-arrival path cannot claim these
        # metres and this is genuinely the sleep case.
        t = _time.time() - 50000
        with TestClient(app) as client:
            def post(*records):
                resp = client.post("/api/telemetry", json={"records": list(records)})
                assert resp.status_code == 200, resp.text
                return resp.json()

            def drive(from_km, to_km, at):
                post(_tele_record(vin, at, odo=from_km / mi, mph=0.0,
                                  gear="ShiftStateP", kwh=56.0, soc=80.0))
                at += 10
                post(_tele_record(vin, at, mph=25.0, gear="ShiftStateD"))
                for i in range(1, 21):
                    at += 30
                    post(_tele_record(vin, at,
                                      odo=(from_km + (to_km - from_km) * i / 20) / mi,
                                      kwh=56.0 - 1.0 * i / 20))
                return at

            # Last night: reaches 31,161.861 and the signal dies there.
            t = drive(31158.066, 31161.861, t)
            assert routes_mod._settle_shadows(SessionLocal()) == 1
            first = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))[0]
            assert first["ended_on"] == "stream_lost"
            assert first["end_odo_km"] == pytest.approx(31161.861, abs=0.002)

            # This morning, 0.338 km further on than it was last seen. The
            # correction must land as the new trip OPENS — waiting for it to
            # finish would leave last night recorded short for the whole
            # drive, with the number to fix it already in hand.
            t0 = t + 43000
            post(_tele_record(vin, t0, odo=(31161.861 + lost_km) / mi, mph=0.0,
                              gear="ShiftStateP", kwh=56.0, soc=80.0))
            post(_tele_record(vin, t0 + 10, mph=25.0, gear="ShiftStateD"))
            opened = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
            assert opened[0]["recovered_km"] == pytest.approx(lost_km, abs=0.002), \
                "paid back at the departure, not at the next arrival"

            t = drive(31161.861 + lost_km, 31166.0, t + 43000)
            post(_tele_record(vin, t + 5, mph=0.0, gear="ShiftStateP", seat=False))
            assert routes_mod._settle_shadows(SessionLocal()) == 1

            trips = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))

        assert len(trips) == 2, [t_["start_time"] for t_ in trips]
        night, morning = trips
        assert night["recovered_km"] == pytest.approx(lost_km, abs=0.002)
        # And by this path, not the late-arrival one, which the night ruled out.
        assert night.get("tail_amended_km") is None
        assert night["end_odo_km"] == pytest.approx(morning["start_odo_km"], abs=0.002)
        # Distance recomputed from the bracket, so the two trips now meet and
        # nothing is left in the gap between them.
        assert night["distance_km"] == pytest.approx(3.795 + lost_km, abs=0.003)
        # The morning's own distance is untouched — the gap is paid to the
        # trip that drove it, not taken from the one that found it.
        assert morning["distance_km"] == pytest.approx(31166.0 - 31161.861 - lost_km,
                                                       abs=0.01)
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev_trips or "[]")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, prev_shadow or "{}")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_a_whole_drive_through_the_real_ingest_path():
    """One journey, posted the way the car sends it, checked against the car.

    The unit tests drive the state machine directly. This drives it the way
    production does: one field per record, state serialised to JSON and read
    back between every batch. The figures are the 11 km trip the car itself
    displayed as 10.9 km / 178.3 Wh/km.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev_trips = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    prev_shadow = state.get(sess, state.TELEMETRY_SHADOW_KEY)
    prev_latest = state.get(sess, state.TELEMETRY_LATEST_KEY)
    vin = "WHOLEDRIVE000001"
    mi = sync_mod.MILES_TO_KM
    start_mi, end_mi = 31107.098 / mi, 31117.988 / mi
    try:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, "[]")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, "{}")
        sess.commit()

        t = 1_788_866_978.0
        with TestClient(app) as client:
            def post(*records):
                resp = client.post("/api/telemetry", json={"records": list(records)})
                assert resp.status_code == 200, resp.text

            post(_tele_record(vin, t, odo=start_mi, mph=0.0,
                              gear="ShiftStateP", kwh=56.56, soc=80.0))
            t += 10
            post(_tele_record(vin, t, mph=25.0, gear="ShiftStateD"))
            steps = 57
            for i in range(1, steps + 1):
                frac = i / steps
                t += 28
                post(_tele_record(vin, t, odo=start_mi + (end_mi - start_mi) * frac))
                t += 1
                post(_tele_record(vin, t, kwh=56.56 - 1.96 * frac))
                t += 1
                post(_tele_record(vin, t, soc=80.0 - 3.0 * frac))
            stop = t + 1
            post(_tele_record(vin, stop, mph=0.0, gear="ShiftStateP", seat=False))
            # Stationary readings after parking measure the arrival better than
            # the ones held at the instant P was reached.
            post(_tele_record(vin, stop + 30, odo=end_mi, kwh=54.60, soc=77.0))
            post(_tele_record(vin, stop + 200, mph=0.0))

        trips = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
        assert len(trips) == 1, trips
        trip = trips[0]
        assert trip["distance_km"] == pytest.approx(10.890, abs=0.002)
        assert trip["energy_kwh"] == pytest.approx(1.96, abs=0.001)
        assert trip["wh_per_km"] == pytest.approx(180.0, abs=0.1)
        assert trip["soc_start"] == 80.0 and trip["soc_end"] == 77.0
        assert trip["ended_on"] == "exit"
        # Closed by the live path, so the machine is clear and reusable.
        shadow = _json.loads(state.get(SessionLocal(), state.TELEMETRY_SHADOW_KEY))[vin]
        assert not shadow.get("open") and not shadow.get("out_of_order")
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev_trips or "[]")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, prev_shadow or "{}")
        state.put(sess, state.TELEMETRY_LATEST_KEY, prev_latest or "{}")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_carpark_blackout_and_replay_end_to_end():
    """Park underground, lose the stream, get it back on the way out.

    The arrival is measured and transmitted, just late. Losing it costs the
    same tail on every trip that ends in the same carpark, so this checks the
    recovery lands exactly and does not bleed into the journey that follows.
    """
    import json as _json
    import time as _time

    from app.database import SessionLocal
    from app.api import routes as routes_mod
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev_trips = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    prev_shadow = state.get(sess, state.TELEMETRY_SHADOW_KEY)
    vin = "CARPARK000000001"
    mi = sync_mod.MILES_TO_KM
    start_km, end_km, lost_km = 31200.0, 31211.0, 0.278
    try:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, "[]")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, "{}")
        sess.commit()

        # Anchored near now, because settle_shadow works in true epoch seconds.
        t = _time.time() - 3000
        with TestClient(app) as client:
            def post(*records):
                resp = client.post("/api/telemetry", json={"records": list(records)})
                assert resp.status_code == 200, resp.text

            post(_tele_record(vin, t, odo=start_km / mi, mph=0.0,
                              gear="ShiftStateP", kwh=56.0, soc=80.0))
            t += 10
            post(_tele_record(vin, t, mph=25.0, gear="ShiftStateD"))
            steps = 40
            for i in range(1, steps + 1):
                frac = i / steps
                t += 28
                post(_tele_record(vin, t, odo=(start_km + (end_km - start_km - lost_km) * frac) / mi))
                t += 2
                post(_tele_record(vin, t, kwh=56.0 - 2.0 * frac))
            stop = t + 1
            post(_tele_record(vin, stop, mph=0.0, gear="ShiftStateP", seat=False))
            # Signal dies here: the last odometer seen is short by lost_km.

            assert routes_mod._settle_shadows(SessionLocal()) == 1
            short = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))[0]
            assert short["distance_km"] == pytest.approx(11.0 - lost_km, abs=0.002)
            closed_at = short["end_time"]

            # Coverage returns and the car replays what it buffered.
            post(_tele_record(vin, stop + 20, resend=True,
                              odo=end_km / mi, kwh=54.0, soc=77.0),
                 _tele_record(vin, stop + 40, resend=True, mph=0.0))

            healed = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
            assert len(healed) == 1, "the replay must not become a second trip"
            assert healed[0]["distance_km"] == pytest.approx(11.0, abs=0.002)
            assert healed[0]["tail_amended_km"] == pytest.approx(lost_km, abs=0.002)
            # Only what it had driven is re-read; when it stopped is not.
            assert healed[0]["end_time"] == closed_at

            # Drive away again. The second journey must neither be swallowed
            # by the first nor swallow it.
            t2 = stop + 1200
            post(_tele_record(vin, t2, mph=20.0, gear="ShiftStateD"))
            for i in range(1, 9):
                t2 += 30
                post(_tele_record(vin, t2, odo=(end_km + 0.25 * i) / mi,
                                  kwh=54.0 - 0.05 * i))
            t2 += 10
            post(_tele_record(vin, t2, mph=0.0, gear="ShiftStateP", seat=False))
            post(_tele_record(vin, t2 + 200, odo=(end_km + 2.0) / mi, kwh=53.6))

        both = _json.loads(state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY))
        assert len(both) == 2
        spans = [(x["start_odo_km"], x["end_odo_km"]) for x in both]
        assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:])), \
            f"odometer spans overlap, so distance is counted twice: {spans}"
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev_trips or "[]")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, prev_shadow or "{}")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_telemetry_is_carried_into_the_drive_history_without_losing_polling():
    """The switch: streamed trips become the figures the dashboard reads.

    Telemetry earned it over ten judged trips — distance total -0.4% against
    the car with a worst trip of 0.9%, where polling ran +0.4% with a worst
    of 3.7%. But the dashboard, the analysis and the alerts all read the
    Drive table, so being more accurate in a JSON blob beside it was worth
    nothing to anybody.

    Three things this must get right, and all three are here: it corrects a
    polled row rather than adding a second one; it keeps what polling said so
    the two sources stay independent; and running it again changes nothing.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle
    from app.api import routes as routes_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    made, vehicle = [], None
    try:
        now = sync_mod.now_local()

        def _epoch(local):
            return local.replace(tzinfo=sync_mod.MYT).timestamp()

        with TestClient(app) as client:
            vehicle = Vehicle(vin="PROMOTE00000001", name="Test", model="Model 3")
            sess.add(vehicle)
            sess.commit()

            # What polling logged: 11.4 km, and 16 minutes late off the line.
            at = now - timedelta(minutes=90)
            polled = Drive(vehicle_id=vehicle.id,
                           start_time=at + timedelta(minutes=16),
                           end_time=at + timedelta(minutes=35),
                           distance_km=11.4, duration_min=19.0,
                           start_soc=60, end_soc=57, energy_used_kwh=1.73,
                           avg_speed_kmh=36, max_speed_kmh=70, outside_temp_c=29)
            sess.add(polled)
            sess.commit()
            made.append(polled.id)

            # What the car streamed: the same journey, seen from the start.
            trip = {"vin": "PROMOTE00000001",
                    "start_ts": _epoch(at), "end_ts": _epoch(at + timedelta(minutes=35)),
                    "start_time": at.isoformat(timespec="seconds"),
                    "end_time": (at + timedelta(minutes=35)).isoformat(timespec="seconds"),
                    "distance_km": 11.25, "duration_min": 35.1, "energy_kwh": 1.9,
                    "wh_per_km": 168.9, "soc_start": 60.5, "soc_end": 57.9,
                    "max_speed_kmh": 71.2, "avg_speed_kmh": 19.2,
                    "idle_min": 4.0, "idle_tracked": True, "climate_min": 12.0,
                    "start_odo_km": 31162.199, "end_odo_km": 31173.449,
                    "ended_on": "exit"}
            state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([trip]))
            sess.commit()

            preview = client.get("/api/telemetry/promote").json()
            assert preview["would_change"] == 1, preview
            assert preview["trips"][0]["action"] == "correct"
            with SessionLocal() as chk:
                assert chk.get(Drive, polled.id).distance_km == 11.4, "preview wrote"

            done = client.get("/api/telemetry/promote?apply=true").json()
            assert done["changed"] == 1

        with SessionLocal() as chk:
            row = chk.get(Drive, polled.id)
            assert row.source == "telemetry"
            assert row.distance_km == pytest.approx(11.25, abs=0.001)
            assert row.energy_used_kwh == pytest.approx(1.9, abs=0.001)
            # The 16-minute blind head is gone: the row now starts when the
            # car actually moved, which is the whole point of the switch.
            assert row.duration_min == pytest.approx(35.1, abs=0.1)
            # And the row stops claiming its efficiency is a speed-based
            # estimate: the stream measured the stop, so idle_tracked is
            # true and the dashboard's "estimated" badge clears.
            assert row.idle_tracked is True
            assert row.idle_min == pytest.approx(4.0, abs=0.01)
            assert row.climate_min == pytest.approx(12.0, abs=0.01)
            # And polling's own figures survive, or the comparison would be
            # scoring telemetry against itself from here on.
            assert row.polled_km == pytest.approx(11.4, abs=0.001)
            assert row.polled_kwh == pytest.approx(1.73, abs=0.001)
            assert chk.query(Drive).filter(Drive.vehicle_id == vehicle.id).count() == 1

        # Again: corrects the same row, keeps polling's original, adds nothing.
        with TestClient(app) as client:
            client.get("/api/telemetry/promote?apply=true")
        with SessionLocal() as chk:
            row = chk.get(Drive, polled.id)
            assert row.polled_km == pytest.approx(11.4, abs=0.001), \
                "a second run overwrote polling with telemetry's own answer"
            assert chk.query(Drive).filter(Drive.vehicle_id == vehicle.id).count() == 1
    finally:
        with SessionLocal() as cleanup:
            for drive_id in made:
                d = cleanup.get(Drive, drive_id)
                if d is not None:
                    cleanup.delete(d)
            if vehicle is not None:
                for d in cleanup.query(Drive).filter(
                        Drive.vehicle_id == vehicle.id).all():
                    cleanup.delete(d)
                v = cleanup.get(Vehicle, vehicle.id)
                if v is not None:
                    cleanup.delete(v)
            cleanup.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_a_trip_never_takes_a_drive_that_belongs_to_a_later_one():
    """The fault the first real preview showed, in miniature.

    Fourteen trips polling had certainly logged came back as "add". In a
    drive history that is not a wrong number, it is a duplicate journey — so
    the matching now uses the comparison's rules, which have been pairing
    these two sources correctly for days: the drive that overlaps MOST, and
    only if no earlier trip has taken it.

    Here trip A overlaps both drives and would take the wrong one on a
    first-overlap rule, leaving trip B to ask for a duplicate.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle
    from app.api import routes as routes_mod

    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    vehicle = None
    try:
        now = sync_mod.now_local()

        def _epoch(local):
            return local.replace(tzinfo=sync_mod.MYT).timestamp()

        vehicle = Vehicle(vin="OVERLAP000000001", name="Test", model="Model 3")
        sess.add(vehicle)
        sess.commit()

        def drive(from_min, to_min, km):
            d = Drive(vehicle_id=vehicle.id,
                      start_time=now - timedelta(minutes=from_min),
                      end_time=now - timedelta(minutes=to_min),
                      distance_km=km, duration_min=from_min - to_min,
                      start_soc=60, end_soc=57, energy_used_kwh=km * 0.17,
                      avg_speed_kmh=30, max_speed_kmh=60, outside_temp_c=29)
            sess.add(d)
            sess.commit()
            return d.id

        def trip(from_min, to_min, km):
            a = now - timedelta(minutes=from_min)
            b = now - timedelta(minutes=to_min)
            return {"vin": "OVERLAP000000001", "start_ts": _epoch(a),
                    "end_ts": _epoch(b),
                    "start_time": a.isoformat(timespec="seconds"),
                    "end_time": b.isoformat(timespec="seconds"),
                    "distance_km": km, "duration_min": from_min - to_min,
                    "energy_kwh": km * 0.17}

        # A brushes the end of the first drive by a minute and covers the
        # second almost exactly. First-overlap grabs the first; most-overlap
        # takes the one it belongs to.
        first, second = drive(90, 61, 8.0), drive(60, 40, 6.0)
        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps(
            [trip(62, 41, 6.1), trip(89, 62, 8.1)]))
        sess.commit()

        out = routes_mod._promote_shadow_trips(SessionLocal(), apply=False)
        by_start = {e["start"]: e for e in out}
        assert all(e["action"] == "correct" for e in out), out
        assert {e["was"]["id"] for e in out} == {first, second}, out
    finally:
        with SessionLocal() as cleanup:
            if vehicle is not None:
                for d in cleanup.query(Drive).filter(
                        Drive.vehicle_id == vehicle.id).all():
                    cleanup.delete(d)
                v = cleanup.get(Vehicle, vehicle.id)
                if v is not None:
                    cleanup.delete(v)
            cleanup.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()


def test_a_row_telemetry_added_can_still_be_judged_against_the_car():
    """A journey polling never saw is exactly the one telemetry exists to
    capture, so it must not be excluded from the accuracy report.

    These rows used to be filtered out, and rightly so while the comparison
    was telemetry against polling: pairing a telemetry trip with a row
    telemetry wrote would have reported a delta of zero and added a perfect
    agreement to every median.

    That reasoning does not survive the reference changing. Against the CAR,
    the row is not the thing being compared — it is only how a car reading is
    found, since readings are recorded against a drive id. Measured, 11
    September: the 11:48 trip was promoted as a new row, the filter hid it,
    and the trip reported drive_id null with no vs_car at all.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    vehicle = None
    try:
        now = sync_mod.now_local()
        at = now - timedelta(minutes=50)
        vehicle = Vehicle(vin="SELFPAIR00000001", name="Test", model="Model 3")
        sess.add(vehicle)
        sess.commit()
        # The row promotion would have created: telemetry's, nothing preserved.
        added = Drive(vehicle_id=vehicle.id, start_time=at,
                      end_time=at + timedelta(minutes=8), distance_km=2.791,
                      duration_min=8.3, start_soc=60, end_soc=59,
                      energy_used_kwh=0.74, avg_speed_kmh=20, max_speed_kmh=50,
                      outside_temp_c=29, source="telemetry",
                      shadow_start_ts=at.replace(tzinfo=sync_mod.MYT).timestamp())
        sess.add(added)
        sess.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([{
            "vin": "SELFPAIR00000001",
            "start_ts": at.replace(tzinfo=sync_mod.MYT).timestamp(),
            "end_ts": (at + timedelta(minutes=8)).replace(
                tzinfo=sync_mod.MYT).timestamp(),
            "start_time": at.isoformat(timespec="seconds"),
            "end_time": (at + timedelta(minutes=8)).isoformat(timespec="seconds"),
            "distance_km": 2.791, "duration_min": 8.3, "energy_kwh": 0.74,
            "wh_per_km": 265.1}]))
        sess.commit()

        with TestClient(app) as client:
            body = client.get("/api/telemetry/compare?days=1").json()
        row = next(r for r in body["trips"]
                   if r["telemetry"]["start"] == at.isoformat(timespec="seconds"))
        # The row IS found, so a car reading recorded against it can attach.
        assert row["drive_id"] is not None, \
            "a promoted row was hidden, so its car reading has nowhere to go"
        # And its own figures are nowhere in the report: there is no polled
        # block any more, so telemetry cannot be scored against itself
        # however the rows are matched.
        assert "polled" not in row and "delta" not in row
    finally:
        with SessionLocal() as cleanup:
            if vehicle is not None:
                for d in cleanup.query(Drive).filter(
                        Drive.vehicle_id == vehicle.id).all():
                    cleanup.delete(d)
                v = cleanup.get(Vehicle, vehicle.id)
                if v is not None:
                    cleanup.delete(v)
            cleanup.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_an_unattended_run_refuses_to_add_a_wall_of_rows():
    """The guard that would have stopped 183 duplicates at one.

    A tick running every minute has at most one finished journey to carry
    across. Many at once means the matching has stopped recognising the
    history, which is exactly what happened — and an unattended write to real
    records must fail by doing nothing, not by doing all of it.

    The cap binds only the automatic path. A person who has read the preview
    can apply what the preview showed them.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle
    from app.api import routes as routes_mod

    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    vehicle = None
    try:
        now = sync_mod.now_local()
        vehicle = Vehicle(vin="FLOOD00000000001", name="Test", model="Model 3")
        sess.add(vehicle)
        sess.commit()

        trips = []
        for i in range(6):
            at = now - timedelta(hours=8 - i)
            trips.append({
                "vin": "FLOOD00000000001",
                "start_ts": at.replace(tzinfo=sync_mod.MYT).timestamp(),
                "end_ts": (at + timedelta(minutes=15)).replace(
                    tzinfo=sync_mod.MYT).timestamp(),
                "start_time": at.isoformat(timespec="seconds"),
                "end_time": (at + timedelta(minutes=15)).isoformat(timespec="seconds"),
                "distance_km": 9.0 + i, "duration_min": 15.0, "energy_kwh": 1.6})
        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps(trips))
        sess.commit()

        refused = routes_mod._promote_shadow_trips(
            SessionLocal(), apply=True, max_add=routes_mod.PROMOTE_AUTO_MAX_ADD)
        assert refused[0]["action"] == "refused", refused
        assert refused[0]["would_add"] == 6
        with SessionLocal() as chk:
            assert chk.query(Drive).filter(
                Drive.vehicle_id == vehicle.id).count() == 0, \
                "a refused run wrote something"

        # Uncapped — a person applying what they have read — goes through.
        routes_mod._promote_shadow_trips(SessionLocal(), apply=True)
        with SessionLocal() as chk:
            assert chk.query(Drive).filter(
                Drive.vehicle_id == vehicle.id).count() == 6
    finally:
        with SessionLocal() as cleanup:
            if vehicle is not None:
                for d in cleanup.query(Drive).filter(
                        Drive.vehicle_id == vehicle.id).all():
                    cleanup.delete(d)
                v = cleanup.get(Vehicle, vehicle.id)
                if v is not None:
                    cleanup.delete(v)
            cleanup.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()


def test_promotion_recognises_its_own_rows_across_a_storage_round_trip():
    """The fault that put 198 drives that never happened into the history.

    Identity was decided by float equality on a true epoch near 1.79e9 with a
    fractional part. It does not come back from Postgres bit-identical, so
    every automatic run failed to recognise what the previous run had written
    and added the same journeys again — fourteen a tick, for fourteen ticks.

    SQLite round-trips the value exactly, which is why every test passed. So
    this one perturbs the stored value the way a database would and insists
    the trip is still recognised.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle
    from app.api import routes as routes_mod

    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    vehicle = None
    try:
        now = sync_mod.now_local()
        at = now - timedelta(minutes=70)
        start_ts = at.replace(tzinfo=sync_mod.MYT).timestamp() + 0.708992

        vehicle = Vehicle(vin="ROUNDTRIP0000001", name="Test", model="Model 3")
        sess.add(vehicle)
        sess.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([{
            "vin": "ROUNDTRIP0000001", "start_ts": start_ts,
            "end_ts": start_ts + 1200,
            "start_time": at.isoformat(timespec="seconds"),
            "end_time": (at + timedelta(minutes=20)).isoformat(timespec="seconds"),
            "distance_km": 11.25, "duration_min": 20.0, "energy_kwh": 1.9}]))
        sess.commit()

        assert routes_mod._promote_shadow_trips(
            SessionLocal(), apply=True)[0]["action"] == "add"
        with SessionLocal() as chk:
            row = chk.query(Drive).filter(Drive.vehicle_id == vehicle.id).one()
            # What a float column that is not quite double precision does to
            # a number this size: at 1.79e9 a single-precision float resolves
            # about every 128 seconds, so not even whole seconds survive.
            row.shadow_start_ts = float(f"{row.shadow_start_ts:.7g}")
            chk.commit()
            assert int(row.shadow_start_ts) != int(start_ts), \
                "the perturbation has to actually break the float"

        again = routes_mod._promote_shadow_trips(SessionLocal(), apply=True)
        assert again[0]["action"] == "correct", again
        with SessionLocal() as chk:
            assert chk.query(Drive).filter(
                Drive.vehicle_id == vehicle.id).count() == 1, \
                "the same journey was written twice"
    finally:
        with SessionLocal() as cleanup:
            if vehicle is not None:
                for d in cleanup.query(Drive).filter(
                        Drive.vehicle_id == vehicle.id).all():
                    cleanup.delete(d)
                v = cleanup.get(Vehicle, vehicle.id)
                if v is not None:
                    cleanup.delete(v)
            cleanup.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()


def test_a_corrected_row_can_be_put_back_the_way_polling_had_it():
    """polled_km was called "the way back" before there was one."""
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    vehicle = None
    try:
        now = sync_mod.now_local()

        def _epoch(local):
            return local.replace(tzinfo=sync_mod.MYT).timestamp()

        with TestClient(app) as client:
            # Promotion now runs on telemetry ingest as well as on the sync
            # tick, so any earlier test that posted a batch has left rows of
            # its own in this shared database. Cleared first, or this measures
            # them instead of its own.
            for stray in sess.scalars(
                    select(Drive).where(Drive.source == "telemetry",
                                        Drive.polled_km.is_(None))).all():
                sess.delete(stray)
            sess.commit()
            vehicle = Vehicle(vin="UNDO00000000001", name="Test", model="Model 3")
            sess.add(vehicle)
            sess.commit()
            at = now - timedelta(minutes=80)
            polled = Drive(vehicle_id=vehicle.id, start_time=at + timedelta(minutes=2),
                           end_time=at + timedelta(minutes=20), distance_km=11.4,
                           duration_min=18, start_soc=60, end_soc=57,
                           energy_used_kwh=1.73, avg_speed_kmh=38,
                           max_speed_kmh=70, outside_temp_c=29)
            sess.add(polled)
            sess.commit()
            # And one telemetry alone saw, which has nothing to go back to.
            solo = now - timedelta(minutes=40)
            state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([
                {"vin": "UNDO00000000001", "start_ts": _epoch(at),
                 "end_ts": _epoch(at + timedelta(minutes=20)),
                 "start_time": at.isoformat(timespec="seconds"),
                 "end_time": (at + timedelta(minutes=20)).isoformat(timespec="seconds"),
                 "distance_km": 11.25, "duration_min": 20.0, "energy_kwh": 1.9},
                {"vin": "UNDO00000000001", "start_ts": _epoch(solo),
                 "end_ts": _epoch(solo + timedelta(minutes=8)),
                 "start_time": solo.isoformat(timespec="seconds"),
                 "end_time": (solo + timedelta(minutes=8)).isoformat(timespec="seconds"),
                 "distance_km": 2.79, "duration_min": 8.0, "energy_kwh": 0.74}]))
            sess.commit()
            client.get("/api/telemetry/promote?apply=true")

            undo = client.get("/api/telemetry/unpromote?apply=true").json()

        assert undo["restored"] == 1, undo
        assert len(undo["kept_because_polling_never_saw_them"]) == 1
        with SessionLocal() as chk:
            row = chk.get(Drive, polled.id)
            assert row.distance_km == pytest.approx(11.4, abs=0.001)
            assert row.energy_used_kwh == pytest.approx(1.73, abs=0.001)
            assert row.source == "" and row.shadow_start_ts is None
            assert row.polled_km is None
            # The added journey stays: deleting one the car really drove is a
            # worse answer than keeping one whose figures are argued about.
            assert chk.query(Drive).filter(
                Drive.vehicle_id == vehicle.id).count() == 2
    finally:
        with SessionLocal() as cleanup:
            if vehicle is not None:
                for d in cleanup.query(Drive).filter(
                        Drive.vehicle_id == vehicle.id).all():
                    cleanup.delete(d)
                v = cleanup.get(Vehicle, vehicle.id)
                if v is not None:
                    cleanup.delete(v)
            cleanup.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_a_trip_telemetry_alone_saw_is_added_rather_than_lost():
    """Polling misses whole journeys — one evening it merged two into one.

    Where telemetry has a trip and polling has nothing covering it, the row
    is added. That is the half of the switch that grows the history rather
    than correcting it.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    vehicle = None
    try:
        now = sync_mod.now_local()

        def _epoch(local):
            return local.replace(tzinfo=sync_mod.MYT).timestamp()

        with TestClient(app) as client:
            vehicle = Vehicle(vin="ADDONLY000000001", name="Test", model="Model 3")
            sess.add(vehicle)
            sess.commit()
            at = now - timedelta(minutes=45)
            state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([{
                "vin": "ADDONLY000000001",
                "start_ts": _epoch(at), "end_ts": _epoch(at + timedelta(minutes=8)),
                "start_time": at.isoformat(timespec="seconds"),
                "end_time": (at + timedelta(minutes=8)).isoformat(timespec="seconds"),
                "distance_km": 2.791, "duration_min": 8.3, "energy_kwh": 0.74,
                "wh_per_km": 265.1, "ended_on": "stream_lost"}]))
            sess.commit()

            assert client.get("/api/telemetry/promote").json()["trips"][0]["action"] == "add"
            client.get("/api/telemetry/promote?apply=true")

        with SessionLocal() as chk:
            rows = chk.query(Drive).filter(Drive.vehicle_id == vehicle.id).all()
            assert len(rows) == 1
            assert rows[0].source == "telemetry"
            assert rows[0].distance_km == pytest.approx(2.791, abs=0.001)
            # Nothing to preserve: polling never saw this one.
            assert rows[0].polled_km is None
    finally:
        with SessionLocal() as cleanup:
            if vehicle is not None:
                for d in cleanup.query(Drive).filter(
                        Drive.vehicle_id == vehicle.id).all():
                    cleanup.delete(d)
                v = cleanup.get(Vehicle, vehicle.id)
                if v is not None:
                    cleanup.delete(v)
            cleanup.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_a_trip_that_is_mostly_rounding_does_not_referee_the_others():
    """Two kinds of trip cannot judge, and averaging them in makes it worse.

    A 0.34 kWh trip carries a 0.02 kWh step, so nearly six percent of its
    energy figure is quantisation — it cannot say whether either source is
    three percent out. And a trip with no odometer bracket was measured
    before that fix existed; it is the -12.5% outlier dragging the median
    around, and it is history rather than a live defect.

    Both stay in the report. Neither enters the medians, and the reason is
    named, because a judged count that quietly shrinks is how a comparison
    stops meaning anything without anyone noticing.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod
    from app.models import Drive, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in
            (state.TELEMETRY_TRIPS_KEY, state.CAR_READINGS_KEY)}
    made = []
    try:
        now = sync_mod.now_local()

        def _epoch(local):
            return local.replace(tzinfo=sync_mod.MYT).timestamp()

        vehicle = Vehicle(vin="TESTVIN-REFEREE", name="Test", model="Model 3")
        sess.add(vehicle)
        sess.commit()

        def pair(mins_ago, km, kwh, odo, mins):
            at = now - timedelta(minutes=mins_ago)
            end = at + timedelta(minutes=mins)
            d = Drive(vehicle_id=vehicle.id, start_time=at, end_time=end,
                      distance_km=km, duration_min=mins, start_soc=60, end_soc=59,
                      energy_used_kwh=kwh, avg_speed_kmh=km * 6,
                      max_speed_kmh=60, outside_temp_c=29)
            sess.add(d)
            sess.commit()
            made.append(d.id)
            # Naive MYT wall-clock in, true epoch out. at.timestamp() would
            # read the naive value as the container's own zone and land the
            # trip eight hours from the drive it is meant to match.
            t = {"start_ts": _epoch(at), "end_ts": _epoch(end),
                 "start_time": at.isoformat(timespec="seconds"),
                 "end_time": end.isoformat(timespec="seconds"),
                 "distance_km": km, "duration_min": float(mins),
                 "energy_kwh": kwh,
                 "wh_per_km": round(kwh * 1000.0 / km, 1)}
            if odo:
                t["start_odo_km"], t["end_odo_km"] = odo, round(odo + km, 3)
            return t, {"drive_id": d.id, "km": km,
                       "wh_per_km": round(kwh * 1000.0 / km, 1),
                       "pct": round(kwh / 68.0 * 100.0, 2)}

        with TestClient(app) as client:      # startup may reseed, so build after
            # Ten-minute trips throughout, so one 60-second sampling
            # interval is a tenth of each trip's energy. 1.9 kWh carries
            # 0.19 and referees fine at 10%; 0.34 kWh carries 0.034, which
            # is also a tenth — so the tiny trip has to be made short as
            # well as small to be excluded, which is what a real one is.
            good, r1 = pair(180, 10.8, 1.9, 31138.7, 10.0)
            tiny, r2 = pair(120, 0.49, 0.34, 31150.0, 1.5)
            old, r3 = pair(60, 3.0, 0.51, None, 10.0)        # no odo bracket
            state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([good, tiny, old]))
            state.put(sess, state.CAR_READINGS_KEY, _json.dumps([r1, r2, r3]))
            sess.commit()
            body = client.get("/api/telemetry/compare").json()

        assert body["telemetry_trips"] == 3, "all three still reported"
        assert body["matched"] == 3, [r["drive_id"] for r in body["trips"]]
        assert body["judged"] == 1, body["not_judged"]
        # Summed as well as judged, over the same set. A boundary drawn in
        # the wrong place moves energy between two trips without losing any,
        # so a median calls that an error twice while a sum cancels it.
        # Two columns now, not three: totals are telemetry against the
        # car, and the polled sum moved to its own key because it is
        # taken over a different set of trips (see judged_polled), and
        # printing it alongside would invite an invalid comparison.
        assert body["totals"]["km"] == [10.8, 10.8], body["totals"]
        assert body["totals"]["order"] == "telemetry, car"
        # One figure: telemetry against the car. Polling is no longer a
        # column here at all — it was measuring at a cadence that cannot
        # referee a trip, and a second opinion from a worse instrument is
        # not a comparison.
        assert body["totals"]["km_err_pct"] == 0.0
        whys = " ".join(e["why"] for e in body["not_judged"])
        assert "quantisation" in whys and "no odometer bracket" in whys, whys
        # And the total carries what it is worth, which is the only thing
        # that says whether its error is a finding or a coin toss.
        assert body["totals"]["kwh_unc_pct"] is not None
    finally:
        for drive_id in made:
            d = sess.get(Drive, drive_id)
            if d is not None:
                sess.delete(d)
        sess.commit()
        sess.delete(vehicle)
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_compare_reports_distance_no_trip_covers():
    """A trip should end where the next one begins.

    When it does not, the odometer moved during a stretch no trip covers —
    a departure lost while the car was out of coverage, or a boundary drawn
    wrongly. Inventing a trip to hold it would be a guess; absorbing it into a
    neighbour would be worse. Report it, so an incomplete record cannot look
    complete. The real stream showed 31117.988 -> 31118.140 across one
    carpark blackout: 0.152 km driven that nothing accounts for.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    try:
        now = sync_mod.now_local()

        def trip(mins_ago, start_odo, end_odo):
            at = now - timedelta(minutes=mins_ago)
            end = at + timedelta(minutes=10)
            return {"start_ts": at.timestamp(), "end_ts": end.timestamp(),
                    "start_time": at.isoformat(timespec="seconds"),
                    "end_time": end.isoformat(timespec="seconds"),
                    "distance_km": round(end_odo - start_odo, 3),
                    "duration_min": 10.0, "energy_kwh": 1.0, "wh_per_km": 100.0,
                    "start_odo_km": start_odo, "end_odo_km": end_odo}

        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([
            trip(120, 31107.098, 31117.988),
            trip(60, 31118.140, 31120.640),      # 0.152 km unaccounted before it
            trip(30, 31120.640, 31121.640),      # continuous: no gap
        ]))
        sess.commit()

        with TestClient(app) as client:
            body = client.get("/api/telemetry/compare").json()
        gaps = [r["telemetry"]["odo_gap_before_km"] for r in body["trips"]]
        assert gaps == [None, pytest.approx(0.152, abs=0.001), None], gaps
        assert body["summary"]["unaccounted_km"] == pytest.approx(0.152, abs=0.001)
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_second_field_set_is_recorded_but_not_yet_believed():
    """LifetimeEnergyUsed and BMSState are carried raw, and derived from later.

    EnergyRemaining moves in steps of 0.02 kWh, which is the floor under every
    short-trip figure this project has produced. A lifetime counter has no such
    step, so measuring the same trip both ways says how much of a disagreement
    is quantisation. Its units are undocumented, though, and assuming one is
    how a systematic error gets buried — so it is recorded, not used.
    """
    from app import sync as sync_mod

    snap = sync_mod.snapshot_from_telemetry(
        {"Odometer": 19000.0, "LifetimeEnergyUsed": 4321.5,
         "BMSState": "BMSStateDrive", "VehicleSpeed": 30.0}, 1_788_900_000.0)
    assert snap["energy_used_raw"] == 4321.5      # unscaled
    assert snap["bms_state"] == "BMSStateDrive"
    # Absent on the current field set, and that must read as unknown.
    bare = sync_mod.snapshot_from_telemetry({"Odometer": 19000.0}, 1_788_900_000.0)
    assert bare["energy_used_raw"] is None and bare["bms_state"] is None
    # Semi-truck only, per Tesla's proto: it has never arrived on this car.
    assert bare["energy_drive_raw"] is None

    # A trip carries the counter's bracket as a difference, beside the
    # EnergyRemaining one, so the two can be compared on the same journey.
    shadow: dict = {}
    t = 1_788_900_000.0

    def s(ts, mi, **kw):
        base = {"Odometer": mi, "EnergyRemaining": kw.pop("kwh", 55.0),
                "LifetimeEnergyUsed": kw.pop("used", 4000.0), "Soc": 80.0}
        base.update(kw)
        return sync_mod.snapshot_from_telemetry(base, ts)

    sync_mod.advance_shadow(shadow, s(t, 19000.0))
    sync_mod.advance_shadow(shadow, s(t + 10, 19000.0, VehicleSpeed=30.0,
                                      Gear="ShiftStateD"))
    sync_mod.advance_shadow(shadow, s(t + 610, 19005.0, VehicleSpeed=30.0,
                                      Gear="ShiftStateD", kwh=53.4, used=4001.6))
    sync_mod.advance_shadow(shadow, s(t + 900, 19008.0, kwh=53.0, used=4002.0,
                                      DriverSeatOccupied=False))
    trip = sync_mod.advance_shadow(shadow, s(t + 1100, 19008.0, kwh=53.0,
                                             used=4002.0, DriverSeatOccupied=False))
    assert trip is not None
    assert trip["used_delta"] == pytest.approx(2.0, abs=0.001)
    assert trip["energy_kwh"] == pytest.approx(2.0, abs=0.001)
    # Nothing derived from it: energy still comes from EnergyRemaining alone.
    assert trip["wh_per_km"] == pytest.approx(
        trip["energy_kwh"] * 1000 / trip["distance_km"], abs=0.1)


def test_a_stale_gear_cannot_open_a_trip():
    """Gear streams only on change, so it can read Drive for hours.

    A car that loses signal while manoeuvring into an underground bay never
    sends ShiftStateP. The composite then reads Drive for as long as it stays
    offline, and on reconnect — still parked — that stale gear would open a
    journey the car is not on. Speed refreshes every ten seconds, so requiring
    motion to BEGIN a trip is what keeps a stale gear from inventing one. A
    trip still CONTINUES on gear alone: a car at a red light is in Drive and
    still on its journey.
    """
    from app import sync as sync_mod

    def snap(ts, mph, gear, mi=19348.0):
        return sync_mod.snapshot_from_telemetry(
            {"Odometer": mi, "VehicleSpeed": mph, "Gear": gear}, ts)

    t = 1_788_909_241.0
    parked_but_says_drive = snap(t, 0.0, "ShiftStateD")
    # The machine agrees the car is "driving" — that is the trap.
    assert sync_mod.is_driving(parked_but_says_drive)

    shadow: dict = {}
    for i in range(6):                    # six minutes of reconnected silence
        sync_mod.advance_shadow(shadow, snap(t + i * 60, 0.0, "ShiftStateD"))
    assert not shadow.get("open"), "a stale gear opened a trip on a parked car"

    # Real motion still opens one immediately.
    sync_mod.advance_shadow(shadow, snap(t + 400, 20.0, "ShiftStateD"))
    assert shadow.get("open")
    # And a stop at a light does not end it.
    sync_mod.advance_shadow(shadow, snap(t + 460, 0.0, "ShiftStateD", mi=19348.5))
    assert shadow.get("open") and shadow.get("still_since") is None


def test_a_trip_says_how_its_end_was_decided():
    """An arrival measured after parking is worth more than a last gasp.

    This car loses signal as it reaches its bay, so ShiftStateP never arrives
    and the journey is closed on silence instead. The end is then the last
    thing the car managed to send, and the trip is short by whatever it drove
    after that — the same carpark every evening, so a bias rather than noise.
    The trip has to carry which of those happened, or the two are averaged
    together as if equally measured.
    """
    from app import sync as sync_mod

    def snap(ts, mi, mph=0.0, gear="ShiftStateP", seat=True, doors=False):
        return sync_mod.snapshot_from_telemetry(
            {"Odometer": mi, "VehicleSpeed": mph, "Gear": gear,
             "DriverSeatOccupied": seat, "EnergyRemaining": 55.0,
             "DoorState": {"DriverFront": doors}}, ts)

    t, m = 1_788_909_000.0, 19000.0

    # Parked properly, with readings still arriving afterwards.
    sh: dict = {}
    sync_mod.advance_shadow(sh, snap(t, m, mph=20, gear="ShiftStateD"))
    sync_mod.advance_shadow(sh, snap(t + 600, m + 5, mph=20, gear="ShiftStateD"))
    sync_mod.advance_shadow(sh, snap(t + 900, m + 8, seat=False, doors=True))
    done = sync_mod.advance_shadow(sh, snap(t + 1100, m + 8, seat=False, doors=True))
    assert done["ended_on"] == "exit"

    # Signal lost on the way into the bay: no P, no readings after.
    sh = {}
    sync_mod.advance_shadow(sh, snap(t, m, mph=20, gear="ShiftStateD"))
    sync_mod.advance_shadow(sh, snap(t + 600, m + 5, mph=20, gear="ShiftStateD"))
    # Last thing it managed to send: still in Drive, almost stopped.
    sync_mod.advance_shadow(sh, snap(t + 660, m + 5.4, mph=1, gear="ShiftStateD"))
    done = sync_mod.settle_shadow(sh, t + 660 + 700)
    assert done["ended_on"] == "stream_lost"
    assert done["end_ts"] == t + 660
    # And the marker does not leak into the next journey.
    assert "stream_lost" not in sh


def test_silence_is_read_differently_depending_on_the_last_speed():
    """A car that went quiet at walking pace has arrived; at road speed it has not.

    This car reaches its bay underground and loses signal before it can send
    ShiftStateP — the journey is over and waiting ten minutes to say so leaves
    a finished trip absent. A car that goes quiet at 45 km/h is in a tunnel,
    and closing early would cut one drive into two. The wait changes no figure
    either way: the trip ends at the last record, and the wait only decides
    how soon it can be read.
    """
    from app import sync as sync_mod

    def snap(ts, mi, mph, gear="ShiftStateD"):
        return sync_mod.snapshot_from_telemetry(
            {"Odometer": mi, "VehicleSpeed": mph, "Gear": gear,
             "EnergyRemaining": 55.0}, ts)

    t, m = 1_788_909_000.0, 19000.0

    def drive_then_go_quiet(final_mph):
        sh: dict = {}
        sync_mod.advance_shadow(sh, snap(t, m, 20.0))
        sync_mod.advance_shadow(sh, snap(t + 600, m + 5, 20.0))
        sync_mod.advance_shadow(sh, snap(t + 660, m + 5.4, final_mph))
        return sh

    # Walking pace, then silence: this is an arrival.
    arriving = drive_then_go_quiet(0.6)          # ~1 km/h
    assert sync_mod.settle_shadow(arriving, t + 660 + 100) is None
    done = sync_mod.settle_shadow(arriving, t + 660 + 200)
    assert done is not None, "an arrival waited longer than three minutes"
    assert done["ended_on"] == "stream_lost"
    assert done["end_ts"] == t + 660          # the last record, not the close

    # Road speed, then silence: a tunnel, and the journey continues.
    driving = drive_then_go_quiet(28.0)          # ~45 km/h
    assert sync_mod.settle_shadow(driving, t + 660 + 200) is None
    assert sync_mod.settle_shadow(driving, t + 660 + 550) is None
    still = sync_mod.settle_shadow(driving, t + 660 + 700)
    assert still is not None and still["end_ts"] == t + 660


def test_mode_changes_are_logged_with_the_moment_they_happened():
    """BMSState and friends stream only when they change.

    The composite therefore holds the current value and nothing holds the
    moment it moved — and the moment is the question: a car parked with its
    driver aboard is ambiguous to this app for ten minutes by a timer, while
    the car itself decides at some point that the journey is over. If the pack
    leaves Drive, that is the car's own answer.
    """
    import json as _json

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_MODES_KEY)
    vin = "MODES00000000001"
    try:
        state.put(sess, state.TELEMETRY_MODES_KEY, "[]")
        sess.commit()

        with TestClient(app) as client:
            def post(ts, key, value, wrap="stringValue"):
                r = client.post("/api/telemetry", json={"records": [{
                    "vin": vin, "createdAt": ts,
                    "data": [{"key": key, "value": {wrap: value}}]}]})
                assert r.status_code == 200, r.text

            post("2026-09-09T14:00:00Z", "BMSState", "BMSStateDrive")
            post("2026-09-09T14:00:30Z", "CenterDisplay", "DisplayStateDriving")
            # Repeats are not changes and must not fill the log.
            post("2026-09-09T14:01:00Z", "BMSState", "BMSStateDrive")
            post("2026-09-09T14:40:00Z", "BMSState", "BMSStateStandby")

            body = client.get("/api/telemetry/modes").json()
            assert body["changes"] == 3, body["recent"]
            moves = [(m["field"], m["from"], m["to"]) for m in body["recent"]]
            assert ("BMSState", None, "BMSStateDrive") in moves
            assert ("BMSState", "BMSStateDrive", "BMSStateStandby") in moves
            assert ("CenterDisplay", None, "DisplayStateDriving") in moves
            # The moment is what matters, and it is the record's, not now's.
            leaving = [m for m in body["recent"]
                       if m["to"] == "BMSStateStandby"][0]
            assert leaving["ts"].endswith("22:40:00")   # 14:40Z in local time
            # And when this heard about it, which is a different question once
            # the car has been out of coverage: a change made underground is
            # reported on reconnect and arrives looking like one made just now.
            assert leaving["seen"] and leaving["lag_sec"] is not None

            one = client.get("/api/telemetry/modes?field=BMSState").json()
            assert {m["field"] for m in one["recent"]} == {"BMSState"}
            # A filter narrows what comes back; it must not make the log look
            # empty. Asked mid-charge for a field that had not moved yet, this
            # answered {"changes": 0, "recent": []} and read as data loss.
            assert one["changes"] == 3, "the whole log, not the filtered part"
            assert one["matching"] == 2
            assert one["field"] == "BMSState"

            none = client.get("/api/telemetry/modes?field=HvacPower").json()
            assert none["matching"] == 0 and none["changes"] == 3
    finally:
        state.put(sess, state.TELEMETRY_MODES_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_only_changed_state_is_written_on_a_telemetry_batch():
    """The buffer's size is paid on every batch, not once.

    Each POST appends a few records and rewrites the whole raw blob, so a
    300-record buffer cost ~100 KB per batch — some 200 MB of writes a day to
    carry under 1 MB of new data. The other blobs are fine: no UPDATE is
    emitted when a value has not changed, and trips and mode changes are
    unchanged on almost every batch — and they are now skipped outright
    rather than read and committed to discover that. This pins both halves.
    """
    import json as _json

    from sqlalchemy import event

    from app.database import SessionLocal, engine
    from app import state
    from app.api import routes as routes_mod

    # The budget is bytes, not records. This first guarded a record count,
    # which was the right instinct measured on the wrong axis: dropping the
    # duplicate copy of each record halved what one costs, so the same budget
    # now buys three times the window. Pinning the count would have made a
    # strictly cheaper buffer fail.
    sample = _json.dumps({
        "received_at": "2026-09-10T13:15:48", "vin": "LRW3F7EK3RC309372",
        "created_at": "2026-09-10T05:15:47.708992906Z",
        "fields": {"EnergyRemaining": 44.87999899685383}, "resend": False})
    budget_kb = len(sample) * routes_mod.TELEMETRY_RAW_MAX / 1024.0
    assert budget_kb <= 30, (
        f"the raw buffer is a diagnostic, not an archive: {budget_kb:.0f} KB "
        f"rewritten on every batch")

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in (
        state.TELEMETRY_RAW_KEY, state.TELEMETRY_TRIPS_KEY,
        state.TELEMETRY_MODES_KEY, state.TELEMETRY_LATEST_KEY)}
    vin = "WRITES0000000001"
    updated: list[str] = []

    def _watch(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("UPDATE SETTINGS"):
            updated.append(str(params))

    try:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, "[]")
        state.put(sess, state.TELEMETRY_MODES_KEY, "[]")
        sess.commit()
        with TestClient(app) as client:
            def post(ts, speed):
                r = client.post("/api/telemetry", json={"records": [{
                    "vin": vin, "createdAt": ts,
                    "data": [{"key": "VehicleSpeed", "value": {"doubleValue": speed}}]}]})
                assert r.status_code == 200, r.text

            post("2026-09-09T15:00:00Z", 20.0)          # warm the state up
            event.listen(engine, "before_cursor_execute", _watch)
            try:
                post("2026-09-09T15:00:10Z", 21.0)
            finally:
                event.remove(engine, "before_cursor_execute", _watch)

        written = " ".join(updated)
        # Speed changed, so the raw buffer and the composite must be written.
        assert "telemetry_raw" in written and "telemetry_latest" in written
        # Nothing closed and no mode moved, so these must NOT be rewritten.
        assert "telemetry_trips" not in written, "unchanged trips were rewritten"
        assert "telemetry_modes" not in written, "unchanged mode log was rewritten"
        # And a car that is not charging has no session to remember. This
        # store held a full forty-field snapshot of a parked car and was
        # committed on every batch, all day, to record that nothing had
        # happened.
        assert "telemetry_charge_shadow" not in written, \
            "an idle charge shadow was rewritten"
    finally:
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_sentry_alert_says_what_actually_happened(monkeypatch):
    """"The car movement detected" said the opposite of what Aware means.

    Aware is the car noticing something NEAR it — someone walking past. The car
    has not moved and nothing has been touched. Panic is the alarm going off.
    Tesla streams no camera, zone or sensor, so which side of the car cannot be
    answered; what can be answered is whether anything actually happened to it,
    which is the question at one in the morning.
    """
    from app.api import routes as routes_mod

    settings = get_settings()
    old_sk = settings.sync_key
    settings.sync_key = "cronkey"
    alerts: list[tuple] = []
    monkeypatch.setattr(routes_mod.notifications, "notify",
                        lambda s, title, body, tag=None: alerts.append((title, body, tag)))
    monkeypatch.setattr(routes_mod, "SENTRY_COOLDOWN_SEC", 0.0)

    def batch(vin, *entries):
        return {"records": [{"vin": vin, "createdAt": at, "data": data}
                            for at, data in entries]}

    def sentry(state):
        return [{"key": "SentryMode", "value": {"sentryModeStateValue": state}}]

    try:
        with TestClient(app) as client:
            client.post("/api/telemetry?key=cronkey", json=batch(
                "SENTRYWORDS00001",
                ("2026-09-09T09:00:00Z", [
                    {"key": "Locked", "value": {"booleanValue": True}},
                    {"key": "Soc", "value": {"doubleValue": 70.4}},
                    {"key": "DoorState", "value": {"doorValue": {
                        "DriverFront": False, "TrunkRear": False}}}]),
                ("2026-09-09T09:00:10Z", sentry("SentryModeStateArmed")),
                ("2026-09-09T09:00:20Z", sentry("SentryModeStateAware"))))

        assert len(alerts) == 1, alerts
        title, body, tag = alerts[0]
        assert title == "Sentry" and tag == "sentry"
        assert "moved near the car" in body
        assert "The car has not moved." in body
        assert "all doors shut" in body and "locked" in body
        assert "battery 70%" in body, "a bare percentage could be anything"

        alerts.clear()
        with TestClient(app) as client:
            client.post("/api/telemetry?key=cronkey", json=batch(
                "SENTRYWORDS00002",
                ("2026-09-09T09:10:00Z", [
                    {"key": "Locked", "value": {"booleanValue": False}},
                    {"key": "DoorState", "value": {"doorValue": {
                        "DriverFront": True, "TrunkRear": False}}}]),
                ("2026-09-09T09:10:10Z", sentry("SentryModeStatePanic"))))

        title, body, _ = alerts[0]
        # An alarm reads differently from someone walking past, and the two
        # facts that matter are that it is open and unlocked.
        assert title == "Sentry: alarm" and "ALARM went off" in body
        # Tesla's key names in words: nobody should have to translate
        # TrunkFront into "frunk" while reading an alert at one in the morning.
        assert "OPEN: driver door" in body and "UNLOCKED" in body
    finally:
        settings.sync_key = old_sk


def test_windows_are_read_so_the_intrusion_check_can_see_them():
    """The app has always alerted on a car opened while parked and locked, and
    that check covers windows as well as doors — but this path reported them as
    unknown, so a window was the one way in the stream could not see."""
    from app import sync as sync_mod

    def snap(**fields):
        fields.setdefault("Odometer", 19000.0)
        return sync_mod.snapshot_from_telemetry(fields, 1_788_900_000.0)

    shut = dict(FdWindow="WindowStateClosed", FpWindow="WindowStateClosed",
                RdWindow="WindowStateClosed", RpWindow="WindowStateClosed")
    assert snap(**shut)["windows_open"] is False
    # A window lowered an inch is a way in, and is what one forced from
    # outside looks like.
    assert snap(**dict(shut, RdWindow="WindowStatePartiallyOpen"))["windows_open"] is True
    assert snap(**dict(shut, FdWindow="WindowStateOpened"))["windows_open"] is True

    # Unknown is not a confirmed shut. The intrusion check treats None and
    # False very differently, and a sealed car nobody looked at is not sealed.
    assert snap(**{k: "WindowStateUnknown" for k in shut})["windows_open"] is None
    assert snap()["windows_open"] is None
    # One readable window among unknowns is still an answer.
    assert snap(FdWindow="WindowStateUnknown",
                RdWindow="WindowStateOpened")["windows_open"] is True


def test_the_other_third_set_fields_are_recorded_raw():
    from app import sync as sync_mod

    s = sync_mod.snapshot_from_telemetry(
        {"Odometer": 19000.0, "PairedPhoneKeyAndKeyFobQty": 3,
         "ChargePortDoorOpen": True, "DriverSeatBelt": False}, 1_788_900_000.0)
    assert s["paired_keys"] == 3.0
    assert s["charge_port_door_open"] is True
    # NOT read as "belted": observed false while a belted driver drove at
    # 17 km/h. Carried raw, claimed as nothing, until its transitions say
    # what it reports.
    assert s["driver_belt_raw"] is False
    assert s["driver_belt"] is None

    bare = sync_mod.snapshot_from_telemetry({"Odometer": 19000.0}, 1_788_900_000.0)
    assert bare["paired_keys"] is None
    assert bare["charge_port_door_open"] is None
    assert bare["driver_belt_raw"] is None and bare["driver_belt"] is None


def test_sentry_alert_reports_a_window(monkeypatch):
    """Once the third field set is sent, a window is the thing worth saying.

    It is the classic way in, and until now the stream could not see it.
    Unknown must still say nothing rather than reassure.
    """
    from app.api import routes as routes_mod

    settings = get_settings()
    old_sk = settings.sync_key
    settings.sync_key = "cronkey"
    alerts: list[tuple] = []
    monkeypatch.setattr(routes_mod.notifications, "notify",
                        lambda s, title, body, tag=None: alerts.append((title, body, tag)))
    monkeypatch.setattr(routes_mod, "SENTRY_COOLDOWN_SEC", 0.0)

    def run(vin, windows):
        data = [{"key": k, "value": {"windowStateValue": v}}
                for k, v in windows.items()]
        with TestClient(app) as client:
            client.post("/api/telemetry?key=cronkey", json={"records": [
                {"vin": vin, "createdAt": "2026-09-09T16:00:00Z", "data": data},
                {"vin": vin, "createdAt": "2026-09-09T16:00:10Z", "data": [
                    {"key": "SentryMode",
                     "value": {"sentryModeStateValue": "SentryModeStateAware"}}]}]})
        return alerts[-1][1]

    shut = {"FdWindow": "WindowStateClosed", "FpWindow": "WindowStateClosed",
            "RdWindow": "WindowStateClosed", "RpWindow": "WindowStateClosed"}
    try:
        assert "windows shut" in run("SENTRYWIN0000001", shut)
        assert "A WINDOW IS OPEN" in run(
            "SENTRYWIN0000002", dict(shut, RdWindow="WindowStatePartiallyOpen"))
        # Not configured: say nothing rather than claim the car is sealed.
        body = run("SENTRYWIN0000003", {})
        assert "window" not in body.lower()
    finally:
        settings.sync_key = old_sk


def test_waiting_in_the_seat_does_not_end_the_journey():
    """Park with the driver still aboard is waiting, not arriving.

    Measured: a 35-minute drive with a pause in the middle came back as 14
    minutes and 2.2 km of 4.9, because ten minutes of stillness was taken for
    an arrival. The driver never left the seat — someone was being waited for.
    """
    from app import sync as sync_mod

    def snap(ts, mi, *, mph=0.0, gear="ShiftStateP", seat=True, doors=False):
        return sync_mod.snapshot_from_telemetry(
            {"Odometer": mi, "VehicleSpeed": mph, "Gear": gear,
             "DriverSeatOccupied": seat, "EnergyRemaining": 55.0,
             "DoorState": {"DriverFront": doors}}, ts)

    t, m = 1_788_900_000.0, 19000.0
    sh: dict = {}
    sync_mod.advance_shadow(sh, snap(t, m, mph=20, gear="ShiftStateD"))
    sync_mod.advance_shadow(sh, snap(t + 600, m + 1.5, mph=20, gear="ShiftStateD"))
    # Shifts to P and waits, seated. Twenty minutes of it.
    for i in range(1, 21):
        assert sync_mod.advance_shadow(sh, snap(t + 600 + i * 60, m + 1.5)) is None, \
            f"the journey was cut short after {i} minutes of waiting"
    assert sh.get("open")
    # Drives on, and it is still the same journey.
    sync_mod.advance_shadow(sh, snap(t + 1900, m + 1.5, mph=20, gear="ShiftStateD"))
    sync_mod.advance_shadow(sh, snap(t + 2400, m + 3.0, mph=20, gear="ShiftStateD"))
    # Arrives properly: the driver gets out.
    sync_mod.advance_shadow(sh, snap(t + 2500, m + 3.2, seat=False, doors=True))
    done = sync_mod.advance_shadow(sh, snap(t + 2700, m + 3.2, seat=False, doors=True))
    assert done is not None, "leaving the seat must still end the journey"
    assert done["ended_on"] == "exit"
    # One trip covering both legs, ending when the car actually stopped.
    assert done["end_ts"] == t + 2500
    assert done["distance_km"] == pytest.approx(3.2 * sync_mod.MILES_TO_KM, abs=0.01)


def test_a_long_wait_does_not_delay_a_real_arrival():
    """Waiting is generous, arriving is not made slower by it.

    The short window is measured from when the car STOPPED, so by the time
    someone finally leaves the seat it has long since passed and the trip
    closes at once — with the end time being when it first parked, not when
    the door opened.
    """
    from app import sync as sync_mod

    def snap(ts, mi, *, mph=0.0, gear="ShiftStateP", seat=True):
        return sync_mod.snapshot_from_telemetry(
            {"Odometer": mi, "VehicleSpeed": mph, "Gear": gear,
             "DriverSeatOccupied": seat, "EnergyRemaining": 55.0}, ts)

    t, m = 1_788_900_000.0, 19000.0
    sh: dict = {}
    sync_mod.advance_shadow(sh, snap(t, m, mph=20, gear="ShiftStateD"))
    sync_mod.advance_shadow(sh, snap(t + 600, m + 5, mph=20, gear="ShiftStateD"))
    stopped = t + 700
    sync_mod.advance_shadow(sh, snap(stopped, m + 5.4))          # parks, stays in
    for i in range(1, 40):                                       # 39 minutes
        sync_mod.advance_shadow(sh, snap(stopped + i * 60, m + 5.4))
    assert sh.get("open")
    done = sync_mod.advance_shadow(sh, snap(stopped + 2400, m + 5.4, seat=False))
    assert done is not None, "leaving after a long wait must close it immediately"
    assert done["end_ts"] == stopped, "the trip ended when the car stopped"


def test_silence_is_recorded_and_charged_to_the_trip_it_falls_in():
    """A gear change made out of coverage is never transmitted.

    It does not arrive late — it does not arrive. Measured: a park at 17:39
    and the departure after it are both simply absent, and the stream resumes
    with the car in Drive, so every rule reads it as one unbroken journey. The
    silence cannot recover what was lost; it can say that something was lost,
    which is the difference between an answer that is wrong and one that
    admits it does not know.
    """
    import json as _json
    from datetime import timezone as _tz

    from app.database import SessionLocal
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in (
        state.TELEMETRY_GAPS_KEY, state.TELEMETRY_SEEN_KEY,
        state.TELEMETRY_TRIPS_KEY, state.TELEMETRY_SHADOW_KEY)}
    vin = "BLACKOUT00000001"
    try:
        for key in (state.TELEMETRY_GAPS_KEY, state.TELEMETRY_TRIPS_KEY):
            state.put(sess, key, "[]")
        state.put(sess, state.TELEMETRY_SEEN_KEY, "{}")
        state.put(sess, state.TELEMETRY_SHADOW_KEY, "{}")
        sess.commit()

        # A true epoch, not now_local(): that is naive MYT wall-clock, and
        # converting it as if it were UTC puts every record hours in the
        # future — the same mistake that once closed a trip on every cron tick.
        import time as _time

        base_ts = _time.time() - 7200

        def at(offset):
            return datetime.fromtimestamp(base_ts + offset, _tz.utc).isoformat(
                ).replace("+00:00", "Z")

        with TestClient(app) as client:
            def post(offset, **kv):
                keys = {"mph": "VehicleSpeed", "odo": "Odometer", "gear": "Gear"}
                data = [{"key": keys[k],
                         "value": ({"stringValue": v} if keys[k] == "Gear"
                                   else {"doubleValue": v})}
                        for k, v in kv.items()]
                r = client.post("/api/telemetry", json={
                    "records": [{"vin": vin, "createdAt": at(offset), "data": data}]})
                assert r.status_code == 200, r.text

            post(0, odo=19000.0, mph=0.0, gear="ShiftStateP")
            post(10, mph=30.0, gear="ShiftStateD")
            post(60, odo=19000.5, mph=30.0)
            # Ten minutes underground. Whatever happened here was never sent.
            post(660, odo=19002.0, mph=30.0)
            post(700, odo=19002.2, mph=0.0, gear="ShiftStateP")

            body = client.get("/api/telemetry/gaps").json()
            assert body["gaps"] == 1, body
            assert body["recent"][0]["seconds"] == 600

        # And the journey it fell inside says so.
        state.put(SessionLocal(), state.TELEMETRY_SEEN_KEY, "{}")
        with TestClient(app) as client:
            trips = _json.loads(
                state.get(SessionLocal(), state.TELEMETRY_TRIPS_KEY) or "[]")
            if not trips:                       # not yet settled; force it
                from app.api import routes as routes_mod
                routes_mod._settle_shadows(SessionLocal())
            body = client.get("/api/telemetry/compare").json()
        ours = [t for t in body["trips"]
                if t["telemetry"].get("blackout_sec")]
        assert ours, "a journey driven through ten minutes of silence said nothing about it"
        assert ours[0]["telemetry"]["blackout_sec"] == pytest.approx(600, abs=5)
    finally:
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def _tele_post(client, vin, ts, key, value, wrap="stringValue"):
    from datetime import timezone as _tz

    stamp = datetime.fromtimestamp(ts, _tz.utc).isoformat().replace("+00:00", "Z")
    resp = client.post("/api/telemetry", json={"records": [
        {"vin": vin, "createdAt": stamp,
         "data": [{"key": key, "value": {wrap: value}}]}]})
    assert resp.status_code == 200, resp.text


def test_the_gap_log_reports_how_many_records_arrived_late():
    """Whether the car replays a blackout is a measurement, not an opinion.

    The trip machine counts every record older than one it has already read
    and discards it. Nothing read that count, so the one number that says
    whether a lost arrival is recoverable — the car buffered and sent it — or
    genuinely gone was being written and thrown away every time.
    """
    import time as _time

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in
            (state.TELEMETRY_GAPS_KEY, state.TELEMETRY_SEEN_KEY,
             state.TELEMETRY_SHADOW_KEY, state.TELEMETRY_LATEST_KEY)}
    vin = "REPLAYCAR0000001"
    try:
        for key in prev:
            state.put(sess, key, "")
        sess.commit()
        t = _time.time() - 3600
        with TestClient(app) as client:
            _tele_post(client, vin, t, "VehicleSpeed", 40.0, "doubleValue")
            assert client.get("/api/telemetry/gaps").json()["replayed_total"] == 0

            # The car resurfaces and replays what it buffered: a record older
            # than the one already read.
            _tele_post(client, vin, t - 60, "VehicleSpeed", 35.0, "doubleValue")
            body = client.get("/api/telemetry/gaps").json()
        assert body["replayed_total"] == 1, body
        assert body["replayed"][vin] == 1, body
    finally:
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_silence_is_a_property_of_one_car_not_of_the_app():
    """One vehicle streaming must not hide another's outage.

    A single last-seen timestamp across all cars means the gap log goes quiet
    exactly when it matters — a second car reporting normally keeps the clock
    moving while the first is off the air for ten minutes.
    """
    import time as _time

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in
            (state.TELEMETRY_GAPS_KEY, state.TELEMETRY_SEEN_KEY)}
    a, b = "GAPCARA000000001", "GAPCARB000000002"
    try:
        state.put(sess, state.TELEMETRY_GAPS_KEY, "[]")
        state.put(sess, state.TELEMETRY_SEEN_KEY, "{}")
        sess.commit()
        t = _time.time() - 3600
        with TestClient(app) as client:
            for off in (0, 10, 20, 30):
                _tele_post(client, a, t + off, "VehicleSpeed", 20.0, "doubleValue")
                _tele_post(client, b, t + off + 1, "VehicleSpeed", 20.0, "doubleValue")
            assert client.get("/api/telemetry/gaps").json()["gaps"] == 0

            # A goes quiet for 700 seconds; B never stops.
            for off in range(60, 720, 60):
                _tele_post(client, b, t + off, "VehicleSpeed", 20.0, "doubleValue")
            _tele_post(client, a, t + 730, "VehicleSpeed", 0.0, "doubleValue")

            body = client.get("/api/telemetry/gaps").json()
        assert body["gaps"] == 1, body["recent"]
        entry = body["recent"][0]
        assert entry["vin"] == a, "the gap must name the car it belongs to"
        assert entry["seconds"] == pytest.approx(700, abs=5)
    finally:
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_a_change_reported_late_is_logged_even_though_it_is_not_adopted():
    """lag_sec exists to expose a change reported long after it happened.

    The composite refuses values older than it already holds — that is what
    stops a replay rewinding the odometer — and it ran first, so a stale
    change never reached the mode log and lag_sec could never be anything but
    nothing. The fact that the car reported it, and when, is recorded; the
    value is still not adopted.
    """
    import json as _json
    import time as _time

    from app.database import SessionLocal
    from app import state

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = {k: state.get(sess, k) for k in
            (state.TELEMETRY_MODES_KEY, state.TELEMETRY_LATEST_KEY)}
    vin = "LATECHANGE000001"
    try:
        state.put(sess, state.TELEMETRY_MODES_KEY, "[]")
        sess.commit()
        t = _time.time() - 3600
        with TestClient(app) as client:
            _tele_post(client, vin, t, "Gear", "ShiftStateP")
            _tele_post(client, vin, t + 600, "Gear", "ShiftStateD")
            _tele_post(client, vin, t - 1800, "Gear", "ShiftStateR")   # stale

            entries = [m for m in client.get("/api/telemetry/modes").json()["recent"]
                       if m["vin"] == vin]
        late = [m for m in entries if m["to"] == "ShiftStateR"]
        assert late, "a stale change vanished, so lag_sec could never fire"
        assert late[0]["lag_sec"] > 1700
        assert late[0]["stale"] is True
        # And the composite is unmoved: a replay must not rewind the car.
        car = _json.loads(state.get(SessionLocal(), state.TELEMETRY_LATEST_KEY))[vin]
        assert car["Gear"] == "ShiftStateD"
    finally:
        for key, was in prev.items():
            state.put(sess, key, was or "")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_a_trip_is_only_compared_against_its_own_car():
    """Two vehicles in one account are driven at much the same times.

    A commute and a school run overlap almost every morning, and matching on
    time alone scored one car's telemetry against the other car's polled trip
    — a number that measures nothing while looking exactly like a measurement.
    """
    import json as _json
    import time as _time

    from app.database import SessionLocal
    from app.models import Drive, Vehicle
    from app import state, sync as sync_mod

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    sess = SessionLocal()
    prev = state.get(sess, state.TELEMETRY_TRIPS_KEY)
    try:
        mine = Vehicle(name="Mine", vin="OWNCARVIN0000001")
        theirs = Vehicle(name="Theirs", vin="OTHERCARVIN00001")
        sess.add_all([mine, theirs])
        sess.commit()

        # True epochs converted with _dt, which is the convention the stored
        # timestamps use. Building them from now_local() instead puts every
        # value eight hours out, twice over.
        start_ts, end_ts = _time.time() - 2400, _time.time() - 600
        start, end = sync_mod._dt(start_ts), sync_mod._dt(end_ts)

        # Only the OTHER car has a polled drive across this window.
        sess.add(Drive(vehicle_id=theirs.id, start_time=start, end_time=end,
                       distance_km=11.0, energy_used_kwh=2.0, duration_min=30))
        sess.commit()
        state.put(sess, state.TELEMETRY_TRIPS_KEY, _json.dumps([{
            "vin": mine.vin, "start_ts": start_ts, "end_ts": end_ts,
            "start_time": start.isoformat(timespec="seconds"),
            "end_time": end.isoformat(timespec="seconds"),
            "distance_km": 5.0, "duration_min": 30.0, "energy_kwh": 1.0,
            "wh_per_km": 200.0, "start_odo_km": 100.0, "end_odo_km": 105.0}]))
        sess.commit()

        with TestClient(app) as client:
            row = client.get("/api/telemetry/compare").json()["trips"][0]
            assert row["drive_id"] is None, \
                f"matched another car's drive: {row['drive_id']}"

            sess.add(Drive(vehicle_id=mine.id, start_time=start, end_time=end,
                           distance_km=5.1, energy_used_kwh=1.05, duration_min=30))
            sess.commit()
            row = client.get("/api/telemetry/compare").json()["trips"][0]
        assert row["drive_id"] is not None, "its own car's drive was not matched"
    finally:
        state.put(sess, state.TELEMETRY_TRIPS_KEY, prev or "[]")
        sess.commit()
        sess.close()
        settings.app_passcode = old_pc


def test_purge_pre_telemetry_plans_then_deletes_and_restores():
    """The purge deletes only trips older than the first telemetry-sourced
    one, states what the parked-drain fits lose, and can be undone.

    The backup is the whole point: polled trips came from an API that no
    longer holds the history, so a purge without a restore path is the one
    irreversible act in this app. This asserts the round trip returns every
    row under its ORIGINAL id — trip numbers, cost overrides and the
    car-readings log all reference drive ids, and a restore that renumbered
    them would look successful and quietly break all three.
    """
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            from app.database import SessionLocal
            from app.models import Drive, Vehicle

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-PURGE", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                # Three polled trips, then two the telemetry path wrote.
                for day, src in ((1, ""), (2, ""), (3, ""), (10, "telemetry"),
                                 (11, "telemetry")):
                    s.add(Drive(
                        vehicle_id=v.id,
                        start_time=datetime(2025, 6, day, 8, 0),
                        end_time=datetime(2025, 6, day, 8, 30),
                        distance_km=10, duration_min=30, start_soc=80, end_soc=76,
                        energy_used_kwh=4.0, avg_speed_kmh=20, max_speed_kmh=40,
                        outside_temp_c=28, source=src, end_location="Home",
                        tag="work" if day == 2 else "",
                    ))
                s.commit()
                doomed_ids = sorted(
                    d.id for d in s.scalars(
                        select(Drive).where(Drive.vehicle_id == v.id,
                                            Drive.source == "")).all())

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-PURGE"})

            plan = client.get("/api/data/purge-pre-telemetry").json()
            assert plan["applied"] is False
            assert plan["would_delete"] == 3
            assert plan["would_keep"] == 2
            assert plan["cutover"].startswith("2025-06-10")
            # Planning must not have touched anything.
            with SessionLocal() as s:
                assert s.query(Drive).filter(Drive.vehicle_id == v.id).count() == 5
            # The cost is stated, not asserted in prose: both columns present.
            assert set(plan["standby_fits"]) == {"before", "after"}

            # Applying freezes the parked-drain fits before deleting the trips
            # they were measured from — without it the purge takes the fits
            # with it, which is the whole objection to running one.
            assert "would_freeze" in plan
            done = client.post("/api/data/purge-pre-telemetry?apply=true").json()
            assert done["applied"] is True
            assert done["deleted"] == 3
            assert done["backup_rows"] == 3
            assert done["froze"]["from_gaps"] >= 0
            with SessionLocal() as s:
                from app import state as state_mod
                assert state_mod.get(s, state_mod.FROZEN_RATES_KEY)
            with SessionLocal() as s:
                left = s.scalars(select(Drive).where(Drive.vehicle_id == v.id)).all()
                assert len(left) == 2
                assert all(d.source == "telemetry" for d in left)

            # A SECOND apply against the already-purged database must change
            # nothing. Reported live: it re-froze from the emptied history and
            # wrote a zero-row backup over the one holding every deleted trip,
            # so a run with nothing to do destroyed both things this endpoint
            # exists to protect.
            with SessionLocal() as s:
                from app import state as state_mod
                frozen_before = state_mod.get(s, state_mod.FROZEN_RATES_KEY)
                backup_before = state_mod.get(s, state_mod.PURGED_DRIVES_KEY)
            noop = client.post("/api/data/purge-pre-telemetry?apply=true").json()
            assert noop["deleted"] == 0
            with SessionLocal() as s:
                from app import state as state_mod
                assert state_mod.get(s, state_mod.PURGED_DRIVES_KEY) == backup_before
                assert state_mod.get(s, state_mod.FROZEN_RATES_KEY) == frozen_before

            back = client.post("/api/data/restore-purged-drives?apply=true").json()
            assert back["restored"] == 3
            with SessionLocal() as s:
                rows = s.scalars(
                    select(Drive).where(Drive.vehicle_id == v.id,
                                        Drive.source == "")).all()
                assert sorted(d.id for d in rows) == doomed_ids
                # Every column, not just the id — a backup built off the mapper
                # is only worth having if it carries the columns nobody thought
                # to check.
                by_day = {d.start_time.day: d for d in rows}
                assert by_day[2].tag == "work"
                assert by_day[1].distance_km == 10
                assert by_day[3].end_location == "Home"

            # Restoring twice is a no-op, not a duplicate set.
            again = client.post("/api/data/restore-purged-drives?apply=true").json()
            assert again["restored"] == 0
            assert again["already_present"] == 3
            with SessionLocal() as s:
                assert s.query(Drive).filter(Drive.vehicle_id == v.id).count() == 5
    finally:
        settings.app_passcode = old


def test_purge_pre_telemetry_refuses_without_a_telemetry_trip():
    """With nothing streamed yet there is no cutover, and the obvious
    fallback ("anything older than today") would delete the whole history."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            from app.database import SessionLocal
            from app.models import Drive, Vehicle

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-NOCUT", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                s.add(Drive(
                    vehicle_id=v.id,
                    start_time=datetime(2025, 6, 1, 8, 0),
                    end_time=datetime(2025, 6, 1, 8, 30),
                    distance_km=10, duration_min=30, start_soc=80, end_soc=76,
                    energy_used_kwh=4.0, avg_speed_kmh=20, max_speed_kmh=40,
                    outside_temp_c=28,
                ))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-NOCUT"})
            out = client.post("/api/data/purge-pre-telemetry?apply=true").json()
            assert out["deleted"] == 0
            assert "cutover" in out["error"]
            with SessionLocal() as s:
                assert s.query(Drive).filter(Drive.vehicle_id == v.id).count() == 1
    finally:
        settings.app_passcode = old


def test_refreezing_keeps_rates_the_current_history_can_no_longer_fit():
    """A freeze records what the fits said while they had evidence, so a
    re-freeze against a history that can no longer produce a figure must keep
    the one already stored rather than blank it.

    This is the bug that ate four real rates in production: the freeze
    replaced outright, so running it a second time after the purge wrote an
    empty set over Home, Office and the armed-Sentry rate.
    """
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            from app.database import SessionLocal
            from app.models import Vehicle
            from app import state as state_mod

            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-REFREEZE", name="Test", model="Model 3")
                s.add(v)
                s.commit()
                state_mod.put(s, state_mod.FROZEN_RATES_KEY, json.dumps({
                    "at": "2026-09-11T07:15:11",
                    "places": {"Home": 0.032, "Office": 0.03},
                    "sentry_armed_kw": 0.233,
                    "whole_history_kw": 0.059,
                }))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": "TESTVIN-REFREEZE"})
            # No drives at all, so nothing can be fitted from scratch.
            out = client.post("/api/data/freeze-parked-rates?apply=true").json()
            assert out["places"]["Home"] == 0.032
            assert out["places"]["Office"] == 0.03
            assert out["sentry_armed_kw"] == 0.233
            assert out["whole_history_kw"] == 0.059
            assert out["kept_from"] == "2026-09-11T07:15:11"

            # And thaw is still the deliberate way to clear it.
            client.post("/api/data/thaw-parked-rates")
            with SessionLocal() as s:
                assert not state_mod.get(s, state_mod.FROZEN_RATES_KEY)
    finally:
        settings.app_passcode = old


def test_supplied_frozen_rates_are_bounded_before_they_are_trusted():
    """Rates handed in by hand reprice real parks — vampire_drain substitutes
    a rate for the measurement on any gap too short to measure — so a typo
    must be refused, not stored."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            from app.database import SessionLocal
            from app.models import Vehicle
            from app import state as state_mod

            with SessionLocal() as s:
                s.add(Vehicle(vin="TESTVIN-SUPPLIED", name="Test", model="Model 3"))
                s.commit()
            client.post("/api/active-vehicle", json={"vin": "TESTVIN-SUPPLIED"})

            good = json.dumps({"places": {"Home": 0.032, "Office": 0.03},
                               "sentry_armed_kw": 0.233, "whole_history_kw": 0.059})
            out = client.post(
                "/api/data/freeze-parked-rates?apply=true", params={"rates": good}).json()
            assert out["places"]["Home"] == 0.032
            assert out["sentry_armed_kw"] == 0.233
            assert out["source"] == "supplied"

            # A misplaced decimal point is outside the plausible band.
            bad = json.dumps({"places": {"Home": 32.0}})
            err = client.post(
                "/api/data/freeze-parked-rates?apply=true", params={"rates": bad}).json()
            assert "outside the plausible band" in err["error"]
            with SessionLocal() as s:
                stored = json.loads(state_mod.get(s, state_mod.FROZEN_RATES_KEY))
                assert stored["places"]["Home"] == 0.032   # unchanged

            # Malformed input is refused rather than half-applied.
            assert "error" in client.post(
                "/api/data/freeze-parked-rates?apply=true",
                params={"rates": "not json"}).json()
    finally:
        settings.app_passcode = old


def test_telemetry_writes_battery_readings_on_pollings_own_rules():
    """The stream writes the table polling was the only writer of.

    Battery health, the current-SoC gauge, odometer continuity and
    gap_sentry_state all read BatteryReading, and gap_sentry_state is the one
    that reshapes real energy — it decides whether a parked gap is priced at
    the armed rate or the place rate. Cutting the cron starves all four unless
    the stream writes them.
    """
    import json as _json

    from app import state
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import BatteryReading, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-TELEBATT"

    def batch(ts_iso, soc, sentry="SentryModeStateOff", odo_miles=19337.0):
        return {"records": [{
            "vin": vin, "createdAt": ts_iso,
            "data": [{"key": "Soc", "value": {"doubleValue": soc}},
                     {"key": "RatedRange", "value": {"doubleValue": 250.0}},
                     {"key": "Odometer", "value": {"doubleValue": odo_miles}},
                     {"key": "SentryMode", "value": {"stringValue": sentry}},
                     {"key": "HvacPower", "value": {"stringValue": "HvacPowerStateOff"}},
                     {"key": "Gear", "value": {"stringValue": "ShiftStateP"}},
                     {"key": "VehicleSpeed", "value": {"doubleValue": 0}}],
        }]}

    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.add(Vehicle(vin=vin, name="Test", model="Model 3"))
                s.commit()
                vid = s.scalars(select(Vehicle).where(Vehicle.vin == vin)).first().id
                state.put(s, state.TELEMETRY_LATEST_KEY, _json.dumps({}))
                state.put(s, state.TELEMETRY_SHADOW_KEY, _json.dumps({vin: {}}))
                s.commit()

            def rows():
                with SessionLocal() as s:
                    return s.scalars(
                        select(BatteryReading)
                        .where(BatteryReading.vehicle_id == vid)
                        .order_by(BatteryReading.ts)).all()

            # First reading: nothing stored yet, so it writes.
            out = client.post("/api/telemetry", json=batch("2026-09-12T02:00:00Z", 80.0))
            assert out.json()["battery_readings"] == 1
            assert len(rows()) == 1

            # A batch a moment later with SoC unmoved and no state change is
            # not a reading. A parked car posts one of these every twenty
            # seconds — writing each would be 1,700 rows a day.
            out = client.post("/api/telemetry", json=batch("2026-09-12T02:00:40Z", 80.0))
            assert out.json()["battery_readings"] == 0
            assert len(rows()) == 1

            # Sentry arming is a reading even with SoC unmoved — that is the
            # whole point of the column, and SoC will not have moved a full
            # point by the time the car parks and arms.
            out = client.post("/api/telemetry", json=batch(
                "2026-09-12T02:01:00Z", 80.0, sentry="SentryModeStateArmed"))
            assert out.json()["battery_readings"] == 1
            assert rows()[-1].sentry_mode is True

            # A whole point of SoC is a reading.
            out = client.post("/api/telemetry", json=batch(
                "2026-09-12T03:00:00Z", 79.0, sentry="SentryModeStateArmed"))
            assert out.json()["battery_readings"] == 1

            # A replayed record older than what is stored must not be written:
            # gap_sentry_state and odometer_continuity both read this table as
            # a sequence.
            out = client.post("/api/telemetry", json=batch(
                "2026-09-12T01:00:00Z", 60.0, sentry="SentryModeStateOff"))
            assert out.json()["battery_readings"] == 0
            stored = rows()
            assert stored == sorted(stored, key=lambda r: r.ts)

            # A field the configured set does not carry stays unknown, not
            # False — the column exists to keep those distinct.
            assert stored[-1].cabin_overheat_protection is None
    finally:
        settings.app_passcode = old_pc


def test_telemetry_fields_reports_the_set_without_leaking_values():
    """The configure script reads this to avoid sending the car a smaller
    field set than it already has.

    Measured, 11 September: a plain re-run reported "fields sent: 34" — the
    bare default — to a car that had been streaming BMSState for a fortnight,
    and BMSState is what trip ends are detected from. The level file the
    script consulted was read but never written by anything, so it could only
    ever be absent. This is the check that does not depend on local state.
    """
    import json as _json

    from app import state
    from app.config import get_settings
    from app.database import SessionLocal

    settings = get_settings()
    old_pc, old_key = settings.app_passcode, settings.sync_key
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                state.put(s, state.TELEMETRY_RAW_KEY, _json.dumps([
                    {"vin": "V1", "fields": {"Gear": "ShiftStateP", "Soc": 70.0}}]))
                # BMSState lives only in the composite here, not the recent
                # buffer — which is the normal case for a parked car, since a
                # field that is not changing stops being sent. Reading the
                # buffer alone would call a correctly configured car a
                # downgrade and refuse a good configuration.
                state.put(s, state.TELEMETRY_LATEST_KEY, _json.dumps(
                    {"V1": {"BMSState": "BMSStateStandby", "Odometer": 19337.0,
                            "_ts": "2026-09-11T01:42:33Z"}}))
                s.commit()

            out = client.get("/api/telemetry/fields").json()
            assert out["level"] == 2                     # BMSState present
            assert "BMSState" in out["fields"]
            assert "Gear" in out["fields"]
            # Names only. A sync-key holder must not be able to read the car's
            # location out of this.
            assert all(isinstance(f, str) for f in out["fields"])
            assert "70.0" not in _json.dumps(out)
            # The composite keeps bookkeeping of its own beside the car's
            # fields. Reported live: _ts appeared in the list and in the count.
            assert not [f for f in out["fields"] if f.startswith("_")]
            assert out["count"] == len(out["fields"])

            with SessionLocal() as s:
                state.put(s, state.TELEMETRY_LATEST_KEY, _json.dumps(
                    {"V1": {"BMSState": "BMSStateStandby", "FdWindow": "Closed"}}))
                s.commit()
            assert client.get("/api/telemetry/fields").json()["level"] == 3

            with SessionLocal() as s:
                state.put(s, state.TELEMETRY_LATEST_KEY, _json.dumps({"V1": {"Gear": "P"}}))
                s.commit()
            assert client.get("/api/telemetry/fields").json()["level"] == 1

        # Reachable with the sync key alone: the script on the receiver box
        # has no passcode cookie.
        settings.app_passcode = "shh"
        settings.sync_key = "abc123"
        with TestClient(app) as client:
            assert client.get("/api/telemetry/fields").status_code == 401
            assert client.get("/api/telemetry/fields?key=abc123").status_code == 200
    finally:
        settings.app_passcode, settings.sync_key = old_pc, old_key


def test_promoted_trip_gets_place_names_without_overwriting_polled_ones():
    """A trip telemetry ADDS has no place names, and the names are not
    decoration — Top Routes groups by them, the Work/Personal tag is a
    geofence match on them, and the per-place parked rates are keyed by the
    end name. A trip without them is invisible to all three.

    A row polling already named keeps that name. Re-geocoding on every
    promotion would churn a name someone may have corrected by hand and spend
    a lookup per trip per run to arrive back where it started.
    """
    import json as _json

    from app import state
    from app.api.routes import _geocode_shadow_drive
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Drive, Place, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                v = Vehicle(vin="TESTVIN-GEO", name="Test", model="Model 3")
                s.add(v)
                s.add(Place(name="Home", lat=5.3435, lon=100.3112,
                            radius_km=0.5, created_at=datetime(2026, 1, 1)))
                s.commit()

                shadow = {"start_lat": 5.3435, "start_lon": 100.3112,
                          "end_lat": 5.3526, "end_lon": 100.3003}

                # A trip telemetry added: no names, no coords.
                fresh = Drive(
                    vehicle_id=v.id, start_time=datetime(2026, 9, 12, 8, 0),
                    end_time=datetime(2026, 9, 12, 8, 30), distance_km=7.6,
                    duration_min=30, start_soc=80, end_soc=77,
                    energy_used_kwh=1.5, avg_speed_kmh=15, max_speed_kmh=59,
                    outside_temp_c=30)
                s.add(fresh)
                s.commit()
                _geocode_shadow_drive(s, fresh, shadow)
                s.commit()
                # The geofence resolves without a network lookup, and gives
                # the same string a polled trip would have got — so both
                # sources group together in Top Routes.
                assert fresh.start_location == "Home"
                assert fresh.start_coords.startswith("5.3435")
                assert fresh.end_coords.startswith("5.3526")

                # A row polling already named: left alone.
                named = Drive(
                    vehicle_id=v.id, start_time=datetime(2026, 9, 12, 9, 0),
                    end_time=datetime(2026, 9, 12, 9, 30), distance_km=7.6,
                    duration_min=30, start_soc=77, end_soc=74,
                    energy_used_kwh=1.5, avg_speed_kmh=15, max_speed_kmh=59,
                    outside_temp_c=30,
                    start_location="A name someone typed",
                    start_coords="1.0000, 2.0000")
                s.add(named)
                s.commit()
                _geocode_shadow_drive(s, named, shadow)
                s.commit()
                assert named.start_location == "A name someone typed"
                assert named.start_coords == "1.0000, 2.0000"
                # ...but its empty end still gets filled.
                assert named.end_location
    finally:
        settings.app_passcode = old_pc


def test_promote_charges_adds_missed_sessions_and_never_reprices_polled_ones():
    """Charges are the last dashboard figure with no telemetry path, and they
    carry more than their own cost — usable pack capacity is fitted from them.
    A 2.5-hour AC session fits entirely between four-hourly ticks.

    Adds only. Which counter matches the car's own "Added" is unsettled, so a
    polled charge keeps its energy: re-pricing a consistent history on an
    unsettled answer moves every trip costed from it.
    """
    import json as _json

    from app import state
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Charge, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-PROMOCHG"
    from app import sync as sync_mod

    # Through the app's own clock, both ways. Stored times are naive MYT wall
    # clock (see sync.now_local), so datetime.fromtimestamp here would be
    # eight hours out on a UTC host and nothing would match.
    base = sync_mod.to_epoch(datetime(2026, 9, 12, 12, 0))

    def shadow(start_off, end_off, pack_level, wall):
        return {"vin": vin, "start_ts": base + start_off, "end_ts": base + end_off,
                "start_time": "2026-09-12T12:00:00", "end_time": "2026-09-12T14:00:00",
                "duration_min": (end_off - start_off) / 60.0,
                "kwh_pack_level": pack_level, "kwh_pack_meter": wall * 0.99,
                "kwh_wall": wall, "kwh_lifetime": wall,
                "soc_start": 54.0, "soc_end": 80.0, "peak_kw": 7.5,
                "fast": False, "lat": 5.3435, "lon": 100.3112}

    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                v = Vehicle(vin=vin, name="Test", model="Model 3")
                s.add(v)
                s.commit()
                vid = v.id
                # One session polling already has, and one it never saw.
                s.add(Charge(
                    vehicle_id=vid,
                    start_time=sync_mod._dt(base),
                    end_time=sync_mod._dt(base + 7200),
                    duration_min=120, start_soc=54, end_soc=80,
                    energy_added_kwh=18.32, charge_type="AC",
                    max_power_kw=7.5, location="Home", cost=16.49))
                s.commit()
                state.put(s, state.TELEMETRY_CHARGES_KEY, _json.dumps([
                    shadow(0, 7200, 18.32, 20.6),            # the polled one
                    shadow(90000, 97200, 12.10, 13.6),       # never recorded
                ]))
                s.commit()

            plan = client.get("/api/telemetry/promote-charges?days=90").json()
            assert plan["would_add"] == 1
            assert plan["already_recorded"] == 1
            # All four counters shown, because the choice between them is the
            # open question and a preview showing only the winner hides it.
            missed = next(c for c in plan["charges"] if c["action"] == "add")
            assert set(missed["counters"]) == {"pack_level", "pack_meter",
                                               "wall", "lifetime"}
            assert missed["energy_source"] == "pack_meter"
            with SessionLocal() as s:
                assert s.query(Charge).filter(Charge.vehicle_id == vid).count() == 1

            done = client.post("/api/telemetry/promote-charges?apply=true&days=90").json()
            assert done["added"] == 1
            with SessionLocal() as s:
                rows = s.scalars(select(Charge).where(Charge.vehicle_id == vid)
                                 .order_by(Charge.start_time)).all()
                assert len(rows) == 2
                polled, added = rows
                # The polled session is untouched, energy included.
                assert polled.energy_added_kwh == 18.32
                assert (polled.source or "") == ""
                # The added one carries which counter it used, so a row
                # written under this answer stays readable after it changes.
                assert added.source == "telemetry"
                # pack_meter, per CHARGE_ENERGY_SOURCE: the fixture's
                # wall x 0.99, not its pack level.
                assert added.energy_added_kwh == pytest.approx(13.464)
                # Named on the row, so a session written under one answer
                # stays findable after the answer changes — which it did, on
                # 11 September, from pack_level to pack_meter.
                assert added.energy_source == "pack_meter"
                assert added.charge_type == "AC"

            # Running again adds nothing: identity is the row's own
            # start_time, not a float epoch that fails to round-trip.
            again = client.post("/api/telemetry/promote-charges?apply=true&days=90").json()
            assert again["added"] == 0
            with SessionLocal() as s:
                assert s.query(Charge).filter(Charge.vehicle_id == vid).count() == 2
    finally:
        settings.app_passcode = old_pc


def test_telemetry_raises_the_parked_alerts_and_does_not_double_send():
    """The three parked-car alerts move to the stream, because at a
    four-hourly tick every one of them becomes useless: low battery reported
    hours after the crossing, a Sentry episode judged from two readings hours
    apart, and an intrusion check watching a door someone could open and shut
    twice between ticks.

    Both paths share the same state flags, so whichever source crosses the
    line first sends the message and the other finds the flag already set.
    Two sources cannot make two notifications out of one event.
    """
    import json as _json

    from app import state
    from app.api import routes as routes_mod
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import SecurityEvent, Vehicle

    settings = get_settings()
    saved = (settings.app_passcode, settings.low_soc_notify_pct,
             settings.intrusion_notify)
    settings.app_passcode = ""
    settings.low_soc_notify_pct = 20.0
    settings.intrusion_notify = True
    vin = "TESTVIN-TELEALERT"
    sent = []

    def fake_notify(session, title, body, tag=None, **kw):
        sent.append(tag)

    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.add(Vehicle(vin=vin, name="Test", model="Model 3"))
                s.commit()
                state.put(s, state.TELEMETRY_LATEST_KEY, _json.dumps({}))
                state.put(s, state.TELEMETRY_SHADOW_KEY, _json.dumps({vin: {}}))
                s.commit()

            import unittest.mock as _mock
            with _mock.patch.object(routes_mod.notifications, "notify", fake_notify):
                def batch(ts, soc, door="DoorStateClosed"):
                    return {"records": [{"vin": vin, "createdAt": ts, "data": [
                        {"key": "Soc", "value": {"doubleValue": soc}},
                        {"key": "RatedRange", "value": {"doubleValue": 250.0}},
                        {"key": "Odometer", "value": {"doubleValue": 19337.0}},
                        {"key": "SentryMode", "value": {"stringValue": "SentryModeStateArmed"}},
                        {"key": "Locked", "value": {"booleanValue": True}},
                        {"key": "DoorState", "value": {"doorValue": {"DriverFront": door == "open"}}},
                        {"key": "Gear", "value": {"stringValue": "ShiftStateP"}},
                        {"key": "VehicleSpeed", "value": {"doubleValue": 0}}]}]}

                client.post("/api/telemetry", json=batch("2026-09-13T02:00:00Z", 50.0))
                assert "low-soc" not in sent

                # Crossing the threshold: the stream raises it.
                client.post("/api/telemetry", json=batch("2026-09-13T03:00:00Z", 18.0))
                assert sent.count("low-soc") == 1

                # Still low on the next batch: the flag holds it to one.
                client.post("/api/telemetry", json=batch("2026-09-13T03:01:00Z", 17.0))
                assert sent.count("low-soc") == 1

                # A door opening on a locked, unoccupied car: alert plus a
                # persisted SecurityEvent, exactly as the polled path does.
                client.post("/api/telemetry", json=batch(
                    "2026-09-13T03:02:00Z", 17.0, door="open"))
                assert "intrusion" in sent
                with SessionLocal() as s:
                    v = s.scalars(select(Vehicle).where(Vehicle.vin == vin)).first()
                    assert s.query(SecurityEvent).filter(
                        SecurityEvent.vehicle_id == v.id).count() == 1

            # And the dashboard's status card now comes from the stream.
            with SessionLocal() as s:
                status = _json.loads(
                    state.get(s, state.scoped(state.LAST_STATUS_KEY, vin)))
                assert status["source"] == "telemetry"
                assert status["soc"] == 17.0
    finally:
        (settings.app_passcode, settings.low_soc_notify_pct,
         settings.intrusion_notify) = saved


def test_status_staleness_is_judged_against_the_source_own_cadence():
    """"Stale" has to mean "this source has gone quiet", not "ten minutes
    passed".

    The threshold was a fixed ten minutes, sized for a cron ticking every
    minute. At a thirty-minute cron the card reads stale for twenty minutes in
    every thirty with nothing wrong; at four-hourly it never reads anything
    else. And the two sources now run three orders of magnitude apart — a
    streaming car writes every twenty seconds, a watchdog cron every few
    hours — so one constant cannot serve both.
    """
    import json as _json

    from app import state
    from app.api.routes import CRON_STALE_FACTOR, CRON_STALE_MIN, _save_last_status
    from app.database import SessionLocal

    vin = "TESTVIN-CADENCE"
    with SessionLocal() as s:
        key = state.scoped(state.LAST_STATUS_KEY, vin)
        base = 1_789_000_000.0
        # A cron ticking every 30 minutes, four times.
        for i in range(4):
            _save_last_status(s, vin, status="asleep", ts=base + i * 1800,
                              soc=70.0, source="polled")
        s.commit()
        stored = _json.loads(state.get(s, key))
        observed = stored["cadence"]["polled"]
        assert 1500 <= observed <= 1800, observed   # converging on 1800 s

        # Which makes the threshold ~90 min, not 10.
        allow = max(CRON_STALE_MIN, observed / 60.0 * CRON_STALE_FACTOR)
        assert allow > 60.0

        # A streaming source writing every 20 s must not get a 60-second
        # threshold — the floor is what stops a fast source making the card
        # twitchy.
        for i in range(4):
            _save_last_status(s, vin, status="online", ts=base + 10_000 + i * 20,
                              soc=70.0, source="telemetry")
        s.commit()
        stored = _json.loads(state.get(s, key))
        fast = stored["cadence"]["telemetry"]
        assert fast <= 25, fast
        assert max(CRON_STALE_MIN, fast / 60.0 * CRON_STALE_FACTOR) == CRON_STALE_MIN

        # Each source keeps its own cadence: the stream's 20 s must not
        # redefine what "quiet" means for a half-hourly cron.
        assert stored["cadence"]["polled"] == observed

        # A long gap is the car having been asleep, not the cron having
        # slowed, and must not be allowed to raise the threshold for hours.
        _save_last_status(s, vin, status="asleep", ts=base + 200_000,
                          soc=70.0, source="polled")
        s.commit()
        assert _json.loads(state.get(s, key))["cadence"]["polled"] == observed


def test_polling_stops_writing_dashboard_rows_but_keeps_watching():
    """With polling_writes off, the tick stops being a second author of the
    same history and becomes what it is uniquely good for.

    Telemetry supplies drives, charges and readings now, and two sources
    writing one history is how a polled row that merged two real trips ends up
    beside the two telemetry trips that split them correctly — which a
    half-hourly tick does routinely. What must NOT stop is the status card and
    the alerts: those are the watchdog's own output, and the alerts are the
    failsafe for exactly the case where the bridge is down and the stream
    cannot raise them.
    """
    from types import SimpleNamespace

    import json as _json

    from app import state
    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import BatteryReading, Drive, Vehicle

    def make_settings(writes):
        return SimpleNamespace(
            energy_price_per_kwh=0.90, energy_price_ac_kwh=0.0,
            energy_price_dc_kwh=0.0, energy_price_peak_kwh=0.0,
            energy_price_offpeak_kwh=0.0, tariff_peak_start_hour=8,
            tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
            battery_capacity_kwh=0.0, battery_new_range_km=0.0,
            low_soc_notify_pct=0.0, sentry_drain_notify_pct=0.0,
            intrusion_notify=False, drive_min_km=0.5,
            polling_writes=writes, bridge_quiet_alert_min=0.0)

    def vdata(ts, odo, soc, shift):
        return {
            "vin": "TESTVIN-NOWRITE", "display_name": "Test", "vehicle_config": {},
            "vehicle_state": {"odometer": odo, "is_user_present": False,
                              "locked": True, "sentry_mode": False},
            "drive_state": {"timestamp": ts * 1000, "shift_state": shift,
                            "speed": 60 if shift == "D" else 0,
                            "latitude": 5.34, "longitude": 100.31},
            "charge_state": {"battery_level": soc, "battery_range": 200.0,
                             "charging_state": "Disconnected",
                             "charger_power": 0.0, "charge_energy_added": 0.0},
            "climate_state": {"outside_temp": 30.0},
        }

    t = 1_789_100_000
    try:
        with SessionLocal() as s:
            v = Vehicle(vin="TESTVIN-NOWRITE", name="Test", model="Model 3")
            s.add(v)
            s.commit()
            vid = v.id

            def tick(dt, odo, soc, shift, writes):
                _process_vehicle(s, vdata(t + dt, odo, soc, shift),
                                 {"vin": "TESTVIN-NOWRITE"}, make_settings(writes))
                s.commit()

            def counts():
                return (s.query(Drive).filter(Drive.vehicle_id == vid).count(),
                        s.query(BatteryReading).filter(
                            BatteryReading.vehicle_id == vid).count())

            # A whole trip, with writes off: park -> drive -> park.
            tick(0, 2000.0, 80, "P", False)
            tick(300, 2005.0, 78, "D", False)
            tick(600, 2012.0, 75, "P", False)
            assert counts() == (0, 0), counts()

            # The tick still ran the state machine rather than skipping it:
            # the snapshot it saw is stored, so the next tick starts from the
            # right place and turning writes back on cannot resume mid-trip.
            snap_raw = state.get(
                s, state.scoped(state.SNAPSHOT_KEY, "TESTVIN-NOWRITE"))
            assert snap_raw
            # In km: vehicle_data reports the odometer in MILES, which the
            # snapshot converts. The whole project's worst class of bug is a
            # figure quietly scaled wrong, so the test states the conversion
            # rather than a number someone would have to trust.
            assert _json.loads(snap_raw)["odo_km"] == pytest.approx(
                2012.0 * 1.60934, abs=0.01)

            # And with writes on, the same sequence does produce rows, so the
            # switch is what changed and not the fixture.
            tick(900, 2012.0, 75, "P", True)
            tick(1200, 2018.0, 73, "D", True)
            tick(1500, 2025.0, 70, "P", True)
            drives, readings = counts()
            assert drives >= 1, (drives, readings)
    finally:
        with SessionLocal() as s:
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-NOWRITE").first()
            if v:
                for model in (Drive, BatteryReading):
                    s.query(model).filter(model.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_bridge_quiet_alert_fires_only_when_the_car_is_awake_and_silent():
    """The one fault the stream cannot report about itself.

    A dead receiver and a sleeping car look identical from inside the app —
    both are silence. Reaching the car is what tells them apart, and only
    polling can do that.
    """
    import json as _json
    from types import SimpleNamespace
    from unittest import mock

    from app import state
    from app.api import routes as routes_mod
    from app.api.routes import _check_bridge_quiet
    from app.database import SessionLocal

    vin = "TESTVIN-BRIDGE"
    settings = SimpleNamespace(bridge_quiet_alert_min=20.0)
    vehicle = SimpleNamespace(name="Test")
    sent = []

    with SessionLocal() as s:
        state.put(s, state.TELEMETRY_SEEN_KEY, _json.dumps(
            {"last_record_ts_by_vin": {vin: "2026-09-13T02:00:00Z"}}))
        state.put(s, state.scoped(state.BRIDGE_QUIET_NOTIFIED_KEY, vin), "")
        s.commit()
        last = routes_mod._telemetry_ts("2026-09-13T02:00:00Z")

        with mock.patch.object(routes_mod.notifications, "notify",
                               lambda *a, **k: sent.append(k.get("tag"))):
            # Five minutes quiet: ordinary.
            assert _check_bridge_quiet(
                s, vehicle, vin, {"ts": last + 300}, settings) is False
            assert sent == []

            # Forty minutes quiet on a car polling just reached: not ordinary.
            assert _check_bridge_quiet(
                s, vehicle, vin, {"ts": last + 2400}, settings) is True
            assert sent == ["bridge-quiet"]

            # Still quiet: reported once, not every tick until fixed.
            assert _check_bridge_quiet(
                s, vehicle, vin, {"ts": last + 3000}, settings) is False
            assert sent == ["bridge-quiet"]

            # Records arrive again -> re-armed for the next outage.
            state.put(s, state.TELEMETRY_SEEN_KEY, _json.dumps(
                {"last_record_ts_by_vin": {vin: "2026-09-13T03:00:00Z"}}))
            s.commit()
            back = routes_mod._telemetry_ts("2026-09-13T03:00:00Z")
            assert _check_bridge_quiet(
                s, vehicle, vin, {"ts": back + 60}, settings) is False
            assert _check_bridge_quiet(
                s, vehicle, vin, {"ts": back + 2400}, settings) is True
            assert sent == ["bridge-quiet", "bridge-quiet"]

        # A car that has never streamed is not a broken bridge.
        state.put(s, state.TELEMETRY_SEEN_KEY, _json.dumps({}))
        s.commit()
        assert _check_bridge_quiet(
            s, vehicle, vin, {"ts": back + 9999}, settings) is False


def test_telemetry_config_separates_a_quiet_field_from_a_missing_one():
    """/api/telemetry/fields alone cannot tell these apart.

    Fleet Telemetry transmits a field when it CHANGES, so a field that has not
    arrived may be absent from the car's configuration, or may simply not have
    moved since the configuration landed — a lifetime charge counter on a car
    that has not charged, a DC power reading on a car that has not
    supercharged. One reads as a broken deploy and the other is ordinary.
    """
    import json as _json
    from unittest import mock

    from app import state
    from app.api import routes as routes_mod
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-CONFIG"
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.add(Vehicle(vin=vin, name="Test", model="Model 3"))
                # Two of the three configured fields have actually arrived.
                state.put(s, state.TELEMETRY_LATEST_KEY, _json.dumps(
                    {vin: {"Soc": 70.0, "Gear": "ShiftStateP", "_ts": "x"}}))
                state.put(s, state.TELEMETRY_RAW_KEY, "[]")
                state.put(s, state.TOKEN_KEY, "tok")
                s.commit()

            # The shape Tesla actually returns, taken from production:
            # TeslaClient._get has already stripped the "response" envelope,
            # so synced and config sit at the top.
            fake = {"synced": True, "config": {
                "hostname": "telemetry.example",
                "fields": {"Soc": {}, "Gear": {},
                           "LifetimeEnergyChargedKwh": {}}}}
            with mock.patch(
                    "app.tesla_client.TeslaClient.telemetry_config",
                    lambda self, v: fake):
                out = client.get("/api/telemetry/config").json()

            assert out["synced"] is True
            assert out["count"] == 3
            # The car holds it; it just has not moved. Not a failed deploy.
            assert out["configured_but_quiet"] == ["LifetimeEnergyChargedKwh"]
            # Nothing should arrive that was never asked for.
            assert out["arriving_unconfigured"] == []
    finally:
        settings.app_passcode = old_pc


def test_telemetry_config_does_not_blame_the_car_when_it_cannot_read_the_answer():
    """Reported live: the endpoint read Tesla's response as an empty config
    and duly listed all 38 arriving fields as "arriving unconfigured" — on a
    car that was plainly configured, because it was streaming them.

    A diagnostic that blames the car for a parsing failure is worse than one
    that says nothing: it sends someone to reconfigure a car that works.
    """
    from unittest import mock

    from app import state
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-CFGSHAPE"
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.add(Vehicle(vin=vin, name="Test", model="Model 3"))
                state.put(s, state.TELEMETRY_LATEST_KEY,
                          json.dumps({vin: {"Soc": 70.0, "Gear": "ShiftStateP"}}))
                state.put(s, state.TELEMETRY_RAW_KEY, "[]")
                state.put(s, state.TOKEN_KEY, "tok")
                s.commit()

            # A shape neither this endpoint nor the configure script expects.
            with mock.patch("app.tesla_client.TeslaClient.telemetry_config",
                            lambda self, v: {"something_else": 1}):
                out = client.get("/api/telemetry/config").json()
            assert "error" in out
            assert out["arriving_now"] == 2
            assert "arriving_unconfigured" not in out
            # The raw body is returned, because the shape is the finding.
            assert "something_else" in out["raw"]

            # And a shape that puts the fields one level up is still read,
            # rather than being called an error.
            with mock.patch("app.tesla_client.TeslaClient.telemetry_config",
                            lambda self, v: {"synced": True, "config": {
                                "fields": {"Soc": {}, "Gear": {}, "BMSState": {}}}}):
                out = client.get("/api/telemetry/config").json()
            assert out["count"] == 3
            assert out["configured_but_quiet"] == ["BMSState"]
    finally:
        settings.app_passcode = old_pc


def test_duplicate_trips_keeps_the_copy_that_saw_the_whole_journey():
    """Reported live, 11 September: the stream recorded the 11:48 trip as row
    727 and the half-hourly cron wrote the same journey again as 728, at 0.432
    kWh and 40 Wh/km because polling saw only part of it. Both stood, and a
    duplicate journey double-counts distance and energy in every total.

    Which copy goes is decided by evidence, not by age or id order.
    """
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-DEDUPE"
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                v = Vehicle(vin=vin, name="Test", model="Model 3")
                s.add(v)
                s.commit()
                vid = v.id
                now = __import__("app.sync", fromlist=["x"]).now_local()

                def drive(start_min, end_min, km, kwh, source=""):
                    return Drive(
                        vehicle_id=vid,
                        start_time=now - timedelta(minutes=start_min),
                        end_time=now - timedelta(minutes=end_min),
                        distance_km=km, duration_min=start_min - end_min,
                        start_soc=70, end_soc=68, energy_used_kwh=kwh,
                        avg_speed_kmh=20, max_speed_kmh=60, outside_temp_c=30,
                        source=source)

                # The real pair: one journey, two rows. The polled copy is the
                # LOWER id here so the test cannot pass by preferring newest.
                s.add(drive(100, 80, 10.8, 0.432))                # polled
                s.add(drive(100, 67, 10.794, 1.96, "telemetry"))  # stream
                # And a genuinely separate trip that merely abuts the first —
                # a close a moment late and an open a moment early must not
                # read as a duplicate.
                s.add(drive(66, 40, 5.0, 0.9))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": vin})
            plan = client.get("/api/data/duplicate-trips?days=7").json()
            assert plan["would_remove"] == 1
            assert plan["trips_checked"] == 3
            pair = plan["pairs"][0]
            assert pair["keep"]["source"] == "telemetry"
            assert pair["keep"]["kwh"] == 1.96
            assert pair["drop"]["kwh"] == 0.432
            assert pair["why"] == "telemetry saw the whole journey"
            # Stated in the terms that matter: what it was adding to the totals.
            assert plan["double_counted_km"] == 10.8

            with SessionLocal() as s:
                assert s.query(Drive).filter(Drive.vehicle_id == vid).count() == 3

            done = client.post("/api/data/duplicate-trips?days=7&apply=true").json()
            assert done["removed"] == 1
            with SessionLocal() as s:
                left = s.scalars(select(Drive).where(Drive.vehicle_id == vid)).all()
                assert len(left) == 2
                assert 0.432 not in [d.energy_used_kwh for d in left]
                # The backup went to its own key, not the purge's.
                from app import state as state_mod
                assert state_mod.get(s, state_mod.DEDUPED_DRIVES_KEY)

            # Nothing left to find.
            assert client.get(
                "/api/data/duplicate-trips?days=7").json()["would_remove"] == 0
    finally:
        settings.app_passcode = old_pc


def test_polling_does_not_re_record_a_journey_the_stream_already_has():
    """The cause of the 11 September duplicate, closed at the point the second
    row would be written.

    Promotion runs first in a sync tick and adds a row for a finished shadow
    trip; _process_vehicle runs second and, knowing nothing about it, wrote
    the same journey again. Promotion cannot prevent that from its side — by
    the time polling writes, promotion has already run — so the check belongs
    where the second row is about to be created.
    """
    from types import SimpleNamespace

    from app.api.routes import _process_vehicle
    from app.database import SessionLocal
    from app.models import Drive, Vehicle
    from app.sync import _dt as sync_mod_dt

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_ac_kwh=0.0, energy_price_dc_kwh=0.0,
        energy_price_peak_kwh=0.0, energy_price_offpeak_kwh=0.0,
        tariff_peak_start_hour=8, tariff_peak_end_hour=22,
        tariff_weekend_offpeak=True, battery_capacity_kwh=0.0,
        battery_new_range_km=0.0, low_soc_notify_pct=0.0,
        sentry_drain_notify_pct=0.0, intrusion_notify=False, drive_min_km=0.5,
        polling_writes=True, bridge_quiet_alert_min=0.0)

    def vdata(ts, odo, soc, shift):
        return {
            "vin": "TESTVIN-NODUPE", "display_name": "Test", "vehicle_config": {},
            "vehicle_state": {"odometer": odo, "is_user_present": False,
                              "locked": True, "sentry_mode": False},
            "drive_state": {"timestamp": ts * 1000, "shift_state": shift,
                            "speed": 60 if shift == "D" else 0,
                            "latitude": 5.34, "longitude": 100.31},
            "charge_state": {"battery_level": soc, "battery_range": 200.0,
                             "charging_state": "Disconnected",
                             "charger_power": 0.0, "charge_energy_added": 0.0},
            "climate_state": {"outside_temp": 30.0},
        }

    t = 1_789_200_000
    try:
        with SessionLocal() as s:
            v = Vehicle(vin="TESTVIN-NODUPE", name="Test", model="Model 3")
            s.add(v)
            s.commit()
            vid = v.id

            def tick(dt, odo, soc, shift):
                _process_vehicle(s, vdata(t + dt, odo, soc, shift),
                                 {"vin": "TESTVIN-NODUPE"}, settings)
                s.commit()

            # The stream got there first, as it does on a slow cron.
            streamed = Drive(
                vehicle_id=vid,
                # Through the app's own clock. Stored times are naive MYT (see
                # sync.now_local), so datetime.fromtimestamp here would put the
                # streamed row eight hours from the polled one on a UTC host
                # and the overlap check would find nothing to compare.
                start_time=sync_mod_dt(t + 300),
                end_time=sync_mod_dt(t + 1800),
                distance_km=10.794, duration_min=25.0, start_soc=80, end_soc=77,
                energy_used_kwh=1.96, avg_speed_kmh=26, max_speed_kmh=80,
                outside_temp_c=30, source="telemetry")
            s.add(streamed)
            s.commit()

            # Polling now drives the same journey through its own machine.
            tick(0, 2000.0, 80, "P")
            tick(300, 2000.0, 80, "D")
            tick(1800, 2006.7, 77, "P")

            rows = s.scalars(select(Drive).where(Drive.vehicle_id == vid)).all()
            assert len(rows) == 1, [(r.id, r.source, r.distance_km) for r in rows]
            assert rows[0].source == "telemetry"
            assert rows[0].energy_used_kwh == 1.96

            # A genuinely separate later journey is still recorded — the guard
            # must not swallow real trips.
            tick(9000, 2006.7, 77, "D")
            tick(10800, 2020.0, 74, "P")
            assert s.query(Drive).filter(Drive.vehicle_id == vid).count() == 2
    finally:
        with SessionLocal() as s:
            v = s.query(Vehicle).filter(Vehicle.vin == "TESTVIN-NODUPE").first()
            if v:
                s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_a_gap_the_car_drove_through_is_marked_as_a_missed_journey():
    """A sleeping car and a dead receiver both produce silence, and every gap
    in this log reads alike — except one the car drove through.

    Measured, 8 September: the stream went quiet at 17:39 and returned at
    06:39, indistinguishable from an overnight sleep, except three journeys
    were driven inside it and only polling recorded them. It surfaced three
    days later as an odometer discontinuity, because nothing looked for this.
    """
    import json as _json

    from app import state, sync as sync_mod
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-GAPDRIVE"
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                v = Vehicle(vin=vin, name="Test", model="Model 3")
                s.add(v)
                s.commit()
                base = sync_mod.now_local().replace(microsecond=0)
                # One journey inside the first silence, none inside the second.
                s.add(Drive(
                    vehicle_id=v.id,
                    start_time=base - timedelta(hours=10),
                    end_time=base - timedelta(hours=9, minutes=40),
                    distance_km=9.2, duration_min=20, start_soc=77, end_soc=73,
                    energy_used_kwh=1.74, avg_speed_kmh=28, max_speed_kmh=60,
                    outside_temp_c=31))
                state.put(s, state.TELEMETRY_GAPS_KEY, _json.dumps([
                    {"vin": vin,
                     "from": (base - timedelta(hours=12)).isoformat(),
                     "to": (base - timedelta(hours=2)).isoformat(),
                     "seconds": 36000},
                    {"vin": vin,
                     "from": (base - timedelta(hours=1)).isoformat(),
                     "to": base.isoformat(), "seconds": 3600},
                ]))
                s.commit()

            out = client.get("/api/telemetry/gaps").json()
            assert out["gaps_with_journeys"] == 1
            assert out["km_missed"] == 9.2

            drove_through, slept = out["recent"]
            assert drove_through["missed_journeys"] is True
            assert drove_through["drives_inside"] == 1
            assert drove_through["km_inside"] == 9.2
            # The ordinary case stays ordinary: a parked car is not a fault.
            assert slept["missed_journeys"] is False
            assert slept["drives_inside"] == 0
    finally:
        settings.app_passcode = old_pc
        with SessionLocal() as s:
            v = s.query(Vehicle).filter(Vehicle.vin == vin).first()
            if v:
                s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                s.delete(v)
                s.commit()


def test_the_dashboard_settles_a_trip_that_ended_while_the_car_slept():
    """The one journey the ingest cannot announce for itself.

    A trip closing in coverage is promoted the moment the stream says so. A
    trip that ends with the car going to sleep is different: the shadow stays
    open, no further records arrive, and nothing runs. That was the cron's
    job, which at thirty minutes — let alone four hours — is why a finished
    trip could be missing from the page that exists to show it.
    """
    import json as _json

    from app import state, sync as sync_mod
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Drive, Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-SETTLE"
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.add(Vehicle(vin=vin, name="Test", model="Model 3"))
                s.commit()
                # A shadow trip left open, its last record long past the
                # window that decides the stream is gone.
                long_ago = sync_mod.to_epoch(
                    sync_mod.now_local() - timedelta(hours=3))
                state.put(s, state.TELEMETRY_TRIPS_KEY, "[]")
                state.put(s, state.TELEMETRY_SHADOW_KEY, _json.dumps({vin: {
                    "open": {"ts": long_ago, "odo_km": 31000.0, "soc": 70.0,
                             "energy_kwh": 48.0, "shift": "D", "speed_kmh": 40.0,
                             "lat": 5.34, "lon": 100.31},
                    "last": {"ts": long_ago + 900, "odo_km": 31008.0, "soc": 68.0,
                             "energy_kwh": 46.6, "shift": "D", "speed_kmh": 0.0,
                             "lat": 5.35, "lon": 100.30},
                }}))
                s.commit()
                before = s.query(Drive).count()

            client.post("/api/active-vehicle", json={"vin": vin})
            # Reading the dashboard is what closes it. No cron, no network.
            client.get("/api/summary?days=7")

            with SessionLocal() as s:
                assert s.query(Drive).count() == before + 1
                row = s.scalars(select(Drive).order_by(Drive.id.desc())).first()
                assert row.source == "telemetry"
                assert round(row.distance_km, 1) == 8.0
    finally:
        settings.app_passcode = old_pc
        with SessionLocal() as s:
            v = s.query(Vehicle).filter(Vehicle.vin == vin).first()
            if v:
                s.query(Drive).filter(Drive.vehicle_id == v.id).delete()
                s.delete(v)
            state.put(s, state.TELEMETRY_SHADOW_KEY, "{}")
            state.put(s, state.TELEMETRY_TRIPS_KEY, "[]")
            s.commit()


def test_the_live_readout_comes_from_the_stream_not_the_last_poll():
    """A live readout is only live if it is live.

    The current-drive panel read the polled open trip and the polled snapshot,
    which are as fresh as the last tick — half an hour at the new cron, and at
    the four-hourly watchdog no readout at all. The stream has the journey in
    flight to the second and the shadow machine holds the same pair.
    """
    import json as _json

    from app import state, sync as sync_mod
    from app.config import get_settings
    from app.database import SessionLocal
    from app.models import Vehicle

    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    vin = "TESTVIN-LIVE"
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.add(Vehicle(vin=vin, name="Test", model="Model 3"))
                s.commit()
                now_ts = sync_mod.to_epoch(sync_mod.now_local())
                # The polled pair is stale: 8 km, half an hour old.
                state.put(s, state.scoped(state.OPEN_TRIP_KEY, vin), _json.dumps(
                    {"ts": now_ts - 1800, "odo_km": 31000.0, "soc": 70.0,
                     "range_km": 300.0, "max_speed": 60.0}))
                state.put(s, state.scoped(state.SNAPSHOT_KEY, vin), _json.dumps(
                    {"ts": now_ts - 1740, "odo_km": 31008.0, "soc": 69.0,
                     "range_km": 296.0, "speed_kmh": 50.0, "shift": "D"}))
                # The stream has the same journey, 22 km in, seconds ago.
                state.put(s, state.TELEMETRY_SHADOW_KEY, _json.dumps({vin: {
                    "open": {"ts": now_ts - 1800, "odo_km": 31000.0, "soc": 70.0,
                             "range_km": 300.0, "energy_kwh": 48.0,
                             "max_speed": 60.0, "shift": "D", "speed_kmh": 0.0},
                    "last": {"ts": now_ts - 20, "odo_km": 31022.0, "soc": 66.0,
                             "range_km": 284.0, "energy_kwh": 44.6,
                             "shift": "D", "speed_kmh": 63.0},
                }}))
                s.commit()

            client.post("/api/active-vehicle", json={"vin": vin})
            live = client.get("/api/summary?current_drive=true").json()["live_trip"]

            assert live is not None
            # The stream's 22 km, not the poll's 8.
            assert round(live["distance_km"], 1) == 22.0
    finally:
        settings.app_passcode = old_pc
        with SessionLocal() as s:
            v = s.query(Vehicle).filter(Vehicle.vin == vin).first()
            if v:
                s.delete(v)
            state.put(s, state.TELEMETRY_SHADOW_KEY, "{}")
            s.commit()
