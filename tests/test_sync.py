"""Tests for the snapshot session state machine (app/sync.py)."""
from app.sync import process_snapshot, snapshot_from_vehicle_data

T0 = 1_760_000_000.0  # seconds epoch


def snap(ts, odo_km, soc, shift="P", speed=0.0, charging=False, kw=0.0,
         fast=False, present=False, locked=False, lat=None, lon=None):
    return {
        "ts": ts, "odo_km": odo_km, "soc": soc, "shift": shift,
        "speed_kmh": speed, "charging": charging, "charger_kw": kw,
        "fast": fast, "out_temp": 28.0, "user_present": present,
        "locked": locked, "lat": lat, "lon": lon,
    }


def step(prev, cur, trip=None, charge=None):
    return process_snapshot(prev, cur, trip, charge, 60.0, 0.90)


def test_snapshot_parses_vehicle_data_ms_timestamp_and_miles():
    data = {
        "drive_state": {"timestamp": 1_760_000_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72, "charging_state": "Disconnected"},
        "climate_state": {"outside_temp": 31.5},
        "vehicle_state": {"odometer": 6215.0},
    }
    s = snapshot_from_vehicle_data(data)
    assert s["ts"] == 1_760_000_000.0          # ms -> s
    assert abs(s["odo_km"] - 6215.0 * 1.60934) < 0.01
    assert s["soc"] == 72 and s["out_temp"] == 31.5


def test_trip_opens_spans_snapshots_and_closes_on_park():
    """One drive across four snapshots = exactly one logged entry."""
    s1 = snap(T0, 10_000.0, 80)                               # parked at home
    s2 = snap(T0 + 600, 10_005.0, 78, shift="D", speed=60)    # driving
    s3 = snap(T0 + 1200, 10_015.0, 75, shift="D", speed=90)   # still driving
    s4 = snap(T0 + 1800, 10_024.9, 72)                        # back to P

    d, c, trip, charge = step(None, s1)
    assert (d, c, trip, charge) == ([], [], None, None)

    d, c, trip, charge = step(s1, s2)
    assert d == [] and trip is not None          # trip opened, nothing logged
    assert trip["odo_km"] == 10_000.0            # anchored at the parked snapshot

    d, c, trip, charge = step(s2, s3, trip)
    assert d == [] and trip is not None          # still open
    assert trip["max_speed"] == 90               # max speed tracked

    d, c, trip, charge = step(s3, s4, trip)
    assert trip is None and len(d) == 1          # closed on P
    (drive,) = d
    assert drive["distance_km"] == 24.9          # full span, not fragments
    assert drive["duration_min"] == 30.0
    assert abs(drive["energy_used_kwh"] - 4.8) < 1e-6   # 8% of 60 kWh
    assert drive["max_speed_kmh"] == 90


def test_trip_survives_brief_stop_and_closes_on_power_down():
    """A stop with the driver still inside keeps the trip open; leaving ends it."""
    s1 = snap(T0, 10_000.0, 80, lat=3.10, lon=101.60)
    s2 = snap(T0 + 600, 10_010.0, 77, shift="D", speed=70, present=True)
    s3 = snap(T0 + 1200, 10_010.5, 77, present=True)  # parked, driver inside
    s4 = snap(T0 + 1800, 10_020.0, 74, shift="D", speed=80, present=True)
    s5 = snap(T0 + 2400, 10_025.0, 73, lat=3.15, lon=101.71)  # driver gone

    d, c, trip, charge = step(s1, s2)
    assert trip is not None
    d, c, trip, charge = step(s2, s3, trip)
    assert d == [] and trip is not None          # brief stop does NOT cut the trip
    d, c, trip, charge = step(s3, s4, trip)
    assert d == [] and trip is not None
    d, c, trip, charge = step(s4, s5, trip)
    assert trip is None and len(d) == 1          # closed only on power-down
    (drive,) = d
    assert drive["distance_km"] == 25.0          # the whole errand run, one entry
    assert drive["avg_speed_kmh"] == 37.5        # 25 km over 40 min
    assert drive["start_location"] == "3.1000, 101.6000"
    assert drive["end_location"] == "3.1500, 101.7100"


def test_locked_car_ends_the_trip_even_with_presence_lag():
    """Parked + locked = drive over, even if presence detection still says yes."""
    s1 = snap(T0, 10_000.0, 80)
    s2 = snap(T0 + 600, 10_010.0, 77, shift="D", speed=70, present=True)
    s3 = snap(T0 + 1200, 10_012.0, 76, present=True, locked=True)  # locked up

    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    d, _, trip, _ = step(s2, s3, trip)
    assert trip is None and len(d) == 1
    assert d[0]["distance_km"] == 12.0


def test_snapshot_parses_user_present_and_position():
    data = {
        "drive_state": {"timestamp": 1_760_000_000_000, "shift_state": "D",
                        "speed": 40, "latitude": 3.0733, "longitude": 101.6067},
        "charge_state": {"battery_level": 72},
        "vehicle_state": {"odometer": 6215.0, "is_user_present": True},
    }
    s = snapshot_from_vehicle_data(data)
    assert s["user_present"] is True
    assert s["lat"] == 3.0733 and s["lon"] == 101.6067


def test_live_trip_reports_progress():
    from app.sync import live_trip

    trip = {"ts": T0, "odo_km": 10_000.0, "soc": 80, "max_speed": 95}
    now = snap(T0 + 1800, 10_030.0, 74, shift="D", speed=80, present=True)
    lt = live_trip(trip, now, capacity_kwh=60.0)
    assert lt["distance_km"] == 30.0
    assert lt["duration_min"] == 30
    assert lt["avg_speed_kmh"] == 60.0
    assert lt["soc_used"] == 6
    assert lt["km_per_soc"] == 5.0
    assert lt["energy_kwh"] == 3.6                 # 6% of 60 kWh
    assert lt["wh_per_km"] == 120                  # 3.6 kWh over 30 km
    assert live_trip(None, now) is None


def test_charge_stays_open_until_it_stops():
    """A charge across snapshots = one entry, no 10-minute fragments."""
    c1 = snap(T0, 10_000.0, 60)
    c2 = snap(T0 + 600, 10_000.0, 65, charging=True, kw=11)
    c3 = snap(T0 + 1800, 10_000.0, 74, charging=True, kw=11)
    c4 = snap(T0 + 3600, 10_000.0, 78)

    d, c, trip, charge = step(None, c1)
    d, c, trip, charge = step(c1, c2, charge=charge)
    assert c == [] and charge is not None        # opened, anchored at c1
    d, c, trip, charge = step(c2, c3, charge=charge)
    assert c == [] and charge is not None        # still charging, nothing logged
    d, c, trip, charge = step(c3, c4, charge=charge)
    assert charge is None and len(c) == 1
    (chg,) = c
    assert abs(chg["energy_added_kwh"] - 10.8) < 1e-6   # 60 -> 78 = 18% of 60 kWh
    assert chg["charge_type"] == "AC"
    assert abs(chg["cost"] - 9.72) < 1e-6
    assert chg["duration_min"] == 60.0


def test_gap_fallback_logs_merged_sessions():
    """Everything missed between two parked snapshots still gets logged."""
    prev = snap(T0, 10_000.0, 80)
    cur = snap(T0 + 7200, 10_030.0, 85)  # drove 30 km AND charged while unseen
    d, c, trip, charge = step(prev, cur)
    assert len(d) == 1 and len(c) == 1
    assert d[0]["distance_km"] == 30.0
    assert c[0]["energy_added_kwh"] == 3.0       # +5% of 60 kWh
    assert trip is None and charge is None


def test_fast_charge_flag_makes_dc():
    prev = snap(T0, 10_000.0, 40, charging=True, kw=150, fast=True)
    cur = snap(T0 + 1500, 10_000.0, 75)
    _, c, _, _ = step(prev, cur)
    assert c[0]["charge_type"] == "DC"
    assert c[0]["max_power_kw"] == 150


def test_no_change_logs_nothing():
    prev = snap(T0, 10_000.0, 80)
    cur = snap(T0 + 600, 10_000.0, 80)
    assert step(prev, cur) == ([], [], None, None)
