"""Reconstruct drive/charge sessions from successive vehicle_data snapshots.

The cron pings every few minutes, so sessions are tracked with a small state
machine instead of raw snapshot deltas:

  * a TRIP opens when the car is seen in gear and closes when the car powers
    down (driver gone, not merely shifted to P) — so a drive with brief stops
    stays one entry, however many snapshots it spanned;
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
# A trip ends when the car stops moving — not only when it powers down. If the
# driver stays aboard (A/C running) the car may sit parked for a long time, and
# that idle time must not be counted as drive time/energy. PARK_END_MIN is how
# long the car may sit still (shift P) before the trip is closed at the point it
# stopped. PARK_GAP_MIN is the blind-gap equivalent (the car slept, unpolled).
# PARK_SPEED_KMH: below this implied speed across a gap the car was parked, not
# driving through it (so a continuous drive with a missed poll isn't split).
PARK_END_MIN = 15.0
PARK_GAP_MIN = 20.0
PARK_SPEED_KMH = 15.0
# If the last snapshot is older than this AND the car barely moved since (it was
# parked/asleep, not driving), a new drive must NOT be anchored to it — otherwise
# the overnight idle time and its vampire drain get counted into the trip.
STALE_ANCHOR_MIN = 15.0
MYT = timezone(timedelta(hours=8))  # Malaysia has no DST


CITY_SPEED_KMH = 30.0  # assumed door-to-door pace when the real duration is unknown


def _was_parked_since(prev: dict | None, cur: dict) -> bool:
    """True if the last snapshot is stale — the car sat parked/asleep in between
    (a long wall-clock gap with almost no odometer movement), so a drive seen now
    started just now, not back then."""
    if not prev:
        return False
    gap_h = (cur["ts"] - prev["ts"]) / 3600.0
    if gap_h * 60.0 <= STALE_ANCHOR_MIN:
        return False
    implied_kmh = (cur["odo_km"] - prev["odo_km"]) / max(gap_h, 1e-9)
    return implied_kmh < PARK_SPEED_KMH


def _reanchor_stale(d: dict, cur: dict, capacity_kwh: float) -> dict:
    """Fix a gap-fallback drive whose start snapshot was stale (the car sat
    parked/asleep for hours before it).

    When a whole drive is reconstructed from ``prev -> cur`` but ``prev`` is
    last night's snapshot, the wall-clock span and the range delta both cover
    the entire idle period — so the trip reads as hours long (696 min for a
    10-min drive) and its energy includes overnight vampire drain (0.82 kWh for
    a 0.6 kWh drive). We can't recover the exact start, so:

      * re-estimate the duration from the distance at a typical city pace, and
        back-date the start from ``cur`` (the drive just ended);
      * recompute the energy from the distance at the car's *current* rated
        efficiency, which strips the idle drain the range delta had folded in.
    """
    distance = d["distance_km"]
    est_min = round(distance / CITY_SPEED_KMH * 60.0, 1)
    d["duration_min"] = est_min
    d["start_time"] = _dt(cur["ts"] - est_min * 60.0)
    avg = distance / (est_min / 60.0) if est_min else 0.0
    d["avg_speed_kmh"] = round(avg, 1)
    d["max_speed_kmh"] = round(max(d.get("max_speed_kmh", 0.0), avg), 1)
    # Energy from the car's current rated consumption (kWh/km implied by the
    # rated range at the current SoC), not the drain-contaminated range delta.
    soc = cur.get("soc") or 0.0
    range_km = cur.get("range_km") or 0.0
    if soc >= 5 and range_km > 0:
        full_range = range_km / (soc / 100.0)
        if full_range > 0:
            rated_wh_per_km = capacity_kwh * 1000.0 / full_range
            energy = distance * rated_wh_per_km / 1000.0
            d["energy_used_kwh"] = (
                round(energy, 2)
                if energy * 1000.0 / distance >= MIN_PLAUSIBLE_WH_PER_KM else 0.0
            )
    return d


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
        "range_km": float(cs.get("battery_range") or 0.0) * MILES_TO_KM,
        "charging": cs.get("charging_state") == "Charging",
        "charger_kw": float(cs.get("charger_power") or 0.0),
        # Tesla's own measured energy added this session (kWh) — accumulates
        # while charging, resets per session. More accurate than a SoC estimate.
        "energy_added_kwh": float(cs.get("charge_energy_added") or 0.0),
        "fast": bool(cs.get("fast_charger_present")),
        "out_temp": float(temp) if temp is not None else 20.0,
        "shift": ds.get("shift_state") or "P",
        "speed_kmh": float(ds.get("speed") or 0.0) * MILES_TO_KM,
        "user_present": bool(vs.get("is_user_present")),
        "locked": bool(vs.get("locked")),
        "lat": ds.get("latitude"),
        "lon": ds.get("longitude"),
    }


def is_driving(s: dict[str, Any]) -> bool:
    return (s.get("shift") or "P") != "P" or (s.get("speed_kmh") or 0.0) > 0


def _open_trip_at(base: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """Start a fresh open-trip anchored at ``base`` (the snapshot it began from)."""
    return {
        "ts": base["ts"],
        "odo_km": base["odo_km"],
        "soc": base["soc"],
        "range_km": base.get("range_km"),
        "max_speed": cur.get("speed_kmh") or 0.0,
        "lat": base.get("lat"),
        "lon": base.get("lon"),
    }


def is_powered_down(s: dict[str, Any]) -> bool:
    """Trip boundary: parked AND done driving.

    "Done" means the driver left the cabin (no user present) OR the car is
    locked — locking is the definitive end-of-drive signal and closes the
    trip even if presence detection lags. A brief unlocked stop with the
    driver inside keeps the trip open, so one errand run with short stops
    logs as a single power-on-to-power-down trip. Snapshots without
    ``is_user_present`` fall back to plain "in P" so older state keeps working.
    """
    return not is_driving(s) and (not s.get("user_present") or bool(s.get("locked")))


def _coords(s: dict[str, Any] | None) -> str:
    """'lat, lon' string for the location columns (searchable in any maps app)."""
    if not s or s.get("lat") is None or s.get("lon") is None:
        return ""
    return f"{float(s['lat']):.4f}, {float(s['lon']):.4f}"


def _energy_kwh(frm: dict, to: dict, capacity_kwh: float) -> float:
    """Battery energy drawn between two snapshots (kWh).

    battery_level is an integer percent, which quantises a short trip to
    whole-percent steps (a 0.6% trip reads as 1% — a huge Wh/km error).
    The rated remaining range is fractional, so prefer its delta scaled
    through the projected full range; fall back to the SoC delta.
    """
    r0 = frm.get("range_km") or 0.0
    r1 = to.get("range_km") or 0.0
    soc0 = frm.get("soc") or 0.0
    if r0 > 0 and r1 > 0 and soc0 >= 5:
        full = r0 / (soc0 / 100.0)
        if full > 0:
            return max(r0 - r1, 0.0) / full * capacity_kwh
    return max(soc0 - (to.get("soc") or 0.0), 0.0) / 100.0 * capacity_kwh


MIN_PLAUSIBLE_WH_PER_KM = 40.0  # below this over a whole trip = contaminated data


def driving_wh_per_km(energy_kwh, distance_km, duration_min, out_temp_c=None,
                      avg_speed_kmh=None, max_speed_kmh=None):
    """Estimate the *driving-only* Wh/km by removing modeled idle/climate load.

    Our trips span power-on to power-down, so genuine stop-go traffic (the car
    sped up, then sat stopped with A/C in the heat) captures idle energy that
    Tesla's "Current Drive" excludes. This subtracts an estimate of it so the
    number is comparable to the car's screen.

    Idle is only inferred when we actually observed a peak speed meaningfully
    above the trip average — i.e. the car really did go faster and therefore
    must have been stopped for the rest. A slow-but-*continuous* crawl (low
    average, no higher peak) is treated as real driving with no idle, so the
    figure isn't wrongly trimmed. It never inflates efficiency.
    """
    if not energy_kwh or energy_kwh <= 0 or distance_km <= 0 or duration_min <= 0:
        return None
    avg = avg_speed_kmh if avg_speed_kmh and avg_speed_kmh > 0 else distance_km / (duration_min / 60.0)
    mx = max_speed_kmh or 0.0
    # Average speed while actually moving. Only assume the car went faster than
    # its trip average — meaning some time was spent stopped — when a higher peak
    # was actually seen; otherwise it moved steadily and there's no idle.
    v_moving = max(avg, 0.65 * mx) if mx > avg + 5 else avg
    idle_frac = max(0.0, 1.0 - avg / v_moving) if v_moving > 0 else 0.0
    idle_min = duration_min * idle_frac
    t = out_temp_c if out_temp_c is not None else 22.0
    # Climate/accessory draw while stopped — higher the further from a mild ~22°C.
    idle_kw = min(0.35 + 0.12 * abs(t - 22.0), 2.6)
    driving_kwh = max(energy_kwh - idle_min / 60.0 * idle_kw, energy_kwh * 0.5)
    return round(driving_kwh * 1000.0 / distance_km)


def _drive_from(start: dict, cur: dict, capacity_kwh: float, max_speed: float = 0.0):
    distance = cur["odo_km"] - start["odo_km"]
    if distance < DRIVE_MIN_KM:
        return None
    dt_min = max((cur["ts"] - start["ts"]) / 60.0, 0.0)
    soc_used = max(start["soc"] - cur["soc"], 0.0)
    energy = _energy_kwh(start, cur, capacity_kwh)
    # A real drive can't average below ~40 Wh/km over its whole distance — that
    # means the range reading was refilled mid-trip (a charge or BMS recalibration
    # slipped into the session). Flag energy unknown so the trip shows "—" and is
    # left out of Wh/km averages rather than reporting an impossibly low figure.
    if energy * 1000.0 / distance < MIN_PLAUSIBLE_WH_PER_KM:
        energy = 0.0
    avg_speed = distance / (dt_min / 60.0) if dt_min else 0.0
    # Speed is only visible in the moment, so a drive with no mid-drive
    # snapshot would record max 0 — the average is the honest floor.
    return {
        "start_time": _dt(start["ts"]),
        "end_time": _dt(cur["ts"]),
        "distance_km": round(distance, 1),
        "duration_min": round(dt_min, 1),
        "start_soc": start["soc"],
        "end_soc": cur["soc"],
        "energy_used_kwh": round(energy, 2),
        "avg_speed_kmh": round(avg_speed, 1),
        "max_speed_kmh": round(max(max_speed, avg_speed), 1),
        "outside_temp_c": cur["out_temp"],
        "start_location": _coords(start),
        "end_location": _coords(cur),
    }


def live_trip(
    open_trip: dict | None, snap: dict | None, capacity_kwh: float = 75.0
) -> dict | None:
    """Progress of the drive in flight — the dashboard's "current drive" view."""
    if not open_trip or not snap:
        return None
    distance = max(snap["odo_km"] - open_trip["odo_km"], 0.0)
    dt_min = max((snap["ts"] - open_trip["ts"]) / 60.0, 0.0)
    soc_used = max(open_trip["soc"] - snap["soc"], 0.0)
    energy_kwh = _energy_kwh(open_trip, snap, capacity_kwh)
    avg_speed = distance / (dt_min / 60.0) if dt_min else 0.0
    # Current speed and average both bound the max from below.
    observed_max = max(open_trip.get("max_speed", 0.0),
                       snap.get("speed_kmh") or 0.0, avg_speed)
    # Integer SoC barely ticks on a short live drive, so derive the % used from
    # the measured energy (fractional range delta) when it's the larger figure.
    # Same contamination guard as completed drives: sub-40 Wh/km over the trip
    # means the range reading was refilled mid-drive — treat energy as unknown.
    if distance >= DRIVE_MIN_KM and energy_kwh * 1000.0 / distance < MIN_PLAUSIBLE_WH_PER_KM:
        energy_kwh = 0.0
    soc_from_energy = (energy_kwh / capacity_kwh * 100.0) if capacity_kwh else 0.0
    soc_eff = max(soc_used, soc_from_energy)
    return {
        "start_time": _dt(open_trip["ts"]).isoformat(timespec="minutes"),
        "distance_km": round(distance, 1),
        "duration_min": round(dt_min),
        "avg_speed_kmh": round(avg_speed, 1),
        "max_speed_kmh": round(observed_max, 1),
        "start_soc": open_trip["soc"],
        "soc": snap["soc"],
        "soc_used": round(soc_used, 1),
        "km_per_soc": round(distance / soc_eff, 1) if soc_eff >= 0.2 and distance else None,
        "energy_kwh": round(energy_kwh, 2),
        "wh_per_km": round(energy_kwh * 1000.0 / distance) if energy_kwh > 0 and distance >= DRIVE_MIN_KM else None,
        "driving_wh_per_km": (
            driving_wh_per_km(energy_kwh, distance, dt_min, snap.get("out_temp"),
                              avg_speed, observed_max)
            if energy_kwh > 0 and distance >= DRIVE_MIN_KM else None
        ),
    }


def _charge_from(start: dict, cur: dict, capacity_kwh: float, price_per_kwh: float):
    gain = cur["soc"] - start["soc"]
    if gain < CHARGE_MIN_PCT:
        return None
    dt_min = max((cur["ts"] - start["ts"]) / 60.0, 0.0)
    # Prefer Tesla's own measured energy for the session (charge_energy_added,
    # which accumulates during charging). Fall back to the range/SoC estimate
    # when the meter isn't available (e.g. a session missed between snapshots).
    measured = (cur.get("energy_added_kwh") or 0.0) - (start.get("energy_added_kwh") or 0.0)
    energy = measured if measured > 0 else _energy_kwh(cur, start, capacity_kwh)
    dc = bool(start.get("fast") or cur.get("fast"))
    energy_measured = measured > 0
    # Where the car was charging: GPS coords (named later in the API layer).
    # Without location access, fall back to the charger type so the Charging
    # Locations card still groups sessions meaningfully instead of being blank.
    location = _coords(start) or _coords(cur) or (
        "DC fast charger" if dc else "AC / home charger")
    return {
        "start_time": _dt(start["ts"]),
        "end_time": _dt(cur["ts"]),
        "duration_min": round(dt_min, 1),
        "start_soc": start["soc"],
        "end_soc": cur["soc"],
        "energy_added_kwh": round(energy, 2),
        "charge_type": "DC" if dc else "AC",
        "max_power_kw": max(start.get("max_kw", 0.0), cur.get("charger_kw", 0.0)),
        "location": location,
        "cost": round(energy * price_per_kwh, 2),
        "outside_temp_c": cur["out_temp"],
        # Transient (not a DB column): whether energy came from Tesla's meter,
        # so usable capacity can be calibrated only from real measurements.
        "energy_measured": energy_measured,
    }


def implied_capacity_kwh(charge: dict) -> float | None:
    """Usable pack capacity implied by a Tesla-measured charge (kWh).

    energy_added = SoC-gain-fraction × usable_capacity, so
    usable_capacity = energy_added / (SoC gain / 100). Only trust a
    Tesla-*measured* charge (calibrating from the SoC estimate would be
    circular) with a decent gain (limits integer-SoC quantisation), and
    clamp to a sane pack range so a bad reading can't corrupt Wh/km.
    """
    if not charge.get("energy_measured"):
        return None
    gain = (charge.get("end_soc") or 0) - (charge.get("start_soc") or 0)
    energy = charge.get("energy_added_kwh") or 0.0
    if gain < 15 or energy <= 0:
        return None
    cap = energy / (gain / 100.0)
    return round(cap, 1) if 45.0 <= cap <= 95.0 else None


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

    # --- Trips: open on power-on/in-gear, close when the car stops ---------
    if open_trip:
        open_trip = {
            **open_trip,
            "max_speed": max(open_trip.get("max_speed", 0.0), cur.get("speed_kmh") or 0.0),
        }
        gap_min = ((cur["ts"] - prev["ts"]) / 60.0) if prev else 0.0
        moved = cur["odo_km"] - (prev["odo_km"] if prev else cur["odo_km"])
        implied = (moved / (gap_min / 60.0)) if gap_min > 0 else 0.0

        if is_driving(cur) and prev and gap_min >= PARK_GAP_MIN and implied < PARK_SPEED_KMH:
            # Blind gap with little movement: the car parked and slept (unpolled),
            # then a new drive began. Close the first drive at the last seen point
            # and start a fresh one — two drives across a nap aren't one trip.
            d = _drive_from(open_trip, prev, capacity_kwh, open_trip.get("max_speed", 0.0))
            if d:
                drives.append(d)
            open_trip = _open_trip_at(cur, cur)
        elif is_driving(cur):
            open_trip["stop_at"] = None   # moving — cancel any pending stop point
        else:
            # Parked (not driving). Remember when it first stopped, and end the
            # trip *at that point* — so trailing idle (driver aboard, A/C on) is
            # never counted — once it's clearly over: powered down, charging, or
            # it has sat still past PARK_END_MIN.
            if not open_trip.get("stop_at"):
                open_trip["stop_at"] = {
                    k: cur.get(k) for k in
                    ("ts", "odo_km", "soc", "range_km", "out_temp", "lat", "lon")
                }
            stop_at = open_trip["stop_at"]
            parked_min = (cur["ts"] - stop_at["ts"]) / 60.0
            if is_powered_down(cur) or cur.get("charging") or parked_min >= PARK_END_MIN:
                d = _drive_from(open_trip, stop_at, capacity_kwh, open_trip.get("max_speed", 0.0))
                if d:
                    drives.append(d)
                open_trip = None
    elif is_driving(cur):
        # Anchor the new trip to the last snapshot — unless that snapshot is
        # stale (the car sat parked/asleep since), in which case the drive began
        # just now, not back then, so start it here. Anchoring to a stale prev
        # would backdate the start by hours and fold overnight drain into it.
        base = cur if _was_parked_since(prev, cur) else (prev or cur)
        open_trip = _open_trip_at(base, cur)
    elif prev:
        # A whole drive happened between snapshots (asleep / cron gap).
        d = _drive_from(prev, cur, capacity_kwh)
        if d:
            # If prev was stale (car parked overnight, then a short morning
            # drive), the reconstructed span/energy cover the idle period too —
            # re-estimate the timing and strip the vampire drain.
            if _was_parked_since(prev, cur):
                _reanchor_stale(d, cur, capacity_kwh)
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
            "range_km": base.get("range_km"),
            # Baseline for the measured meter is THIS (first charging) snapshot,
            # not prev — Tesla resets charge_energy_added to ~0 at plug-in, and
            # prev's value is stale from a previous session.
            "energy_added_kwh": cur.get("energy_added_kwh") or 0.0,
            "max_kw": cur.get("charger_kw") or 0.0,
            "fast": bool(cur.get("fast")),
            "lat": cur.get("lat"),
            "lon": cur.get("lon"),
        }
    elif prev:
        # A whole charge happened between snapshots — the session meter is
        # unreliable across the gap, so match cur's value to force the
        # range/SoC estimate instead of a spurious measured delta.
        c = _charge_from(
            {
                "ts": prev["ts"],
                "soc": prev["soc"],
                "range_km": prev.get("range_km"),
                "energy_added_kwh": cur.get("energy_added_kwh") or 0.0,
                "max_kw": prev.get("charger_kw", 0.0),
                "fast": prev.get("fast"),
                "lat": prev.get("lat"),
                "lon": prev.get("lon"),
            },
            cur,
            capacity_kwh,
            price_per_kwh,
        )
        if c:
            charges.append(c)

    return drives, charges, open_trip, open_charge
