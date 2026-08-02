"""Driving pattern analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from .. import sync as sync_mod
from ..models import Charge, Drive
from . import has_valid_energy, haversine_km, linregress, mean, percentile, safe_div

# Minimum parked gap (hours) between two consecutive drives worth counting as
# vampire drain — long enough to read as "parked/idle," not a quick errand
# stop or red-light-adjacent pause that's really still part of the day's
# driving.
VAMPIRE_MIN_GAP_HOURS = 1.0


# Odometer reconciliation. Below this a mismatch is parking shuffle, GPS-grade
# odometer jitter, or the 0.1 km the odometer itself is reported to — not a
# boundary the app got wrong.
CONTINUITY_TOLERANCE_KM = 0.15


def odometer_continuity(drives: list[Any], readings: list[Any]) -> dict[str, Any]:
    """Check each trip's recorded stop against where the car was actually seen
    resting afterwards, and report the ground no trip claims.

    The odometer only counts up, so it is the one measurement in the system
    that cannot be argued with. A trip that closed early still leaves the car
    sitting further along, and the readings taken while it is parked say
    exactly where — so the difference between a trip's ``end_odo_km`` and the
    highest odometer observed before the next trip began is distance that
    happened and belongs to that trip's arrival.

    ``end_lost_km`` is subtracted before judging, because a trip that already
    reported its own shortfall has not hidden anything. What is left is
    unrecorded: the case where a close was anchored short and nothing
    corrected it (trip 314), where the provisional 0.0 from a sleep close was
    never revisited, or where real movement was logged as no trip at all.

    Note what this deliberately cannot see. When a trip closes short and the
    *next* trip's departure recovery pulls its anchor back over the same
    ground, the odometer stays perfectly continuous — every metre is claimed
    exactly once, just by the wrong trip. That is a misattribution, not a
    discontinuity, and only the parked readings between the two can expose it,
    which is why this compares against readings rather than chaining trip to
    trip.
    """
    ordered = [d for d in sorted(drives, key=lambda d: d.start_time)
               if getattr(d, "end_odo_km", None) is not None]
    if not ordered or not readings:
        return {"available": False, "gaps": [], "unattributed_km": 0.0}
    obs = sorted(
        ((r.ts, r.odo_km) for r in readings if getattr(r, "odo_km", None) is not None),
        key=lambda x: x[0],
    )
    if not obs:
        return {"available": False, "gaps": [], "unattributed_km": 0.0}

    out: list[dict[str, Any]] = []
    total = 0.0
    for i, d in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        # Readings taken while parked after this trip: after it stopped, and
        # before the next one set off. The highest is where the car came to
        # rest, whatever the trip recorded.
        until = nxt.start_time if nxt else None
        resting = [o for t, o in obs
                   if t >= d.end_time and (until is None or t <= until)]
        if not resting:
            continue
        seen = max(resting)
        missing = seen - d.end_odo_km - (getattr(d, "end_lost_km", None) or 0.0)
        if missing <= CONTINUITY_TOLERANCE_KM:
            continue
        total += missing
        out.append({
            "drive_id": getattr(d, "id", None),
            "route": f"{d.start_location} → {d.end_location}"
            if d.start_location and d.end_location else "",
            "end_time": d.end_time.isoformat(timespec="minutes"),
            "recorded_end_odo_km": round(d.end_odo_km, 1),
            "observed_odo_km": round(seen, 1),
            "unrecorded_km": round(missing, 2),
        })
    return {
        "available": True,
        "gaps": out[-10:],
        "trips_checked": len(ordered),
        "unattributed_km": round(total, 2),
    }


# Measuring this car's own parked standby draw once it is properly asleep.
# Deliberately stricter than vampire_drain's reporting thresholds, because this
# feeds a correction rather than a narrative: start/end SoC are whole percents,
# so a gap has to be long enough for the loss to cross a point at all before
# its rate means anything, and a handful of gaps is not a rate.
#
# The floor was 2 h and that was measuring the wrong thing. Every park begins
# awake — screens, Sentry arming, HVAC settling — at several times the sleeping
# rate (see parked_awake_kw), so a short gap is mostly that opening burst and a
# long one is mostly sleep. Averaging both together lands between the two and
# describes neither: this car read 0.22 kW that way, while a measured 12.3 h
# overnight park lost under one whole percent, i.e. under 0.06 kW. At 0.22 kW
# it would have lost 3.9%. Six hours makes the opening burst a small enough
# share that what is left really is the sleeping rate.
#
# 2-6 h gaps now belong to neither rate, on purpose. They are a mixture, and
# there is no way to split one without knowing when the car actually fell
# asleep — which the API does not report.
STANDBY_MIN_GAP_HOURS = 6.0
# Two overnight parks. Raised with the floor: at 6 h a 12 h total could be a
# single gap, and one gap has never been a rate anywhere else in this module.
STANDBY_MIN_TOTAL_HOURS = 24.0
# Outside this the answer is a measurement artifact, not a parked car — a
# Tesla idles somewhere near 100-500 W depending on Sentry, climate and how
# long it takes to fall asleep.
STANDBY_PLAUSIBLE_KW = (0.02, 1.5)


# The first stretch after parking is a different animal from the hours that
# follow. The car is still awake — screens up, HVAC settling, Sentry arming —
# and draws several times what it settles to once asleep. Measured live on
# trip 316: a 17-minute tail cost ~0.5 kW where this car's multi-hour gaps
# average ~0.22.
#
# Individually these gaps say nothing: 17 minutes at 0.5 kW is 0.14 kWh, a
# fifth of one whole-percent SoC point, so most read as an exact zero. The rate
# only appears once enough of them are summed, which is why the totals required
# here are about the aggregate rather than any single gap.
AWAKE_MIN_GAP_HOURS = 0.15   # below this it's a traffic stop, not a park
AWAKE_MAX_GAP_HOURS = 2.0    # past this the sleeping hours start to dominate
AWAKE_MIN_TOTAL_HOURS = 6.0


def _gap_rate_kw(drives: list[Any], charges: list[Any] | None, capacity_kwh: float,
                 min_gap_h: float, max_gap_h: float | None,
                 min_total_h: float) -> float | None:
    """Average draw (kW) across the parked gaps falling in a duration band.

    Shared by the two rates that matter — the deep-sleep average and the
    just-parked one — because they differ only in which gaps they look at, and
    letting them drift apart in method would make them incomparable.
    """
    ordered = sorted(drives, key=lambda d: d.start_time)
    if len(ordered) < 2 or not capacity_kwh:
        return None
    charge_starts = sorted(c.start_time for c in (charges or []))
    total_kwh = 0.0
    total_hours = 0.0
    for a, b in zip(ordered, ordered[1:]):
        gap_start, gap_end = a.end_time, b.start_time
        gap_hours = (gap_end - gap_start).total_seconds() / 3600.0
        if gap_hours < min_gap_h or (max_gap_h is not None and gap_hours >= max_gap_h):
            continue
        # A charge anywhere inside the gap moved SoC upward, so its endpoints
        # say nothing about drain. Scanned per gap rather than with a marching
        # index: the bands skip gaps, so a shared cursor would fall behind.
        if any(gap_start < c < gap_end for c in charge_starts):
            continue
        total_kwh += max(a.end_soc - b.start_soc, 0.0) / 100.0 * capacity_kwh
        total_hours += gap_hours
    if total_hours < min_total_h or total_kwh <= 0:
        return None
    rate = total_kwh / total_hours
    lo, hi = STANDBY_PLAUSIBLE_KW
    return round(rate, 3) if lo <= rate <= hi else None


def standby_kw(drives: list[Any], charges: list[Any] | None,
               capacity_kwh: float) -> float | None:
    """This car's own average standby draw once properly asleep, in kW.

    Measured the same way vampire_drain measures a gap — the SoC a trip ended
    on minus the SoC the next one started from, over the hours between — but
    aggregated into a rate, and only from gaps long enough that the awake
    opening burst no longer dominates them. None when the history can't
    support a figure yet, which the caller must treat as "don't correct
    anything" rather than substituting a guess: a wrong rate here would
    quietly reshape real trip energy.
    """
    return _gap_rate_kw(drives, charges, capacity_kwh,
                        STANDBY_MIN_GAP_HOURS, None, STANDBY_MIN_TOTAL_HOURS)


# Directional cost of a route. Elevation is the one term the car's own energy
# breakdown reports that this app does not model at all, and it is the only
# component that reverses sign when you drive a route the other way: the climb
# out costs what the roll back returns (less regen losses), while rolling drag,
# aero, climate and accessories are the same both ways. So the difference
# between a route's two directions isolates it — from this car's own history,
# with no elevation service to call.
#
# The confound is that direction and conditions are often correlated: a commute
# runs outbound in morning traffic and home in evening traffic. That cannot be
# separated with the data here, so it is not hidden either — every row carries
# the mean speed each way, and `comparable` is False when they differ enough
# that traffic, not terrain, is the likelier explanation. A row that is not
# comparable is still reported; it just isn't evidence about elevation.
ROUTE_MIN_TRIPS_PER_DIRECTION = 3
ROUTE_MIN_KM = 3.0            # under this, boundary rounding swamps the signal
ROUTE_SPEED_GAP_MAX_KMH = 6.0  # beyond this the two directions aren't like-for-like


def _direction_stats(group: list[Any]) -> dict[str, Any]:
    """Distance-weighted Wh/km for one direction, plus what it was driven at."""
    distance = sum(d.distance_km for d in group)
    energy = sum(d.energy_used_kwh for d in group)
    speeds = [d.avg_speed_kmh for d in group if getattr(d, "avg_speed_kmh", None)]
    return {
        "n": len(group),
        "km": round(distance / len(group), 1),
        "wh_per_km": round(energy / distance * 1000.0, 1) if distance > 0 else None,
        "avg_speed_kmh": round(mean(speeds), 1) if speeds else None,
    }


def direction_wh_per_km(drives: list[Any], start_area: str,
                        end_area: str) -> dict[str, Any] | None:
    """What this exact direction of this exact route has actually cost.

    The planner's other bases are all averages over something else — every
    route at this hour, or every route at this speed. This one is the road
    being planned, driven the way it is about to be driven, which is why it
    also settles the elevation term that no average can: a route's climb only
    cancels when both directions are pooled.

    None unless the same direction has been driven enough times to mean
    something; the caller keeps its existing basis rather than trading a broad
    measurement for a thin one.
    """
    if not start_area or not end_area:
        return None
    group = [
        d for d in drives
        if (getattr(d, "start_area", "") or d.start_location) == start_area
        and (getattr(d, "end_area", "") or d.end_location) == end_area
        and d.start_location and d.end_location
        and has_valid_energy(d) and d.distance_km >= ROUTE_MIN_KM
    ]
    if len(group) < ROUTE_MIN_TRIPS_PER_DIRECTION:
        return None
    stats = _direction_stats(group)
    return stats if stats["wh_per_km"] else None


def route_asymmetry(drives: list[Any]) -> list[dict[str, Any]]:
    """Routes driven both ways, and what the direction costs in Wh/km.

    Reported rather than applied. The figure is a measurement of this car on
    these roads, but attributing it to elevation is an inference, and this
    audit has twice had to withdraw a conclusion drawn from a plausible
    inference over too few samples.
    """
    by_pair: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for d in drives:
        if not (d.start_location and d.end_location) or not has_valid_energy(d):
            continue
        if d.distance_km < ROUTE_MIN_KM:
            continue
        by_pair[(
            getattr(d, "start_area", "") or d.start_location,
            getattr(d, "end_area", "") or d.end_location,
        )].append(d)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key, group in by_pair.items():
        reverse = (key[1], key[0])
        # Each unordered pair once. `seen` rather than an ordering rule on the
        # key, so the direction reported as "out" is whichever the dict reached
        # first and the two rows can't disagree about which way is which.
        if key in seen or reverse in seen or reverse not in by_pair:
            continue
        back = by_pair[reverse]
        if min(len(group), len(back)) < ROUTE_MIN_TRIPS_PER_DIRECTION:
            continue
        seen.add(key)
        out_stats, back_stats = _direction_stats(group), _direction_stats(back)
        if out_stats["wh_per_km"] is None or back_stats["wh_per_km"] is None:
            continue
        speed_gap = (
            abs(out_stats["avg_speed_kmh"] - back_stats["avg_speed_kmh"])
            if out_stats["avg_speed_kmh"] and back_stats["avg_speed_kmh"] else None
        )
        label = Counter(f"{d.start_location} → {d.end_location}" for d in group)
        back_label = Counter(f"{d.start_location} → {d.end_location}" for d in back)
        out.append({
            "route": label.most_common(1)[0][0],
            "reverse_route": back_label.most_common(1)[0][0],
            "out": out_stats,
            "back": back_stats,
            "delta_wh_per_km": round(
                out_stats["wh_per_km"] - back_stats["wh_per_km"], 1),
            "speed_gap_kmh": round(speed_gap, 1) if speed_gap is not None else None,
            "comparable": speed_gap is not None and speed_gap <= ROUTE_SPEED_GAP_MAX_KMH,
        })
    out.sort(key=lambda r: abs(r["delta_wh_per_km"]), reverse=True)
    return out[:5]


def parked_awake_kw(drives: list[Any], charges: list[Any] | None,
                    capacity_kwh: float) -> float | None:
    """Draw over the first stretch after parking, before the car sleeps.

    The rate that belongs to a trimmed tail. standby_kw samples gaps of hours,
    so it measures a sleeping car and understates the minutes just after
    arrival by roughly half — applying it to a trim under-corrects by that
    much. Errand stops are the sample: park, sit twenty minutes, drive on,
    with the car awake throughout, which is exactly the window a trim covers.
    """
    return _gap_rate_kw(drives, charges, capacity_kwh,
                        AWAKE_MIN_GAP_HOURS, AWAKE_MAX_GAP_HOURS,
                        AWAKE_MIN_TOTAL_HOURS)


def vampire_drain(
    drives: list[Drive], charges: list[Charge] | None, capacity_kwh: float,
    anchor: tuple[datetime, float] | None = None,
) -> dict[str, Any]:
    """kWh lost while parked between two consecutive drives, with no charge in
    between — standby/vampire drain (sentry mode, cabin overheat protection,
    preconditioning, plain self-discharge). Not part of any single Drive's own
    energy_used_kwh, since it happens in the *gap* between trips, not during
    one — this is the only place it gets measured.

    The kWh total sums *every* charge-free gap, any duration — a 15-minute
    errand-stop still drains real energy, and excluding it would make
    trip kWh + vampire kWh fall short of total battery used. The narrative
    fields (``hours``/``gaps``/``gap_list``/``longest``) stay scoped to gaps
    at least VAMPIRE_MIN_GAP_HOURS long, though — see that constant — so
    "N parked gaps, Yh parked" reads as genuine idle stretches, not every
    red-light stop. A charge starting inside a gap invalidates it as a
    pure-drain measurement (the charge itself moved SoC upward), so that gap
    is skipped entirely (from both the kWh total and the narrative) rather
    than netted against the charge.

    ``anchor``, if given, is ``(end_time, end_soc)`` for a boundary *before*
    the first drive — typically the last charge that ended before the
    window (e.g. for a "since charge" window, that charge's own end). Without
    it, the gap before the very first drive in ``drives`` is invisible to
    this function (there's nothing earlier in the list to measure it
    against) — for a "since charge" window that's usually the single longest
    qualifying gap of all (the overnight stretch right after charging, before
    the day's first drive), so omitting the anchor there would silently drop
    most of the real parked time.

    No extrapolated "%/day" rate is reported: real standby drain is mostly
    near-zero deep-sleep punctuated by short high-drain bursts (sentry
    trigger, cabin overheat protection cooling), so a typical few-hour gap
    is disproportionately likely to catch one of those bursts and linearly
    projecting its rate to a full day systematically overstates what a full
    day parked would actually cost — there's no way to tell from a single
    short gap whether it's representative.

    Returns the aggregate (kwh/hours/gaps) plus a per-gap ``gap_list`` —
    {before_drive_id, hours, kwh, pct, start, end} for the drive that
    followed each qualifying gap — so a caller (e.g. the recent-trips list)
    can annotate "parked Xh, lost Y% before this trip" per trip, not just
    report one window-wide total. ``longest`` is the single longest
    qualifying gap ({hours, start, end}, or None) — useful on its own (e.g.
    "longest idle stretch this window") separately from the aggregate.
    """
    ordered = sorted(drives, key=lambda d: d.start_time)
    boundary = SimpleNamespace(end_time=anchor[0], end_soc=anchor[1]) if anchor else None
    chain = ([boundary] if boundary else []) + ordered
    if len(chain) < 2 or not capacity_kwh:
        return {"kwh": 0.0, "hours": 0.0, "gaps": 0, "gap_list": [], "longest": None}
    charge_starts = sorted(c.start_time for c in (charges or []))
    total_kwh = 0.0
    total_hours = 0.0
    gap_list: list[dict[str, Any]] = []
    ci = 0
    for a, b in zip(chain, chain[1:]):
        gap_start, gap_end = a.end_time, b.start_time
        gap_hours = (gap_end - gap_start).total_seconds() / 3600.0
        if gap_hours <= 0:
            continue
        while ci < len(charge_starts) and charge_starts[ci] < gap_start:
            ci += 1
        if ci < len(charge_starts) and charge_starts[ci] < gap_end:
            continue  # a charge happened in this gap — not a pure-drain measurement
        # A charge-free gap counts as parked drain even if SoC happened to
        # read unchanged — SoC is only integer precision, so a real sub-1%
        # loss (very plausible over just a short stop) doesn't necessarily
        # cross a whole point and show up here. Zero drop just means zero
        # measured *kwh* for this gap, not that it didn't happen.
        drop_pct = max(a.end_soc - b.start_soc, 0.0)
        kwh = drop_pct / 100.0 * capacity_kwh
        total_kwh += kwh
        if gap_hours < VAMPIRE_MIN_GAP_HOURS:
            continue  # too short to count toward the "parked gaps/hours" narrative
        total_hours += gap_hours
        gap_list.append({
            "before_drive_id": getattr(b, "id", None),
            "hours": round(gap_hours, 1),
            "kwh": round(kwh, 2),
            "pct": round(drop_pct, 1),
            "start": gap_start.isoformat(timespec="minutes"),
            "end": gap_end.isoformat(timespec="minutes"),
        })
    longest = max(gap_list, key=lambda g: g["hours"]) if gap_list else None
    return {
        "kwh": round(total_kwh, 2), "hours": round(total_hours, 1),
        "gaps": len(gap_list), "gap_list": gap_list,
        "longest": {"hours": longest["hours"], "start": longest["start"], "end": longest["end"]}
        if longest else None,
    }


def _speed_bucket(speed: float) -> str:
    if speed < 30:
        return "City (<30)"
    if speed < 60:
        return "Urban (30-60)"
    if speed < 90:
        return "Rural (60-90)"
    return "Highway (90+)"


def _behaviour(drives: list[Drive], total_distance: float, total_energy: float,
               effs: list[float]) -> dict[str, Any]:
    """Study the driver's own patterns and measure what each habit costs.

    Every factor is measured from this driver's data (penalty = mean Wh/km of
    the habit's drives minus the rest), so the advice is personal, not generic.
    """
    w = [d for d in drives if d.distance_km > 0]
    if len(w) < 5 or not total_distance:
        return {"available": False, "n_drives": len(w)}

    def eff(sub):
        return mean([d.wh_per_km for d in sub])

    def km_share(sub):
        return 100.0 * sum(d.distance_km for d in sub) / total_distance

    def factor(sub, rest):
        """(share of km, measured Wh/km penalty, kWh it cost in this window)."""
        if not sub or not rest:
            return 0.0, 0.0, 0.0
        pen = eff(sub) - eff(rest)
        kwh = sum(d.distance_km for d in sub) * max(pen, 0.0) / 1000.0
        return round(km_share(sub), 1), round(pen, 1), round(kwh, 2)

    fast = [d for d in w if d.max_speed_kmh > 110]
    stopgo = [d for d in w if d.avg_speed_kmh < 50
              and d.max_speed_kmh > 2.2 * d.avg_speed_kmh]
    short = [d for d in w if d.distance_km < 3]
    peak = [d for d in w if d.start_time.hour in (7, 8, 17, 18, 19)]
    hot = [d for d in w if d.outside_temp_c >= 33]

    speeding = factor(fast, [d for d in w if d not in fast])
    sg = factor(stopgo, [d for d in w if d not in stopgo])
    st = factor(short, [d for d in w if d not in short])
    pk = factor(peak, [d for d in w if d not in peak])
    ht = factor(hot, [d for d in w if d not in hot])

    # Personal-best benchmark: the driver's own most efficient quartile.
    best_q = percentile(effs, 0.25)
    overall = mean(effs)
    potential_kwh = max(0.0, total_energy - best_q * total_distance / 1000.0)
    score = round(min(100.0, 100.0 * best_q / overall)) if overall else 0

    return {
        "available": True,
        "n_drives": len(w),
        "score": score,  # 100 = typical driving matches your personal best
        "best_quartile_wh_per_km": round(best_q, 1),
        "potential_saving_kwh": round(potential_kwh, 1),
        "speeding_share_pct": speeding[0], "speeding_penalty_wh": speeding[1],
        "speeding_saving_kwh": speeding[2],
        "stopgo_share_pct": sg[0], "stopgo_penalty_wh": sg[1],
        "stopgo_saving_kwh": sg[2],
        "short_trip_share_pct": st[0], "short_trip_penalty_wh": st[1],
        "short_trip_saving_kwh": st[2],
        "peak_hour_share_pct": pk[0], "peak_hour_penalty_wh": pk[1],
        "peak_hour_saving_kwh": pk[2],
        "hot_weather_share_pct": ht[0], "hot_weather_penalty_wh": ht[1],
        "hot_weather_saving_kwh": ht[2],
    }


def eco_score(wh_per_km: float, rated_wh_per_km: float) -> int:
    """0-100 efficiency grade for a Wh/km figure against the car's rated one.

    Calibrated so ~15% below rated scores 100, exactly rated scores 85, and it
    falls ~1 point per 1% over rated — a simple, absolute driving grade that
    works per trip and per window.
    """
    if not rated_wh_per_km or wh_per_km <= 0:
        return 0
    ratio = wh_per_km / rated_wh_per_km
    return max(0, min(100, round(100 - (ratio - 0.85) * 100)))


def score_grade(score: int) -> str:
    """A / B / C / D / E band for a 0-100 score."""
    return "A" if score >= 85 else "B" if score >= 70 else \
        "C" if score >= 55 else "D" if score >= 40 else "E"


def _trip_conditions(d: Drive) -> str:
    """Route/traffic character inferred from the trip's own signals.

    The speed profile tells the story: high peak with a high average is open
    highway; high peak with a low average means congestion; a low average
    with spiky peaks is stop-go traffic. Peak-hour timing and heat are added
    as context tags.
    """
    avg, mx = d.avg_speed_kmh or 0.0, d.max_speed_kmh or 0.0
    if mx >= 90:
        base = "highway + congestion" if avg < 50 else "highway cruise"
    elif avg < 50 and mx > 2.2 * avg > 0:
        base = "stop-go traffic"
    elif avg < 40:
        base = "city driving"
    else:
        base = "steady flow"
    parts = [base]
    if d.start_time.hour in (7, 8, 17, 18, 19):
        parts.append("peak hour")
    if d.outside_temp_c >= 33:
        parts.append(f"hot {round(d.outside_temp_c)}°C")
    return " · ".join(parts)


def _data_quality(d: Drive) -> str:
    """How trustworthy this trip's efficiency figures are, so the dashboard
    can show which trips are real measurements vs a fallback estimate:
      - "measured": valid energy AND idle live-tracked while the trip was
        open — driving_wh_per_km reflects an actual observed stop, not a
        guess.
      - "estimated": valid energy but idle wasn't live-tracked (a trip
        logged before that existed, or reconstructed across an unpolled
        gap) — driving_wh_per_km falls back to the avg/max-speed heuristic.
      - "incomplete": no valid energy (a range-reading gap contaminated the
        trip) — Wh/km and cost are unavailable for it.
    """
    if not has_valid_energy(d):
        return "incomplete"
    return "measured" if getattr(d, "idle_tracked", False) else "estimated"


def _distance_flag(d: Drive) -> str | None:
    """Flags a trip whose logged odometer distance is implausibly short
    against the straight-line distance between its own stored endpoints — a
    real driven distance can never be shorter than a straight line between
    the same two points, so this catches an odometer/GPS data glitch that
    the energy math alone wouldn't reveal. None when there's nothing to
    compare (older trips with no stored coords) or the numbers are sane.
    """
    start = getattr(d, "start_coords", "") or ""
    end = getattr(d, "end_coords", "") or ""
    straight = haversine_km(start, end)
    if straight is None or straight < 0.3:   # too short to be meaningful either way
        return None
    if d.distance_km < straight * 0.9:
        return "distance_short"
    return None


def _insights(drives: list[Drive]) -> list[str]:
    """Data-driven observations from the raw drives — patterns the aggregate
    KPIs can't show. Only reports a pattern when there are enough drives on
    both sides of a comparison (>= 3) and the difference is material (>= 8%),
    so a single odd trip never masquerades as a trend."""
    out: list[str] = []
    eff = [d for d in drives if d.distance_km > 0 and has_valid_energy(d)]

    def median_whkm(subset: list[Drive]) -> float:
        return percentile([d.wh_per_km for d in subset], 0.5) if subset else 0.0

    def compare(a: list[Drive], b: list[Drive], a_name: str, b_name: str, verb: str):
        if len(a) < 3 or len(b) < 3:
            return
        ma, mb = median_whkm(a), median_whkm(b)
        if not ma or not mb:
            return
        diff = (ma - mb) / mb * 100.0
        if abs(diff) >= 8.0:
            worse, better, pct = (a_name, b_name, diff) if diff > 0 else (b_name, a_name, -diff)
            out.append(
                f"{worse.capitalize()} {verb} average {round(pct)}% more Wh/km "
                f"than {better} ({round(ma if diff > 0 else mb)} vs "
                f"{round(mb if diff > 0 else ma)})."
            )

    peak = [d for d in eff if d.start_time.hour in (7, 8, 17, 18, 19)]
    off = [d for d in eff if d.start_time.hour not in (7, 8, 17, 18, 19)]
    compare(peak, off, "peak-hour drives", "off-peak drives", "use on")

    weekend = [d for d in eff if d.start_time.weekday() >= 5]
    weekday = [d for d in eff if d.start_time.weekday() < 5]
    compare(weekend, weekday, "weekend drives", "weekday drives", "use on")

    hot = [d for d in eff if (d.outside_temp_c or 0) >= 33]
    mild = [d for d in eff if 0 < (d.outside_temp_c or 0) < 33]
    compare(hot, mild, "hot-day drives (33°C+)", "milder-day drives", "use on")

    short = [d for d in eff if d.distance_km < 5]
    longer = [d for d in eff if d.distance_km >= 5]
    compare(short, longer, "short hops (<5 km)", "longer drives", "use on")

    return out[:3]


def layered_trip_costs(
    drives: list[Drive], charges: list[Charge],
) -> dict[int, dict[str, Any]]:
    """Price each trip against the charge session that actually put that
    energy in the pack, instead of one flat rate applied to everything.

    Each completed charge pushes a layer — its own rate (cost ÷ kWh added)
    and its own kWh — onto a stack. Trips drain the most-recently-pushed
    layer first; once it's fully used up, consumption falls back to the
    layer beneath (an older charge), and so on, cascading further back for
    as long as there's no new charge. Completing a new charge always resets
    consumption to a fresh top layer, even if older layers still have kWh
    left in them.

    Returns ``{drive_id: {"cost": float|None, "parts": [...]}}``, where each
    part is ``{kwh, rate, charge_id}`` for one layer the trip drew from — so a
    trip straddling a boundary is auditable as "X kWh at one rate plus Y at
    another" instead of a single blended figure that can't be checked. A trip
    that outruns every layer in the vehicle's whole charge history (should only
    happen right at the very start of its tracked history, or if a charge
    record was deleted) gets ``cost: None`` and no parts, rather than a guessed
    rate.

    ``drives``/``charges`` must be the vehicle's FULL history in
    chronological order, not just whatever window is being displayed — an
    old trip's correct layer can depend on a charge from well before the
    window starts. Trips with no valid energy reading, or no real id, are
    left out of both the allocation and the returned mapping.
    """
    events: list[tuple[datetime, int, Any]] = []
    for c in charges:
        if c.energy_added_kwh and c.cost is not None:
            events.append((c.end_time, 0, c))  # charges settle before same-time drives
    for d in drives:
        if has_valid_energy(d) and getattr(d, "id", None) is not None:
            events.append((d.start_time, 1, d))
    events.sort(key=lambda e: (e[0], e[1]))

    # [rate, remaining_kwh, charge_id] — top of stack = most recent charge
    stack: list[list[Any]] = []
    costs: dict[int, dict[str, Any]] = {}
    for _, kind, obj in events:
        if kind == 0:
            stack.append([obj.cost / obj.energy_added_kwh, obj.energy_added_kwh, obj.id])
            continue
        need = obj.energy_used_kwh
        cost = 0.0
        parts: list[dict[str, Any]] = []
        while need > 1e-9 and stack:
            rate, remaining, charge_id = stack[-1]
            take = min(remaining, need)
            cost += take * rate
            # One entry per layer drawn from, so a trip that straddles a
            # boundary is auditable as "X kWh at one rate + Y at another"
            # rather than a single blended number nobody can check.
            parts.append({
                "kwh": round(take, 3), "rate": round(rate, 4), "charge_id": charge_id,
            })
            need -= take
            stack[-1][1] -= take
            if stack[-1][1] <= 1e-9:
                stack.pop()
        priced = need <= 1e-9
        costs[obj.id] = {
            "cost": round(cost, 2) if priced else None,
            "parts": parts if priced else [],
        }
    return costs


def analyze(drives: list[Drive], rated_wh_per_km: float = 150.0,
            capacity_kwh: float = 75.0, energy_price: float = 0.0,
            charges: list[Charge] | None = None,
            vampire_anchor: tuple[datetime, float] | None = None,
            recent_trips_limit: int | None = 5,
            trip_costs: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """``energy_price`` is either a flat RM/kWh float, or a
    ``datetime -> RM/kWh`` callable (time-of-use pricing — see app.tariff) for
    per-trip rates by when each drive happened. ``charges`` (optional) is this
    same window's charges, used only to exclude a parked gap that actually had
    a charge in it from the vampire-drain figuring below — leave it out and
    every gap between drives is assumed charge-free. ``vampire_anchor``
    (optional) is ``(end_time, end_soc)`` for a boundary before this window's
    first drive (e.g. a "since charge" window's own last charge) — see
    vampire_drain()'s docstring for why this matters. ``recent_trips_limit``
    caps how many of the most recent drives get a full ``recent_trips``
    entry — 5 by default for any window, ``None`` to list every drive (the
    caller's own "show more" affordance raises this rather than the window
    itself deciding whether to cap). ``trip_costs`` (optional), from
    layered_trip_costs() over the vehicle's FULL history, prices every trip
    and the window total/cost-per-km/by-tag breakdown; ``energy_price``
    still prices vampire drain (never tied to one trip) and is the fallback
    when trip_costs is omitted."""
    if not drives:
        return {"available": False}
    price_at = energy_price if callable(energy_price) else (lambda _dt: energy_price)

    distances = [d.distance_km for d in drives]
    durations = [d.duration_min for d in drives]
    speeds = [d.avg_speed_kmh for d in drives]
    # Efficiency-bearing drives only: a drive whose range reading was missing
    # logs 0 kWh. Including its distance (but no energy) would understate Wh/km
    # and inflate the eco score, so every efficiency/behaviour figure below is
    # computed from these — while distance/duration/counts use every drive.
    eff_drives = [d for d in drives if d.distance_km > 0 and has_valid_energy(d)]
    effs = [d.wh_per_km for d in eff_drives]
    eff_distance = sum(d.distance_km for d in eff_drives)
    eff_energy = sum(d.energy_used_kwh for d in eff_drives)

    total_distance = sum(distances)
    total_duration_h = sum(durations) / 60.0
    total_energy = sum(d.energy_used_kwh for d in drives)
    ordered = sorted(drives, key=lambda x: x.start_time)
    # Standby/vampire drain in the parked gaps *between* this window's drives
    # (sentry mode, preconditioning, plain self-discharge) — see
    # vampire_drain(). Not part of any drive's own energy_used_kwh, so it's
    # otherwise invisible; added back in below so "kWh used" is the real
    # total drawn from the pack, not just what happened while actually moving.
    vampire = vampire_drain(ordered, charges, capacity_kwh, anchor=vampire_anchor)
    vampire_kwh = vampire["kwh"]
    # Trip drain, measured PER DRIVE at its best-available precision: each
    # drive's own fractional energy_used_kwh (from its range delta — sub-1%
    # precise) OR its integer SoC drop × capacity, whichever is larger. A
    # range-reading gap logs ~0 kWh for a trip that plainly dropped whole SoC
    # points, so the integer drop rescues that trip; a normal trip's
    # fractional energy exceeds its coarse integer drop, so that wins. Taking
    # the max PER DRIVE and then summing (not max(sum_frac, sum_int) at the
    # window level) is what keeps this accurate: a window-level max silently
    # drops a data-gap trip's real drain whenever *another* trip's fractional
    # energy happens to be the larger of the two window sums — the gap trip's
    # SoC points then never surface at all.
    def _trip_kwh(d: Drive) -> float:
        integer_kwh = max(d.start_soc - d.end_soc, 0.0) / 100.0 * capacity_kwh if capacity_kwh else 0.0
        return max(d.energy_used_kwh, integer_kwh)
    # Unrounded throughout — km_per_soc and soc_used are sensitive to error
    # introduced by rounding an intermediate sum, so only the values actually
    # returned below get rounded, at the very end.
    trip_energy_used_raw = sum(_trip_kwh(d) for d in drives)
    # Gross battery energy drawn over the window — the real drain from the
    # pack, so it *includes* parking, climate-while-stopped and overnight
    # vampire loss, not just the driving energy summed per trip. (Per-trip
    # Wh/km and the Avg Efficiency figure stay driving-only; this is the "kWh
    # used" headline that should reflect everything the battery actually
    # lost.) Always exactly trip_energy_used_kwh + vampire_drain.kwh — no
    # separate max()/heuristic at this level, so the two never drift apart.
    total_energy_used_raw = trip_energy_used_raw + vampire_kwh
    soc_used = (total_energy_used_raw / capacity_kwh * 100.0) if capacity_kwh else 0.0
    # Real-world range yardstick: km per 1% of battery used, from the same
    # total (trip + vampire) — moving further per % is a real efficiency
    # signal, but so is *not* leaving it parked draining for no distance, so
    # this isn't purely a driving-efficiency number and shouldn't be read as
    # one in isolation.
    km_per_soc = round(total_distance / soc_used, 1) if soc_used >= 0.2 and total_distance else None
    # Round the total once, then derive the displayed vampire/trip split from
    # that ROUNDED total by subtraction — rounding total, vampire and trip
    # independently (e.g. 7.5, 5.25->5.2 or 5.3, 2.25->2.2) can be off by a
    # few cents at 1-decimal precision even though the raw figures agree
    # exactly; deriving one from the other guarantees they still sum exactly
    # at the precision actually shown on screen.
    total_energy_used = round(total_energy_used_raw, 1)
    vampire_kwh = round(vampire_kwh, 1)
    trip_energy_used = round(total_energy_used - vampire_kwh, 1)
    # None-id drives (unpersisted, e.g. a static-mode import) all collide on
    # the same key — excluded, since there's no way to attribute the gap to
    # one of them specifically, and a wrong attribution is worse than a
    # missing annotation.
    vampire_by_drive_id = {
        g["before_drive_id"]: g for g in vampire["gap_list"] if g["before_drive_id"] is not None
    }

    # Distribution of distance driven across speed regimes, and the measured
    # Wh/km within each. The efficiency split exists because the relationship
    # between average speed and consumption is U-shaped — stop-go crawling
    # burns more per km (acceleration losses, climate spread over little
    # distance), a moderate cruise is the sweet spot, and highway speed costs
    # again on aero drag. A single linear slope fitted across that curve
    # points the WRONG WAY when extrapolated to a crawl, so anything wanting
    # "what does this driver use at N km/h" must read the measured band here
    # rather than projecting from speed_efficiency_slope_wh_per_kmh.
    by_speed: dict[str, float] = defaultdict(float)
    band_energy: dict[str, float] = defaultdict(float)
    band_distance: dict[str, float] = defaultdict(float)
    for d in drives:
        by_speed[_speed_bucket(d.avg_speed_kmh)] += d.distance_km
        if has_valid_energy(d):
            band = _speed_bucket(d.avg_speed_kmh)
            band_energy[band] += d.energy_used_kwh
            band_distance[band] += d.distance_km
    efficiency_by_speed_band = {
        band: round(band_energy[band] * 1000.0 / km, 1)
        for band, km in band_distance.items() if km > 0 and band_energy[band] > 0
    }

    # Trips per hour-of-day and per weekday for usage patterns.
    by_hour = Counter(d.start_time.hour for d in drives)
    by_weekday = Counter(d.start_time.weekday() for d in drives)
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Average efficiency for trips starting in each hour — distance-weighted
    # (energy over distance), same convention as avg_efficiency_wh_per_km,
    # so one long trip in an otherwise-quiet hour doesn't skew it. None for
    # hours with no energy-bearing trips, so the frontend line has a real gap
    # rather than a misleading 0 Wh/km.
    eff_energy_by_hour: dict[int, float] = defaultdict(float)
    eff_distance_by_hour: dict[int, float] = defaultdict(float)
    for d in eff_drives:
        h = d.start_time.hour
        eff_energy_by_hour[h] += d.energy_used_kwh
        eff_distance_by_hour[h] += d.distance_km
    efficiency_by_hour = {
        str(h): (
            round(eff_energy_by_hour[h] * 1000.0 / eff_distance_by_hour[h], 1)
            if eff_distance_by_hour.get(h) else None
        )
        for h in range(24)
    }

    # Most frequent routes. Grouped by the coarser start/end *area* (a
    # district/suburb bucket, stable across GPS jitter between repeat visits
    # to "the same place" — the specific matched POI/building can legitimately
    # differ a few metres apart) rather than the specific location string, so
    # a real repeated route doesn't fragment into many near-duplicate
    # single-count entries. Each group still displays its most common
    # specific label, not the coarse area, so the list stays informative.
    # Rows logged before start_area/end_area existed fall back to the
    # specific location as their own grouping key.
    route_counts: Counter[tuple[str, str]] = Counter()
    route_labels: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for d in drives:
        if not (d.start_location and d.end_location):
            continue
        area_key = (
            getattr(d, "start_area", "") or d.start_location,
            getattr(d, "end_area", "") or d.end_location,
        )
        route_counts[area_key] += 1
        route_labels[area_key][f"{d.start_location} → {d.end_location}"] += 1
    routes = Counter({
        route_labels[key].most_common(1)[0][0]: count
        for key, count in route_counts.items()
    })

    # How strongly speed affects efficiency (Wh/km per km/h).
    speed_slope, _ = linregress([d.avg_speed_kmh for d in eff_drives], effs)

    # Distance-weighted window efficiency (energy-bearing drives only), and its
    # absolute driving score. Zero energy means the range reading was missing (a
    # data gap), not a real 0 Wh/km — leave efficiency and the score as unknown
    # so the UI shows "—" instead of a misleading 0 / grade E.
    window_eff = round(eff_energy * 1000.0 / eff_distance, 1) if eff_distance and eff_energy > 0 else None
    window_score = eco_score(window_eff, rated_wh_per_km) if window_eff else None

    # Blended RM/kWh actually paid across the window's priceable trips (each
    # priced at its own start_time under time-of-use), used for the window
    # cost total. Falls back to the flat rate (price_at applied to "now") when
    # there's no priceable energy yet, so a window with only a data-gap drive
    # doesn't silently show no cost.
    priced = [(d.energy_used_kwh, price_at(d.start_time)) for d in eff_drives]
    priced_energy = sum(e for e, _ in priced)
    window_price = (
        safe_div(sum(e * p for e, p in priced), priced_energy) if priced_energy
        else price_at(drives[-1].start_time)
    )

    # A trip's cost: its own charge-layer figure when trip_costs was given
    # (None if the layer stack ran dry — see layered_trip_costs), else the
    # flat/ToU rate at its own start_time.
    def _trip_cost(d: Drive) -> float | None:
        if trip_costs is not None:
            entry = trip_costs.get(getattr(d, "id", None))
            return entry.get("cost") if entry else None
        return (
            round(d.energy_used_kwh * price_at(d.start_time), 2)
            if has_valid_energy(d) and price_at(d.start_time) else None
        )

    def _trip_cost_parts(d: Drive) -> list[dict[str, Any]] | None:
        """Which charge layer(s) paid for this trip — see layered_trip_costs."""
        if trip_costs is None:
            return None
        entry = trip_costs.get(getattr(d, "id", None))
        return (entry.get("parts") or None) if entry else None

    # Per-tag totals (distance/energy/cost), keyed by whatever's in Drive.tag
    # ("" groups every untagged trip together) — the expense-claim view: how
    # much of this window's driving/cost was "work" vs "personal" etc. A trip
    # with no known cost (layer stack ran dry, not yet manually priced)
    # simply doesn't add to its tag's total rather than blanking the whole
    # tag — same "show what's known" stance as the per-trip figure.
    by_tag: dict[str, dict[str, float]] = defaultdict(lambda: {"distance_km": 0.0, "energy_kwh": 0.0, "cost": 0.0})
    for d in drives:
        row = by_tag[getattr(d, "tag", "") or ""]
        row["distance_km"] += d.distance_km
        if has_valid_energy(d):
            row["energy_kwh"] += d.energy_used_kwh
            c = _trip_cost(d)
            if c is not None:
                row["cost"] += c
    tag_totals = {
        (tag or "untagged"): {
            "distance_km": round(v["distance_km"], 1),
            "energy_kwh": round(v["energy_kwh"], 1),
            "cost": round(v["cost"], 2) if window_price else None,
        }
        for tag, v in by_tag.items()
    }

    # Window total: sum of every trip's own known cost (charge-layer or
    # manual override) plus vampire drain (never tied to one trip, so it
    # stays priced at the blended/flat rate above) — trips with no known
    # cost simply don't contribute, so the total quietly reflects only what
    # can actually be priced rather than guessing. None only when nothing in
    # the window could be priced at all.
    if trip_costs is not None:
        known_trip_cost = sum(c for d in eff_drives if (c := _trip_cost(d)) is not None)
        vampire_cost = vampire_kwh * window_price if window_price else 0.0
        priceable = known_trip_cost > 0 or vampire_cost > 0
        total_cost_out = round(known_trip_cost + vampire_cost, 2) if priceable else None
        cost_per_km_out = (
            round((known_trip_cost + vampire_cost) / total_distance, 3)
            if priceable and total_distance else None
        )
    else:
        total_cost_out = round(total_energy_used * window_price, 2) if window_price else None
        cost_per_km_out = (
            round(total_energy_used * window_price / total_distance, 3)
            if window_price and total_distance else None
        )

    return {
        "available": True,
        "total_drives": len(drives),
        "total_distance_km": round(total_distance, 1),
        "total_duration_h": round(total_duration_h, 1),
        "total_energy_kwh": round(total_energy, 1),
        # Gross drain including parking/idle/overnight (see above) — the KPI's
        # "kWh used" headline. total_energy_kwh stays the driving-only sum.
        "total_energy_used_kwh": total_energy_used,
        # The same total split into what was actually driven vs. lost while
        # parked between drives — trip_energy_used_kwh + vampire_drain.kwh
        # always sums back to total_energy_used_kwh exactly (see analyze()).
        "trip_energy_used_kwh": trip_energy_used,
        "vampire_drain": {
            "kwh": vampire_kwh,
            "hours": vampire["hours"],
            "gaps": vampire["gaps"],
            "longest": vampire["longest"],
        },
        "avg_trip_distance_km": round(mean(distances), 1),
        "avg_trip_duration_min": round(mean(durations), 1),
        "avg_speed_kmh": round(mean(speeds), 1),
        "km_per_soc_pct": km_per_soc,
        "soc_used_pct": round(soc_used, 1),
        # What the window's gross battery drain cost. Priced at the blended
        # rate actually paid across the window's trips (their own energy at
        # their own timestamps' rates) rather than a single flat number — so
        # under time-of-use pricing, a window heavy on peak-hour driving costs
        # more per kWh here than one that's mostly off-peak, matching what a
        # driver actually paid. Vampire/idle-between-trips energy (the gap
        # between total_energy_used and the driving-only sum) isn't tied to a
        # specific timestamp, so it's priced at that same blended rate.
        "total_cost": total_cost_out,
        "cost_per_km": cost_per_km_out,
        "insights": _insights(drives),
        # Only surfaced if at least one trip in the window is tagged, so an
        # account nobody ever tags doesn't grow an "untagged: everything" card.
        "by_tag": tag_totals if any(k != "untagged" for k in tag_totals) else None,
        "p95_speed_kmh": round(percentile([d.max_speed_kmh for d in drives], 0.95), 1),
        "max_speed_kmh": round(max((d.max_speed_kmh for d in drives), default=0.0), 1),
        "longest_trip_km": round(max(distances), 1),
        "distance_by_speed_band": {k: round(v, 1) for k, v in sorted(by_speed.items())},
        # Measured Wh/km per speed regime — the empirical answer to "what does
        # this driver actually use at N km/h", safe where extrapolating the
        # linear slope is not (see the U-shape note above). Bands with no
        # energy-bearing trips are simply absent.
        "efficiency_by_speed_band": dict(sorted(efficiency_by_speed_band.items())),
        "trips_by_hour": {str(h): by_hour.get(h, 0) for h in range(24)},
        "efficiency_by_hour": efficiency_by_hour,
        "trips_by_weekday": {weekdays[i]: by_weekday.get(i, 0) for i in range(7)},
        "top_routes": routes.most_common(5),
        # What driving a route the other way costs — the only handle this app
        # has on elevation (see route_asymmetry).
        "route_asymmetry": route_asymmetry(drives),
        "speed_efficiency_slope_wh_per_kmh": round(speed_slope, 3),
        # Distance-weighted (total energy over total km): one noisy short trip
        # can't skew it the way a plain mean of per-trip ratios does.
        "avg_efficiency_wh_per_km": window_eff,
        # Absolute driving score for the whole window (efficiency vs rated).
        "eco_score": window_score,
        "eco_grade": score_grade(window_score) if window_score is not None else None,
        "behaviour": _behaviour(eff_drives, eff_distance, eff_energy, effs),
        "recent_trips": [
            {
                "id": getattr(d, "id", None),
                "start_time": d.start_time.isoformat(timespec="minutes"),
                "end_time": d.end_time.isoformat(timespec="minutes"),
                "distance_km": round(d.distance_km, 1),
                "duration_min": round(d.duration_min),
                "avg_speed_kmh": round(d.avg_speed_kmh),
                "max_speed_kmh": round(d.max_speed_kmh),
                "wh_per_km": round(d.wh_per_km) if has_valid_energy(d) else None,
                "energy_kwh": round(d.energy_used_kwh, 2) if has_valid_energy(d) else None,
                "driving_wh_per_km": (
                    # Gross minus the climate load, modelled across the WHOLE
                    # trip rather than over sustained stops only. Climate runs
                    # while the car moves just as much as while it sits, and
                    # gating it on idle meant stop-go traffic — frequent stops,
                    # each too short to count — had nothing stripped at all, so
                    # this figure came out equal to the gross exactly when it
                    # was most worth having. Needs no speed heuristic either,
                    # so legacy trips get a real figure instead of a fallback.
                    driving_wh_val := sync_mod.driving_only_wh_per_km(
                        d.energy_used_kwh, d.distance_km, d.duration_min,
                        d.outside_temp_c, getattr(d, "climate_min", None))
                    if has_valid_energy(d) else None
                ),
                # Propulsion-only energy for this drive, the counterpart to
                # driving_wh_per_km (≈ Tesla's "Driving" energy-breakdown line).
                # NB the *gross* energy_kwh is what matches Tesla's "Current
                # Drive" total, which includes climate/idle; this strips that
                # out. Derived from the same driving Wh/km so the two agree;
                # equals the gross energy when no idle was found.
                "driving_energy_kwh": (
                    round(driving_wh_val * d.distance_km / 1000.0, 2)
                    if has_valid_energy(d) and driving_wh_val else
                    (round(d.energy_used_kwh, 2) if has_valid_energy(d) else None)
                ),
                "eco_score": eco_score(driving_wh_val, rated_wh_per_km) if has_valid_energy(d) and driving_wh_val else None,
                # What this trip's energy cost — its own charge-layer figure
                # (or a manual override) when trip_costs was given, else the
                # flat/ToU tariff at its own start_time. None when the charge
                # history can't price it yet (see layered_trip_costs) and no
                # override has been set — the UI offers a manual entry then.
                "cost": _trip_cost(d) if has_valid_energy(d) else None,
                # Which charge(s) actually paid for this trip: one entry per
                # layer drawn from, so a trip spanning the end of a free
                # charge shows the split rather than one blended figure.
                "cost_parts": _trip_cost_parts(d) if has_valid_energy(d) else None,
                "cost_source": (
                    ("manual" if getattr(d, "cost_override", None) is not None else "auto")
                    if trip_costs is not None and has_valid_energy(d) and _trip_cost(d) is not None
                    else None
                ),
                "conditions": _trip_conditions(d),
                # "measured" (real tracked idle) / "estimated" (heuristic
                # fallback) / "incomplete" (no valid energy) — how much to
                # trust this trip's efficiency figures.
                "data_quality": _data_quality(d),
                # Set only when the odometer distance is implausibly short
                # against the trip's own stored endpoints — an odometer/GPS
                # glitch, independent of the energy math.
                "distance_flag": _distance_flag(d),
                # User-assigned category ("work"/"personal"/...); "" = untagged.
                "tag": getattr(d, "tag", "") or "",
                # Seconds this trip's stop time was back-dated (see
                # Drive.tail_trim_sec) — surfaced so a trip whose duration
                # reads short against the car's own screen can be checked for a
                # clipped tail instead of the answer being unknowable. Affects
                # duration/avg_speed only: distance and energy are measured
                # from the real reading regardless of the recorded timestamp.
                # None on trips logged before this was recorded.
                "tail_trim_sec": getattr(d, "tail_trim_sec", None),
                # Distance driven before this trip's start anchor, missing from
                # distance_km (see Drive.start_lost_km) — the other end of the
                # same question tail_trim_sec answers.
                "start_lost_km": getattr(d, "start_lost_km", None),
                # And after the closing anchor (see Drive.end_lost_km). A trip
                # that reads short on distance but not on energy points here
                # rather than at the start anchor, which loses both together.
                "end_lost_km": getattr(d, "end_lost_km", None),
                # What the departure recovery pulled back in, which is what
                # tells a "nothing was lost" 0.0 apart from a "the recovery
                # reclaimed it" 0.0 (see Drive.start_recovered_km).
                "start_recovered_km": getattr(d, "start_recovered_km", None),
                # Where the two anchors sat on the odometer, so a trip can be
                # reconciled against the readings around it without re-deriving
                # its position from every trip before it.
                "start_odo_km": getattr(d, "start_odo_km", None),
                "end_odo_km": getattr(d, "end_odo_km", None),
                # How wide the polling window was at each boundary — the
                # trip's own uncertainty there (see Drive.start_gap_sec).
                "start_gap_sec": getattr(d, "start_gap_sec", None),
                "end_gap_sec": getattr(d, "end_gap_sec", None),
                "route": f"{d.start_location} → {d.end_location}"
                if d.start_location and d.end_location else "",
                # Raw endpoints, so the UI can offer "name this place" (a
                # geofence) without a separate lookup. Empty for rows logged
                # before coords were stored.
                "start_coords": getattr(d, "start_coords", "") or "",
                "end_coords": getattr(d, "end_coords", "") or "",
                # Live directions link (Google Maps start -> end) when the raw
                # endpoints were kept; empty for rows logged before coords
                # were stored.
                "map_url": (
                    "https://www.google.com/maps/dir/?api=1"
                    f"&origin={getattr(d, 'start_coords', '').replace(' ', '')}"
                    f"&destination={getattr(d, 'end_coords', '').replace(' ', '')}"
                    if getattr(d, "start_coords", "") and getattr(d, "end_coords", "")
                    else None
                ),
                # % of the battery this trip drew. start_soc/end_soc come from
                # Tesla's integer battery_level, so their delta is whole-number
                # only — useless at 1 decimal. When the trip has valid energy
                # (from the fractional range delta) derive the % from that
                # instead, giving true sub-1% precision; fall back to the
                # integer delta only when energy is unknown (a range gap).
                "soc_used_pct": (
                    round(d.energy_used_kwh / capacity_kwh * 100.0, 1)
                    if has_valid_energy(d) and capacity_kwh
                    else round(max(d.start_soc - d.end_soc, 0.0), 1)
                ),
                # The parked gap immediately before this trip, if it was long
                # enough and charge-free to count as vampire drain (see
                # vampire_drain()) — None when this is the first drive in the
                # window, the gap was too short, or a charge happened in it.
                "vampire_before": vampire_by_drive_id.get(getattr(d, "id", None)),
            }
            for d in sorted(drives, key=lambda x: x.start_time, reverse=True)[:recent_trips_limit]
        ],
    }
