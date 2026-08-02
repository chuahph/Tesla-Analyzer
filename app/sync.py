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
DRIVE_MIN_KM = 0.1   # ignore odometer jitter below this (gap-split / movement floor)
# A completed drive must clear a higher bar than raw jitter before it's recorded
# as a *trip*: unlocking/accessing a parked car (phone-as-key wake) can nudge the
# odometer a couple of tenths without any real driving, which would otherwise log
# a phantom "0 min, 0% battery, Home -> Home" trip. A genuine short move (charger
# to a parking spot) is ~0.4 km+. Below this floor the drive is dropped and its
# near-zero SoC change stays in the surrounding parked gap, counted as standby
# drain — which is what it actually is.
TRIP_MIN_KM = 0.3
CHARGE_MIN_PCT = 0.5  # ignore SoC jitter below this
# Independent absolute-kWh floor alongside CHARGE_MIN_PCT — see _charge_from()
# for why the %-gain gate alone isn't enough (a BMS SoC recalibration blip,
# e.g. right after a vehicle software reset, can clear it with ~0 real energy).
CHARGE_MIN_KWH = 0.2
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
# Most odometer movement a blind gap is allowed to fold into an adjacent trip
# rather than being left as an unattributed loss — shared by both ends: a blind
# gap's tail folding into the trip that just ended (Drive.end_lost_km), and an
# is_driving(prev) departure folding into the trip that's about to start
# (Drive.start_lost_km). PARK_SPEED_KMH/CITY_SPEED_KMH bound the gap's *rate*,
# not its total: a long enough gap can stay under those and still cover real
# distance, which would be an unobserved drive rather than a few metres of
# parking creep or a poor-signal departure. Beyond this the movement is left
# out and recorded rather than attributed on a guess.
GAP_CREEP_MAX_KM = 1.0
# The departure-side counterpart to GAP_CREEP_MAX_KM's is_driving(prev) case,
# but deliberately more generous rather than reusing that same cap. The two
# scenarios aren't the same size of problem: GAP_CREEP_MAX_KM elsewhere bounds
# genuine parking creep, a few metres at most. Here the question is whether a
# poor-signal departure could plausibly cover this much ground before network
# returned — and confirmed live, a departure through a hillside stretch (poor
# coverage from the driveway itself) lost 1.11 km before the first tracked
# reading arrived, comfortably past 1.0 km. was_parked already establishes
# the gap looks parked *on average* (see CITY_SPEED_KMH below) — that's the
# rate check; this is a separate, purely-empirical judgement call on how much
# absolute distance a real departure can plausibly hide, not a value derived
# from anything stricter. Revisit if a genuinely separate short trip ever
# turns out to be getting merged in under this.
DEPARTURE_GAP_MAX_KM = 3.0
# Mirror of MIN_PLAUSIBLE_WH_PER_KM below, used to decide whether prev is still
# a usable SoC/range baseline for the departure recovery above. The recovered
# *distance* is a measured odometer fact at any gap length, but SoC and range
# also fall while the car merely sits, so prev's pair is only the trip's true
# starting energy if the gap was mostly departure rather than mostly parking.
# Gap length alone doesn't separate those (a genuine poor-signal departure can
# span 20+ minutes), but the implied efficiency of the recovered stretch does:
# divide the energy that pulling the baseline back would add by the distance it
# would add, and standby drain masquerading as driving shows up immediately as
# an impossible Wh/km. Confirmed live, trip 309: a 2.5 h sleep before a 5.9 km
# drive offered 0.22 kWh against 0.2 km of parking shuffle — ~1100 Wh/km, where
# a real (if slow, climate-loaded) departure runs a few hundred at most. Above
# this the trip keeps cur's own SoC/range and measures only the driving.
MAX_PLAUSIBLE_WH_PER_KM = 600.0
# How stale the last parked reading may be before it stops counting as a
# departure baseline at all, whatever its implied efficiency looks like.
#
# MAX_PLAUSIBLE_WH_PER_KM alone is not enough, because a long park and a slow
# hot crawl produce similar figures. Confirmed live, trip 319: a 2.3 hour park
# offered 0.52 km at roughly 406 Wh/km — comfortably under that bound, since
# 400 Wh/km is entirely ordinary for half a kilometre of parking-lot crawl with
# the air-conditioning fighting 34 degrees. The SoC baseline came back with the
# odometer and the park's standby drain came with it, putting the trip 5% over
# the car's own figure.
#
# Duration separates them where efficiency cannot. A poor-signal departure is a
# matter of minutes — the live cases this recovery exists for ran about twenty
# — while a stale anchor is hours old. This bound was rejected once on the
# reasoning that "a real departure runs 20+ minutes so gap length can't
# discriminate", which confused twenty minutes with two hours; set well clear
# of the former and nowhere near the latter. The odometer still comes back
# regardless: distance is a measured fact at any staleness, and the recovered
# stretch is priced at the trip's own efficiency (see energy_for_blind_distance).
#
# Both boundaries use this, which is why the name is about staleness rather
# than departures. The arrival side has the identical failure mode — a trip
# closed on a sleep report, then a later poll folding the intervening SoC drop
# into it — and is if anything the likelier end for it, since a sleep close
# means the car went quiet and the next poll is often hours away.
STALE_ANCHOR_MAX_MIN = 45.0
DEPARTURE_STALE_MAX_MIN = STALE_ANCHOR_MAX_MIN  # back-compat alias
# How long after a sustained-offline close (see routes.py's UNREACHABLE_CLOSE_MIN
# — just 3 minutes, deliberately short so a genuinely-ended short trip closes
# promptly) the next successful poll can still merge further movement into
# that trip on distance alone, no matter how large. 3 minutes offline is
# routinely exceeded by an active drive through a real dead zone — a long
# tunnel, a hilly or rural stretch with patchy coverage — not just a car that
# stopped: confirmed live, a single trip through a hillside area came back
# online already 4 km and several minutes further along, all one continuous
# drive with no actual stop in between. GAP_CREEP_MAX_KM's distance cap is the
# right guard against merging a genuine second, later drive when nothing else
# distinguishes the two; it is the wrong guard here, where the close itself is
# already known to be unreliable. Elapsed time is the more honest signal for
# *this* mechanism: within a plausible single-drive span of the close, still
# finding the car parked (not mid-departure) is strong enough evidence of
# continuity on its own. Past this window it reverts to the same distance cap
# as every other fold-in, since by then a genuinely separate later trip is the
# more likely explanation.
SLEEP_CLOSE_MERGE_MAX_MIN = 60.0
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


def now_local() -> datetime:
    """Now, as naive MYT wall-clock — the convention every stored timestamp
    uses (see ``_dt``, which is what writes them).

    ``datetime.now()`` is NOT interchangeable with this. It returns the
    *server's* local time, and nothing pins the server to MYT: a container
    runs UTC unless told otherwise, so on the deployed host every
    ``datetime.now()`` compared against a stored Drive/Charge timestamp is
    eight hours adrift — a trip logged at 23:00 reads as being in the future.
    Anything that windows, buckets or dates stored rows against "now" must
    come through here.

    Not a substitute for ``datetime.now().timestamp()``, which is already
    correct: a naive datetime converts to epoch through the system zone, so
    that round-trips no matter what the system zone is. This matters only
    where the naive value itself is compared against stored wall-clock.
    """
    return datetime.now(MYT).replace(tzinfo=None)


def to_epoch(dt: datetime) -> float:
    """Epoch seconds for a naive MYT wall-clock datetime (see ``now_local``).

    ``.timestamp()`` on its own would read the value as the *server's* zone,
    which is exactly the bug this exists to avoid; re-attaching MYT first is
    what makes the conversion independent of where the app runs.
    """
    return dt.replace(tzinfo=MYT).timestamp()


# Door/trunk and window openings Tesla reports on vehicle_state. Each is an
# int where 0 means shut, so any truthy value is "open".
_DOOR_FIELDS = ("df", "dr", "pf", "pr", "ft", "rt")
_WINDOW_FIELDS = ("fd_window", "fp_window", "rd_window", "rp_window")


def _any_open(vs: dict[str, Any], fields: tuple[str, ...]) -> bool | None:
    """Whether any of ``fields`` reads as open. None (not False) when the car
    reported none of them at all — "unknown" has to stay distinguishable from
    a confirmed all-shut, same rule as sentry_mode/climate_on below."""
    seen = [vs[f] for f in fields if f in vs and vs[f] is not None]
    return any(bool(v) for v in seen) if seen else None


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
        # Car Wash Mode shifts to Neutral so a conveyor/attendant can move the
        # car, which would otherwise read as "driving" (shift != "P") and
        # keep a trip open or reopen one right after parking.
        "car_wash_mode": bool(vs.get("car_wash_mode")),
        "lat": ds.get("latitude"),
        "lon": ds.get("longitude"),
        # Parked-drain context, not used for the drive/charge state machine —
        # only persisted onto BatteryReading (see /api/sync) so a later
        # vampire-drain gap can look up what was running right before the car
        # slept. None (not False) when Tesla didn't report the field at all,
        # kept distinct from a confirmed off.
        "sentry_mode": vs.get("sentry_mode") if "sentry_mode" in vs else None,
        # Physical-entry signals for the parked-intrusion alert (see
        # /api/sync). Unlike Sentry's own alarm state — which Tesla doesn't
        # publish at all — an opened door persists until someone shuts it,
        # so a 1-2 min poll catches it reliably rather than by luck.
        "doors_open": _any_open(vs, _DOOR_FIELDS),
        "windows_open": _any_open(vs, _WINDOW_FIELDS),
        # Logged only, nothing reads these yet — they exist to find out
        # empirically whether a Sentry trigger is visible in the API at all
        # (see BatteryReading's own note). Free to collect: same payload.
        "dashcam_state": vs.get("dashcam_state") if "dashcam_state" in vs else None,
        "center_display_state": (
            vs.get("center_display_state") if "center_display_state" in vs else None
        ),
        "climate_on": cl.get("is_climate_on") if "is_climate_on" in cl else None,
        # Tesla reports this as a tri-state string ("Off"/"On"/"FanOnly"), not
        # a bool — but it's the *setting* (whether COP is allowed to run at
        # all), which most owners leave "On" permanently as a safety
        # default, regardless of whether it's ever actually triggered. NOT a
        # drain signal by itself — see cabin_overheat_protection_actively_
        # cooling below for whether it's really running right now.
        "cabin_overheat_protection": cl.get("cabin_overheat_protection")
        if "cabin_overheat_protection" in cl else None,
        # The live flag: is COP actually cooling the cabin right now (drawing
        # real power), as opposed to merely being enabled as a setting above.
        "cabin_overheat_protection_actively_cooling": (
            cl.get("cabin_overheat_protection_actively_cooling")
            if "cabin_overheat_protection_actively_cooling" in cl else None
        ),
    }


def is_driving(s: dict[str, Any]) -> bool:
    # Car Wash Mode puts the car in Neutral (and it may get pushed a few
    # metres by the conveyor) without anyone actually driving it — never
    # treat that as a drive, regardless of shift/speed.
    if s.get("car_wash_mode"):
        return False
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
        # How wide the polling window was that the departure actually happened
        # inside. Every anchor at this end is an estimate placed somewhere in
        # this window, so its size IS the trip's start-side uncertainty — and
        # without it every trip reads as equally authoritative whether its
        # first driving reading arrived thirty seconds or eight minutes after
        # the last parked one (see Drive.start_gap_sec).
        "start_gap_sec": (round(cur["ts"] - prev["ts"], 1)
                          if prev and cur["ts"] >= prev["ts"] else None),
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


def _track_climate(open_trip: dict[str, Any], prev: dict[str, Any] | None,
                   cur: dict[str, Any]) -> None:
    """Accumulate how long climate ran during this trip, from the same
    interval walk _track_idle uses.

    Needed because climate is a whole-trip load, not an idle one: it runs
    while the car is moving just as much as while it sits, so the share of the
    trip it was actually on is what decides how much of the energy was not
    propulsion. Tracks the observed minutes and the minutes the car actually
    reported the flag separately — ``climate_on`` is None on cars/firmware
    that don't report it, and an unknown must not read as "off". Mutates
    open_trip in place.
    """
    if not prev:
        return
    interval_min = (cur["ts"] - prev["ts"]) / 60.0
    if interval_min <= 0 or interval_min >= PARK_GAP_MIN:
        return
    on = cur.get("climate_on")
    if on is None:
        return
    open_trip["climate_known_min"] = open_trip.get("climate_known_min", 0.0) + interval_min
    if on:
        open_trip["climate_min"] = open_trip.get("climate_min", 0.0) + interval_min


def climate_on_fraction(open_trip: dict[str, Any]) -> float:
    """Share of the observed trip that climate was running, 0..1.

    1.0 when the car never reported the flag — the pre-existing assumption,
    and the safe one: it keeps the correction working on cars that don't
    report climate rather than silently switching it off for them.
    """
    known = open_trip.get("climate_known_min", 0.0)
    if known <= 0:
        return 1.0
    return min(max(open_trip.get("climate_min", 0.0) / known, 0.0), 1.0)


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

    The "full pack range" projection (range / (soc/100)) is only as precise
    as *one* integer-percent SoC reading — e.g. a true 62.3% reported as 62
    skews the projected full range, and so the whole trip's energy, by
    ~0.5% — proportionally much worse on a short trip, where the range
    delta itself is small next to that fixed rounding error (reported live:
    a 9 km trip read noticeably low on kWh/Wh-per-km against the car's own
    display). Both endpoints carry the same *absolute* ±0.5-point rounding,
    but that's a larger *fraction* of a low-SoC reading, so a low-SoC
    endpoint's own projection is the noisier one. Combining the two as
    ``100 * (range0 + range1) / (soc0 + soc1)`` — total range over total
    SoC — is a precision-weighted estimate that leans on the higher-SoC
    (more reliable) endpoint, strictly beating a plain average of the two
    projections on a wide-SoC-span trip and matching it on a short one,
    never worse.
    """
    r0 = frm.get("range_km") or 0.0
    r1 = to.get("range_km") or 0.0
    soc0 = frm.get("soc") or 0.0
    soc1 = to.get("soc") or 0.0
    valid = [(r, s) for r, s in ((r0, soc0), (r1, soc1)) if r > 0 and s >= 5]
    if r0 > 0 and r1 > 0 and valid:
        full = 100.0 * sum(r for r, _ in valid) / sum(s for _, s in valid)
        if full > 0:
            return max(r0 - r1, 0.0) / full * capacity_kwh
    return max(soc0 - soc1, 0.0) / 100.0 * capacity_kwh


MIN_PLAUSIBLE_WH_PER_KM = 40.0  # below this over a whole trip = contaminated data
# Non-propulsion load while driving, split into the two parts the car's own
# energy breakdown reports separately — and calibrated against it across eight
# audited trips rather than assumed.
#
# The accessory term (Tesla's "Everything Else": 12V, electronics, pumps) is
# the steadiest figure in the whole dataset — 0.40 to 0.63 kW, a +/-21% spread
# with no visible dependence on anything. Flat is the honest shape for it.
#
# Climate is noisier: 0.67 to 1.34 kW, +/-33%, and the variation does NOT track
# outside temperature the way the model used to assume — 33-degree trips came
# in at 0.76 and 0.80 while 31-degree ones read 1.16 and 1.30. Cabin soak, sun
# load and fan setting evidently matter more than the number on the dash. The
# temperature term is kept because heating a cold cabin genuinely costs more
# and nothing here samples below 27 degrees, but its slope is cut to match what
# was measured: the old 0.12/degree averaged 1.43 kW against 1.01 observed, 42%
# high, which is what the discarded share-cap was really compensating for.
#
# Revisit both with cold-weather data. Eight trips inside a 27-34 degree band
# cannot say what happens at 5.
ACCESSORY_KW = 0.5
CLIMATE_BASE_KW = 0.35
CLIMATE_KW_PER_DEGREE = 0.08
CLIMATE_MAX_KW = 2.6


def climate_kwh(duration_min, out_temp_c=None, climate_frac=1.0):
    """Modelled climate/accessory energy over a whole trip, in kWh.

    The distinction that matters: this is a load that runs for the WHOLE trip,
    not only while the car sits. The previous model subtracted it over idle
    minutes alone, which meant stop-go traffic — where stops are frequent but
    each too short to count as idle — had no climate stripped at all, and the
    driving-only figure came out equal to the gross. Trips 313 and 317 both
    reported driving_wh_per_km identical to wh_per_km for exactly that reason,
    while the car's own screen attributed a fifth of each trip to Climate.

    ``climate_frac`` is the measured share of the trip climate actually ran
    (see climate_on_fraction), so a trip driven with it off is not charged for
    it. Defaults to 1.0, which is what cars that never report the flag get —
    the same assumption the idle model always made.
    """
    if duration_min <= 0:
        return 0.0
    t = out_temp_c if out_temp_c is not None else 22.0
    kw = min(CLIMATE_BASE_KW + CLIMATE_KW_PER_DEGREE * abs(t - 22.0), CLIMATE_MAX_KW)
    return kw * (duration_min / 60.0) * max(min(climate_frac, 1.0), 0.0)


def driving_only_kwh(energy_kwh, duration_min, out_temp_c=None, climate_min=None,
                     distance_km=None):
    """Propulsion-only energy: gross minus the loads that run regardless of how
    far the car goes — climate, and the steady accessory draw the car's own
    breakdown files under "Everything Else".

    Both are modelled because both are in the gap between the gross figure and
    Tesla's "Driving" line, and subtracting only climate left this figure
    structurally unable to reach it. Accessories are the better-behaved of the
    two: measured at 0.40-0.63 kW across the audited trips against climate's
    0.67-1.34.

    ``climate_min`` is the measured minutes climate ran, or None when the car
    never reported the flag — None means assume it ran throughout, which keeps
    the correction working on cars that don't report it. Accessory draw is not
    gated on it; it runs whenever the car is on.

    Floored at what the distance alone must have cost at MIN_PLAUSIBLE_WH_PER_KM,
    replacing an earlier cap at a fixed share of the gross. That share was the
    wrong shape: non-propulsion load scales with time, so as a fraction of a
    trip it is small on a fast run and large on a slow one — measured at 65% of
    a 45-minute, 8.9 km crawl, where a 40% cap blocked a subtraction the car's
    own numbers said should have been larger. A distance floor bounds the
    absurd case without fighting the physical one.
    """
    if not energy_kwh or energy_kwh <= 0:
        return energy_kwh
    frac = 1.0 if climate_min is None else (
        min(max(climate_min / duration_min, 0.0), 1.0) if duration_min > 0 else 1.0)
    modelled = climate_kwh(duration_min, out_temp_c, frac)
    modelled += ACCESSORY_KW * max(duration_min, 0.0) / 60.0
    floor = (max(distance_km, 0.0) * MIN_PLAUSIBLE_WH_PER_KM / 1000.0
             if distance_km else 0.0)
    return max(energy_kwh - modelled, floor)


def driving_only_wh_per_km(energy_kwh, distance_km, duration_min,
                           out_temp_c=None, climate_min=None):
    """driving_only_kwh expressed over the distance, in Wh/km."""
    if not energy_kwh or energy_kwh <= 0 or not distance_km or distance_km <= 0:
        return None
    return round(
        driving_only_kwh(energy_kwh, duration_min, out_temp_c, climate_min, distance_km)
        * 1000.0 / distance_km)


def _idle_adjusted_kwh(energy_kwh, idle_min, out_temp_c=None):
    """Driving-only energy (kWh): gross minus modeled climate/accessory draw
    over the idle minutes. Floored at half the gross so a noisy idle estimate
    can never wipe out most of the drive. This is the energy Tesla's own
    "Current Drive" reflects — it excludes the draw while sitting still."""
    t = out_temp_c if out_temp_c is not None else 22.0
    # Climate/accessory draw while stopped — higher the further from a mild ~22°C.
    idle_kw = min(0.35 + 0.12 * abs(t - 22.0), 2.6)
    return max(energy_kwh - idle_min / 60.0 * idle_kw, energy_kwh * 0.5)


# Most of a trip's distance that may have been folded in without its own
# energy reading before the correction below refuses to apply. Past this the
# "keep Wh/km constant" assumption is carrying more of the trip than the
# measured part is, and a wrong efficiency would be amplified rather than
# extended.
BLIND_DISTANCE_MAX_SHARE = 0.5


def energy_for_blind_distance(energy_kwh: float, distance_km: float,
                              blind_km: float) -> float:
    """Trip energy with the folded-in distance's own consumption added back.

    Both trip boundaries can pull odometer distance into a trip without the
    matching energy reading. The departure recovery moves the start anchor
    back over ground the car really covered, but only takes the SoC/range with
    it when that pair looks like driving rather than standby drain — and when
    it doesn't, the distance arrives with nothing attached. The blind-gap
    close does the same at the other end, deliberately: it folds the parking
    creep's metres in while keeping the earlier reading's SoC, because taking
    the later one would drag a whole nap's drain in with it.

    Both leave the same artifact — distance grew, energy didn't, so Wh/km is
    diluted by exactly the folded share. This is the identical defect the
    sustained-offline top-up had before it was fixed, where +33% distance
    against +0.00 kWh dropped Wh/km by a quarter.

    The estimate is the trip's own measured efficiency over the part that DID
    carry a reading, applied to the part that didn't — which is the same as
    holding Wh/km constant. That assumes the blind stretch was driven like the
    rest of the trip, which is why it is refused once the blind part is a
    large share of the whole.
    """
    measured = distance_km - blind_km
    if (not energy_kwh or energy_kwh <= 0 or blind_km <= 0
            or measured <= 0 or blind_km > distance_km * BLIND_DISTANCE_MAX_SHARE):
        return energy_kwh
    return energy_kwh * distance_km / measured


def trim_standby_kwh(energy_kwh: float, distance_km: float, trim_sec: float,
                     standby_kw: float | None) -> float:
    """Trip energy with the trimmed tail's parked drain taken back out.

    The pace-based stop correction (see Drive.tail_trim_sec) rewrites the
    recorded stop time but deliberately leaves the stop snapshot's own
    odo/SoC/range alone, so the trip's energy still runs to whenever the
    reading actually arrived. While the trim stayed near its 60s floor that
    was immaterial. It is not once an arrival lands in a dead zone: confirmed
    live, a 4.2 km trip trimmed by 1002 s carried 0.14 kWh of post-arrival
    standby — a sixth of the whole trip, and exactly the 0.2 SoC points it
    read high by against the car's own screen.

    ``standby_kw`` is this car's *measured* parked draw
    (driving.standby_kw), not a modelled one. The existing idle model is the
    wrong instrument here: it describes a car stopped mid-trip with someone
    aboard and climate running, and at 31 degrees would assume 1.43 kW against
    the ~0.5 kW a just-parked car actually showed. None means the history
    can't support a figure, and then nothing is subtracted at all &mdash;
    leaving the energy slightly high beats reshaping it with a guess.

    Floored at what the distance alone must have cost, so an over-long trim
    can never drive a real drive's energy down to nothing.
    """
    if not trim_sec or trim_sec <= 0 or not standby_kw:
        return energy_kwh
    drained = standby_kw * (trim_sec / 3600.0)
    floor = max(distance_km, 0.0) * MIN_PLAUSIBLE_WH_PER_KM / 1000.0
    return round(max(energy_kwh - drained, floor), 3)


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
                idle_min: float = 0.0, idle_tracked: bool = False,
                drive_min_km: float = DRIVE_MIN_KM):
    distance = cur["odo_km"] - start["odo_km"]
    # A recorded trip must clear the real-trip floor, not just the jitter floor,
    # so a wake-and-lock odometer nudge never logs as a phantom drive (see
    # TRIP_MIN_KM). A caller can still raise the bar further via drive_min_km.
    # Floor-test the *rounded* distance (same 1-decimal precision Tesla's own
    # screen shows), not the raw float — a genuine trip the car itself
    # displays as "0.3 km" can have a true odometer delta anywhere from 0.25
    # to 0.35, and comparing that raw value against a 0.3 floor discards real
    # short trips (e.g. charger bay to parking spot) about half the time.
    if round(distance, 1) < max(drive_min_km, TRIP_MIN_KM):
        return None
    dt_min = max((cur["ts"] - start["ts"]) / 60.0, 0.0)
    soc_used = max(start["soc"] - cur["soc"], 0.0)
    energy = _energy_kwh(start, cur, capacity_kwh)
    # Distance either anchor folded in without a matching SoC/range reading —
    # priced at the trip's own efficiency rather than left at zero, which
    # would dilute Wh/km by exactly the folded share (see
    # energy_for_blind_distance).
    energy = energy_for_blind_distance(
        energy, distance,
        (start.get("start_recovered_km") or 0.0
         if not start.get("start_energy_recovered") else 0.0)
        + (cur.get("end_folded_km") or 0.0),
    )
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
        # The polling windows the two boundaries were placed inside — the
        # trip's own uncertainty at each end (see Drive.start_gap_sec).
        "start_gap_sec": start.get("start_gap_sec"),
        "end_gap_sec": cur.get("end_gap_sec"),
        # Minutes climate was observed running, for the whole-trip climate
        # model (see climate_kwh). None when the car never reported the flag,
        # which must read as "unknown", not "off".
        "climate_min": (round(min(start.get("climate_min", 0.0), dt_min), 1)
                        if start.get("climate_known_min") else None),
        "idle_tracked": idle_tracked,
        # Seconds this trip's stop time was back-dated by the pace-based
        # correction, when the closing path evaluated one (see
        # Drive.tail_trim_sec). None from paths that never trim, so "not
        # applicable" stays distinct from a confirmed no-trim 0.0.
        "tail_trim_sec": cur.get("trim_sec"),
        # Odometer distance that happened before this trip's start anchor and
        # so isn't in its distance (see Drive.start_lost_km). Read from the
        # open trip, which is the `start` argument here.
        "start_lost_km": start.get("start_lost_km"),
        # And the same at the closing end (see Drive.end_lost_km) — read from
        # the close point, which is the `cur` argument.
        "end_lost_km": cur.get("end_lost_km"),
        # How much the departure recovery pulled back into this trip, which is
        # what disambiguates a 0.0 start_lost_km (see Drive.start_recovered_km).
        "start_recovered_km": start.get("start_recovered_km"),
        # Where on the odometer the two anchors sat. distance_km is their
        # difference; these are what let a trip be checked against the readings
        # taken around it (see driving.odometer_continuity).
        "start_odo_km": round(start["odo_km"], 3) if start.get("odo_km") is not None else None,
        "end_odo_km": round(cur["odo_km"], 3) if cur.get("odo_km") is not None else None,
    }


def close_trip_on_sleep(open_trip: dict, last_snapshot: dict, capacity_kwh: float,
                        drive_min_km: float = DRIVE_MIN_KM):
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
    # last_snapshot is the *intended* true end: sleep is only reachable once
    # the car has actually stopped moving, so under a genuine "asleep" report
    # there is no further tail to have lost distance in. A real 0.0, not the
    # null a raw snapshot would otherwise leave (see Drive.end_lost_km). This
    # is provisional, not guaranteed, for the caller closing on sustained
    # "offline" instead — that report doesn't carry the same guarantee (a dead
    # zone right at arrival can leave this reading genuinely short), so the
    # caller re-checks against the next real poll and corrects end_lost_km
    # then if it turns out to be wrong (see LAST_SLEEP_CLOSE_KEY in routes.py).
    # tail_trim_sec stays unset here regardless — this path runs no
    # pace-based stop estimate, so it genuinely never evaluates one, which is
    # exactly what null means for that field.
    return _drive_from(open_trip, {**last_snapshot, "end_lost_km": 0.0}, capacity_kwh,
                       open_trip.get("max_speed", 0.0),
                       idle_min, idle_tracked=True, drive_min_km=drive_min_km)


def live_trip(
    open_trip: dict | None, snap: dict | None, capacity_kwh: float = 75.0,
    drive_min_km: float = DRIVE_MIN_KM,
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
    if distance >= drive_min_km and energy_kwh * 1000.0 / distance < MIN_PLAUSIBLE_WH_PER_KM:
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
            if energy_kwh > 0 and distance >= drive_min_km else None
        ),
        "wh_per_km": round(energy_kwh * 1000.0 / distance) if energy_kwh > 0 and distance >= drive_min_km else None,
        "driving_wh_per_km": (
            _subtract_idle_energy(energy_kwh, distance, idle_min, snap.get("out_temp"))
            if energy_kwh > 0 and distance >= drive_min_km else None
        ),
    }


def _charge_from(start: dict, cur: dict, capacity_kwh: float, price_per_kwh: float,
                 drive_min_km: float = DRIVE_MIN_KM, price_per_kwh_dc: float | None = None):
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
        and (cur["odo_km"] - start["odo_km"]) >= drive_min_km
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
    # A second, independent floor on the *absolute* kWh, not just the SoC%
    # gain above: SoC is itself a BMS estimate, not a direct measurement, and
    # can nudge by a whole integer point on its own after a vehicle software
    # reset/reboot with no real energy added — on a small-ish pack that one
    # point alone can clear CHARGE_MIN_PCT. A session this size adds nothing
    # informative and, worse, becomes the "since last charge" anchor — reject
    # it outright rather than log a session that rounds to "0 kWh".
    if energy < CHARGE_MIN_KWH:
        return None
    dc = bool(start.get("fast") or cur.get("fast"))
    # Where the car was charging: GPS coords (named later in the API layer).
    # Without location access, fall back to the charger type so the Charging
    # Locations card still groups sessions meaningfully instead of being blank.
    location = _coords(start) or _coords(cur) or (
        "DC fast charger" if dc else "AC / home charger")
    # DC-specific rate wins when the caller supplied one — see
    # energy_price_dc_kwh in config.py; otherwise both charger types share
    # the single price_per_kwh the caller passed in.
    rate = price_per_kwh_dc if (dc and price_per_kwh_dc is not None) else price_per_kwh
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
        "cost": round(energy * rate, 2),
        "outside_temp_c": cur["out_temp"],
        # Transient (not a DB column): whether energy came from Tesla's meter,
        # so usable capacity can be calibrated only from real measurements.
        "energy_measured": energy_measured,
        # Transient: the session's own (SoC, kWh) samples, for measuring pack
        # capacity from the slope through them (see _charge_curve).
        "curve": start.get("curve") or [],
    }


def close_charge_on_sleep(open_charge: dict, last_snapshot: dict, capacity_kwh: float,
                          price_per_kwh: float, drive_min_km: float = DRIVE_MIN_KM,
                          price_per_kwh_dc: float | None = None):
    """Close a charge session the moment the car is confirmed asleep/gone
    unreachable, symmetric to ``close_trip_on_sleep``.

    Charging usually keeps a Tesla's computer awake, so this fires rarely —
    but connectivity can still drop (Wi-Fi/cell issue at the charge site)
    without the session having actually ended, so it's still worth closing
    from the last real reading rather than leaving it open indefinitely
    waiting for a reconnect that might be hours away.
    """
    return _charge_from(open_charge, last_snapshot, capacity_kwh, price_per_kwh, drive_min_km,
                        price_per_kwh_dc)


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


# Most (SoC, kWh) samples kept from one charging session. A long AC session
# polled every couple of minutes would otherwise grow this without bound, and
# the slope stops improving long before then — the spread of SoC covered
# matters, not how densely it was sampled.
CHARGE_CURVE_MAX_SAMPLES = 400


def _charge_curve(open_charge: dict, cur: dict) -> list[list[float]]:
    """Append this poll's (SoC, energy-added-so-far) pair to the session's
    curve, for measuring pack capacity from the slope rather than the ends.

    Only samples that move the SoC are kept. A charge polled every two minutes
    spends most of its samples on the same whole percent — Tesla reports SoC as
    an integer — and keeping all of them would weight the regression toward
    whichever percent happened to be sampled most, rather than toward the span
    the session actually covered.
    """
    curve = list(open_charge.get("curve") or [])
    soc = cur.get("soc")
    energy = cur.get("energy_added_kwh")
    if soc is None or energy is None or energy <= 0:
        return curve
    if curve and curve[-1][0] == soc:
        # Same whole percent: keep the latest reading for it rather than a
        # second point, so the pair describes where that percent ENDED.
        curve[-1] = [float(soc), float(energy)]
        return curve
    curve.append([float(soc), float(energy)])
    return curve[-CHARGE_CURVE_MAX_SAMPLES:]


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


def _split_gap_events(prev: dict, cur: dict, capacity_kwh: float, price_per_kwh: float,
                      drive_min_km: float = DRIVE_MIN_KM, price_per_kwh_dc: float | None = None):
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
    if meter_total is None or moved < drive_min_km:
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
        capacity_kwh, price_per_kwh, drive_min_km, price_per_kwh_dc,
    )
    drive = _drive_from(
        {"ts": split_ts, "odo_km": prev["odo_km"], "soc": split_soc,
         "lat": prev.get("lat"), "lon": prev.get("lon"),
         # Anchored to prev's odometer, so the reconstructed distance spans the
         # whole gap and nothing can have been dropped ahead of it — a confirmed
         # 0.0, not an unknown. Leaving it unset would make this path's rows
         # indistinguishable from pre-instrumentation ones.
         "start_lost_km": 0.0, "start_recovered_km": 0.0},
        {**cur, "end_lost_km": 0.0}, capacity_kwh, drive_min_km=drive_min_km,
    )
    return charge, drive


def process_snapshot(
    prev: dict | None,
    cur: dict,
    open_trip: dict | None,
    open_charge: dict | None,
    capacity_kwh: float,
    price_per_kwh: float,
    drive_min_km: float = DRIVE_MIN_KM,
    price_per_kwh_dc: float | None = None,
) -> tuple[list[dict], list[dict], dict | None, dict | None]:
    """Advance the session state machine by one snapshot.

    ``drive_min_km``: the minimum odometer movement treated as a real trip
    rather than jitter (a car nudged while parked, GPS drift, a multi-point
    turn) — see DRIVE_MIN_KM. Configurable (settings.drive_min_km) since it's
    a real trade-off, not a bug fix: lower it to catch genuinely short moves
    (a charger-to-parking-spot shuffle) at the cost of more exposure to
    logging non-trips as tiny phantom drives.

    ``price_per_kwh_dc``: DC fast-charging rate, when it differs from
    ``price_per_kwh`` (the AC/default rate) — see energy_price_dc_kwh in
    config.py. None means both charger types share ``price_per_kwh``.

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
        split_charge, split_drive = _split_gap_events(
            prev, cur, capacity_kwh, price_per_kwh, drive_min_km, price_per_kwh_dc)

    # --- Trips: open on power-on/in-gear, close when the car stops ---------
    if open_trip:
        open_trip = {
            **open_trip,
            "max_speed": max(open_trip.get("max_speed", 0.0), cur.get("speed_kmh") or 0.0),
        }
        _track_idle(open_trip, prev, cur)
        _track_climate(open_trip, prev, cur)
        gap_min = ((cur["ts"] - prev["ts"]) / 60.0) if prev else 0.0
        moved = cur["odo_km"] - (prev["odo_km"] if prev else cur["odo_km"])
        implied = (moved / (gap_min / 60.0)) if gap_min > 0 else 0.0

        if is_driving(cur) and prev and gap_min >= PARK_GAP_MIN and implied < PARK_SPEED_KMH:
            # Blind gap with little movement: the car parked and slept (unpolled),
            # then a new drive began. Close the first drive at the last seen point
            # and start a fresh one — two drives across a nap aren't one trip.
            #
            # The odometer movement across the gap is the tail of the drive
            # that just ended — the last metres of pulling into a spot — so
            # extend this trip to cover it, the same way the parked close below
            # keeps tracking the odometer forward. Left out it would belong to
            # no trip at all, since the next one opens at cur (reported live:
            # a trip reading 0.3 km short of the car's own display after
            # parking in a car park, with its energy intact — the signature of
            # a clipped tail rather than a clipped start).
            #
            # ONLY the odometer is extended. The timestamp and SoC/range stay
            # at prev, because the gap is overwhelmingly parked time: taking
            # cur's would add the whole nap to the duration and its standby
            # drain to the energy, and the gap has no upper bound, so that
            # drain can dwarf the ~0.06 kWh the creep itself used. Omitting the
            # creep's own small energy is the bounded error of the two, and the
            # deliberate cost of keeping the nap out.
            #
            # trim_sec is a real 0.0 for the same reason it is below: this path
            # closes at a known reading, so nothing was trimmed off the tail,
            # as distinct from a path that never considered one.
            creep_km = round(max(cur["odo_km"] - prev["odo_km"], 0.0), 3)
            fold_in = creep_km <= GAP_CREEP_MAX_KM
            close_at = {
                **prev,
                "odo_km": cur["odo_km"] if fold_in else prev["odo_km"],
                "trim_sec": 0.0,
                "end_lost_km": 0.0 if fold_in else creep_km,
                # Folded odometer distance carrying no SoC/range of its own —
                # this close keeps prev's reading deliberately, so the creep's
                # energy has to be priced rather than dropped.
                "end_folded_km": creep_km if fold_in else 0.0,
            }
            d = _drive_from(open_trip, close_at, capacity_kwh, open_trip.get("max_speed", 0.0),
                            _confirmed_idle_min(open_trip, prev["ts"]), idle_tracked=True,
                            drive_min_km=drive_min_km)
            if d:
                drives.append(d)
            open_trip = _open_trip_at(cur, cur, prev)
            # The gap's movement was accounted for above — folded into the trip
            # that just closed, or recorded on it as end_lost_km. Either way it
            # is not this trip's to lose, so say so explicitly rather than
            # leaving the field unset (which would read as "uninstrumented")
            # or reporting the same distance lost twice under two names.
            open_trip["start_lost_km"] = 0.0
            open_trip["start_recovered_km"] = 0.0
        elif is_driving(cur):
            open_trip["stop_at"] = None   # moving — cancel any pending stop point
        else:
            # Parked (not driving). Remember when it first stopped, and end the
            # trip *at that point* — so trailing idle (driver aboard, A/C on) is
            # never counted — once it's clearly over: powered down, charging, or
            # it has sat still past PARK_END_MIN.
            #
            # "First stopped" is a proxy for "the car has actually come to
            # rest" — wrong whenever the first "not driving" reading catches
            # it still creeping (a large named area/parking lot, not a single
            # point: shift/speed already read parked-ish before the car
            # finished pulling in). Freezing right there silently drops that
            # remaining creep from the trip's own distance/energy — it was
            # real, forward, odometer-confirmed movement, not idle — and it
            # never resurfaces anywhere else either (reported live: two
            # consecutive short trips at the same shared location read ~0.5
            # km short/long of the car's own display, not from the energy
            # math but from exactly this). So keep extending stop_at forward
            # (re-running the same pace-corrected estimate against the
            # latest reading) for as long as the odometer keeps climbing;
            # only once two consecutive "not driving" readings agree does
            # the car actually seem to have stopped, and stop_at freezes for
            # real.
            if not open_trip.get("stop_at") or cur["odo_km"] > open_trip["stop_at"]["odo_km"]:
                stop = {
                    k: cur.get(k) for k in
                    ("ts", "odo_km", "soc", "range_km", "out_temp", "lat", "lon")
                }
                # If this parked reading arrived after an unpolled gap during
                # which the car was still moving (poor signal on arrival, synced
                # later), cur's timestamp is the *sync* time, not when the car
                # actually stopped — trusting it balloons the duration with a
                # trailing tail of pure idle logged as if it were still driving
                # (reported live: a 7-min gap with the car parked after the
                # first ~1 min, logged as one 7-min "trip" at an impossible 2
                # km/h / 800 Wh/km). Only when the gap's own average implied
                # speed reads below a normal driving pace (CITY_SPEED_KMH) —
                # at or above it, the whole gap already looks like real
                # driving throughout, nothing to trim. A real (nonzero) speed
                # reading seen this trip is direct evidence it was genuinely
                # moving, so a shorter gap (IDLE_STREAK_MIN) is trusted; with
                # none at all — shift never confirmed in gear and moving,
                # just briefly nonzero odometer jitter — require the longer
                # PARK_END_MIN gap before assuming a floor pace covered it.
                # Below 60s of estimated correction isn't worth the
                # imprecision either way. The car covered the gap's distance
                # and then parked, so estimate the real stop as the last
                # reading plus the time to drive that distance at the trip's
                # moving pace — using *prev*'s own last-seen speed, not the
                # trip's peak, as the pace evidence (symmetric to the
                # power-on side using cur's first-seen speed): reported live,
                # a drive that had cruised much faster earlier still had that
                # early peak drive the pace estimate for the final,
                # already-slower-by-prev approach into a no-signal parking
                # spot, understating a genuine ~1-2 min slow-down-and-park by
                # assuming it was covered at the earlier, faster pace —
                # recording the stop just seconds after the last live
                # reading instead of when the car actually parked.
                min_gap = IDLE_STREAK_MIN if open_trip.get("max_speed", 0.0) > 0 else PARK_END_MIN
                # Default 0.0, not None: reaching here means a trim was
                # genuinely considered, so "it didn't fire" is a real finding
                # worth distinguishing from "never evaluated" (see
                # Drive.tail_trim_sec).
                stop["trim_sec"] = 0.0
                # The interval this stop point was chosen inside — the trip's
                # arrival-side uncertainty, whether or not a trim then fired.
                stop["end_gap_sec"] = round(gap_min * 60.0, 1) if prev else None
                # This close point tracks the odometer forward for as long as
                # it keeps climbing, so by the time the trip ends nothing is
                # left beyond it — a measured 0.0, not an assumption.
                stop["end_lost_km"] = 0.0
                if prev and gap_min >= min_gap and implied < CITY_SPEED_KMH and moved >= drive_min_km:
                    # The floor exists for when there is no speed evidence at
                    # all, not to overrule evidence that disagrees with it. A
                    # car nosing into a multi-storey car park genuinely was
                    # doing 5-10 km/h on its last reading, and forcing that up
                    # to 30 puts the estimated stop earlier than it happened —
                    # so the trim under-corrects and the trip still reads long
                    # (trip 316 kept +3 min after a 1002 s trim). Trust a real
                    # nonzero reading; fall back to the floor only when the
                    # car reported nothing to go on.
                    last_speed = prev.get("speed_kmh") or 0.0
                    pace = last_speed * 0.65 if last_speed > 0 else CITY_SPEED_KMH
                    est_stop = min(cur["ts"], prev["ts"] + moved / pace * 3600.0)
                    # Worth applying once it trims at least a minute of idle
                    # off the tail — not the estimate's own travel time (a
                    # short real move, like a final parking shuffle, always
                    # implies a travel time under a minute at any plausible
                    # pace, which would otherwise block exactly the case this
                    # exists to fix).
                    if cur["ts"] - est_stop >= 60:
                        stop["trim_sec"] = round(cur["ts"] - est_stop, 1)
                        stop["ts"] = est_stop
                open_trip["stop_at"] = stop
            stop_at = open_trip["stop_at"]
            parked_min = (cur["ts"] - stop_at["ts"]) / 60.0
            if is_powered_down(cur) or cur.get("charging") or parked_min >= PARK_END_MIN:
                d = _drive_from(open_trip, stop_at, capacity_kwh, open_trip.get("max_speed", 0.0),
                                _confirmed_idle_min(open_trip, stop_at["ts"]), idle_tracked=True,
                                drive_min_km=drive_min_km)
                if d:
                    drives.append(d)
                open_trip = None
    elif is_driving(cur):
        # Anchor the new trip to the last snapshot — unless that snapshot is
        # stale (the car sat parked/asleep since), in which case the drive began
        # just now, not back then, so start it here. Anchoring to a stale prev
        # would backdate the start by hours and fold overnight drain into it.
        # _was_parked_since alone only fires past STALE_ANCHOR_MIN (15 min) —
        # too coarse for a *confirmed* park (prev itself reads shift P, zero
        # speed, not just "gap too short to tell"): reported live, a car
        # parked and locked, then a short network gap (a few minutes) before
        # the next poll caught it already driving again — the gap was well
        # under 15 min, so this fell through to base=prev, backdating the new
        # trip's start straight into the park and showing zero gap against
        # the previous trip's end. When prev is confirmed parked, trust a
        # shorter gap too, but only when BOTH the gap's own implied speed
        # stayed low throughout (below PARK_SPEED_KMH) AND cur itself already
        # shows a real nonzero speed — direct evidence the car was already
        # moving normally by the time it was observed, meaning most of the
        # gap was still parked, not a slow, still-in-progress departure. A
        # zero-speed "just shifted into gear" cur (a car easing out of a
        # parking spot, still creeping) is exactly the ordinary case this
        # must NOT touch: implied speed reads low there too, but the car has
        # been continuously, gradually departing since prev, and the gap
        # genuinely belongs to this trip.
        was_parked = _was_parked_since(prev, cur)
        if (not was_parked and prev and not is_driving(prev)
                and (cur.get("speed_kmh") or 0.0) > 0):
            gap_h = (cur["ts"] - prev["ts"]) / 3600.0
            implied_kmh = (cur["odo_km"] - prev["odo_km"]) / max(gap_h, 1e-9)
            was_parked = implied_kmh < PARK_SPEED_KMH
        base = cur if was_parked else (prev or cur)
        open_trip = _open_trip_at(base, cur, prev)
        # Odometer movement that happened BEFORE this trip's anchor and is
        # therefore not counted in its distance — the symmetric counterpart to
        # tail_trim_sec at the other end. Zero when anchored at prev (nothing
        # can precede it) or once the recovery below pulls the movement back
        # in. A nonzero value is precisely the amount the trip reads short by,
        # which is otherwise impossible to see after the fact: the odometer is
        # continuous, so the distance doesn't go anywhere visible, it simply
        # belongs to no trip.
        open_trip["start_lost_km"] = (
            round(max(cur["odo_km"] - prev["odo_km"], 0.0), 3)
            if (prev and was_parked) else 0.0
        )
        # Nothing reclaimed unless the recovery below fires. A real 0.0, so a
        # trip can always be asked the question rather than answering None.
        open_trip["start_recovered_km"] = 0.0
        # Symmetric to the arrival case: if the first *driving* reading only came
        # through after an unpolled gap (poor signal at power-on), the last
        # parked reading is well before the car actually set off, so counting
        # from it inflates the start. When the car covered the gap's distance
        # slower than a steady city pace, it sat parked for part of it — start
        # the clock from when driving plausibly began, from the odometer, not
        # from the stale parked reading's timestamp. Applied regardless of
        # which base was picked above (reported live: a "was parked since"
        # gap picked base=cur, so a real ~4-5 min head start before the first
        # driving reading arrived was never corrected for at all — the two
        # branches need the same fix, not just the base=prev one). A gap with
        # negligible real movement (the genuine overnight-sleep case) still
        # estimates a start close to cur either way, so this doesn't regress
        # that case.
        if prev:
            gap_min = (cur["ts"] - prev["ts"]) / 60.0
            moved = cur["odo_km"] - prev["odo_km"]
            implied = moved / (gap_min / 60.0) if gap_min > 0 else 0.0
            # Same evidence-gated threshold as the arrival-side correction:
            # only when the gap's own average implied speed reads below a
            # normal driving pace (CITY_SPEED_KMH) — at or above it, the
            # whole gap already looks like real driving throughout, nothing
            # to back-estimate. A real (nonzero) speed on this first driving
            # reading is direct evidence the car's already moving, so a
            # shorter gap (IDLE_STREAK_MIN) is trusted; a bare
            # in-gear-but-still-0-speed reading has no such evidence, so
            # require the longer PARK_END_MIN gap before assuming a floor
            # pace covered it (a normal, close-to-real-time power-on
            # shouldn't get backdated on a hunch). Below 60s of estimated
            # correction isn't worth the imprecision either way. When
            # was_parked already anchored the start at cur, no gap floor at
            # all: any odometer movement proves the trip began before cur,
            # so a ≥60s back-estimate can only move the start closer to the
            # truth, never inflate it.
            min_gap = IDLE_STREAK_MIN if (cur.get("speed_kmh") or 0.0) > 0 else PARK_END_MIN
            if was_parked:
                min_gap = 0.0
            if gap_min >= min_gap and implied < CITY_SPEED_KMH and moved >= drive_min_km:
                # What pulling the energy baseline back to prev would add to
                # the trip, per km of the distance it would add with it — the
                # test for whether prev is still a departure reading or has
                # aged into a parked one (see MAX_PLAUSIBLE_WH_PER_KM).
                recovered_wh_per_km = (
                    _energy_kwh(prev, cur, capacity_kwh) * 1000.0 / moved
                    if moved > 0 else float("inf")
                )
                # is_driving(prev) blocks recovery below because prev isn't a
                # *confirmed* park (shift P, zero speed) — it could be a
                # genuinely separate, still-open earlier trip the gap simply
                # never caught closing, and re-anchoring onto that would wrongly
                # merge the two. But a poor-signal departure can just as easily
                # leave prev mid-transition (a glitched shift/speed reading right
                # as the car pulled off, not a real second trip) — reported live,
                # good network at the previous trip's stop but none at this
                # trip's own start, losing 0.566 km (and, separately, 1.11 km) of
                # a departure that was otherwise cleanly parked overnight.
                # implied/moved above already prove the gap looks parked on
                # average; bounding the recovered distance to DEPARTURE_GAP_MAX_KM
                # makes it safe to extend recovery to this case too — large
                # enough to catch a missed departure through a real dead zone,
                # small enough that a genuine separate unclosed trip's worth of
                # distance still gets left alone as start_lost_km rather than
                # silently merged in.
                if was_parked and (not is_driving(prev) or moved <= DEPARTURE_GAP_MAX_KM):
                    # base=cur anchored the trip's own odo/SoC to the *first
                    # driving* reading, which already reflects the "catch-up"
                    # distance/energy this block just proved happened before
                    # cur arrived — left as cur's, that chunk would silently
                    # vanish from the trip and surface one gap earlier as
                    # vampire drain instead (reported live: parked-gap kWh
                    # reading noticeably higher than expected, "should belong
                    # to trip kWh"). prev genuinely hadn't moved yet (the car
                    # doesn't move while parked), so its odo/SoC are the
                    # correct baseline for wherever within [prev, cur]
                    # departure actually began — same anchor the
                    # was_parked=False branch already uses by default.
                    # range_km must move with soc: _energy_kwh derives energy
                    # from the range delta *first*, so restoring soc alone
                    # left the energy uncorrected — and worse, handed it a
                    # mismatched pair (prev's soc against cur's range) to
                    # project the full pack from.
                    #
                    # Unconditional on moved >= drive_min_km alone, not also
                    # gated on the timestamp estimate below being "worth it"
                    # (>= 60s) — the movement itself is a measured fact from
                    # two real odometer readings, not an estimate, so unlike
                    # the clock guess it doesn't need a confidence floor.
                    # Previously sharing that 60s gate meant a short
                    # pre-departure stretch (under ~0.5 km at the pace floor)
                    # kept falling into the vampire-drain miscount above with
                    # no recovery at all (reported live, checked against the
                    # car's own trip meter: a 4.1 km drive logged as 3.6 km,
                    # its kWh short by the same stretch).
                    # Record what was reclaimed BEFORE zeroing the loss —
                    # otherwise the two zeros are indistinguishable, which is
                    # exactly the ambiguity start_recovered_km exists to end.
                    open_trip["start_recovered_km"] = round(
                        max(cur["odo_km"] - prev["odo_km"], 0.0), 3)
                    open_trip["odo_km"] = prev["odo_km"]
                    open_trip["start_lost_km"] = 0.0  # pulled back in, nothing lost
                    # The start coordinates move with the odometer, or the trip
                    # says two contradictory things about where it began: an
                    # odometer reading from the parking spot and a position from
                    # wherever the first poll after the blackout caught the car.
                    # Confirmed live (trip 322): a departure seen 1.579 km late
                    # kept the correct odometer but recorded its start 725 m
                    # away on a highway, so the trip read "Lim Chong Eu" when
                    # the car had left from home.
                    #
                    # Unconditional like the odometer, and for a stronger
                    # reason: a parked car does not move, so prev's position is
                    # exactly right however stale it is — unlike SoC, which
                    # drifts while it sits. Skipped only when prev carries no
                    # fix at all, since blanking a known position to adopt an
                    # unknown one would lose information rather than correct it.
                    if prev.get("lat") is not None and prev.get("lon") is not None:
                        open_trip["lat"] = prev["lat"]
                        open_trip["lon"] = prev["lon"]
                    # The odometer above is safe to pull back unconditionally —
                    # it only ever counts forward, so it carries no standby
                    # drain and prev's reading is a valid distance baseline no
                    # matter how stale it is. SoC/range are not: they fall while
                    # the car merely sits, so a prev from before a real park
                    # hands the trip that park's vampire drain as if the drive
                    # had spent it (confirmed live, trip 309: a 2.5 h sleep
                    # before a 5.9 km drive read +17% on energy and Wh/km, with
                    # the parked gap before it reporting an impossible 0.0 kWh —
                    # vampire_drain measures a gap as the previous trip's
                    # end_soc minus this trip's start_soc, so moving start_soc
                    # back to before the park makes the drain vanish from the
                    # gap and reappear inside the drive). was_parked sets
                    # min_gap to 0.0 above, so nothing else bounds how stale
                    # prev may be here; the implied-efficiency check is that
                    # bound. Past it the trip keeps cur's own SoC/range and
                    # measures only the driving, omitting the recovered
                    # stretch's own small energy — the same bounded trade the
                    # blind-gap tail fold-in makes for the same reason (see
                    # GAP_CREEP_MAX_KM's fold above), and the safer direction
                    # of the two: a few hundred Wh left out of one trip beats
                    # hours of standby drain moved into it.
                    #
                    # Both must move together or not at all: _energy_kwh
                    # projects the full pack from the range/SoC pair, so a
                    # mismatched pair (prev's soc against cur's range) is worse
                    # than either end used consistently.
                    open_trip["start_energy_recovered"] = (
                        recovered_wh_per_km <= MAX_PLAUSIBLE_WH_PER_KM
                        and gap_min <= STALE_ANCHOR_MAX_MIN)
                    if open_trip["start_energy_recovered"]:
                        open_trip["soc"] = prev["soc"]
                        open_trip["range_km"] = prev.get("range_km")
                # Same pace model as the arrival-side estimate: ``cur`` is the
                # first driving reading, so its instantaneous speed is real
                # evidence of the pace, not just an assumption — prefer it
                # over the flat city-speed floor when it implies a faster
                # start (e.g. already on a fast road when first seen). This
                # part *is* just an estimate, so it keeps its own 60s "worth
                # it" floor, independent of the odo/SoC recovery above.
                pace = max((cur.get("speed_kmh") or 0.0) * 0.65, CITY_SPEED_KMH)
                shift_sec = moved / pace * 3600.0
                if shift_sec >= 60:
                    est_start = cur["ts"] - shift_sec
                    open_trip["ts"] = min(max(est_start, prev["ts"]), cur["ts"])
    elif prev and split_drive:
        # A charge and a drive both happened in this gap — see
        # _split_gap_events for why the plain whole-gap drive reconstruction
        # below would get the wrong energy here.
        drives.append(split_drive)
    elif prev and (cur.get("car_wash_mode") or prev.get("car_wash_mode")):
        # Odometer moved (conveyor/attendant creep) but Car Wash Mode was
        # involved at either end of the gap — that's not a drive, so don't
        # reconstruct one from it.
        pass
    elif prev:
        # A whole drive happened between snapshots (asleep / cron gap).
        # prev is the start anchor and the distance is cur.odo - prev.odo, so
        # the span covers everything between the two readings: start_lost_km is
        # a confirmed 0.0 here. Set it on a copy rather than mutating prev,
        # which the caller still holds.
        d = _drive_from({**prev, "start_lost_km": 0.0, "start_recovered_km": 0.0},
                        {**cur, "end_lost_km": 0.0}, capacity_kwh,
                        drive_min_km=drive_min_km)
        if d:
            # If prev was stale (car parked overnight, then a short morning
            # drive), the reconstructed span/energy cover the idle period too —
            # re-estimate the timing and strip the vampire drain.
            if _was_parked_since(prev, cur):
                _reanchor_stale(d, cur, capacity_kwh)
            drives.append(d)

    # --- Charges: open while charging, close when it stops -----------------
    # Charging can never coincide with the car actively driving — a
    # "Charging" reading seen alongside is_driving(cur) is a stale/glitched
    # telemetry value (observed case: a regen-braking SoC uptick mid-drive
    # briefly misread as "started charging", logging a phantom session at
    # neither trip endpoint with SoC going the wrong way), not a real
    # session. Treat it as a reason to close out (if one was open) or never
    # open one at all.
    if open_charge:
        open_charge = {
            **open_charge,
            "max_kw": max(open_charge.get("max_kw", 0.0), cur.get("charger_kw") or 0.0),
            "fast": bool(open_charge.get("fast") or cur.get("fast")),
            # Every poll during a charge is a (SoC, kWh-so-far) pair, and the
            # slope through them IS the pack size — a far better measurement
            # than the session's two endpoints, which is all the endpoint
            # method has. See battery.capacity_from_curve.
            "curve": _charge_curve(open_charge, cur),
        }
        if not cur.get("charging") or is_driving(cur):
            c = _charge_from(open_charge, cur, capacity_kwh, price_per_kwh, drive_min_km,
                             price_per_kwh_dc)
            if c:
                charges.append(c)
            open_charge = None
    elif cur.get("charging") and not is_driving(cur):
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
    elif prev and not is_driving(cur):
        # A whole charge happened between snapshots. When the session meter
        # proves how much (see _gap_meter_total — it resets at plug-in, so a
        # changed value across a parked gap IS this session's total), use
        # that real measurement; otherwise match cur's value to force the
        # range/SoC estimate instead of a spurious stale-meter delta.
        # is_driving(cur) excluded: same reasoning as the live open/close
        # branches above — a SoC delta across a gap that ends with the car
        # actively driving isn't proof a charge happened (a regen uptick is
        # the observed real-world cause), and split_charge above already
        # only ever covers a charge-then-drive gap that ends back at rest.
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
            drive_min_km,
            price_per_kwh_dc,
        )
        if c:
            charges.append(c)

    return drives, charges, open_trip, open_charge
