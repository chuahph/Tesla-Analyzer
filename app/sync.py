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
# Independent absolute-kWh floor alongside CHARGE_MIN_PCT: the %-gain gate
# alone isn't enough, because a BMS SoC recalibration blip — e.g. right after
# a vehicle software reset — can clear it with ~0 real energy.
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
MYT = timezone(timedelta(hours=8))  # Malaysia has no DST


CITY_SPEED_KMH = 30.0  # assumed door-to-door pace when the real duration is unknown

# Three samples, no longer one-sided. The value is provisional.
#
# It is also, on later evidence, only right for SOME places. Three departures
# from Home with long blind heads — trips 397, 402 and 407 — ran the unseen
# stretch at 47, 55 and 45 km/h against this 20, because that particular
# driveway reaches a trunk road inside a kilometre. All three starts came out
# 9 to 12 minutes early. The three samples this constant was fitted from
# (11/20/28 km/h) were departures from other places, and they are not wrong
# either; there is simply no single number, and the spread is the place, not
# the noise.
#
# So a Place may carry its own ``departure_pace_kmh`` and this stays the
# default for everywhere else.
#
# A time-of-day split was nearly added on top and should not be. Six Home
# departures had separated perfectly — mornings at 20.7, 32.0 and 35.7 km/h,
# evenings at 45.3, 47.2 and 55.3, no overlap and an obvious mechanism in
# commuter traffic — and a split would have cut the mean clock error from 1.86
# minutes to 1.01. Trip 447 then departed Home at 18:46 and ran its head at
# 24.4 km/h, straight through the middle of the "morning" band. The separation
# was three samples a side arranging themselves, which is the same shape as
# the convex climate curve and the two-sample accessory constant, both also
# withdrawn. A split still fits marginally better in minutes; that is two extra
# parameters absorbing scatter, not a mechanism.
#
# 45 remains the best single value across all seven (1.86 min mean, against
# 2.04 at 40 and 2.09 at 50). Note what that is NOT: it is a setting, not
# something the app learns. The arrival tail is learnable because a later
# poll observes where the car actually stopped; nothing whatsoever observes
# when it actually STARTED, so no accumulation of history can score a
# departure guess. max_speed_kmh gives no signal either — trip 397 recorded a
# 37 km/h peak over a head that ran at 47, because the peak only covers the
# part we saw.
DEPARTURE_PACE_KMH = 20.0


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
        # vehicle_data has no state to give: Sentry is a bare boolean there,
        # which is exactly why the boolean above conflates Idle with Armed.
        # Only the stream can fill this.
        "sentry_state": None,
        # Physical-entry signals for the parked-intrusion alert. Unlike
        # Sentry's own alarm state, which vehicle_data cannot report — it
        # gives a bare boolean, where the stream gives Aware and Panic — an
        # opened door persists until someone shuts it, so even a slow poll
        # catches it reliably rather than by luck.
        "doors_open": _any_open(vs, _DOOR_FIELDS),
        "windows_open": _any_open(vs, _WINDOW_FIELDS),
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
#
# A later set of audits nearly overturned that "does NOT track temperature"
# finding, and the record is kept here because the near-miss is the useful
# part. Six consecutive trips came in ordered perfectly by ambient — 30C 0.93,
# 32C 1.29, 33C 1.69/1.77, 34C 2.78/3.27 — with our error monotonic across
# all six, +6% at the bottom and -60% at the top. That is exactly the
# signature of a slope too shallow, and it argued for replacing the linear
# term with something convex.
#
# It does not survive contact with the trips above. Those put 33C at 0.76 and
# 0.80; the newer ones put 33C at 1.69 and 1.77. Same temperature, 2.2x apart
# — a spread at ONE point wider than the whole trend across four degrees. The
# ordering in the newer six is real but it is a coincidence of which trips
# happened to fall where, not a curve.
#
# So the model stays linear and stays shallow. Whatever drives climate load
# here is not on the dashboard, and fitting a curve through six points that a
# seventh contradicts would only launder the scatter into false precision.
# What would settle it is repeat trips at ONE temperature: several at 33C
# would separate a genuine curve from noise in a single afternoon, which no
# amount of spreading samples across the band can do.
#
# Those repeats have started arriving, and they say something better than a
# slope would have. Grouped by ambient, using each trip's own duration:
#
#   30C   0.98                       (ours 0.99, +1%)
#   31C   1.04, 1.16, 1.30, 1.30     (ours 1.07, -11% against their mean)
#   32C   1.29                       (ours 1.15, -11%)
#   33C   0.76, 0.80, 1.69, 1.77
#   34C   2.78, 3.27
#
# The fourth 31C reading is trip 407, and it repeats the top of that group
# exactly rather than widening it: four trips at one ambient now span 1.04 to
# 1.30, still the tightest cluster in the band. The model sits at its floor,
# not outside it.
#
# The scatter is not uniform across the band — it EXPLODES with heat. Three
# trips at 31C hold within +/-11% of each other; four at 33C span 2.3x. If
# ambient were the whole story the spread would be alike at both, so what is
# missing is a second load that stays dormant at the bottom of the band and
# dominates at the top: sun on the glass, a soaked cabin, a compressor near
# its duty limit. All plausible, none of them on the dashboard.
#
# That is also why the linear term is worth keeping rather than replacing.
# It is not a bad fit everywhere — it is nearly exact from 30 to 31C, where
# the missing load is quiet, and unreliable from 33C up, where it is not.
# A steeper or convex slope would trade the accurate end for the vague one.
#
# ACCESSORY_KW has its own record, and it reads nothing like the one above.
# Two trips whose drive-level split was photographed off the car's own screen
# put "Everything Else" at:
#
#   407   0.3% over 19 min at 31C   0.648 kW
#   406   0.4% over 25 min at 33C   0.657 kW
#   408   0.3% over 24 min at 32C   0.513 kW
#   409   0.5% over 36 min at 30C   0.570 kW
#   418   0.5% over 38 min at 29C   0.540 kW
#   419   0.5% over 40 min at 31C   0.513 kW   (after the change; out of sample)
#
# Mean 0.586 over the first five, 0.574 with 419. Spread 0.513-0.657. The first two arrived 1.4% apart and looked
# like a constant 23% above the 0.5 that used to sit here; 0.65 was tried on
# that basis and reverted when 408 came in at 0.513. That was the right call
# — two readings agreeing is two readings, not a constant.
#
# Five is enough to take the mean, and the mean is 0.586. The value below is
# that, set from these five readings ALONE. It is not fitted to the
# non-propulsion totals in
# test_non_propulsion_load_matches_the_cars_own_breakdown, and that is what
# keeps those totals a check on it rather than a restatement of it.
#
# The check: against the eight trips whose non-propulsion the car has
# reported, the model used to run a mean of -10.0%, six of the eight low.
# That is a bias, not scatter, and 0.5 was 15% under this term's own mean.
# With both terms set to their own measured means the totals come to +1.8%
# mean, four of eight low, and the worst single trip improves from 23% to 15%.
#
# 419 then arrived, the first trip logged after the change and so the only
# out-of-sample test of it. Its propulsion figure read 78 Wh/km against the
# car's own Driving+Elevation of 79.1 — 0.4% — where the old constants would
# have put it at 88.2, or +11.5%. Across nine trips now: mean -9.4% -> +2.6%,
# low on seven of nine -> four, with mean absolute error unchanged at ~10%.
# The bias went; the scatter is the scatter and always was.
#
# One thing worth knowing regardless: this error had been HIDDEN. Trip 407's
# logged duration was 28 minutes against a true 19, and billing a rate 19% low
# over a clock 47% long came out 19% high instead. Correcting the clock
# (Place.departure_pace_kmh) removes that cancellation, so whatever the rate
# turns out to be, it has to stand on its own from here.
ACCESSORY_KW = 0.59
# This was raised to 0.47 for one evening and put back the next morning, and
# the reason is worth keeping because it is the same trap this file keeps
# describing.
#
# The argument for 0.47 was that across the twelve readings of the car's own
# Climate line grouped above, the model averaged 1.123 kW against the car's
# 1.239 — 9% low across the whole band. True, and still true. But those
# twelve readings are not the ten trips whose non-propulsion TOTAL is checked
# in test_non_propulsion_load_matches_the_cars_own_breakdown, and closing a
# gap measured on one sample does not close it on another. Measured: with
# trip 420 in, 0.47 gives mean +4.7% and mean absolute 11.3%, against -2.4%
# and 9.2% at 0.35. It made the bias look better and the answers worse.
#
# ACCESSORY_KW's raise survives the same test because it was never fitted to
# a mean of anything else: six direct reads of the term itself, averaging
# 0.574, replacing a 0.50 that sat under every one of them.
#
# 420 is also why the slope stays where it is. It reads 1.310 kW at 29C where
# 418 read 1.836 at the same ambient — 40% apart, the two of them the only 29C
# samples there are.
#
# At that point the whole SHAPE was tested rather than nudged, since a level
# that will not settle usually means the form is wrong. Four candidates, each
# refitted freely to minimise mean absolute PERCENTAGE error (not least
# squares, which the two longest trips would otherwise dominate) across the
# ten trips whose non-propulsion the car has reported:
#
#   flat kW x hours                     best possible  9.6%   worst 31%
#   rate + a fixed per-trip kWh                        9.5%   worst 25%
#   rate + a per-km term                               9.4%   worst 26%
#   (base + slope/degree) x hours                      8.8%   worst 18%
#   ---
#   what this file actually ships                      9.2%   worst 18%
#
# Three things follow, and they close the question rather than reopen it.
#
# The per-trip and per-km terms fit at essentially zero (0.14 kWh and 0.008
# kWh/km, both inside their own noise) and buy 0.1-0.2 points. There is no
# pull-down transient to find and the load does not scale with distance: this
# is a rate against TIME and nothing else, which is what the model already
# assumes.
#
# The temperature slope is real, and earlier notes here saying otherwise
# overstated it. The best flat model is 9.6% and the best sloped one 8.8%; the
# slope carries the difference. It is simply much shallower than the scatter
# at any one ambient, which is what made it look absent.
#
# And the free optimum lands at base 1.06 kW total with slope 0.065/degree,
# against the 0.94 and 0.080 shipped here — near enough that refitting buys
# 0.4 points on a 9% residual, from two parameters against ten points. That
# is not a model improvement, it is the shape of a fit to its own sample.
#
# So: the residual is not ambient, not duration, not distance and not a
# startup cost. It is cabin soak, sun on the glass, fan speed, recirculation,
# how many people are aboard — none of which Tesla's API reports. Do not
# refit this against a bigger set of the same four variables; measure
# something new, or leave it.
#
# Eleven trips in, what ships runs a mean of -0.5% with mean absolute 10.0%,
# low on four of them. The bias is gone and the scatter is exactly where it
# was, which is the outcome this note predicts and the reason to stop here.
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
    # ``climate_min`` says WHETHER climate ran, not for how much of the drive
    # its load should be counted — so it gates the correction on or off and
    # never prorates it. Prorating measured far too little: the car reports the
    # flag on for only part of a drive its own energy breakdown bills for
    # climate throughout, which is what a cycling compressor under a continuous
    # cabin load looks like through a boolean sampled at poll rate. The
    # fraction we were applying tracked nothing physical — it just rose with
    # trip length, against a car whose own load barely moved:
    #
    #   trip 363   36 min   33C   frac 0.28   ours 0.85 kW   car 1.69 kW
    #   trip 359   66 min   33C   frac 0.67   ours 1.32 kW   car 1.77 kW
    #   trip 360   80 min   30C   frac 0.88   ours 1.37 kW   car 1.76 kW
    #
    # Those three fit a flat 1.82 kW with a fixed term of -0.06 kWh: a pure
    # rate, no per-drive cost. Recorded because the residual against the old
    # model looked like a clean fixed ~0.5 kWh offset holding across a 2.2x
    # span of durations — which was this fraction's own drift seen from the
    # other side, and a constant fitted to it would have been an artifact.
    frac = 1.0 if climate_min is None or climate_min > 0 else 0.0
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
# extended. Past it the trip reports no energy at all.
#
# That second consequence is why this moved from 0.5 to 0.75. A dash is the
# right answer when a number would be mistaken for a reading, and when this
# was written that was the only way to say so. Drive.data_quality came later
# and says it explicitly: anything past INFERRED_SHARE_MAX (10%) blind already
# reads "estimated", so a 52%-blind trip was being labelled AND blanked.
#
# Blanking is not the conservative choice it looks like. A trip with no energy
# has no cost either, so it silently leaves the month's total — three trips a
# month out of this car's home car park, every one of them a real drive that
# really cost money. An explicit estimate beats a silent omission.
#
# What is actually being extrapolated is better resolved than the 0.5 implies.
# _energy_kwh prefers the RANGE delta, which is fractional, over the integer
# SoC — so the measured stretch's Wh/km is good to a couple of percent even
# when it is short, and the uncertainty is not rounding but whether the blind
# head drove like the rest of the trip. Measured, that ratio has run 0.91
# (trip 366), 1.10 (359 and 378), and 1.54-1.56 on the two ~1 km heads the
# departure premium was fitted from. Call it +/-30% on the blind portion:
# at three quarters blind, +/-22% on the trip, labelled as an estimate.
#
# 0.75 and not higher because past it the measured part is carrying more than
# three times its own length, and there is a point where "estimated" stops
# being an adequate warning.
BLIND_DISTANCE_MAX_SHARE = 0.75

# How much more energy a blind stretch at the DEPARTURE takes than the trip's
# own average, because it is not an average piece of the trip: it is the first
# minutes of one. The cabin is being pulled down from a hot parked car, the
# drivetrain is cold, and the car is crawling out of a car park — all of it
# front-loaded, and none of it captured by a rate averaged over the whole drive.
#
# Measured against the car's own screen, comparing FRACTION of pack consumed
# rather than kWh, so the figure owes nothing to the capacity constant:
#
#   trip 332   0% blind   +0.6%   (control: no blind distance, no deficit)
#   trip 334   1.7%       -0.9%   implies 1.54x
#   trip 333   9.2%       -5.2%   implies 1.56x
#
# Two independent trips agreeing to two decimal places, monotonic in the blind
# share, with a zero-blind control that shows no deficit at all. 334's figure
# is a lower bound, since part of its shortfall is an uncounted arrival tail.
#
# Deliberately NOT applied at the arrival end. A blind arrival is the last
# minutes of a drive, where the same physics runs the other way: cabin already
# cool, drivetrain warm, and the car rolling to a stop. Holding the trip
# average there is the conservative choice and there is no measurement yet
# saying otherwise.
DEPARTURE_BLIND_LOAD = 1.55

# How far into a trip the departure premium above still applies. Past this the
# blind stretch is priced at the trip's own flat average like any other.
#
# The premium is a FIXED cost, not a proportional one: pulling a hot cabin
# down, warming a cold drivetrain and crawling out of a car park all happen
# once, in the opening minutes, and are finished long before a long blind
# stretch is. Multiplying the whole stretch by 1.55 treats a front-loaded cost
# as if it scaled with distance, which over-prices exactly as the blind share
# grows — the case where the correction matters most.
#
# One kilometre, which is what the original calibration itself implies: both
# trips DEPARTURE_BLIND_LOAD was fitted on had ~1 km of blind departure and
# measured 1.54 and 1.56 across the whole of it, so a premium confined to the
# first kilometre reproduces them exactly and changes nothing for that range.
#
# Set to 2.0 first, on trip 359 alone. Three trips with a blind head have since
# been checked against the car, and the whole-stretch ratio they actually show
# collapses as the stretch lengthens — the signature of a fixed front-load
# rather than a proportional one:
#
#   trip 378   2.98 km blind   1.10
#   trip 366   4.79 km blind   0.92
#   trip 359  10.09 km blind   1.10
#   trips 333/334  ~1 km       1.54, 1.56
#
# Against the car's own consumption those three came out +6.8%, +13.6% and
# +0.2% at a 2.0 km cap (mean +6.9%) and +2.1%, +8.6% and -1.7% at 1.0 (mean
# +3.0%). Dropping the premium altogether fits them better still (-0.9%), but
# that contradicts two direct measurements at ~1 km, and one kilometre is the
# only value that honours both ends of the evidence.
DEPARTURE_PREMIUM_MAX_KM = 1.0


# Blind share past which the trip's OWN measured rate stops being the better
# estimator and this car's fleet rate takes over for the unseen part.
#
# Holding Wh/km constant is sound while most of the trip carried a reading. It
# stops being sound when the reading covers a sliver: the sliver is then a
# small, biased sample of one trip rather than an estimate of it. Measured,
# trip 500 — 2.96 km of a 4.12 km drive unseen, so the rate came from 1.16 km
# of the slowest, most congested part and was then marked up 1.55x on top. It
# read 207 Wh/km against the car's 161.9.
#
# Priced instead at the median of this car's own measured trips, the same trip
# comes to 0.72 kWh against the car's 0.70. A median over a hundred-odd trips
# beats a quarter of one, which is the argument that does not rest on the
# single trip confirming it.
#
# No departure premium in that branch: a fleet median already contains
# everybody's departures, so charging one again would count it twice.
BLIND_RATE_FALLBACK_SHARE = 0.5


def energy_for_blind_distance(energy_kwh: float, distance_km: float,
                              blind_km: float,
                              departure_blind_km: float = 0.0,
                              fleet_wh_per_km: float | None = None) -> float:
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

    ``departure_blind_km`` is the portion of ``blind_km`` that sits at the
    START of the trip, and it is priced higher (DEPARTURE_BLIND_LOAD) because a
    trip's opening minutes genuinely cost more than its average. Pass it and
    the rest is treated as arrival-side, at the flat average.

    The refusal threshold still looks at the total blind distance, not the
    weighted one: the question it asks is how much of the trip is being
    inferred rather than measured, and weighting cannot add measurements.
    """
    measured = distance_km - blind_km
    if blind_km <= 0 or energy_kwh is None or energy_kwh < 0:
        return energy_kwh
    # Past BLIND_RATE_FALLBACK_SHARE the trip's own rate is a sliver, not a
    # sample — see that constant. Checked BEFORE the refusal below, and that
    # ordering is the whole point: BLIND_DISTANCE_MAX_SHARE exists because
    # projecting the trip's OWN rate across a large blind share is unsound,
    # and a fleet rate is not the trip's own rate. Behind the refusal this
    # branch could never run for the trips it was written for.
    #
    # Measured, trip 505: 6.278 km of an 8.0 km drive unseen, 78.5%, three
    # points past the refusal. It reported no energy and therefore no cost at
    # all — the exact outcome the pricing exists to prevent, produced by the
    # pricing declining. With the fleet rate it comes to about 1.39 kWh, or
    # 173 Wh/km, which is at least a number of the right kind.
    if fleet_wh_per_km and blind_km > distance_km * BLIND_RATE_FALLBACK_SHARE:
        return energy_kwh + blind_km * fleet_wh_per_km / 1000.0
    if (not energy_kwh or energy_kwh <= 0
            or measured <= 0 or blind_km > distance_km * BLIND_DISTANCE_MAX_SHARE):
        return energy_kwh
    # Only the first DEPARTURE_PREMIUM_MAX_KM of a blind departure carries the
    # premium — beyond that the opening-minutes costs it prices are over and
    # the stretch is ordinary driving (see DEPARTURE_PREMIUM_MAX_KM).
    departure = min(max(departure_blind_km, 0.0), blind_km, DEPARTURE_PREMIUM_MAX_KM)
    # The blind distance re-expressed as the equivalent amount of AVERAGE
    # driving, so one flat rate can still price it: a departure kilometre
    # counts for 1.55, an arrival kilometre for 1.
    weighted = departure * DEPARTURE_BLIND_LOAD + (blind_km - departure)
    return energy_kwh * (measured + weighted) / measured


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


# An arrival nobody saw still happened. Where signal dies before the car
# settles — a car park, a basement, a concrete stairwell of a building — the
# closing reading is wherever the last poll reached, and the remaining drive is
# invisible: no reading covers it, and the next one comes only once the car is
# moving again, by which point its odometer also carries the new trip's start.
#
# Nothing estimates that tail any more. The estimator belonged to the polled
# sleep-close, which no longer exists, and what remains is measurement: the
# odometer against the readings taken while the car sat parked, applied by
# /api/repair-arrivals once the ground is actually visible. An estimate had to
# be made at close time, before anything could see the tail; a measurement can
# simply wait.
#
# The cap survives it, because the question it answers outlived the estimate:
# how far past its recorded stop a trip may be extended before the likelier
# story is a journey nobody logged. Measured, a 1.82 km overnight gap at Home
# was exactly that, and folding it into the arriving trip would have buried
# the evidence.
ARRIVAL_EST_MAX_KM = GAP_CREEP_MAX_KM
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


# AC (home/destination) charging routes mains power through the car's onboard
# charger, which loses ~5% to heat converting it to DC for the pack — so
# Tesla's reported charge_energy_added for an AC session runs a few % above
# what actually reached the battery. DC (Supercharger) feeds the pack
# directly with negligible conversion loss, so it's left unadjusted. Without
# this, every implied-capacity reading from AC charges (most home charging)
# is inflated, which then inflates every trip's computed kWh by the same
# proportion (confirmed against real Tesla-app Current Drive readings that
# ran ~5% under the uncorrected figure across independent trips).
#
# 0.95 was that ~5%, taken as the generic figure for an onboard charger. This
# car has since measured its own, from two sources that had to be combined
# because neither sees both sides of the loss:
#
#   charger-side  three AC sessions of 56, 68 and 81 SoC points, agreeing to
#                 0.75% — 71.28, 71.50, 71.82 kWh per 100%
#   pack-side     four readings off the car's own energy screen — 68.14,
#                 68.69, 68.82, 69.01, median 68.67
#
# The ratio is what the onboard charger actually loses: 0.960, not 0.950.
# Rounded there and no further — the two medians carry about +/-0.7% between
# them, so 0.9603 would be precision the evidence cannot support.
#
# At 0.95 this car's measured pack came out 67.9 against a screen saying
# 68.67, an over-correction of 1.1%; at 0.96 it lands on 68.6.
#
# DC WAS left unadjusted, on one wide Supercharger session implying 70.86
# against the same screen median, with a note to revisit at three. Three
# arrived, and they say the split was never real:
#
#   the six highest-precision sessions on record (1.2-1.8%), raw, are
#   71.28 AC, 71.00 AC, 70.86 DC, 71.50 AC, 71.59 DC, 71.82 AC
#
# AC and DC interleave completely, mean 71.34, and against a pooled screen
# figure of 68.36 over 70.8 SoC points that is a factor of 0.958 — this same
# constant, for both. So it is no longer AC-specific.
#
# What made DC look different was its MEDIAN, 70.33 against AC's 71.14. DC's
# two best sessions are also its two highest, so the median of five sits below
# them while AC's sits among its own. Sorting by precision rather than taking
# a median over mixed precisions is the whole difference between a 2.9% type
# split and no split at all.
# Briefly suspected of being 0.95. Eight of the car's own drive screens were
# implying a usable pack of 67.7 kWh against the 68.4 measured from charges,
# and 0.95 in place of 0.96 would have closed that to 0.01 kWh — which looked
# less like arithmetic than like an answer.
#
# It did not survive its own next two samples. Pooling the readings (total kWh
# over total percent, which lets the rounding average out instead of
# compounding it trip by trip) puts the car's own figure at 67.97 against our
# 68.4 — 0.6%, where the whole-percent rounding alone is worth +/-1.7%. The
# gap was the small-percentage trips being read one at a time.
CHARGE_EFFICIENCY = 0.96
# Was AC_CHARGE_EFFICIENCY while it applied to one charge type. Kept as a name
# so an old reference cannot silently read as 1.0.
AC_CHARGE_EFFICIENCY = CHARGE_EFFICIENCY


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
    # Every charge type, not just AC. See CHARGE_EFFICIENCY: on the six
    # highest-precision sessions this car has recorded, AC and DC interleave
    # with no separation at all, and the apparent split that once justified
    # exempting DC was an artifact of taking a median over mixed precisions.
    cap *= CHARGE_EFFICIENCY
    return round(cap, 1) if 45.0 <= cap <= 95.0 else None


# Fleet Telemetry reports distances in miles and speeds in mph regardless of
# the car's display units — verified against this car three ways: RatedRange
# 71.47 at 25.456% SoC gives 452 km at 100% (the owner measures 453), every
# VehicleSpeed sample is the exact mph value of a whole km/h, and Odometer
# 19326.786 x 1.609344 is 31,103 km, which is what the dashboard shows.
# Temperature is Celsius, so not everything follows the same convention —
# check each field rather than assuming.
_TELEMETRY_SHIFT = {
    "ShiftStateP": "P", "ShiftStateD": "D", "ShiftStateR": "R",
    "ShiftStateN": "N", "ShiftStateInvalid": "P", "Unknown": "P",
}


# The car's four windows, in the order the polling path names them.
_TELEMETRY_WINDOWS = ("FdWindow", "FpWindow", "RdWindow", "RpWindow")


def _any_window_open(fields: dict[str, Any]) -> bool | None:
    """Whether any window reads as open, or None if the car said nothing.

    WindowStateClosed / PartiallyOpen / Opened / Unknown. Partially open is
    open: a window lowered an inch is a way in, and it is also what a window
    forced from outside looks like. Unknown is not a confirmed shut, so a car
    reporting nothing but Unknown stays unknown — the intrusion check treats
    None and False very differently, and inventing a False here would report a
    sealed car that nobody has actually looked at.
    """
    seen = [str(fields[k]) for k in _TELEMETRY_WINDOWS
            if fields.get(k) is not None and not str(fields[k]).endswith("Unknown")]
    if not seen:
        return None
    return any(v.endswith(("Opened", "PartiallyOpen")) for v in seen)


def snapshot_from_telemetry(fields: dict[str, Any], ts: float) -> dict[str, Any]:
    """Flatten accumulated telemetry field values into a sync snapshot.

    Same shape as snapshot_from_vehicle_data, so everything downstream — the
    drive/charge state machine, the splitter, the analysis — works unchanged.
    ``fields`` is the latest value seen for each key, not one message: the car
    sends only what changed, so a single record never describes the car.

    Fields the car is not configured to stream come back as None rather than a
    default. None means "unknown" and False means "confirmed off", and the
    parked-drain code already depends on telling those apart — inventing a
    False here would make a sleeping car look like one with everything
    verified off.
    """
    def num(key: str) -> float | None:
        """A number, whatever wrapper Tesla chose for it.

        Strings are accepted because the stream is not typed consistently —
        the same field can arrive as stringValue or doubleValue — and the
        alternative failure is silent: a rejected odometer becomes 0.0, which
        reads downstream as a car that has never moved rather than as a value
        that could not be parsed.
        """
        value = fields.get(key)
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def enum_str(key: str) -> str | None:
        """The enum string the car sent, unaltered, or None."""
        value = fields.get(key)
        return value if isinstance(value, str) and value else None

    # Enum values Tesla uses for "the car did not answer". They are not the
    # same as the field being absent, but they mean the same thing here.
    _UNREADABLE = ("SNA", "Unknown", "Invalid")

    def enum_flag(key: str, true_when: tuple[str, ...]) -> bool | None:
        """A boolean read off an enum string, by suffix.

        bool() of one of these is not a reading: every non-empty string is
        true, so ChargePortLatchDisengaged and ChargePortLatchEngaged both
        come out True and the latch can never report itself open. Suffixes are
        matched instead, and a value the car could not supply stays None
        rather than becoming a confident False.
        """
        raw = enum_str(key)
        if raw is None or any(raw.endswith(u) for u in _UNREADABLE):
            return None
        return any(raw.endswith(t) for t in true_when)

    def enum_word(key: str, prefix: str) -> str | None:
        """The tail of an enum string, once its type name is taken off.

        CabinOverheatProtectionModeStateFanOnly is "FanOnly" — which is the
        word the polled column already holds, so the two sources land in the
        same vocabulary instead of needing a mapping table invented between
        them. A value the car could not supply stays None.
        """
        raw = enum_str(key)
        if raw is None or any(raw.endswith(u) for u in _UNREADABLE):
            return None
        return raw[len(prefix):] if raw.startswith(prefix) else raw

    odo = num("Odometer")
    rated = num("RatedRange")
    speed = num("VehicleSpeed")
    charge_state = fields.get("DetailedChargeState") or ""
    location = fields.get("Location") or {}
    doors = fields.get("DoorState")
    sentry = fields.get("SentryMode")

    return {
        "ts": ts,
        "odo_km": (odo * MILES_TO_KM) if odo is not None else 0.0,
        "soc": num("Soc") or 0.0,
        "range_km": (rated * MILES_TO_KM) if rated is not None else 0.0,
        "charging": charge_state.endswith("Charging"),
        # Whichever side is delivering. A Supercharge reports DCChargingPower
        # and leaves the AC field at nothing, so reading only the AC one puts
        # a 250 kW session in the history at 0 kW — and a charge with no power
        # is one whose duration and cost cannot be checked against anything.
        # Measured on AC: ACChargingPower 7.5 kW with no DC figure at all, so
        # the max is the AC value and nothing changes for the charging this
        # car actually does.
        "charger_kw": max(num("ACChargingPower") or 0.0,
                          num("DCChargingPower") or 0.0),
        # Energy straight from the pack, which polling has never had: it is
        # measured rather than SoC multiplied by an estimated capacity, so a
        # trip's energy is a subtraction and the capacity constant leaves the
        # path entirely. Nothing downstream reads this yet.
        "energy_kwh": num("EnergyRemaining"),
        # Still 0, but no longer for want of knowing what the counters mean —
        # see below. What is missing now is only which of the two matches the
        # "Added" figure the car displays, and so what the polled history has
        # always stored.
        "energy_added_kwh": 0.0,
        # Measured, on the AC charge of 10 September.
        #
        # ACChargingEnergyIn is PER-SESSION: it read 16.70 days earlier and
        # 4.72 partway through this one, so it resets. It is in kWh and it
        # agrees with the power being reported — 4.715 to 5.218 over four
        # minutes is 7.55 kW against ACChargingPower's 7.5.
        #
        # DCChargingEnergyIn runs during an AC charge too, which the name does
        # not suggest. It is not "energy from a DC charger", it is the energy
        # that reached the pack: 4.48 to 4.84 over three minutes is 7.2 kW
        # against 7.55 going in at the wall. That ratio, about 95%, is the
        # onboard charger's efficiency, and it is the difference between the
        # kWh a session is billed for and the kWh the battery got.
        #
        # Still not written into energy_added_kwh. Which of the two matches
        # the "Added" figure the car itself displays — and so what the polled
        # history has always stored — has not been checked against a finished
        # session, and pricing every charge in the record on an unverified
        # guess is the one mistake here that would be expensive.
        "charge_energy_in_raw": num("ACChargingEnergyIn"),
        "dc_energy_in_raw": num("DCChargingEnergyIn"),
        # The third counter, and the only one that cannot reset under a
        # session. The two above are per-session and the first charge this app
        # ever recorded was joined halfway through, which left its deltas
        # measuring the part that was seen while the counters themselves had
        # been climbing since the plug went in. A lifetime total is immune to
        # both problems: a session is the difference of its ends whenever
        # those ends are observed, and a dropped record costs nothing because
        # the next one carries the same running total.
        #
        # Still raw and unconverted, name notwithstanding. The field is called
        # ...Kwh, and the odometer was called Odometer and arrived in miles.
        # It is checked against a quantity already known before anything is
        # derived from it — see the shadow charge, which now carries this
        # delta beside the two it already had.
        "lifetime_charged_raw": num("LifetimeEnergyChargedKwh"),
        "charge_state_raw": charge_state or None,
        "fast": charge_state.startswith("DetailedChargeStateDC"),
        "out_temp": num("OutsideTemp") if num("OutsideTemp") is not None else 20.0,
        "shift": _TELEMETRY_SHIFT.get(str(fields.get("Gear") or ""), "P"),
        "speed_kmh": (speed * MILES_TO_KM) if speed is not None else 0.0,
        "locked": bool(fields.get("Locked")),
        "lat": location.get("latitude") if isinstance(location, dict) else None,
        "lon": location.get("longitude") if isinstance(location, dict) else None,
        "sentry_mode": (sentry != "SentryModeStateOff") if sentry else None,
        # And the state itself, because the boolean above cannot answer what
        # the parked-drain fit asks of it. See BatteryReading.sentry_state.
        "sentry_state": sentry or None,
        "doors_open": (any(bool(v) for v in doors.values())
                       if isinstance(doors, dict) and doors else None),
        # Not streamed by the configured field set — Fleet Telemetry has no
        # equivalent of vehicle_data's is_user_present at all. None, not
        # False: the intrusion check is this field's only consumer, and
        # "unknown" is not the same claim as "definitely not there," even
        # though today's check treats them alike (see
        # settings.intrusion_confirm_sec for what actually tells them
        # apart on this path).
        "user_present": None,
        "car_wash_mode": False,
        # Read at last. The app has alerted on a car opened while parked since
        # long before telemetry existed, and that check covers windows as well
        # as doors — but this path reported them as unknown, so a window was
        # the one way in the stream could not see. Partially open counts:
        # a window lowered an inch is not shut.
        "windows_open": _any_window_open(fields),
        # CenterDisplay IS streamed, and is deliberately not mapped here.
        # Polling stores this as Tesla's integer code; telemetry reports an
        # enum string (DisplayStateDriving, DisplayStateSentry, ...) whose
        # correspondence to those integers is undocumented. Writing a guessed
        # mapping into the same column would put a silent error under every
        # comparison of the two sources — the odometer already taught that
        # lesson. The string is carried below instead, under its own name.
        # Now configured, and checked against Tesla's proto rather than
        # guessed: CabinOverheatProtectionMode is field 180, with states
        # Off / On / FanOnly. The polled column holds the same three words, so
        # the enum's tail maps onto it directly — no invented correspondence
        # of the kind the CenterDisplay integer would have needed.
        "cabin_overheat_protection": enum_word("CabinOverheatProtectionMode",
                                               "CabinOverheatProtectionModeState"),
        # Whether it is COOLING, which is not the same as being switched on.
        # HvacPowerStateOverheatProtect (field value 4) is the car saying the
        # system is running for that reason right now; the mode above only
        # says it is allowed to.
        "cabin_overheat_protection_actively_cooling": enum_flag(
            "HvacPower", ("OverheatProtect",)),
        # Off / On / Dog / Party. A climate keeper left running through a long
        # park is a large draw that this app has never been able to see, which
        # is why the standby attribution has had to say "climate (maybe)".
        "climate_keeper": enum_word("ClimateKeeperMode",
                                    "ClimateKeeperModeState"),
        # The enum as the car sends it — DisplayStateSentry, DisplayStateDog,
        # DisplayStateDriving and so on. Kept after the center_display_state
        # column went, and the distinction is the point: that column held
        # Tesla's POLLED integer code, which no streamed value can be turned
        # into without inventing a mapping, while this is the car's own word
        # for the same state and needs no mapping at all. Nothing reads it
        # yet; it is here so that whatever eventually wants display state
        # starts from the form that cannot be silently wrong.
        "display_state_raw": enum_str("CenterDisplay"),
        # HvacPower is an enum, not a number: HvacPowerStateOn / ...Off /
        # ...Precondition / ...OverheatProtect. Anything that is not plainly
        # off means the system is drawing power, which is what climate_on
        # means to every caller of it.
        "climate_on": enum_flag("HvacPower", ("On", "Precondition",
                                              "OverheatProtect")),

        # --- Fields polling has never had. Carried through unconverted.
        #
        # The odometer taught this: assuming a unit is the cheapest way to put
        # a systematic error underneath everything and have it look like the
        # car disagreeing with itself. Whether these counters are kWh or Wh is
        # undocumented, so they are recorded raw and checked against a
        # quantity already known — see the shadow trip, which carries both the
        # counter's delta and the EnergyRemaining delta so one real journey
        # settles the ratio.
        # LifetimeEnergyUsedDrive is marked "Semi-truck only" in Tesla's proto
        # and has never arrived on this car. Kept because a record that asked
        # for it should say so, not because it is expected.
        "energy_drive_raw": num("LifetimeEnergyUsedDrive"),
        "energy_regen_raw": num("LifetimeEnergyGainedRegen"),
        # The one that is not model-restricted, and the point of the second
        # field set. Carried raw: its units are undocumented, and assuming a
        # unit is how a systematic error gets buried under everything.
        "energy_used_raw": num("LifetimeEnergyUsed"),
        # How many phone keys and fobs the car will answer to. A key being
        # ADDED is how a stolen Tesla is prepared, and nothing else in the 270
        # fields would show it. Recorded, not yet alerted on: what this reads
        # normally — whether it settles, or ticks as phones come and go — has
        # not been watched yet, and an alarm built on an unwatched baseline
        # cries wolf until it is ignored.
        "paired_keys": num("PairedPhoneKeyAndKeyFobQty"),
        "charge_port_door_open": (bool(fields["ChargePortDoorOpen"])
                                  if "ChargePortDoorOpen" in fields else None),
        # Inverted, and only meaningful while someone is sitting there. Read
        # off the mode log rather than assumed, six transitions on 11
        # September, every one of them consistent:
        #
        #   12:21:03  false -> true   car stopping; driver unbuckles, still
        #                             seated (Gear R->P one second later)
        #   12:21:50  true  -> false  driver got out (occupancy fell 12:21:49)
        #   13:55:53  false -> true   driver got in (occupancy rose 13:55:53)
        #   13:57:15  true  -> false  got out again
        #   13:57:45  false -> true   got back in
        #   13:58:16  true  -> false  buckled and drove off (Gear P->D, same
        #                             second; occupancy stayed true throughout)
        #
        # That last one settles it. Every other transition also moves with
        # occupancy, so the field could have been occupancy under another
        # name — but here the seat stayed occupied and the flag cleared at the
        # exact moment the car was put in Drive, which is when a driver
        # buckles. So true means "someone is in that seat and not belted": the
        # warning condition, not the buckle. It read false throughout the
        # 11:48 journey, which under this reading means belted, and matches
        # the earlier puzzle of it reading false at 17 km/h.
        #
        # None while the seat is empty, because an empty seat's flag is false
        # and false must never be read here as "belted".
        "driver_belt_raw": fields.get("DriverSeatBelt"),
        "driver_belt": (
            None if ("DriverSeatBelt" not in fields
                     or not fields.get("DriverSeatOccupied"))
            else not bool(fields["DriverSeatBelt"])),
        # The pack's own notion of driving (BMSStateDrive). Trip boundaries
        # are inferred from Gear and speed; this is the car's own answer, and
        # is recorded to be compared against that inference rather than to
        # replace it before it has been checked.
        "bms_state": enum_str("BMSState"),
        # Occupancy, which says directly what a door event only implies: a
        # door can be opened by a passenger, for luggage, or not reported at
        # all if it opens and shuts inside the streaming interval.
        "seat_occupied": (bool(fields["DriverSeatOccupied"])
                          if "DriverSeatOccupied" in fields else None),
        # Kept for the raw record. num() is None here because the value is an
        # enum string; climate_on above is the reading of it.
        "hvac_raw": enum_str("HvacPower"),
        "inside_temp": num("InsideTemp"),
        # The pack's own temperature. Cold-weather losses are a property of
        # the battery, not of the air the efficiency chart currently plots.
        "pack_temp_c": num("ModuleTempMin"),
        # Road gradient. On this island it is likely the largest unexplained
        # term in per-trip Wh/km.
        "grade_pct": num("GradeEstimatePercent"),
        "charger_v": num("ChargerVoltage"),
        "charger_a": num("ChargeAmps"),
        "charger_phases": num("ChargerPhases"),
        # ChargePortLatchEngaged / ...Disengaged / ...Blocking / ...SNA. This
        # was bool() of the string, which is true for every one of them — a
        # latch that could never report itself open.
        "charge_port_latched": enum_flag("ChargePortLatch", ("Engaged",)),
        "charge_limit_soc": num("ChargeLimitSoc"),
    }


# How long the car must stay in P before the trip is called finished. Not for
# detecting the stop — Gear arrives the second it changes — but for the brief
# P that is part of a journey rather than the end of it: a three-point turn's
# P-R-D, a drive-through window, dropping someone at the door. Sitting in D at
# a light never reaches this path at all, since is_driving is true whenever
# the gear is not P.
SHADOW_SETTLE_SEC = 180.0
# Park with the driver still in the seat is WAITING, not arriving — someone
# is being picked up, a call is being finished, a queue is being sat in. The
# journey has not ended, and the car agrees: it kept one such drive whole as
# 35 minutes while this closed it at 14, because ten minutes of stillness was
# taken for an arrival.
#
# Ninety minutes instead. It costs nothing to be generous here, which is the
# part worth understanding: the trip's end is the moment P was engaged, not
# the moment this timer expires, so a longer wait cannot lengthen a trip. And
# a real arrival does not wait it out — the moment the driver leaves the seat
# the window becomes SHADOW_SETTLE_SEC measured from when the car first
# stopped, which by then has already passed, so it closes at once with the
# right end time.
#
# What the ninety minutes actually bounds is the case where nobody ever
# leaves and the car never sleeps. Left unbounded the trip would stay open
# forever; this closes it eventually without cutting anybody's wait in half.
SHADOW_SETTLE_NO_EXIT_SEC = 5400.0
# And closed at the last motion if the stream simply stops: a car that sleeps
# without sending a final ShiftStateP would otherwise leave a trip open for
# hours and then absorb the next journey into it.
SHADOW_GAP_SEC = 600.0
# How long to wait before closing a journey that went silent having just
# reported itself at walking pace or less. Same three minutes as the settle
# window a car gets when it parks in coverage, because it is the same event —
# the car has arrived, it simply could not say so.
SHADOW_ARRIVED_QUIET_SEC = 180.0
# No single shadow trip is longer than this. Not a real driving limit — a
# floor under nonsense, so a boundary that went wrong shows up as a missing
# trip rather than as a plausible-looking record with an impossible number in
# it. The car's whole odometer reads about 31,000 km.
SHADOW_MAX_KM = 2000.0

# The car's own answer to "is this journey over".
#
# BMSState is the battery management system's operating mode, and it is not a
# guess: measured against five boundaries this app derived independently, it
# entered Drive 1 to 13 seconds before the trip started and left it 3 to 27
# seconds after the trip ended. Nothing else available comes close.
#
# It is better than Gear for the question that has caused the most trouble
# here — a driver who shifts to Park and stays in the seat. Gear says Park
# the moment the lever moves, which is why this app had to wait ninety
# minutes before believing it; BMSState held Drive through a five-minute
# idle at 17:39 and only released at 17:50, when the drive was genuinely
# finished. The car knows, and says so.
#
# Drive means the drivetrain is live, not that the wheels are turning: the
# BMS sat in Drive for sixteen minutes at a standstill with the odometer
# unchanged. So this ends a trip; it never starts one, and distance still
# decides whether there was a trip at all.
BMS_DRIVE = "BMSStateDrive"

# EnergyRemaining is reported in steps of 0.02 kWh. A trip's energy is the
# difference of two of those readings, so each end contributes up to half a
# step of error and the figure carries one full step of uncertainty — 0.02
# kWh, whether the trip spent 0.3 kWh or 3. On a short drive that is several
# percent before anything else goes wrong, which is why a 487-metre trip can
# report 698 Wh/km and be neither a bug nor a measurement.
ENERGY_QUANTUM_KWH = 0.02
# And the step is not the big term. EnergyRemaining is streamed once a
# minute, so each end of a trip's bracket is a reading taken up to sixty
# seconds from the boundary it is supposed to mark — sixty seconds during
# which the car was drawing several kilowatts. That is worth about 0.06 kWh
# on this car's trips, three times the 0.02 step.
#
# Measured against nine trips judged by the car's own figures: the spread of
# the disagreement is 0.075 kWh and one sampling interval at each trip's own
# average power is 0.062. The same number. What looked like a systematic
# error was the sampling interval, and taking it as a percentage of a small
# trip made it look like a large one — a 1.0 kWh journey carries the same
# 0.06 kWh as a 2.5 kWh journey and reports three times the percentage.
# The fallback only. Every trip closed by this machine now carries the
# interval its OWN readings arrived at, measured from their timestamps —
# see _energy_gap_sec. This constant is what a trip without that measurement
# gets, which means the ones recorded before it existed, and they were all
# driven while the car was configured at sixty seconds.
#
# It is a constant rather than a date because a configuration change reaches
# the car when the car next connects, which may be hours or days after it was
# sent and is not knowable from here. Dating the cutover by hand meant
# guessing that moment, and guessing it early is the bad direction: it would
# have told every trip driven in the meantime that it was six times more
# precise than it was. Measuring the interval per trip removes the guess
# entirely, and keeps working the next time an interval changes.
ENERGY_SAMPLE_SEC = 60.0
# How many gaps between readings are enough to call the cadence measured. Two
# readings make one gap and one gap is an anecdote — a single record delayed
# by a reconnect would set the figure for the whole trip. Three is the point
# where the median stops being the only sample.
ENERGY_SAMPLE_MIN_GAPS = 3
# What a measured cadence is allowed to be. Below the first, something is
# wrong with the timestamps rather than fast with the car; above the second,
# the trip was streaming so poorly that the fallback is the honest answer.
ENERGY_SAMPLE_MIN_SEC = 2.0
ENERGY_SAMPLE_MAX_SEC = 300.0


def _energy_gap_sec(hist: dict | None) -> float | None:
    """The median gap between EnergyRemaining readings, from a trip's tally.

    A histogram of whole seconds rather than a list of them: a trip holds
    hundreds of readings and this lives in a JSON blob that is rewritten on
    every batch. The median, not the mean, because one blackout mid-trip is
    a single enormous gap and the mean would hand it the whole answer.
    """
    if not isinstance(hist, dict):
        return None
    pairs = []
    for key, count in hist.items():
        try:
            pairs.append((float(key), int(count)))
        except (TypeError, ValueError):
            continue
    pairs.sort()
    total = sum(c for _, c in pairs if c > 0)
    if total < ENERGY_SAMPLE_MIN_GAPS:
        return None
    half, seen = (total + 1) // 2, 0
    for gap, count in pairs:
        if count <= 0:
            continue
        seen += count
        if seen >= half:
            if gap < ENERGY_SAMPLE_MIN_SEC or gap > ENERGY_SAMPLE_MAX_SEC:
                return None
            return gap
    return None


# How long the stream must have been silent before a trip is closed without
# it. Only a safety margin: the trip's own end time comes from the snapshot
# taken when the car stopped, so waiting longer costs nothing but the delay
# before the trip appears. It exists so this can never race a live stream,
# where advance_shadow is the thing that should close the trip.
SHADOW_QUIET_SEC = 120.0


def settle_shadow(shadow: dict[str, Any], now_ts: float) -> dict[str, Any] | None:
    """Close a trip the car stopped reporting on, without a new snapshot.

    advance_shadow only runs when a record arrives, so every path out of an
    open trip needed the car to keep talking. It does not: a Tesla goes to
    sleep shortly after it is parked and the telemetry connection goes with
    it. The last record of the day is then the one that would have started the
    settle window, and the trip stays open until the next drive — which reads,
    from outside, as telemetry having missed the journey entirely.

    Called on a schedule instead. The end of the trip is still the moment the
    car stopped, not now, so a trip closed this way is the same trip that
    would have been emitted had the stream continued.
    """
    open_at, last = shadow.get("open"), shadow.get("last")
    if not open_at or not last:
        return None
    last_ts = float(last.get("ts") or 0.0)
    # Only ever act on a stream that has actually gone away. While records are
    # arriving, advance_shadow owns the decision and knows more than this does.
    if now_ts - last_ts < SHADOW_QUIET_SEC:
        return None

    still_since = shadow.get("still_since")
    if still_since is not None:
        window = (SHADOW_SETTLE_SEC if shadow.get("exit_seen")
                  else SHADOW_SETTLE_NO_EXIT_SEC)
        if now_ts - float(still_since) >= window:
            # Same rule as the live path: time from when it stopped, readings
            # from the last thing the car said while parked in that spot.
            return _shadow_close(shadow, shadow.get("still_snap") or last,
                                 readings=last)
        return None

    # The car never reported itself stationary, so it went silent while the
    # machine still considered it under way. Two very different things look
    # like this, and the last speed it managed to send separates them.
    #
    # Arriving: it reported walking pace or less and then stopped talking.
    # This car does that every day — it reaches its bay underground, loses
    # signal before it can send ShiftStateP, and the journey is over. Waiting
    # ten minutes to say so is ten minutes of a finished trip being absent.
    #
    # Still going: it reported road speed and then stopped talking. That is a
    # tunnel or a dead zone, the journey continues, and closing it early would
    # cut one drive into two.
    #
    # Neither wait affects a single figure. The trip ends at the last record
    # either way; the wait only decides how soon it can be read, and how
    # confident we are that there is nothing more to come.
    # If the last thing the car said was that its drivetrain had shut down,
    # there is nothing to wait for. advance_shadow closes on that the moment
    # it arrives, so this only catches the case where the record landed and
    # the car then went quiet inside SHADOW_QUIET_SEC — but that is exactly
    # what parking underground looks like.
    if (shadow.get("bms_seen_drive")
            and last.get("bms_state") not in (None, BMS_DRIVE)):
        shadow["ended_by_bms"] = True
        return _shadow_close(shadow, shadow.get("still_snap") or last,
                             readings=last)

    arriving = float(last.get("speed_kmh") or 0.0) <= ZERO_SPEED_KMH
    if now_ts - last_ts > (SHADOW_ARRIVED_QUIET_SEC if arriving
                           else SHADOW_GAP_SEC):
        shadow["stream_lost"] = True
        return _shadow_close(shadow, last)
    return None


def advance_shadow(shadow: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any] | None:
    """Step the shadow trip machine with one telemetry snapshot.

    Mutates ``shadow`` and returns a finished trip, or None. Deliberately does
    not reuse the polling state machine: almost all of that code exists to
    reconstruct what happened between two snapshots minutes apart — blind
    distance, departure premiums, arrival tails, estimated energy. Telemetry
    reports the gear change itself, so a trip here is bounded by what the car
    said rather than inferred from what it had done by the time we asked.

    Energy is a subtraction of EnergyRemaining, so no capacity constant enters
    the calculation. That is the whole reason this is worth building.
    """
    ts = float(snap.get("ts") or 0.0)
    # A vehicle out of coverage buffers and replays later — every record
    # carries isResend for exactly that reason. Replayed records arrive after
    # newer ones, and taking them at face value would read as the odometer
    # jumping backwards and the car teleporting. Distance survives either way
    # because the odometer is cumulative; it is the boundaries that would be
    # corrupted, so anything older than what the machine has already seen is
    # counted and skipped.
    seen_ts = float(shadow.get("seen_ts") or 0.0)
    if ts and seen_ts and ts < seen_ts:
        shadow["out_of_order"] = int(shadow.get("out_of_order") or 0) + 1
        return None
    shadow["seen_ts"] = max(ts, seen_ts)
    last = shadow.get("last")
    open_at = shadow.get("open")
    done = None

    # How often EnergyRemaining actually arrives, tallied while a trip is
    # open. A trip's energy is the difference between two readings of it, so
    # each end of that bracket is stale by up to one interval — and how large
    # that interval is cannot be assumed from here. It is a setting on the
    # car, changed by a configuration the car adopts whenever it next
    # connects, which may be days after it was sent.
    #
    # Counted on CHANGES of the value, not on records carrying the field: the
    # composite carries the last value forward, so every snapshot reports
    # EnergyRemaining once any record has. The car sends the field when it
    # moves and under load it always moves, so a change is a send.
    e_val = snap.get("energy_kwh")
    if e_val is not None:
        e_prev, e_ts = shadow.get("e_val"), float(shadow.get("e_ts") or 0.0)
        if e_prev is None or e_val != e_prev:
            gap = ts - e_ts
            # Not across a blackout. A car that went quiet for an hour was
            # not sampling once an hour; it was not sampling at all, and the
            # gap log is where that belongs.
            if open_at and e_ts and 0.0 < gap <= SHADOW_GAP_SEC:
                hist = shadow.setdefault("e_gaps", {})
                key = str(int(round(gap)))
                hist[key] = int(hist.get(key) or 0) + 1
            shadow["e_val"] = e_val
            shadow["e_ts"] = ts

    # A stream that stops mid-trip closes it at the last motion seen, not at
    # the next record — which could be the following morning.
    if open_at and last and ts - float(last["ts"]) > SHADOW_GAP_SEC:
        done = _shadow_close(shadow, last)
        open_at = None

    # The car's own verdict, taken before the gear is consulted. Checked here
    # rather than inside the stopped branch because a car that loses signal
    # mid-manoeuvre leaves the composite reading Drive, and a trip held open
    # by a stale gear would never reach that branch to be closed at all.
    #
    # The journey ends where the car stopped, not where the BMS got round to
    # saying so — still_snap if it was seen to stop, otherwise the last thing
    # it sent. The closing odometer comes from this snapshot, which is newer
    # and measures the arrival better.
    #
    # Never while the car is moving. Only Drive, Support and Standby have
    # been observed and the drivetrain must be live for the wheels to turn,
    # so this should be unreachable — which is exactly why it is guarded. An
    # unrecognised state arriving mid-journey would otherwise end a trip at
    # speed, and this app has already been taught once what a single
    # unexpected value does to a boundary. The trip simply stays open until
    # the car is next seen stopped, which is where it ends anyway.
    if (open_at and shadow.get("bms_seen_drive")
            and float(snap.get("speed_kmh") or 0.0) <= ZERO_SPEED_KMH):
        bms_now = snap.get("bms_state")
        if bms_now is not None and bms_now != BMS_DRIVE:
            shadow["ended_by_bms"] = True
            done = _shadow_close(shadow, shadow.get("still_snap") or last or snap,
                                 readings=snap) or done
            open_at = None

    # A car drawing power is a car that has arrived. No timer, no inference,
    # and no need for anyone to have got out — plugging in and sitting in the
    # car while it charges is an ordinary thing to do, and it is exactly the
    # case where the driver-left test says nothing and the BMS may hold Drive
    # for another ten minutes.
    #
    # A backstop rather than a discovery: when the car can say it is charging
    # it can usually say the rest too. It is the only signal here that cannot
    # be wrong about what it means.
    #
    # It can still be stale, which is a different thing, and the same trap the
    # gear and the BMS both set. DetailedChargeState is sent on change, so a
    # composite carrying Charging from the session the driver has just
    # unplugged from would close the new trip the instant it opened. Requiring
    # the car to have been seen NOT charging first makes this a transition
    # rather than a reading — and unplugging necessarily happens before
    # driving off, so the honest case always qualifies.
    if open_at and snap.get("charging") and shadow.get("seen_unplugged"):
        shadow["ended_by_charge"] = True
        done = _shadow_close(shadow, shadow.get("still_snap") or last or snap,
                             readings=snap) or done
        open_at = None

    # Gear and VehicleSpeed arrive as separate records — gear on change,
    # speed every ten seconds — so a car that selects Park is described by a
    # composite still carrying the speed it had a moment earlier, and
    # is_driving reads that as motion until the next speed record lands.
    # Measured on trip 537: Park at 12:33:04, trip closed at 12:33:14. You
    # cannot select Park while moving, so the gear settles it on its own.
    #
    # For ENDING a trip only. A composite that has not yet heard a Gear
    # record reads P, and Gear is sent on change — so requiring a non-P gear
    # to START would mean a trip that never opens until the next time the
    # lever moves, which may be at its destination. Opening still asks for
    # real motion, which is what keeps a stale gear from inventing a journey.
    # Idle and climate, accumulated while the trip is open.
    #
    # Both were polling's guesses before this. idle_tracked was false on
    # every streamed trip, so the dashboard marked the most accurate journeys
    # it has recorded as estimates — correctly, since nothing had measured a
    # stop. Telemetry is the better instrument for it: VehicleSpeed arrives
    # every ten seconds where a poll sees the car once a minute at best.
    #
    # A stop counts only once it has lasted IDLE_STREAK_MIN, which is what
    # separates a run of traffic lights from actually waiting somewhere.
    # Climate counts by the second it was on, which is what decides how much
    # of a trip's energy was not propulsion.
    if open_at and last:
        gap = ts - float(last["ts"])
        # Nothing is inferred across a blackout. A car that went quiet for an
        # hour was not idling for an hour, and counting it as such would put
        # a fictional stop into the one figure this exists to measure.
        if 0.0 < gap <= SHADOW_GAP_SEC:
            # Outside temperature, weighted by the seconds it applied for.
            #
            # The trip used to record the reading taken at the INSTANT it
            # ended, which is one sample of a quantity the climate model
            # integrates over the whole drive — and the model costs 0.08 kW
            # per degree, so a single degree of sampling error is worth half
            # the disagreement that model is currently being judged on. An
            # afternoon run that ends in an underground car park and an
            # evening one that ends in the open are not sampling the same
            # thing, and the stream sends this every five minutes throughout.
            stopped = float(snap.get("speed_kmh") or 0.0) <= ZERO_SPEED_KMH
            if last.get("out_temp") is not None:
                # Held while stationary, committed only when the car moves
                # again — the same shape as idle_run_since below, and for the
                # same reason. A trip ends at the moment the car stopped, but
                # records keep arriving through the settle window, so a plain
                # accumulator keeps integrating after the journey is over.
                #
                # Measured: a 15-minute drive entirely at 40 C, parked
                # somewhere 20 C, recorded 36.5. The accumulator had run to
                # 1090 seconds against a 900-second trip, and the extra 190
                # were the arrival weighted at the parked temperature — which
                # is the endpoint-sampling bias this averaging exists to
                # remove, quietly reintroduced inside a mean that looks
                # principled. 3.5 C is 0.28 kW, larger than the whole
                # disagreement being measured.
                #
                # A stop mid-journey is different and must still count: the
                # car sits in traffic under the same climate load, and that
                # stretch is part of the drive. Committing the hold on the
                # next movement keeps those and drops only the last one, which
                # is the arrival by definition — nothing followed it.
                temp = float(last["out_temp"])
                if stopped:
                    shadow["temp_hold_sec"] = float(
                        shadow.get("temp_hold_sec") or 0.0) + gap
                    shadow["temp_hold_sum"] = float(
                        shadow.get("temp_hold_sum") or 0.0) + temp * gap
                else:
                    shadow["temp_sec"] = (float(shadow.get("temp_sec") or 0.0)
                                          + float(shadow.pop("temp_hold_sec", 0.0) or 0.0)
                                          + gap)
                    shadow["temp_sum"] = (float(shadow.get("temp_sum") or 0.0)
                                          + float(shadow.pop("temp_hold_sum", 0.0) or 0.0)
                                          + temp * gap)
            if last.get("climate_on"):
                shadow["climate_sec"] = float(shadow.get("climate_sec") or 0.0) + gap

            # Distance, time and ENERGY banded by the speed they happened at.
            #
            # A trip has only ever carried two speed numbers, an average and a
            # peak, and a peak is one sample. That is enough to put a whole
            # trip in one box and not enough to say a trip was partly one thing
            # and partly another — so a 20 km motorway run with 8 km of town
            # either side was averaged into a single row and the motorway part
            # disappeared. Measured on trip 735: 28 km at an average of 75 with
            # a peak of 160, which is neither of the two things it actually was.
            #
            # The speed here is IMPLIED from the odometer over the interval,
            # not read off the speedometer: it is the average over exactly the
            # stretch being attributed, where an instantaneous sample is the
            # value at one instant of it. And the energy is the car's own
            # EnergyRemaining delta over the same interval, so each band's kWh
            # is measured rather than apportioned from the trip total by
            # distance — which would hand highway and city the same Wh/km and
            # destroy the one distinction the matrix exists to draw.
            # Bounded by two real ODOMETER readings, not by two records. The
            # car streams Odometer every 30 seconds and VehicleSpeed and
            # EnergyRemaining every 10, so the composite repeats the same
            # odometer two records out of three — and measuring a step against
            # the RECORD gap divided 30 seconds of distance by 10 seconds of
            # clock, reading three times the true speed, while the two
            # intervals that showed no movement banded their energy at zero
            # km/h against no distance at all.
            #
            # Simulated on a steady 60 km/h, 150 Wh/km drive: every kilometre
            # landed in the 160+ bucket at 50 Wh/km and two thirds of the
            # energy landed in the 0 bucket with nothing to divide by. That is
            # exactly what the matrix showed live — 114 km of "Fast Highway"
            # at 6 Wh/km beside 4 km of "Slow City" at 3511 — on a car whose
            # average speed for the window was 36 km/h.
            #
            # So the anchor carries the odometer, the clock and the energy
            # counter from the last banded step, and a step is taken only when
            # the odometer itself moves. The intervals in between are not
            # discarded: their seconds and their kWh belong to the stretch the
            # next reading completes, which is where they happened.
            odo_now = snap.get("odo_km")
            if odo_now is not None:
                anchor_km = shadow.get("band_odo")
                # Not "span": the idle-run block below owns that name, and
                # these two measure different stretches.
                band_span = ts - float(shadow.get("band_ts") or ts)
                step_km = (float(odo_now) - float(anchor_km)
                           if anchor_km is not None else None)
                if step_km is not None and 0.0 < step_km < 10.0 and band_span > 0.0:
                    kmh = step_km / (band_span / 3600.0)
                    # Ten-wide buckets keyed by their lower edge, topping out
                    # at 160. Fixed rather than derived from the mode
                    # thresholds, because those are tunable and this is written
                    # once at ingest: fine buckets can be summed to any cut
                    # later, a bucket cut to today's threshold cannot.
                    band = str(int(min(kmh // 10.0 * 10.0, 160.0)))
                    bands = shadow.setdefault("bands", {})
                    slot = bands.setdefault(band, [0.0, 0.0, 0.0])
                    slot[0] += step_km
                    slot[1] += band_span
                    e_a, e_b = shadow.get("band_e"), snap.get("energy_kwh")
                    if e_a is not None and e_b is not None:
                        used = float(e_a) - float(e_b)
                        # Regen makes this genuinely negative and that is real,
                        # so it is kept signed. Only a jump too large to be one
                        # stretch is dropped, which is a replay rather than a
                        # car.
                        if abs(used) < 5.0:
                            slot[2] += used
                    shadow["band_odo"] = float(odo_now)
                    shadow["band_ts"] = ts
                    shadow["band_e"] = snap.get("energy_kwh")
                elif step_km is None or step_km < 0.0 or step_km >= 10.0:
                    # Nothing to measure from yet, or a rewind/replay. Start
                    # the next step here rather than band one that never
                    # happened.
                    shadow["band_odo"] = float(odo_now)
                    shadow["band_ts"] = ts
                    shadow["band_e"] = snap.get("energy_kwh")
                # step_km == 0: the odometer has not moved yet. The anchor is
                # deliberately left where it is — see above.

            run_since = shadow.get("idle_run_since")
            if stopped and run_since is None:
                # The moment the car came to rest, counted. idle_min only ever
                # counted stops of five minutes or more — deliberately, since a
                # commute through a dozen lights is driving rather than idling
                # — which leaves nothing at all measuring stop-start traffic.
                # Trip 744 crawled 9.7 km at 18.5 km/h through peak hour and
                # recorded zero idle. This is the count that says otherwise.
                shadow["stops"] = int(shadow.get("stops") or 0) + 1
                shadow["idle_run_since"] = float(last["ts"])
            elif not stopped and run_since is not None:
                span = float(last["ts"]) - float(run_since)
                if span >= IDLE_STREAK_MIN * 60.0:
                    shadow["idle_sec"] = float(shadow.get("idle_sec") or 0.0) + span
                shadow["idle_run_since"] = None
        else:
            # A blackout, or two records sharing a timestamp. The band anchor
            # must not reach across it: the car drove somewhere in there and
            # nothing measured how fast, so a step spanning it would band real
            # distance at an invented speed. Start afresh on this side of it.
            shadow["band_odo"] = snap.get("odo_km")
            shadow["band_ts"] = ts
            shadow["band_e"] = snap.get("energy_kwh")

    parked_gear = (snap.get("shift") or "P") == "P"
    if is_driving(snap) and not (open_at and parked_gear):
        # Not without an odometer to start from. A telemetry message carries
        # only what changed, so a composite that has not yet seen an Odometer
        # record reports 0.0 — and Odometer is streamed every 30 seconds while
        # VehicleSpeed comes every 10, so after any reset of the store the
        # first driving snapshot arrives before the first odometer one. A trip
        # opened there measures from zero and closes against the real reading:
        # 31,127 km, written as a single journey.
        #
        # Nor without actual motion. A trip CONTINUES on gear alone, because a
        # car stopped at a light is still in Drive and still on its journey.
        # It must not BEGIN on gear alone: Gear streams only when it changes,
        # so a car that loses signal mid-manoeuvre and never sends
        # ShiftStateP leaves the composite reading Drive for as long as it
        # stays offline — observed, reversing into an underground bay. When it
        # reconnects, still parked, that stale gear would open a journey it is
        # not on. Speed is reported every ten seconds and refreshes on
        # reconnect, so requiring it to open is what keeps a stale gear from
        # inventing a trip.
        if (not open_at and float(snap.get("odo_km") or 0.0) > 0.0
                and float(snap.get("speed_kmh") or 0.0) > 0.0):
            shadow["open"] = dict(snap)
            shadow["max_speed_kmh"] = 0.0
            # Per trip. The cadence is a property of the journey it was
            # measured during, not of the car in general — the configuration
            # can change between two trips on the same day.
            #
            # The anchor goes with the histogram. Clearing one and not the
            # other measured this trip's first gap from the previous trip's
            # last reading, across the park between them — and any park under
            # SHADOW_GAP_SEC passes the blackout guard, which is every park
            # shorter than ten minutes. The median absorbs one outlier among
            # many; what it cannot absorb is the count, and a trip owning two
            # gaps of its own has too few to report a cadence at all until
            # the park is counted as a third.
            shadow["e_gaps"] = {}
            shadow.pop("e_ts", None)
            shadow.pop("e_val", None)
            shadow.pop("temp_sec", None)
            shadow.pop("temp_sum", None)
            shadow.pop("bands", None)
            # The band anchor starts where the journey does — odometer, clock
            # and energy counter together, so the first stretch is measured
            # from the trip's own beginning rather than from wherever the
            # previous one left off.
            shadow["band_odo"] = snap.get("odo_km")
            shadow["band_ts"] = ts
            shadow["band_e"] = snap.get("energy_kwh")
            shadow.pop("stops", None)
            shadow.pop("temp_hold_sec", None)
            shadow.pop("temp_hold_sum", None)
            open_at = shadow["open"]
        elif not open_at:
            # Nothing to open yet, and nothing to measure until there is.
            shadow["last"] = dict(snap)
            return done
        shadow["still_since"] = None
        shadow.pop("still_snap", None)
        shadow.pop("exit_seen", None)
        shadow["max_speed_kmh"] = max(
            float(shadow.get("max_speed_kmh") or 0.0), float(snap.get("speed_kmh") or 0.0))
        # Only a BMS that was seen in Drive during THIS trip may end it. The
        # composite holds the last value the car sent, and BMSState is sent
        # only when it changes — so a trip opened while the composite still
        # reads Support from hours ago would be closed by its own stale
        # value at the first red light. Same trap as the stale gear, same
        # answer: require the evidence to have arrived during the journey.
        if snap.get("bms_state") == BMS_DRIVE:
            shadow["bms_seen_drive"] = True
        if not snap.get("charging"):
            shadow["seen_unplugged"] = True
    elif open_at:
        still_since = shadow.get("still_since")
        # Did anyone actually leave? Occupancy answers it directly; a door
        # only implies it, and can be opened by a passenger, for luggage, or
        # not reported at all when it opens and shuts inside the streaming
        # interval. Locked is no use either — this car reports Locked true
        # mid-journey from auto-lock, so it would end trips still running.
        if snap.get("seat_occupied") is False:
            shadow["exit_seen"] = True
        elif snap.get("seat_occupied") is None and snap.get("doors_open"):
            shadow["exit_seen"] = True
        if still_since is None:
            # Remember the car as it was when it stopped, not as it will be
            # once the settle window expires. Closing on the later snapshot
            # would add up to SHADOW_SETTLE_SEC to every trip's duration,
            # understate its average speed by the same amount, and charge it
            # for three minutes of parked accessory draw.
            shadow["still_since"] = ts
            shadow["still_snap"] = dict(snap)
        else:
            window = (SHADOW_SETTLE_SEC if shadow.get("exit_seen")
                      else SHADOW_SETTLE_NO_EXIT_SEC)
            if ts - float(still_since) >= window:
                # Time from when it stopped; readings from now. The car has
                # not moved in between, so the odometer and energy that have
                # arrived since are the true end-of-trip values — while the
                # ones held at the instant P was reached can be half a minute
                # stale, which on this car's 30-second Odometer interval loses
                # a quarter of a kilometre off every arrival.
                done = _shadow_close(shadow, shadow.get("still_snap") or snap,
                                     readings=snap)

    shadow["last"] = dict(snap)
    return done


# How long after a trip ended a late reading may still describe its arrival,
# and how far the odometer may move in that window. A car buffers while out of
# coverage and replays on reconnect, so the reading that measures where a trip
# truly ended can arrive minutes — or a night — after the trip was closed.
SHADOW_TAIL_SEC = 900.0
# Tightened from 1.0 km on the driver's own account of the thing being
# measured: entering a carpark without signal costs 200 to 400 metres. Every
# gap this app has recorded agrees — 0.004, 0.061, 0.082, 0.161, 0.338, and
# about 0.31 on the trip of 10 September.
#
# Tighter because of which way this bound fails. Too tight and a real arrival
# is refused, the distance stays in unaccounted_km, and it is visible there to
# be argued about. Too loose and a movement that was never an arrival is
# quietly added to a trip that did not drive it, and nothing ever says so. A
# bound with a silent failure on one side belongs near the evidence, not three
# times past it — and if a genuine arrival ever exceeds this, it will appear
# as unaccounted_km and can raise the number with a measurement behind it.
SHADOW_TAIL_MAX_KM = 0.6


def amend_closed_trip(trip: dict[str, Any], snap: dict[str, Any]) -> bool:
    """Fold a late arrival reading into a trip that has already been closed.

    This is the same correction the live path makes with ``readings``: a
    stationary car keeps reporting, and a reading taken shortly after it parked
    measures the end of the journey better than one taken at the instant it
    stopped, which on a 30-second Odometer interval can be a quarter of a
    kilometre stale. The live path can only apply it when the reading arrives
    before the trip closes.

    It often does not. A car parked underground buffers what it cannot send and
    replays it on reconnect, so the readings that measure the arrival turn up
    after the trip has been closed on a timeout — measured, transmitted, and
    then discarded for being late. Trips that end in the same carpark every
    evening lose the same tail every evening, which is a bias rather than
    noise.

    Time is not touched. The car stopped when it stopped; only what it had
    driven and spent by then is being read more accurately.

    The window and the distance cap are what keep this honest. A car shunted a
    few metres in the same carpark within the window would have that movement
    folded into the trip it just finished rather than recorded separately — a
    misattribution the bounds keep under a kilometre, and the alternative is
    discarding every real arrival to avoid it.
    """
    if not trip or is_driving(snap):
        return False
    end_ts, ts = trip.get("end_ts"), snap.get("ts")
    odo, end_odo = snap.get("odo_km"), trip.get("end_odo_km")
    if end_ts is None or ts is None or odo is None or end_odo is None:
        return False
    # Only the window just after this trip, and only forwards. A reading from
    # before the end is stale in the ordinary sense; one from far after belongs
    # to whatever the car did next.
    if not 0.0 <= float(ts) - float(end_ts) <= SHADOW_TAIL_SEC:
        return False
    gain = round(float(odo) - float(end_odo), 3)
    if gain <= 0.0 or gain > SHADOW_TAIL_MAX_KM:
        return False

    start_odo = trip.get("start_odo_km")
    if start_odo is None:
        return False
    distance = round(float(odo) - float(start_odo), 3)
    trip["end_odo_km"] = round(float(odo), 3)
    trip["distance_km"] = distance
    # Cumulative, so re-running this on a later reading refines the same trip
    # rather than compounding — the figures above are recomputed from the
    # bracket each time, never adjusted by a delta.
    trip["tail_amended_km"] = round(
        float(trip.get("tail_amended_km") or 0.0) + gain, 3)

    # The recovered metres were driven, so they cost something — but the
    # reading that measures them was also taken while the car sat there
    # drawing power, and subtracting it raw charges the journey for up to
    # fifteen minutes of standby it did not spend driving. So the energy is
    # added the same way recover_sleep_gap adds it, at the trip's own Wh/km,
    # and the two recovery paths agree with each other instead of one
    # measuring standby and the other inferring propulsion.
    whkm = trip.get("wh_per_km")
    if whkm and trip.get("energy_kwh") is not None:
        gained_kwh = round(gain * float(whkm) / 1000.0, 3)
        trip["energy_kwh"] = round(float(trip["energy_kwh"]) + gained_kwh, 3)
        trip["end_energy_kwh"] = snap.get("energy_kwh")
    energy = trip.get("energy_kwh")
    trip["wh_per_km"] = (round(energy * 1000.0 / distance, 1)
                         if energy and distance > 0 else None)
    minutes = float(trip.get("duration_min") or 0.0)
    if minutes > 0:
        trip["avg_speed_kmh"] = round(distance / (minutes / 60.0), 1)
    if snap.get("soc") is not None:
        trip["soc_end"] = snap.get("soc")
    return True


# A plug-in that moved less than this was somebody testing the cable, not a
# charge. Well above the 0.02 kWh step EnergyRemaining moves in.
CHARGE_MIN_KWH = 0.05
# Silence longer than this ends a charge at the last thing the car said. A
# charging car is awake and talking every thirty seconds; ten minutes of
# nothing means the session is over and the car went to sleep, not that it is
# still drawing.
CHARGE_GAP_SEC = 600.0


def advance_charge(shadow: dict[str, Any], snap: dict[str, Any]) -> dict[str, Any] | None:
    """Step the shadow CHARGE machine with one telemetry snapshot.

    Exists to settle one question and to be honest about not having settled
    it. The car reports two energy counters during a charge and the app has
    never known which one the polled history stores.

    What eighteen recorded minutes of the 10 September session showed: the
    two counters agree to within a percent of each other, and both sit about
    11% above the pack's own level. So they are not a wall meter and a pack
    meter — an earlier reading that said 95% was three minutes of the 0.02
    kWh step on DCChargingEnergyIn, not a converter. The loss is between
    either counter and EnergyRemaining.

    Which lines up with the two figures from outside the car: the charging
    network billed 19.799 kWh and the car itself called it 18 kWh added,
    90.9% — against 89% measured here. That points at the pack level being
    what the car reports as "added", and it is still one partial session
    against one rounded display, so nothing is written from it yet. Guessing
    would misprice every charge in the history, and a wrong price is not
    visibly wrong.

    Mutates ``shadow`` and returns a finished charge, or None.
    """
    # `is None`, not falsiness. A timestamp of zero is a real epoch — the
    # first second of 1970 — and while no car will ever send one, a guard
    # that quietly discards a valid value is the kind that is found later by
    # something else going wrong. It was found here by a test at ts=0, which
    # opened no session at all.
    if snap.get("ts") is None:
        return None
    ts = float(snap["ts"])
    open_at = shadow.get("open")
    last = shadow.get("last")
    charging = bool(snap.get("charging"))

    # A charging car talks constantly, so a long silence is the session
    # ending rather than a pause in it.
    if open_at and last and ts - float(last["ts"]) > CHARGE_GAP_SEC:
        done = _charge_close(shadow, last)
        open_at, last = None, None
        if not charging:
            shadow["last"] = dict(snap)
            return done
        # Straight into a new session, which the reset check below also
        # catches — but only if a counter is being reported, and it may not
        # be on the record that reopens this.
        shadow["open"] = dict(snap)
        shadow["peak_kw"] = float(snap.get("charger_kw") or 0.0)
        shadow["fast"] = bool(snap.get("fast"))
        shadow["last"] = dict(snap)
        return done

    if charging and not open_at:
        shadow["open"] = dict(snap)
        shadow["peak_kw"] = float(snap.get("charger_kw") or 0.0)
        shadow["fast"] = bool(snap.get("fast"))
        shadow["last"] = dict(snap)
        return None

    # A parked car that is not charging has no session to remember, and this
    # store is written on every batch the receiver posts. Keeping a full
    # forty-field snapshot in it for a car doing nothing meant serialising
    # and committing one every time, all day, to record that nothing had
    # happened. Cleared instead, so the blob settles to {} and the
    # write-only-what-changed rule in the ingest can skip it entirely.
    if not charging and not open_at:
        shadow.pop("last", None)
        return None

    if charging and open_at:
        # ACChargingEnergyIn is per-session: it read 16.70 days before this
        # session and 4.72 during it, so it resets rather than accumulating.
        # A counter that goes backwards is therefore a new session starting,
        # not a bad reading — and carrying on would report one charge with a
        # negative meter.
        started = _charge_counter(open_at)
        now_counter = _charge_counter(snap)
        if (started is not None and now_counter is not None
                and now_counter < started - 1e-9):
            done = _charge_close(shadow, last or snap)
            shadow["open"] = dict(snap)
            shadow["peak_kw"] = float(snap.get("charger_kw") or 0.0)
            shadow["fast"] = bool(snap.get("fast"))
            shadow["last"] = dict(snap)
            return done
        shadow["peak_kw"] = max(float(shadow.get("peak_kw") or 0.0),
                                float(snap.get("charger_kw") or 0.0))
        # Whether ANY snapshot in the session reported DC, not just the two
        # that happen to be kept as ``open``/``last``. Those are the record
        # the session started on and whichever the machine most recently saw
        # — and a DC session opens on "Starting" and can close on "Complete",
        # neither of which is the DetailedChargeStateDC... string this reads.
        # A 41 kWh Supercharge stop was filed as AC because both ends of it
        # missed the state that every snapshot in between was reporting.
        # peak_kw already accumulates the same way across the whole session
        # for the same reason: the two ends are not the session, they are
        # just what is left of it once the middle has been thrown away.
        shadow["fast"] = bool(shadow.get("fast")) or bool(snap.get("fast"))
        shadow["last"] = dict(snap)
        return None

    if open_at and not charging:
        # Closed on the last snapshot that was still charging, not on this
        # one: unplugging is what makes charging false, and the counters have
        # no reason to still be right afterwards.
        done = _charge_close(shadow, last or snap)
        shadow["last"] = dict(snap)
        return done

    shadow["last"] = dict(snap)
    return None


def _charge_counter(snap: dict[str, Any]) -> float | None:
    """Whichever wall meter this session is being measured by."""
    ac = snap.get("charge_energy_in_raw")
    return ac if ac is not None else snap.get("dc_energy_in_raw")


def settle_charge(shadow: dict[str, Any], now_ts: float) -> dict[str, Any] | None:
    """Close a charge the car stopped reporting on, without a new snapshot.

    The same hole settle_shadow fills for trips: a car that finishes charging
    and goes to sleep sends nothing more, so the session would stay open until
    the next plug-in and read as one enormous charge.
    """
    open_at, last = shadow.get("open"), shadow.get("last")
    if not open_at or not last:
        return None
    if now_ts - float(last.get("ts") or 0.0) <= CHARGE_GAP_SEC:
        return None
    return _charge_close(shadow, last)


def _charge_close(shadow: dict[str, Any], end: dict[str, Any]) -> dict[str, Any] | None:
    """Emit the open charge, ending at ``end``, and clear the machine."""
    start = shadow.pop("open", None)
    peak = float(shadow.pop("peak_kw", 0.0) or 0.0)
    # Popped rather than re-derived from start/end below — see advance_charge,
    # where it accumulates across every snapshot the session actually saw.
    fast = bool(shadow.pop("fast", False))
    if not start:
        return None

    def delta(key: str) -> float | None:
        a, b = start.get(key), end.get(key)
        if a is None or b is None:
            return None
        return round(float(b) - float(a), 3)

    # The wall meter, the pack meter, and the pack's own level. Named for
    # where each is measured rather than for what the field is called: the
    # field names say AC and DC, but both counters ran on an AC charge, so
    # the names describe the side of the converter and not the socket.
    kwh_wall = delta("charge_energy_in_raw")
    kwh_pack_meter = delta("dc_energy_in_raw")
    kwh_pack_level = delta("energy_kwh")
    # The lifetime counter's movement across this session. Same quantity as
    # one of the two above — which one is exactly the open question — but
    # arrived at without depending on a per-session counter having been
    # watched from zero.
    kwh_lifetime = delta("lifetime_charged_raw")
    minutes = max((float(end["ts"]) - float(start["ts"])) / 60.0, 0.0)
    biggest = max((v for v in (kwh_wall, kwh_pack_meter, kwh_pack_level,
                               kwh_lifetime) if v is not None), default=0.0)
    if biggest < CHARGE_MIN_KWH or minutes <= 0:
        return None

    return {
        "start_ts": float(start["ts"]),
        "end_ts": float(end["ts"]),
        "start_time": _dt(start["ts"]).isoformat(timespec="seconds"),
        "end_time": _dt(end["ts"]).isoformat(timespec="seconds"),
        "duration_min": round(minutes, 1),
        "kwh_wall": kwh_wall,
        "kwh_pack_meter": kwh_pack_meter,
        "kwh_pack_level": kwh_pack_level,
        "kwh_lifetime": kwh_lifetime,
        # The counters as they finished, not only how far they moved. A
        # session recorded from partway through — the machine was deployed
        # mid-charge the first time it ever ran — has a delta that measures
        # the part it saw, while the counter itself has been climbing since
        # the plug went in. Comparing what the charging network billed for
        # against a partial delta proves nothing; comparing it against the
        # final reading proves whether this is the same meter.
        "wall_meter_end": end.get("charge_energy_in_raw"),
        "pack_meter_end": end.get("dc_energy_in_raw"),
        # Kept for the same reason as the two above, and for one more: this
        # one is a lifetime total, so two sessions' readings of it are
        # directly comparable and the difference between them is every kWh
        # that went into the pack in between — including any charge the app
        # missed entirely, which is the check that says whether it missed one.
        "lifetime_meter_end": end.get("lifetime_charged_raw"),
        # Two ratios, because the first session measured properly showed they
        # are not the same question.
        #
        # meters_agree_pct is the two counters against each other. Read over
        # three minutes they looked 5% apart and that was called the onboard
        # charger's efficiency; read over eighteen it is 99.2%, and the
        # earlier figure was the 0.02 kWh step on DCChargingEnergyIn being
        # mistaken for a signal. Two counters that agree to within a percent
        # are not a converter and its output — they are two views of the same
        # side of it.
        #
        # pack_vs_wall_pct is where the loss actually shows: the meters said
        # 2.237 kWh while the pack's own level rose 2.0, about 89%. The
        # charging network billed 19.799 kWh for the session those minutes
        # belong to and the car called it 18 kWh added — 90.9%. Those are the
        # same number, which is what says the pack level is the figure the
        # car reports as "added".
        "meters_agree_pct": round(kwh_pack_meter / kwh_wall * 100.0, 1)
        if kwh_wall and kwh_pack_meter is not None and kwh_wall > 0 else None,
        "pack_vs_wall_pct": round(kwh_pack_level / kwh_wall * 100.0, 1)
        if kwh_wall and kwh_pack_level is not None and kwh_wall > 0 else None,
        "soc_start": start.get("soc"),
        "soc_end": end.get("soc"),
        "peak_kw": round(peak, 1),
        "fast": fast,
        "lat": start.get("lat"), "lon": start.get("lon"),
        "pack_temp_c": end.get("pack_temp_c"),
        # Nothing writes a Charge row from this. It is evidence.
        "state_raw": end.get("charge_state_raw"),
    }


def recover_sleep_gap(prev: dict[str, Any], nxt: dict[str, Any],
                      rested_odo_km: float | None = None,
                      via: str = "unknown") -> bool:
    """Give a trip back the metres it drove after its last transmission.

    The mechanism, measured rather than assumed. A car out of coverage
    buffers what it cannot send and replays it in order when the signal
    returns — an entire 35-minute drive arrived 36 minutes late this way and
    reconstructed to within 0.4% of the car's own figure. The buffer does not
    survive the car going to sleep. Park underground, lose signal, sleep
    before it comes back, and the last hundred metres are gone for good:
    there is no BMSState leaving Drive anywhere in the log for that arrival,
    because the car never got to send one.

    What is not gone is the odometer. It counts up and it does not reset, so
    the next trip's opening reading measures the same ground the lost one
    covered. Trip 535 ended at 31,161.861 and the next began at 31,162.199 —
    338 metres the car definitely drove and this app definitely did not see.
    Against the car's own 4.1 km that turns -7.4% into +0.8%.

    Two bounds keep it honest. The previous trip must have ended
    ``stream_lost``, which is the only ending that means the arrival was
    never confirmed; and the gap must be under ``SHADOW_TAIL_MAX_KM``. The
    cap is not a taste: an arrival roll is a few hundred metres, while a
    whole journey driven offline is kilometres, and gluing one of those onto
    the previous trip would be a worse error than losing it. Over the cap
    this refuses, and the distance stays in unaccounted_km where it can be
    seen.

    The recovered energy is inferred, and says so. EnergyRemaining at the
    next trip's start includes a night of standby drain, so it cannot be
    used as this trip's closing reading; the assumption instead is that the
    unseen metres were driven at the same Wh/km as the seen ones. That keeps
    distance, energy and Wh/km consistent with each other, which leaving it
    out would not.

    ``via`` is recorded on the trip, not used for anything here — it exists
    because there are three call sites (the moment the next trip opens, the
    moment it closes, and the manual backfill) and, without a record of
    which one actually fired, that was previously a guess from wall-clock
    behaviour rather than a fact. "open" needs the next trip's very first
    telemetry record to already carry an odometer value, which is not
    guaranteed — Fleet Telemetry only streams a field when it is due — so
    a miss there silently falls through to "close" and is otherwise
    invisible.
    """
    if not prev or not nxt or prev.get("ended_on") != "stream_lost":
        return False
    end_odo, next_start = prev.get("end_odo_km"), nxt.get("start_odo_km")
    start_odo = prev.get("start_odo_km")
    if end_odo is None or next_start is None or start_odo is None:
        return False
    # Where the car was actually SEEN resting, when anything saw it, bounds
    # this. The next trip's starting odometer is not evidence of where the
    # previous one stopped — it is evidence of where the next one was first
    # noticed, and those differ by however much ground the departure covered
    # before its first record arrived. Crediting that to the arriving trip
    # moves one trip's distance onto another.
    #
    # Measured, 13 September: trip 740 closed at 31405.714, and a continuity
    # scan ten minutes later confirmed the car resting at exactly that
    # odometer. Three hours later trip 741 began at 31405.922. A parked car's
    # odometer cannot creep, so those 208 metres were driven at 741's
    # departure — and this function gave every one of them to 740, taking it
    # from 0.55% short of the car's own figure to 1.2% long.
    #
    # Across the judged history the pattern holds: trips whose arrival was
    # reconstructed read +0.80% against the car and 7 of 8 long, while trips
    # left alone read -0.46% and 2 of 14 long. That is 1.26 points of
    # difference at t = 4.5, and it all points one way.
    #
    # So a reading takes precedence over the next trip's anchor, and no
    # reading at all leaves the old behaviour intact — without evidence there
    # is nothing better to do than assume the gap was the arrival, which is
    # still likelier than losing it.
    boundary = float(next_start)
    if rested_odo_km is not None:
        boundary = min(boundary, float(rested_odo_km))
    gain = round(boundary - float(end_odo), 3)
    if gain <= 0.0 or gain > SHADOW_TAIL_MAX_KM:
        return False

    whkm = prev.get("wh_per_km")
    distance = round(boundary - float(start_odo), 3)
    # Recomputed from the bracket, never adjusted by a delta — so if this
    # ever runs twice on the same pair the second call finds no gap and
    # refuses, rather than counting the same metres again.
    prev["end_odo_km"] = round(boundary, 3)
    prev["distance_km"] = distance
    prev["recovered_km"] = round(
        float(prev.get("recovered_km") or 0.0) + gain, 3)
    if whkm and prev.get("energy_kwh") is not None:
        gained_kwh = round(gain * float(whkm) / 1000.0, 3)
        prev["energy_kwh"] = round(float(prev["energy_kwh"]) + gained_kwh, 3)
        prev["recovered_kwh"] = round(
            float(prev.get("recovered_kwh") or 0.0) + gained_kwh, 3)
        prev["wh_per_km"] = round(
            float(prev["energy_kwh"]) * 1000.0 / distance, 1) if distance > 0 else None
    # Time is not touched, and cannot be. The odometer is cumulative so the
    # distance comes back; nothing was listening while the clock ran, so the
    # duration stays a lower bound.
    #
    # Which is why the average speed is not recomputed either, though it was
    # at first. Dividing the recovered distance by the unrecovered duration
    # is the one combination that is wrong on both counts: 3.795 km over 17.2
    # minutes is 13.2 km/h and both halves were measured over the same
    # window, while 4.133 km over the same 17.2 minutes reads 14.4 and the
    # car was slower than that, not faster. The figure measured before the
    # correction is the better estimate of the real average, because its
    # numerator and denominator are short by the same missing minutes.
    prev["recovered_via"] = via
    prev["recovered_at"] = now_local().isoformat(timespec="seconds")
    return True


def _bands_out(bands: dict[str, Any] | None) -> dict[str, dict[str, float]] | None:
    """The banded accumulator, rounded for storage. None when nothing banded.

    Rounded here rather than at every read: this is written once and read on
    every dashboard load, and three decimals of a kilometre is a metre.
    """
    if not bands:
        return None
    out = {}
    for key, slot in bands.items():
        km, sec, kwh = float(slot[0]), float(slot[1]), float(slot[2])
        if km <= 0 and sec <= 0:
            continue
        out[key] = {"km": round(km, 3), "min": round(sec / 60.0, 2),
                    "kwh": round(kwh, 4)}
    return out or None


def _shadow_close(shadow: dict[str, Any], end: dict[str, Any],
                  readings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Emit the open trip, ending at ``end``, and clear the machine.

    ``readings`` supplies the closing odometer and energy when they are known
    to be better than ``end``'s — a stationary car keeps reporting, so a
    reading taken after it parked measures the same moment more accurately
    than one taken at the instant it stopped. Time still comes from ``end``.
    """
    final = readings or end
    start = shadow.pop("open", None)
    max_speed = float(shadow.pop("max_speed_kmh", 0.0) or 0.0)
    sample_sec = _energy_gap_sec(shadow.pop("e_gaps", None))
    # Read before clearing: this is reported on the trip, and popping it
    # first would make every trip claim it ended on a timeout.
    exit_seen = bool(shadow.get("exit_seen"))
    ended_by_bms = bool(shadow.pop("ended_by_bms", False))
    ended_by_charge = bool(shadow.pop("ended_by_charge", False))
    idle_sec = float(shadow.pop("idle_sec", 0.0) or 0.0)
    climate_sec = float(shadow.pop("climate_sec", 0.0) or 0.0)
    temp_sec = float(shadow.pop("temp_sec", 0.0) or 0.0)
    temp_sum = float(shadow.pop("temp_sum", 0.0) or 0.0)
    bands = shadow.pop("bands", None)
    # The anchor goes with the histogram it feeds, for the same reason e_ts
    # does: left behind, the next trip's first step would be measured from
    # this one's last odometer reading, across the park in between.
    shadow.pop("band_odo", None)
    shadow.pop("band_ts", None)
    shadow.pop("band_e", None)
    stops = int(shadow.pop("stops", 0) or 0)
    # Whatever is still held is the arrival: nothing moved after it. Dropped
    # rather than counted, exactly as the trailing idle run above is.
    shadow.pop("temp_hold_sec", None)
    shadow.pop("temp_hold_sum", None)
    # An idle run still open at the close is the arrival itself — the trip
    # ends at the moment the car stopped, so that stretch comes after it, not
    # during it. Dropped rather than counted.
    shadow.pop("idle_run_since", None)
    shadow["still_since"] = None
    shadow.pop("still_snap", None)
    shadow.pop("exit_seen", None)
    shadow.pop("bms_seen_drive", None)
    shadow.pop("seen_unplugged", None)
    if not start:
        return None

    start_odo = float(start.get("odo_km") or 0.0)
    distance = round(float(final.get("odo_km") or 0.0) - start_odo, 3)
    minutes = max((float(end["ts"]) - float(start["ts"])) / 60.0, 0.0)
    if distance < TRIP_MIN_KM or minutes <= 0:
        return None
    # Both ends have to be real odometer readings, and the gap between them
    # has to be a journey. A trip measured from a missing reading is not a
    # long trip, it is an arithmetic accident, and it enters the record
    # looking exactly like data.
    if start_odo <= 0.0 or distance > SHADOW_MAX_KM:
        return None

    # Both ends measured, so this is a subtraction rather than a percentage
    # multiplied by a capacity nobody has pinned down.
    #
    # From `end`, not from `final`. The two are the same moment for a trip
    # that closed on its last record, and minutes apart for one that closed
    # on a settle window — and the odometer and the energy want opposite
    # things from that gap. A car that has stopped may still roll a few
    # metres, so a later odometer measures the arrival better. A car that has
    # stopped is still drawing: screen, climate, the car staying awake. Three
    # minutes of that is 0.05 kWh or so, spent after the journey ended, and
    # this was charging it to the journey.
    #
    # Which made the trip internally contradictory: its duration ended when
    # the car stopped and its energy went on accruing for another three
    # minutes. Every trip's energy was overstated, always in the same
    # direction, and the accuracy report showed exactly that — seven trips
    # judged against the car and all seven positive.
    e0, e1 = start.get("energy_kwh"), end.get("energy_kwh")
    energy = round(e0 - e1, 3) if e0 is not None and e1 is not None else None

    # The drive counter is the better measure of the two: monotonic, so a
    # lost record costs nothing, and it counts only traction — where
    # EnergyRemaining also falls for climate and standby. Its units are
    # undocumented, so both are carried and neither is trusted over the other
    # yet. One real journey settles the ratio; until then this is evidence,
    # not a figure.
    u0, u1 = start.get("energy_used_raw"), final.get("energy_used_raw")
    used_delta = round(u1 - u0, 4) if u0 is not None and u1 is not None else None
    d0, d1 = start.get("energy_drive_raw"), final.get("energy_drive_raw")
    drive_delta = round(d1 - d0, 4) if d0 is not None and d1 is not None else None
    r0, r1 = start.get("energy_regen_raw"), final.get("energy_regen_raw")
    regen_delta = round(r1 - r0, 4) if r0 is not None and r1 is not None else None

    return {
        "start_ts": float(start["ts"]),
        "end_ts": float(end["ts"]),
        "start_time": _dt(start["ts"]).isoformat(timespec="seconds"),
        "end_time": _dt(end["ts"]).isoformat(timespec="seconds"),
        "distance_km": distance,
        "duration_min": round(minutes, 1),
        "energy_kwh": energy,
        "wh_per_km": round(energy * 1000.0 / distance, 1)
        if energy and distance > 0 else None,
        # No energy_unc here on purpose. It is derived from this trip's own
        # energy and duration, both of which are recorded, so storing it
        # would freeze a formula rather than a measurement — and that formula
        # has already moved once, from EnergyRemaining's 0.02 kWh step to the
        # 60-second sampling interval that turned out to be three times
        # larger. Trips closed either side of that carried different answers
        # to the same question and the accuracy report added them together.
        # See _energy_unc_kwh in the API layer, which works it out on read.
        #
        # What IS stored is the third input, because unlike the other two it
        # cannot be recovered later: how often this trip's own readings
        # actually arrived. That is a measurement of the conditions the trip
        # was recorded under — the same kind of thing as its odometer
        # bracket, not an answer derived from them — and nothing on a closed
        # trip can reconstruct it once the records have rolled out of the
        # buffer. None means too few readings to say, and the reader falls
        # back to the interval this car streamed at before any of this was
        # measured.
        "energy_sample_sec": sample_sec,
        # Measured, not estimated from average speed — which is what lets a
        # streamed trip say its driving-only figure is real rather than
        # wearing the dashboard's "estimated" badge for want of a stop
        # nobody had recorded.
        "idle_min": round(idle_sec / 60.0, 1),
        "idle_tracked": True,
        # Where the trip's kilometres, minutes and kWh actually went, banded by
        # the speed they happened at, and how many times the car came to rest.
        # See the accumulation in advance_shadow: a trip carrying two speed
        # numbers can be put in one box, and cannot be described as partly one
        # thing and partly another.
        "speed_bands": _bands_out(bands),
        "stops": stops,
        "climate_min": round(climate_sec / 60.0, 1),
        "soc_start": start.get("soc"),
        "soc_end": end.get("soc"),
        "max_speed_kmh": round(max_speed, 1),
        "avg_speed_kmh": round(distance / (minutes / 60.0), 1),
        "start_lat": start.get("lat"), "start_lon": start.get("lon"),
        "end_lat": end.get("lat"), "end_lon": end.get("lon"),
        # Evidence, not yet figures. A counter's delta over energy_kwh is the
        # ratio that says whether it counts kWh, Wh, or something else — and
        # how much of the pack's fall was traction rather than climate.
        # The raw bracket, so a distance argument can be settled rather than
        # inferred. Both are read while the car is stationary, so their
        # difference IS the distance travelled — if that still disagrees with
        # the car's own trip meter, the disagreement is between the vehicle's
        # two measures and not something this app can fix by moving a
        # boundary.
        "start_odo_km": round(float(start.get("odo_km") or 0.0), 3),
        "end_odo_km": round(float(final.get("odo_km") or 0.0), 3),
        # The energy bracket, kept rather than only its difference: a reading
        # that arrives after the trip closed can then be folded in by
        # subtraction, instead of the trip having to remember how it got here.
        "start_energy_kwh": e0,
        "end_energy_kwh": e1,
        "drive_delta": drive_delta,
        "regen_delta": regen_delta,
        # The reason for measuring this twice: EnergyRemaining moves in steps
        # of 0.02 kWh, which is the floor under every short-trip figure here.
        # A lifetime counter only counts up and has no such step, so the gap
        # between the two says how much of a disagreement is quantisation.
        "used_delta": used_delta,
        # How the journey's end was decided, because that is what says how
        # much to trust its final odometer:
        #   charging    the car was drawing power, so it had arrived. The
        #               only ending here that cannot be wrong
        #   bms         the car's battery management system left Drive. Its
        #               own answer, within seconds of the truth, and it does
        #               not care whether the driver stayed in the seat
        #   exit        the driver was seen to leave, and readings kept
        #               arriving afterwards — the arrival is measured
        #   timeout     the car sat still long enough while still reporting
        #   stream_lost the car went silent mid-journey and never said it had
        #               parked. The end is the last thing it managed to send,
        #               so the arrival is short by whatever it drove after
        #               that — typically a hundred metres or two here, and
        #               systematic rather than random, because it is the same
        #               carpark every time.
        "ended_on": ("stream_lost" if shadow.pop("stream_lost", False)
                     else "charging" if ended_by_charge
                     else "bms" if ended_by_bms
                     else "exit" if exit_seen else "timeout"),
        "pack_temp_c": end.get("pack_temp_c"),
        "inside_temp": end.get("inside_temp"),
        # The mean across the drive where the stream gave enough of it, and
        # the closing reading otherwise. Both are the car's own sensor; the
        # difference is one moment against the journey.
        "out_temp": (round(temp_sum / temp_sec, 1) if temp_sec > 0
                     else end.get("out_temp")),
        # Kept beside it, because the two answer different questions: the
        # trip's weather as it finished is what a reader recognises, and the
        # mean is what the climate model should integrate.
        "out_temp_end": end.get("out_temp"),
    }
