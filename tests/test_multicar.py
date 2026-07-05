"""Multi-car account support: register all cars, per-VIN state, active picker."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Vehicle


@pytest.fixture(autouse=True)
def _db_ready():
    """Ensure the schema exists for tests that use SessionLocal directly."""
    from app.database import init_db

    init_db()
    yield


class _FakeClient:
    """Stands in for TeslaClient so link_with_token needs no network."""
    CARS = [
        {"vin": "VINAAAAAAAAAAAAAA", "display_name": "Model 3"},
        {"vin": "VINBBBBBBBBBBBBBB", "display_name": "Model Y"},
    ]

    def __init__(self, **_):
        pass

    def list_vehicles(self):
        return list(self.CARS)


def _reset_to_demo():
    """Undo any linked state so later tests see the usual demo dataset."""
    from app import services, state
    from app.collector import seed_demo_if_empty

    with SessionLocal() as s:
        services._wipe(s)
        for key in (state.TOKEN_KEY, state.REFRESH_KEY, state.BASE_URL_KEY,
                    state.ACTIVE_VIN_KEY, state.LINKED_VIN_KEY, state.SOURCE_KEY):
            state.put(s, key, "")
    seed_demo_if_empty()


def test_scoped_state_is_per_vin():
    from app import state

    a = state.scoped(state.OPEN_TRIP_KEY, "VIN_A")
    b = state.scoped(state.OPEN_TRIP_KEY, "VIN_B")
    assert a != b
    # No VIN falls back to the bare key (identical single-car behaviour).
    assert state.scoped(state.OPEN_TRIP_KEY, "") == state.OPEN_TRIP_KEY

    with SessionLocal() as s:
        state.put(s, a, "tripA")
        state.put(s, b, "tripB")
        assert state.get(s, a) == "tripA"
        assert state.get(s, b) == "tripB"     # cars don't clobber each other
        state.put(s, a, "")
        state.put(s, b, "")


def test_link_registers_all_cars_and_sets_active(monkeypatch):
    from app import services, state

    monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
    try:
        with SessionLocal() as s:
            result = services.link_with_token(s, "tok")
            vins = {v.vin for v in s.query(Vehicle).all()}
            assert {"VINAAAAAAAAAAAAAA", "VINBBBBBBBBBBBBBB"} <= vins
            # Both cars are reported; the first becomes the active one.
            assert len(result["vehicles"]) == 2
            assert state.active_vin(s) == "VINAAAAAAAAAAAAAA"
    finally:
        _reset_to_demo()


def test_relink_keeps_the_current_active_car(monkeypatch):
    from app import services, state

    monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
    try:
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.ACTIVE_VIN_KEY, "VINBBBBBBBBBBBBBB")  # user picked #2
            services.link_with_token(s, "tok")                      # a later sync/relink
            assert state.active_vin(s) == "VINBBBBBBBBBBBBBB"        # pick preserved
    finally:
        _reset_to_demo()


def test_active_vehicle_switch_endpoint(monkeypatch):
    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
    try:
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
        with TestClient(app) as client:
            body = client.get("/api/summary").json()
            assert body["active_vin"] == "VINAAAAAAAAAAAAAA"
            assert {c["vin"] for c in body["garage"]} == {
                "VINAAAAAAAAAAAAAA", "VINBBBBBBBBBBBBBB"}

            # Switch to the second car; the dashboard follows it.
            resp = client.post("/api/active-vehicle", json={"vin": "VINBBBBBBBBBBBBBB"})
            assert resp.status_code == 200
            assert resp.json()["active_vin"] == "VINBBBBBBBBBBBBBB"
            assert client.get("/api/summary").json()["active_vin"] == "VINBBBBBBBBBBBBBB"

            # An unknown VIN is rejected.
            assert client.post("/api/active-vehicle", json={"vin": "NOPE"}).status_code == 404
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_unlink_clears_account_but_keeps_history(monkeypatch):
    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
    try:
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.SNAPSHOT_KEY, "VINAAAAAAAAAAAAAA"), "{}")
        with TestClient(app) as client:
            assert client.get("/api/health").json()["mode"] == "live"
            resp = client.post("/api/unlink")
            assert resp.status_code == 200
            assert resp.json() == {"status": "unlinked"}
        with SessionLocal() as s:
            assert state.active_token(s) == ""          # token gone
            assert not state.is_live(s)                 # no longer live
            # Per-VIN scoped state is cleared too.
            assert state.get(s, state.scoped(state.SNAPSHOT_KEY, "VINAAAAAAAAAAAAAA")) == ""
            # Cars remain as history.
            assert s.query(Vehicle).count() >= 2
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_sync_returns_clean_503_on_network_error(monkeypatch):
    import httpx

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""

    class _Unreachable(_FakeClient):
        def list_vehicles(self):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
    try:
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
        # Now make the Tesla API unreachable during sync.
        monkeypatch.setattr("app.tesla_client.TeslaClient", _Unreachable)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            # A network error must be a clean 503 (JSON), never an unhandled 500.
            assert resp.status_code == 503
            assert "reach Tesla" in resp.json()["detail"]
    finally:
        settings.app_passcode = old
        _reset_to_demo()


class _SyncClient:
    """A parked, online car for driving /api/sync — configurable odometer."""
    VIN = "VINAAAAAAAAAAAAAA"
    ODO_KM = 10030.0

    def __init__(self, **_):
        pass

    def list_vehicles(self):
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": "online"}]

    def wake_up(self, vid):
        return True

    def vehicle_data(self, vid):
        return {
            "vin": self.VIN,
            "display_name": "Highland",
            "drive_state": {"timestamp": 1_760_500_000_000, "shift_state": "P", "speed": None},
            "charge_state": {"battery_level": 74, "battery_range": 370.0 / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 30},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(_SyncClient.ODO_KM),
                              "is_user_present": False, "locked": True},
            "vehicle_config": {"car_type": "model3"},
        }


def ODO_KM_TO_MI(km):
    return km / 1.60934


def _mk_snap(ts, odo_km, soc, range_km):
    """A complete snapshot dict (all fields _drive_from reads), like the collector."""
    from app.sync import snapshot_from_vehicle_data

    return snapshot_from_vehicle_data({
        "drive_state": {"timestamp": int(ts * 1000), "shift_state": "P"},
        "charge_state": {"battery_level": soc, "battery_range": range_km / 1.60934,
                         "charging_state": "Disconnected"},
        "climate_state": {"outside_temp": 30},
        "vehicle_state": {"odometer": odo_km / 1.60934, "is_user_present": False, "locked": True},
    })


def test_sync_recovers_drive_missed_at_multicar_upgrade(monkeypatch):
    """A drive taken around the pre-VIN → per-VIN upgrade must not be dropped.

    Reproduces the field bug: the legacy global snapshot held the pre-drive
    odometer, the new scoped snapshot the post-drive odometer, and the drive
    between them was never logged. The migration reconstructs it.
    """
    import json

    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vin = "VINAAAAAAAAAAAAAA"
            state.put(s, state.ACTIVE_VIN_KEY, vin)
            state.put(s, state.LINKED_VIN_KEY, vin)
            # Legacy global snapshot (pre-drive) + scoped snapshot (post-drive),
            # built like real snapshots so they carry every field _drive_from reads.
            legacy = _mk_snap(1_760_490_000.0, 10000.0, 82, 400.0)
            scoped = _mk_snap(1_760_499_000.0, 10030.0, 74, 370.0)
            state.put(s, state.SNAPSHOT_KEY, json.dumps(legacy))            # global
            state.put(s, state.scoped(state.SNAPSHOT_KEY, vin), json.dumps(scoped))
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _SyncClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
            assert resp.json()["logged"]["drives"] == 1     # the missed drive recovered

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).all()
            assert len(drives) == 1
            assert abs(drives[0].distance_km - 30.0) < 0.1   # 10000 -> 10030 km
            # Sensible reconstructed duration (not a multi-hour sleep gap).
            assert 0 < drives[0].duration_min < 120
            # Legacy global keys are consumed so it never double-logs.
            assert state.get(s, state.SNAPSHOT_KEY) == ""
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_single_car_summary_has_no_garage_picker():
    """A one-car (demo) dashboard exposes no garage, so no picker shows."""
    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:  # startup seeds demo
            body = client.get("/api/summary").json()
            assert body["garage"] == []      # demo isn't a linked account
    finally:
        settings.app_passcode = old
