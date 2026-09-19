"""Driving pattern analysis."""
from __future__ import annotations

import json as _json

from bisect import bisect_left, bisect_right
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

# How far the odometer may move across a "parked" gap before the gap was not a
# park at all.
#
# Two rows being consecutive in the drive table does not make the time between
# them still: a journey nobody recorded leaves no row, and the gap either side
# of it closes over the top. Its SoC drop is then read as standby drain —
# which is a car's whole consumption charged to sitting in a car park. This
# app knows that happens and by how much: /api/telemetry/compare reported
# 9.985 unaccounted kilometres across a fortnight.
#
# The odometer is what settles it, because it counts whether or not anything
# was listening. The tolerance is a kilometre rather than the 0.15 above: an
# arrival the stream lost can leave the closing trip several hundred metres
# short (trip 535 lost 338 m, trip 731 lost 245 m), and that ground belongs
# to the trip rather than to a journey nobody saw.
PARKED_GAP_MAX_MOVE_KM = 1.0


def _gap_moved_km(a: Any, b: Any) -> float | None:
    """How far the odometer moved between two trips, or None if it cannot say.

    Zero is treated as no reading rather than as a reading of zero: it is what
    a row carries when the odometer was never recorded, and a pair of them
    would otherwise prove every gap was a park.
    """
    end_odo = getattr(a, "end_odo_km", None)
    start_odo = getattr(b, "start_odo_km", None)
    if not end_odo or not start_odo:
        return None
    return float(start_odo) - float(end_odo)


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

    A trip with no parked reading before the next one started cannot be
    judged at all — there is nothing to compare ``end_odo_km`` against — and
    that is a different fact from a trip that WAS compared and matched.
    Folding both into the same silent "no gap" would be exactly the failure
    this file keeps finding elsewhere: a missing case reported as a confident
    zero. So the two are kept apart in the return value; a caller that only
    reads ``gaps`` still behaves as before, but one that ignores ``unchecked``
    is choosing to, not being unable to tell the difference.
    """
    ordered = [d for d in sorted(drives, key=lambda d: d.start_time)
               if getattr(d, "end_odo_km", None) is not None]
    if not ordered:
        return {"available": False, "gaps": [], "unchecked": [], "unattributed_km": 0.0}

    def _unchecked_entry(d: Any) -> dict[str, Any]:
        return {
            "drive_id": getattr(d, "id", None),
            "route": f"{d.start_location} → {d.end_location}"
            if d.start_location and d.end_location else "",
            "end_time": d.end_time.isoformat(timespec="minutes"),
        }

    if not readings:
        # No readings anywhere in the window — every trip here is unchecked,
        # not confirmed clean. The old version of this returned early with
        # nothing in `unchecked` either, which was the same silent zero this
        # function exists to avoid, one case earlier.
        return {"available": False, "gaps": [],
                "trips_checked": len(ordered),
                "unchecked": [_unchecked_entry(d) for d in ordered][-10:],
                "unattributed_km": 0.0}
    obs = sorted(
        ((r.ts, r.odo_km) for r in readings if getattr(r, "odo_km", None) is not None),
        key=lambda x: x[0],
    )
    if not obs:
        return {"available": False, "gaps": [],
                "trips_checked": len(ordered),
                "unchecked": [_unchecked_entry(d) for d in ordered][-10:],
                "unattributed_km": 0.0}

    out: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []
    total = 0.0
    for i, d in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        # Readings taken while parked after this trip: after it stopped, and
        # before the next one set off. The highest is where the car came to
        # rest, whatever the trip recorded.
        until = nxt.start_time if nxt else None
        resting = [(t, o) for t, o in obs
                   if t >= d.end_time and (until is None or t <= until)]
        if not resting:
            # No evidence either way — most often a short trip followed by the
            # car sleeping before the next watchdog poll landed, which Fleet
            # Telemetry made routine now that polling only happens at all to
            # check on a car the stream has gone quiet on.
            unchecked.append(_unchecked_entry(d))
            continue
        seen = max(o for _, o in resting)
        missing = seen - d.end_odo_km - (getattr(d, "end_lost_km", None) or 0.0)
        if missing <= CONTINUITY_TOLERANCE_KM:
            continue
        total += missing
        next_claimed = (getattr(nxt, "start_recovered_km", None) or 0.0) if nxt else 0.0
        out.append({
            "drive_id": getattr(d, "id", None),
            # The trip that most likely holds this ground now, because its
            # departure recovery reached back over it. Reported so a finding
            # is directly actionable: repair-trip-boundary needs both ids and
            # the odometer the boundary belongs at, and all three are here.
            "next_drive_id": getattr(nxt, "id", None) if nxt else None,
            "boundary_odo_km": round(seen, 3),
            "route": f"{d.start_location} → {d.end_location}"
            if d.start_location and d.end_location else "",
            "end_time": d.end_time.isoformat(timespec="minutes"),
            "recorded_end_odo_km": round(d.end_odo_km, 1),
            "observed_odo_km": round(seen, 1),
            # WHEN the car was first seen at that reading, and when it was
            # last seen still at the recorded one. A parked car's odometer
            # cannot creep, so the movement happened between these two — and
            # the only way to judge a large finding is to know when to
            # remember. Measured: 1.82 km somewhere in an overnight park is a
            # different question depending on whether it moved at 19:30 or
            # 02:00.
            "observed_at": min(t for t, o in resting
                               if o >= seen - 0.001).isoformat(timespec="minutes"),
            "last_at_recorded": (
                max((t for t, o in resting if o <= d.end_odo_km + 0.001),
                    default=None).isoformat(timespec="minutes")
                if any(o <= d.end_odo_km + 0.001 for _, o in resting) else None),
            "unrecorded_km": round(missing, 2),
            # How much of this ground the NEXT trip has already claimed as its
            # own blind departure head. A reading taken while the car was
            # rolling out of its bay — before the app had declared the trip
            # started — lands inside the "resting" window above and reads as
            # ground the ARRIVING trip failed to record. It is the opposite:
            # ground the DEPARTING trip drove, and already knows it drove.
            #
            # Measured, trip 456: 0.22 km of it, against a start_recovered_km
            # of 0.214 on trip 457. The car's own trip meters settle which
            # trip owns it — 44.0 km for 456, which the recorded 43.94 already
            # matches, and 19.8 for 457, which the recorded 19.2 does not.
            # Moving the boundary would have made both figures worse.
            "claimed_by_next_km": round(next_claimed, 3),
            "claimed_by_departure": missing - next_claimed <= CONTINUITY_TOLERANCE_KM,
        })
    return {
        "available": True,
        "gaps": out[-10:],
        "trips_checked": len(ordered),
        "unchecked": unchecked[-10:],
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
# Read this before changing any of the three constants below, or before
# trusting what they produce for a place where Sentry is off.
#
# Fifty gaps and 538 parked hours, grouped by whether a BatteryReading inside
# the gap saw Sentry armed:
#
#   sentry on        9 gaps   101 h   34 pts   231 W
#   sentry off      18 gaps   174 h    8 pts    32 W
#   unknown         23 gaps   264 h   18 pts    47 W
#
# 7.3x, and the cleanest split in this dataset. It is also the SAME split the
# places make — Home and Office are where Sentry is off — so the per-place
# rate already carries it and a second mechanism would be doing one job twice.
# The Sentry-on figure is a real measurement: 34 points is fifty times the
# quantum and nothing here is rounding.
#
# The Sentry-off figure is where this stops being measurement. The car's own
# parked screen put everything since one charge at 0.4% — 0.274 kWh across
# ~19.6 parked hours, about 14 W, with every category but Vehicle Standby at
# zero. Against that:
#
#   - a 10-hour night at 14 W is 0.20 of an SoC point, so roughly one night in
#     five should read 1 point and the rest 0;
#   - the seven Home nights to 24 Aug read 1, 1, 1, 2, 1, 1, 1;
#   - the night of 23-24 Aug read a whole point, 0.686 kWh, on its own more
#     than the car attributed to that entire window.
#
# Seven from seven is not a 0.20 probability. Something adds close to a point
# per overnight park that is not drain, and the likeliest candidate is the
# pack's own SoC estimate settling as it cools: the arriving reading is taken
# on a warm pack and the departing one after a night of cooling. That is a
# systematic offset, not noise, and no amount of averaging removes it — which
# is why the 22 older Home gaps read 33 W with twelve of them at zero while
# the seven newest read 69 W with none at zero.
#
# So: SENTRY-ON rates are measured, SENTRY-OFF rates are an upper bound and
# probably mostly drift. Do not "improve" the off figure by collecting more
# gaps. What would settle it is the car's Park screen read at both ends of ONE
# overnight park, which measures the same quantity at 0.1% instead of 1%.
# Drives needed on BOTH sides before a habit's cost is worth reporting. The
# penalty is a difference of two means, so a lopsided split measures the
# smaller sample rather than the habit.
FACTOR_MIN_DRIVES = 3

# Below this a gap between two trips is not a park at all.
#
# Mirrors sync.SHADOW_SETTLE_SEC, which is how long the car must sit in P
# before the trip machine will call a journey finished: a three-point turn's
# P-R-D, a drive-through window, dropping someone at the door. If that is not
# long enough to END a trip, it is not long enough to be parked time between
# two of them, and the same number should decide both.
#
# Not a cosmetic floor. Measured live: a gap of under three minutes carried a
# whole 0.14% gauge step and came out at 0.870 kW — four times what a parked
# car draws with Sentry on — because the percentage is quantised and the hours
# are not. One such gap put SE at "0.14% of the battery over 0 h in 1 park",
# a rate built entirely from where a rounding step happened to land.
#
# Kept as a local constant rather than imported from sync: this module is pure
# analysis and importing the poller to read one float would invert that. The
# comment is the link, and the two are checked against each other by a test.
PARKED_MIN_GAP_HOURS = 180.0 / 3600.0

STANDBY_MIN_GAP_HOURS = 6.0
# Two overnight parks. Raised with the floor: at 6 h a 12 h total could be a
# single gap, and one gap has never been a rate anywhere else in this module.
STANDBY_MIN_TOTAL_HOURS = 24.0
# A sanity floor for the matrix's fit, and no more than that. It pools EVERY
# parked gap rather than only the long ones, so hours alone stopped being the
# thing worth testing — see STANDBY_SNR_MIN, which is what actually decides.
STANDBY_ALL_MIN_TOTAL_HOURS = 24.0

# How far a parked rate must stand above the gauge's own rounding before it is
# reported at all.
#
# A flat hours threshold was the wrong shape and the arithmetic says so. Each
# end of a gap is read to a whole SoC point, so a gap's drop carries about 0.41
# points of quantisation error; over N independent gaps that grows as 0.41*sqrt(N)
# while the real drain grows with the HOURS. So what matters is not how many
# hours there are but how few gaps they arrive in:
#
#    144 h in 12 parks of 12 h  ->  8.40 points of drain, 1.42 of noise  S/N 5.9
#    144 h in 72 parks of  2 h  ->  8.40 points of drain, 3.48 of noise  S/N 2.4
#
# Identical hours, identical drain, identical rate — and one of them is a
# measurement while the other is mostly rounding. Any single hours figure therefore passes some histories it should
# refuse and refuses others it should pass, which is exactly what a constant
# chosen by judgement does when the quantity it stands in for is available.
#
# The quantity IS available, so the test is the quantity: report the rate when
# it is at least this many times its own noise. It makes the threshold
# self-adjusting in the right direction — a history of long overnight parks
# qualifies sooner than one of the same hours in errand stops, because it is
# genuinely better evidence — and it needs no judgement about how many hours a
# car ought to spend parked.
#
# Three, because at S/N 3 the rate is within roughly a third of itself, which is
# the point where the figure starts being worth acting on. A lower bar would
# report numbers whose sign is safe but whose value is not.
STANDBY_SNR_MIN = 3.0


def _noise_points(gaps: int) -> float:
    """Quantisation error, in SoC points, expected across ``gaps`` readings.

    Each end of a gap is read to a whole point, so its error is roughly uniform
    over half a point either way: standard deviation 0.5/sqrt(3) = 0.289, and a
    DIFFERENCE of two such readings carries sqrt(2) times that, about 0.41.
    Independent across gaps, so the sum grows as the square root of the count.
    """
    return 0.41 * (gaps ** 0.5) if gaps > 0 else 0.0


def _noise_kw(gaps: int, hours: float, capacity_kwh: float) -> float | None:
    """The same error expressed as a rate, for comparing against a fitted one."""
    if not gaps or not hours or not capacity_kwh:
        return None
    return round(_noise_points(gaps) / 100.0 * capacity_kwh / hours, 4)
# Outside this the answer is a measurement artifact, not a parked car — a
# Tesla idles somewhere near 100-500 W depending on Sentry, climate and how
# long it takes to fall asleep.
STANDBY_PLAUSIBLE_KW = (0.02, 1.5)
# How far SoC may read HIGHER at the end of a parked gap than at its start
# before the gap is thrown out as something other than a park. One whole point
# of the pack's own estimate wandering overnight is ordinary; anything past
# that is energy arriving from outside.
SOC_RISE_TOLERANCE_PCT = 1.0


# There was a parked_awake_kw here, and the reasoning behind it still stands:
# the first stretch after arriving is a different animal from the hours that
# follow, with the car awake and drawing several times what it settles to.
#
# It is gone because that rate CANNOT BE MEASURED from this data, not because
# the physics is wrong. It sampled gaps of 0.15-2 h needing 6 h in total,
# while one whole-percent SoC point is ~0.7 kWh — around eighteen hours of
# parked drain on this car. Six hours of aggregate is under half a point, so
# the fit read almost pure rounding, and _gap_rate_kw's max(drop, 0) keeps the
# upward halves of that noise while clipping the downward ones.
#
# Measured live: it returned 0.348 kW while the car's own screen put ALL
# parked drain since the last charge at 2.0%, about 0.034 kW. Ten times over,
# stated confidently, and it had already been wired into short-gap vampire
# drain before anyone checked it against the car.
#
# Deleted rather than left unused, because a well-documented function that
# returns a plausible number from unmeasurable input is a trap for the next
# caller. Bring it back when a finer SoC source exists to fit it from.


def _gap_totals(drives: list[Any], charges: list[Any] | None,
                min_gap_h: float, max_gap_h: float | None,
                place: str | None = None,
                keep: Any = None, signed: bool = False) -> tuple[float, float, int]:
    """SoC points drained, hours parked and gaps counted, over one gap set.

    Split out from _gap_rate_kw so that a rate and the evidence behind it come
    from ONE definition of "a qualifying parked gap". The decomposition below
    needs the hours as well as the rate — to say why a row is blank, and to
    weigh armed parks against unarmed ones — and a second loop applying the
    same seven predicates by hand is exactly how two fits drift apart while
    both look right.

    Points rather than kWh: the pack's quantum is the unit every comment in
    this module reasons in, and capacity is a scale factor the caller applies.
    """
    ordered = sorted(drives, key=lambda d: d.start_time)
    if len(ordered) < 2:
        return 0.0, 0.0, 0
    charge_starts = sorted(c.start_time for c in (charges or []))
    total_points = 0.0
    total_hours = 0.0
    total_gaps = 0
    for a, b in zip(ordered, ordered[1:]):
        if place is not None and getattr(a, "end_location", None) != place:
            continue
        gap_start, gap_end = a.end_time, b.start_time
        gap_hours = (gap_end - gap_start).total_seconds() / 3600.0
        if gap_hours < min_gap_h or (max_gap_h is not None and gap_hours >= max_gap_h):
            continue
        # Asked after the duration band, not before it: ``keep`` is the one
        # predicate here that costs anything to evaluate (it goes looking
        # through the readings), and most gaps in a full history are short
        # errand stops the band throws out anyway. Both are pure tests of the
        # same gap, so the order changes only what gets asked, never the answer.
        if keep is not None and not keep(a, b):
            continue
        # A charge anywhere inside the gap moved SoC upward, so its endpoints
        # say nothing about drain. Scanned per gap rather than with a marching
        # index: the bands skip gaps, so a shared cursor would fall behind.
        if any(gap_start < c < gap_end for c in charge_starts):
            continue
        # And the car has to have stood still. A journey nobody recorded
        # leaves no row for this loop to see, so the gap closes over it and
        # its consumption is read as standby — which is how a rate fitted
        # from parked cars ends up describing driving.
        moved = _gap_moved_km(a, b)
        if moved is not None and moved > PARKED_GAP_MAX_MOVE_KM:
            continue
        # ...and the charge log is not the only way to learn that. A gap whose
        # SoC came out HIGHER than it went in did not measure drain either,
        # whatever the log says: energy went in from somewhere.
        #
        # Measured, 30 July: a gap at the Office read -68 points across 8.1
        # hours with no charge logged inside it. max(drop, 0) below scored
        # that as zero drain and kept all 8.1 hours in the denominator, so a
        # missed charge quietly diluted the rate rather than being excluded
        # like every other charge. Dropping the gap outright is what the
        # charge test above would have done had it seen it.
        #
        # The tolerance is a whole point because the pack's own estimate can
        # wander one either way overnight; a real charge clears it by orders
        # of magnitude, so nothing is riding on where exactly it sits.
        if b.start_soc - a.end_soc > SOC_RISE_TOLERANCE_PCT:
            continue
        drop = a.end_soc - b.start_soc
        # ``signed`` is the difference between a fit that can pool SHORT parks
        # and one that cannot.
        #
        # Clipping at zero is right for a handful of long gaps, where a
        # negative reading means something went wrong. It is fatal once short
        # gaps are included: a one-hour park drains a twentieth of an SoC
        # point, so its reading is rounding, and rounding scatters both ways.
        # Keeping the upward halves in full while truncating the downward ones
        # RECTIFIES that noise — it has a mean, and the mean is not the truth.
        # That, not the pooling, is what made parked_awake_kw read 0.348 kW
        # against the car's own 0.034 and got it deleted.
        #
        # Summed signed, the rounding cancels instead of accumulating: error
        # grows as the square root of the gap count while signal grows with the
        # hours, so hundreds of parked hours resolve a rate that six could not.
        # The gaps a negative reading should genuinely disqualify — a charge
        # nobody logged — are already gone, thrown out above by the
        # SOC_RISE_TOLERANCE_PCT test, which only lets sub-point noise through.
        total_points += drop if signed else max(drop, 0.0)
        total_hours += gap_hours
        total_gaps += 1
    return total_points, total_hours, total_gaps


def _rate_kw(points: float, hours: float, capacity_kwh: float,
             min_total_h: float) -> float | None:
    """Points and hours into a kW rate, with the minimum and the band applied.

    The one place those two refusals live. Both the fits and the decomposition
    that explains them go through here, so a rate either module reports has
    passed the same tests — and neither can start reporting one that has not.
    """
    if not capacity_kwh or hours < min_total_h:
        return None
    total_kwh = points / 100.0 * capacity_kwh
    if total_kwh <= 0:
        return None
    rate = total_kwh / hours
    lo, hi = STANDBY_PLAUSIBLE_KW
    return round(rate, 3) if lo <= rate <= hi else None


def _rate_refusal(points: float, hours: float, capacity_kwh: float,
                  min_total_h: float, gaps: int = 0) -> str | None:
    """Why _rate_kw said None, or None if it did not. Same tests, same order.

    A blank rate has three quite different causes and they call for three
    different responses from a reader: wait, look at the gauge, or distrust the
    fit. Collapsing them into one dash sent a dashboard showing 1,182 parked
    hours the message "not enough parked history", which is the one explanation
    that was definitely false.

    Kept beside _rate_kw rather than folded into it so every existing caller
    keeps its plain float-or-None return, and deliberately re-running the same
    conditions in the same order: a reason that can disagree with the decision
    it explains is worse than no reason.
    """
    if not capacity_kwh:
        return "no usable capacity for this car yet"
    if hours < min_total_h:
        return (f"only {hours:.0f} h of parked time — needs {min_total_h:.0f} h "
                f"before a rate means anything")
    total_kwh = points / 100.0 * capacity_kwh
    if total_kwh <= 0:
        return ("these parks show no net drain — the gauge reads to 1%, and "
                "across them it did not move")
    rate = total_kwh / hours
    lo, hi = STANDBY_PLAUSIBLE_KW
    if not lo <= rate <= hi:
        return (f"the fit came out at {rate:.3f} kW, outside the {lo}-{hi} kW "
                f"a parked car can plausibly draw")
    noise = _noise_kw(gaps, hours, capacity_kwh)
    if noise and rate < STANDBY_SNR_MIN * noise:
        return (f"{rate:.3f} kW is under {STANDBY_SNR_MIN:g}x the "
                f"{noise:.3f} kW the 1% gauge steps alone would produce across "
                f"{gaps} parks — more hours, or fewer and longer ones, would "
                f"separate it")
    return None


def _gap_rate_kw(drives: list[Any], charges: list[Any] | None, capacity_kwh: float,
                 min_gap_h: float, max_gap_h: float | None,
                 min_total_h: float, place: str | None = None,
                 keep: Any = None) -> float | None:
    """Average draw (kW) across the parked gaps falling in a duration band.

    Shared by the two rates that matter — the deep-sleep average and the
    just-parked one — because they differ only in which gaps they look at, and
    letting them drift apart in method would make them incomparable.

    ``place`` restricts the fit to gaps that began where a trip ENDED there,
    which turns out to matter more than the duration band does — see
    place_standby_kw.
    """
    points, hours, _ = _gap_totals(drives, charges, min_gap_h, max_gap_h,
                                   place=place, keep=keep)
    return _rate_kw(points, hours, capacity_kwh, min_total_h)


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


# Sentry states in which the car is actually watching, and drawing for it.
#
# Read off this car's own transitions rather than assumed. A park goes
# Off -> Idle at the moment it stops, Idle -> Armed about two and a half
# minutes later, and back Armed -> Idle when the driver returns — so Idle is
# Sentry enabled and NOT yet watching, and it is also where a car sits for the
# whole of a park in an excluded location. Aware is the car having noticed
# something and Panic is the alarm going off, both of which are Armed and then
# some. Quiet is armed with the siren suppressed, which changes what the car
# does about an intrusion and not what it spends watching for one.
SENTRY_ARMED_STATES = frozenset({
    "SentryModeStateArmed", "SentryModeStateAware",
    "SentryModeStatePanic", "SentryModeStateQuiet",
})


def sentry_armed(state: Any, flag: Any) -> bool | None:
    """Was Sentry actually watching, from whichever of the two a row carries.

    The state wins where it exists, because the boolean cannot answer this:
    it is true for every state but Off, so it calls a car in Idle armed. That
    is not a rounding error in the parked-drain fit — Idle is precisely the
    state a car sits in where Sentry is excluded, so the parks that should
    define the UNARMED rate were the ones being counted as armed.

    The boolean is the fallback, and has to be: it is all that exists on every
    row written before the state was recorded, and on anything polling wrote.
    A gap spanning the change is therefore mixed, and mixed in a known
    direction — the older rows over-report armed.
    """
    if state:
        return str(state) in SENTRY_ARMED_STATES
    return flag


class SentryIndex:
    """Sentry states sorted by timestamp, for asking about one gap at a time.

    gap_sentry_state below used to walk every reading to answer one gap, and
    sentry_standby_kw asks it once per gap in the car's whole history. At 287
    drives and 2,795 readings that is 800,000 comparisons to fit one rate, and
    the dashboard fits it twice per load — which is where the seconds were
    going. Sorted once, each question costs a pair of bisects plus the readings
    that actually fall inside the gap.
    """

    __slots__ = ("_ts", "_state")

    def __init__(self, readings: list[Any]):
        rows = sorted(
            ((r.ts, sentry_armed(getattr(r, "sentry_state", None),
                                 r.sentry_mode))
             for r in readings if getattr(r, "ts", None) is not None),
            key=lambda pair: pair[0])
        self._ts = [ts for ts, _ in rows]
        self._state = [st for _, st in rows]

    @classmethod
    def from_sorted(cls, pairs) -> "SentryIndex":
        """Build from (ts, sentry_mode) pairs the DATABASE has already ordered.

        The same index, skipping the two things that cost at this size: a
        Python sort of the car's whole reading history, and an attribute
        lookup per row to get at two values the query already returns in
        order. Index access instead, because a row here is a plain pair.

        The caller owns the ordering claim. Hand it unordered pairs and every
        bisect below silently answers the wrong question, so this is only for
        a query that says ORDER BY.
        """
        # Materialised first. Both comprehensions below walk it, so handed a
        # generator the second one found it already spent: every timestamp
        # present, every state gone, and an index that answers "nobody knows"
        # to every question it is asked. Caught by the equivalence test rather
        # than by anything going wrong.
        rows = list(pairs)
        index = cls.__new__(cls)
        index._ts = [pair[0] for pair in rows]
        index._state = [pair[1] for pair in rows]
        return index

    def __len__(self) -> int:
        return len(self._ts)

    def state(self, start, end) -> bool | None:
        lo = bisect_left(self._ts, start)
        hi = bisect_right(self._ts, end)
        seen = [st for st in self._state[lo:hi] if st is not None]
        return any(seen) if seen else None

    def around(self, start, end) -> dict[str, Any]:
        """The nearest reading either side of a gap, for diagnosing an unread one.

        A park with no reading INSIDE it cannot be attributed, and there are
        three quite different reasons for that which this tells apart:

        - nothing either side within hours: the car was not streaming, and
          nothing can be done about that park, ever;
        - a reading seconds before the gap opens: the recording works and the
          BOUNDARY is off, which is a bug and fixable;
        - readings far either side but none within: an ordinary quiet park that
          predates the state being recorded at all.

        Guessing which of those it is from the outside is how a "fix" gets
        written for the wrong one, so this reports rather than infers.
        """
        out: dict[str, Any] = {"before": None, "after": None}
        lo = bisect_left(self._ts, start)
        if lo > 0:
            out["before"] = {
                "sec": round((start - self._ts[lo - 1]).total_seconds()),
                "armed": self._state[lo - 1],
            }
        hi = bisect_right(self._ts, end)
        if hi < len(self._ts):
            out["after"] = {
                "sec": round((self._ts[hi] - end).total_seconds()),
                "armed": self._state[hi],
            }
        return out


def _sentry_index(readings: Any) -> SentryIndex:
    """A SentryIndex over ``readings``, or ``readings`` itself if already one.

    Callers that ask about many gaps build the index once and hand it down;
    ones that ask about a single gap can keep passing a plain list.
    """
    return readings if isinstance(readings, SentryIndex) else SentryIndex(readings)


def gap_sentry_state(readings: list[Any], start, end) -> bool | None:
    """Was Sentry armed during this parked gap? None when nothing said.

    /api/sync writes a BatteryReading on any sentry_mode change, so a park that
    armed or disarmed leaves one even where SoC never moves a whole point.
    Absent readings mean the car was unreachable throughout — usually a
    signal-dead car park — and the answer is unknown, not "off".

    Accepts either a plain list of readings or a SentryIndex over them.
    """
    return _sentry_index(readings).state(start, end)


def sentry_standby_kw(drives: list[Any], charges: list[Any] | None,
                      capacity_kwh: float, readings: list[Any],
                      armed: bool) -> float | None:
    """The standby fit restricted to parks whose Sentry state was ``armed``.

    Place was doing this job by proxy, and a proxy is exactly what it was:
    Home and Office are where Sentry is off, everywhere else is where it is on,
    and the per-place rates duplicated the split so faithfully that a second
    mechanism looked redundant. Trip 448 parked at the resort — a 220 W place
    fitted entirely from armed parks — with Sentry off. Fifteen hours later the
    gap was priced at 220 W, which put the modelled drain past a whole SoC
    point, which made the code report the raw point instead: 0.69 kWh, more
    than the car attributed to every park since its last charge.

    The state is the cause and the place only correlates with it, so where the
    state is known it wins. Armed is the half worth fitting — 34 SoC points
    across nine parks, fifty times the quantum. The unarmed half is the one
    Place.parked_draw_w exists for.
    """
    if not readings:
        return None
    index = _sentry_index(readings)
    return _gap_rate_kw(
        drives, charges, capacity_kwh,
        STANDBY_MIN_GAP_HOURS, None, STANDBY_MIN_TOTAL_HOURS,
        keep=lambda a, b: index.state(a.end_time, b.start_time) is armed)


def window_accounting(drives: list[Any], charges: list[Any] | None,
                      readings: list[Any], since: Any, until: Any) -> dict[str, Any]:
    """Where the window's hours went, so the matrix adds up to something.

    The driving rows and the parked rows have always described DIFFERENT
    POPULATIONS, and that is what stopped the table summing. Every trip in the
    window becomes a driving row, while the parked rows are rates fitted from
    gaps of six hours and up needing twenty-four in total — so the parked rows
    were never "what standing still cost this window", they were the asymptotic
    deep-sleep rate, and the majority of a normal day's parked time (errand
    stops of a quarter-hour to three hours) appeared in neither.

    This walk places EVERY hour between ``since`` and ``until`` in exactly one
    bucket, so the report can say where the time went:

        driving      the trips themselves
        parked       stationary, no charge inside, bounded by two trips
        charging     a gap with a charge in it — stationary, but not drain
        excluded     a gap no rate may be applied to: the odometer says the car
                     moved (a journey nobody recorded), or SoC came out higher
                     than it went in. Real hours, not attributable ones.
        unbounded    the window's two edges — before the first trip and after
                     the last. Real time with no pair of trips bracketing it,
                     so nothing measured it.

    Its rules are deliberately NOT _gap_totals's. That helper's exclusions
    exist to protect a RATE, and silently dropping hours is the right thing for
    a fit and the wrong thing for an accounting — an hour a fit ignores still
    happened. So the same conditions appear here as buckets rather than as
    skips, and the totals close.
    """
    ordered = sorted(drives, key=lambda d: d.start_time)
    index = _sentry_index(readings) if readings else None
    charge_starts = sorted(c.start_time for c in (charges or []))

    drive_hours = sum(float(getattr(d, "duration_min", 0.0) or 0.0) / 60.0
                      for d in ordered)
    drive_kwh = sum(float(getattr(d, "energy_used_kwh", 0.0) or 0.0)
                    for d in ordered)
    by_state = {"sentry_off": 0.0, "sentry_on": 0.0, "unknown": 0.0}
    gaps_by_state = {"sentry_off": 0, "sentry_on": 0, "unknown": 0}
    parked_hours = charging_hours = excluded_hours = 0.0
    parked_gaps = charging_gaps = excluded_gaps = 0
    short_hours = 0.0
    short_gaps = 0
    short_by_state = {"sentry_off": 0.0, "sentry_on": 0.0, "unknown": 0.0}

    for a, b in zip(ordered, ordered[1:]):
        hours = (b.start_time - a.end_time).total_seconds() / 3600.0
        # Too short to be a park — see PARKED_MIN_GAP_HOURS. Not counted
        # anywhere, because it is not time the car spent parked; it is the
        # seam between two halves of one journey.
        if hours < PARKED_MIN_GAP_HOURS:
            continue
        if any(a.end_time < c < b.start_time for c in charge_starts):
            charging_hours += hours
            charging_gaps += 1
            continue
        moved = _gap_moved_km(a, b)
        if (moved is not None and moved > PARKED_GAP_MAX_MOVE_KM) or (
                b.start_soc - a.end_soc > SOC_RISE_TOLERANCE_PCT):
            excluded_hours += hours
            excluded_gaps += 1
            continue
        parked_hours += hours
        parked_gaps += 1
        armed = index.state(a.end_time, b.start_time) if index else None
        key = ("sentry_on" if armed else
               "sentry_off" if armed is False else "unknown")
        by_state[key] += hours
        gaps_by_state[key] += 1
        # The hours the old six-hour rule could never see, kept apart so the
        # effect of admitting them is measurable rather than assumed.
        #
        # The question this exists to answer: a Sentry state is read from the
        # readings falling INSIDE a gap, and a short gap is less likely to
        # contain one — so pooling short parks may grow the unknown bucket
        # faster than it grows ID. Unknown hours sit in PK and, by subtraction,
        # in SE, which would push ID down and SE up for a reason that is about
        # observation rather than about the car. If that is happening it will
        # show here as short hours concentrated in "unknown".
        if hours < STANDBY_MIN_GAP_HOURS:
            short_hours += hours
            short_gaps += 1
            short_by_state[key] += hours

    span_hours = (until - since).total_seconds() / 3600.0 if since and until else 0.0
    # The edges, by subtraction rather than by measuring them: everything
    # between the first trip's start and the last one's end is already placed
    # above, so whatever the span has left over is the two ends.
    placed = drive_hours + parked_hours + charging_hours + excluded_hours
    return {
        "span_hours": round(span_hours, 1),
        "driving": {"hours": round(drive_hours, 1), "kwh": round(drive_kwh, 2),
                    "trips": len(ordered)},
        "parked": {"hours": round(parked_hours, 1), "gaps": parked_gaps,
                   "by_state": {k: round(v, 1) for k, v in by_state.items()},
                   "gaps_by_state": dict(gaps_by_state),
                   "short": {"hours": round(short_hours, 1), "gaps": short_gaps,
                             "under_hours": STANDBY_MIN_GAP_HOURS,
                             "by_state": {k: round(v, 1)
                                          for k, v in short_by_state.items()}}},
        "charging": {"hours": round(charging_hours, 1), "gaps": charging_gaps},
        "excluded": {"hours": round(excluded_hours, 1), "gaps": excluded_gaps},
        "unbounded": {"hours": round(max(span_hours - placed, 0.0), 1)},
    }


def parked_share(drives: list[Any], charges: list[Any] | None,
                 capacity_kwh: float, readings: list[Any],
                 anchor: tuple[datetime, float] | None = None) -> dict[str, Any]:
    """How much battery the window's parking actually ate, split by Sentry state.

    The question the codes were invented to answer, and it is a SUM rather than
    a fit: add up what the gauge lost across every parked gap, and attribute
    each gap to the Sentry state it was in. SoC points are already percent, so
    the headline needs no capacity, no rate, and no model.

    That is why this exists beside parked_decomposition rather than being
    derived from it. A rate is a generalisation — what an hour of parking costs
    in general — and it has to earn that with a plausibility band, a minimum,
    and a signal-to-noise test, any of which can refuse and leave the row
    blank. A total does not generalise and so owes none of it: there is always
    an answer to "what did this month cost", and the only honest addition is
    how much of it is the gauge's own rounding.

    The split is DIRECTLY MEASURED here, which is the other reason to prefer
    it. In the rate model SE is a residual, PK minus ID, so parks whose Sentry
    state nothing recorded land in it by subtraction and inflate it. Here every
    gap is attributed to the state it was actually in, and the ones nothing
    recorded get their own line instead of being quietly charged to Sentry:

        PK = ID + SE + unknown

    exactly, as a sum of measurements, with nothing standing in for anything.

    ``anchor``, if given, is ``(end_time, end_soc)`` for a boundary *before*
    the first drive — the same shape vampire_drain() takes, for the same
    reason. Without it, the parked stretch before the window's first drive is
    invisible to this function, because the loop below only ever looks
    BETWEEN two drives it already has. For a since-charge window that gap is
    typically the overnight stretch right after the charge itself — often the
    single longest one in the window — and vampire_drain() has been receiving
    an anchor for it since the two cards' windows were aligned; this function
    was not, and a reader comparing the two still saw them disagree by
    whatever that one gap was worth.
    """
    ordered = sorted(drives, key=lambda d: d.start_time)
    boundary = SimpleNamespace(end_time=anchor[0], end_soc=anchor[1]) if anchor else None
    chain = ([boundary] if boundary else []) + ordered
    index = _sentry_index(readings) if readings else None
    charge_starts = sorted(c.start_time for c in (charges or []))
    states = ("sentry_off", "sentry_on", "unknown")
    pts = {k: 0.0 for k in states}
    hrs = {k: 0.0 for k in states}
    gaps = {k: 0 for k in states}
    unread: list[dict[str, Any]] = []

    for a, b in zip(chain, chain[1:]):
        hours = (b.start_time - a.end_time).total_seconds() / 3600.0
        if hours < PARKED_MIN_GAP_HOURS:
            continue
        # The same three exclusions window_accounting makes, so the hours here
        # are the hours it calls parked and the two reports cannot disagree.
        if any(a.end_time < c < b.start_time for c in charge_starts):
            continue
        moved = _gap_moved_km(a, b)
        if (moved is not None and moved > PARKED_GAP_MAX_MOVE_KM) or (
                b.start_soc - a.end_soc > SOC_RISE_TOLERANCE_PCT):
            continue
        armed = index.state(a.end_time, b.start_time) if index else None
        key = ("sentry_on" if armed else
               "sentry_off" if armed is False else "unknown")
        if key == "unknown":
            # Named, not just counted. "?? is still there" is unanswerable
            # from a bare total: it cannot say whether one old park is ageing
            # out of the window or every new one is still going unread.
            row_gap = {
                "at": a.end_time.isoformat(timespec="minutes"),
                "hours": round(hours, 2),
                "place": getattr(a, "end_location", None),
                "after_drive_id": getattr(a, "id", None),
            }
            if index is not None:
                row_gap["nearest"] = index.around(a.end_time, b.start_time)
            unread.append(row_gap)
        # Signed, for the reason _gap_totals is: clipping each gap at zero
        # rectifies the rounding, and a total built from rectified noise reads
        # high by exactly the half it threw away.
        pts[key] += a.end_soc - b.start_soc
        hrs[key] += hours
        gaps[key] += 1

    def row(keys: tuple[str, ...]) -> dict[str, Any]:
        points = sum(pts[k] for k in keys)
        hours = sum(hrs[k] for k in keys)
        count = sum(gaps[k] for k in keys)
        out = {
            # SoC points ARE percent, so this is the measurement itself.
            "pct": round(points, 2),
            # What the 1% gauge steps alone would contribute. Not a gate here,
            # only a caveat: a total is worth reporting even when the rounding
            # is a large part of it, so long as it says so.
            "pct_noise": round(_noise_points(count), 2) or None,
            "hours": round(hours, 1),
            "gaps": count,
            "kwh": round(points / 100.0 * capacity_kwh, 2) if capacity_kwh else None,
        }
        # The rate, where the hours can carry one — and only there. Derived
        # from the same sum, so it can never disagree with the total above it.
        #
        # Gated on the noise for the reason the fitted rate is, and the screen
        # showed why: one park of 6.6 minutes carrying a single 0.14% gauge
        # step reported 0.870 kW, four times what a parked car draws with
        # Sentry armed. The percentage there is a measurement of something
        # small; the RATE is that measurement divided by a tenth of an hour,
        # which multiplies the rounding by ten rather than averaging it away.
        #
        # The percent stays either way. It is what was actually measured, and a
        # total is worth reporting with its error beside it. A rate is a claim
        # about what an hour costs, and has to earn that.
        out["kw_noise"] = _noise_kw(count, hours, capacity_kwh)
        rate = (round(points / 100.0 * capacity_kwh / hours, 4)
                if hours and capacity_kwh and points > 0 else None)
        if rate is not None and out["kw_noise"] and rate < STANDBY_SNR_MIN * out["kw_noise"]:
            rate = None
        out["kw"] = rate
        return out

    total = row(states)
    return {
        "total": total,
        "sentry_off": row(("sentry_off",)),
        "sentry_on": row(("sentry_on",)),
        "unknown": row(("unknown",)),
        # Which parks could not be attributed, and what was seen near them.
        # Newest first, because the question asked of this list is always
        # "are the RECENT ones still unread?"
        "unread_parks": list(reversed(unread))[:20],
        # Stated rather than left to be checked: the three parts are measured
        # separately and must come back to the whole.
        "reconciles": abs(total["pct"] - sum(
            round(pts[k], 2) for k in states)) < 0.011,
    }


def parked_decomposition(drives: list[Any], charges: list[Any] | None,
                         capacity_kwh: float,
                         readings: list[Any]) -> dict[str, Any]:
    """The parked bill split into bare idling and what Sentry adds: PK = ID + SE.

    Three figures over one gap set, so they are arithmetic rather than three
    opinions:

        PK  every qualifying parked gap in the window, whatever Sentry did.
            The bill — what the car actually cost standing still.
        ID  the gaps Sentry was measurably OFF for. The floor: what leaving
            the car somewhere costs when nothing is watching.
        SE  PK minus ID. Everything in the bill that bare idling does not
            explain.

    SE is a RESIDUAL, and that is the honest description of it. It is not the
    Sentry-armed fit — that figure is reported beside it as ``armed_kw`` for
    corroboration, and the two answer different questions. armed_kw is what an
    armed hour costs; SE is what arming it cost *this window*, which depends on
    how much of the window was armed. An owner who never arms Sentry has an
    armed_kw and an SE of zero, and only the second of those is the truth
    about their month.

    Being a residual also means SE carries the window's unknown-state parks:
    gaps where no reading said either way land in PK and in neither ID nor the
    armed fit, so their excess over ID shows up here. ``hours`` reports them
    rather than burying them, and the direction is knowable — an unreachable
    car is usually an underground car park away from home, which is where
    Sentry is armed. Read a large unknown share as "SE is an upper bound".

    SE can come out negative, and is reported that way. It does not mean
    Sentry gives energy back; it means the unarmed gaps in this window drained
    faster than the window as a whole, so the split has not separated yet. The
    unarmed half is the shakier one — see sentry_standby_kw, where a night's
    drain sits a fifth of the way to one SoC point and the pack's own estimate
    wanders about that far on its own.

    Every rate here passes the same minimum and plausibility band as any other
    fit in this module, so any of the three can come back None independently:
    with no unarmed parks yet, PK stands and ID and SE do not.
    """
    index = _sentry_index(readings) if readings else None

    def armed_is(want: bool | None):
        """A gap-keeper for one Sentry verdict, or None to take every gap."""
        if want is None or index is None:
            return None
        return lambda a, b: index.state(a.end_time, b.start_time) is want

    def totals(want: bool | None):
        # EVERY parked gap, not only the long ones, and summed signed so the
        # short ones can be included at all. This is the same gap set
        # window_accounting calls "parked", which is what lets the energy column
        # be measured over the hours it reports rather than projected onto them.
        return _gap_totals(drives, charges, PARKED_MIN_GAP_HOURS, None,
                           keep=armed_is(want), signed=True)

    all_points, all_hours, all_gaps = totals(None)
    off_points, off_hours, off_gaps = totals(False) if index else (0.0, 0.0, 0)
    on_points, on_hours, on_gaps = totals(True) if index else (0.0, 0.0, 0)

    def rate(points: float, hours: float, gaps: int) -> float | None:
        """The fit, reported only where it stands clear of its own rounding."""
        fitted = _rate_kw(points, hours, capacity_kwh, STANDBY_ALL_MIN_TOTAL_HOURS)
        if fitted is None:
            return None
        noise = _noise_kw(gaps, hours, capacity_kwh)
        if noise and fitted < STANDBY_SNR_MIN * noise:
            return None
        return fitted

    pk = rate(all_points, all_hours, all_gaps)
    idle = rate(off_points, off_hours, off_gaps)
    armed = rate(on_points, on_hours, on_gaps)
    # How much of the answer is the gauge's own resolution. Each end of a gap is
    # read to a whole SoC point, so a gap's drop carries about 0.41 points of
    # quantisation error; summed over independent gaps that grows as the square
    # root of their count while the drain grows with the hours. Reported rather
    # than hidden behind a threshold: a rate that is not several times this is
    # not yet a measurement, and the reader can see which it is.
    def noise(gaps: int, hours: float) -> float | None:
        return _noise_kw(gaps, hours, capacity_kwh)
    return {
        "pk_kw": pk,
        "id_kw": idle,
        "pk_noise_kw": noise(all_gaps, all_hours),
        "id_noise_kw": noise(off_gaps, off_hours),
        # Why a blank row is blank, in the row's own terms.
        "pk_why": _rate_refusal(all_points, all_hours, capacity_kwh,
                                STANDBY_ALL_MIN_TOTAL_HOURS, all_gaps),
        "id_why": _rate_refusal(off_points, off_hours, capacity_kwh,
                                STANDBY_ALL_MIN_TOTAL_HOURS, off_gaps),
        # The 6-hour deep-sleep rate, kept beside the all-hours one because they
        # are different quantities and both are wanted. This is the figure
        # sync.py subtracts from real trip energy, and the gap between the two
        # is the premium a car draws while still awake — screens up, Sentry
        # arming — which is exactly what pooling short parks is meant to catch.
        "deep_sleep_kw": _rate_kw(
            *_gap_totals(drives, charges, STANDBY_MIN_GAP_HOURS, None)[:2],
            capacity_kwh, STANDBY_MIN_TOTAL_HOURS),
        # Rounded from the two rounded figures on purpose: the row shows
        # PK and ID to the same three places, and a residual that does not
        # subtract to what the reader can see would be read as a third fit.
        "se_kw": None if pk is None or idle is None else round(pk - idle, 3),
        "armed_kw": armed,
        "gaps": {"total": all_gaps, "sentry_off": off_gaps,
                 "sentry_on": on_gaps,
                 "unknown": all_gaps - off_gaps - on_gaps},
        "hours": {"total": round(all_hours, 1),
                  "sentry_off": round(off_hours, 1),
                  "sentry_on": round(on_hours, 1),
                  "unknown": round(all_hours - off_hours - on_hours, 1)},
        "min_gap_hours": STANDBY_MIN_GAP_HOURS,
        "min_total_hours": STANDBY_MIN_TOTAL_HOURS,
    }


def place_standby_kw(drives: list[Any], charges: list[Any] | None,
                     capacity_kwh: float, place: str | None) -> float | None:
    """The same standby fit, restricted to parks at ONE place.

    A single whole-history rate turned out to describe nowhere this car
    actually parks. Across 42 gaps and 450 parked hours:

        Home        23 gaps   256 h   13 points   0.035 kW
        Office       9 gaps    84 h    5 points   0.041 kW
        elsewhere    9 gaps   101 h   34 points   0.230 kW

    Six and a half times, and not noise — the split is Sentry Mode, which is
    excluded at home and work and armed everywhere else. The blended 0.079 kW
    the whole-history fit returns is 2.3x too high for the places this car
    spends most of its nights and 3x too low for the rest.

    That blend is what made an overnight park read wrong. vampire_drain
    reports the fitted rate instead of a measured SoC point only while the
    modelled drain is under one point, and 0.079 kW across 9.2 hours comes to
    0.727 kWh against a 0.684 quantum — just over, so trip 409 reported a
    whole point, 0.68 kWh, where the car's own screen said 0.137. At Home's
    own 0.035 the same gap models 0.32 kWh and the substitution fires.

    None when this place has too little history to carry its own figure, which
    the caller should read as "use the whole-history rate": Home, Office and
    the resort all clear the 24-hour minimum comfortably, while a hotel stayed
    at once does not.

    Note what this still cannot do. One point at Home's rate spreads over 19.7
    hours, so no SINGLE overnight park here is measurable — the 13 points are
    real only as a sum across 256 hours. The rate is a measurement; any one
    gap it is applied to is not.
    """
    if not place:
        return None
    return _gap_rate_kw(drives, charges, capacity_kwh,
                        STANDBY_MIN_GAP_HOURS, None, STANDBY_MIN_TOTAL_HOURS,
                        place=place)


def parked_rate_kw(drives: list[Any], charges: list[Any] | None,
                   capacity_kwh: float) -> float | None:
    """The rate to charge a short parked stretch at, in kW.

    Minutes, not hours, and through them the car is still awake — screens up,
    Sentry arming — drawing roughly twice what it settles to once asleep. So
    parked_awake_kw is the right rate and standby_kw is only the fallback: it
    under-corrects by about half, but under-correcting beats not correcting,
    and until enough errand stops have accumulated it is the only rate this
    car's history can support.

    One definition, because both ends of the same correction depend on it: the
    minutes taken OFF a trip (a trimmed tail, or a departure gap's parked part)
    and the same minutes added back to the parked gap that should carry them.
    Two rates would leave energy created or destroyed at the boundary.

    parked_awake_kw was preferred here and no longer is, because it cannot be
    measured from this data. Its band is 0.15-2 h and it needs only 6 h of gap
    time in total, while one whole-percent SoC point is 0.7 kWh — around 18
    hours of parked drain on this car. Six hours of aggregate is under half a
    point, so the fit is reading almost pure rounding, and _gap_rate_kw's
    max(drop, 0) clips the downward halves of that noise while counting the
    upward ones in full. Rectified noise has a mean, and it is not the truth.
    Measuring 0.034 kW against a 0.7 kWh quantum needs on the order of a
    hundred parked hours, not six.

    Measured live: the awake fit returned 0.348 kW while the car's own screen
    put ALL parked drain since the last charge at 2.0% — about 0.034 kW across
    the window. Ten times over, and confidently so, which is worse than the
    noisy zero it replaced.

    standby_kw samples gaps of 6 h and up, needs 24 h in total, and carries a
    plausibility band. Still thin, but an order of magnitude better placed
    against the quantum. Its docstring's rule holds here too: None means do
    not correct, never substitute a guess.
    """
    return standby_kw(drives, charges, capacity_kwh)


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
        # A round trip inside one area is its own reverse, so it would be
        # compared against itself for a guaranteed zero — a row saying nothing,
        # occupying one of the five.
        if key == reverse:
            continue
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


def vampire_drain(
    drives: list[Drive], charges: list[Charge] | None, capacity_kwh: float,
    anchor: tuple[datetime, float] | None = None,
    rate_history: tuple[list[Any], list[Any]] | None = None,
    place_rates: dict[str, float] | None = None,
    readings: list[Any] | None = None,
    frozen: dict[str, Any] | None = None,
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
    # What to FIT the parked rate from, as against what to report on. A car's
    # standby draw is a property of the car, not of whichever window is on
    # screen — and the fit needs 24 hours of qualifying gaps, which a short
    # window simply does not contain.
    #
    # Measured, trip 415: an overnight park at Home reported a whole SoC
    # point, 0.68 kWh, where Home's own measured 0.035 kW over those 10.7
    # hours models 0.37. The substitution below never ran, because the window
    # was "since last charge" — six trips — and no rate could be fitted from
    # it at all. The same gap in a 30-day window would have been corrected.
    # A figure that changes with the dropdown is not measuring the car.
    fit_drives, fit_charges = rate_history or (drives, charges)
    # Indexed once for the whole call. Three things below ask what Sentry was
    # doing during a gap — the armed-parks fit, and the per-gap annotations
    # twice — and each of them would otherwise re-sort the readings.
    readings = _sentry_index(readings) if readings else None
    ordered = sorted(drives, key=lambda d: d.start_time)
    boundary = SimpleNamespace(end_time=anchor[0], end_soc=anchor[1]) if anchor else None
    chain = ([boundary] if boundary else []) + ordered
    if len(chain) < 2 or not capacity_kwh:
        return {"kwh": 0.0, "hours": 0.0, "gaps": 0, "gap_list": [], "longest": None}
    charge_starts = sorted(c.start_time for c in (charges or []))
    # Fitted lazily: the add-back below needs it only when some trip gave
    # drain up, and the short-gap substitution only when a gap is too brief to
    # measure. Windows with neither shouldn't pay for the fit.
    _rate: dict[Any, float | None] = {}
    # A fit that no longer has evidence behind it falls back to what the same
    # fit said while it did. Consulted only where the live fit returns None —
    # never as an override — because a live fit that resolves has cleared the
    # same 24-hour threshold and is current, and the frozen figure is by
    # definition older. This exists for one situation: the history the fit was
    # measured from has been deleted (see /api/data/purge-pre-telemetry), and
    # the alternative is not a fresher number but no number at all.
    frozen_rates = frozen or {}
    frozen_places = frozen_rates.get("places") or {}

    def park_rate(place: str | None = None, armed: bool | None = None) -> float | None:
        """What this car draws parked HERE, in the state it was actually in.

        Sentry first, because it is the cause and everything else correlates
        with it: armed parks measure 231 W and unarmed ones a fraction of that,
        and place was standing in for the distinction until a resort park with
        Sentry off was priced at the resort's armed rate (see
        sentry_standby_kw).

        Then the place — a figure read off the car's own Park screen if it has
        one, since that measures the same quantity at 0.1% where the fit has 1%
        and a temperature bias on top (Place.parked_draw_w), else the place's
        own fit. Then the whole-history blend, which fits no regime exactly but
        sits between them, and beats declining to correct a gap at all.
        """
        if armed:
            key = ("sentry", True)
            if key not in _rate:
                _rate[key] = (sentry_standby_kw(
                    fit_drives, fit_charges, capacity_kwh, readings, True)
                    if readings else None)
                if _rate[key] is None:
                    _rate[key] = frozen_rates.get("sentry_armed_kw")
            if _rate[key]:
                return _rate[key]
        if place not in _rate:
            given = (place_rates or {}).get(place) if place else None
            if given:
                _rate[place] = given
            elif place:
                _rate[place] = (
                    place_standby_kw(fit_drives, fit_charges, capacity_kwh, place)
                    or frozen_places.get(place))
            else:
                _rate[place] = (parked_rate_kw(fit_drives, fit_charges, capacity_kwh)
                                or frozen_rates.get("whole_history_kw"))
        if _rate[place] is None and place is not None:
            return park_rate(None)
        return _rate[place]
    # SoC is stored to whole percent and nothing finer exists at a trip
    # boundary, so one point is the smallest drain a gap can express.
    soc_point_kwh = capacity_kwh / 100.0
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
        # Nor is a gap the car drove through. Same reasoning as the charge
        # above: the endpoints stop describing standby the moment something
        # else moved the SoC between them, and an unrecorded journey moves it
        # a great deal more than a night of standby does.
        moved = _gap_moved_km(a, b)
        if moved is not None and moved > PARKED_GAP_MAX_MOVE_KM:
            continue
        # A charge-free gap counts as parked drain even if SoC happened to
        # read unchanged — SoC is only integer precision, so a real sub-1%
        # loss (very plausible over just a short stop) doesn't necessarily
        # cross a whole point and show up here. Zero drop just means zero
        # measured *kwh* for this gap, not that it didn't happen.
        drop_pct = max(a.end_soc - b.start_soc, 0.0)
        kwh = drop_pct / 100.0 * capacity_kwh
        # ...and the same integer precision reads far too HIGH on a short gap,
        # which nothing guarded. One SoC point is 0.7 kWh here, while a parked
        # car draws about 0.04 kW — so it takes roughly 18 hours to move a
        # single point, and any gap shorter than that resolves the drain no
        # better than "0 or 0.7 kWh". Where it lands is rounding, not
        # measurement, and rounding that lands on 1 or 2 points is reported as
        # real energy.
        #
        # Measured against the car's own screen: a 1.7-hour parked gap read 2
        # points and was reported as 1.39 kWh — more, on its own, than the
        # 1.3% (0.90 kWh) the car attributed to ALL parked drain since the
        # last charge, a window holding that gap and many others.
        #
        # So when the expected drain over a gap is under one SoC point, the
        # measurement carries no information about it and the fitted rate is
        # strictly the better estimator. Longer gaps keep their measurement:
        # the 18.5-hour park above read 1 point, 0.70 kWh, against 0.71
        # modelled — at that length the two agree and the reading wins.
        #
        # The rate is itself fitted from gaps long enough to measure (see
        # parked_rate_kw), so this substitutes measurement at a scale that
        # works for measurement at a scale that doesn't. It is not a guess
        # standing in for data.
        #
        # Only over the hours the SoC drop actually SPANS, which is not the
        # whole gap when the trip after it recovered its baseline: then
        # b.start_soc is the pre-gap reading, the drop covers nothing, and the
        # add-back below is what accounts for those minutes. Substituting
        # across the full gap and then adding them again charges the same
        # minutes twice — measured on trip 451, a 1.9 h park at Home with 113
        # of its 114 minutes recovered, reported at 0.05 kWh where 14 W over
        # 1.9 h is 0.027. Exactly double, because park_min was nearly the
        # whole gap.
        park_min = getattr(b, "start_park_min", None) or 0.0
        measured_hours = max(gap_hours - park_min / 60.0, 0.0)
        if measured_hours > 0:
            rate = park_rate(getattr(a, "end_location", None),
                             gap_sentry_state(readings, gap_start, gap_end)
                             if readings else None)
            if rate and rate * measured_hours < soc_point_kwh:
                kwh = rate * measured_hours
                drop_pct = kwh / capacity_kwh * 100.0
        # Drain the following trip gave up, put back where it belongs. When
        # b's departure was recovered across a blackout its start_soc is the
        # PRE-gap reading, so the drop above stops there while this gap's own
        # clock runs on to b.start_time. Those minutes (Drive.start_park_min)
        # had their drain subtracted from b's energy — real standby that would
        # otherwise be in neither, making trip kWh + vampire kWh fall short of
        # the battery actually used, which is the very thing this function
        # counts every short gap to avoid.
        #
        # Recomputed rather than stored, so a small residual survives: the rate
        # here is fitted to the window's history, the one that did the
        # subtracting to the history at sync time. Over <=45 minutes a 20%
        # difference between them is ~0.03 kWh, against the ~0.17 being
        # restored. Second-order, and cheaper than a column that exists only
        # to bookkeep.
        park_rate_kw = park_rate(
            getattr(a, "end_location", None),
            gap_sentry_state(readings, gap_start, gap_end) if readings else None
        ) if park_min > 0 else None
        if park_min > 0 and park_rate_kw:
            kwh += park_rate_kw * park_min / 60.0
            drop_pct = kwh / capacity_kwh * 100.0   # keep pct saying what kwh says
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
        """(share of km, measured Wh/km penalty, kWh it cost in this window).

        Both sides need enough drives to average, which is the same bar
        _insights already applies to every comparison it reports. A habit
        covering 86% of the window leaves a handful of drives to compare it
        against, and the difference of two means is then mostly the smaller
        sample: measured live, stop-go traffic over 86% of the kilometres came
        out 96 Wh/km CHEAPER than "the rest", which is not a finding about
        stop-go traffic, it is three drives.
        """
        if len(sub) < FACTOR_MIN_DRIVES or len(rest) < FACTOR_MIN_DRIVES:
            return round(km_share(sub), 1) if sub else 0.0, 0.0, 0.0
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


# The plain-language word for each of the matrix's own codes — one voice,
# not two. This used to run its own average/peak heuristic (mx >= 90 read as
# "highway cruise"), independent of drive_mode, and the two could and did
# disagree ON THE SAME TRIP: an 11.7 km town hop that briefly touched
# 107 km/h read "highway cruise" here while the matrix correctly called it
# Slow City, because 107 for a few seconds is not a sustained highway pace —
# drive_mode requires the AVERAGE to clear a highway bar too, this heuristic
# never checked one. A reader seeing "SC · highway cruise" on one line has no
# way to tell which of the two the app actually believes, which is worse than
# either verdict alone.
#
# What each code is CALLED, read once here rather than typed twice — this
# used to be its own paraphrase ("highway cruise" for CH, "steady flow" for
# CC…), a second wording for the same five names MODE_NAMES already carries.
# The matrix table's own row and this sentence disagreeing on what to CALL a
# code was the same "two voices" problem as disagreeing on WHICH code, one
# layer further in: a reader could see "CH" paired with "Constant Highway" in
# the matrix and "highway cruise" here, two labels for one thing, neither
# wrong. Lower-cased because this reads mid-sentence, not as a heading.
MODE_NAMES = {
    "FH": "Fast Highway",
    "CH": "Constant Highway",
    "CC": "Constant City",
    "SC": "Slow City",
    "HC": "Heavy City",
}
_COND_BASE = {code: name.lower() for code, name in MODE_NAMES.items()}


def _trip_conditions(mode: str | None, d: Drive) -> str:
    """Route/traffic character, in the SAME words the Driving matrix uses.

    ``mode`` is drive_mode's own verdict for this trip (see MODE_NAMES /
    MATRIX_DEFINITIONS) — passed in rather than re-derived here, for the same
    reason drive_mode_explained's code is read from drive_mode instead of
    reimplemented: two readings of one classification can drift apart, one
    reading cannot. Peak-hour timing and heat are still this function's own
    observation, layered on afterwards — they are context, not a competing
    classification.
    """
    parts = [_COND_BASE[mode]] if mode in _COND_BASE else []
    if d.start_time.hour in (7, 8, 17, 18, 19):
        parts.append("peak hour")
    if (d.outside_temp_c or 0.0) >= 33:
        parts.append(f"hot {round(d.outside_temp_c)}°C")
    return " · ".join(parts)


# Share of a trip's distance that may be inferred before it stops counting as
# a measurement. A labelling threshold, not a physical one: boundary recovery
# of a few hundred metres on a 10 km trip is still a measured trip, while one
# reconstructed for the most part is not, whatever its idle tracking says.
INFERRED_SHARE_MAX = 0.10


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

    DISTANCE counts too, and for a long time it did not. A trip whose start
    was recovered across a blackout, or whose arrival was estimated, carries
    ground no poll ever saw — its energy over that stretch is projected from
    the rest, not read. Measured live and the reason this changed: trip 397
    recovered 7.078 km of a 10.339 km drive, had its energy replaced by hand
    from the car's own screen, and still reported "measured".

    So a trip is only "measured" if most of its distance actually was. The
    threshold is a labelling choice, not a physical constant — a tenth is
    small enough that ordinary boundary recovery of a few hundred metres
    still reads as measured, and large enough that a trip mostly reconstructed
    cannot.
    """
    if not has_valid_energy(d):
        return "incomplete"
    # A priced trip is never "measured", whatever the rest of it looks like.
    # This is the one case the checks below cannot see: distance, duration and
    # the idle record can all be perfect on a trip whose battery reading never
    # arrived, so without the flag a figure this app invented would present
    # itself as one the car reported.
    if getattr(d, "energy_estimated", False):
        return "estimated"
    distance = getattr(d, "distance_km", 0.0) or 0.0
    inferred = ((getattr(d, "start_recovered_km", None) or 0.0)
                + (getattr(d, "end_est_km", None) or 0.0))
    if distance > 0 and inferred / distance > INFERRED_SHARE_MAX:
        return "estimated"
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
            trip_costs: dict[int, dict[str, Any]] | None = None,
            vampire_rate_history: tuple[list[Any], list[Any]] | None = None,
            vampire_place_rates: dict[str, float] | None = None,
            vampire_readings: list[Any] | None = None,
            vampire_frozen: dict[str, Any] | None = None,
            mode_cuts: dict[str, float] | None = None,
            ) -> dict[str, Any]:
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
    when trip_costs is omitted. ``vampire_rate_history`` (optional) is the
    vehicle's FULL (drives, charges) for fitting its parked-draw rate, which
    is a property of the car rather than of this window — see vampire_drain();
    omitted, the window fits its own rate and a short one may fit none.
    ``mode_cuts`` (optional) are the same tunable cut-points the driving
    matrix reads (see resolved_cuts) — passed through so a recent trip's
    own condition tag agrees with the row it landed in there, rather than
    each reading its own copy of the thresholds and drifting apart when
    one is retuned."""
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
    vampire = vampire_drain(ordered, charges, capacity_kwh, anchor=vampire_anchor,
                            rate_history=vampire_rate_history,
                            place_rates=vampire_place_rates,
                            readings=vampire_readings,
                            frozen=vampire_frozen)
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
        """A trip's energy, rescued from an under-read where one is possible.

        The max() is for POLLED trips. There, energy is inferred from rated
        range read minutes apart, and a reading that arrives stale or missing
        makes a trip look cheaper than the pack's own SoC says it was — so the
        SoC drop is taken as a floor.

        It must not apply to a STREAMED trip. There, energy is a subtraction of
        the car's own EnergyRemaining counter, good to about 0.02 kWh, while
        the SoC drop is quantised to a whole point — 0.69 kWh on this pack. So
        the floor is coarser than the thing it is protecting, and whenever the
        rounding happens to land above the measurement it replaces a good
        number with a worse one. Always upward, never down.

        Measured on eight streamed trips: 21.9 kWh against a measured 20.8,
        which is 1.1 kWh of pure rounding — and it is why the dashboard's
        "battery used" and the driving matrix's total disagreed by 5% while
        describing the same eight journeys over the same 120.8 km. The
        comment on ground_truth_used_kwh already called this bias
        one-directional and worked around it for the since-charge view; this
        removes it at the source for the trips that never needed it.

        A streamed trip with no energy figure at all still takes the floor:
        the rescue is about a missing measurement, and that is one.
        """
        integer_kwh = max(d.start_soc - d.end_soc, 0.0) / 100.0 * capacity_kwh if capacity_kwh else 0.0
        measured = float(d.energy_used_kwh or 0.0)
        if measured > 0 and (getattr(d, "source", "") or "") == "telemetry":
            return measured
        return max(measured, integer_kwh)
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
                # Scored on the GROSS figure, like the window score above and
                # like the car's own "X% more than Rated". It was scored on
                # the propulsion-only figure against the same rated baseline —
                # two different quantities against one yardstick — and since
                # that figure has climate and accessories stripped out of it,
                # it sits 30-50% under rated on an ordinary drive and the
                # score clamped to 100 on essentially every trip. A grade that
                # is always full marks grades nothing.
                #
                # The conditions line beside it is what keeps a low score
                # honest: 302 Wh/km in stop-go traffic at 34 degrees is a real
                # efficiency, and reporting it as an A because the climate it
                # spent on was subtracted first would be flattery.
                "eco_score": (eco_score(d.wh_per_km, rated_wh_per_km)
                              if has_valid_energy(d) else None),
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
                # The temperature this trip was DRIVEN in, and the one it
                # ended at. Both, because the climate model integrates the
                # first while a reader recognises the second — and the gap
                # between them is what the model's input was wrong by before
                # 13 September, which is only visible if both travel together.
                # out_temp_end is null on every trip closed before that, and
                # must stay null: unknown is not zero.
                "out_temp": d.outside_temp_c,
                "out_temp_end": getattr(d, "out_temp_end_c", None),
                # bms / exit / timeout / stream_lost. Usually the whole
                # explanation for a trip that reads short against the car.
                "ended_on": getattr(d, "ended_on", None),
                # The driving matrix's OWN classification of this same trip —
                # computed first because "conditions" below is written IN
                # these words rather than its own separate guess (see
                # _trip_conditions). None when the trip lacks a duration,
                # distance or peak speed to sort by (see drive_mode).
                "matrix_mode": (_dme := drive_mode_explained(d, mode_cuts))["mode"],
                "matrix_mode_why": _dme.get("why"),
                "conditions": _trip_conditions(_dme["mode"], d),
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
                # How much of distance_km at that end is an ESTIMATE rather
                # than a reading (see Drive.end_est_km) — the one number that
                # separates "the car drove this" from "we think it did". A
                # trip that reads long against the car's own screen by exactly
                # this much has been answered without deriving anything.
                "end_est_km": getattr(d, "end_est_km", None),
                # And whether that estimate has been checked against the car
                # itself — the difference between a figure still in question
                # and one already settled.
                "end_est_verified": getattr(d, "end_est_verified", None),
                # What the departure recovery pulled back in, which is what
                # tells a "nothing was lost" 0.0 apart from a "the recovery
                # reclaimed it" 0.0 (see Drive.start_recovered_km).
                "start_recovered_km": getattr(d, "start_recovered_km", None),
                # Parked minutes inside the departure gap whose standby
                # drain was taken back off this trip (see
                # Drive.start_park_min).
                "start_park_min": getattr(d, "start_park_min", None),
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


# --- driving-condition matrix ----------------------------------------------
#
# A taxonomy the owner defined, not one inferred from the data, and the shape
# of it is the whole reason a new classifier was needed rather than reusing
# _trip_conditions above. Their own measured ranges run
#
#     CC 553 km  >  CH 494  >  SC 460  >  HC 345
#
# which is NOT monotonic in speed: a 60-80 km/h cruise beats a 110 km/h one,
# because drag costs more than the extra speed saves. Any classifier keyed on
# average speed alone — which is exactly what _trip_conditions is — collapses
# CC and CH together and cannot reproduce that ordering.
#
# What separates them is in the owner's own labels: "Constant" against
# "Intermittent Idling" against "Repeated Idling". So idling decides the pair
# first, and speed only separates within it.
# How close a trip stayed to its own peak speed. This, not idle_min, is what
# separates constant driving from stop-go — and getting that wrong the first
# time is worth recording, because the mistake was reading a field's name
# instead of its definition.
#
# idle_min counts only stopped streaks of IDLE_STREAK_MIN (five minutes) or
# more, and sync.py says why in as many words: a stop-go commute chaining a
# long light, queue creep and the next light into three or four continuous
# near-stationary minutes "is driving, not idling". So idle_min measures
# WAITING — a pickup, a drive-through — and deliberately excludes traffic.
# Keyed on it, 24 real trips averaging 25.7 km/h were sorted as constant city
# cruising at 0% idle, and the mode came out 6% WORSE than the rated baseline
# when it should be the best condition there is.
#
# avg / max has none of that problem. A cruise holds most of its peak; a crawl
# through lights does not, whatever its stops were too short to register as.
MODE_CONSTANT_RATIO = 0.55    # at or above: the trip largely held one speed
MODE_SLOW_RATIO = 0.28        # between: intermittent. below: repeatedly stopped
# Both moved (from 0.35 and 0.30) after two real trips — a 24-min commute
# with one ~10-minute wait (idle share 0.40, ratio 0.31) and a 17-minute hop
# with no long stop at all (ratio 0.33) — both landed in HC despite costing
# 129 and 150 Wh/km, cheaper than plenty of this car's SC and CC trips. HC
# is a traffic-PATTERN verdict, not a cost one, and at the old cuts it was
# catching ordinary stop-go rather than the genuine crawls it exists for.
# Slow city's floor came down to absorb that middle ground; heavy idle's
# floor went up so a single ordinary light no longer earns HC on its own —
# it now takes waiting through nearly half the trip, not moving as little
# as a third of one, before genuine idle overrides the speed reading.
MODE_IDLE_HEAVY = 0.45        # or this much of it spent genuinely waiting
# "110 km/h +/- 10%" is the reference this was built against, so the floor is
# that band's bottom: 110 - 10% = 99. A trip has to have been UP there (the
# peak) and to have STAYED there (the average) before it is a highway drive —
# one slip-road burst inside a city run is not, and the pair of tests is what
# tells those apart.
MODE_HIGHWAY_MAX_KMH = 99.0
MODE_HIGHWAY_AVG_KMH = 70.0
# Above the band entirely. Drag goes as the square of speed, so the stretch
# past 130 is a different animal from a 110 cruise and averaging them together
# hides exactly the cost worth seeing — the same argument that separates CC
# from CH one band lower.
MODE_FAST_MAX_KMH = 130.0
# Held at 0.7 of the peak floor, the same proportion CH uses (70 against 99),
# so the two bands are one rule rather than two opinions. Tested BEFORE the CH
# pair, and falling through to it: a trip that touched 130 but averaged 75 is a
# highway drive that was briefly fast, not a fast one.
MODE_FAST_AVG_KMH = 91.0


def resolved_cuts(cuts: dict[str, float] | None = None) -> dict[str, float]:
    """The cut-points in force: what was stored, falling back to the defaults.

    One place, because there are now seven of them and every caller that spells
    the mapping out again is a chance to spell one wrong. The first version of
    the per-trip endpoint built the constant names from the key names by string
    surgery and would have raised AttributeError on the first request — the
    same fault, in the same file, as the threshold validator a few commits
    earlier. Twice is a pattern, so the mapping is written once and looked up.
    """
    c = cuts or {}
    defaults = {
        "constant_ratio_min": MODE_CONSTANT_RATIO,
        "slow_ratio_min": MODE_SLOW_RATIO,
        "heavy_idle_share_max": MODE_IDLE_HEAVY,
        "highway_max_kmh": MODE_HIGHWAY_MAX_KMH,
        "highway_avg_kmh": MODE_HIGHWAY_AVG_KMH,
        "fast_max_kmh": MODE_FAST_MAX_KMH,
        "fast_avg_kmh": MODE_FAST_AVG_KMH,
    }
    return {k: float(c.get(k, v)) for k, v in defaults.items()}


def mode_split(d: Any, cuts: dict[str, float] | None = None) -> dict[str, dict[str, float]]:
    """How one trip divides across the modes: {mode: {km, min, kwh}}.

    A trip is not necessarily one thing. Trip 735 covered 28 km at an average
    of 75 with a peak of 160 — a motorway run with town at either end — and
    whole-trip classification has to call that one category and be wrong about
    most of it.

    Where the stream recorded a speed profile, the highway share is split out
    by MEASURED distance: the bands at or above each speed bar carry their own
    kilometres, minutes and kWh, all three accumulated from the odometer and
    the car's own EnergyRemaining over the stretches they happened in. Nothing
    is apportioned — apportioning energy by distance would hand highway and
    city the same Wh/km, which is the one distinction this whole table exists
    to draw.

    What is left over is city, and the city share keeps the whole-trip verdict
    that drive_mode already reaches. That part is unchanged on purpose: telling
    CC from SC from HC is a question about stopping rather than speed, and the
    thresholds for doing it per-stretch would be invented rather than measured.
    Measured where it can be measured, unchanged where it cannot.

    A trip with no profile — everything written before the accumulator existed,
    and anything polling wrote — comes back whole under its own mode, so the
    two kinds live in one table without either pretending to be the other.
    """
    whole = drive_mode(d, cuts)
    km = float(getattr(d, "distance_km", 0.0) or 0.0)
    mins = float(getattr(d, "duration_min", 0.0) or 0.0)
    kwh = float(getattr(d, "energy_used_kwh", 0.0) or 0.0)
    if whole is None:
        return {}
    bands = speed_profile_of(d)
    if not bands:
        return {whole: {"km": km, "min": mins, "kwh": kwh}}

    c = resolved_cuts(cuts)
    hw_max, fast_max = c["highway_max_kmh"], c["fast_max_kmh"]
    out: dict[str, dict[str, float]] = {}

    def add(mode: str, part: dict[str, float]) -> None:
        slot = out.setdefault(mode, {"km": 0.0, "min": 0.0, "kwh": 0.0})
        for k in ("km", "min", "kwh"):
            slot[k] += part[k]

    leftover = {"km": 0.0, "min": 0.0, "kwh": 0.0}
    city_peak = 0.0
    for edge, part in bands.items():
        try:
            lower = float(edge)
        except (TypeError, ValueError):
            continue
        # A band is attributed by its LOWER edge, so a band only counts as
        # highway when every kilometre in it was at or above the bar. The
        # alternative rounds a 95-105 band up and claims motorway distance the
        # trip may not have driven.
        row = {"km": float(part.get("km") or 0.0),
               "min": float(part.get("min") or 0.0),
               "kwh": float(part.get("kwh") or 0.0)}
        if lower >= fast_max:
            add("FH", row)
        elif lower >= hw_max:
            add("CH", row)
        else:
            for k in ("km", "min", "kwh"):
                leftover[k] += row[k]
            # The city stretch's own peak, from the highest band it actually
            # used — its upper edge, since a band holds speeds up to it. The
            # trip's overall peak is the wrong number here by construction: it
            # belongs to the motorway part that has just been taken out, and
            # using it makes the town look far less constant than it was.
            city_peak = max(city_peak, lower + 10.0)

    if leftover["km"] > 0 or leftover["min"] > 0:
        # The city remainder under the trip's own verdict — unless that verdict
        # was itself a highway one, which happens when the trip averaged up
        # there. Then the leftover is the town at either end of it, and calling
        # that Constant Highway would be the original error in miniature.
        city = whole if whole in ("CC", "SC", "HC") else drive_mode(
            SimpleTrip(km=leftover["km"], mins=leftover["min"],
                       mx=city_peak,
                       idle=float(getattr(d, "idle_min", 0.0) or 0.0)), cuts)
        add(city or "HC", leftover)

    # The profile is sampled from records and the trip's own totals come from
    # its endpoints, so the two differ by whatever the stream missed. Scaled to
    # the trip rather than left short: the row totals are what the rest of the
    # report adds up, and a split that does not reconstitute its own trip would
    # put the difference nowhere.
    got_km = sum(v["km"] for v in out.values())
    got_kwh = sum(v["kwh"] for v in out.values())
    got_min = sum(v["min"] for v in out.values())
    for v in out.values():
        if got_km > 0 and km > 0:
            v["km"] *= km / got_km
        if got_min > 0 and mins > 0:
            v["min"] *= mins / got_min
        if got_kwh > 0 and kwh > 0:
            v["kwh"] *= kwh / got_kwh
    return out


class SimpleTrip:
    """The four fields drive_mode reads, for classifying a PART of a trip.

    A named object rather than a dict or a namespace built inline, because
    drive_mode reads its inputs by attribute and a typo in one of them reads as
    zero — which is the difference between Heavy City and unclassifiable,
    silently.
    """

    __slots__ = ("distance_km", "duration_min", "max_speed_kmh", "idle_min")

    def __init__(self, km: float, mins: float, mx: float, idle: float = 0.0):
        self.distance_km = km
        self.duration_min = mins
        self.max_speed_kmh = mx
        self.idle_min = idle


def speed_profile_of(d: Any) -> dict[str, dict[str, float]] | None:
    """A trip's stored speed profile, parsed. None where there is not one.

    Most of the history has no profile and never will — the accumulator did not
    exist when those trips were recorded, and nothing can reconstruct it. So
    every caller has to handle None, and the report has to show both kinds
    without either pretending to be the other.

    A profile that cannot describe a real drive is treated the same way, and
    that is not hypothetical: every profile written before the banding was
    anchored to the odometer (see sync.advance_shadow) divided 30 seconds of
    distance by a 10-second record gap, filing kilometres at three times the
    speed they were driven at and stranding the energy of the intervals in
    between in a bucket with no distance to divide it by. Those rows are
    already in the database and cannot be rebuilt — the records they came
    from are long gone — so they are refused here rather than left to produce
    a "Fast Highway" row at 6 Wh/km and a "Slow City" one at 3511. Refusing
    costs the per-band split and nothing else: the caller falls back to the
    whole-trip verdict, which is what every pre-accumulator trip already
    uses.
    """
    raw = getattr(d, "speed_profile", None)
    if not raw:
        return None
    if isinstance(raw, dict):
        got = raw
    else:
        try:
            got = _json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(got, dict) or not got:
        return None
    # Two things a correctly built profile cannot contain. Energy in a band
    # the car covered no ground in is the stranded-interval half of that bug;
    # a band whose floor is above the trip's own peak speed is the
    # three-times-too-fast half. Ten of tolerance because the buckets are ten
    # wide, so a trip peaking at 135 legitimately reaches the 130 bucket.
    peak = float(getattr(d, "max_speed_kmh", 0.0) or 0.0)
    for edge, part in got.items():
        if not isinstance(part, dict):
            return None
        try:
            km = float(part.get("km") or 0.0)
            kwh = float(part.get("kwh") or 0.0)
            floor = float(edge)
        except (TypeError, ValueError):
            return None
        if km <= 0.0 and abs(kwh) > 0.01:
            return None
        if km > 0.0 and peak and floor > peak + 10.0:
            return None
    return got


def drive_mode_explained(d: Any, cuts: dict[str, float] | None = None) -> dict[str, Any]:
    """One trip's mode and the numbers that put it there.

    The cut-points are tunable, and until now there was no way to see a single
    trip's classification or what decided it — only the per-mode aggregates,
    which is the wrong end for the question people actually ask: "I drove fast
    on Saturday, why is that not Fast Highway?"

    Returns the same code drive_mode does, plus the three figures the decision
    turns on and a sentence naming the test that settled it. The code is taken
    FROM drive_mode rather than re-derived here: an explanation that can
    disagree with the decision it explains is worse than none.
    """
    code = drive_mode(d, cuts)
    dur = float(getattr(d, "duration_min", 0.0) or 0.0)
    dist = float(getattr(d, "distance_km", 0.0) or 0.0)
    mx = float(getattr(d, "max_speed_kmh", 0.0) or 0.0)
    idle = float(getattr(d, "idle_min", 0.0) or 0.0)
    c = resolved_cuts(cuts)
    constant = c["constant_ratio_min"]
    slow = c["slow_ratio_min"]
    heavy_idle = c["heavy_idle_share_max"]
    hw_max = c["highway_max_kmh"]
    hw_avg = c["highway_avg_kmh"]
    fast_max = c["fast_max_kmh"]
    fast_avg = c["fast_avg_kmh"]

    out: dict[str, Any] = {
        "id": getattr(d, "id", None),
        "at": (d.start_time.isoformat(timespec="minutes")
               if getattr(d, "start_time", None) else None),
        "km": round(dist, 2), "min": round(dur, 1),
        "max_kmh": round(mx, 1),
        "mode": code,
    }
    if code is None:
        out["why"] = ("not sortable — needs a duration, a distance and a peak "
                      "speed, and one of them is missing or zero")
        return out
    avg = dist / (dur / 60.0)
    ratio = avg / mx
    out["avg_kmh"] = round(avg, 1)
    out["constancy"] = round(ratio, 3)
    out["idle_share"] = round(idle / dur, 3) if dur else None

    if (idle / dur if dur else 0.0) >= heavy_idle:
        out["why"] = (f"{idle / dur * 100:.0f}% of it was spent genuinely "
                      f"waiting, past the {heavy_idle * 100:.0f}% mark — heavy "
                      f"whatever the speeds looked like")
    elif mx >= fast_max and avg >= fast_avg:
        out["why"] = (f"peaked at {mx:.0f} and AVERAGED {avg:.0f} — clears "
                      f"both fast bars ({fast_max:.0f} peak, {fast_avg:.0f} "
                      f"average)")
    elif mx >= hw_max and avg >= hw_avg:
        missed = []
        if mx < fast_max:
            missed.append(f"peaked at {mx:.0f}, under the {fast_max:.0f} FH bar")
        if avg < fast_avg:
            missed.append(f"averaged {avg:.0f}, under the {fast_avg:.0f} FH bar")
        out["why"] = (f"over the highway bars ({hw_max:.0f} peak, "
                      f"{hw_avg:.0f} average) — but " + " and ".join(missed))
    elif ratio < slow:
        out["why"] = (f"held only {ratio:.2f} of its own peak, under the "
                      f"{slow:.2f} slow cut — repeatedly stopped")
    elif ratio < constant:
        out["why"] = (f"held {ratio:.2f} of its peak, between the {slow:.2f} "
                      f"and {constant:.2f} cuts — moving, but well under "
                      f"itself a fair part of the time")
    else:
        missed = []
        if mx < hw_max:
            missed.append(f"peaked at {mx:.0f}, under the {hw_max:.0f} bar")
        if avg < hw_avg:
            missed.append(f"averaged {avg:.0f}, under the {hw_avg:.0f} bar")
        out["why"] = (f"held {ratio:.2f} of its peak, at or above the "
                      f"{constant:.2f} constant cut, but not at highway pace — "
                      + " and ".join(missed))
    return out


def drive_mode(d: Any, cuts: dict[str, float] | None = None) -> str | None:
    """Which driving condition this trip was, or None when it cannot be said.

    None rather than a guess where idle was never tracked: the split between
    constant and intermittent IS the idle fraction, so a trip without it can
    only be sorted on speed, and sorting on speed is the thing that does not
    work here. A trip that cannot be classified is better left out of a
    per-mode average than quietly placed in the wrong one.
    """
    dur = float(getattr(d, "duration_min", 0.0) or 0.0)
    dist = float(getattr(d, "distance_km", 0.0) or 0.0)
    mx = float(getattr(d, "max_speed_kmh", 0.0) or 0.0)
    if dur <= 0 or dist <= 0 or mx <= 0:
        return None
    c = cuts or {}
    constant = float(c.get("constant_ratio_min", MODE_CONSTANT_RATIO))
    slow = float(c.get("slow_ratio_min", MODE_SLOW_RATIO))
    heavy_idle = float(c.get("heavy_idle_share_max", MODE_IDLE_HEAVY))
    hw_max = float(c.get("highway_max_kmh", MODE_HIGHWAY_MAX_KMH))
    hw_avg = float(c.get("highway_avg_kmh", MODE_HIGHWAY_AVG_KMH))
    fast_max = float(c.get("fast_max_kmh", MODE_FAST_MAX_KMH))
    fast_avg = float(c.get("fast_avg_kmh", MODE_FAST_AVG_KMH))
    avg = dist / (dur / 60.0)
    ratio = avg / mx
    # Genuine waiting outranks the ratio: a trip that spent a third of itself
    # stopped is heavy whatever the moving part looked like.
    if (float(getattr(d, "idle_min", 0.0) or 0.0) / dur) >= heavy_idle:
        return "HC"
    # The highway classes are decided by ABSOLUTE speed, before constancy is
    # consulted at all. Constancy then sorts only what is left, which is the
    # city.
    #
    # That order matters, and putting it the other way round was wrong.
    # Constancy is average divided by PEAK, and a peak is one sample — so a
    # single burst drags the ratio down and the trip looks less constant for
    # having briefly gone faster. Measured on trip 735: 28.12 km in 22.4
    # minutes, averaging 75 km/h with a peak of 160, was classified SLOW CITY.
    # Had the peak been 130 the same trip would have read 0.58 and landed in
    # Constant Highway. A higher top speed made it a slower category, which is
    # not a threshold that needs tuning — it is the wrong shape.
    #
    # Absolute speed has no such failure: you cannot average 70 km/h over a
    # whole trip in city traffic, however variable the trip was. So averaging
    # up there IS the evidence of a highway, and how steady it felt is a
    # separate question that only becomes interesting below that speed.
    if mx >= fast_max and avg >= fast_avg:
        return "FH"
    if mx >= hw_max and avg >= hw_avg:
        return "CH"
    if ratio >= constant:
        return "CC"
    return "SC" if ratio >= slow else "HC"


def condition_matrix(drives: list[Any], capacity_kwh: float,
                     baseline_range_km: float | None = None,
                     cuts: dict[str, float] | None = None) -> dict[str, Any]:
    """Per-condition efficiency, as a range rather than a rate.

    Wh/km is weighted by DISTANCE, not averaged across trips. A mean of means
    lets a 2 km crawl move the figure as far as a 40 km run, and the question
    this answers is what a full battery is worth in each condition — which is
    a property of the kilometres, not of the trips they were grouped into.
    """
    # Each trip's contribution to each mode, so a journey that was partly one
    # thing and partly another lands in both — see mode_split. A trip with no
    # speed profile contributes wholly to one mode, exactly as before.
    parts: dict[str, dict[str, float]] = {}
    members: dict[str, list[Any]] = {}
    split_trips = 0
    buckets: dict[str, list[Any]] = {}
    unclassified = 0
    # The energy that does NOT reach a row, kept apart by reason. Without these
    # the table looks like it accounts for the window's driving and quietly does
    # not: a trip with implausible energy was skipped without even being
    # counted, and an unsortable one was counted but its kWh was not. Both are
    # real energy the car used, so the report has to be able to show that
    # sum(modes) + these = the window's driving energy, rather than leaving a
    # reader to find the difference and wonder which figure is wrong.
    unclassified_kwh = 0.0
    no_energy = 0
    no_energy_kwh = 0.0
    for d in drives:
        if not has_valid_energy(d):
            no_energy += 1
            no_energy_kwh += float(getattr(d, "energy_used_kwh", 0.0) or 0.0)
            continue
        m = drive_mode(d, cuts)
        if m is None:
            unclassified += 1
            unclassified_kwh += float(d.energy_used_kwh)
            continue
        buckets.setdefault(m, []).append(d)
        share = mode_split(d, cuts)
        if len(share) > 1:
            split_trips += 1
        for code, got_part in share.items():
            slot = parts.setdefault(code, {"km": 0.0, "min": 0.0, "kwh": 0.0})
            for k in ("km", "min", "kwh"):
                slot[k] += got_part[k]
            members.setdefault(code, []).append(d)

    rows = []
    for code in ("FH", "CH", "CC", "SC", "HC"):
        # Driven by the members list, not the whole-trip bucket: a mode can now
        # be reached by a journey whose whole-trip verdict was something else —
        # the motorway share of a mixed trip.
        got = members.get(code) or []
        if not got:
            continue
        # Distance, time and energy come from the SPLIT, so a trip counted in
        # two modes contributes its measured share to each rather than its
        # whole self to both. Everything else on the row — trip count, speeds,
        # temperature — describes the journeys that touched this mode.
        share = parts.get(code) or {"km": 0.0, "min": 0.0, "kwh": 0.0}
        km, mins, kwh = share["km"], share["min"], share["kwh"]
        idle = sum(float(getattr(d, "idle_min", 0.0) or 0.0) for d in got)
        wh = kwh * 1000.0 / km if km else None
        rng = capacity_kwh / (wh / 1000.0) if wh else None
        temps = [float(d.outside_temp_c) for d in got
                 if getattr(d, "outside_temp_c", None) is not None]
        peaks = [float(d.max_speed_kmh) for d in got
                 if getattr(d, "max_speed_kmh", None)]
        rows.append({
            "code": code, "name": MODE_NAMES[code], "trips": len(got),
            "km": round(km, 1), "hours": round(mins / 60.0, 1),
            "avg_speed_kmh": round(km / (mins / 60.0), 1) if mins else None,
            # The two figures the classifier actually keyed on, and the ratio
            # it derived from them. Carried so a row that looks wrong can be
            # checked against what put it there instead of being argued about:
            # the bands are tunable, and tuning them blind is guessing.
            "max_speed_kmh": round(mean(peaks), 1) if peaks else None,
            "constancy": (round((km / (mins / 60.0)) / mean(peaks), 2)
                          if mins and peaks and mean(peaks) else None),
            "wh_per_km": round(wh, 1) if wh else None,
            # The same condition priced per HOUR rather than per kilometre,
            # which is the unit a parked car can also be quoted in — so the
            # whole table, moving and standing still, sits on one axis.
            #
            # It is not a restatement of Wh/km. A slow condition is typically
            # cheap per hour and expensive per kilometre because the car
            # covers so little ground for what it burns — but "slow" here
            # means covering little ground, not necessarily costing the most
            # per km: HC can come in under SC, because a car standing
            # genuinely still (HC's idle share) draws far less than one
            # repeatedly slowing and re-accelerating (SC's ratio). The two
            # columns answer different questions — how far will this take
            # me, and how long can I sit here — and a range-only table can
            # only show one of them.
            "kw": round(kwh / (mins / 60.0), 2) if mins else None,
            # The climate context, measured rather than assumed. Every trip
            # here runs with the air conditioning on — in this climate that is
            # a given, not a setting — but the load it draws follows the
            # OUTSIDE temperature, and that is not constant: the same route
            # differs by ten degrees between a morning and an afternoon. So
            # the ambient is carried as a column rather than as a label.
            "out_temp_c": round(sum(temps) / len(temps), 1) if temps else None,
            "out_temp_range_c": ([round(min(temps), 1), round(max(temps), 1)]
                                 if temps else None),
            # What a full battery is worth driven entirely like this, and what
            # one percent of it buys — the form the owner's own reference
            # table is written in.
            "range_km": round(rng) if rng else None,
            "km_per_pct": round(rng / 100.0, 1) if rng else None,
            "vs_baseline_pct": (round((rng - baseline_range_km)
                                      / baseline_range_km * 100.0)
                                if rng and baseline_range_km else None),
            "avg_trip_min": round(mins / len(got), 1),
            "idle_share_pct": round(idle / mins * 100.0, 1) if mins else None,
            # What this condition actually cost, as opposed to what it costs per
            # kilometre or per hour. Measured, not projected — these are the
            # trips' own summed energy — which is the difference between this
            # column here and the same column on a parked row.
            "kwh": round(kwh, 2),
            # And the same figure as a share of the pack, which is the ONE unit
            # every row in the report can be stated in. Driving rows were in
            # kWh and parked rows in percent, so the table could not be added
            # up — a reader wanting "where did my battery go" had to convert
            # half of it by hand. This is what makes
            #     CC + CH + SC + HC + PK = the window's consumption
            # something the report can show rather than something to work out.
            "pct": round(kwh / capacity_kwh * 100.0, 2) if capacity_kwh else None,
        })
    # Each mode's share of the driving, on the modes' OWN totals so they sum to
    # 100%. The share_* fields the endpoint adds are of driving PLUS parked,
    # which answers a different question — "how much of my month was this" —
    # and cannot close on 100 across the driving rows alone. Both are wanted:
    # one says how the driving splits, the other how the driving compares with
    # standing still.
    #
    # Unclassified trips are outside this denominator rather than inside it. A
    # share of a total that includes rows not shown does not sum to anything a
    # reader can check, and the count of what was left out is reported beside
    # it already.
    mode_km = sum(r["km"] for r in rows) or 0.0
    mode_kwh = sum(r["kwh"] for r in rows) or 0.0
    for r in rows:
        r["share_km_pct"] = round(r["km"] / mode_km * 100.0, 1) if mode_km else None
        r["share_drive_kwh_pct"] = (round(r["kwh"] / mode_kwh * 100.0, 1)
                                    if mode_kwh else None)

    # Ranked both ways, because the ordering IS the finding and reading it off
    # two unsorted columns is work the table can do for the reader.
    for key, field in (("rank_per_km", "wh_per_km"), ("rank_per_hour", "kw")):
        ranked = sorted((r for r in rows if r.get(field) is not None),
                        key=lambda r: r[field])
        for i, r in enumerate(ranked, 1):
            r[key] = i
    return {
        "baseline_range_km": (round(baseline_range_km)
                              if baseline_range_km else None),
        "modes": rows,
        # Trips that could not be sorted, and why it matters: they are missing
        # from every row above rather than distributed among them.
        "unclassified_trips": unclassified,
        # Journeys that contributed to more than one mode, because the stream
        # recorded where their kilometres actually happened. The rest were
        # placed whole, either because they were one thing throughout or
        # because they predate the speed profile and nothing can reconstruct it.
        "split_trips": split_trips,
        "unclassified_kwh": round(unclassified_kwh, 2),
        "unclassified_pct": (round(unclassified_kwh / capacity_kwh * 100.0, 2)
                             if capacity_kwh else None),
        # And the ones that never reached the classifier at all, because their
        # energy is not plausible enough to feed an efficiency figure (see
        # analysis.has_valid_energy). Reported rather than silently dropped —
        # this was the larger of the two holes and the only invisible one.
        "no_energy_trips": no_energy,
        "no_energy_kwh": round(no_energy_kwh, 2),
        "no_energy_pct": (round(no_energy_kwh / capacity_kwh * 100.0, 2)
                          if capacity_kwh else None),
        "modes_kwh": round(sum(r["kwh"] for r in rows), 2),
        "modes_pct": round(sum(r["pct"] or 0.0 for r in rows), 2),
        "thresholds": resolved_cuts(cuts),
    }


# What every code and column on the report means, carried with the numbers.
# A matrix whose labels live only in someone's head stops being readable the
# first time it is shared, or reread six months later.
MATRIX_DEFINITIONS = {
    "modes": [
        {"code": "FH", "name": "Fast Highway",
         "means": "Constancy 0.55 or above, with a peak of at least 130 km/h AND "
                  "an average of at least 91. Above the 110 band entirely — drag "
                  "goes as the square of speed, so these kilometres cost "
                  "materially more than CH's and averaging the two together "
                  "hides exactly that."},
        {"code": "CH", "name": "Constant Highway",
         "means": "Constancy 0.55 or above, with a peak of at least 99 km/h — "
                  "110 minus 10%, the bottom of the reference band — AND an "
                  "average of at least 70. Fast and it stayed fast, but not "
                  "into FH territory. More expensive per hour than CC: drag "
                  "rises faster than the speed saves."},
        {"code": "CC", "name": "Constant City",
         "means": "Below the highway bars, but held 0.55 or more of its own "
                  "peak — open roads, few interruptions, typically 60-80 km/h. "
                  "Usually the cheapest kilometres a car does, and cheaper per "
                  "kilometre than CH."},
        {"code": "SC", "name": "Slow City",
         "means": "Constancy between 0.28 and 0.55. Spent a fair part of the trip "
                  "well under its own peak — lights and moderate traffic, but "
                  "still moving. This is ordinary stop-go, not the worst "
                  "condition here: it can cost more per km than HC, because a "
                  "car that is genuinely stopped burns far less than one "
                  "repeatedly slowing and accelerating."},
        {"code": "HC", "name": "Heavy City",
         "means": "Constancy under 0.28, or 45% or more of the trip spent "
                  "genuinely waiting (stops of five minutes or more) — a "
                  "deliberately severe bar, since either one means most of the "
                  "trip was barely moving at all. Cheapest per hour — the car "
                  "burns little because it covers little — but not "
                  "necessarily worst per kilometre; see SC."},
        {"code": "PK", "name": "Park Overall",
         "means": "What the window's parking cost, as a percentage of the "
                  "battery: add up what the gauge lost across every parked "
                  "gap. A measurement, not a model, so it is always there. "
                  "PK = ID + SE, with the parks nothing recorded a state for "
                  "widening both rather than sitting in a third."},
        {"code": "ID", "name": "Idling, Sentry off",
         "means": "The share of PK lost across parks Sentry was measurably OFF "
                  "for. The floor of what this car costs to own: doors locked, "
                  "nothing watching."},
        {"code": "SE", "name": "Sentry impact",
         "means": "The share of PK lost across parks Sentry was armed for — "
                  "measured on those parks, not inferred by subtracting ID from "
                  "PK. That matters: subtracting would charge every park whose "
                  "state went unrecorded to Sentry as well."},
    ],
    "columns": [
        {"name": "ID and SE as a range",
         "means": "Sentry is on or off; there is no third state. Where a park "
                  "went by with nothing recording which it was, that is a gap "
                  "in what the app SAW rather than a kind of park — so it "
                  "widens both rows instead of becoming one. ID reads at least "
                  "its measured share and at most that plus the unread parks, "
                  "and SE the same. The truth is one point on each interval. "
                  "The car's own Park screen has no such gap, which is why it "
                  "is worth filing one."},
        {"name": "Wh/km", "means": "Energy per kilometre, weighted by distance "
                                   "rather than averaged across trips — a 2 km "
                                   "crawl should not move it as far as a 40 km "
                                   "run. A parked row has no kilometres, so it "
                                   "carries the percentage of the battery that "
                                   "parking ate instead, with how much of the "
                                   "figure is 1% gauge steps beside it."},
        {"name": "kW", "means": "Energy per hour. The unit a parked car can also "
                                "be quoted in, so driving and standing still "
                                "compare on one scale."},
        {"name": "kWh", "means": "What this condition actually cost over the "
                                 "window, and the share of hours and energy "
                                 "beside it. Measured on every row: a driving "
                                 "row sums the trips' own energy, and a parked "
                                 "row is fitted from the same parked gaps it is "
                                 "then applied to — errand stops included, not "
                                 "only the long parks. The gauge reads to a "
                                 "whole SoC point, so a parked row also carries "
                                 "how much of its rate is that resolution, and "
                                 "is left blank until it stands three times "
                                 "clear of it. That is a test of the evidence "
                                 "rather than of the calendar: the same hours "
                                 "in a few long parks are better evidence than "
                                 "in many short stops, because the rounding "
                                 "grows with the number of parks and the drain "
                                 "grows with the hours."},
        {"name": "Range", "means": "What a full battery is worth driven entirely "
                                   "in this condition."},
        {"name": "km/1%", "means": "What one percent of the battery buys here."},
        {"name": "Rank", "means": "Position among the driving modes. The per-km "
                                  "and per-hour rankings are reverses of each "
                                  "other, which is the point of showing both."},
    ],
    "split_trips": (
        "A trip is not necessarily one thing. Where the stream recorded where a "
        "journey's kilometres actually happened, its motorway share is counted "
        "as motorway and its town share as town — measured, not apportioned, "
        "down to each band's own energy. A 28 km run at an average of 75 with a "
        "peak of 160 is a motorway drive with town at either end, and counting "
        "it as one category is wrong about most of it. Trips recorded before "
        "this existed have no profile and are still placed whole; the report "
        "says how many of each."
    ),
    "adds_up": (
        "Every hour of the window lands in exactly one bucket — driving, parked, "
        "charging, excluded (a gap the odometer says the car moved through, or "
        "where SoC rose), or the two unmeasured edges before the first trip and "
        "after the last. So the table sums to the window rather than to two "
        "different populations: previously the driving rows counted every trip "
        "while the parked rows were a rate fitted from long parks only, and a "
        "normal day's errand stops appeared in neither."
    ),
    "speed_bands": (
        "FH needs a peak of 130 km/h and an average of 91; CH a peak of 99 "
        "(110 minus 10%) and an average of 70. Both tests matter — the peak "
        "says the road was there, the average says the trip stayed on it — and "
        "they are tried fastest first, so a trip that touched 130 once but "
        "averaged 75 is a highway drive that was briefly fast, not a fast one. "
        "These are decided on ABSOLUTE speed, before constancy is consulted at "
        "all: you cannot average 70 km/h over a whole trip in city traffic, "
        "however variable it was."
    ),
    "constancy": (
        "Below the highway bars, the city modes turn on CONSTANCY: the trip's "
        "average speed divided by its maximum. It asks how much of its own peak "
        "the trip held — 1.0 is a trip at one speed throughout, 0.16 a crawl "
        "that briefly touched 74 km/h. It is needed because speed alone sorts "
        "the cheap kilometres wrongly: a 60-80 km/h cruise costs LESS per "
        "kilometre than a 110 one, since drag rises faster than the speed "
        "saves. But it is used only below highway pace, because the peak is a "
        "single sample — a trip that averaged 75 with one burst to 160 scored "
        "0.47 and was called Slow City, where the same trip peaking at 130 "
        "would have been Constant Highway. A higher top speed must not make a "
        "trip a slower category."
    ),
    "how_sorted": (
        "A trip is sorted by how close it stayed to its own peak speed — average "
        "divided by maximum — and only then by how fast that was. Not by idle "
        "time: this app counts a stop as idle only past five minutes, so a "
        "commute through a dozen lights registers none at all."
    ),
}


def split_matrices(drives: list[Any], capacity_kwh: float,
                   baseline_range_km: float | None = None,
                   cuts: dict[str, float] | None = None) -> dict[str, Any]:
    """The matrix whole, and split into weekdays and weekends.

    Kept as one call because the three share every threshold and baseline, and
    computing them apart is how two of them drift. A split with no trips in it
    is returned as an empty set of modes rather than omitted, so a reader can
    tell "no weekend driving in this window" from "this report forgot".
    """
    def weekday(d: Any) -> bool:
        return d.start_time.weekday() < 5

    return {
        "overall": condition_matrix(drives, capacity_kwh, baseline_range_km, cuts),
        "weekday": condition_matrix([d for d in drives if weekday(d)],
                                    capacity_kwh, baseline_range_km, cuts),
        "weekend": condition_matrix([d for d in drives if not weekday(d)],
                                    capacity_kwh, baseline_range_km, cuts),
    }
