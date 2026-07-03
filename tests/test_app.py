"""App-level tests: passcode gate boundaries and the Tesla partner key path."""
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


def test_summary_since_charge_window():
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo data
            full = client.get("/api/summary?days=365").json()
            since = client.get("/api/summary?days=365&since_charge=1").json()
            assert since["window_label"] == "since last charge"
            # The window starts at the last charge, so it holds a subset of drives
            # and no completed charging sessions from before it.
            full_drives = full["driving"].get("total_drives", 0)
            since_drives = since["driving"].get("total_drives", 0) if since["driving"]["available"] else 0
            assert since_drives <= full_drives
            since_charges = since["charging"].get("total_sessions", 0) if since["charging"]["available"] else 0
            assert since_charges <= 1  # at most a charge that started after the last one ended
    finally:
        settings.app_passcode = old


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
