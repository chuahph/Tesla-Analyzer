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
        # Per-VIN scoped state (open trips, last snapshot, wake tracking) must
        # not leak into the next test's fresh link.
        state.delete_scoped(
            s, state.SNAPSHOT_KEY, state.OPEN_TRIP_KEY, state.OPEN_CHARGE_KEY,
            state.LAST_VSTATE_KEY, state.WOKE_AT_KEY,
        )
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


class _DrivingClient:
    """An online car actively driving — for the poll_fast=True-while-driving case."""
    VIN = "VINAAAAAAAAAAAAAA"

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
            "drive_state": {"timestamp": 1_760_500_000_000, "shift_state": "D", "speed": 40},
            "charge_state": {"battery_level": 74, "battery_range": 370.0 / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 30},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(10030.0),
                              "is_user_present": True, "locked": False},
            "vehicle_config": {"car_type": "model3"},
        }


class _WokeParkedClient:
    """Car just came online on its own (list state = online) but sits parked,
    not driving — the ambiguous case a bounded escalation window is for."""
    VIN = "VINAAAAAAAAAAAAAA"

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
            "drive_state": {"timestamp": 1_760_500_000_000, "shift_state": "P", "speed": 0},
            "charge_state": {"battery_level": 74, "battery_range": 370.0 / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 30},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(10030.0),
                              "is_user_present": True, "locked": False},
            "vehicle_config": {"car_type": "model3"},
        }


def test_poll_fast_true_while_driving(monkeypatch):
    """The sync cron should be told to poll again soon while a trip is in
    progress, so an arrival/lock is caught within seconds instead of up to a
    full cron tick late."""
    from app import services

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        with SessionLocal() as s:
            services.link_with_token(s, "tok")

        monkeypatch.setattr("app.tesla_client.TeslaClient", _DrivingClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "driving"
            assert body["poll_fast"] is True
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_poll_fast_true_briefly_after_unexpected_wake(monkeypatch):
    """A car that comes online on its own (phone-as-key, precondition — not our
    manual wake_up) may be about to drive off. Even though it's still parked,
    poll_fast should go True for a short bounded window so the cron catches
    the departure almost immediately instead of up to a full tick late."""
    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_VSTATE_KEY, vin), "asleep")

        monkeypatch.setattr("app.tesla_client.TeslaClient", _WokeParkedClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "parked"    # not driving yet...
            assert body["poll_fast"] is True      # ...but escalate briefly — it just woke up

        with SessionLocal() as s:
            assert float(state.get(s, state.scoped(state.WOKE_AT_KEY, vin))) > 0
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_poll_fast_false_once_wake_window_expires(monkeypatch):
    """Once the bounded escalation window lapses with no drive detected, the
    cron must fall back to the normal cadence — an online-but-idle car isn't
    kept awake by our polling indefinitely."""
    import time as _time

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_VSTATE_KEY, vin), "online")  # no fresh transition
            state.put(s, state.scoped(state.WOKE_AT_KEY, vin), str(_time.time() - 5 * 60))

        monkeypatch.setattr("app.tesla_client.TeslaClient", _WokeParkedClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "parked"
            assert body["poll_fast"] is False
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_manual_wake_does_not_trigger_fast_poll_escalation(monkeypatch):
    """The user's own manual Sync (wake=1) on a sleeping car isn't an
    'unexpected' wake worth chasing — it's just the user checking the
    dashboard, not a signal the car is about to drive off."""
    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)  # skip the real wake-poll delay

    class _ManualWakeClient:
        VIN = "VINAAAAAAAAAAAAAA"
        woken = False

        def __init__(self, **_):
            pass

        def list_vehicles(self):
            st = "online" if _ManualWakeClient.woken else "asleep"
            return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": st}]

        def wake_up(self, vid):
            _ManualWakeClient.woken = True
            return True

        def vehicle_data(self, vid):
            return {
                "vin": self.VIN,
                "display_name": "Highland",
                "drive_state": {"timestamp": 1_760_500_000_000, "shift_state": "P", "speed": 0},
                "charge_state": {"battery_level": 74, "battery_range": 370.0 / 1.60934,
                                 "charging_state": "Disconnected"},
                "climate_state": {"outside_temp": 30},
                "vehicle_state": {"odometer": ODO_KM_TO_MI(10030.0),
                                  "is_user_present": True, "locked": True},
                "vehicle_config": {"car_type": "model3"},
            }

    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_VSTATE_KEY, vin), "asleep")

        monkeypatch.setattr("app.tesla_client.TeslaClient", _ManualWakeClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync?wake=1")
            assert resp.status_code == 200
            assert resp.json()["poll_fast"] is False

        with SessionLocal() as s:
            assert state.get(s, state.scoped(state.WOKE_AT_KEY, vin)) == ""
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
