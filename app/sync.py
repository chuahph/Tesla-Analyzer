"""Reconstruct drive/charge sessions from successive vehicle_data snapshots.

The free-tier server sleeps between visits, so instead of continuous polling
the app snapshots the car whenever the dashboard is opened (or Sync is tapped)
and derives what happened since the previous snapshot: an odometer increase
becomes a drive, a battery-level increase becomes a charge. Energy is estimated
from the SoC delta against the vehicle's pack capacity.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

MILES_TO_KM = 1.60934


def snapshot_from_vehicle_data(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Tesla vehicle_data payload into the fields the sync needs."""
    ds = data.get("drive_state") or {}
    cs = data.get("charge_state") or {}
    cl = data.get("climate_state") or {}
    vs = data.get("vehicle_state") or {}

    ts = ds.get("timestamp") or vs.get("timestamp") or cs.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 1e12:  # Tesla uses ms epochs
        ts = ts / 1000.0
    ts = float(ts) if ts else datetime.now().timestamp()

    temp = cl.get("outside_temp")
    return {
        "ts": ts,
        "odo_km": float(vs.get("odometer") or 0.0) * MILES_TO_KM,
        "soc": float(cs.get("battery_level") or 0.0),
        "charging": cs.get("charging_state") == "Charging",
        "charger_kw": float(cs.get("charger_power") or 0.0),
        "fast": bool(cs.get("fast_charger_present")),
        "out_temp": float(temp) if temp is not None else 20.0,
        "shift": ds.get("shift_state") or "P",
        "speed_kmh": float(ds.get("speed") or 0.0) * MILES_TO_KM,
    }


def sessions_between(
    prev: dict[str, Any] | None,
    cur: dict[str, Any],
    capacity_kwh: float,
    price_per_kwh: float,
) -> tuple[list[dict], list[dict]]:
    """Derive (drives, charges) that happened between two snapshots."""
    drives: list[dict] = []
    charges: list[dict] = []
    if not prev:
        return drives, charges

    dt_min = max((cur["ts"] - prev["ts"]) / 60.0, 0.0)
    start = datetime.fromtimestamp(prev["ts"])
    end = datetime.fromtimestamp(cur["ts"])

    odo_delta = cur["odo_km"] - prev["odo_km"]
    if odo_delta > 0.5:
        soc_used = max(prev["soc"] - cur["soc"], 0.0)
        energy = soc_used / 100.0 * capacity_kwh
        drives.append({
            "start_time": start,
            "end_time": end,
            "distance_km": round(odo_delta, 1),
            "duration_min": round(dt_min, 1),
            "start_soc": prev["soc"],
            "end_soc": cur["soc"],
            "energy_used_kwh": round(energy, 2),
            "avg_speed_kmh": round(odo_delta / (dt_min / 60.0), 1) if dt_min else 0.0,
            "max_speed_kmh": 0.0,
            "outside_temp_c": cur["out_temp"],
            "start_location": "",
            "end_location": "",
        })

    soc_gain = cur["soc"] - prev["soc"]
    if soc_gain > 0.5:
        energy = soc_gain / 100.0 * capacity_kwh
        dc = bool(prev.get("fast") or cur.get("fast"))
        charges.append({
            "start_time": start,
            "end_time": end,
            "duration_min": round(dt_min, 1),
            "start_soc": prev["soc"],
            "end_soc": cur["soc"],
            "energy_added_kwh": round(energy, 2),
            "charge_type": "DC" if dc else "AC",
            "max_power_kw": max(prev.get("charger_kw", 0.0), cur.get("charger_kw", 0.0)),
            "location": "",
            "cost": round(energy * price_per_kwh, 2),
            "outside_temp_c": cur["out_temp"],
        })

    return drives, charges
