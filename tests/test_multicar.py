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
    closed_id, dist_before, _energy, drives, logged = _run_asleep_close(
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


def _run_asleep_close(monkeypatch, client_cls):
    """Drive, fall genuinely asleep (closing the trip), then poll once more."""
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
            return (closed_id, dist_before, energy_before, drives,
                    resp2.json()["logged"]["drives"])
    finally:
        settings.app_passcode = old
        _reset_to_demo()


def test_asleep_close_still_recovers_a_short_arrival_tail(monkeypatch):
    """Regression for trip 314: a trip closed on a genuine "asleep" read 0.4 km
    and a minute short, because sleep proves the car had STOPPED but not that
    the closing reading was taken at the stop — last_snapshot can be a poll
    interval old. A small tail must still fold back in."""
    closed_id, dist_before, _energy, drives, logged = _run_asleep_close(
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
    closed_id, dist_before, _energy, drives, _ = _run_asleep_close(
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
    not."""
    closed_id, dist_before, energy_before, drives, logged = _run_asleep_close(
        monkeypatch, _AsleepThenWakesHoursLaterClient)
    assert logged == 0
    assert len(drives) == 1
    closed = drives[0]
    assert closed.id == closed_id
    assert closed.distance_km == 12.4          # distance: measured, folds in
    assert closed.energy_used_kwh == pytest.approx(energy_before, abs=0.011)


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
    closed_id, dist_before, _e, drives, _logged = _run_asleep_close(
        monkeypatch, _SlowlyArrivesThenDrivesAgainClient)
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
    """Last seen crawling (20 mph) with the close 3 minutes past it, so the
    tail is estimated at 0.805 km — but the car had very nearly arrived and the
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
    closed_id, dist_before, _e, drives, logged = _run_asleep_close(
        monkeypatch, _SlowlyArrivesThenBarelyMovesClient)
    assert dist_before == pytest.approx(12.8), "the estimate must have fired"
    assert logged == 0                       # topped up, not a phantom trip
    assert len(drives) == 1
    closed = drives[0]
    assert closed.id == closed_id
    assert closed.distance_km == pytest.approx(12.1)   # measured, not estimated
    assert closed.end_est_km is None         # nothing left standing on a guess


class _SlowlyArrivesThenParksFurtherOnClient(_AsleepThenParksWithCreepClient):
    """The ordinary correction: the tail was estimated at 0.805 km and the car
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
    what was left over after it — 0.805 km out, 0.095 km back, and the 0.805
    the car genuinely drove credited to nobody. The trip must end up covering every
    metre between its anchor and the reading that could finally see it."""
    closed_id, dist_before, _e, drives, logged = _run_asleep_close(
        monkeypatch, _SlowlyArrivesThenParksFurtherOnClient)
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
