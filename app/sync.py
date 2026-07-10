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


def _lock_unlocked(prev: dict | None, cur: dict) -> bool:
    """True if the car transitioned from locked to unlocked between snapshots.

    This is a strong signal of driving intent — the user explicitly unlocked
    the car, so a following shift to D/R/N is almost certainly the start of a trip.
    Used to confirm trip start when shift changes or speed increases.
    """
    if not prev:
        return False
    return bool(prev.get("locked")) and not bool(cur.get("locked"))


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

    Anchoring the end to ``cur`` assumes cur is itself a prompt reading (the
    normal case: the car stays reachable and the next poll catches it shortly
    after arrival). That assumption breaks if the car locks and falls straight
    back to sleep — cur then arrives whenever the car next wakes on its own,
    which can be much later, and the whole window reads late by exactly that
    amount. There's no reliable way to tell the two cases apart from just
    ``prev``/``cur`` (splitting the difference instead makes the far more
    common prompt case worse), so this is a known blind spot: the fix is
    catching the drive live via tighter polling (see poll_fast in the sync
    endpoint), not guessing harder after the fact.
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


ZERO_SPEED_KMH = 2.0  # below this = "stopped", not still rolling (GPS/speedo jitter floor)
# A stopped streak only counts as idle once sustained this long. 5 min (up
# from 3): real-world stop-go commutes chain a long traffic light + queue
# creep + the next light into 3-4 continuous near-stationary minutes, which
# is driving, not idling — a genuine mid-trip idle (parked with A/C, a
# pickup, a drive-through) comfortably exceeds 5.
IDLE_STREAK_MIN = 5.0
# An interval counts as stationary when the odometer implies at most this
# speed across it. A speed, not a fixed distance: 50 m over a 1-min poll is
# queue creep (moving traffic, ~3 km/h) and must break the still run rather
# than chain two light-waits into one long "idle", while 50 m over 4 sparse
# minutes (~0.75 km/h) genuinely is a car sitting still.
IDLE_CREEP_KMH = 1.5


def _open_trip_at(base: dict[str, Any], cur: dict[str, Any], prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a fresh open-trip anchored at ``base`` (the snapshot it began from).

    Tracks whether the unlock event preceded this shift to confirm driving intent.
    """
    return {
        "ts": base["ts"],
        "odo_km": base["odo_km"],
        "soc": base["soc"],
        "range_km": base.get("range_km"),
        "max_speed": cur.get("speed_kmh") or 0.0,
        "lat": base.get("lat"),
        "lon": base.get("lon"),
        # Lock event tracking: if the car was just unlocked, this trip is confirmed
        # as intentional driving (not just a brief shift to P or accidental gear change).
        "unlocked_before_drive": _lock_unlocked(prev, cur),
        # Real (not estimated) idle-time tracking, from the odometer: idle_min
        # accumulates stationary runs of at least IDLE_STREAK_MIN; still_run is
        # the in-progress run not yet committed, and still_since is when that
        # run began (so a trip closed mid-run counts only the in-window part).
        # Odometer-based, so it catches a sustained stop even when polling is
        # sparse and never samples the car at zero speed mid-stop.
        "idle_min": 0.0,
        "still_run": 0.0,
        "still_since": None,
    }


def _flush_idle_run(open_trip: dict[str, Any]) -> None:
    """Commit an in-progress stationary run to idle_min if it lasted long
    enough to be real idling (>= IDLE_STREAK_MIN), then clear it. A brief
    stop — a red light, a give-way — never reaches the threshold and is
    dropped as normal driving."""
    run = open_trip.get("still_run", 0.0)
    if run >= IDLE_STREAK_MIN:
        open_trip["idle_min"] = open_trip.get("idle_min", 0.0) + run
    open_trip["still_run"] = 0.0
    open_trip["still_since"] = None


def _track_idle(open_trip: dict[str, Any], prev: dict[str, Any] | None,
                cur: dict[str, Any]) -> None:
    """Accumulate real idle time from the *odometer* between two snapshots.

    If the wheels covered essentially no distance over an interval (implied
    speed at most IDLE_CREEP_KMH), the car sat still for that whole interval
    — true regardless of the instantaneous speed reading, so a stop is caught
    even when polling never lands a zero-speed sample mid-stop (the common
    case at multi-minute cron cadence, which the old speed-only tracker
    missed). Consecutive still intervals build a run that only counts once
    sustained past IDLE_STREAK_MIN, so short stops and chained light-waits
    with queue creep between them don't register while a genuine sit does.
    Intervals long enough to be a park/nap (>= PARK_GAP_MIN, handled
    separately as a trip boundary) end the run so overnight/parked drain is
    never folded into in-drive idle. Mutates open_trip in place.
    """
    if not prev:
        return
    interval_min = (cur["ts"] - prev["ts"]) / 60.0
    if interval_min <= 0 or interval_min >= PARK_GAP_MIN:
        _flush_idle_run(open_trip)
        return
    moved = (cur.get("odo_km") or 0.0) - (prev.get("odo_km") or 0.0)
    if moved / (interval_min / 60.0) <= IDLE_CREEP_KMH:
        if not open_trip.get("still_run"):
            # Anchor the run's start so a trip closed mid-run can count only
            # the part that falls inside the trip window (see _confirmed_idle_min).
            open_trip["still_since"] = prev["ts"]
        open_trip["still_run"] = open_trip.get("still_run", 0.0) + interval_min
    else:
        _flush_idle_run(open_trip)


def _confirmed_idle_min(open_trip: dict[str, Any], end_ts: float) -> float:
    """Real idle minutes accumulated in ``open_trip`` as of ``end_ts`` —
    committed runs plus any in-progress stationary run, truncated at
    ``end_ts``, once the counted part is sustained past IDLE_STREAK_MIN.

    The truncation matters at trip close: a trip that ends by sitting parked
    closes backdated to ``stop_at`` (when it first stopped), but the run kept
    accumulating through the trailing parked wait (up to PARK_END_MIN before
    the timeout close). Only the portion before ``end_ts`` is in-drive idle;
    the rest is post-trip parking and counting it would over-strip idle
    energy from driving_wh_per_km."""
    idle_min = open_trip.get("idle_min", 0.0)
    run = open_trip.get("still_run", 0.0)
    since = open_trip.get("still_since")
    if since is not None:
        run = min(run, max((end_ts - since) / 60.0, 0.0))
    if run >= IDLE_STREAK_MIN:
        idle_min += run
    return idle_min


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


def _idle_adjusted_kwh(energy_kwh, idle_min, out_temp_c=None):
    """Driving-only energy (kWh): gross minus modeled climate/accessory draw
    over the idle minutes. Floored at half the gross so a noisy idle estimate
    can never wipe out most of the drive. This is the energy Tesla's own
    "Current Drive" reflects — it excludes the draw while sitting still."""
    t = out_temp_c if out_temp_c is not None else 22.0
    # Climate/accessory draw while stopped — higher the further from a mild ~22°C.
    idle_kw = min(0.35 + 0.12 * abs(t - 22.0), 2.6)
    return max(energy_kwh - idle_min / 60.0 * idle_kw, energy_kwh * 0.5)


def _subtract_idle_energy(energy_kwh, distance_km, idle_min, out_temp_c=None):
    """Driving-only Wh/km: the idle-adjusted energy over the distance. Shared
    by the historical-trip estimate below and live_trip's real-tracked figure,
    so both use the same climate-load model."""
    if not energy_kwh or energy_kwh <= 0 or distance_km <= 0:
        return None
    return round(_idle_adjusted_kwh(energy_kwh, idle_min, out_temp_c) * 1000.0 / distance_km)


def driving_wh_per_km(energy_kwh, distance_km, duration_min, out_temp_c=None,
                      avg_speed_kmh=None, max_speed_kmh=None):
    """Estimate the *driving-only* Wh/km by removing modeled idle/climate load,
    for a completed trip where only start/end + peak speed are known (no
    continuous speed record was kept, e.g. legacy/imported trips).

    Our trips span power-on to power-down, so genuine stop-go traffic (the car
    sped up, then sat stopped with A/C in the heat) captures idle energy that
    Tesla's "Current Drive" excludes. This subtracts an estimate of it so the
    number is comparable to the car's screen.

    Idle is only inferred when we actually observed a peak speed meaningfully
    above the trip average — i.e. the car really did go faster and therefore
    must have been stopped for the rest. A slow-but-*continuous* crawl (low
    average, no higher peak) is treated as real driving with no idle, so the
    figure isn't wrongly trimmed. It never inflates efficiency.

    Prefer ``live_trip``'s real-tracked idle time when available (during an
    open trip) — this estimate is a fallback for when only the closed trip's
    summary fields survive, not a continuous record of when it was stopped.
    """
    if duration_min <= 0:
        return None
    avg = avg_speed_kmh if avg_speed_kmh and avg_speed_kmh > 0 else distance_km / (duration_min / 60.0)
    mx = max_speed_kmh or 0.0
    # Average speed while actually moving. Only assume the car went faster than
    # its trip average — meaning some time was spent stopped — when a higher peak
    # was actually seen; otherwise it moved steadily and there's no idle.
    v_moving = max(avg, 0.65 * mx) if mx > avg + 5 else avg
    idle_frac = max(0.0, 1.0 - avg / v_moving) if v_moving > 0 else 0.0
    idle_min = duration_min * idle_frac
    return _subtract_idle_energy(energy_kwh, distance_km, idle_min, out_temp_c)


def _drive_from(start: dict, cur: dict, capacity_kwh: float, max_speed: float = 0.0,
                idle_min: float = 0.0, idle_tracked: bool = False):
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
        # Real (not estimated) minutes spent stopped >= IDLE_STREAK_MIN, from
        # _track_idle — only meaningful when idle_tracked is true (live
        # tracking actually ran for this trip). False for whole-gap
        # reconstructions, where no tracking happened at all: idle_min stays
        # 0.0 there too, but analysis code must not read that as "confirmed
        # zero" without checking idle_tracked first.
        "idle_min": round(min(idle_min, dt_min), 1) if dt_min else 0.0,
        "idle_tracked": idle_tracked,
    }


def close_trip_on_sleep(open_trip: dict, last_snapshot: dict, capacity_kwh: float):
    """Close a trip the moment the car is confirmed properly asleep.

    A car cannot reach true sleep while driving — it needs power to move, so
    sleep is only reachable once parked and idle for a while. If a trip is
    still open when that happens, it is therefore definitely over, and
    ``last_snapshot`` (the most recent successful read) is a *good* anchor for
    the end, not a guess: with the sync endpoint's own poll-throttle bypassing
    for any car with an open trip, that reading is at most one poll interval
    old, never the hours-stale reading a later reconnect could bring. This
    avoids the whole-gap reconstruction (``_reanchor_stale``) and its inherent
    "which end of the gap did the drive happen near" ambiguity entirely, for
    this specific transition.
    """
    idle_min = _confirmed_idle_min(open_trip, last_snapshot["ts"])
    return _drive_from(open_trip, last_snapshot, capacity_kwh, open_trip.get("max_speed", 0.0),
                       idle_min, idle_tracked=True)


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
    idle_min = _confirmed_idle_min(open_trip, snap["ts"])
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
        "driving_energy_kwh": (
            round(_idle_adjusted_kwh(energy_kwh, idle_min, snap.get("out_temp")), 2)
            if energy_kwh > 0 and distance >= DRIVE_MIN_KM else None
        ),
        "wh_per_km": round(energy_kwh * 1000.0 / distance) if energy_kwh > 0 and distance >= DRIVE_MIN_KM else None,
        "driving_wh_per_km": (
            _subtract_idle_energy(energy_kwh, distance, idle_min, snap.get("out_temp"))
            if energy_kwh > 0 and distance >= DRIVE_MIN_KM else None
        ),
    }


def _charge_from(start: dict, cur: dict, capacity_kwh: float, price_per_kwh: float):
    dt_min = max((cur["ts"] - start["ts"]) / 60.0, 0.0)
    # Prefer Tesla's own measured energy for the session (charge_energy_added,
    # which accumulates during charging). Fall back to the range/SoC estimate
    # when the meter isn't available (e.g. a session missed between snapshots).
    measured = (cur.get("energy_added_kwh") or 0.0) - (start.get("energy_added_kwh") or 0.0)
    energy_measured = measured > 0

    # If the odometer moved since the charge opened, a drive happened before
    # this close poll ever got a chance to see "charging just stopped" — so
    # cur's SoC/range no longer reflect the charge alone, they've already
    # had the drive's consumption folded in. The plain SoC-gain gate below
    # would then judge a real, fully-measured charge as "too small" (or
    # even negative) purely because of what happened *after* it, and drop
    # the whole session despite good meter data. Tesla's own session meter
    # doesn't move for driving, so it stays trustworthy regardless; use it
    # for both the "was this real" gate and the end-SoC estimate in that
    # case, instead of the now-contaminated raw reading.
    moved = (
        start.get("odo_km") is not None and cur.get("odo_km") is not None
        and (cur["odo_km"] - start["odo_km"]) >= DRIVE_MIN_KM
    )
    if moved and energy_measured:
        gain = measured / capacity_kwh * 100.0 if capacity_kwh else 0.0
        end_soc = min(start["soc"] + gain, 100.0)
    else:
        gain = cur["soc"] - start["soc"]
        end_soc = cur["soc"]
    if gain < CHARGE_MIN_PCT:
        return None

    energy = measured if energy_measured else _energy_kwh(cur, start, capacity_kwh)
    dc = bool(start.get("fast") or cur.get("fast"))
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
        "end_soc": end_soc,
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


def close_charge_on_sleep(open_charge: dict, last_snapshot: dict, capacity_kwh: float,
                          price_per_kwh: float):
    """Close a charge session the moment the car is confirmed asleep/gone
    unreachable, symmetric to ``close_trip_on_sleep``.

    Charging usually keeps a Tesla's computer awake, so this fires rarely —
    but connectivity can still drop (Wi-Fi/cell issue at the charge site)
    without the session having actually ended, so it's still worth closing
    from the last real reading rather than leaving it open indefinitely
    waiting for a reconnect that might be hours away.
    """
    return _charge_from(open_charge, last_snapshot, capacity_kwh, price_per_kwh)


# AC (home/destination) charging routes mains power through the car's onboard
# charger, which loses ~5% to heat converting it to DC for the pack — so
# Tesla's reported charge_energy_added for an AC session runs a few % above
# what actually reached the battery. DC (Supercharger) feeds the pack
# directly with negligible conversion loss, so it's left unadjusted. Without
# this, every implied-capacity reading from AC charges (most home charging)
# is inflated, which then inflates every trip's computed kWh by the same
# proportion (confirmed against real Tesla-app Current Drive readings that
# ran ~5% under the uncorrected figure across independent trips).
AC_CHARGE_EFFICIENCY = 0.95


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
    if charge.get("charge_type") != "DC":
        cap *= AC_CHARGE_EFFICIENCY
    return round(cap, 1) if 45.0 <= cap <= 95.0 else None


def _gap_meter_total(prev: dict, cur: dict) -> float | None:
    """Unlogged kWh that Tesla's session meter proves was charged inside an
    unpolled ``prev -> cur`` gap, or None when the meter shows nothing new.

    ``charge_energy_added`` resets to ~0 at plug-in, accumulates while
    charging, and then PERSISTS untouched until the next plug-in. So what a
    changed value means depends entirely on what ``prev`` was doing:

      * ``prev`` parked/idle: its meter value is a stale leftover from some
        earlier session, so it must NOT be subtracted — a changed value
        means a new session ran inside the gap and ``cur``'s value IS that
        session's full total. (Subtracting the stale value was a real bug:
        whenever the previous session had added MORE than this one, the
        difference came out negative and the whole charge was treated as
        "no meter evidence" — then dropped outright if a post-charge drive
        had eaten the net SoC gain.)
      * ``prev`` mid-charge: same session, no reset in between — the
        portion up to ``prev`` was already tracked live (or logged by a
        sleep-close), so only the delta beyond it is new.

    A plugged-in-but-never-charged gap resets the meter to ~0 without
    adding anything; ``cur`` <= the noise floor returns None so that case
    can't fabricate a session.
    """
    cur_kwh = cur.get("energy_added_kwh") or 0.0
    prev_kwh = prev.get("energy_added_kwh") or 0.0
    if cur_kwh <= 0.05:
        return None
    if prev.get("charging"):
        delta = cur_kwh - prev_kwh
        return delta if delta > 0.05 else None
    return cur_kwh if abs(cur_kwh - prev_kwh) > 0.05 else None


def _split_gap_events(prev: dict, cur: dict, capacity_kwh: float, price_per_kwh: float):
    """Reconstruct a charge immediately followed by a short drive, when both
    happened inside one unpolled gap (the car charged, then set off before
    the next poll caught it — e.g. a nap-time top-up followed by a school run).

    The plain whole-gap fallbacks (below, in ``process_snapshot``) size each
    kind of event purely from the net prev->cur delta — the drive from the
    odometer, the charge from the SoC/range change. That's wrong once *both*
    kinds of event share the gap: the drive eats into the charge's net SoC
    gain, which can sink it below CHARGE_MIN_PCT and drop the whole session
    (exactly what a short errand right after a top-up charge does), while the
    drive's own energy calc gets a range delta that's really measuring the
    charge, not the drive.

    Tesla's own per-session charge meter (``energy_added_kwh``) survives in
    the vehicle_data payload until the *next* plug-in resets it — so a value
    higher than ``prev`` had, on two snapshots that are both parked/not
    charging, means a charge really completed inside this gap regardless of
    what driving happened afterward. Paired with genuine odometer movement
    (not jitter — see DRIVE_MIN_KM), that's enough to split the gap into an
    ordered charge-then-drive pair instead of corrupting or losing one of
    them.

    Returns ``(charge_or_None, drive_or_None)``; both None when there's no
    evidence of a combined event (the caller then uses the plain fallbacks).
    Order is assumed charge-first (plug in, charge, then depart) — the common
    case, and the only one there's any evidence for from just two snapshots.
    """
    meter_total = _gap_meter_total(prev, cur)
    moved = max(cur["odo_km"] - prev["odo_km"], 0.0)
    if meter_total is None or moved < DRIVE_MIN_KM:
        return None, None

    gained_pct = meter_total / capacity_kwh * 100.0 if capacity_kwh else 0.0
    split_soc = min(prev["soc"] + gained_pct, 100.0)

    # The charge dominates the gap in the common case (a multi-hour AC
    # session vs. a short errand); estimate the drive's own span from its
    # distance at a typical city pace, anchored to end at `cur` (the
    # prompt-poll assumption used throughout this module — see
    # _reanchor_stale), leaving the rest of the gap to the charge.
    drive_min = moved / CITY_SPEED_KMH * 60.0
    gap_min = max((cur["ts"] - prev["ts"]) / 60.0, 0.0)
    drive_min = min(drive_min, max(gap_min - 1.0, 0.0))
    split_ts = cur["ts"] - drive_min * 60.0

    charge = _charge_from(
        {"ts": prev["ts"], "soc": prev["soc"], "range_km": prev.get("range_km"),
         "energy_added_kwh": 0.0, "max_kw": prev.get("charger_kw", 0.0),
         "fast": prev.get("fast"), "lat": prev.get("lat"), "lon": prev.get("lon")},
        {"ts": split_ts, "soc": split_soc, "energy_added_kwh": meter_total,
         "charger_kw": 0.0, "fast": bool(prev.get("fast") or cur.get("fast")),
         "out_temp": cur["out_temp"]},
        capacity_kwh, price_per_kwh,
    )
    drive = _drive_from(
        {"ts": split_ts, "odo_km": prev["odo_km"], "soc": split_soc,
         "lat": prev.get("lat"), "lon": prev.get("lon")},
        cur, capacity_kwh,
    )
    return charge, drive


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

    # Detect a charge-then-drive combo sharing this gap up front — reached
    # only when both the trip and charge fallbacks below would otherwise run
    # (no open session, nothing in progress right now) — see
    # _split_gap_events for why the plain fallbacks corrupt/drop one event
    # when both happened together.
    split_charge = split_drive = None
    if (
        not open_trip and not open_charge and prev
        and not is_driving(cur) and not cur.get("charging")
    ):
        split_charge, split_drive = _split_gap_events(prev, cur, capacity_kwh, price_per_kwh)

    # --- Trips: open on power-on/in-gear, close when the car stops ---------
    if open_trip:
        open_trip = {
            **open_trip,
            "max_speed": max(open_trip.get("max_speed", 0.0), cur.get("speed_kmh") or 0.0),
        }
        _track_idle(open_trip, prev, cur)
        gap_min = ((cur["ts"] - prev["ts"]) / 60.0) if prev else 0.0
        moved = cur["odo_km"] - (prev["odo_km"] if prev else cur["odo_km"])
        implied = (moved / (gap_min / 60.0)) if gap_min > 0 else 0.0

        if is_driving(cur) and prev and gap_min >= PARK_GAP_MIN and implied < PARK_SPEED_KMH:
            # Blind gap with little movement: the car parked and slept (unpolled),
            # then a new drive began. Close the first drive at the last seen point
            # and start a fresh one — two drives across a nap aren't one trip.
            d = _drive_from(open_trip, prev, capacity_kwh, open_trip.get("max_speed", 0.0),
                            _confirmed_idle_min(open_trip, prev["ts"]), idle_tracked=True)
            if d:
                drives.append(d)
            open_trip = _open_trip_at(cur, cur, prev)
        elif is_driving(cur):
            open_trip["stop_at"] = None   # moving — cancel any pending stop point
        else:
            # Parked (not driving). Remember when it first stopped, and end the
            # trip *at that point* — so trailing idle (driver aboard, A/C on) is
            # never counted — once it's clearly over: powered down, charging, or
            # it has sat still past PARK_END_MIN.
            if not open_trip.get("stop_at"):
                stop = {
                    k: cur.get(k) for k in
                    ("ts", "odo_km", "soc", "range_km", "out_temp", "lat", "lon")
                }
                # If this parked reading arrived after a long unpolled gap during
                # which the car was still moving (poor signal on arrival, synced
                # much later), cur's timestamp is the *sync* time, not when the
                # car actually stopped — trusting it balloons the duration. The
                # car covered the gap's distance and then parked, so estimate the
                # real stop as the last reading plus the time to drive that
                # distance at the trip's moving pace.
                if prev and gap_min > PARK_END_MIN and moved >= DRIVE_MIN_KM:
                    pace = max(open_trip.get("max_speed", 0.0) * 0.65, CITY_SPEED_KMH)
                    stop["ts"] = min(cur["ts"], prev["ts"] + moved / pace * 3600.0)
                open_trip["stop_at"] = stop
            stop_at = open_trip["stop_at"]
            parked_min = (cur["ts"] - stop_at["ts"]) / 60.0
            if is_powered_down(cur) or cur.get("charging") or parked_min >= PARK_END_MIN:
                d = _drive_from(open_trip, stop_at, capacity_kwh, open_trip.get("max_speed", 0.0),
                                _confirmed_idle_min(open_trip, stop_at["ts"]), idle_tracked=True)
                if d:
                    drives.append(d)
                open_trip = None
    elif is_driving(cur):
        # Anchor the new trip to the last snapshot — unless that snapshot is
        # stale (the car sat parked/asleep since), in which case the drive began
        # just now, not back then, so start it here. Anchoring to a stale prev
        # would backdate the start by hours and fold overnight drain into it.
        base = cur if _was_parked_since(prev, cur) else (prev or cur)
        open_trip = _open_trip_at(base, cur, prev)
        # Symmetric to the arrival case: if the first *driving* reading only came
        # through after a long unpolled gap (poor signal at power-on), the last
        # parked reading is well before the car actually set off, so counting
        # from it inflates the start. When the car covered the gap's distance
        # slower than a steady city pace, it sat parked for part of it — start
        # the clock from when driving plausibly began, from the odometer, not
        # from the stale parked reading's timestamp.
        if base is prev and prev:
            gap_min = (cur["ts"] - prev["ts"]) / 60.0
            moved = cur["odo_km"] - prev["odo_km"]
            if gap_min > PARK_END_MIN and moved >= DRIVE_MIN_KM:
                # Same pace model as the arrival-side estimate: ``cur`` is the
                # first driving reading, so its instantaneous speed is real
                # evidence of the pace, not just an assumption — prefer it
                # over the flat city-speed floor when it implies a faster
                # start (e.g. already on a fast road when first seen).
                pace = max((cur.get("speed_kmh") or 0.0) * 0.65, CITY_SPEED_KMH)
                est_start = cur["ts"] - moved / pace * 3600.0
                open_trip["ts"] = min(max(est_start, prev["ts"]), cur["ts"])
    elif prev and split_drive:
        # A charge and a drive both happened in this gap — see
        # _split_gap_events for why the plain whole-gap drive reconstruction
        # below would get the wrong energy here.
        drives.append(split_drive)
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
            # Captured only to detect a drive slipping in before the close
            # poll notices charging stopped (see _charge_from) — not used
            # for anything else here.
            "odo_km": base.get("odo_km"),
            # Baseline is 0, not cur's already-accumulated meter reading. Tesla
            # resets charge_energy_added to ~0 at the true plug-in moment, so
            # by the time we first observe charging=True, cur's value already
            # reflects energy delivered since that reset — including whatever
            # was added during the poll gap before we noticed. Treating that
            # as a baseline to subtract silently discarded it, undercounting
            # every session that starts between polls (worst on fast DC —
            # a 5-minute miss at 100+ kW is several kWh gone from the total).
            # prev's meter value is never used here: it's stale from whatever
            # session was last measured, not this one.
            "energy_added_kwh": 0.0,
            "max_kw": cur.get("charger_kw") or 0.0,
            "fast": bool(cur.get("fast")),
            "lat": cur.get("lat"),
            "lon": cur.get("lon"),
        }
    elif prev and split_charge:
        # A charge and a drive both happened in this gap — see
        # _split_gap_events for why the plain whole-gap charge reconstruction
        # below would drop or shrink this session.
        charges.append(split_charge)
    elif prev:
        # A whole charge happened between snapshots. When the session meter
        # proves how much (see _gap_meter_total — it resets at plug-in, so a
        # changed value across a parked gap IS this session's total), use
        # that real measurement; otherwise match cur's value to force the
        # range/SoC estimate instead of a spurious stale-meter delta.
        meter_total = _gap_meter_total(prev, cur)
        cur_kwh = cur.get("energy_added_kwh") or 0.0
        c = _charge_from(
            {
                "ts": prev["ts"],
                "soc": prev["soc"],
                "range_km": prev.get("range_km"),
                # start baseline chosen so _charge_from's (cur - start)
                # difference yields exactly the proven total — or zero
                # (forcing the SoC estimate) when the meter proves nothing.
                "energy_added_kwh": (cur_kwh - meter_total) if meter_total is not None else cur_kwh,
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
