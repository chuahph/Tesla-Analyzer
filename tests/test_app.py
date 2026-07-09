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
            # The key opens only /api/sync, nothing else.
            assert client.get("/api/summary?key=cron-key-42").status_code == 401
    finally:
        settings.app_passcode, settings.sync_key = old_pc, old_sk


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
    finally:
        settings.app_passcode = old


def test_summary_reports_battery_balance():
    """battery_balance reports the window's kWh used as a % of the pack
    capacity implied by the last 100% charge, plus the current SoC as the
    "left in the pack" figure."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            body = client.get("/api/summary?days=365").json()
            bal = body["battery_balance"]
            assert set(bal) == {
                "full_charge_kwh", "charged_kwh", "used_kwh", "used_pct", "current_soc_pct",
            }
            assert bal["charged_kwh"] >= 0
            assert bal["used_kwh"] >= 0
            assert bal["full_charge_kwh"] > 0
            if bal["used_pct"] is not None:
                assert round(bal["used_pct"], 1) == round(bal["used_kwh"] / bal["full_charge_kwh"] * 100.0, 1)
            if bal["current_soc_pct"] is not None:
                assert 0 <= bal["current_soc_pct"] <= 100
    finally:
        settings.app_passcode = old


def test_last_full_charge_kwh_prefers_recent_near_full_and_corrects_ac():
    """Capacity is calibrated from the most recent (near-)100% charge, with
    an AC onboard-charger loss correction — this is what keeps per-trip kWh
    in line with the car's own Current Drive screen."""
    from app.api.routes import _last_full_charge_kwh
    from app.database import SessionLocal
    from app.models import Charge, Vehicle

    with SessionLocal() as s:
        v = Vehicle(vin="TESTVIN-CAP", name="Test", model="Model 3")
        s.add(v)
        s.flush()
        # DC (Supercharger) near-full charge: 20% -> 100%, 60 kWh added.
        # No conversion loss on DC, so capacity is unadjusted: 75.0 kWh.
        s.add(Charge(vehicle_id=v.id, start_time=datetime(2026, 1, 1),
                      end_time=datetime(2026, 1, 1, 1), start_soc=20, end_soc=100,
                      energy_added_kwh=60.0, charge_type="DC"))
        s.commit()
        assert _last_full_charge_kwh(s, v.id, fallback=999.0) == 75.0

        # A later AC near-full charge takes precedence (most recent wins)
        # and gets the ~5% onboard-charger loss correction applied.
        s.add(Charge(vehicle_id=v.id, start_time=datetime(2026, 1, 2),
                      end_time=datetime(2026, 1, 2, 1), start_soc=10, end_soc=100,
                      energy_added_kwh=63.0, charge_type="AC"))
        s.commit()
        assert _last_full_charge_kwh(s, v.id, fallback=999.0) == 66.5


def test_last_full_charge_kwh_falls_back_to_best_gain_when_never_full():
    """A daily charge limit under 95% (e.g. 80%) shouldn't strand capacity
    calibration on the stale default — the largest-gain recent charge is
    used instead."""
    from app.api.routes import _last_full_charge_kwh
    from app.database import SessionLocal
    from app.models import Charge, Vehicle

    with SessionLocal() as s:
        v = Vehicle(vin="TESTVIN-CAP2", name="Test", model="Model 3")
        s.add(v)
        s.flush()
        # Never reaches 95%: a small top-up and a bigger 20->80% charge.
        s.add(Charge(vehicle_id=v.id, start_time=datetime(2026, 1, 1),
                      end_time=datetime(2026, 1, 1, 1), start_soc=30, end_soc=50,
                      energy_added_kwh=15.0, charge_type="AC"))
        s.add(Charge(vehicle_id=v.id, start_time=datetime(2026, 1, 2),
                      end_time=datetime(2026, 1, 2, 1), start_soc=20, end_soc=80,
                      energy_added_kwh=42.0, charge_type="AC"))
        s.commit()
        assert _last_full_charge_kwh(s, v.id, fallback=999.0) == 66.5


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
