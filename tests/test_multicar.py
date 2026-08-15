"""Multi-car account support: register all cars, per-VIN state, active picker."""
import json as _json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import ArrivalTailSample, Vehicle


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
                    state.ACTIVE_VIN_KEY, state.LINKED_VIN_KEY, state.SOURCE_KEY,
                    # An armed sleep back-off outlives its account otherwise,
                    # and the next test's first sync is silently skipped.
                    state.SUSPEND_KEY, state.SYNC_LOG_KEY,
                    state.FULL_TICK_KEY):
            state.put(s, key, "")
        # Per-VIN scoped state (open trips, last snapshot, wake tracking) must
        # not leak into the next test's fresh link.
        state.delete_scoped(
            s, state.SNAPSHOT_KEY, state.OPEN_TRIP_KEY, state.OPEN_CHARGE_KEY,
            state.LAST_VSTATE_KEY, state.WOKE_AT_KEY, state.LAST_POLL_KEY,
            state.UNREACHABLE_SINCE_KEY, state.LAST_SLEEP_CLOSE_KEY,
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


def test_sync_refreshes_token_on_403(monkeypatch):
    """Tesla sometimes reports an expired/revoked access token as 403 rather
    than 401 (seen live on /api/1/vehicles) — the refresh retry must catch
    that case too, not just a literal 401."""
    import httpx

    from app import auth, services, state

    settings = get_settings()
    old_passcode = settings.app_passcode
    old_client_id = settings.tesla_client_id
    old_client_secret = settings.tesla_client_secret
    settings.app_passcode = ""
    # oauth_configured() requires both to be set.
    settings.tesla_client_id = "test-client-id"
    settings.tesla_client_secret = "test-client-secret"

    def _forbidden():
        req = httpx.Request("GET", "https://fleet-api.example/api/1/vehicles")
        resp = httpx.Response(403, request=req)
        raise httpx.HTTPStatusError("403 Forbidden", request=req, response=resp)

    class _StaleThenFresh(_FakeClient):
        # "asleep" so the per-vehicle sync loop skips vehicle_data() entirely —
        # this test only cares about the list_vehicles()/refresh handshake.
        CARS = [{"vin": "VINAAAAAAAAAAAAAA", "display_name": "Model 3", "state": "asleep"}]

        def __init__(self, access_token=None, **_):
            self.access_token = access_token

        def list_vehicles(self):
            if self.access_token == "stale":
                _forbidden()
            return list(self.CARS)

    monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
    try:
        with SessionLocal() as s:
            services.link_with_token(s, "stale")
            state.put(s, state.REFRESH_KEY, "refresh-me")

        monkeypatch.setattr("app.tesla_client.TeslaClient", _StaleThenFresh)
        monkeypatch.setattr(
            auth, "refresh_tokens",
            lambda refresh_token: {"access_token": "fresh", "refresh_token": "refresh-me"},
        )
        with TestClient(app) as client:
            resp = client.post("/api/sync")
            assert resp.status_code == 200
        with SessionLocal() as s:
            assert state.get(s, state.TOKEN_KEY) == "fresh"
    finally:
        settings.app_passcode = old_passcode
        settings.tesla_client_id = old_client_id
        settings.tesla_client_secret = old_client_secret
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


def test_sync_poll_interval_defaults_to_two_minutes():
    """Tesla's Fleet API bills per data call, and 1-minute idle polling spent a
    whole month's free credit in 22 days. 2.0 halves the idle cost. Safe to
    raise because it gates only the online-but-idle case — an open trip or
    charge bypasses it (see test_trip_in_progress_bypasses_the_poll_throttle),
    so trip-boundary accuracy doesn't depend on this number."""
    from app.config import Settings

    assert Settings().sync_poll_interval_min == 2.0


def test_ac_dc_charge_price_defaults():
    from app.config import Settings

    s = Settings()
    assert s.energy_price_ac_kwh == 1.10   # typical Malaysian public AC rate
    assert s.energy_price_dc_kwh == 1.40   # typical Malaysian public DC rate


def test_idle_car_polling_actually_respects_the_two_minute_interval(monkeypatch):
    """Both sides of the threshold, because only the pair proves the default is
    enforced: a read 70s ago must be SKIPPED (it wouldn't have been under the
    old 1-minute default, so this is what the billing saving actually rests
    on), and one 130s ago must go through."""
    import time as _time

    from app import services, state

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    vin = "VINAAAAAAAAAAAAAA"
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        with SessionLocal() as s:
            services.link_with_token(s, "tok")

        monkeypatch.setattr("app.tesla_client.TeslaClient", _CountingParkedClient)

        def poll_after(seconds_ago):
            with SessionLocal() as s:
                state.put(s, state.scoped(state.LAST_POLL_KEY, vin),
                          str(_time.time() - seconds_ago))
            _CountingParkedClient.calls = 0
            with TestClient(app) as client:
                assert client.post("/api/sync").status_code == 200
            return _CountingParkedClient.calls

        assert poll_after(70) == 0     # inside the interval — no Tesla call, no charge
        assert poll_after(130) == 1    # past it — read as normal
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


class _AsleepThenParksWithCreepClient(_SleepsAfterDrivingClient):
    """Reports a genuine "asleep" (not "offline") after driving, then comes
    back parked a little further along. Sleep proves the car had stopped, but
    the closing reading can still be a poll interval old, so the last stretch
    of the arrival can be missing from the trip."""

    def list_vehicles(self):
        # Asleep only for the read that closes the trip; back online after, so
        # there is a poll to carry the correction.
        st = "asleep" if type(self).step == 2 else "online"
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": st}]

    def vehicle_data(self, vid):
        if type(self).step < 3:
            return super().vehicle_data(vid)
        return {
            "vin": self.VIN,
            "display_name": "Highland",
            "drive_state": {"timestamp": 1_760_500_000_000 + 3 * 300_000,
                            "shift_state": "P", "speed": 0},
            "charge_state": {"battery_level": 75, "battery_range": 375.0 / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 28},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(10012.4),
                              "is_user_present": False, "locked": True},
            "vehicle_config": {"car_type": "model3"},
        }


class _AsleepThenParksWithCreepAndCoordsClient(_AsleepThenParksWithCreepClient):
    """The creep case, with the car reporting a position throughout: driving at
    one point, finally at rest a little further on."""

    DRIVING_AT = (5.30000, 100.30000)
    PARKED_AT = (5.32000, 100.31000)

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        lat, lon = (self.PARKED_AT if type(self).step >= 3 else self.DRIVING_AT)
        d["drive_state"]["latitude"], d["drive_state"]["longitude"] = lat, lon
        return d


def test_a_folded_arrival_tail_moves_the_trip_s_destination(monkeypatch):
    """Mirror of the departure-side rule. The fold-in grows a sleep-closed
    trip's distance to cover ground it drove after its anchor, so leaving
    end_coords at that anchor makes the row claim two different places for one
    arrival — the odometer says it travelled further, the map pin says it
    didn't. The car reads parked at this poll, so that reading is where it
    actually came to rest."""
    from app.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "_place_and_area",
                        lambda coords, session=None: (f"place<{coords}>", "area"))
    closed_id, dist_before, _energy, drives, logged, _s = _run_asleep_close(
        monkeypatch, _AsleepThenParksWithCreepAndCoordsClient)
    assert logged == 0
    closed = next(d for d in drives if d.id == closed_id)
    assert closed.distance_km == 12.4                     # the tail folded in
    lat, lon = _AsleepThenParksWithCreepAndCoordsClient.PARKED_AT
    assert closed.end_coords == f"{lat:.4f}, {lon:.4f}"   # ...and so did the pin
    assert closed.end_area == "area"


class _AsleepThenDrivesAgainClient(_AsleepThenParksWithCreepClient):
    """Same genuine sleep, but the car wakes and makes a whole separate short
    trip before the next poll catches it parked again — 3 km on, well past a
    parking creep."""

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        if type(self).step >= 3:
            d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10015.0)
        return d


def _run_asleep_close(monkeypatch, client_cls, place_tail=None):
    """Drive, fall genuinely asleep (closing the trip), then poll once more.

    ``place_tail`` stands in for what the arrival place has been MEASURED to
    cost (see routes._place_tail_km). Without it no estimate fires at all,
    which is the new default and the honest one: a place with no measurements
    has nothing to say about its tail.
    """
    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    client_cls.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id
        if place_tail is not None:
            monkeypatch.setattr("app.api.routes._place_tail_km",
                                lambda *a, **k: place_tail)

        monkeypatch.setattr("app.tesla_client.TeslaClient", client_cls)
        with TestClient(app) as client:
            client.post("/api/sync")
            client_cls.step = 1
            client.post("/api/sync")
            client_cls.step = 2
            resp = client.post("/api/sync")          # reports "asleep" -> closes
            assert resp.json()["logged"]["drives"] == 1

        with SessionLocal() as s:
            d = s.query(Drive).filter(Drive.vehicle_id == vid).one()
            closed_id, dist_before = d.id, d.distance_km
            energy_before = d.energy_used_kwh
            # >= not ==: a client whose last reading looks like an arrival
            # gets an estimated tail folded in (see estimate_arrival_tail), so
            # the exact figure is the individual test's business, not this
            # helper's.
            assert dist_before >= 12.0
            # Armed even for a trustworthy asleep close — the anchor can still
            # be a poll interval short of the true stop.
            assert state.get(s, state.scoped(state.LAST_SLEEP_CLOSE_KEY, vin)) != ""

        client_cls.step = 3
        with TestClient(app) as client:
            resp2 = client.post("/api/sync")
        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).order_by(Drive.id).all()
            # Read before the finally below wipes them. Rows keyed by
            # vehicle_id do not survive _reset_to_demo, and must not — that is
            # the leak services._wipe exists to prevent — so anything a caller
            # wants to assert on has to leave the session with the rest.
            samples = s.query(ArrivalTailSample).all()
            return (closed_id, dist_before, energy_before, drives,
                    resp2.json()["logged"]["drives"], samples)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_asleep_close_still_recovers_a_short_arrival_tail(monkeypatch):
    """Regression for trip 314: a trip closed on a genuine "asleep" read 0.4 km
    and a minute short, because sleep proves the car had STOPPED but not that
    the closing reading was taken at the stop — last_snapshot can be a poll
    interval old. A small tail must still fold back in."""
    closed_id, dist_before, _energy, drives, logged, _s = _run_asleep_close(
        monkeypatch, _AsleepThenParksWithCreepClient)
    assert logged == 0                      # topped up, not a phantom second trip
    assert len(drives) == 1
    assert drives[0].id == closed_id
    assert drives[0].distance_km == 12.4    # the 0.4 km arrival tail recovered


def test_asleep_close_does_not_swallow_a_later_separate_trip(monkeypatch):
    """The other half of the same rule. After a genuine sleep, movement past a
    parking creep is a NEW trip — the time-based merge that exists for
    mid-drive "offline" closes must not apply here, and the closed trip must
    not be stamped with a loss it never had."""
    closed_id, dist_before, _energy, drives, _, _s = _run_asleep_close(
        monkeypatch, _AsleepThenDrivesAgainClient)
    closed = next(d for d in drives if d.id == closed_id)
    assert closed.distance_km == dist_before   # untouched by the 3 km
    # Must not be stamped with the later trip's 3 km. Unknown rather than a
    # measured zero: this path cannot see the tail, only decline to invent one.
    assert closed.end_lost_km is None


class _AsleepThenWakesHoursLaterClient(_AsleepThenParksWithCreepClient):
    """Same genuine sleep and the same small arrival creep, but the car stays
    quiet for two hours before the next poll reaches it — the normal shape of a
    sleep close, since the whole point of the path is that the car went silent.
    The SoC drop across those two hours is standby drain, not the arrival."""

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        if type(self).step >= 3:
            # +2 h after the close. The drain is deliberately small enough to
            # look like driving: ~0.2 kWh over the 0.4 km creep is ~490 Wh/km,
            # under MAX_PLAUSIBLE_WH_PER_KM, which is the whole point — this is
            # the trip 319 shape, where efficiency alone waves it through.
            # Integer SoC doesn't move at all across it, so the range delta is
            # the only thing that shows it happened.
            d["drive_state"]["timestamp"] = 1_760_500_000_000 + 300_000 + 7_200_000
            d["charge_state"]["battery_range"] = 378.6 / 1.60934
        return d


def test_a_stale_arrival_keeps_its_distance_but_not_the_parked_energy(monkeypatch):
    """Regression, and the mirror of trip 319. The energy fold was gated on
    implied efficiency alone — the exact test that trip proved cannot separate
    a stale anchor from a slow crawl, because 0.4 km of parking-lot creep at
    400 Wh/km looks entirely ordinary. Here the same 0.4 km arrives with two
    hours of standby drain attached, which passes that gate comfortably.

    The odometer is measured at any staleness, so the distance still folds in;
    the SoC drop across a two-hour park is not this drive's energy and must
    not.

    But "not the park's energy" is not "no energy". The 0.4 km is ground the
    car really covered, and giving the trip those metres for free dilutes
    Wh/km by exactly the folded share — the defect energy_for_blind_distance
    exists to prevent. The right answer is the third one: the blind stretch
    priced at the trip's OWN measured efficiency."""
    closed_id, dist_before, energy_before, drives, logged, _s = _run_asleep_close(
        monkeypatch, _AsleepThenWakesHoursLaterClient)
    assert logged == 0
    assert len(drives) == 1
    closed = drives[0]
    assert closed.id == closed_id
    assert closed.distance_km == 12.4          # distance: measured, folds in
    # 12.0 km measured at 3.0 kWh is 250 Wh/km; 0.4 km more of the same drive
    # is +0.1 kWh. Held to the ratio rather than a literal so the assertion
    # states the rule, not one arithmetic outcome.
    assert closed.energy_used_kwh == pytest.approx(
        energy_before * closed.distance_km / dist_before, abs=0.011)
    # The point of the gate, still enforced: the measured SoC/range drop over
    # those two parked hours is ~0.2 kWh — twice the drive's own rate for the
    # same metres — and none of that standby drain may reach the trip.
    assert closed.energy_used_kwh < energy_before + 0.15


class _SlowlyArrivesThenDrivesAgainClient(_AsleepThenDrivesAgainClient):
    """Last seen crawling — an arrival, so the tail gets estimated — then the
    car is next heard from 3 km on, too far for the fold-in to reclaim. That is
    the case where the estimate has to stand AND be handed over."""

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        if type(self).step < 3:
            d["drive_state"]["speed"] = 20      # final approach, not mid-drive
        return d


def test_an_estimated_tail_is_credited_once_not_to_both_trips(monkeypatch):
    """The blind stretch between the last reading and the next is one fixed
    quantity. Crediting the arriving trip with an estimate of it and then
    letting the departing trip's recovery claim the whole thing counts the same
    ground twice — which is precisely how the reverted place-split corrupted
    two trips. Whatever the estimate takes, the next trip must start past."""
    closed_id, dist_before, _e, drives, _logged, _s = _run_asleep_close(
        monkeypatch, _SlowlyArrivesThenDrivesAgainClient, place_tail=0.8)
    rows = sorted(drives, key=lambda d: d.id)
    assert len(rows) >= 2, "needs both trips to check the hand-over"
    assert rows[0].end_est_km, "the estimate must actually have fired here"
    # However the tail was handled, consecutive trips must not overlap: each
    # starts at or after the previous one's end. An overlap is double-counting.
    for a, b in zip(rows, rows[1:]):
        if a.end_odo_km is not None and b.start_odo_km is not None:
            assert b.start_odo_km >= a.end_odo_km - 0.002, (
                f"trip {b.id} starts {a.end_odo_km - b.start_odo_km:.3f} km "
                f"before trip {a.id} ended")


class _SlowlyArrivesThenBarelyMovesClient(_AsleepThenParksWithCreepClient):
    """The place has measured a 0.8 km tail — but the car had very nearly arrived and the
    odometer, once a poll can read it again, has moved only 0.1 km. The
    estimate overshot the ground."""

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        if type(self).step < 3:
            d["drive_state"]["speed"] = 20
        else:
            d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10012.1)
        return d


def test_an_over_estimated_tail_is_trimmed_to_the_ground_actually_covered(monkeypatch):
    """An estimate is a claim awaiting a measurement, and the measurement can
    come back smaller. The arithmetic that netted the estimate off the raw
    movement went negative when it did — failing the `0 < moved` guard, so the
    over-estimate was never corrected AND the odometer was never handed over,
    leaving the next trip free to re-count the same ground. The estimate must
    give way to the measurement in both directions."""
    closed_id, dist_before, _e, drives, logged, _s = _run_asleep_close(
        monkeypatch, _SlowlyArrivesThenBarelyMovesClient, place_tail=0.8)
    assert dist_before == pytest.approx(12.8), "the estimate must have fired"
    assert logged == 0                       # topped up, not a phantom trip
    assert len(drives) == 1
    closed = drives[0]
    assert closed.id == closed_id
    assert closed.distance_km == pytest.approx(12.1)   # measured, not estimated
    assert closed.end_est_km is None         # nothing left standing on a guess


class _SlowlyArrivesThenNeverMovesClient(_AsleepThenParksWithCreepClient):
    """The shape of nearly every arrival: the last reading before signal died
    WAS the arrival, and the odometer never moves again. Any estimated tail
    here is distance the car did not drive."""

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        if type(self).step < 3:
            d["drive_state"]["speed"] = 20
        else:
            d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10012.0)   # unmoved
        return d


def test_an_estimate_the_car_never_drove_is_taken_back_when_it_never_moves(monkeypatch):
    """When the last reading before signal died really was the arrival, the
    car's odometer never moves again — and an estimated tail on top of it is
    distance that did not happen.

    The correction could never reach that case: it required strictly positive
    movement since the close, so zero movement fell through the guard, and the
    block clears its marker on the way out. One chance, declined. Zero is a
    measurement here, and the strongest one available: it does not merely fail
    to confirm the tail, it disproves it."""
    closed_id, dist_before, energy_before, drives, logged, _s = _run_asleep_close(
        monkeypatch, _SlowlyArrivesThenNeverMovesClient, place_tail=0.8)
    assert dist_before == pytest.approx(12.8), "the estimate must have fired"
    assert logged == 0
    assert len(drives) == 1
    closed = drives[0]
    assert closed.id == closed_id
    assert closed.distance_km == pytest.approx(12.0)   # back to the measurement
    assert closed.end_est_km is None
    assert closed.end_odo_km == pytest.approx(10012.0, abs=0.002)
    # The clock came back with the kilometres — it was the same assumption.
    # 5.0 min is the measured trip; the estimate had added its ~60 s window.
    assert closed.duration_min == pytest.approx(5.0)
    # And the energy, which the estimate had priced at the trip's own rate.
    assert closed.energy_used_kwh == pytest.approx(
        energy_before * 12.0 / dist_before, abs=0.011)


class _SlowlyArrivesThenParksFurtherOnClient(_AsleepThenParksWithCreepClient):
    """The ordinary correction: the place's measured tail is 0.8 km and the car
    turns out to have covered 0.9 — more than the guess, still a creep."""

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        if type(self).step < 3:
            d["drive_state"]["speed"] = 20
        else:
            d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10012.9)
        return d


def test_a_measured_tail_replaces_the_estimate_whole(monkeypatch):
    """The fold-in revokes the estimate and then has to put the *whole*
    measurement in its place. It was subtracting the estimate and adding only
    what was left over after it — 0.8 km out, 0.1 km back, and the 0.8 the car
    genuinely drove credited to nobody. The trip must end up covering every
    metre between its anchor and the reading that could finally see it."""
    closed_id, dist_before, _e, drives, logged, _s = _run_asleep_close(
        monkeypatch, _SlowlyArrivesThenParksFurtherOnClient, place_tail=0.8)
    assert dist_before == pytest.approx(12.8), "the estimate must have fired"
    assert logged == 0
    assert len(drives) == 1
    closed = drives[0]
    assert closed.id == closed_id
    assert closed.distance_km == pytest.approx(12.9)
    assert closed.end_est_km is None
    # The odometer is the independent check: the trip's end has to agree with
    # what the car actually read, not with the estimate it superseded.
    assert closed.end_odo_km == pytest.approx(10012.9, abs=0.002)


class _OfflineThenParksWithCreepClient(_OfflineAfterDrivingClient):
    """Same offline episode as above, but once back online the car reports a
    little further odometer movement while already parked — the dead zone
    swallowed the last few metres of the arrival, not just the reconnect
    delay itself. Tests that the sustained-offline close gets topped up
    rather than spawning a disconnected phantom trip for the remainder."""

    def list_vehicles(self):
        st = "offline" if type(self).step == 2 else "online"
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": st}]

    def vehicle_data(self, vid):
        if type(self).step < 3:
            return super().vehicle_data(vid)
        return {
            "vin": self.VIN,
            "display_name": "Highland",
            "drive_state": {"timestamp": 1_760_500_000_000 + 3 * 300_000,
                            "shift_state": "P", "speed": 0},
            "charge_state": {"battery_level": 75,
                             "battery_range": (400.0 - 3 * 20) / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 28},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(10012.3),
                              "is_user_present": True, "locked": True},
            "vehicle_config": {"car_type": "model3"},
        }


def test_sustained_offline_close_is_topped_up_by_a_small_further_creep(monkeypatch):
    """A dead zone right at arrival can close the sustained-offline trip a
    little short of where the car actually came to rest, exactly as it can
    with a live-tracked trip (see the symmetric fold-ins in process_snapshot).
    The next successful poll is the only chance to tell: if it's now parked
    with a small further odometer movement, that movement belongs to the trip
    that already closed, not a new one."""
    import time as _time

    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _OfflineThenParksWithCreepClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _OfflineThenParksWithCreepClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineThenParksWithCreepClient.step = 1
            client.post("/api/sync")
            _OfflineThenParksWithCreepClient.step = 2
            client.post("/api/sync")                       # offline episode begins

        with SessionLocal() as s:
            state.put(s, state.scoped(state.UNREACHABLE_SINCE_KEY, vin),
                      str(_time.time() - 4 * 60))

        with TestClient(app) as client:
            resp = client.post("/api/sync")                # sustained offline -> closes at 12.0 km
            assert resp.json()["logged"]["drives"] == 1

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).all()
            assert len(drives) == 1
            closed_id = drives[0].id
            assert drives[0].distance_km == 12.0
            duration_before = drives[0].duration_min      # 5.0: step0 -> step1, 300s

        _OfflineThenParksWithCreepClient.step = 3
        with TestClient(app) as client:
            resp2 = client.post("/api/sync")                # back online, parked, 0.3 km further
            assert resp2.json()["logged"]["drives"] == 0     # topped up, not a new phantom trip

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).all()
            assert len(drives) == 1                          # still just the one trip
            assert drives[0].id == closed_id
            assert drives[0].distance_km == 12.3              # extended by the creep
            # end_time was anchored to the marker's own stale timestamp, same
            # as distance was — topping up distance without also moving the
            # clock would leave duration understated by however long the dead
            # zone lasted. 0.3 km at the CITY_SPEED_KMH floor (30 km/h) is 36s
            # of estimated travel, so duration grows by 0.6 min, not to the
            # full 15 min gap until reconnect (that would double-count the
            # nap on top of the dead zone).
            assert drives[0].duration_min == round(duration_before + 0.6, 1)
            assert drives[0].avg_speed_kmh == round(12.3 / (drives[0].duration_min / 60.0), 1)
            assert state.get(s, state.scoped(state.LAST_SLEEP_CLOSE_KEY, vin)) == ""  # marker consumed
    finally:
        settings.app_passcode = old
        _reset_to_demo()


class _OfflineThenParksFarButSoonClient(_OfflineThenParksWithCreepClient):
    """Same offline episode, but the car reconnects already 4 km further along
    (not just a few metres of creep) — still within a plausible single-drive
    span of the close, though. Regression case for a real production trip: a
    drive through a hillside stretch with patchy coverage exceeded
    UNREACHABLE_CLOSE_MIN (3 min) mid-drive, closing the trip early; the next
    poll found the car already parked at the true destination, several
    kilometres and several minutes further on, with no actual stop in
    between. Distance alone must not be the only guard here."""

    def vehicle_data(self, vid):
        if type(self).step < 3:
            return super().vehicle_data(vid)
        d = super().vehicle_data(vid)
        d["drive_state"]["timestamp"] = 1_760_500_900_000  # 10 min after the close
        d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10016.0)  # 4 km further
        return d


class _OfflineThenParksFarAndLateClient(_OfflineThenParksWithCreepClient):
    """Same 1.5 km further movement as the original too-large case, but the
    reconnect takes well over an hour — long enough that a genuinely separate,
    later drive is the more likely explanation, not a continuing one."""

    def vehicle_data(self, vid):
        if type(self).step < 3:
            return super().vehicle_data(vid)
        d = super().vehicle_data(vid)
        d["drive_state"]["timestamp"] = 1_760_504_200_000  # 65 min after the close
        d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10013.5)
        return d


def test_sustained_offline_close_merges_a_large_gap_if_reconnect_is_soon(monkeypatch):
    """A dead zone can easily outlast UNREACHABLE_CLOSE_MIN (3 min) while the
    car keeps driving — reconnecting to find it several km further along and
    already parked is still the same drive continuing through the gap, not a
    second one. Must merge regardless of distance, as long as the reconnect is
    soon enough after the close to still plausibly be one trip."""
    import time as _time

    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _OfflineThenParksFarButSoonClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _OfflineThenParksFarButSoonClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineThenParksFarButSoonClient.step = 1
            client.post("/api/sync")
            _OfflineThenParksFarButSoonClient.step = 2
            client.post("/api/sync")                       # offline episode begins

        with SessionLocal() as s:
            state.put(s, state.scoped(state.UNREACHABLE_SINCE_KEY, vin),
                      str(_time.time() - 4 * 60))

        with TestClient(app) as client:
            client.post("/api/sync")                       # sustained offline -> closes at 12.0 km

        with SessionLocal() as s:
            closed_id = s.query(Drive).filter(Drive.vehicle_id == vid).first().id

        _OfflineThenParksFarButSoonClient.step = 3
        with TestClient(app) as client:
            resp = client.post("/api/sync")                # back online, parked, 4 km further, 10 min later
            assert resp.json()["logged"]["drives"] == 0      # merged, not a second trip

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).all()
            assert len(drives) == 1
            assert drives[0].id == closed_id
            assert drives[0].distance_km == 16.0              # 12.0 + the further 4.0
            assert state.get(s, state.scoped(state.LAST_SLEEP_CLOSE_KEY, vin)) == ""
    finally:
        settings.app_passcode = old
        _reset_to_demo()


class _OfflineThenParksWithConsistentEnergyClient(_OfflineThenParksWithCreepClient):
    """Reconnects 3 km further along at a rate of rated-range loss that
    matches the trip's own: the drive burned 20 km of range over 12 km, and
    this stretch burns 5 km over 3 km — the same 250 Wh/km. Whatever the
    fold-in does to distance it must do to energy, or the trip's Wh/km moves
    when nothing about how it was driven did."""

    def vehicle_data(self, vid):
        if type(self).step < 3:
            return super().vehicle_data(vid)
        d = super().vehicle_data(vid)
        d["drive_state"]["timestamp"] = 1_760_500_900_000      # 10 min after the close
        d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10015.0)  # 3 km further
        d["charge_state"]["battery_level"] = 75                # 76 -> 75
        d["charge_state"]["battery_range"] = 375.0 / 1.60934   # 380 -> 375
        return d


def test_sustained_offline_top_up_folds_in_energy_with_the_distance(monkeypatch):
    """Regression: the top-up grew distance_km but left energy_used_kwh at its
    close-time value, so wh_per_km (energy / distance) and soc_used_pct (also
    derived from the energy) both silently fell by whatever fraction of the
    trip the dead zone had swallowed — measured at -25% on a 4 km fold-in.

    The reconnect poll carries fresh soc/range and the close reading is still
    sitting in last_snapshot, so the stretch is measurable, not a guess. Here
    it was driven at exactly the trip's own efficiency, so folding it in
    correctly must leave wh_per_km untouched while distance and energy both
    grow."""
    import time as _time

    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _OfflineThenParksWithConsistentEnergyClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient",
                            _OfflineThenParksWithConsistentEnergyClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineThenParksWithConsistentEnergyClient.step = 1
            client.post("/api/sync")
            _OfflineThenParksWithConsistentEnergyClient.step = 2
            client.post("/api/sync")                       # offline episode begins

        with SessionLocal() as s:
            state.put(s, state.scoped(state.UNREACHABLE_SINCE_KEY, vin),
                      str(_time.time() - 4 * 60))

        with TestClient(app) as client:
            client.post("/api/sync")                       # closes at 12.0 km

        with SessionLocal() as s:
            d = s.query(Drive).filter(Drive.vehicle_id == vid).one()
            closed_id, energy_before, wh_before = d.id, d.energy_used_kwh, d.wh_per_km
            assert d.distance_km == 12.0
            assert d.end_soc == 76

        _OfflineThenParksWithConsistentEnergyClient.step = 3
        with TestClient(app) as client:
            resp = client.post("/api/sync")                # parked, 3 km on, 10 min later
            assert resp.json()["logged"]["drives"] == 0     # merged, not a second trip

        with SessionLocal() as s:
            d = s.query(Drive).filter(Drive.vehicle_id == vid).one()
            assert d.id == closed_id
            assert d.distance_km == 15.0                    # 12.0 + the further 3.0
            assert d.energy_used_kwh > energy_before         # and the energy came too
            assert d.end_soc == 75                           # measured at the reconnect
            # The whole point: same driving, same efficiency figure. Before the
            # fix this dropped by 3/15 of itself.
            assert abs(d.wh_per_km - wh_before) < wh_before * 0.01
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_sustained_offline_close_records_a_gap_too_late_to_merge(monkeypatch):
    """Past SLEEP_CLOSE_MERGE_MAX_MIN, a large further movement is more likely
    a genuinely separate, later drive than a continuation — the closed trip's
    distance must stay untouched.

    And it must not be reported as lost from that trip either: process_snapshot
    runs next against the same unmodified prev and turns the movement into its
    own drive (asserted below), so stamping end_lost_km on the closed trip as
    well would report the same distance twice under two names — the thing the
    blind-gap fold-in explicitly avoids. The 0.0 recorded at close time is the
    right answer here: this trip really did end where it said it did."""
    import time as _time

    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _OfflineThenParksFarAndLateClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _OfflineThenParksFarAndLateClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineThenParksFarAndLateClient.step = 1
            client.post("/api/sync")
            _OfflineThenParksFarAndLateClient.step = 2
            client.post("/api/sync")                       # offline episode begins

        with SessionLocal() as s:
            state.put(s, state.scoped(state.UNREACHABLE_SINCE_KEY, vin),
                      str(_time.time() - 4 * 60))

        with TestClient(app) as client:
            client.post("/api/sync")                       # sustained offline -> closes at 12.0 km

        with SessionLocal() as s:
            closed_id = s.query(Drive).filter(Drive.vehicle_id == vid).first().id

        _OfflineThenParksFarAndLateClient.step = 3
        with TestClient(app) as client:
            resp = client.post("/api/sync")                # back online, parked, 1.5 km further, 65 min later
            assert resp.json()["logged"]["drives"] == 1      # the 1.5 km becomes its own drive

        with SessionLocal() as s:
            drives = s.query(Drive).filter(Drive.vehicle_id == vid).order_by(Drive.id).all()
            assert len(drives) == 2
            closed = next(d for d in drives if d.id == closed_id)
            assert closed.distance_km == 12.0                # unchanged, not guessed at
            # Not double-reported. Unknown, not a measured zero — the close
            # can't see past its own last reading.
            assert closed.end_lost_km is None
            # The 1.5 km is accounted for exactly once — on the drive that
            # actually covers it, not as a phantom loss on the one before.
            later = next(d for d in drives if d.id != closed_id)
            assert later.distance_km == 1.5
            assert state.get(s, state.scoped(state.LAST_SLEEP_CLOSE_KEY, vin)) == ""
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


class _DrivesThenParksOnlineClient(_SleepsAfterDrivingClient):
    """Drives, then parks and STAYS online — the trip is still open (the
    parked close waits PARK_END_MIN), which is the arrival window."""

    def list_vehicles(self):
        return [{"vin": self.VIN, "id_s": "1", "id": 1, "state": "online"}]

    def vehicle_data(self, vid):
        if type(self).step < 2:
            return super().vehicle_data(vid)
        import time as _t
        return {
            "vin": self.VIN, "display_name": "Highland",
            "drive_state": {"timestamp": int(_t.time() * 1000),
                            "shift_state": "P", "speed": 0},
            "charge_state": {"battery_level": 76, "battery_range": 380.0 / 1.60934,
                             "charging_state": "Disconnected"},
            "climate_state": {"outside_temp": 28},
            "vehicle_state": {"odometer": ODO_KM_TO_MI(10012.0),
                              "is_user_present": True, "locked": False},
            "vehicle_config": {"car_type": "model3"},
        }


def test_arrival_keeps_the_fast_poll_cadence(monkeypatch):
    """The moment a car stops, is_driving goes false and the cadence used to
    drop straight back to the idle tick — exactly when the trip's stop anchor
    most needs a prompt reading. That is what left trip 314's arrival 0.4 km
    short and made trip 316 need a 1002 s trim. A trip still open and only
    just stopped must hold the tight cadence."""
    from app import services

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _DrivesThenParksOnlineClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        with SessionLocal() as s:
            services.link_with_token(s, "tok")

        monkeypatch.setattr("app.tesla_client.TeslaClient", _DrivesThenParksOnlineClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _DrivesThenParksOnlineClient.step = 1
            moving = client.post("/api/sync").json()
            assert moving["status"] == "driving"
            assert moving["poll_fast"] is True            # moving, as before

            _DrivesThenParksOnlineClient.step = 2
            arrived = client.post("/api/sync").json()

        assert arrived["trip_in_progress"] is True        # trip still open
        assert arrived["status"] == "stopped"             # and no longer driving
        assert arrived["poll_fast"] is True               # settling, not abandoned
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repair_arrival_tail_takes_back_only_what_the_estimate_credited(monkeypatch):
    """The automatic correction gets exactly one chance, at the first poll
    after the close. A trip that was already wrong before that correction
    existed is past it and needs a hand repair — driven by the car's own trip
    meter, which is the only authority that settles it.

    And bounded by the estimate: this tool removes a guess, so it must refuse
    to remove anything the guess didn't put there. A trip reading long for some
    other reason is a different fault, and quietly absorbing it here would
    destroy the evidence for it."""
    from app import state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                d = s.query(Drive).order_by(Drive.id).first()
                d.distance_km, d.end_est_km = 14.1, 0.163
                d.start_odo_km, d.end_odo_km = 28943.109, 28957.172
                d.energy_used_kwh = 1.77
                d.duration_min, d.avg_speed_kmh = 21.0, 41.0
                d.end_time = d.start_time + timedelta(minutes=21)
                s.commit()
                did = d.id

            # More than the estimate ever credited: refused, not absorbed.
            over = client.get("/api/repair-arrival-tail",
                              params={"drive_id": did, "true_distance_km": 13.0})
            assert "estimate" in over.json()["detail"]
            assert over.status_code == 409
            # Nor a trip that reads short — the opposite fault entirely.
            short = client.get("/api/repair-arrival-tail",
                               params={"drive_id": did, "true_distance_km": 15.0})
            assert short.status_code == 409

            # A pending automatic correction on this same trip would apply the
            # same fix a second time, against a marker still carrying the
            # original estimate. The dry run has to say so, and applying has to
            # stand it down.
            with SessionLocal() as s:
                veh = s.get(Vehicle, s.get(Drive, did).vehicle_id)
                state.put(s, state.scoped(state.LAST_SLEEP_CLOSE_KEY, veh.vin),
                          _json.dumps({"drive_id": did, "odo_km": 28956.689,
                                       "ts": 0.0, "est_km": 0.483}))
                s.commit()

            r = client.get("/api/repair-arrival-tail",
                           params={"drive_id": did, "true_distance_km": 13.9,
                                   "true_duration_min": 18})
            body = r.json()
            assert r.status_code == 200
            assert body["applied"] is False
            assert body["retracted_km"] == 0.163
            assert body["after"]["distance_km"] == 13.9
            assert body["after"]["end_est_km"] is None
            assert body["pending_auto_correction"] is True
            assert body["after"]["end_odo_km"] == pytest.approx(28957.009)
            assert body["after"]["duration_min"] == 18.0
            # Energy leaves with the kilometres, at the trip's own rate.
            # 1.77 kWh over 14.1 km, kept at that rate over 13.9.
            assert body["after"]["energy_used_kwh"] == pytest.approx(
                1.77 * 13.9 / 14.1, abs=0.006)

            with SessionLocal() as s:
                assert s.get(Drive, did).distance_km == 14.1   # dry run wrote nothing

            client.get("/api/repair-arrival-tail",
                       params={"drive_id": did, "true_distance_km": 13.9,
                               "true_duration_min": 18, "apply": "true"})
            with SessionLocal() as s:
                # Stood down, so the next poll cannot re-apply the same fix —
                # but NOT cleared, or the hand-over it also carries dies with
                # it and the next trip re-counts the tail (see trip 333).
                m = _json.loads(state.get(
                    s, state.scoped(state.LAST_SLEEP_CLOSE_KEY, veh.vin)))
                assert m["corrected"] is True
                fixed = s.get(Drive, did)
                assert fixed.distance_km == 13.9
                assert fixed.end_est_km is None
                assert fixed.duration_min == 18.0
                # avg_speed follows both, or the row contradicts itself.
                assert fixed.avg_speed_kmh == pytest.approx(13.9 / (18.0 / 60.0), abs=0.05)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_estimated_tails_lists_what_to_check_ranked_by_distortion(monkeypatch):
    """Making one trip's estimate visible was half the job: end_est_km answers
    "is THIS trip estimated" and nothing answered "which of them are". Ranked
    by share rather than kilometres, because a fixed tail on a short trip
    distorts its Wh/km far more than the same tail on a long one, and Wh/km is
    what these figures are read through."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with SessionLocal() as s:
            rows = s.query(Drive).order_by(Drive.id).limit(3).all()
            big, small, clean = rows
            big.distance_km, big.end_est_km = 25.0, 0.5        # 2.0%
            small.distance_km, small.end_est_km = 2.0, 0.5     # 25.0%
            clean.end_est_km = None
            s.commit()
            big_id, small_id, clean_id = big.id, small.id, clean.id

        with TestClient(app) as client:
            body = client.get("/api/estimated-tails").json()

        ids = [t["drive_id"] for t in body["trips"]]
        assert clean_id not in ids            # a measured arrival isn't listed
        assert ids.index(small_id) < ids.index(big_id), (
            "the same 0.5 km distorts the 2 km trip more, so it ranks first")
        row = next(t for t in body["trips"] if t["drive_id"] == small_id)
        assert row["estimated_share_pct"] == 25.0
        assert row["distance_if_no_tail"] == 1.5
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_a_verified_estimate_leaves_the_review_list(monkeypatch):
    """A checklist that can never be worked down stops being read. end_est_km
    alone conflates a guess still awaiting a check with one already checked and
    found right — both unseen by any poll, but only the first an open question.
    Repairing against the car's own trip meter settles it, so it goes."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with SessionLocal() as s:
            d = s.query(Drive).order_by(Drive.id).first()
            d.distance_km, d.end_est_km, d.end_est_verified = 14.1, 0.483, None
            d.start_odo_km, d.end_odo_km = 28943.109, 28957.172
            d.energy_used_kwh, d.duration_min = 1.77, 20.8
            d.end_time = d.start_time + timedelta(minutes=20.8)
            s.commit()
            did = d.id

        with TestClient(app) as client:
            listed = client.get("/api/estimated-tails").json()
            assert did in [t["drive_id"] for t in listed["trips"]]

            client.get("/api/repair-arrival-tail",
                       params={"drive_id": did, "true_distance_km": 13.9,
                               "true_duration_min": 18, "apply": "true"})

            after = client.get("/api/estimated-tails").json()
            assert did not in [t["drive_id"] for t in after["trips"]]

        with SessionLocal() as s:
            fixed = s.get(Drive, did)
            # Gone from the list, but the provenance is NOT erased: 0.32 km of
            # this trip still never appeared in any poll, and a reader has to
            # be able to see that.
            assert fixed.end_est_km == pytest.approx(0.32)
            assert fixed.end_est_verified is True
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_confirming_a_trip_that_already_matches_is_a_result_not_an_error(monkeypatch):
    """"It already agrees with the car" is the common outcome of checking a
    trip, and this endpoint is how a trip gets checked. Refusing it as an error
    left trip 332 stranded: repaired before end_est_verified existed, it could
    never be marked, because asking again produced a difference of exactly zero
    and got a 409. A check that cannot return "correct" is not a check."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with SessionLocal() as s:
            d = s.query(Drive).order_by(Drive.id).first()
            # Already reconciled: distance matches the car, tail still recorded.
            d.distance_km, d.end_est_km, d.end_est_verified = 13.9, 0.32, None
            d.start_odo_km, d.end_odo_km = 28943.109, 28957.009
            d.energy_used_kwh, d.duration_min = 1.74, 18.0
            s.commit()
            did, energy, dist = d.id, d.energy_used_kwh, d.distance_km

        with TestClient(app) as client:
            assert did in [t["drive_id"] for t in
                           client.get("/api/estimated-tails").json()["trips"]]

            r = client.get("/api/repair-arrival-tail",
                           params={"drive_id": did, "true_distance_km": 13.9,
                                   "apply": "true"})
            body = r.json()
            assert r.status_code == 200
            assert body["outcome"] == "already_matches"
            assert body["retracted_km"] == 0.0
            # Not -0.0: this endpoint gives a negative difference the meaning
            # "shorter than the car says", so a match must not wear that sign.
            import math
            assert not math.copysign(1, body["difference_km"]) < 0
            assert body["after"]["distance_km"] == dist      # nothing rewritten
            assert body["after"]["end_est_km"] == 0.32

            # Still a real check that can fail: a car figure that disagrees by
            # more than the screen's own resolution is not "already matches".
            assert client.get("/api/repair-arrival-tail",
                              params={"drive_id": did,
                                      "true_distance_km": 15.0}).status_code == 409

            assert did not in [t["drive_id"] for t in
                               client.get("/api/estimated-tails").json()["trips"]]

        with SessionLocal() as s:
            done = s.get(Drive, did)
            assert done.end_est_verified is True
            assert done.distance_km == dist
            assert done.energy_used_kwh == energy
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_a_measured_tail_is_recorded_as_a_calibration_sample(monkeypatch):
    """The arrival model's window shipped set to the poller's own unreachable
    timeout, and the first trip that could test it came back 51% long. Fixing
    that needs evidence, not another guess — so every time a poll measures the
    stretch an estimate was about, the pair is kept.

    The prediction is stored UNCLAMPED. est_credited is trimmed to what the car
    actually covered before the trip is corrected, which is right for the
    correction and useless for scoring: a prediction trimmed to fit the outcome
    always scores perfectly."""
    from app.models import ArrivalTailSample

    *_, rows = _run_asleep_close(monkeypatch, _SlowlyArrivesThenParksFurtherOnClient, place_tail=0.8)
    assert len(rows) == 1
    r = rows[0]
    assert r.est_km == pytest.approx(0.8)       # predicted, not clamped
    assert r.measured_km == pytest.approx(0.9)  # the whole unseen stretch
    assert r.speed_kmh == pytest.approx(32.19, abs=0.01)
    assert r.reason == "asleep"


def test_no_sample_when_the_tail_cannot_be_measured(monkeypatch):
    """A car already driving again when next seen has an odometer covering the
    new trip too, so the stretch the estimate was about is not isolatable. No
    row beats a row that quietly conflates the two — a calibration set is only
    worth having if every pair in it is real."""
    from app.models import ArrivalTailSample

    *_, rows = _run_asleep_close(monkeypatch, _SlowlyArrivesThenDrivesAgainClient, place_tail=0.8)
    assert rows == []


def test_arrival_estimates_reports_what_each_place_has_measured(monkeypatch):
    """The places block is the model, not a summary of it: those medians are
    what a future arrival at that car park will actually be credited, and
    in_use says whether the place has enough measurements to be trusted.

    A place short of data is reported but not used — no estimate at all is the
    honest answer there, and the one that beat the speed model this replaced."""
    from datetime import datetime as _dt

    from app.models import ArrivalTailSample, Vehicle as _V

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                vid = s.query(_V).first().id
                s.query(ArrivalTailSample).delete()
                for i, (place, measured) in enumerate(
                        (("Home", 0.320), ("Home", 0.111), ("Home", 0.053),
                         ("Office", 0.015))):
                    s.add(ArrivalTailSample(
                        vehicle_id=vid, drive_id=None, ts=_dt(2026, 8, 5, 12, i),
                        est_km=0.4, measured_km=measured, place=place,
                        reason="verified"))
                s.commit()

            body = client.get("/api/arrival-estimates").json()

        home = next(p for p in body["places"] if p["place"] == "Home")
        assert home["samples"] == 3
        assert home["median_tail_km"] == pytest.approx(0.111)
        assert home["in_use"] is True
        office = next(p for p in body["places"] if p["place"] == "Office")
        assert office["samples"] == 1 and office["in_use"] is False
        assert body["summary"]["places_estimating"] == 1
        assert body["summary"]["places_short_of_data"] == 1
        # The scorecard: 0.4 credited against tails that averaged well under.
        assert body["summary"]["over_predicting"] is True
        # And nothing about a window, because there is no longer one to tune.
        assert "suggested_window_sec" not in body["summary"]
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_a_hand_repaired_trip_still_hands_its_tail_over(monkeypatch):
    """Trip 333. The sleep-close marker does two jobs — correct the trip that
    closed, and tell the NEXT trip where to begin — and repair_arrival_tail
    cleared it outright to stop the first. That cancelled the second too, so
    the following trip anchored to the pre-blackout reading and re-counted
    0.320 km trip 332 already held: 11.0 km against the car's 10.7.

    The marker is now updated rather than dropped, carrying the estimate that
    survived the repair, which is exactly the amount the next trip must start
    past."""
    from app import state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                d = s.query(Drive).order_by(Drive.id).first()
                d.distance_km, d.end_est_km, d.end_est_verified = 14.1, 0.483, None
                d.start_odo_km, d.end_odo_km = 28943.109, 28957.172
                d.energy_used_kwh, d.duration_min = 1.77, 20.8
                d.end_time = d.start_time + timedelta(minutes=20.8)
                veh = s.get(Vehicle, d.vehicle_id)
                key = state.scoped(state.LAST_SLEEP_CLOSE_KEY, veh.vin)
                state.put(s, key, _json.dumps(
                    {"drive_id": d.id, "odo_km": 28956.689, "ts": 0.0,
                     "est_km": 0.483, "est_sec": 180.0, "reason": "asleep"}))
                s.commit()
                did, vin = d.id, veh.vin

            client.get("/api/repair-arrival-tail",
                       params={"drive_id": did, "true_distance_km": 13.9,
                               "true_duration_min": 18, "apply": "true"})

        with SessionLocal() as s:
            marker = _json.loads(state.get(s, state.scoped(
                state.LAST_SLEEP_CLOSE_KEY, vin)))
        # Alive, so the hand-over survives...
        assert marker["corrected"] is True
        # ...and carrying the corrected amount, not the original 0.483, which
        # is what made keeping it dangerous before.
        assert marker["est_km"] == pytest.approx(0.32)
        assert marker["odo_km"] == 28956.689     # a reading, not a claim
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repair_trip_overlap_takes_back_only_the_doubled_ground(monkeypatch):
    """repair_trip_boundary refuses this and is right to: it MOVES a shared
    boundary, conserving distance between two trips whose anchors agree. An
    overlap is where they disagree, and it is not a transfer — the earlier trip
    is already verified, so the later trip's excess belongs to nobody."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                a, b = s.query(Drive).order_by(Drive.id).limit(2).all()
                a.end_odo_km = 28957.009                    # verified arrival
                b.start_odo_km, b.end_odo_km = 28956.689, 28967.696
                b.distance_km, b.energy_used_kwh = 11.0, 1.96
                b.duration_min, b.start_recovered_km = 34.0, 1.307
                s.commit()
                aid, bid = a.id, b.id

            # The boundary tool declines, naming the reason.
            clash = client.get("/api/repair-trip-boundary",
                               params={"closed_id": aid, "open_id": bid,
                                       "boundary_odo_km": 28957.009})
            assert clash.status_code == 409

            body = client.get("/api/repair-trip-overlap",
                              params={"closed_id": aid, "open_id": bid}).json()
            assert body["overlap_km"] == pytest.approx(0.32)
            assert body["open"]["distance_km"] == [11.0, 10.7]   # the car's figure
            assert body["open"]["energy_kwh"][1] == pytest.approx(1.9, abs=0.005)
            # What the recovery reclaimed is measured from the anchor, so it
            # moves with it or it claims ground the previous trip now holds.
            assert body["open"]["start_recovered_km"] == [1.307, 0.987]
            with SessionLocal() as s:
                assert s.get(Drive, bid).distance_km == 11.0    # dry run only

            client.get("/api/repair-trip-overlap",
                       params={"closed_id": aid, "open_id": bid, "apply": "true"})

        with SessionLocal() as s:
            fixed, kept = s.get(Drive, bid), s.get(Drive, aid)
            assert fixed.start_odo_km == pytest.approx(28957.009)
            assert fixed.distance_km == 10.7
            assert fixed.avg_speed_kmh == pytest.approx(10.7 / (34.0 / 60.0), abs=0.05)
            assert kept.end_odo_km == pytest.approx(28957.009)   # authority untouched
    finally:
        settings.app_passcode = old
        _reset_to_demo()


class _OfflineThenDrivingAgainClient(_OfflineThenParksWithCreepClient):
    """The reconnect catches the car ALREADY DRIVING, 0.425 km on, half an hour
    after the close — inside the merge window, but with the odometer covering
    two trips at once and no reading between them to say where one ends."""

    def vehicle_data(self, vid):
        d = super().vehicle_data(vid)
        if type(self).step >= 3:
            d["drive_state"]["shift_state"] = "D"
            d["drive_state"]["speed"] = 25
            d["vehicle_state"]["odometer"] = ODO_KM_TO_MI(10012.425)
        return d


def test_a_gap_spanning_two_trips_is_not_charged_to_the_earlier_one(monkeypatch):
    """Trips 334 and 335. The offline close logged end_lost_km 0.425 while the
    next trip recorded start_recovered_km 0.425 for the same ground — the same
    distance twice under two names, which is the defect this branch's own
    comment exists to prevent. The time window did not catch it: the reconnect
    was 27 minutes later, well inside the merge window.

    What the window cannot see is that the car was already DRIVING. `moved`
    then spans the arrival this trip was cut short of and the departure the
    next one is in the middle of, and the real split (about 0.198 / 0.227) is
    unknowable. None says that; a number asserts otherwise."""
    import time as _time

    from app import services, state
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    _OfflineThenDrivingAgainClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            vid = s.query(Vehicle).filter(Vehicle.vin == vin).first().id

        monkeypatch.setattr("app.tesla_client.TeslaClient", _OfflineThenDrivingAgainClient)
        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineThenDrivingAgainClient.step = 1
            client.post("/api/sync")
            _OfflineThenDrivingAgainClient.step = 2
            client.post("/api/sync")
        with SessionLocal() as s:
            state.put(s, state.scoped(state.UNREACHABLE_SINCE_KEY, vin),
                      str(_time.time() - 4 * 60))
        with TestClient(app) as client:
            client.post("/api/sync")                    # sustained offline -> closes
        with SessionLocal() as s:
            closed_id = s.query(Drive).filter(Drive.vehicle_id == vid).one().id

        _OfflineThenDrivingAgainClient.step = 3
        with TestClient(app) as client:
            client.post("/api/sync")                    # back online, already driving

        with SessionLocal() as s:
            closed = s.get(Drive, closed_id)
            assert closed.distance_km == 12.0           # untouched
            assert closed.end_lost_km is None, (
                "the 0.425 km spans two trips; charging all of it here "
                "double-reports what the next trip is about to recover")
            # The open trip anchors at the close, so the ground is carried
            # exactly once — by the trip that is actually driving through it.
            open_trip = _json.loads(state.get(
                s, state.scoped(state.OPEN_TRIP_KEY, vin)) or "null")
            assert open_trip and open_trip["odo_km"] == pytest.approx(10012.0, abs=0.002)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_clearing_a_duplicated_loss_refuses_to_erase_a_real_one(monkeypatch):
    """Trip 334 claimed end_lost_km 0.425 while trip 335 recovered the same
    0.425. Clearing that is right; clearing a loss no one else accounts for
    would destroy the only record of a genuine boundary error, which is the
    opposite of what the field is for. So the tool re-derives the duplication
    from the two rows and refuses when it does not hold."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                a, b = s.query(Drive).order_by(Drive.start_time).limit(2).all()
                a.end_odo_km, a.end_lost_km = 28974.998, 0.425
                b.start_odo_km, b.start_recovered_km = 28974.998, 0.425
                s.commit()
                aid, bid = a.id, b.id

            listed = client.get("/api/clear-duplicated-loss").json()
            row = next(c for c in listed["candidates"] if c["drive_id"] == aid)
            assert row["next_drive_id"] == bid
            assert row["end_lost_km"] == 0.425
            with SessionLocal() as s:
                assert s.get(Drive, aid).end_lost_km == 0.425   # listing writes nothing

            # A genuine loss: the next trip did NOT recover it. Refused.
            with SessionLocal() as s:
                s.get(Drive, bid).start_recovered_km = 0.0
                s.commit()
            assert client.get("/api/clear-duplicated-loss",
                              params={"drive_id": aid}).status_code == 409
            # Nor when the two trips don't share a boundary at all.
            with SessionLocal() as s:
                nb = s.get(Drive, bid)
                nb.start_recovered_km, nb.start_odo_km = 0.425, 28970.0
                s.commit()
            assert client.get("/api/clear-duplicated-loss",
                              params={"drive_id": aid}).status_code == 409

            with SessionLocal() as s:
                nb = s.get(Drive, bid)
                nb.start_odo_km = 28974.998
                s.commit()
            r = client.get("/api/clear-duplicated-loss",
                           params={"drive_id": aid, "apply": "true"})
            assert r.status_code == 200 and r.json()["applied"] is True

        with SessionLocal() as s:
            assert s.get(Drive, aid).end_lost_km is None
            # The next trip keeps its record: the ground is still accounted
            # for, once, by the trip that actually covered it.
            assert s.get(Drive, bid).start_recovered_km == 0.425
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_an_estimated_arrival_cannot_end_after_the_next_trip_starts(monkeypatch):
    """Trip 339 ended at 17:01 while trip 340 started at 16:59. The sleep-close
    estimate moves the clock forward with the odometer, and nothing bounded it
    by what happened next: it credited three minutes of arriving to a car that
    was driving again within one.

    services.edit_drive already refuses to let a person create overlapping
    trips by hand, so the app was producing a state it will not accept."""
    from datetime import datetime as _dt

    from app.api.routes import _unoverlap_previous
    from app.models import Drive, Vehicle as _V

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            assert client
            with SessionLocal() as s:
                vid = s.query(_V).first().id
                s.query(Drive).delete()
                est = Drive(vehicle_id=vid, start_time=_dt(2026, 8, 5, 16, 26),
                            end_time=_dt(2026, 8, 5, 17, 1), distance_km=7.1,
                            duration_min=34.0, avg_speed_kmh=12.0, end_est_km=0.394)
                s.add(est)
                s.commit()
                eid = est.id

                _unoverlap_previous(s, vid, _dt(2026, 8, 5, 16, 59))
                s.commit()
                fixed = s.get(Drive, eid)
                assert fixed.end_time == _dt(2026, 8, 5, 16, 59)
                assert fixed.duration_min == 33.0
                # Speed follows the clock, or the row contradicts itself.
                assert fixed.avg_speed_kmh == pytest.approx(7.1 / (33.0 / 60.0), abs=0.05)
                # The distance stays: the next trip already begins past it, so
                # trimming it here would leave that ground belonging to nobody.
                assert fixed.distance_km == 7.1

                # A MEASURED end is never moved. If a real reading lands after
                # the next start, something is wrong that a timestamp cannot fix.
                meas = Drive(vehicle_id=vid, start_time=_dt(2026, 8, 5, 18, 0),
                             end_time=_dt(2026, 8, 5, 18, 30), distance_km=5.0,
                             duration_min=30.0, avg_speed_kmh=10.0, end_est_km=None)
                s.add(meas)
                s.commit()
                _unoverlap_previous(s, vid, _dt(2026, 8, 5, 18, 20))
                s.commit()
                assert s.get(Drive, meas.id).end_time == _dt(2026, 8, 5, 18, 30)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_a_checked_trip_feeds_the_place_it_arrived_at(monkeypatch):
    """The automatic sample needs a poll that finds the car parked after a
    no-network arrival — the exact case a multi-storey defeats, so at Home it
    has never once fired. Checking a trip against the car's own screen is the
    only measurement available there, and it now feeds the model rather than
    only correcting one row.

    Two measurements at a place, and the estimate starts using its median."""
    from app.api.routes import PLACE_TAIL_MIN_SAMPLES, _place_tail_km
    from app.models import ArrivalTailSample, Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.query(ArrivalTailSample).delete()
                rows = s.query(Drive).order_by(Drive.id).limit(2).all()
                for d, (span_end, est) in zip(rows, ((29030.117, 0.322),
                                                     (29008.311, 0.394))):
                    d.end_location = "Home"
                    d.start_odo_km, d.end_odo_km = span_end - 2.469, span_end
                    d.distance_km, d.end_est_km = 2.5, est
                    d.energy_used_kwh, d.duration_min = 0.51, 9.0
                s.commit()
                ids = [d.id for d in rows]
                assert _place_tail_km(s, "Home") is None      # nothing measured yet

            # Trip 341's real figures: 2.2 km on the car against our 2.469 span
            # with 0.322 estimated, so the tail it actually drove was 0.053.
            client.get("/api/repair-arrival-tail",
                       params={"drive_id": ids[0], "true_distance_km": 2.2,
                               "apply": "true"})
            client.get("/api/repair-arrival-tail",
                       params={"drive_id": ids[1], "true_distance_km": 2.3,
                               "apply": "true"})

            # Run again: an arrival is one event, however many times it is
            # checked. Re-confirming must not let it vote twice.
            client.get("/api/repair-arrival-tail",
                       params={"drive_id": ids[0], "true_distance_km": 2.2,
                               "apply": "true"})

            body = client.get("/api/arrival-estimates").json()

        home = next(p for p in body["places"] if p["place"] == "Home")
        assert home["samples"] == PLACE_TAIL_MIN_SAMPLES
        assert home["in_use"] is True
        with SessionLocal() as s:
            got = s.query(ArrivalTailSample).all()
            assert sorted(round(g.measured_km, 3) for g in got) == [0.053, 0.225]
            assert all(g.place == "Home" for g in got)
            # And the estimate now runs on what the place showed, not a guess.
            assert _place_tail_km(s, "Home") == pytest.approx(0.139, abs=0.001)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repairing_an_arrival_moves_the_next_trip_start_with_it(monkeypatch):
    """A boundary is one position shared by two rows, so moving it in one is
    never enough. Trip 341's end came back 0.269 km; trip 342 had been logged
    hours earlier and kept starting where the estimate had wrongly put the car,
    leaving 0.269 km of real driving belonging to no trip. It read 11.1 km
    against the car's own 11.3."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                a, b = s.query(Drive).order_by(Drive.start_time).limit(2).all()
                a.start_odo_km, a.end_odo_km = 29027.648, 29030.117
                a.distance_km, a.end_est_km, a.end_location = 2.5, 0.322, "Home"
                a.energy_used_kwh, a.duration_min = 0.51, 9.0
                b.start_odo_km, b.end_odo_km = 29030.117, 29041.227
                b.distance_km, b.energy_used_kwh = 11.1, 1.57
                b.duration_min, b.start_recovered_km = 29.0, 0.125
                s.commit()
                aid, bid = a.id, b.id

            r = client.get("/api/repair-arrival-tail",
                           params={"drive_id": aid, "true_distance_km": 2.2,
                                   "apply": "true"})
            assert r.json()["next_trip"]["gap_km"] == pytest.approx(0.269)

            # Checking it AGAIN retracts nothing — the first check already did
            # — but must not undo or re-apply the successor's shift. A repair
            # that only works the first time is a trap for anyone who reruns it.
            again = client.get("/api/repair-arrival-tail",
                               params={"drive_id": aid, "true_distance_km": 2.2,
                                       "apply": "true"}).json()
            assert again["outcome"] == "already_matches"
            assert again["next_trip"] is None

        with SessionLocal() as s:
            fixed, nxt = s.get(Drive, aid), s.get(Drive, bid)
            assert fixed.end_odo_km == pytest.approx(29029.848, abs=0.002)
            # The successor starts where its predecessor now ends. No gap.
            assert nxt.start_odo_km == pytest.approx(fixed.end_odo_km, abs=0.002)
            assert nxt.distance_km == pytest.approx(11.4)
            # Energy follows at the trip's own rate, and the ground it gained
            # was unseen by any poll, which is what start_recovered_km records.
            assert nxt.energy_used_kwh == pytest.approx(1.57 * 11.379 / 11.110, abs=0.01)
            assert nxt.start_recovered_km == pytest.approx(0.394)  # 0.125 + 0.269
            assert nxt.avg_speed_kmh == pytest.approx(11.4 / (29.0 / 60.0), abs=0.05)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repairing_a_departure_takes_the_swallowed_park_s_drain_with_it(monkeypatch):
    """Trip 340 read 16 minutes against the car's own 5, and 0.50 kWh against
    0.38, because an 11-minute park fell under the re-anchoring threshold and
    landed inside the trip. sync no longer lets that happen, but a row already
    written keeps it, and neither odometer repair can touch it — the distance
    and the boundary are right, the clock is wrong.

    The park's drain has to leave with its minutes. Moving the clock alone
    would leave a trip whose Wh/km still carries eleven minutes of standing
    still, which is the whole reason the figure was wrong."""
    from datetime import datetime as _dt

    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    # A known parked rate, so the arithmetic is the test's rather than the
    # seeded history's. None is also valid — trim_standby_kwh subtracts nothing
    # when a car cannot yet support a figure — and the response says so through
    # standby_rate_kw, but then there is no subtraction to assert.
    monkeypatch.setattr("app.api.routes._trim_rate_kw", lambda *a, **k: 0.5)
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                d = s.query(Drive).order_by(Drive.start_time).first()
                d.start_time = _dt(2026, 8, 5, 16, 59)
                d.end_time = _dt(2026, 8, 5, 17, 15)
                d.duration_min, d.distance_km = 16.0, 1.8
                d.energy_used_kwh, d.avg_speed_kmh = 0.50, 6.0
                d.idle_min = 12.0
                s.commit()
                did = d.id

            # A trip reading SHORT is a different fault and must be refused.
            assert client.get("/api/repair-departure-start",
                              params={"drive_id": did,
                                      "true_duration_min": 20}).status_code == 409

            body = client.get("/api/repair-departure-start",
                              params={"drive_id": did, "true_duration_min": 5}).json()
            assert body["applied"] is False
            assert body["moved_sec"] == pytest.approx(660.0)      # the 11 min park
            assert body["start_time"][1].endswith("17:10:00")
            assert body["distance_km"] == 1.8                     # never altered
            # 11 min at 0.5 kW is 0.092 kWh, and it leaves with its minutes.
            assert body["standby_rate_kw"] == 0.5
            assert body["energy_kwh"][1] == pytest.approx(0.41)
            with SessionLocal() as s:
                assert s.get(Drive, did).duration_min == 16.0     # dry run only

            client.get("/api/repair-departure-start",
                       params={"drive_id": did, "true_duration_min": 5,
                               "apply": "true"})

        with SessionLocal() as s:
            fixed = s.get(Drive, did)
            assert fixed.start_time == _dt(2026, 8, 5, 17, 10)
            assert fixed.duration_min == 5.0
            assert fixed.distance_km == 1.8
            assert fixed.avg_speed_kmh == pytest.approx(1.8 / (5.0 / 60.0), abs=0.05)
            # Idle cannot outlast a duration that just shrank.
            assert fixed.idle_min <= 5.0
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_moving_a_boundary_forward_records_the_tail_it_measured(monkeypatch):
    """Trips 334 and 337 read SHORT, so repair_arrival_tail refuses them — it
    only removes estimated distance. The boundary tool handles the other
    direction, and in doing so it measures the very thing the arrival model now
    runs on: the ground between the last reading a poll took and where the car
    turned out to have stopped.

    At a place with no signal that check is the only source of such a
    measurement, so spending one on a single row and discarding it is waste."""
    from app.api.routes import _place_tail_km
    from app.models import ArrivalTailSample, Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                s.query(ArrivalTailSample).delete()
                a, b = s.query(Drive).order_by(Drive.start_time).limit(2).all()
                # Trip 337's shape: 0.04 estimated, so a poll last saw
                # 29008.271, and the car actually stopped at 29008.464.
                a.start_odo_km, a.end_odo_km = 28990.464, 29008.311
                a.distance_km, a.end_est_km, a.end_location = 17.8, 0.04, "Home"
                a.energy_used_kwh, a.duration_min = 2.70, 43.0
                b.start_odo_km, b.end_odo_km = 29008.311, 29019.076
                b.distance_km, b.energy_used_kwh = 10.8, 1.94
                b.duration_min = 36.0
                s.commit()
                aid, bid = a.id, b.id

            body = client.get("/api/repair-trip-boundary",
                              params={"closed_id": aid, "open_id": bid,
                                      "boundary_odo_km": 29008.464,
                                      "apply": "true"}).json()
            assert body["applied"] is True
            assert body["closed"]["distance_km"] == [17.8, 18.0]
            assert body["open"]["distance_km"] == [10.8, 10.6]

        with SessionLocal() as s:
            sample = s.query(ArrivalTailSample).one()
            # 29008.464 minus the 29008.271 a poll last saw.
            assert sample.measured_km == pytest.approx(0.193)
            assert sample.est_km == pytest.approx(0.04)   # what had been guessed
            assert sample.place == "Home"
            closed = s.get(Drive, aid)
            # Unseen by any poll still, but no longer a guess.
            assert closed.end_est_km == pytest.approx(0.193)
            assert closed.end_est_verified is True
            assert _place_tail_km(s, "Home") is None      # one sample is not a median
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_confirming_a_trip_still_corrects_a_clock_the_estimate_inflated(monkeypatch):
    """Trip 341 read 9 minutes against the car's 6 after its distance had been
    fixed. The arrival estimate moves the odometer and the clock together, and
    so does the retraction — but the retraction returns early once there is no
    distance left to take off, so a duration given on a later call did nothing.

    Distance and energy stay put: they already agree, and minutes that were
    never driven have nothing to reprice."""
    from datetime import datetime as _dt

    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                d = s.query(Drive).order_by(Drive.start_time).first()
                d.start_time = _dt(2026, 8, 5, 18, 5)
                d.end_time = _dt(2026, 8, 5, 18, 14)
                d.duration_min, d.avg_speed_kmh = 9.0, 14.7
                d.start_odo_km, d.end_odo_km = 29027.648, 29029.848
                d.distance_km, d.end_est_km = 2.2, 0.053
                d.energy_used_kwh = 0.45
                s.commit()
                did = d.id

            body = client.get("/api/repair-arrival-tail",
                              params={"drive_id": did, "true_distance_km": 2.2,
                                      "true_duration_min": 6,
                                      "apply": "true"}).json()
            assert body["outcome"] == "already_matches"
            assert body["retracted_km"] == 0.0          # nothing to take off
            assert body["after"]["duration_min"] == 6.0  # but the clock moves

        with SessionLocal() as s:
            fixed = s.get(Drive, did)
            assert fixed.duration_min == 6.0
            assert fixed.end_time == _dt(2026, 8, 5, 18, 11)
            assert fixed.distance_km == 2.2             # untouched
            assert fixed.energy_used_kwh == 0.45        # untouched
            assert fixed.avg_speed_kmh == pytest.approx(2.2 / (6.0 / 60.0), abs=0.05)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_the_cars_own_wh_per_km_beats_charging_a_park_at_an_average(monkeypatch):
    """Trip 340 still read 0.54 kWh against the car's 0.38 after its clock was
    fixed. The drain correction charges a swallowed park at this car's MEAN
    parked rate, and a specific park can be far from the mean: eleven minutes
    in a 34 degree car park, awake with climate running, drew about 1.07 kW
    where the average said 0.323.

    Where the car has measured the drive itself, measurement wins — the same
    principle as true_distance_km for an arrival. And a figure no real drive
    could average is refused rather than written."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                d = s.query(Drive).order_by(Drive.start_time).first()
                d.duration_min, d.distance_km = 5.0, 1.8
                d.energy_used_kwh, d.avg_speed_kmh = 0.54, 21.6
                s.commit()
                did, started = d.id, d.start_time

            # Neither figure given: nothing to correct from.
            assert client.get("/api/repair-departure-start",
                              params={"drive_id": did}).status_code == 409
            # A rate no drive could average is refused, not written.
            assert client.get("/api/repair-departure-start",
                              params={"drive_id": did,
                                      "true_wh_per_km": 5}).status_code == 409

            body = client.get("/api/repair-departure-start",
                              params={"drive_id": did, "true_wh_per_km": 223.8,
                                      "apply": "true"}).json()
            assert body["energy_source"] == "car"
            # No modelled rate was used, so none is reported.
            assert body["standby_rate_kw"] is None
            assert body["energy_kwh"] == [0.54, pytest.approx(0.40)]
            assert body["duration_min"] == [5.0, 5.0]      # clock left alone

        with SessionLocal() as s:
            fixed = s.get(Drive, did)
            assert fixed.energy_used_kwh == pytest.approx(0.40)
            assert fixed.start_time == started              # not moved
            assert fixed.distance_km == 1.8
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_polling_stands_down_while_every_car_is_asleep(monkeypatch):
    """list_vehicles() runs on every /api/sync whether or not anything can have
    changed, and a sleeping car cannot move. On a real account that was 60% of
    a month's Fleet API requests — RM 105 projected against a RM 45 allowance,
    with the billing limit reached around the 22nd and the app then blind.

    Quiet means every car reads not-online AND none has a trip or charge open.
    Any doubt clears the window rather than setting it: a missed departure
    costs boundary precision, and that is the error this whole week undid."""
    from app import services, state

    settings = get_settings()
    old_pass, old_win = settings.app_passcode, settings.sleep_recheck_min
    settings.app_passcode = ""
    settings.sleep_recheck_min = 20.0
    _SleepsAfterDrivingClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")

        calls = {"n": 0}
        real = _SleepsAfterDrivingClient.list_vehicles

        def counted(self):
            calls["n"] += 1
            return real(self)

        monkeypatch.setattr(_SleepsAfterDrivingClient, "list_vehicles", counted)
        monkeypatch.setattr("app.tesla_client.TeslaClient", _SleepsAfterDrivingClient)

        with TestClient(app) as client:
            client.post("/api/sync")                 # online, parked
            _SleepsAfterDrivingClient.step = 1
            client.post("/api/sync")                 # driving: opens a trip
            _SleepsAfterDrivingClient.step = 2       # reports asleep
            client.post("/api/sync")                 # closes the trip on sleep
            armed = calls["n"]
            with SessionLocal() as s:
                assert state.get(s, state.SUSPEND_KEY), "a quiet tick must arm it"

            body = client.post("/api/sync").json()   # cron tick inside the window
            assert body["skipped"] == "asleep"
            assert body["next_check_sec"] > 0
            assert calls["n"] == armed, "the skipped tick must not call Tesla at all"
            # And it must not be able to hold the loop down. state.put commits
            # immediately, so a tick that re-arms the window and then crashes
            # leaves the re-arm behind and nothing else — measured live as 353
            # consecutive skipped ticks over 6.2 hours, an unbroken run with
            # not one check in it, against a 20-minute window. Past
            # SUSPEND_MAX_QUIET_MIN with no tick completing, the window is
            # ignored and the work happens anyway.
            import time as _t

            from app.api import routes as _routes
            with SessionLocal() as s:
                state.put(s, state.SUSPEND_KEY, str(_t.time() + 3600))
                state.put(s, state.FULL_TICK_KEY,
                          str(_t.time() - (_routes.SUSPEND_MAX_QUIET_MIN + 5) * 60))
                s.commit()
            revived = client.post("/api/sync").json()
            assert revived.get("skipped") != "asleep", (
                "a stale completion marker must override the back-off")
            with SessionLocal() as s:
                assert float(state.get(s, state.FULL_TICK_KEY)) > _t.time() - 60, (
                    "a tick that completed must refresh the marker")

            # It must still record that it RAN. Spending nothing and never
            # being called leave the same absence otherwise, and telling those
            # two apart is the whole point of the tick log (see _log_tick).
            assert "backoff" in [r["outcome"] for r in
                                 client.get("/api/sync-log").json()["runs"]]

            # The manual Sync button ignores it — the person pressing it is the
            # reason the button exists. (wake=1 polls a waking car for ~30 s;
            # the waiting is not what is under test.)
            monkeypatch.setattr("time.sleep", lambda *_: None)
            client.post("/api/sync?wake=1")
            assert calls["n"] > armed
    finally:
        settings.app_passcode, settings.sleep_recheck_min = old_pass, old_win
        _reset_to_demo()


def test_a_trip_open_through_a_dead_zone_keeps_polling(monkeypatch):
    """"Nothing is online" is not enough on its own. A car mid-drive through a
    tunnel reads offline with its trip still open, and that is precisely when
    the reconnect must be caught — standing down for the window would leave a
    live drive unwatched and close it on stale readings.

    So the open trip is checked as well as the reported state, and it is the
    condition that saves this case."""
    from app import services, state

    settings = get_settings()
    old_pass, old_win = settings.app_passcode, settings.sleep_recheck_min
    settings.app_passcode = ""
    settings.sleep_recheck_min = 20.0
    _OfflineAfterDrivingClient.step = 0
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        vin = "VINAAAAAAAAAAAAAA"
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
        monkeypatch.setattr("app.tesla_client.TeslaClient", _OfflineAfterDrivingClient)

        with TestClient(app) as client:
            client.post("/api/sync")
            _OfflineAfterDrivingClient.step = 2      # offline mid-drive
            with SessionLocal() as s:
                state.put(s, state.scoped(state.OPEN_TRIP_KEY, vin),
                          _json.dumps({"ts": 0.0, "odo_km": 10_000.0, "soc": 80}))
                s.commit()
            client.post("/api/sync")

            with SessionLocal() as s:
                assert state.get(s, state.SUSPEND_KEY) == "", (
                    "a live trip through a dead zone must not be slept through")

            # And with that trip gone, the same offline reading does arm it.
            with SessionLocal() as s:
                state.put(s, state.scoped(state.OPEN_TRIP_KEY, vin), "")
                s.commit()
            client.post("/api/sync")
            with SessionLocal() as s:
                assert state.get(s, state.SUSPEND_KEY)
    finally:
        settings.app_passcode, settings.sleep_recheck_min = old_pass, old_win
        _reset_to_demo()


def test_repair_lost_departure_gives_back_the_head_a_blackout_took():
    """A row already written keeps whatever sync gave it, and the case that
    most needs fixing is the one sync used to decline: a blackout that hid one
    trip's arrival and then, through the prev it left frozen mid-drive,
    disarmed the recovery at the next trip's departure.

    Trip 359's real numbers: the car reported 27.2 km from Home, we logged
    17.2 km from a street 10.092 km downroad, and start_lost_km recorded the
    difference exactly.

    Guarded by the car's own trip meter — the repair is only allowed when
    moving the start back to the previous trip's end reproduces it, which is
    what separates this trip's missing head from a journey nobody logged."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                a, b = s.query(Drive).order_by(Drive.id).limit(2).all()
                a.end_odo_km = 29318.155
                a.end_coords, a.end_location, a.end_area = "5.4100, 100.3000", "Home", "Home"
                b.start_time = a.end_time + timedelta(minutes=28)
                b.end_time = b.start_time + timedelta(minutes=64)
                b.start_odo_km, b.end_odo_km = 29328.247, 29345.414
                b.distance_km, b.energy_used_kwh = 17.2, 2.91
                b.duration_min, b.avg_speed_kmh = 64.0, 16.0
                b.start_lost_km, b.start_recovered_km = 10.092, 0.0
                b.start_coords = "5.3754, 100.2980"
                b.start_location = b.start_area = "63 Cangkat Bukit Gambir, Farlim"
                s.commit()
                did, prev_id = b.id, a.id

            # A distance the move wouldn't reproduce means the hole is not this
            # trip's head — refused, not absorbed.
            wrong = client.get("/api/repair-lost-departure",
                               params={"drive_id": did, "true_distance_km": 22.0})
            assert wrong.status_code == 409
            assert "not this trip's missing departure" in wrong.json()["detail"]

            r = client.get("/api/repair-lost-departure",
                           params={"drive_id": did, "true_distance_km": 27.2,
                                   "true_duration_min": 66})
            body = r.json()
            assert r.status_code == 200
            assert body["applied"] is False
            assert body["recovered_km"] == 10.092
            assert body["from_trip"]["id"] == prev_id
            assert body["distance_km"] == [17.2, 27.3]
            assert body["start_location"] == ["63 Cangkat Bukit Gambir, Farlim", "Home"]
            # The car's own 6.9% of a 69.5 kWh pack is 4.79 kWh.
            assert body["energy_kwh"][1] == pytest.approx(4.79, abs=0.10)

            with SessionLocal() as s:
                assert s.get(Drive, did).distance_km == 17.2   # dry run wrote nothing

            client.get("/api/repair-lost-departure",
                       params={"drive_id": did, "true_distance_km": 27.2,
                               "true_duration_min": 66, "apply": "true"})
            with SessionLocal() as s:
                d = s.get(Drive, did)
                assert d.start_odo_km == 29318.155
                assert d.distance_km == 27.3
                assert d.start_location == "Home"
                assert d.start_coords == "5.4100, 100.3000"
                # No longer lost, and recorded as the unseen ground it is.
                assert d.start_lost_km == 0.0
                assert d.start_recovered_km == 10.092
                assert d.duration_min == 66.0
                assert d.avg_speed_kmh == pytest.approx(24.8, abs=0.1)

            # Run twice: the gap is gone, so there is nothing left to give back.
            again = client.get("/api/repair-lost-departure",
                               params={"drive_id": did, "true_distance_km": 27.2})
            assert again.status_code == 409
            assert "Nothing was lost" in again.json()["detail"]
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repair_trip_energy_replaces_a_projection_not_a_measurement():
    """A blackout departure brings its distance back but not always its energy:
    past STALE_ANCHOR_MAX_MIN the far SoC carries the park's standby drain, so
    the blind stretch is priced from the trip's own rate instead. That is an
    extrapolation — trip 359's blind head cost 1.10x the rest of its drive,
    trip 366's 0.91x — and where the car measured the whole drive its figure
    wins.

    Scoped to that fault: a trip whose energy was measured end to end is
    refused, because there a disagreement means something else and burying it
    would destroy the evidence."""
    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                a, b = s.query(Drive).order_by(Drive.id).limit(2).all()
                # Trip 366's real shape: 11.3 km with 4.791 km recovered.
                b.distance_km, b.energy_used_kwh = 11.3, 1.66
                b.start_recovered_km = 4.791
                a.start_recovered_km = 0.0          # measured end to end
                s.commit()
                did, measured_id = b.id, a.id

            # A trip with nothing projected is refused outright.
            refused = client.get("/api/repair-trip-energy",
                                 params={"drive_id": measured_id, "true_wh_per_km": 150})
            assert refused.status_code == 409
            assert "direct measurement" in refused.json()["detail"]

            # Exactly one figure, and it has to be a possible one.
            assert client.get("/api/repair-trip-energy", params={"drive_id": did}).status_code == 409
            assert client.get("/api/repair-trip-energy", params={
                "drive_id": did, "true_wh_per_km": 150, "true_consumed_pct": 2.1,
            }).status_code == 409
            silly = client.get("/api/repair-trip-energy",
                               params={"drive_id": did, "true_wh_per_km": 1500})
            assert silly.status_code == 409
            assert "plausible" in silly.json()["detail"]

            # The car's Current Drive readout: 131.5 Wh/km over 11.3 km.
            r = client.get("/api/repair-trip-energy",
                           params={"drive_id": did, "true_wh_per_km": 131.5})
            body = r.json()
            assert r.status_code == 200
            assert body["applied"] is False
            assert body["energy_kwh"] == [1.66, 1.49]
            with SessionLocal() as s:
                assert s.get(Drive, did).energy_used_kwh == 1.66   # dry run wrote nothing

            client.get("/api/repair-trip-energy",
                       params={"drive_id": did, "true_wh_per_km": 131.5, "apply": "true"})
            with SessionLocal() as s:
                d = s.get(Drive, did)
                assert d.energy_used_kwh == 1.49
                assert d.wh_per_km == pytest.approx(131.5, abs=0.6)   # derived, follows

            # The percentage route goes through the capacity constant instead
            # of the distance, so it scales with the percentage given rather
            # than landing on the Wh/km answer (the two only coincide when the
            # capacity in use is the one the car's own percentage implies).
            one = client.get("/api/repair-trip-energy",
                             params={"drive_id": did, "true_consumed_pct": 2.1})
            two = client.get("/api/repair-trip-energy",
                             params={"drive_id": did, "true_consumed_pct": 4.2})
            assert one.status_code == two.status_code == 200
            assert two.json()["energy_kwh"][1] == pytest.approx(
                2 * one.json()["energy_kwh"][1], abs=0.02)
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_sync_log_names_the_cause_of_a_gap_including_no_tick_at_all():
    """A gap in the record has several causes that look identical afterwards.
    Trip 368 lost 12.4 hours containing two real drives, and nothing anywhere
    said whether the loop was skipping on the sleep back-off, finding the car
    unreadable, or simply not being called.

    Each cause writes something — except the one that can't: a stretch with no
    entry at all means the request never arrived, which is what makes absence
    the diagnostic rather than a hole in it."""
    import time as _time
    from datetime import datetime

    from app import state
    from app.api import routes

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            empty = client.get("/api/sync-log").json()
            assert empty["runs"] == []
            assert "starts filling" in empty["note"]

            # Hand-build a history: a long asleep stretch, a silence where
            # nothing ran at all, then reads resuming.
            now = _time.time()
            with SessionLocal() as s:
                state.put(s, state.SYNC_LOG_KEY, _json.dumps([
                    {"o": "asleep", "n": 300, "a": now - 60000, "b": now - 45000},
                    {"o": "read", "n": 4, "a": now - 600, "b": now - 300},
                    {"o": "backoff", "n": 20, "a": now - 240, "b": now - 60},
                ]))
                s.commit()

            body = client.get("/api/sync-log").json()
            kinds = [r["outcome"] for r in body["runs"]]
            # The silence is inserted between the runs that bracket it, and is
            # not something any tick wrote.
            assert kinds == ["asleep", "no-tick", "read", "backoff"]
            hole = body["runs"][1]
            assert hole["minutes"] == pytest.approx(45000 / 60 - 600 / 60, abs=1)
            assert "not called at all" in hole["note"]
            assert body["silences"] >= 1
            # Timestamps are MYT like every other one this app shows, not the
            # server's zone — eight hours out on the deployed host otherwise.
            from app.sync import MYT
            assert body["runs"][0]["from"] == datetime.fromtimestamp(
                now - 60000, MYT).isoformat(timespec="minutes")
            # Runs the ticks did write carry their own counts.
            assert body["runs"][0]["ticks"] == 300
            assert body["runs"][3]["outcome"] == "backoff"
            # A silence running up to NOW is the fault still happening, and was
            # invisible while gaps were only measured between two runs.
            with SessionLocal() as s:
                state.put(s, state.SYNC_LOG_KEY, _json.dumps([
                    {"o": "read", "n": 2, "a": now - 9000, "b": now - 7200},
                ]))
                s.commit()
            live = client.get("/api/sync-log").json()
            assert live["runs"][-1]["outcome"] == "no-tick"
            assert live["runs"][-1]["ongoing"] is True
            assert live["runs"][-1]["minutes"] == pytest.approx(120, abs=1)

            # Consecutive ticks doing the same thing coalesce rather than
            # growing the log a row a minute; a different one starts a run.
            with SessionLocal() as s:
                state.put(s, state.SYNC_LOG_KEY, "")
                s.commit()
                routes._log_tick(s, "asleep")
                routes._log_tick(s, "asleep")
                routes._log_tick(s, "read")
                s.commit()
            runs = client.get("/api/sync-log").json()["runs"]
            assert [(r["outcome"], r["ticks"]) for r in runs] == [("asleep", 2), ("read", 1)]
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repair_split_trip_cuts_one_row_into_the_two_journeys_it_was():
    """A departure recovery reaching across a long blind gap can swallow a
    whole separate drive, the stop after it, and the start of the next one.
    Trip 368: Office->Home, a stop, and half of Home->Penang Retirement Resort
    logged as one 15.665 km trip. No other repair helps — the boundary tools
    trade distance BETWEEN two trips, and here there is one where two belong.

    The odometer boundary is the measured fact; the times are not, because the
    gap is blind precisely because nothing was recorded in it."""
    from datetime import datetime

    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                d = s.query(Drive).order_by(Drive.id).first()
                d.start_time = datetime.fromisoformat("2026-08-10T19:33")
                d.end_time = datetime.fromisoformat("2026-08-10T20:07")
                d.start_odo_km, d.end_odo_km = 29432.359, 29448.024
                d.distance_km, d.energy_used_kwh = 15.7, 0.88
                d.duration_min, d.avg_speed_kmh = 34.0, 28.0
                d.start_recovered_km, d.start_lost_km = 9.448, 0.0
                d.max_speed_kmh, d.end_gap_sec = 63.0, 118.3
                d.start_location, d.end_location = "Office", "Penang Retirement Resort"
                s.commit()
                did = d.id
            # The stop's coordinates are named by the same geofence everything
            # else uses, so the split's middle reads "Home" and not a lat/lon.
            client.post("/api/places", json={
                "name": "Home", "lat": 5.3428, "lon": 100.3106, "radius_km": 0.3})

            # The boundary has to fall inside the trip.
            for bad in (29432.0, 29450.0):
                r = client.get("/api/repair-split-trip",
                               params={"drive_id": did, "boundary_odo_km": bad})
                assert r.status_code == 409
                assert "must fall inside" in r.json()["detail"]

            # Times out of order are refused rather than silently reordered.
            assert client.get("/api/repair-split-trip", params={
                "drive_id": did, "boundary_odo_km": 29436.724,
                "first_start": "2026-08-10T16:40", "first_end": "2026-08-10T16:20",
            }).status_code == 409

            params = {
                "drive_id": did, "boundary_odo_km": 29436.724,
                "first_start": "2026-08-10T16:40", "first_end": "2026-08-10T16:55",
                "second_start": "2026-08-10T19:43",
                "boundary_coords": "5.3428, 100.3106",
            }
            body = client.get("/api/repair-split-trip", params=params).json()
            assert body["applied"] is False
            one, two = body["legs"]
            assert one["distance_km"] == 4.4 and two["distance_km"] == 11.3
            # All 9.448 km of blind distance sat at the front: the first leg is
            # entirely unwatched, the rest lands on the second leg's departure.
            assert one["blind_km"] == 4.365
            assert two["blind_km"] == round(9.448 - 4.365, 3)
            # Nothing was measured on leg one, so it gets no energy rather than
            # a share of a figure that never covered it.
            assert one["energy_kwh"] is None
            assert two["energy_kwh"] > 0.88, "re-priced over its own blind head"

            with SessionLocal() as s:
                assert s.get(Drive, did).distance_km == 15.7   # dry run wrote nothing

            applied = client.get("/api/repair-split-trip",
                                 params={**params, "apply": "true"}).json()
            new_id = applied["new_drive_id"]
            with SessionLocal() as s:
                a, b = s.get(Drive, did), s.get(Drive, new_id)
                # One odometer, shared — the thing every boundary check needs.
                assert a.end_odo_km == b.start_odo_km == 29436.724
                assert a.start_odo_km == 29432.359 and b.end_odo_km == 29448.024
                assert (a.distance_km, b.distance_km) == (4.4, 11.3)
                assert a.energy_used_kwh == 0.0        # unknown, shows as a dash
                assert b.energy_used_kwh > 0.88
                assert a.end_location == b.start_location == "Home"
                assert a.start_location == "Office"
                assert b.end_location == "Penang Retirement Resort"
                assert a.start_time.isoformat(timespec="minutes") == "2026-08-10T16:40"
                assert b.end_time.isoformat(timespec="minutes") == "2026-08-10T20:07"
                # A leg that was never watched must not report a SoC drop that
                # would read as its consumption.
                assert a.start_soc == a.end_soc
                # Nor instrumentation belonging to the other leg: the arrival
                # window was the OLD end's (now leg two's), and the peak speed
                # was measured on leg two, not here.
                assert a.end_gap_sec is None
                assert a.max_speed_kmh == a.avg_speed_kmh
                assert b.end_gap_sec == 118.3        # leg two keeps its own
                assert b.max_speed_kmh == 63.0
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_a_crashed_tick_records_why_instead_of_looking_like_a_dead_cron(monkeypatch):
    """A tick that CRASHED and a tick that never happened leave the same
    absence, because the recorder never runs on the failing path — and those
    two want opposite fixes. Learned the hard way: a run of 500s from the app
    read exactly like a stopped cron, and the diagnosis went to the wrong
    place for two rounds.

    The reason is kept, not just the fact: "a tick failed" is barely more use
    than the silence it replaces, and it has to be readable without host logs.
    """
    from fastapi import HTTPException

    from app import services, state
    from app.api import routes

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        monkeypatch.setattr("app.tesla_client.TeslaClient", _FakeClient)
        with SessionLocal() as s:
            services.link_with_token(s, "tok")
            state.put(s, state.SYNC_LOG_KEY, "")
            s.commit()

        def _boom(*a, **kw):
            raise RuntimeError("column start_park_min does not exist")

        monkeypatch.setattr(routes, "_sync_now_impl", _boom)
        with TestClient(app, raise_server_exceptions=False) as client:
            failed = client.post("/api/sync")
            assert failed.status_code == 500
            # The reason travels in the RESPONSE too. A bare 500 with no body
            # is all the dashboard could show for four rounds while the real
            # error sat in host logs unreachable from a phone.
            assert "start_park_min does not exist" in failed.json()["detail"]
            body = client.get("/api/sync-log").json()

        assert [r["outcome"] for r in body["runs"]] == ["error"]
        assert body["errors"] == 1
        assert body["silences"] == 0, "a crash is not a silence"
        assert "start_park_min does not exist" in body["last_error"]
        assert "RuntimeError" in body["runs"][0]["error"]

        # A HANDLED failure is recorded too. A tick that returned 401 because
        # the token expired achieved as little as one that crashed, and left
        # the same absence — logging only crashes would make the tidiest
        # failures the least visible.
        with SessionLocal() as s:
            state.put(s, state.SYNC_LOG_KEY, "")
            s.commit()

        def _refused(*a, **kw):
            raise HTTPException(401, "Tesla error: token expired")

        monkeypatch.setattr(routes, "_sync_now_impl", _refused)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.post("/api/sync").status_code == 401
            handled = client.get("/api/sync-log").json()
        assert handled["errors"] == 1
        assert "HTTP 401" in handled["last_error"]
        assert "token expired" in handled["last_error"]
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_tick_log_stays_inside_the_column_it_is_stored_in():
    """The bug that took polling down for thirteen hours, and the worst kind:
    the instrument added to explain a gap in polling was what caused one.

    The log capped its RUN COUNT at 120, which at ~75 characters a run is a
    9 KB value against what was then a VARCHAR(2048) column — so once enough
    history built up, every write failed with StringDataRightTruncation.
    _log_tick runs on the back-off path too, so that took the whole of
    /api/sync with it.

    Bounded by serialised size now, and unable to propagate a write failure at
    all: observability must not break the thing it observes."""
    from app import state
    from app.api import routes
    from app.models import Setting

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with SessionLocal() as s:
            state.put(s, state.SYNC_LOG_KEY, "")
            s.commit()
            # Far more history than the column could ever hold, alternating so
            # nothing coalesces into one run.
            for i in range(4000):
                routes._log_tick(s, "read" if i % 2 else "asleep")
            s.commit()

            stored = s.get(Setting, state.SYNC_LOG_KEY).value
            # The column is unbounded now, so the budget is the app's own
            # choice about how much history to carry — but it must still be
            # enforced, or nothing stops the value growing without limit.
            assert getattr(Setting.__table__.c.value.type, "length", None) is None
            assert len(stored) <= routes.SYNC_LOG_MAX_CHARS

            # It kept the NEWEST history, which is what a diagnosis needs.
            runs = _json.loads(stored)
            assert len(runs) > 5, "should still hold useful history"
            assert runs[-1]["o"] == "read"
            # Whole seconds — sub-second precision says nothing about a poll
            # loop and costs 15 characters a run.
            assert all(isinstance(r["a"], int) for r in runs)

            # A long error message can't blow the budget on its own either.
            routes._log_tick(s, "error", detail="X" * 5000)
            s.commit()
            assert len(s.get(Setting, state.SYNC_LOG_KEY).value) <= routes.SYNC_LOG_MAX_CHARS
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repair_missing_trip_fills_a_hole_from_its_own_edges():
    """Every other repair edits a trip that exists. This one fills a hole — a
    stretch where the odometer moved between two logged trips and nothing was
    written for it. Measured: a 4.15 km Home->Bayan Mutiara leg that left no
    trip, and not even a start_lost_km on its successor.

    Only the times are required, because the hole already implies everything
    else: its edges are the neighbours' own boundary, which is also the only
    way the result leaves the odometer continuous."""
    from datetime import datetime, timedelta

    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                a, b = s.query(Drive).order_by(Drive.id).limit(2).all()
                a.start_time = datetime.fromisoformat("2026-08-11T16:15")
                a.end_time = datetime.fromisoformat("2026-08-11T16:27")
                a.start_odo_km, a.end_odo_km = 29502.106, 29508.700
                a.end_location, a.end_area = "Home", "Home"
                a.end_coords, a.end_soc, a.tag = "5.3430, 100.3111", 47.0, "work"
                b.start_time = datetime.fromisoformat("2026-08-11T19:25")
                b.end_time = datetime.fromisoformat("2026-08-11T20:12")
                b.start_odo_km, b.end_odo_km = 29512.856, 29529.812
                b.start_location, b.start_area = "Bayan Mutiara 7", "Bayan Mutiara 7"
                b.start_coords = "5.3519, 100.3112"
                s.commit()
                prev_id, next_id = a.id, b.id
                # Nothing else may sit between them, or the neighbour lookup
                # finds the wrong edges.
                for extra in s.query(Drive).filter(
                        Drive.start_time > a.end_time, Drive.start_time < b.start_time).all():
                    s.delete(extra)
                s.commit()

            params = {"start_time": "2026-08-11T18:30", "end_time": "2026-08-11T18:45"}
            body = client.get("/api/repair-missing-trip", params=params).json()
            assert body["applied"] is False
            assert body["between"] == {"after": prev_id, "before": next_id}
            # Every field derived from the hole's own edges.
            assert body["hole_km"] == 4.156
            assert body["odo"] == [29508.7, 29512.856]
            assert body["route"] == ["Home", "Bayan Mutiara 7"]
            assert body["distance_km"] == 4.2
            assert body["duration_min"] == 15.0
            assert body["leaves_gap_km"] == 0.0
            assert body["energy_kwh"] is None      # never measured, never invented

            # Times that collide with a real trip are refused: an overlap is the
            # one thing no later repair can undo.
            clash = client.get("/api/repair-missing-trip", params={
                "start_time": "2026-08-11T19:30", "end_time": "2026-08-11T19:40"})
            assert clash.status_code == 409
            assert "overlap" in clash.json()["detail"]

            with SessionLocal() as s:
                before = s.query(Drive).count()
            client.get("/api/repair-missing-trip", params={**params, "apply": "true"})
            with SessionLocal() as s:
                assert s.query(Drive).count() == before + 1
                d = s.query(Drive).order_by(Drive.id.desc()).first()
                # The whole point: one continuous odometer across all three.
                assert s.get(Drive, prev_id).end_odo_km == d.start_odo_km == 29508.7
                assert d.end_odo_km == s.get(Drive, next_id).start_odo_km == 29512.856
                assert d.start_location == "Home" and d.end_location == "Bayan Mutiara 7"
                assert d.start_coords == "5.3430, 100.3111"
                assert d.energy_used_kwh == 0.0
                # An unwatched leg must not report a SoC drop that reads as its
                # own consumption.
                assert d.start_soc == d.end_soc == 47.0
                assert d.idle_tracked is False

            # Run twice and the hole is gone, so there is nothing left to fill.
            again = client.get("/api/repair-missing-trip", params=params)
            assert again.status_code == 409
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_trip_gaps_finds_every_boundary_the_odometer_disagrees_about():
    """The odometer only counts forward, so one trip's end and the next one's
    start must be the same reading. Every fault this project spent weeks on
    shows up here as a number — and every one of them was found by scrolling
    the trip list days later, because nothing checked.

    The sizes alone can't say which repair applies, so parked_min carries the
    hint: minutes between the trips means a departure the poll missed, hours
    means a drive nobody logged."""
    from datetime import datetime, timedelta

    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for d in s.query(Drive).all():
                    s.delete(d)
                s.flush()
                veh = s.query(Vehicle).first()
                base = datetime.fromisoformat("2026-08-11T08:00")

                def add(i, s_odo, e_odo, start, mins, lost=0.0):
                    s.add(Drive(vehicle_id=veh.id, start_time=start,
                                end_time=start + timedelta(minutes=mins),
                                distance_km=round(e_odo - s_odo, 1), duration_min=mins,
                                start_soc=60, end_soc=59, energy_used_kwh=1.0,
                                avg_speed_kmh=30, max_speed_kmh=30, outside_temp_c=30,
                                start_location=f"A{i}", end_location=f"B{i}",
                                start_odo_km=s_odo, end_odo_km=e_odo, start_lost_km=lost))

                add(1, 1000.0, 1010.0, base, 20)
                # A hole with minutes between: a departure the poll missed, and
                # the later trip even measured it.
                add(2, 1010.09, 1020.0, base + timedelta(minutes=25), 20, lost=0.09)
                # A hole with hours between: a whole drive nobody logged.
                add(3, 1024.15, 1040.0, base + timedelta(hours=4), 30)
                # An overlap: two trips claiming the same ground.
                add(4, 1039.5, 1050.0, base + timedelta(hours=6), 30)
                s.commit()

            body = client.get("/api/trip-gaps").json()
            assert body["trips_checked"] == 4
            assert body["boundaries_checked"] == 3 and body["boundaries_unchecked"] == 0
            assert body["holes"] == 2 and body["overlaps"] == 1
            assert body["unaccounted_km"] == pytest.approx(0.09 + 4.15, abs=0.002)
            assert body["double_counted_km"] == pytest.approx(0.5, abs=0.002)

            # Biggest first — the one worth fixing is rarely the most recent.
            big = body["findings"][0]
            assert big["gap_km"] == pytest.approx(4.15, abs=0.002)
            assert big["parked_min"] >= 45
            assert "never logged" in big["suggested"]

            small = next(f for f in body["findings"] if f["gap_km"] == pytest.approx(0.09))
            assert small["start_lost_km"] == 0.09
            assert "didn't reclaim it" in small["suggested"]

            over = next(f for f in body["findings"] if f["gap_km"] < 0)
            assert "overlap" in over["suggested"]

            # A clean dataset says so rather than returning an empty list.
            with SessionLocal() as s:
                for d in s.query(Drive).all():
                    if d.start_odo_km != 1000.0:
                        s.delete(d)
                s.commit()
            clean = client.get("/api/trip-gaps").json()
            assert clean["findings"] == []
            assert clean["note"] == "Every trip boundary agrees."
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_repair_all_reclaims_only_what_the_trips_measured_themselves():
    """One pass over the whole history, replacing the trip-at-a-time repairs
    that have been run by hand off the car's screen.

    Its warrant is narrow on purpose. ``start_lost_km`` is the sync's own
    record of ground it watched a trip cover before its anchor and then
    declined to claim; where that matches the hole in front of the trip,
    nothing is inferred and the reclaim just applies a decision the data
    already holds. A drive nobody logged leaves a hole with NO such record,
    and an overlap is a question about ownership — both are handed back.
    """
    from datetime import datetime, timedelta

    from app.models import Drive

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for d in s.query(Drive).all():
                    s.delete(d)
                s.flush()
                veh = s.query(Vehicle).first()
                base = datetime.fromisoformat("2026-08-11T08:00")

                def add(i, s_odo, e_odo, start, mins, lost=0.0):
                    s.add(Drive(vehicle_id=veh.id, start_time=start,
                                end_time=start + timedelta(minutes=mins),
                                distance_km=round(e_odo - s_odo, 1), duration_min=mins,
                                start_soc=60, end_soc=59, energy_used_kwh=1.0,
                                avg_speed_kmh=30, max_speed_kmh=30, outside_temp_c=30,
                                start_location=f"A{i}", end_location=f"B{i}",
                                start_coords="5.34,100.31", end_coords="5.41,100.29",
                                start_odo_km=s_odo, end_odo_km=e_odo, start_lost_km=lost))

                add(1, 1000.0, 1010.0, base, 20)
                # Trip 382's shape: the trip measured its own lost head.
                add(2, 1013.366, 1030.0, base + timedelta(minutes=25), 20, lost=3.366)
                # A drive nobody logged — a hole with nothing claiming it.
                add(3, 1034.15, 1050.0, base + timedelta(hours=4), 30)
                # An overlap: two trips claiming one stretch.
                add(4, 1049.5, 1060.0, base + timedelta(hours=6), 30)
                s.commit()

            dry = client.get("/api/repair-all").json()
            assert dry["repaired"] == 1 and dry["needs_a_human"] == 2
            # Coverage is reported, not assumed: three boundaries between four
            # trips, all with odometers at both ends, so none went unexamined.
            assert dry["boundaries_checked"] == 3
            assert dry["boundaries_unchecked"] == 0
            assert dry["reclaimed_km"] == pytest.approx(3.366, abs=0.002)
            assert dry["applied"] is False
            (fix,) = dry["repairs"]
            assert fix["start_odo_km"] == [1013.366, 1010.0]
            assert fix["distance_km"] == [16.6, 20.0]
            assert fix["start_location"] == ["A2", "B1"]

            # A dry run writes nothing.
            with SessionLocal() as s:
                assert s.query(Drive).filter(Drive.start_odo_km == 1013.366).count() == 1

            # The two it declines carry the exact command to run by hand.
            unlogged = next(m for m in dry["manual"] if m["gap_km"] > 0)
            assert "never logged" in unlogged["why"]
            assert "repair-missing-trip" in unlogged["run"]
            over = next(m for m in dry["manual"] if m["gap_km"] < 0)
            assert "overlap" in over["why"]

            done = client.get("/api/repair-all?apply=true").json()
            assert done["applied"] is True and done["repaired"] == 1
            with SessionLocal() as s:
                d = s.query(Drive).filter(Drive.start_location == "B1").one()
                assert d.start_odo_km == 1010.0        # back to trip 1's end
                assert d.distance_km == 20.0
                assert d.start_lost_km == 0.0
                assert d.start_recovered_km == pytest.approx(3.366, abs=0.002)
                assert d.start_coords == "5.41,100.29"  # origin moved with it
                # Energy grew with the ground, priced flat across the head.
                assert d.energy_used_kwh > 1.0

            # Idempotent: the boundary now agrees, so a second pass is a no-op.
            again = client.get("/api/repair-all?apply=true").json()
            assert again["repaired"] == 0
            assert again["needs_a_human"] == 2

            # A boundary with no odometer is counted as UNCHECKED, never as
            # agreeing — "nothing to reclaim" over trips nobody could measure
            # is the reassurance this whole endpoint exists to avoid giving.
            with SessionLocal() as sess:
                for d in sess.query(Drive).all():
                    if d.start_location != "A1":
                        sess.delete(d)
                sess.flush()
                veh2 = sess.query(Vehicle).first()
                sess.add(Drive(vehicle_id=veh2.id,
                               start_time=datetime.fromisoformat("2026-08-11T12:00"),
                               end_time=datetime.fromisoformat("2026-08-11T12:20"),
                               distance_km=5.0, duration_min=20, start_soc=60,
                               end_soc=59, energy_used_kwh=1.0, avg_speed_kmh=15,
                               max_speed_kmh=30, outside_temp_c=30,
                               start_location="A9", end_location="B9",
                               start_odo_km=None, end_odo_km=None))
                sess.commit()
            blind = client.get("/api/repair-all").json()
            assert blind["boundaries_checked"] == 0
            assert blind["boundaries_unchecked"] == 1
            assert "no odometer to check" in blind["note"]
    finally:
        settings.app_passcode = old


def test_trip_gaps_reconciles_spans_it_has_no_anchors_to_check():
    """Measured live: 61 of 131 boundaries had no odometer at one end, because
    start_odo_km/end_odo_km were added to an existing table. "Every trip
    boundary agrees" over that is true and half-meaningless.

    Chaining odometers backwards through the unanchored trips would make the
    boundary check pass by construction — it would assume the very continuity
    the check tests. This is the check that does work: between two trips that
    DO carry readings, the odometer moved a known amount and the trips between
    claim a known total, and those must agree whatever the middle recorded
    about itself.
    """
    from datetime import datetime, timedelta

    from app.models import Drive

    settings = get_settings()
    old_pass = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for d in s.query(Drive).all():
                    s.delete(d)
                s.flush()
                veh = s.query(Vehicle).first()
                base = datetime.fromisoformat("2026-08-11T08:00")

                def add(i, dist, mins, s_odo=None, e_odo=None):
                    s.add(Drive(vehicle_id=veh.id, start_time=base + timedelta(hours=i),
                                end_time=base + timedelta(hours=i, minutes=mins),
                                distance_km=dist, duration_min=mins, start_soc=60,
                                end_soc=59, energy_used_kwh=1.0, avg_speed_kmh=30,
                                max_speed_kmh=30, outside_temp_c=30,
                                start_location=f"A{i}", end_location=f"B{i}",
                                start_odo_km=s_odo, end_odo_km=e_odo))

                # Anchored at both ends, unanchored in the middle. The odometer
                # moved 30.0 km between them; the three trips claim 25.0, so
                # 5 km of real ground is claimed by nothing.
                add(0, 10.0, 20, 1000.0, 1010.0)
                add(1, 8.0, 20)
                add(2, 9.0, 20)
                add(3, 8.0, 20)
                add(4, 10.0, 20, 1040.0, 1050.0)
                s.commit()

            body = client.get("/api/trip-gaps").json()
            # The boundary check is blind here and says so rather than passing.
            assert body["boundaries_checked"] == 0
            assert body["boundaries_unchecked"] == 4
            assert body["holes"] == 0 and body["overlaps"] == 0

            # The reconciliation is not blind.
            assert body["unanchored_blocks"] == 1
            (blk,) = body["blocks"]
            assert blk["trips"] == 3
            assert blk["odometer_moved_km"] == pytest.approx(30.0, abs=0.002)
            assert blk["trips_claim_km"] == pytest.approx(25.0, abs=0.002)
            assert blk["difference_km"] == pytest.approx(5.0, abs=0.002)
            assert blk["reading"] == "distance no trip claims"
            assert "do NOT reconcile" in body["note"]

            # And when the middle does add up, the verdict says the sentence
            # rests on the reconciliation rather than on the missing anchors.
            with SessionLocal() as s:
                d = s.query(Drive).filter(Drive.start_location == "A2").one()
                d.distance_km = 14.0            # 8 + 14 + 8 = 30
                s.commit()
            ok = client.get("/api/trip-gaps").json()
            assert ok["unanchored_blocks"] == 0
            assert "reconcile against the readings either side" in ok["note"]
    finally:
        settings.app_passcode = old_pass


def test_trip_gaps_admits_the_boundaries_nothing_can_reach():
    """The reconciliation needs a reading on BOTH sides of a block. A run of
    unanchored trips at the START of the history has no earlier reading, so no
    pair of anchors brackets it and nothing examines it.

    Measured live and missed: 61 unchecked boundaries with zero failing blocks,
    reported as "the spans they sit in reconcile" — when in fact the spans sat
    before the first anchor and nothing had looked at them at all. Reporting no
    failures across trips no check can see is the same false all-clear as
    counting an unchecked boundary as a clean one, one level further in.
    """
    from datetime import datetime, timedelta

    from app.models import Drive

    settings = get_settings()
    old_pass = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for d in s.query(Drive).all():
                    s.delete(d)
                s.flush()
                veh = s.query(Vehicle).first()
                base = datetime.fromisoformat("2026-08-11T08:00")

                def add(i, dist, s_odo=None, e_odo=None):
                    s.add(Drive(vehicle_id=veh.id, start_time=base + timedelta(hours=i),
                                end_time=base + timedelta(hours=i, minutes=20),
                                distance_km=dist, duration_min=20, start_soc=60,
                                end_soc=59, energy_used_kwh=1.0, avg_speed_kmh=30,
                                max_speed_kmh=30, outside_temp_c=30,
                                start_location=f"A{i}", end_location=f"B{i}",
                                start_odo_km=s_odo, end_odo_km=e_odo))

                # The live shape: unanchored history, then anchored from the
                # migration onward. Nothing precedes the old trips to measure
                # them from.
                add(0, 10.0)
                add(1, 8.0)
                add(2, 9.0, 1000.0, 1010.0)
                add(3, 10.0, 1010.0, 1020.0)
                s.commit()

            body = client.get("/api/trip-gaps").json()
            assert body["holes"] == 0 and body["overlaps"] == 0
            assert body["boundaries_unchecked"] == 2
            # No block FAILED, because no block could be formed over them.
            assert body["unanchored_blocks"] == 0
            assert body["boundaries_unreachable"] == 2
            # And the verdict must not read as an all-clear over them.
            assert "nothing has checked those" in body["note"]
            assert "reconcile against the readings either side" not in body["note"]
            assert "from_odo_km" in body["hint"]
            # The figure to compare a real record against — derived from the
            # trips in question, so useless as proof and useful as a target.
            assert body["implied_start_odo_km"] == pytest.approx(982.0, abs=0.05)
            assert "982" in body["hint"]

            # One reading from outside the app closes the whole leading run.
            # The two old trips claim 18.0 km and the first anchor sits at
            # 1000.0, so an opening odometer of 982.0 reconciles exactly.
            good = client.get("/api/trip-gaps?from_odo_km=982.0").json()
            assert good["boundaries_unreachable"] == 0
            assert good["unanchored_blocks"] == 0
            assert good["hint"] is None
            assert "nothing has checked those" not in good["note"]

            # A wrong one is caught rather than absorbed: 5 km of the old
            # trips' ground would belong to no trip at all.
            bad = client.get("/api/trip-gaps?from_odo_km=977.0").json()
            assert bad["boundaries_unreachable"] == 0
            assert bad["unanchored_blocks"] == 1
            (blk,) = bad["blocks"]
            assert blk["trips"] == 2
            assert blk["odometer_moved_km"] == pytest.approx(23.0, abs=0.002)
            assert blk["trips_claim_km"] == pytest.approx(18.0, abs=0.002)
            assert blk["difference_km"] == pytest.approx(5.0, abs=0.002)
            assert "do NOT reconcile" in bad["note"]

            # A dated reading from PART WAY through the unanchored run works
            # too, and only the trips after it are counted against the span.
            # This is the realistic case: the readings people actually have are
            # photos of the dash, taken whenever, not at the first trip.
            # Trip A1 ran 10 km from base, so a reading of 992.0 taken just
            # before trip A1 leaves 8.0 km claimed against an 8.0 km span.
            mid = client.get(
                "/api/trip-gaps?from_odo_km=992.0"
                "&from_time=2026-08-11T08:30").json()
            assert mid["unanchored_blocks"] == 0
            assert mid["boundaries_unreachable"] == 1     # the pre-cutoff one
            assert mid["oldest_trip_at"] == "2026-08-11T08:00"
            # The pair bounds the search: a usable reading is dated between
            # these two, and anything after the second proves nothing new.
            assert mid["anchors_begin_at"] == "2026-08-11T10:00"

            # A malformed timestamp is refused rather than silently ignored,
            # which would reconcile against the wrong set of trips.
            assert client.get("/api/trip-gaps?from_odo_km=992.0"
                              "&from_time=july+5").status_code == 400
    finally:
        settings.app_passcode = old_pass


def test_place_lookup_never_caches_its_own_failure():
    """_place_and_area falls back to the coordinate string when the geocoders
    miss, which keeps the trip logged. Caching that fallback made a single
    timeout permanent — the spot was named after its latitude forever, and
    _cached_place_near handed the same failure to anything within GPS drift.

    Measured live: two consecutive trips on 13 Aug both reading
    "5.3354, 100.2974" for one physical stop, once as an arrival and once as
    the next departure.
    """
    import app.api.routes as r

    coords = "5.3354, 100.2974"
    r._PLACE_CACHE.pop(coords, None)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("nominatim down")

    old_get, old_key = r.httpx.get, get_settings().google_maps_api_key
    get_settings().google_maps_api_key = ""
    r.httpx.get = boom
    try:
        assert r._place_and_area(coords) == (coords, coords)
        # The retry is the point: a second visit must try again, not be
        # served the earlier failure out of the cache.
        assert r._place_and_area(coords) == (coords, coords)
        assert len(calls) == 2
        assert coords not in r._PLACE_CACHE
    finally:
        r.httpx.get, get_settings().google_maps_api_key = old_get, old_key
        r._PLACE_CACHE.pop(coords, None)


def test_backfill_place_names_renames_rows_left_holding_coordinates():
    """Rows already written keep whatever the failed lookup gave them. They
    are recognisable without guessing — the label is exactly the coordinate
    string it came from, which no successful geocode returns."""
    from datetime import datetime, timedelta

    from app.models import Drive

    settings = get_settings()
    old_pass = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for d in s.query(Drive).all():
                    s.delete(d)
                s.flush()
                veh = s.query(Vehicle).first()
                base = datetime.fromisoformat("2026-08-13T21:22")
                coords = "5.3354, 100.2974"
                # One stop, two rows: an arrival then the next departure —
                # exactly how a single failure surfaces twice.
                s.add(Drive(vehicle_id=veh.id, start_time=base,
                            end_time=base + timedelta(minutes=15),
                            distance_km=11.9, duration_min=15, start_soc=60,
                            end_soc=58, energy_used_kwh=1.44, avg_speed_kmh=49,
                            max_speed_kmh=92, outside_temp_c=27,
                            start_location="5.2819, 100.2787",
                            start_coords="5.2819, 100.2787",
                            end_location=coords, end_coords=coords,
                            start_odo_km=1000.0, end_odo_km=1011.9))
                s.add(Drive(vehicle_id=veh.id, start_time=base + timedelta(hours=1),
                            end_time=base + timedelta(hours=1, minutes=6),
                            distance_km=3.9, duration_min=6, start_soc=58,
                            end_soc=57, energy_used_kwh=0.52, avg_speed_kmh=40,
                            max_speed_kmh=93, outside_temp_c=27,
                            start_location=coords, start_coords=coords,
                            end_location="Home", end_coords="5.3431, 100.3111",
                            start_odo_km=1011.9, end_odo_km=1015.8))
                s.commit()

            import app.api.routes as r
            named = {"5.3354, 100.2974": ("Bak Kut Teh", "Bayan Baru")}
            old = r._place_and_area
            r._place_and_area = lambda c, sess=None: named.get(c, (c, c))
            try:
                dry = client.get("/api/backfill-place-names").json()
                # Two distinct unnamed coordinates, one of which resolves.
                assert dry["unnamed_coords"] == 2
                assert dry["resolved"] == 1 and dry["still_unnamed"] == 1
                assert len(dry["changes"]) == 2      # both rows share the stop
                assert dry["applied"] is False

                done = client.get("/api/backfill-place-names?apply=true").json()
                assert done["applied"] is True
            finally:
                r._place_and_area = old

            with SessionLocal() as s:
                rows = s.query(Drive).order_by(Drive.start_time).all()
                assert rows[0].end_location == "Bak Kut Teh"
                assert rows[0].end_area == "Bayan Baru"
                assert rows[1].start_location == "Bak Kut Teh"
                # The one the geocoder still can't name is left as it was,
                # not blanked — coordinates remain searchable in a maps app.
                assert rows[0].start_location == "5.2819, 100.2787"
    finally:
        settings.app_passcode = old_pass


def test_energy_reconcile_separates_what_is_accounted_for_from_what_is_not():
    """The car reports this two ways and they disagree: its battery meter read
    38% while its energy screen's categories summed to 36.7%. Both are right —
    one is a raw SoC delta, the other is attributed consumption — and the gap
    is energy that left without being filed anywhere.

    This app has the same two quantities and never compared them. Trips plus
    vampire drain is the attributed side; the SoC the last charge ended on
    minus the SoC now is the raw side. The residual catches a whole class of
    fault at once without knowing in advance which one happened.
    """
    from datetime import datetime, timedelta

    from app import state
    from app.models import Charge, Drive

    settings = get_settings()
    old_pass = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for row in s.query(Drive).all():
                    s.delete(row)
                for row in s.query(Charge).all():
                    s.delete(row)
                s.flush()
                veh = s.query(Vehicle).first()
                base = datetime.fromisoformat("2026-08-14T08:00")
                s.add(Charge(vehicle_id=veh.id, start_time=base - timedelta(hours=2),
                             end_time=base, duration_min=120, start_soc=40,
                             end_soc=80, energy_added_kwh=28.0, cost=15.0,
                             location="Home", outside_temp_c=30))
                # Two trips, 5 kWh between them, and a parked gap after each.
                for i, kwh in enumerate((3.0, 2.0)):
                    st = base + timedelta(hours=3 + 6 * i)
                    s.add(Drive(vehicle_id=veh.id, start_time=st,
                                end_time=st + timedelta(minutes=30),
                                distance_km=20.0, duration_min=30,
                                start_soc=80 - i * 5, end_soc=76 - i * 5,
                                energy_used_kwh=kwh, avg_speed_kmh=40,
                                max_speed_kmh=80, outside_temp_c=30,
                                start_location="A", end_location="B",
                                start_odo_km=1000.0 + 20 * i,
                                end_odo_km=1020.0 + 20 * i))
                s.commit()
                vin = veh.vin
            # The live reading is the raw side and comes from the poll, not the
            # trip rows — which is exactly what makes the comparison meaningful.
            with SessionLocal() as s:
                state.put(s, state.scoped(state.LAST_STATUS_KEY, vin),
                          _json.dumps({"soc": 70.0}))
                s.commit()

            body = client.get("/api/energy-reconcile").json()
            assert body["raw"]["start_soc"] == 80
            assert body["raw"]["now_soc"] == 70.0
            assert body["raw"]["pct"] == pytest.approx(10.0, abs=0.01)
            assert body["attributed"]["trips"] == pytest.approx(5.0, abs=0.01)
            assert body["attributed"]["trips_count"] == 2
            # 10% of the pack against 5 kWh of trips plus whatever the parked
            # gaps came to — the rest is the residual, and it is signed.
            cap = body["capacity_kwh"]
            expected = round(10.0 - (5.0 + body["attributed"]["parked"]) / cap * 100, 2)
            assert body["residual"]["pct"] == pytest.approx(expected, abs=0.02)
            assert "further than anything here accounts for" in body["residual"]["reading"]

            # Claiming MORE than left the pack reads the other way round.
            with SessionLocal() as s:
                state.put(s, state.scoped(state.LAST_STATUS_KEY, vin),
                          _json.dumps({"soc": 79.0}))
                s.commit()
            flipped = client.get("/api/energy-reconcile").json()
            assert flipped["residual"]["pct"] < 0
            assert "claims more energy than left the pack" in flipped["residual"]["reading"]
    finally:
        settings.app_passcode = old_pass


def test_continuity_endpoint_catches_what_boundary_checks_cannot():
    """A trip that closes short passes every boundary check, provided the next
    departure recovery reaches back over the same ground: the odometer stays
    continuous because each metre is claimed exactly once, just by the wrong
    trip. Only the readings taken while the car sat parked expose it.

    The check existed and was computed on every dashboard load, but app.js
    never rendered it, so no one had ever seen the result.
    """
    from datetime import datetime, timedelta

    from app import sync as sync_mod
    from app.models import BatteryReading, Drive

    settings = get_settings()
    old_pass = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for row in s.query(Drive).all():
                    s.delete(row)
                for row in s.query(BatteryReading).all():
                    s.delete(row)
                s.flush()
                veh = s.query(Vehicle).first()
                base = sync_mod.now_local() - timedelta(days=1)

                # Trip A closes at 1010.0; trip B starts there too, so every
                # boundary agrees. But the car was seen resting at 1010.6.
                s.add(Drive(vehicle_id=veh.id, start_time=base,
                            end_time=base + timedelta(minutes=20),
                            distance_km=10.0, duration_min=20, start_soc=60,
                            end_soc=58, energy_used_kwh=1.5, avg_speed_kmh=30,
                            max_speed_kmh=60, outside_temp_c=30,
                            start_location="A", end_location="B",
                            start_odo_km=1000.0, end_odo_km=1010.0))
                s.add(Drive(vehicle_id=veh.id,
                            start_time=base + timedelta(hours=3),
                            end_time=base + timedelta(hours=3, minutes=20),
                            distance_km=10.6, duration_min=20, start_soc=58,
                            end_soc=56, energy_used_kwh=1.6, avg_speed_kmh=32,
                            max_speed_kmh=60, outside_temp_c=30,
                            start_location="B", end_location="C",
                            start_odo_km=1010.0, end_odo_km=1020.6))
                for i in range(3):
                    s.add(BatteryReading(
                        vehicle_id=veh.id,
                        ts=base + timedelta(minutes=40 + 20 * i),
                        soc=58.0, range_km=250.0, odo_km=1010.6))
                s.commit()

            # The boundary check is satisfied: nothing is missing between them.
            gaps = client.get("/api/trip-gaps?days=7").json()
            assert gaps["holes"] == 0 and gaps["overlaps"] == 0

            # The parked readings are not satisfied.
            body = client.get("/api/continuity?days=7").json()
            assert body["available"] is True
            assert body["readings_checked"] == 3
            assert body["unattributed_km"] == pytest.approx(0.6, abs=0.02)
            (gap,) = body["gaps"]
            assert gap["recorded_end_odo_km"] == pytest.approx(1010.0, abs=0.05)
            assert gap["observed_odo_km"] == pytest.approx(1010.6, abs=0.05)
            # Actionable without a second lookup: repair-trip-boundary needs
            # both trip ids and where the boundary belongs.
            with SessionLocal() as sess:
                ids = [d.id for d in sess.query(Drive).order_by(Drive.start_time).all()]
            assert gap["drive_id"] == ids[0]
            assert gap["next_drive_id"] == ids[1]
            assert gap["boundary_odo_km"] == pytest.approx(1010.6, abs=0.02)
            # A parked odometer cannot creep, so the movement happened between
            # these two — which is what makes a large finding judgeable.
            assert gap["observed_at"].startswith(
                (base + timedelta(minutes=40)).isoformat(timespec="minutes")[:13])
            assert "misattribution, not a hole" in body["note"]

            # And with no readings it says it could not look, rather than clean.
            with SessionLocal() as s:
                for row in s.query(BatteryReading).all():
                    s.delete(row)
                s.commit()
            blind = client.get("/api/continuity?days=7").json()
            assert blind["available"] is False
            assert "nothing could be checked" in blind["note"]
    finally:
        settings.app_passcode = old_pass


def test_repair_arrivals_fixes_the_tails_and_refuses_the_journeys():
    """Ten findings is ten hand-built URLs, and the check's whole problem was
    that nobody looked at its output — so making the fix tedious guarantees it
    goes unused again.

    The cap is the code's own ARRIVAL_EST_MAX_KM. Under it a short close is a
    tail the poll missed. Over it the likelier story is a drive nobody logged
    — measured, 1.82 km overnight at Home — and folding that into the arriving
    trip would bury the evidence that it happened.
    """
    from datetime import timedelta

    from app import sync as sync_mod
    from app.models import ArrivalTailSample, BatteryReading, Drive

    settings = get_settings()
    old_pass = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for model in (Drive, BatteryReading, ArrivalTailSample):
                    for row in s.query(model).all():
                        s.delete(row)
                s.flush()
                veh = s.query(Vehicle).first()
                base = sync_mod.now_local() - timedelta(days=2)

                def add(i, s_odo, e_odo, hours, est=None):
                    st = base + timedelta(hours=hours)
                    s.add(Drive(vehicle_id=veh.id, start_time=st,
                                end_time=st + timedelta(minutes=20),
                                distance_km=round(e_odo - s_odo, 1),
                                duration_min=20, start_soc=70 - i, end_soc=68 - i,
                                energy_used_kwh=2.0, avg_speed_kmh=30,
                                max_speed_kmh=60, outside_temp_c=30,
                                start_location=f"P{i}", end_location=f"P{i+1}",
                                start_coords="5.34,100.31", end_coords="5.41,100.29",
                                start_odo_km=s_odo, end_odo_km=e_odo,
                                end_est_km=est))

                # A short tail (0.30, under the cap) and a long one (1.82, over).
                add(0, 1000.0, 1010.0, 0, est=0.15)
                add(1, 1010.0, 1020.0, 6)
                add(2, 1020.0, 1030.0, 12)
                s.flush()
                for ts_h, odo in ((1.0, 1010.30), (7.0, 1021.82)):
                    s.add(BatteryReading(vehicle_id=veh.id,
                                         ts=base + timedelta(hours=ts_h),
                                         soc=68.0, range_km=250.0, odo_km=odo))
                s.commit()

            dry = client.get("/api/repair-arrivals?days=7").json()
            assert dry["repaired"] == 1 and dry["needs_a_human"] == 1
            assert dry["reclaimed_km"] == pytest.approx(0.30, abs=0.02)
            assert dry["applied"] is False
            assert "past ARRIVAL_EST_MAX_KM" in dry["manual"][0]["why"]

            with SessionLocal() as s:
                assert s.query(Drive).filter(Drive.end_odo_km == 1010.0).count() == 1

            done = client.get("/api/repair-arrivals?days=7&apply=true").json()
            assert done["applied"] is True and done["repaired"] == 1
            with SessionLocal() as s:
                rows = s.query(Drive).order_by(Drive.start_time).all()
                assert rows[0].end_odo_km == pytest.approx(1010.30, abs=0.005)
                assert rows[1].start_odo_km == pytest.approx(1010.30, abs=0.005)
                # Distance moved between them; the odometer stays continuous.
                assert rows[0].distance_km == pytest.approx(10.3, abs=0.05)
                assert rows[1].distance_km == pytest.approx(9.7, abs=0.05)
                # The long one is untouched, evidence intact.
                assert rows[1].end_odo_km == pytest.approx(1020.0, abs=0.005)
                # And the measurement is fed back to the model that caused it.
                samples = s.query(ArrivalTailSample).all()
                assert len(samples) == 1
                assert samples[0].measured_km == pytest.approx(0.45, abs=0.02)

            # Idempotent: the boundary now matches the reading.
            again = client.get("/api/repair-arrivals?days=7&apply=true").json()
            assert again["repaired"] == 0
    finally:
        settings.app_passcode = old_pass


def test_capacity_evidence_reports_precision_rather_than_assuming_it():
    """Four screen readings disagreed with the constant the same way each time,
    which is thin ground for changing a number every kWh and every ringgit
    depends on. Every charge is an independent measurement of the same pack
    and none had been looked at.

    The precision is the point. SoC is whole percent, so a session's ratio is
    only as sharp as its swing — a 40-point charge resolves the pack to ~1%, a
    4-point charge to 25% and says nothing. Averaging the small ones in is how
    the parked rate came out ten times over.
    """
    from datetime import timedelta

    from app import sync as sync_mod
    from app.models import Charge

    settings = get_settings()
    old_pass = settings.app_passcode
    settings.app_passcode = ""
    try:
        with TestClient(app) as client:
            with SessionLocal() as s:
                for row in s.query(Charge).all():
                    s.delete(row)
                s.flush()
                veh = s.query(Vehicle).first()
                base = sync_mod.now_local() - timedelta(days=10)

                def charge(i, s_soc, e_soc, kwh, kind="DC"):
                    s.add(Charge(vehicle_id=veh.id,
                                 start_time=base + timedelta(days=i),
                                 end_time=base + timedelta(days=i, hours=2),
                                 duration_min=120, start_soc=s_soc, end_soc=e_soc,
                                 energy_added_kwh=kwh, cost=10.0, location="Home",
                                 charge_type=kind, outside_temp_c=30))

                # Two wide DC sessions that can measure, one narrow one that
                # can't and would drag the median if it were counted.
                charge(0, 30, 80, 34.35)      # 50 pts -> 68.7
                charge(1, 20, 75, 37.79)      # 55 pts -> 68.7
                charge(2, 60, 64, 4.00)       # 4 pts  -> 100.0, precision 25%
                # And one AC session that would read 68.7 uncorrected — a
                # charger reports what went in, not what reached the pack.
                charge(3, 25, 75, 34.35, kind="AC")
                charge(4, 35, 85, 34.35)      # a fourth wide one, so a median
                s.commit()

            body = client.get("/api/capacity-evidence").json()
            assert body["charges_seen"] == 5
            assert body["charges_counted"] == 4          # the 4-point one is out
            # DC 68.7, DC 68.7, AC 68.7*0.95 -> the median stays 68.7 and the
            # AC row sits below it, which is the correction doing its job.
            assert body["median_implied_kwh"] == pytest.approx(68.7, abs=0.05)
            from app.sync import AC_CHARGE_EFFICIENCY
            ac = next(r for r in body["charges"] if r["charge_type"] == "AC")
            # Derived from the constant, not restated: this pins that the
            # correction is APPLIED, and pinning its value here as well would
            # look like a second measurement of it.
            assert ac["implied_capacity_kwh"] == pytest.approx(
                68.7 * AC_CHARGE_EFFICIENCY, abs=0.05)
            assert ac["implied_capacity_kwh"] < 68.7
            # Which correction was applied is reported, because mixing AC and
            # DC rows without knowing which is which makes the figure
            # uninterpretable.
            assert {r["charge_type"] for r in body["charges"]} == {"AC", "DC"}
            # The widest sessions are reported on their own — measured, the
            # precision column predicts the scatter almost exactly.
            assert body["widest_sessions"]["count"] == 4
            assert body["widest_sessions"]["median_kwh"] == pytest.approx(68.7, abs=0.05)

            # Excluded, not hidden — with its precision on show.
            narrow = next(r for r in body["charges"] if r["swing_pct"] == 4.0)
            assert narrow["counts"] is False
            assert narrow["precision_pct"] == pytest.approx(25.0, abs=0.1)
            assert narrow["implied_capacity_kwh"] == pytest.approx(100.0, abs=0.1)

            # The correction is named, not left for the reader to guess at.
            assert "efficiency correction" in body["caveat"]
            # And with four qualifying-ish sessions on record, the capacity in
            # use now comes from the car rather than from the variant spec.
            assert body["in_use"]["kwh"] > 0
            assert "measured from" in body["in_use"]["source"]

            # Lowering the floor lets the unmeasurable 4-point session back
            # in — and the median does not move, which is the reason it is a
            # median. Its own implied figure is 100 kWh; a MEAN over the same
            # five would read about 74 and quietly wreck every kWh downstream.
            loose = client.get("/api/capacity-evidence?min_swing_pct=1").json()
            assert loose["charges_counted"] == 5
            assert loose["median_implied_kwh"] == pytest.approx(68.7, abs=0.05)
            vals = [r["implied_capacity_kwh"] for r in loose["charges"]]
            assert sum(vals) / len(vals) > 73.0
    finally:
        settings.app_passcode = old_pass


def test_repair_arrivals_is_reachable_by_the_cron_key_and_nothing_else_new_is():
    """The arrival correction has to run unattended or it will not run at all —
    the whole finding was that nobody had looked at the check's output. It is
    the only mutating repair on the key's whitelist, so the boundary of what
    that key can reach is worth pinning.
    """
    settings = get_settings()
    old_pass, old_key = settings.app_passcode, settings.sync_key
    settings.app_passcode, settings.sync_key = "1234", "s3cret"
    try:
        with TestClient(app) as client:
            # Reachable with the key, refused without it.
            assert client.get("/api/repair-arrivals?days=7&key=s3cret").status_code == 200
            assert client.get("/api/repair-arrivals?days=7").status_code == 401
            assert client.get("/api/repair-arrivals?days=7&key=wrong").status_code == 401

            # The other repairs stay behind the passcode: this one is exposed
            # because it can only apply measurements already on record, with no
            # figure for a caller to supply.
            for path in ("/api/repair-all", "/api/repair-trip-boundary"
                         "?closed_id=1&open_id=2&boundary_odo_km=1.0"):
                assert client.get(f"{path}&key=s3cret" if "?" in path
                                  else f"{path}?key=s3cret").status_code == 401
    finally:
        settings.app_passcode, settings.sync_key = old_pass, old_key
