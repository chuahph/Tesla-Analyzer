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
            state.LAST_VSTATE_KEY, state.WOKE_AT_KEY, state.LAST_POLL_KEY,
            state.UNREACHABLE_SINCE_KEY,
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


class _CountingParkedClient:
    """An online, parked car that counts vehicle_data() calls — lets tests
    prove whether the poll throttle actually skipped/allowed a real read,
    not just what status string came back."""
    VIN = "VINAAAAAAAAAAAAAA"
    calls = 0

    def __init__(self, **_):
        pass

    def list_vehicles(self):
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": "online"}]

    def wake_up(self, vid):
        return True

    def vehicle_data(self, vid):
        type(self).calls += 1
        return {
            "vin": self.VIN,
            "display_name": "Highland",
            "drive_state": {"timestamp": 1_760_500_000_000, "shift_state": "P", "speed": 0},
            "charge_state": {"battery_level": 74, "battery_range": 370.0 / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 30},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(10030.0),
                              "is_user_present": False, "locked": True},
            "vehicle_config": {"car_type": "model3"},
        }


def test_online_idle_car_is_not_read_again_within_base_interval(monkeypatch):
    """A car that's online but idle must not be read faster than the base
    interval, even if /api/sync itself is called every minute (an external
    cron) — reading it resets Tesla's own sleep countdown, so calling the
    endpoint often must not translate into polling the car often."""
    import time as _time

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _CountingParkedClient.calls = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_POLL_KEY, vin), str(_time.time() - 20))  # read 20s ago

        monkeypatch.setattr("app.tesla_client.TeslaClient", _CountingParkedClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "parked"       # online, just not re-read this tick
            assert "asleep" not in body["note"].lower()
        assert _CountingParkedClient.calls == 0     # vehicle_data() never actually called
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_online_idle_car_is_read_once_base_interval_elapses(monkeypatch):
    """Once the base interval has passed, the normal cadence still applies —
    the throttle only suppresses *extra* reads, not the ones that were due
    anyway."""
    import time as _time

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _CountingParkedClient.calls = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_POLL_KEY, vin), str(_time.time() - 6 * 60))

        monkeypatch.setattr("app.tesla_client.TeslaClient", _CountingParkedClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
            assert resp.json()["status"] == "parked"
        assert _CountingParkedClient.calls == 1
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_drive_min_km_defaults_to_0_1_km():
    from app.config import Settings

    assert Settings().drive_min_km == 0.1


def test_sync_poll_interval_defaults_to_one_minute():
    """The whole point of this project's cron guidance ('every 1 minute')
    is that a real read happens on close to every tick by default — so the
    default must actually be 1, not the old 5-minute value."""
    from app.config import Settings

    assert Settings().sync_poll_interval_min == 1.0


def test_ac_dc_charge_price_defaults():
    from app.config import Settings

    s = Settings()
    assert s.energy_price_ac_kwh == 0.99
    assert s.energy_price_dc_kwh == 1.29


def test_online_idle_car_read_again_after_just_over_a_minute(monkeypatch):
    """With the default 1-minute interval, a car last read 70s ago (just
    past the new threshold, well short of the old 5-minute one) must be
    read again — proving the default actually changed, not just that some
    interval is enforced."""
    import time as _time

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _CountingParkedClient.calls = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_POLL_KEY, vin), str(_time.time() - 70))

        monkeypatch.setattr("app.tesla_client.TeslaClient", _CountingParkedClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
        assert _CountingParkedClient.calls == 1
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_trip_in_progress_bypasses_the_poll_throttle(monkeypatch):
    """A trip already open must always get a fresh read regardless of the base
    interval — it needs live tracking, not a stale skip."""
    import json as _json
    import time as _time

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _CountingParkedClient.calls = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_POLL_KEY, vin), str(_time.time() - 60))  # would block alone
            state.put(s, state.scoped(state.OPEN_TRIP_KEY, vin),
                      _json.dumps({"ts": _time.time(), "odo_km": 10000.0, "soc": 80}))

        monkeypatch.setattr("app.tesla_client.TeslaClient", _CountingParkedClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
        assert _CountingParkedClient.calls == 1
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_manual_sync_bypasses_the_poll_throttle(monkeypatch):
    """The user's own manual Sync button always gets a fresh read of the
    active car, even inside the base-interval throttle window."""
    import time as _time

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _CountingParkedClient.calls = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.scoped(state.LAST_POLL_KEY, vin), str(_time.time() - 60))

        monkeypatch.setattr("app.tesla_client.TeslaClient", _CountingParkedClient)
        with TestClient(app) as client:
            resp = client.post("/api/sync?wake=1")
            assert resp.status_code == 200
        assert _CountingParkedClient.calls == 1
    finally:
        settings.app_passcode = old
        _reset_to_demo()


class _SleepsAfterDrivingClient:
    """Reports driving for its first two reads, then goes properly 'asleep'
    (not just 'offline') on the third — for testing that an open trip closes
    immediately using the last real reading, rather than waiting for the car
    to wake up again."""
    VIN = "VINAAAAAAAAAAAAAA"
    step = 0

    def __init__(self, **_):
        pass

    def list_vehicles(self):
        st = "asleep" if type(self).step >= 2 else "online"
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": st}]

    def wake_up(self, vid):
        return True

    def vehicle_data(self, vid):
        odo = {0: 10_000.0, 1: 10_012.0}[type(self).step]
        soc = {0: 80, 1: 76}[type(self).step]
        return {
            "vin": self.VIN,
            "display_name": "Highland",
            "drive_state": {"timestamp": 1_760_500_000_000 + type(self).step * 300_000,
                            "shift_state": "D", "speed": 60},
            "charge_state": {"battery_level": soc,
                             "battery_range": (400.0 - type(self).step * 20) / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 28},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(odo),
                              "is_user_present": True, "locked": False},
            "vehicle_config": {"car_type": "model3"},
        }


class _OfflineAfterDrivingClient(_SleepsAfterDrivingClient):
    """Same as above, but the third read is ambiguous 'offline' rather than
    a confirmed 'asleep' — must not auto-close on the *first* such reading
    (could just be a signal gap mid-drive, e.g. a tunnel), only once it's
    been sustained for a while."""

    def list_vehicles(self):
        st = "offline" if type(self).step >= 2 else "online"
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": st}]


def test_open_trip_closes_immediately_when_car_falls_asleep(monkeypatch):
    """The car going to true sleep is a definitive 'the drive is over' signal
    (impossible mid-drive) — the trip should close right then using the last
    real reading, not wait for the car to wake up again and reconstruct a
    possibly hours-stale window."""
    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _SleepsAfterDrivingClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _SleepsAfterDrivingClient)
        with TestClient(app) as client:
            resp0 = client.post("/api/sync")            # opens the trip
            assert resp0.json()["status"] == "driving"

            _SleepsAfterDrivingClient.step = 1
            resp1 = client.post("/api/sync")             # still driving, further along
            assert resp1.json()["status"] == "driving"

            _SleepsAfterDrivingClient.step = 2
            resp2 = client.post("/api/sync")              # now properly asleep
            assert resp2.json()["status"] == "asleep"
            assert resp2.json()["logged"]["drives"] == 1   # auto-closed, not left dangling

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).all()
            assert len(drives) == 1
            assert drives[0].distance_km == 12.0             # 10000 -> 10012
            assert state.get(s, state.scoped(state.OPEN_TRIP_KEY, vin)) == ""
    finally:
        settings.app_passcode = old
        _reset_to_demo()


class _SleepsWhileChargingClient:
    """Reports charging for its first two reads, then goes properly 'asleep'
    mid-session — for testing that an open charge closes immediately using
    the last real reading, symmetric to the trip case."""
    VIN = "VINAAAAAAAAAAAAAA"
    step = 0

    def __init__(self, **_):
        pass

    def list_vehicles(self):
        st = "asleep" if type(self).step >= 2 else "online"
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": st}]

    def wake_up(self, vid):
        return True

    def vehicle_data(self, vid):
        soc = {0: 60, 1: 70}[type(self).step]
        energy_added = {0: 3.0, 1: 8.0}[type(self).step]
        return {
            "vin": self.VIN,
            "display_name": "Highland",
            "drive_state": {"timestamp": 1_760_500_000_000 + type(self).step * 300_000,
                            "shift_state": "P", "speed": 0},
            "charge_state": {"battery_level": soc, "battery_range": 300.0 / 1.60934,
                             "charging_state": "Charging", "charger_power": 11,
                             "charge_energy_added": energy_added},
            "climate_state": {"outside_temp": 25},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(10_000.0),
                              "is_user_present": False, "locked": True},
            "vehicle_config": {"car_type": "model3"},
        }


def test_open_charge_closes_immediately_when_car_falls_asleep(monkeypatch):
    """A charge session interrupted by the car going properly asleep (rare —
    charging usually keeps it awake — but connectivity can still drop at the
    charge site) must close using the last real reading, symmetric to the
    trip case, rather than sit open indefinitely and never reach Neon."""
    from app import services, state
    from app.models import Charge

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _SleepsWhileChargingClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _SleepsWhileChargingClient)
        with TestClient(app) as client:
            resp0 = client.post("/api/sync")             # step 0: charging opens
            assert resp0.json()["status"] == "charging"

            _SleepsWhileChargingClient.step = 1
            resp1 = client.post("/api/sync")              # step 1: still charging
            assert resp1.json()["status"] == "charging"

            _SleepsWhileChargingClient.step = 2
            resp2 = client.post("/api/sync")               # step 2: now asleep
            assert resp2.json()["status"] == "asleep"
            assert resp2.json()["logged"]["charges"] == 1    # auto-closed

        with SessionLocal() as s:
            charges = s.query(Charge).filter(Charge.vehicle_id == vid).all()
            assert len(charges) == 1
            assert abs(charges[0].energy_added_kwh - 8.0) < 1e-6  # Tesla's own meter
            assert state.get(s, state.scoped(state.OPEN_CHARGE_KEY, vin)) == ""
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_offline_does_not_auto_close_open_trip_on_first_reading(monkeypatch):
    """A single 'offline' reading is ambiguous — unlike 'asleep' it can mean a
    momentary signal gap during an active drive — so it must not trigger an
    auto-close immediately (that would risk splitting one real trip into two
    over a brief dead zone like a tunnel)."""
    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _OfflineAfterDrivingClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _OfflineAfterDrivingClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineAfterDrivingClient.step = 1
            client.post("/api/sync")
            _OfflineAfterDrivingClient.step = 2
            resp = client.post("/api/sync")               # first offline reading
            assert resp.json()["logged"]["drives"] == 0     # not auto-closed yet

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).all()
            assert len(drives) == 0
            assert state.get(s, state.scoped(state.OPEN_TRIP_KEY, vin)) != ""  # still open
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_sustained_offline_eventually_closes_open_trip(monkeypatch):
    """Some accounts/cars report a genuinely-sleeping car as 'offline' rather
    than a clean 'asleep' — trusting only 'asleep' would leave those trips
    open indefinitely. Once 'offline' has been sustained past
    UNREACHABLE_CLOSE_MIN (not just a single blip), it must still close."""
    import time as _time

    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _OfflineAfterDrivingClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _OfflineAfterDrivingClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineAfterDrivingClient.step = 1
            client.post("/api/sync")
            _OfflineAfterDrivingClient.step = 2
            client.post("/api/sync")                       # offline episode begins

        # Backdate the episode's start past the sustained-offline threshold,
        # as if several more minutes of continued "offline" ticks had passed.
        with SessionLocal() as s:
            state.put(s, state.scoped(state.UNREACHABLE_SINCE_KEY, vin),
                      str(_time.time() - 4 * 60))

        with TestClient(app) as client:
            resp = client.post("/api/sync")                # still offline, now sustained
            assert resp.json()["logged"]["drives"] == 1      # closed despite never seeing "asleep"

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).all()
            assert len(drives) == 1
            assert drives[0].distance_km == 12.0
            assert state.get(s, state.scoped(state.OPEN_TRIP_KEY, vin)) == ""
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_summary_surfaces_last_known_status_from_neon(monkeypatch):
    """/api/summary must reflect the cron's own last determination of car
    status — including 'asleep' — purely from what's already persisted in
    the database, without itself ever pinging Tesla. This is what lets the
    dashboard show a near-live status on page load: the background cron
    already did the polling and left the answer in Neon."""
    from app import services

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _SleepsAfterDrivingClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        with SessionLocal() as s:
            services.link_with_token(s, "tok")

        monkeypatch.setattr("app.tesla_client.TeslaClient", _SleepsAfterDrivingClient)
        with TestClient(app) as client:
            client.post("/api/sync")                      # step 0: driving
            _SleepsAfterDrivingClient.step = 1
            client.post("/api/sync")                       # step 1: still driving
            _SleepsAfterDrivingClient.step = 2
            sync_resp = client.post("/api/sync")            # step 2: asleep
            assert sync_resp.json()["status"] == "asleep"

            # Prove /api/summary reads this back without touching Tesla at all.
            class _ExplodesIfCalled:
                def __init__(self, **_):
                    pass

                def list_vehicles(self):
                    raise AssertionError("summary must not call Tesla")

            monkeypatch.setattr("app.tesla_client.TeslaClient", _ExplodesIfCalled)
            summary = client.get("/api/summary").json()
            assert summary["last_status"]["status"] == "asleep"
            assert summary["last_status"]["soc"] == 76          # from step 1's last real read
            assert summary["last_status"]["stale"] is False      # just written, not stale
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_summary_flags_stale_last_status_when_cron_stops(monkeypatch):
    """last_status.ts is refreshed by /api/sync every cron tick regardless of
    whether the car itself is reachable — so a large gap since it means the
    cron has stopped firing (or something is failing before it can even
    record a status), not that the car has just been busy. /api/summary must
    flag this so the dashboard can show a clear warning instead of quietly
    presenting a stale reading as current."""
    import json as _json
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
            # Simulate a last_status written 20 minutes ago — no cron tick
            # since, well past the staleness threshold.
            state.put(s, state.scoped(state.LAST_STATUS_KEY, vin), _json.dumps({
                "status": "parked", "ts": _time.time() - 20 * 60,
                "soc": 60, "odo_km": 100.0, "speed_kmh": 0,
            }))

        with TestClient(app) as client:
            summary = client.get("/api/summary").json()
            assert summary["last_status"]["stale"] is True
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


def test_compare_endpoint_covers_only_real_cars(monkeypatch):
    """/api/compare returns one row per real (linked) car, skipping demo/
    import placeholders, each with its own driving/charging/battery figures."""
    from datetime import datetime, timedelta

    from app import services
    from app.models import Charge, Drive

    monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
    settings = get_settings()
    old_pc = settings.app_passcode
    settings.app_passcode = ""
    try:
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            car_a = s.query(Vehicle).filter(Vehicle.vin == "VINAAAAAAAAAAAAAA").first()
            car_b = s.query(Vehicle).filter(Vehicle.vin == "VINBBBBBBBBBBBBBB").first()
            now = datetime.now()
            s.add(Drive(
                vehicle_id=car_a.id, start_time=now - timedelta(hours=1), end_time=now,
                distance_km=20.0, duration_min=20.0, start_soc=80, end_soc=70,
                energy_used_kwh=3.0,
            ))
            s.add(Charge(
                vehicle_id=car_a.id, start_time=now - timedelta(hours=2),
                end_time=now - timedelta(hours=1, minutes=30), start_soc=70, end_soc=90,
                energy_added_kwh=15.0, cost=13.5,
            ))
            # Car B has no history at all this window.
            s.commit()

        with TestClient(app) as client:
            body = client.get("/api/compare?days=7").json()
            vins = {row["vin"] for row in body["vehicles"]}
            assert vins == {"VINAAAAAAAAAAAAAA", "VINBBBBBBBBBBBBBB"}   # no DEMO/IMPORT rows
            row_a = next(r for r in body["vehicles"] if r["vin"] == "VINAAAAAAAAAAAAAA")
            row_b = next(r for r in body["vehicles"] if r["vin"] == "VINBBBBBBBBBBBBBB")
            assert row_a["distance_km"] == 20.0
            assert row_a["drives"] == 1
            assert row_a["energy_charged_kwh"] == 15.0
            assert row_b["distance_km"] == 0.0
            assert row_b["drives"] == 0
    finally:
        settings.app_passcode = old_pc
        _reset_to_demo()
