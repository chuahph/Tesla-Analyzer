"""Tests for snapshot-based drive/charge reconstruction (app/sync.py)."""
from app.sync import sessions_between, snapshot_from_vehicle_data

T0 = 1_760_000_000.0  # seconds epoch


def snap(ts, odo_km, soc, charging=False, kw=0.0, fast=False):
    return {
        "ts": ts, "odo_km": odo_km, "soc": soc, "charging": charging,
        "charger_kw": kw, "fast": fast, "out_temp": 28.0,
        "shift": "P", "speed_kmh": 0.0,
    }


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


def test_drive_between_snapshots():
    prev = snap(T0, 10_000.0, 80)
    cur = snap(T0 + 1800, 10_024.9, 72)  # +24.9 km, -8% in 30 min
    drives, charges = sessions_between(prev, cur, 60.0, 0.90)
    assert charges == []
    (d,) = drives
    assert d["distance_km"] == 24.9
    assert abs(d["energy_used_kwh"] - 4.8) < 1e-6     # 8% of 60 kWh
    assert abs(d["avg_speed_kmh"] - 49.8) < 0.1
    assert d["duration_min"] == 30.0


def test_charge_between_snapshots():
    prev = snap(T0, 10_000.0, 72, charging=True, kw=11)
    cur = snap(T0 + 3600, 10_000.0, 90, charging=False)
    drives, charges = sessions_between(prev, cur, 60.0, 0.90)
    assert drives == []
    (c,) = charges
    assert abs(c["energy_added_kwh"] - 10.8) < 1e-6   # 18% of 60 kWh
    assert c["charge_type"] == "AC"
    assert abs(c["cost"] - 9.72) < 1e-6               # RM 0.90/kWh


def test_no_change_and_no_previous():
    cur = snap(T0 + 60, 10_000.0, 80)
    assert sessions_between(None, cur, 60.0, 0.90) == ([], [])
    assert sessions_between(snap(T0, 10_000.0, 80), cur, 60.0, 0.90) == ([], [])


def test_fast_charge_flag_makes_dc():
    prev = snap(T0, 10_000.0, 40, charging=True, kw=150, fast=True)
    cur = snap(T0 + 1500, 10_000.0, 75)
    _, charges = sessions_between(prev, cur, 60.0, 0.90)
    assert charges[0]["charge_type"] == "DC"
    assert charges[0]["max_power_kw"] == 150
