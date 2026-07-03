"""Reconstruct drive/charge sessions from successive vehicle_data snapshots.

The cron pings every few minutes, so sessions are tracked with a small state
machine instead of raw snapshot deltas:

  * a TRIP opens when the car is seen in gear and closes when it returns to P —
    one entry per drive, however many snapshots it spanned;
  * a CHARGE opens when charging is seen and closes when it stops;
  * if a whole drive/charge happened between two snapshots (car asleep, cron
    gap), the odometer / battery delta still logs it as a single merged entry.

Energy is estimated from the SoC delta against the vehicle's pack capacity.
Timestamps are converted to Malaysia wall time (UTC+8, no DST) so rows align
with the dashboard's MYT clock regardless of the server's timezone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

MILES_TO_KM = 1.60934
DRIVE_MIN_KM = 0.5   # ignore odometer jitter below this
CHARGE_MIN_PCT = 0.5  # ignore SoC jitter below this
MYT = timezone(timedelta(hours=8))  # Malaysia has no DST


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, MYT).replace(tzinfo=None)


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


def is_driving(s: dict[str, Any]) -> bool:
    return (s.get("shift") or "P") != "P" or (s.get("speed_kmh") or 0.0) > 0


def _drive_from(start: dict, cur: dict, capacity_kwh: float, max_speed: float = 0.0):
    distance = cur["odo_km"] - start["odo_km"]
    if distance < DRIVE_MIN_KM:
        return None
    dt_min = max((cur["ts"] - start["ts"]) / 60.0, 0.0)
    soc_used = max(start["soc"] - cur["soc"], 0.0)
    energy = soc_used / 100.0 * capacity_kwh
    return {
        "start_time": _dt(start["ts"]),
        "end_time": _dt(cur["ts"]),
        "distance_km": round(distance, 1),
        "duration_min": round(dt_min, 1),
        "start_soc": start["soc"],
        "end_soc": cur["soc"],
        "energy_used_kwh": round(energy, 2),
        "avg_speed_kmh": round(distance / (dt_min / 60.0), 1) if dt_min else 0.0,
        "max_speed_kmh": round(max_speed, 1),
        "outside_temp_c": cur["out_temp"],
        "start_location": "",
        "end_location": "",
    }


def _charge_from(start: dict, cur: dict, capacity_kwh: float, price_per_kwh: float):
    gain = cur["soc"] - start["soc"]
    if gain < CHARGE_MIN_PCT:
        return None
    dt_min = max((cur["ts"] - start["ts"]) / 60.0, 0.0)
    energy = gain / 100.0 * capacity_kwh
    dc = bool(start.get("fast") or cur.get("fast"))
    return {
        "start_time": _dt(start["ts"]),
        "end_time": _dt(cur["ts"]),
        "duration_min": round(dt_min, 1),
        "start_soc": start["soc"],
        "end_soc": cur["soc"],
        "energy_added_kwh": round(energy, 2),
        "charge_type": "DC" if dc else "AC",
        "max_power_kw": max(start.get("max_kw", 0.0), cur.get("charger_kw", 0.0)),
        "location": "",
        "cost": round(energy * price_per_kwh, 2),
        "outside_temp_c": cur["out_temp"],
    }


def process_snapshot(
    prev: dict | None,
    cur: dict,
    open_trip: dict | None,
    open_charge: dict | None,
    capacity_kwh: float,
    price_per_kwh: float,
) -> tuple[list[dict], list[dict], dict | None, dict | None]:
    """Advance the session state machine by one snapshot.

    Returns (drives, charges, open_trip, open_charge) — the sessions completed
    at this snapshot plus the carried-over open sessions.
    """
    drives: list[dict] = []
    charges: list[dict] = []

    # --- Trips: open while in gear, close on return to P -------------------
    if open_trip:
        open_trip = {
            **open_trip,
            "max_speed": max(open_trip.get("max_speed", 0.0), cur.get("speed_kmh") or 0.0),
        }
        if not is_driving(cur):
            d = _drive_from(open_trip, cur, capacity_kwh, open_trip.get("max_speed", 0.0))
            if d:
                drives.append(d)
            open_trip = None
    elif is_driving(cur):
        base = prev or cur  # the trip began somewhere after the last snapshot
        open_trip = {
            "ts": base["ts"],
            "odo_km": base["odo_km"],
            "soc": base["soc"],
            "max_speed": cur.get("speed_kmh") or 0.0,
        }
    elif prev:
        # A whole drive happened between snapshots (asleep / cron gap).
        d = _drive_from(prev, cur, capacity_kwh)
        if d:
            drives.append(d)

    # --- Charges: open while charging, close when it stops -----------------
    if open_charge:
        open_charge = {
            **open_charge,
            "max_kw": max(open_charge.get("max_kw", 0.0), cur.get("charger_kw") or 0.0),
            "fast": bool(open_charge.get("fast") or cur.get("fast")),
        }
        if not cur.get("charging"):
            c = _charge_from(open_charge, cur, capacity_kwh, price_per_kwh)
            if c:
                charges.append(c)
            open_charge = None
    elif cur.get("charging"):
        base = prev or cur
        open_charge = {
            "ts": base["ts"],
            "soc": base["soc"],
            "max_kw": cur.get("charger_kw") or 0.0,
            "fast": bool(cur.get("fast")),
        }
    elif prev:
        # A whole charge happened between snapshots.
        c = _charge_from(
            {
                "ts": prev["ts"],
                "soc": prev["soc"],
                "max_kw": prev.get("charger_kw", 0.0),
                "fast": prev.get("fast"),
            },
            cur,
            capacity_kwh,
            price_per_kwh,
        )
        if c:
            charges.append(c)

    return drives, charges, open_trip, open_charge
