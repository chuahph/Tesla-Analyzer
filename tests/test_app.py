"""App-level tests: passcode gate boundaries and the Tesla partner key path."""
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

PEM_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"


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
                "end_soc", "cost", "charge_type", "location", "rate_per_kwh", "is_free",
                "used_since_kwh", "source", "battery_kwh_at_end",
            }
            assert lc["used_since_kwh"] >= 0
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
    several charge/discharge cycles, with no single "starting battery" to
    divide by) and is computed against what was actually in the pack when
    the last charge ended (end SoC × capacity), not the full pack — a charge
    that only topped up partway shouldn't understate the drain."""
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
                "vampire_avg_pct_per_day",
            }
            assert bal["charged_kwh"] >= 0
            assert bal["used_kwh"] >= 0
            assert bal["full_charge_kwh"] > 0
            assert bal["used_pct"] is None
            # trip_kwh + vampire_kwh always sums back to used_kwh exactly.
            assert round(bal["trip_kwh"] + bal["vampire_kwh"], 1) == round(bal["used_kwh"], 1)
            if bal["current_soc_pct"] is not None:
                assert 0 <= bal["current_soc_pct"] <= 100

            since_body = client.get("/api/summary?since_charge=true").json()
            since_bal = since_body["battery_balance"]
            lc = since_body["last_charge"]
            if lc and lc["battery_kwh_at_end"] > 0:
                assert since_bal["used_pct"] is not None
                assert round(since_bal["used_pct"], 1) == round(
                    since_bal["used_kwh"] / lc["battery_kwh_at_end"] * 100.0, 1)
    finally:
        settings.app_passcode = old


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
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0,
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
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0,
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
        battery_capacity_kwh=0.0, battery_new_range_km=0.0, low_soc_notify_pct=0.0,
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


def test_manual_charge_logs_a_historical_session_additively():
    """A manually-logged charge is inserted for the active vehicle without
    touching any other data — the safe alternative to /api/import (which
    wipes and replaces everything) for backfilling one missed session."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
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
            from datetime import date
            today = date.today().isoformat()
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
