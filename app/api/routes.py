"""REST API endpoints."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import alerts, auth, notifications, pricing_prefs, services, state, tariff, vin as vin_mod
# sync imports nothing from the app, so this is safe at module level (several
# functions below still import it locally; those bindings are just local names
# for the same module). Needed up here for now_local/to_epoch, which anything
# comparing against a stored timestamp has to go through — see sync.now_local.
from .. import sync as sync_mod
from ..analysis import haversine_km, percentile
from ..analysis import narrative as narrative_engine
from ..analysis import battery as battery_analysis
from ..analysis import charging as charging_analysis
from ..analysis import driving as driving_analysis
from ..analysis import efficiency as efficiency_analysis
from ..analysis import recommendations as recommendations_engine
from ..analysis import service as service_analysis
from ..config import get_settings
from ..database import get_session
from ..importer import ImportError_, parse_upload
from ..models import (ArrivalTailSample, BatteryReading, Charge, Drive, Place,
                      SecurityEvent, ServiceRecord, Vehicle)
from ..schemas import ChargeOut, DriveOut, VehicleOut

router = APIRouter(prefix="/api", tags=["analytics"])

# How long after an unexpected wake (phone-as-key, precondition, remote start —
# not our own manual wake_up) the sync cron should treat the car as "worth
# polling tightly": long enough to catch a likely departure, short enough that
# an online-but-idle car isn't kept awake past this on our account.
FAST_POLL_WINDOW_MIN = 3.0
# How long to keep the tight polling cadence after a car with an open trip
# first reads stopped. Covers the final creep into a parking space so the
# trip's stop is anchored where the car actually came to rest, without
# polling hard through the whole PARK_END_MIN wait before the trip closes.
ARRIVAL_SETTLE_MIN = 4.0


def _trim_rate_kw(past_drives: list, past_charges: list,
                  capacity_kwh: float) -> float | None:
    """The standby rate to charge a trimmed tail at, in kW.

    Delegates so that this and vampire_drain's own add-back share one
    definition — see driving.parked_rate_kw. Energy taken off a trip here has
    to be the energy put back on the gap there, and two rates would leak at
    the boundary.
    """
    return driving_analysis.parked_rate_kw(past_drives, past_charges, capacity_kwh)

# A vehicle_data() read is itself an activity signal to the car — it resets
# Tesla's own inactivity countdown, delaying sleep, regardless of how the
# request got triggered. /api/sync may now be called every minute or so (an
# external cron, an uptime monitor), far more often than a car naturally needs
# reading. Outside an active trip or a just-woke escalation window, don't read
# more often than settings.sync_poll_interval_min (config.py) — hitting
# /api/sync more often than that does NOT force more frequent reads, the
# endpoint decides for itself.

# If /api/sync hasn't written a last_status update in this long, something's
# wrong upstream of the app itself — the external cron has stopped firing, or
# a request is failing before it even gets far enough to record a status
# (e.g. a database write failure). A healthy 1-minute cron refreshes this
# every tick regardless of whether the car itself is reachable, so a gap
# this large is a real signal, not normal jitter.
CRON_STALE_MIN = 10.0

# How long "offline" (as opposed to a clean "asleep") must be sustained before
# an open trip is closed on it. Some accounts/cars report a genuinely-sleeping
# car as "offline" rather than "asleep", so treating only "asleep" as
# definitive left those cars' trips open indefinitely. This threshold is long
# enough that a momentary signal gap mid-drive (a tunnel, a dead zone) will
# already have recovered, short enough that a real stop still closes promptly.
UNREACHABLE_CLOSE_MIN = 3.0


# The tick log is run-length encoded — a run is an unbroken stretch of ticks
# that all did the same thing, so an overnight of "found it asleep" is one
# entry rather than five hundred.
#
# Bounded by SERIALISED SIZE rather than run count. Capping the count instead
# let 120 runs of ~75 characters build a 9 KB value against what was then a
# VARCHAR(2048) column; every write failed, and since this is written on every
# sync path it took /api/sync down for thirteen hours — the instrument added
# to explain a gap in polling causing a much larger one.
#
# Setting.value is TEXT now, so this is no longer the database's limit but a
# deliberate one: how much history is worth carrying. The first value chosen
# after that outage, 1800, bought about 37 MINUTES — "read" and "idle"
# alternate every tick while the car is awake and never coalesce, so active
# polling burns runs fast. That is useless for diagnosing anything reported
# later the same day, which is the normal case. A quiet car compresses to
# almost nothing, so this is generous only in the window where it has to be.
SYNC_LOG_MAX_CHARS = 40000

# A hole between two runs longer than this means no tick ran at all — the app
# never got the request. That is a different fault from every outcome the log
# records (all of which mean the tick DID run), and the only one the log finds
# by absence rather than by writing something. Same threshold the dashboard
# already calls a stale status.
SYNC_SILENCE_MIN = CRON_STALE_MIN


def _log_tick(session: Session, outcome: str, detail: str | None = None) -> None:
    """Record what this /api/sync tick actually did.

    LAST_STATUS_KEY is overwritten every tick, so after the fact there is no
    way to tell a cron that stopped firing from a car that was quietly asleep
    — both leave the same single row. Measured, trip 368: 12.4 hours with no
    reading at all, containing two real drives, and nothing anywhere to say
    whether the loop was skipping, sleeping, or simply not running.

    Run-length encoded because the interesting thing is never one tick, it is
    a stretch of them: {o: outcome, n: ticks, a: first ts, b: last ts}. One
    row per tick either way, the same write LAST_STATUS_KEY already costs.

    A GAP BETWEEN RUNS is the fourth outcome and the one no tick can write:
    it means the request never arrived.
    """
    import json as _json

    # Whole seconds. Sub-second precision says nothing about a poll loop and
    # costs 15 characters a run against a budget measured in hundreds.
    now = int(time.time())
    try:
        runs = _json.loads(state.get(session, state.SYNC_LOG_KEY) or "[]")
    except ValueError:
        runs = []
    entry = {"o": outcome, "n": 1, "a": now, "b": now}
    if detail:
        # Kept on the run, not just counted: "error" alone says a tick failed,
        # which is barely more useful than the silence it replaces. The reason
        # is the whole point, and it has to be readable from a phone without
        # host logs.
        # Short enough that one run can never on its own exceed the budget
        # below, however long the exception text.
        entry["e"] = detail[:200]
    if runs and runs[-1].get("o") == outcome and runs[-1].get("e") == entry.get("e"):
        runs[-1]["n"] = (runs[-1].get("n") or 0) + 1
        runs[-1]["b"] = now
    else:
        runs.append(entry)
    # Drop the oldest until it fits. The newest runs are the ones a diagnosis
    # needs, and losing the far end of the history is a cost worth paying to
    # keep this from ever failing a write again.
    payload = _json.dumps(runs)
    while len(payload) > SYNC_LOG_MAX_CHARS and len(runs) > 1:
        del runs[0]
        payload = _json.dumps(runs)
    try:
        state.put(session, state.SYNC_LOG_KEY, payload)
    except Exception:
        # Observability must not be able to break the thing it observes —
        # that is the entire lesson of this bug. Start the history over rather
        # than propagate: losing it costs a diagnosis, raising costs the sync.
        try:
            session.rollback()
            state.put(session, state.SYNC_LOG_KEY, _json.dumps([entry]))
        except Exception:
            pass


# How long the sleep back-off may keep suppressing work after the last tick
# that actually ran to completion. Comfortably more than one suspend window
# (settings.sleep_recheck_min, 10 min) so ordinary cycling is untouched, and
# short enough that a crash loop costs one hour of polling rather than a day.
SUSPEND_MAX_QUIET_MIN = 60.0


def _mark_full_tick(session: Session, now_ts: float) -> None:
    """Record that a tick reached the end without raising.

    Written as late as possible on purpose: it is the evidence the sleep
    back-off is checked against (see SUSPEND_MAX_QUIET_MIN), and a marker set
    early would certify ticks that went on to crash — which is exactly the
    condition it exists to break out of.
    """
    state.put(session, state.FULL_TICK_KEY, str(now_ts))


def _log_tick_isolated(detail: str, session: Session | None = None) -> None:
    """Record a failed tick, as robustly as this can be done.

    Tries the request's own session first, rolled back so a poisoned
    transaction can't block the write. A brand new connection sounds safer and
    is not: on a connection-capped host (Neon's free tier) opening a second
    one mid-request is exactly when it may be refused, and this failing
    silently is how a real 500 left no trace at all. So the extra connection
    is the FALLBACK, for when the request had no usable session.

    Swallows its own failures either way: there is nothing useful left to do
    if neither can write, and raising here would replace the real error with a
    less interesting one.
    """
    if session is not None:
        try:
            session.rollback()
            _log_tick(session, "error", detail=detail)
            return
        except Exception:
            pass
    try:
        from ..database import SessionLocal

        with SessionLocal() as log_session:
            _log_tick(log_session, "error", detail=detail)
            log_session.commit()
    except Exception:
        pass


def _save_last_status(session: Session, vin: str, **fields) -> None:
    """Persist the cron's own last determination of what the car was doing.

    Written on every /api/sync tick (including "found it asleep") so the
    dashboard can show a near-live status straight from the database on page
    load — mirroring what a push-based telemetry feed would give you, but
    built from polling: the cron is the thing pinging Tesla, writing the
    result to Neon every time, and the dashboard only ever reads that back.
    """
    import json as _json

    state.put(session, state.scoped(state.LAST_STATUS_KEY, vin), _json.dumps(fields))


def _first_vehicle(session: Session) -> Vehicle:
    # The car the dashboard follows (the active pick, else the linked car) takes
    # precedence over demo/imported rows.
    active_vin = state.active_vin(session)
    if active_vin:
        vehicle = session.scalars(
            select(Vehicle).where(Vehicle.vin == active_vin)
        ).first()
        if vehicle is not None:
            return vehicle
    vehicle = session.scalars(select(Vehicle).order_by(Vehicle.id)).first()
    if vehicle is None:
        raise HTTPException(404, "No vehicle data. Run the collector or seed demo data.")
    return vehicle


def _attach_curve_capacity(c: dict) -> None:
    """Turn a closed session's charging curve into the capacity it implies, and
    drop the curve itself (transient, not a column). Measured per session so
    the running median has real measurements to work from rather than
    re-deriving everything from endpoints on every read."""
    fit = battery_analysis.capacity_from_curve(
        c.pop("curve", None) or [], c.get("charge_type", "AC"))
    c["implied_capacity_kwh"] = fit["kwh"] if fit else None
    c["capacity_samples"] = fit["samples"] if fit else None


# How many trailing readings the capacity constant's degradation figure is a
# median of. Far wider than the Battery Health card's 12, because the two want
# opposite things: the card should follow the pack, while this number scales
# every kWh and Wh/km the app reports, so it moving is indistinguishable from
# the car's energy use changing.
#
# At 12 it moved 69.7 -> 69.4 -> 69.9 -> 69.4 inside two days (recorded in the
# audit's own diagnostics), which is 0.7% of noise on every energy figure —
# two identical drives on consecutive days differing for no physical reason.
# A pack degrades on the order of 0.1% a month, so a window of weeks costs
# nothing in responsiveness and cuts that scatter by about five times.
CAPACITY_RECENT_N = 300


def _retract_estimated_tail(drive, km: float, sec: float) -> None:
    """Take back `km` (and its `sec`) of a trip's estimated arrival tail.

    Used wherever a later reading knows better than the estimate did: a poll
    that can finally measure the ground, or simply an odometer that moved less
    than the estimate claimed. Distance, energy and the clock all came from one
    assumption, so they all go back together — unwinding only the distance
    would leave a trip whose Wh/km and average speed silently changed.
    """
    if km <= 0 or drive.distance_km <= km:
        return
    new_dist = round(drive.distance_km - km, 1)
    if drive.energy_used_kwh and drive.distance_km > 0:
        drive.energy_used_kwh = round(
            drive.energy_used_kwh * new_dist / drive.distance_km, 2)
    drive.distance_km = new_dist
    if drive.end_odo_km is not None:
        drive.end_odo_km = round(drive.end_odo_km - km, 3)
    drive.end_est_km = round((drive.end_est_km or 0.0) - km, 3) or None
    if sec > 0 and drive.duration_min:
        drive.end_time = drive.end_time - timedelta(seconds=sec)
        drive.duration_min = round(max(drive.duration_min - sec / 60.0, 0.0), 1)
    if drive.duration_min and drive.duration_min > 0:
        drive.avg_speed_kmh = round(new_dist / (drive.duration_min / 60.0), 1)


# Measured arrivals needed at a place before its median is trusted to estimate.
# One is a reading, not a habit — and the whole point of this replacement is
# that it uses what a car park has actually shown rather than what a model
# supposes. Two is the smallest set a median can disagree with itself over.
PLACE_TAIL_MIN_SAMPLES = 2


def _place_tail_km(session: Session, place: str) -> float | None:
    """The median arrival tail this place has actually shown, or None.

    The tail is a property of the car park — a ramp and a slot, or a surface
    bay with signal to the door — which is why this is keyed on place and not
    on anything about the drive (see sync.arrival_tail_for_place). None until
    a place has enough measurements, and None is a real answer: no estimate at
    all beat the speed model it replaced.
    """
    if not place:
        return None
    vals = sorted(
        v for v in (_sample_place(session, s) == place and s.measured_km or None
                    for s in session.scalars(select(ArrivalTailSample)).all())
        if v is not None
    )
    if len(vals) < PLACE_TAIL_MIN_SAMPLES:
        return None
    return round(percentile(vals, 0.5), 3)


def _sample_place(session: Session, sample) -> str:
    """A sample's place, falling back to its drive's arrival for older rows.

    Samples written before place existed carry an empty one. The drive still
    knows where it ended, so the measurement is not lost — only unlabelled, and
    a read-time fallback recovers it without pretending the column was always
    there. New rows store it directly, which is what keeps a measurement alive
    after its trip is deleted.
    """
    if sample.place:
        return sample.place
    drive = session.get(Drive, sample.drive_id) if sample.drive_id else None
    return (drive.end_location if drive else "") or ""


def _record_tail_sample(session: Session, drive: Drive, est_km: float,
                        measured_km: float, **extra) -> None:
    """Keep one (predicted, measured) arrival pair, tagged with its place.

    One per arrival, replacing rather than appending. An arrival is a single
    event and can be measured more than once — the automatic fold-in may see
    it, and a check against the car's own screen may confirm it again, and
    that check can be repeated. Appending would let one car park's median be
    voted on twice by the same afternoon, which is exactly the kind of quiet
    double-count the boundary work has spent this long removing.
    """
    prior = session.scalars(
        select(ArrivalTailSample).where(ArrivalTailSample.drive_id == drive.id)
    ).all() if drive.id else []
    for old in prior:
        session.delete(old)
    session.add(ArrivalTailSample(
        vehicle_id=drive.vehicle_id, drive_id=drive.id,
        ts=sync_mod.now_local(), place=drive.end_location or "",
        est_km=round(est_km, 3), measured_km=round(measured_km, 3), **extra))


def _unoverlap_previous(session: Session, vehicle_id: int, new_start) -> None:
    """Pull an ESTIMATED arrival back so it cannot end after the next trip begins.

    A sleep close moves the clock forward with the odometer, both from one
    assumption about how long the car went on arriving (see
    sync.arrival_tail_for_place). Nothing bounded that by reality: measured
    live, trip 339 was credited three minutes of arriving while the car was
    driving again within one, so it ended at 17:01 while trip 340 started at
    16:59 — two trips overlapping in time, which services.edit_drive already
    refuses to let a person create by hand.

    Only ever an estimated end (end_est_km set) and only ever backwards. A
    measured end is a reading, and if a reading really did land after the next
    trip's start then something is wrong that moving a timestamp would hide.
    """
    prev = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle_id)
        .order_by(Drive.start_time.desc()).limit(1)
    ).first()
    if prev is None or not prev.end_est_km or prev.end_time <= new_start:
        return
    if new_start <= prev.start_time:
        return  # not an overlap this can fix — the two trips are tangled
    prev.end_time = new_start
    prev.duration_min = round(
        (new_start - prev.start_time).total_seconds() / 60.0, 1)
    if prev.duration_min > 0:
        prev.avg_speed_kmh = round(
            prev.distance_km / (prev.duration_min / 60.0), 1)
    # The distance estimate is deliberately left alone. It was handed to this
    # trip and the next one already starts past it (see LAST_SLEEP_CLOSE_KEY),
    # so trimming it here would leave the ground belonging to neither — the
    # exact failure the hand-over exists to prevent. What was wrong was the
    # clock, and that is what this corrects.


def _newest_readings(session: Session, vehicle_id: int, columns: tuple,
                     limit: int = 2000) -> list:
    """The most recent ``limit`` battery readings, returned oldest-first.

    battery.analyze() documents its input as oldest-first and takes the tail
    as "now", so the ordering and the limit have to agree: ORDER BY ts ASC
    with a LIMIT selects the OLDEST rows, and once a car passes the limit the
    "current" estimate silently freezes at whatever the pack looked like back
    then — degradation, battery health and the capacity constant with it.
    Sorting descending to pick the newest and reversing is what the callers
    all meant.
    """
    rows = session.execute(
        select(*columns)
        .where(BatteryReading.vehicle_id == vehicle_id)
        .order_by(BatteryReading.ts.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def _degradation_pct(session: Session, vehicle: Vehicle, settings) -> float | None:
    """The car's own range-based degradation estimate (% capacity lost vs
    new) — the same figure the Battery Health card shows, computed purely
    from range-projection history, independent of any charge-derived kWh
    figure. None while there isn't yet enough reading history.

    Runs on every sync tick (via _usable_capacity), so it selects only the
    two columns the estimate needs rather than hydrating up to 2000 full
    ORM rows a minute — the width of what crosses the wire to a remote
    Postgres matters more here than in a once-per-page-load path.
    """
    rows = _newest_readings(
        session, vehicle.id, (BatteryReading.soc, BatteryReading.range_km))
    vin_info = vin_mod.decode(vehicle.vin)
    spec_km = settings.battery_new_range_km or battery_analysis.new_range_for(
        vehicle.model, vehicle.trim, year=vin_info.get("year"))
    health = battery_analysis.analyze(
        [{"soc": soc, "range_km": range_km} for soc, range_km in rows],
        new_range_km=spec_km, recent_n=CAPACITY_RECENT_N)
    return health["degradation_pct"] if health.get("available") else None


# Charges needed before the measured pack figure outranks spec-minus-degradation.
# One wide session is a reading; a median wants enough to disagree with itself,
# and the EMA the sync keeps is seeded at 75.0 and only converges after a dozen
# or so — a median over stored rows carries no seed at all, which is why this
# recomputes rather than reading vehicle.battery_capacity_kwh.
MEASURED_CAPACITY_MIN_CHARGES = 4
# How far the measured figure may sit from the variant spec before it is
# treated as a bad reading rather than a small pack. Degradation of more than
# a fifth would be a warranty case, not a calibration.
MEASURED_CAPACITY_MAX_DRIFT = 0.20


def _measured_capacity(session: Session, vehicle: Vehicle) -> tuple[float | None, int]:
    """This car's usable pack as its own charging sessions measure it.

    Same rules as sync.capacity_from_charge, applied to the stored rows rather
    than one live session: a gain wide enough that whole-percent SoC does not
    dominate, the AC efficiency correction because a charger reports what went
    in rather than what reached the pack, and a sane clamp so one bad row
    cannot move the answer.

    Median, not mean: a single mis-recorded session should not shift the figure
    every kWh and every ringgit is derived from.
    """
    charges = session.scalars(
        select(Charge).where(Charge.vehicle_id == vehicle.id)
    ).all()
    vals: list[float] = []
    for c in charges:
        gain = (c.end_soc or 0) - (c.start_soc or 0)
        if gain < 15 or not c.energy_added_kwh:
            continue
        cap = c.energy_added_kwh / (gain / 100.0)
        if (c.charge_type or "AC") != "DC":
            cap *= sync_mod.AC_CHARGE_EFFICIENCY
        if 45.0 <= cap <= 95.0:
            vals.append(cap)
    if len(vals) < MEASURED_CAPACITY_MIN_CHARGES:
        return None, len(vals)
    return round(percentile(sorted(vals), 0.5), 1), len(vals)


def _usable_capacity(session: Session, vehicle: Vehicle, settings) -> tuple[float, str]:
    """Usable pack capacity (kWh) for turning a drive's range/SoC delta into
    kWh, plus where it came from.

    Primary method: the factory spec for this exact variant (model/badge/
    wheel from the trim, generation from the VIN year) minus the car's own
    measured degradation (from range-projection history, the same figure the
    Battery Health card shows) — this ties the capacity used for every kWh/%
    calculation to the same degradation signal already displayed elsewhere,
    instead of two unrelated numbers that can silently disagree (e.g. a car
    showing 7% degradation implying, via a noisy charge-derived figure, a
    pack far smaller than spec-minus-7% would say).

    Falls back to the measured charge EMA when degradation data isn't
    available yet (a freshly-linked car with little reading history), then
    spec alone, then a generic default. An explicit config override always
    wins outright.
    """
    if settings.battery_capacity_kwh and settings.battery_capacity_kwh > 0:
        return settings.battery_capacity_kwh, "override"
    spec = battery_analysis.usable_capacity_for(
        vehicle.model, vehicle.trim, vin_mod.decode(vehicle.vin).get("year"))
    # The car's own charging sessions outrank the spec-derived figure, because
    # they measure THIS pack rather than infer it. Spec minus degradation was
    # preferred while the charge-derived number was thought to be the noisy
    # one; four readings off the car's own energy screen have since put the
    # spec path about 1% high every time, which is the wrong direction for a
    # figure every kWh and every ringgit is scaled by.
    #
    # Gated, not trusted blindly: enough sessions for a median to mean
    # something, and within MEASURED_CAPACITY_MAX_DRIFT of spec so a run of bad
    # rows cannot walk the constant away from physical sense. Below either bar
    # it falls through to exactly what it did before.
    measured, samples = _measured_capacity(session, vehicle)
    if measured and (not spec or abs(measured - spec) <= spec * MEASURED_CAPACITY_MAX_DRIFT):
        return measured, f"measured from {samples} charges"
    if spec:
        degradation = _degradation_pct(session, vehicle, settings)
        if degradation is not None:
            return round(spec * (1 - degradation / 100.0), 1), "spec - degradation"
    if vehicle.battery_capacity_kwh and vehicle.battery_capacity_kwh != 75.0:
        return vehicle.battery_capacity_kwh, "measured"
    return (spec or vehicle.battery_capacity_kwh or 75.0), ("variant spec" if spec else "default")


def _window(
    session: Session, vehicle_id: int, days: int,
    since: datetime | None = None, until: datetime | None = None,
):
    if since is None:
        # Stored start_time is naive MYT wall-clock, so the boundary has to be
        # too — datetime.now() would put it eight hours out on a UTC host and
        # slide every "last N days" window off by that much (see
        # sync.now_local).
        since = sync_mod.now_local() - timedelta(days=days)
    drive_q = select(Drive).where(Drive.vehicle_id == vehicle_id, Drive.start_time >= since)
    charge_q = select(Charge).where(Charge.vehicle_id == vehicle_id, Charge.start_time >= since)
    if until is not None:
        drive_q = drive_q.where(Drive.start_time < until)
        charge_q = charge_q.where(Charge.start_time < until)
    drives = session.scalars(drive_q.order_by(Drive.start_time)).all()
    charges = session.scalars(charge_q.order_by(Charge.start_time)).all()
    return list(drives), list(charges)


def _trip_cost_map(session: Session, vehicle_id: int) -> dict[int, dict]:
    """Every trip's cost, priced against the charge-layer history that
    actually supplied its energy (see driving_analysis.layered_trip_costs)
    rather than one flat "latest charge" rate applied to everything. Needs
    the vehicle's FULL history, not just whatever window is being displayed
    — an old trip's correct layer can depend on a charge from well before
    the window starts. A manual per-trip override (Drive.cost_override, set
    via /api/data/set-drive-cost for a trip the charge history can't reach)
    always wins over the computed figure."""
    drives_all = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle_id).order_by(Drive.start_time)
    ).all()
    charges_all = session.scalars(
        select(Charge).where(Charge.vehicle_id == vehicle_id).order_by(Charge.start_time)
    ).all()
    costs = driving_analysis.layered_trip_costs(drives_all, charges_all)
    for d in drives_all:
        if d.cost_override is not None:
            # A manual figure replaces the layer breakdown rather than sitting
            # beside it — the parts describe how the computed cost was reached,
            # and they no longer explain a number the user typed.
            costs[d.id] = {"cost": d.cost_override, "parts": []}
    return costs


def _idle_inducer(session: Session, vehicle_id: int, start_iso: str, end_iso: str) -> str | None:
    """What was likely running when the car parked into this idle gap, from
    BatteryReading rows logged before it fell asleep — the only part of the
    gap the app ever has visibility into (see BatteryReading.sentry_mode's
    docstring: sync deliberately stops polling once the car sleeps, so it
    can't know what a Sentry/climate toggle did hours into the gap).

    Only ever a positive detection ("Sentry Mode (maybe)"): a reading
    showing it off, or no reading at all for this gap, says nothing
    reliable about the rest of the (unobserved) gap, so it's never reported
    as a confirmed negative. Suffixed "(maybe)" rather than a flat "was on"
    for the same reason — sync polls periodically, not continuously, so a
    single reading can't tell a multi-hour run apart from a few-second
    automatic blip (e.g. a brief door-open waking the car into a quick
    cabin-check cycle); reported as a "was on" duration claim that's
    confirmed for the whole gap, it isn't. Kept short (this renders inline
    in a KPI card subtitle, where every character fights for one line)."""
    rows = session.scalars(
        select(BatteryReading).where(
            BatteryReading.vehicle_id == vehicle_id,
            BatteryReading.ts >= datetime.fromisoformat(start_iso),
            BatteryReading.ts <= datetime.fromisoformat(end_iso),
        )
    ).all()
    sentry = any(r.sentry_mode for r in rows)
    # cabin_overheat_protection ("Off"/"On"/"FanOnly") is the car's *setting*
    # for whether COP is allowed to run at all — most owners leave it "On"
    # permanently as a safety default, so checking that field alone would
    # flag COP as a drain cause on nearly every reading regardless of
    # whether it ever actually activated. cabin_overheat_protection_actively_
    # cooling is the live "is it really running right now" flag — that's the
    # one that means it actually drew power during this gap. When it's on,
    # skip the generic climate_on check entirely rather than reporting both,
    # since COP running is exactly what sets climate_on true in the first
    # place.
    cop = any(r.cabin_overheat_protection_actively_cooling for r in rows)
    climate = not cop and any(r.climate_on for r in rows)
    reasons = [r for r, hit in (
        ("Sentry Mode", sentry), ("cabin overheat protection", cop), ("climate", climate),
    ) if hit]
    if not reasons:
        return None
    # "(maybe)", not "was"/"were on" — see docstring: a single reading can't
    # confirm it ran for the gap's whole duration, only that it was observed
    # on at some point within it. Short and number-invariant (no was/were to
    # pick), since this renders inline in a KPI card subtitle already
    # crowded with the gap's own days/ended-at text.
    return f"{' & '.join(reasons)} (maybe)"


def _live_eta(session: Session, snap: dict, live: dict, capacity_kwh: float) -> dict | None:
    """Distance/time/projected-SoC to the nearest named place the car isn't
    already at, estimated from the live drive's own current position and pace.

    Deliberately not a routed ETA (no map/routing service involved) — just a
    straight-line distance at the drive's own average speed so far, which is
    honest about what it is: a rough "will I make it, and with how much
    battery" gut-check, not turn-by-turn navigation. Needs at least one named
    place (see Place/_geofence_name) to have anything to project toward.
    """
    lat, lon = snap.get("lat"), snap.get("lon")
    if lat is None or lon is None:
        return None
    cur_coords = f"{lat}, {lon}"
    best_place, best_km = None, None
    for p in session.query(Place).all():
        dist = haversine_km(cur_coords, f"{p.lat}, {p.lon}")
        if dist is None or dist <= p.radius_km:
            continue  # unknown, or already there
        if best_km is None or dist < best_km:
            best_place, best_km = p, dist
    if best_place is None:
        return None
    from .. import sync as sync_mod

    # A just-started drive has near-zero avg speed (tiny elapsed time) — fall
    # back to a typical city pace rather than projecting a near-infinite ETA.
    pace = live["avg_speed_kmh"] if live.get("avg_speed_kmh", 0) >= 5.0 else sync_mod.CITY_SPEED_KMH
    wh_per_km = live.get("driving_wh_per_km") or live.get("wh_per_km") or 0.0
    projected_soc = None
    if capacity_kwh and wh_per_km:
        used_kwh = best_km * wh_per_km / 1000.0
        projected_soc = round(max(live["soc"] - used_kwh / capacity_kwh * 100.0, 0.0), 1)
    return {
        "place": best_place.name,
        "distance_km": round(best_km, 1),
        "eta_min": round(best_km / pace * 60.0),
        "projected_soc": projected_soc,
    }


_PLACE_CACHE: dict[str, tuple[str, str]] = {}

# How close two coordinates must be to count as the same physical spot for
# naming. A parked car's reported position drifts a few metres between polls,
# so the coords a trip *arrives* at and the ones the next trip *departs* from
# are rarely byte-identical — and an exact-match-only cache then does a second
# lookup that can legitimately return a different nearby business, leaving one
# stop labelled two different ways across consecutive trips (reported:
# "McDonald's Drive Thru" on arrival, a neighbouring company's name on
# departure 13 min later). 60 m is wide enough to absorb that drift and a
# typical car park, narrow enough not to merge genuinely adjacent shopfronts.
_SAME_PLACE_M = 60.0

# Nominatim's usage policy caps anonymous use at ~1 request/second; bursting
# past it earns 429s (or a block), and every failed lookup here degrades a
# trip's name to raw coordinates. Normal sync only ever does a couple of
# lookups per trip close, but bulk relabeling can queue dozens back-to-back,
# so the Nominatim branch of _place_and_area self-paces to this floor.
# (Google's API has real quotas of its own and doesn't need this.)
_NOMINATIM_MIN_INTERVAL_SEC = 1.0
_last_nominatim_at = 0.0


def _label_from_google_geocode(data: dict) -> tuple[str, str]:
    """Turn a Google Geocoding API reverse payload into (label, area), in the
    same (specific spot, coarser area) shape _label_from_geocode returns for
    Nominatim, so callers don't need to know which provider answered.

    Google returns several results for the same point at different
    granularities, not reliably ordered specific-first — a POI/establishment
    result is searched for explicitly rather than trusting results[0].
    """
    results = data.get("results") or []
    poi = next(
        (r for r in results
         if "point_of_interest" in r.get("types", []) or "establishment" in r.get("types", [])),
        None,
    )
    best = poi or (results[0] if results else None)
    if not best:
        return "", ""
    comps = {t: c["long_name"] for c in best.get("address_components", []) for t in c.get("types", [])}
    route = (
        f"{comps['street_number']} {comps['route']}" if comps.get("route") and comps.get("street_number")
        else comps.get("route")
    )
    name = (
        comps.get("point_of_interest") or comps.get("premise") or route
        or comps.get("neighborhood") or comps.get("sublocality") or comps.get("locality") or ""
    )
    area = (
        comps.get("sublocality") or comps.get("locality")
        or comps.get("administrative_area_level_2") or comps.get("administrative_area_level_1") or ""
    )
    label = f"{name}, {area}" if name and area and name != area else (name or area)
    return label[:120], area[:120]


def _google_reverse_geocode(lat: str, lon: str, api_key: str) -> tuple[str, str] | None:
    """(label, area) via Google's Geocoding API, or None on any failure/miss
    — the caller falls back to Nominatim, so a bad key or an exhausted quota
    degrades gracefully instead of blocking trip naming."""
    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lon}", "key": api_key},
            timeout=4.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            return None
        label, area = _label_from_google_geocode(data)
        return (label, area) if label else None
    except Exception:  # noqa: BLE001 — never let naming block trip logging
        return None


def _label_from_geocode(data: dict) -> tuple[str, str]:
    """Turn a Nominatim reverse payload into (label, area).

    ``label`` prefers the most *specific* feature actually at the point — the
    named POI (mall, building, amenity) or the street, with house number when
    present — over the broader neighbourhood/suburb. That's what a person
    calls "the place", so it tracks the real position instead of naming an
    adjacent district; falls back down the granularity ladder so there's
    always a name. ``area`` is the coarser district/suburb it sits in, kept
    separately so callers that need a GPS-jitter-stable grouping key (e.g.
    "did I drive this same route before?") aren't stuck matching on the
    specific label, which can legitimately vary between visits to the same
    place (a different POI/building matched a few metres apart).
    """
    addr = data.get("address") or {}
    # The named feature Nominatim matched at this exact point (a POI/building),
    # then the specific address fields, in decreasing precision.
    poi = (
        data.get("name")
        or addr.get("amenity") or addr.get("shop") or addr.get("building")
        or addr.get("office") or addr.get("leisure") or addr.get("tourism")
    )
    road = addr.get("road")
    if road and addr.get("house_number"):
        road = f"{addr['house_number']} {road}"
    name = (
        poi or road or addr.get("neighbourhood") or addr.get("suburb")
        or addr.get("village") or addr.get("town") or addr.get("city") or ""
    )
    # The surrounding district/city, kept coarser than `name` so the two read
    # as "specific spot, general area" rather than repeating the same word.
    area = (
        addr.get("suburb") or addr.get("city_district") or addr.get("city")
        or addr.get("town") or addr.get("county") or ""
    )
    label = f"{name}, {area}" if name and area and name != area else (name or area)
    return label[:120], area[:120]


def _geofence_name(
    coords: str, session: Session | None, places: list[Place] | None = None,
) -> str | None:
    """Nearest user-defined Place (e.g. "Home", "Office") whose radius
    contains these coords, if any — checked before any network geocode so a
    user's own name for a place always wins over OSM's, and a well-known
    driveway/office never needs a lookup at all.

    ``places`` lets a bulk caller (auto-tag sweeping every trip) load the
    table once and reuse it, instead of one query per coordinate."""
    if not session or not coords or "," not in coords:
        return None
    best_name, best_km = None, None
    for p in (places if places is not None else session.query(Place).all()):
        d = haversine_km(coords, f"{p.lat}, {p.lon}")
        if d is not None and d <= p.radius_km and (best_km is None or d < best_km):
            best_name, best_km = p.name, d
    return best_name


def _place_departure_pace(session: Session, snap: dict | None) -> float | None:
    """The departure pace set on the named Place this snapshot sits in, if any
    — what sync.process_snapshot uses to back-date a start it never saw.

    Nearest containing geofence wins, matching _geofence_name, so overlapping
    places resolve the same way for the pace as for the name. A place with the
    field left at 0 is not a match: it means "no opinion", and returning it
    would shadow a smaller, further place that does have one.
    """
    coords = sync_mod._coords(snap) if snap else ""
    if not coords:
        return None
    best_pace, best_km = None, None
    for p in session.query(Place).all():
        if not p.departure_pace_kmh:
            continue
        d = haversine_km(coords, f"{p.lat}, {p.lon}")
        if d is not None and d <= p.radius_km and (best_km is None or d < best_km):
            best_pace, best_km = p.departure_pace_kmh, d
    return best_pace


def _cached_place_near(coords: str) -> tuple[str, str] | None:
    """An already-resolved label for a coordinate within _SAME_PLACE_M of
    ``coords``, or None. Picks the closest match rather than the first, so a
    dense cache can't attach a slightly-further neighbour's name."""
    best: tuple[str, str] | None = None
    best_km = _SAME_PLACE_M / 1000.0
    for known, result in _PLACE_CACHE.items():
        km = haversine_km(coords, known)
        if km is not None and km <= best_km:
            best, best_km = result, km
    return best


def _place_and_area(coords: str, session: Session | None = None) -> tuple[str, str]:
    """Best-effort reverse geocode of a 'lat, lon' string to (label, area).

    Tries Google's Geocoding API first when GOOGLE_MAPS_API_KEY is set — its
    POI/business coverage is often noticeably better than OpenStreetMap's in
    areas Nominatim only has street-level data for. Falls back to Nominatim
    (no key required) when Google isn't configured or a lookup fails, and
    falls back further to the raw coordinates if both fail — which stay
    searchable in a maps app either way. A coordinate inside a user-defined
    geofence (see _geofence_name) short-circuits this entirely and uses the
    user's own name for both label and area.
    """
    geofenced = _geofence_name(coords, session)
    if geofenced:
        return geofenced, geofenced
    if not coords or "," not in coords:
        return coords, coords
    if coords in _PLACE_CACHE:
        return _PLACE_CACHE[coords]
    # Not an exact hit, but the same spot to within GPS drift? Reuse that
    # name rather than looking it up again — keeps one physical stop labelled
    # consistently across the trip that ends there and the one that starts
    # there (see _SAME_PLACE_M), and saves a paid geocode call besides.
    nearby = _cached_place_near(coords)
    if nearby:
        _PLACE_CACHE[coords] = nearby
        return nearby
    lat, lon = (p.strip() for p in coords.split(",", 1))
    google_key = get_settings().google_maps_api_key
    google_result = _google_reverse_geocode(lat, lon, google_key) if google_key else None
    if google_result:
        result = google_result
    else:
        global _last_nominatim_at
        wait = _NOMINATIM_MIN_INTERVAL_SEC - (time.monotonic() - _last_nominatim_at)
        if wait > 0:
            time.sleep(wait)
        _last_nominatim_at = time.monotonic()
        try:
            resp = httpx.get(
                "https://nominatim.openstreetmap.org/reverse",
                # zoom 18 = building/address level (16 stopped at neighbourhood, so
                # it named a broad district instead of the actual spot);
                # namedetails surfaces the matched feature's own name.
                params={
                    "format": "jsonv2", "lat": lat, "lon": lon,
                    "zoom": 18, "addressdetails": 1, "namedetails": 1,
                },
                headers={"User-Agent": "tesla-analyzer/0.1"},
                timeout=4.0,
            )
            resp.raise_for_status()
            label, area = _label_from_geocode(resp.json())
            result = (label or coords, area or coords)
        except Exception:  # noqa: BLE001 — never let naming block trip logging
            result = (coords, coords)
    # Cache an ANSWER, never a failure. The fallback returns the coordinates
    # themselves, and storing that made one timeout permanent: every later
    # lookup of the spot — including _cached_place_near for anything within
    # GPS drift of it — got the failure back without retrying.
    #
    # Measured live: two consecutive trips on 13 Aug reading "5.3354,
    # 100.2974" for the same physical stop, one as an arrival and the next as
    # a departure. One Nominatim timeout, two trips permanently unnamed, and
    # every future visit to that spot too.
    #
    # Not caching a miss costs at most one retry per trip closed, which is a
    # handful a day and already rate-limited above.
    if result != (coords, coords):
        _PLACE_CACHE[coords] = result
    return result


def _place(coords: str, session: Session | None = None) -> str:
    """Specific place label only — for callers (Charge locations) that don't
    need the coarser route-grouping key."""
    return _place_and_area(coords, session)[0]


def _forward_geocode(query: str) -> tuple[float, float, str] | None:
    """Resolve a typed place/address to (lat, lon, label). Google's Geocoding
    API first when a key is set (better POI coverage), else Nominatim search —
    same free/paid split as reverse geocoding. None on any miss/failure."""
    query = (query or "").strip()
    if not query:
        return None
    key = get_settings().google_maps_api_key
    if key:
        try:
            resp = httpx.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": query, "key": key}, timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "OK" and data.get("results"):
                r = data["results"][0]
                loc = r["geometry"]["location"]
                return float(loc["lat"]), float(loc["lng"]), r.get("formatted_address", query)
        except Exception:  # noqa: BLE001 — fall through to Nominatim
            pass
    try:
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": "tesla-analyzer/0.1"}, timeout=5.0)
        resp.raise_for_status()
        hits = resp.json()
        if hits:
            h = hits[0]
            return float(h["lat"]), float(h["lon"]), h.get("display_name", query)[:120]
    except Exception:  # noqa: BLE001
        pass
    return None


# Straight-line distances undercount real roads; this is a rough motorway/
# arterial-network multiplier for the fallback when no routing API answers.
_ROAD_WINDING_FACTOR = 1.3


def _driving_distance_km(
    origin: str, dest: str, depart_epoch: int | None = None,
) -> tuple[float, str, float | None] | None:
    """(km, method, traffic_kmh) between two 'lat, lon' strings. Google
    Directions gives the real driving distance when a key is set; otherwise a
    straight-line estimate scaled by a road-winding factor. ``method`` is
    "driving" or "straight-line" so the UI can be honest about which it showed.
    None if even the straight line can't be computed.

    ``depart_epoch`` (unix seconds, must be in the future) additionally asks
    Google for ``duration_in_traffic`` — its prediction for that departure —
    and returns the average speed that implies for this route. That's the one
    thing the car's own history genuinely cannot know: history averages across
    every route driven at a given hour, and says nothing about whether *this*
    road is jammed at *that* time. Speed is returned rather than a Wh/km
    figure on purpose — converting speed to consumption is done from the
    driver's own measured speed/efficiency slope, not a Google assumption.
    traffic_kmh is None whenever the prediction isn't available.
    """
    key = get_settings().google_maps_api_key
    if key:
        try:
            o = origin.replace(" ", "")
            d = dest.replace(" ", "")
            params = {"origin": o, "destination": d, "key": key}
            if depart_epoch:
                params["departure_time"] = str(int(depart_epoch))
                params["traffic_model"] = "best_guess"
            resp = httpx.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params=params, timeout=6.0)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "OK" and data.get("routes"):
                leg = data["routes"][0]["legs"][0]
                meters = leg["distance"]["value"]
                secs = (leg.get("duration_in_traffic") or {}).get("value")
                traffic_kmh = (
                    round(meters / 1000.0 / (secs / 3600.0), 1)
                    if secs and secs > 0 else None
                )
                return round(meters / 1000.0, 1), "driving", traffic_kmh
        except Exception:  # noqa: BLE001 — fall through to straight-line
            pass
    straight = haversine_km(origin, dest)
    if straight is None:
        return None
    return round(straight * _ROAD_WINDING_FACTOR, 1), "straight-line", None


def _build_info() -> dict:
    """Deployed version: git SHA (from the host's env) + image build time in MYT."""
    import os
    from pathlib import Path

    from ..sync import MYT

    sha = (os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GITHUB_SHA") or "")
    sha = sha.strip()[:7] or None
    time_str = None
    for p in ("/app/.build_time", ".build_time"):
        try:
            ts = float(Path(p).read_text().strip())
            time_str = datetime.fromtimestamp(ts, MYT).strftime("%d %b %Y %H%M")
            break
        except (OSError, ValueError):
            continue
    return {"sha": sha, "time": time_str}


@router.get("/health")
def health(session: Session = Depends(get_session)):
    source = state.data_source(session)
    mode = "live" if state.is_live(session) else ("imported" if source == "imported" else "demo")
    return {
        "status": "ok",
        "mode": mode,
        "source": source,
        "oauth_available": auth.oauth_configured(),
        "build": _build_info(),
    }


# --- Data source: manual import -------------------------------------------


@router.post("/import")
async def import_data(
    file: UploadFile = File(...), session: Session = Depends(get_session)
):
    """Button 1 — load a Tesla privacy/usage data export (CSV/JSON/ZIP)."""
    content = await file.read()
    try:
        drives, charges = parse_upload(file.filename or "upload", content)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read file: {exc}") from exc
    return services.replace_with_import(session, drives, charges)


@router.post("/data/clear-drives")
def clear_drives(session: Session = Depends(get_session)):
    """Wipe the trip history for a clean start (charges/battery data kept).

    Sits behind the passcode gate like every other endpoint.
    """
    deleted = services.clear_drives(session)
    return {"deleted_drives": deleted}


@router.post("/data/delete-drives")
def delete_drives(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Delete only the selected trips (by id); charges/battery kept."""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).lstrip("-").isdigit()]
    deleted = services.delete_drives(session, ids)
    return {"deleted_drives": deleted}


@router.post("/data/reset-tags")
def reset_tags(session: Session = Depends(get_session)):
    """Clear the work/personal tag on every trip, back to untagged."""
    reset = services.reset_tags(session)
    return {"reset_tags": reset}


@router.post("/data/auto-tag")
def auto_tag(session: Session = Depends(get_session)):
    """Tag every trip Work/Personal by matching its start/end coordinates
    against the Office/Home Place right now — the same geofence match every
    other Place-aware feature uses, so this reflects your latest Place
    definitions, not whatever a trip's location text happened to say when
    it was logged. A Place counts as the office/home if its name contains
    that word ("Office", "My Office", "office hq" all work — matched on
    whole words, so "Home" matches but "Hometown Cafe" doesn't). Office wins
    if a trip touches both (a commute or a client visit is still a work
    trip); a trip touching neither is set back to untagged. Places are the
    single source of truth here: this overwrites every trip's tag, including
    ones set by hand, not just gaps.

    ``office_place``/``home_place`` in the response say whether any Place
    qualified at all, so the UI can tell "0 changed because everything was
    already right" apart from "0 changed because no Place is named for
    either" — the latter used to fail silently.
    """
    places = session.query(Place).all()
    def has_word(word: str, text: str) -> bool:
        return re.search(rf"\b{word}\b", text.lower()) is not None

    office_place = any(has_word("office", p.name) for p in places)
    home_place = any(has_word("home", p.name) for p in places)
    changed = 0
    for d in session.scalars(select(Drive)).all():
        names = {
            (_geofence_name(c, session, places=places) or "")
            for c in (d.start_coords, d.end_coords) if c
        }
        is_office = any(has_word("office", n) for n in names)
        is_home = any(has_word("home", n) for n in names)
        new_tag = "work" if is_office else "personal" if is_home else ""
        if new_tag != d.tag:
            d.tag = new_tag
            changed += 1
    session.commit()
    return {"changed": changed, "office_place": office_place, "home_place": home_place}


def _relabel_drives(session: Session, ids: list[int] | None) -> dict:
    """Re-geocode trips' start/end locations from their stored raw
    coordinates, discarding whatever's currently shown — a Place name from a
    geofence that's since been deleted/moved, or a result cached before a
    better geocoder (e.g. Google, once GOOGLE_MAPS_API_KEY is set) was
    configured. Still goes through the normal lookup (geofence match first,
    then Google-or-Nominatim), so a trip that genuinely belongs to a Place
    comes right back labeled with it — this refreshes stale labels, it
    doesn't strip correct ones. ``ids=None`` means every trip.

    Also clears the trip's Work/Personal tag (manual or from a prior
    Auto-tag run) — a location reset is meant to be a clean slate for the
    trip's whole place-derived identity, not just the label text, and an
    old tag calculated against a location that's about to change is exactly
    as stale as the location was. Re-tag afterwards with Auto-tag if you
    want it filled back in from the refreshed location.

    Distance, energy, timing and everything else are untouched.
    """
    query = select(Drive)
    if ids is not None:
        query = query.where(Drive.id.in_(ids))
    relabeled = skipped = 0
    # Evict each distinct coordinate from the cache once per run, not once per
    # drive — several trips parked at the same spot would otherwise discard
    # each other's just-refreshed result and re-geocode the same point over
    # the network N times (needlessly slow, and needless load against
    # Nominatim's 1 req/sec policy — see _NOMINATIM_MIN_INTERVAL_SEC).
    refreshed: set[str] = set()

    def _fresh(coords: str) -> tuple[str, str]:
        if coords not in refreshed:
            _PLACE_CACHE.pop(coords, None)
            refreshed.add(coords)
        return _place_and_area(coords, session)

    for d in session.scalars(query).all():
        if not d.start_coords and not d.end_coords:
            skipped += 1
            continue
        if d.start_coords:
            d.start_location, d.start_area = _fresh(d.start_coords)
        if d.end_coords:
            d.end_location, d.end_area = _fresh(d.end_coords)
        d.tag = ""
        relabeled += 1
    session.commit()
    return {"relabeled": relabeled, "skipped": skipped}


@router.post("/data/relabel-all-drives")
def relabel_all_drives(session: Session = Depends(get_session)):
    """Re-geocode every trip's location — see _relabel_drives."""
    return _relabel_drives(session, None)


@router.post("/data/relabel-drives")
def relabel_drives(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Re-geocode only the selected trips' locations (by id) — see
    _relabel_drives."""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).lstrip("-").isdigit()]
    return _relabel_drives(session, ids)


@router.post("/data/clear-charges")
def clear_charges(session: Session = Depends(get_session)):
    """Wipe the charging history for a clean start (trips/battery data kept).

    Sits behind the passcode gate like every other endpoint.
    """
    deleted = services.clear_charges(session)
    return {"deleted_charges": deleted}


@router.post("/data/delete-charges")
def delete_charges(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Delete only the selected charges (by id); trips/battery kept."""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).lstrip("-").isdigit()]
    deleted = services.delete_charges(session, ids)
    return {"deleted_charges": deleted}


@router.post("/data/tag-drive")
def tag_drive(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Set (or clear) a single trip's work/personal/... category."""
    drive_id = payload.get("id")
    if not isinstance(drive_id, int):
        raise HTTPException(400, "Missing or invalid 'id'.")
    tag = str(payload.get("tag") or "")
    if not services.tag_drive(session, drive_id, tag):
        raise HTTPException(404, "Trip not found.")
    return {"id": drive_id, "tag": tag}


@router.post("/data/set-drive-cost")
def set_drive_cost(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Manually price a trip the charge-layer cost model couldn't reach —
    every charge in the vehicle's history was already fully consumed by
    earlier trips with no new charge since (see
    driving_analysis.layered_trip_costs). Pass cost=None (or omit it) to
    clear a previously-set override and let it price automatically again."""
    drive_id = payload.get("id")
    if not isinstance(drive_id, int):
        raise HTTPException(400, "Missing or invalid 'id'.")
    cost = payload.get("cost")
    if cost is not None:
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid 'cost'.")
        if cost < 0:
            raise HTTPException(400, "'cost' must be >= 0.")
    drive = services.set_drive_cost(session, drive_id, cost)
    if drive is None:
        raise HTTPException(404, "Trip not found.")
    return {"id": drive_id, "cost_override": drive.cost_override}


@router.post("/data/edit-drive")
def edit_drive(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Manually correct a trip's start and/or end time — for a no-signal
    park/departure the sync-time estimate still got wrong (see sync.py's
    pace-based corrections, which fix this going forward but can't rewrite
    already-logged trips). distance_km/energy_used_kwh are untouched (they
    come from the odometer/SoC readings, not the clock); duration_min and
    avg_speed_kmh are recalculated from the new times.
    """
    drive_id = payload.get("id")
    if not isinstance(drive_id, int):
        raise HTTPException(400, "Missing or invalid 'id'.")
    start_time = end_time = None
    try:
        if payload.get("start_time"):
            start_time = datetime.fromisoformat(payload["start_time"])
        if payload.get("end_time"):
            end_time = datetime.fromisoformat(payload["end_time"])
    except ValueError:
        raise HTTPException(400, "Invalid 'start_time'/'end_time' (expected ISO format).")
    if start_time is None and end_time is None:
        raise HTTPException(400, "Provide 'start_time' and/or 'end_time'.")
    try:
        drive = services.edit_drive(session, drive_id, start_time, end_time)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if drive is None:
        raise HTTPException(404, "Trip not found.")
    return {
        "id": drive.id,
        "start_time": drive.start_time.isoformat(timespec="minutes"),
        "end_time": drive.end_time.isoformat(timespec="minutes"),
        "duration_min": drive.duration_min,
        "avg_speed_kmh": drive.avg_speed_kmh,
    }


# --- Named places (geofenced Home/Office/... trip labels) -----------------


def _relabel_existing(session: Session, place: Place) -> int:
    """Retroactively apply a new/changed geofence's name to already-logged
    trips (and charges, where the raw coords are still recoverable) whose
    stored coordinates fall inside its radius — so defining "Home" today
    renames the driveway on last month's trips too, not just future ones.

    Charge rows don't keep a separate raw-coords column (only the already-
    resolved ``location`` label), so a charge can only be relabeled here when
    that label still looks like coordinates — i.e. the geocode never
    resolved it to a name. Charges already labeled by Nominatim are left as
    they are; only new charges going through ``_place`` pick up the geofence
    from here on.
    """
    changed = 0
    place_coords = f"{place.lat}, {place.lon}"
    drives = session.scalars(
        select(Drive).where((Drive.start_coords != "") | (Drive.end_coords != ""))
    ).all()
    for d in drives:
        if d.start_coords:
            dist = haversine_km(d.start_coords, place_coords)
            if dist is not None and dist <= place.radius_km:
                d.start_location = place.name
                d.start_area = place.name
                changed += 1
        if d.end_coords:
            dist = haversine_km(d.end_coords, place_coords)
            if dist is not None and dist <= place.radius_km:
                d.end_location = place.name
                d.end_area = place.name
                changed += 1
    for c in session.scalars(select(Charge)).all():
        if c.location and "," in c.location:
            dist = haversine_km(c.location, place_coords)
            if dist is not None and dist <= place.radius_km:
                c.location = place.name
                changed += 1
    return changed


@router.get("/places")
def list_places(session: Session = Depends(get_session)):
    """User-defined geofences that override trip/charge location names."""
    places = session.scalars(select(Place).order_by(Place.name)).all()
    return [
        {"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon,
         "radius_km": p.radius_km, "departure_pace_kmh": p.departure_pace_kmh}
        for p in places
    ]


@router.post("/places")
def create_place(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Add (or, by name, update) a named geofence and relabel matching history."""
    name = str(payload.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "Missing 'name'.")
    try:
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Missing or invalid 'lat'/'lon'.")
    radius_km = float(payload.get("radius_km") or 0.15)
    radius_km = max(0.02, min(radius_km, 5.0))  # sane bounds: 20 m to 5 km
    # Absent means "unchanged", not "clear it" — the dashboard's place editor
    # predates this field and posts without it, so reading a missing key as 0
    # would silently wipe the pace every time a geofence was nudged.
    pace = payload.get("departure_pace_kmh")
    if pace is not None:
        try:
            pace = float(pace)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid 'departure_pace_kmh'.")
        # 0 clears it (back to the global default); otherwise a walking pace
        # and a motorway average both mean someone typed the wrong thing.
        if pace and not 5.0 <= pace <= 90.0:
            raise HTTPException(400, "departure_pace_kmh must be 0, or between 5 and 90.")

    place = session.scalars(select(Place).where(Place.name == name)).first()
    if place:
        place.lat, place.lon, place.radius_km = lat, lon, radius_km
    else:
        place = Place(name=name, lat=lat, lon=lon, radius_km=radius_km,
                      created_at=sync_mod.now_local())
        session.add(place)
    if pace is not None:
        place.departure_pace_kmh = pace
    session.flush()
    relabeled = _relabel_existing(session, place)
    session.commit()
    _PLACE_CACHE.clear()  # a newly-named geofence can relabel already-cached coords
    return {"id": place.id, "name": place.name, "lat": place.lat, "lon": place.lon,
            "radius_km": place.radius_km,
            "departure_pace_kmh": place.departure_pace_kmh, "relabeled": relabeled}


@router.get("/set-departure-pace")
def set_departure_pace(
    place: str, kmh: float, session: Session = Depends(get_session)
):
    """Record how fast the car actually gets away from a named place, km/h.

    Only affects departures the sleep-recheck window did not see: the trip's
    distance and energy are anchored to the odometer and SoC either way, so
    this moves the logged START TIME and nothing else. Pass kmh=0 to go back
    to the global default (sync.DEPARTURE_PACE_KMH).

    Where the number comes from, since the app cannot measure it: take a trip
    whose diagnostics show a large ``start_lost_km``, read the car's own
    duration for it off the Trips screen, and divide the blind distance by
    the minutes the car was really driving before we first saw it. Three
    such readings at Home gave 47, 55 and 45 km/h.

    A GET so it can be run from the address bar, like the repair endpoints.
    """
    row = session.scalars(
        select(Place).where(func.lower(Place.name) == place.strip().lower())
    ).first()
    if row is None:
        known = [p.name for p in session.scalars(select(Place).order_by(Place.name))]
        raise HTTPException(404, f"No place named {place!r}. Known places: {known}")
    if kmh and not 5.0 <= kmh <= 90.0:
        raise HTTPException(400, "kmh must be 0 (use the default), or between 5 and 90.")
    was = row.departure_pace_kmh
    row.departure_pace_kmh = round(kmh, 1)
    session.commit()
    return {
        "place": row.name,
        "departure_pace_kmh": row.departure_pace_kmh,
        "was": was,
        "default_kmh": sync_mod.DEPARTURE_PACE_KMH,
        "applies_to": "future departures only — already-logged trips keep their times",
    }


@router.delete("/places/{place_id}")
def delete_place(place_id: int, session: Session = Depends(get_session)):
    """Remove a geofence. Already-relabeled trips keep the place's name —
    only new processing stops applying it."""
    place = session.get(Place, place_id)
    if place is None:
        raise HTTPException(404, "Place not found.")
    session.delete(place)
    session.commit()
    return {"deleted": True}


# --- Service & tyre tracker -------------------------------------------------


@router.get("/service")
def list_service(session: Session = Depends(get_session)):
    """Logged maintenance history plus a due/overdue reading for each known
    service type (see app/analysis/service.py)."""
    vehicle = _first_vehicle(session)
    records = session.scalars(
        select(ServiceRecord)
        .where(ServiceRecord.vehicle_id == vehicle.id)
        .order_by(ServiceRecord.date.desc())
    ).all()
    current_odo = session.scalar(
        select(func.max(BatteryReading.odo_km)).where(BatteryReading.vehicle_id == vehicle.id)
    )
    due = service_analysis.due_status(
        [{"type": r.type, "date": r.date, "odo_km": r.odo_km} for r in records],
        current_odo_km=current_odo,
    )
    for row in due:
        row["last_date"] = row["last_date"].isoformat() if row["last_date"] else None
        row["due_date"] = row["due_date"].isoformat() if row["due_date"] else None
    return {
        "current_odo_km": round(current_odo, 1) if current_odo is not None else None,
        "types": list(service_analysis.SERVICE_INTERVALS.keys()),
        "due": due,
        "records": [
            {"id": r.id, "type": r.type, "date": r.date.isoformat(), "odo_km": r.odo_km,
             "cost": r.cost, "notes": r.notes}
            for r in records
        ],
    }


@router.post("/service")
def add_service(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Log a maintenance event. ``type`` can be any of the known tracked
    types (gets a due-date/odometer projection) or free text (logged for the
    record but never shows as due/overdue)."""
    vehicle = _first_vehicle(session)
    type_ = str(payload.get("type") or "").strip()[:40]
    if not type_:
        raise HTTPException(400, "Missing 'type'.")
    try:
        date = (datetime.fromisoformat(payload["date"]) if payload.get("date")
                else sync_mod.now_local())
    except (KeyError, ValueError):
        raise HTTPException(400, "Invalid 'date' (expected ISO format).")
    try:
        odo_km = float(payload.get("odo_km") or 0.0)
        cost = float(payload.get("cost") or 0.0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid 'odo_km'/'cost'.")
    notes = str(payload.get("notes") or "")[:200]

    record = ServiceRecord(
        vehicle_id=vehicle.id, type=type_, date=date, odo_km=odo_km, cost=cost, notes=notes,
    )
    session.add(record)
    session.commit()
    return {"id": record.id}


@router.delete("/service/{record_id}")
def delete_service(record_id: int, session: Session = Depends(get_session)):
    record = session.get(ServiceRecord, record_id)
    if record is None:
        raise HTTPException(404, "Record not found.")
    session.delete(record)
    session.commit()
    return {"deleted": True}


# --- Data source: link Tesla account --------------------------------------


@router.post("/link/token")
def link_token(
    payload: dict = Body(...), session: Session = Depends(get_session)
):
    """Button 2 (token flow) — link an account with an access token."""
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise HTTPException(422, "access_token is required.")
    try:
        return services.link_with_token(
            session,
            token,
            refresh_token=(payload.get("refresh_token") or "").strip(),
            base_url=(payload.get("base_url") or "").strip() or None,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(401, f"Tesla rejected the token ({exc.response.status_code}).") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not link account: {exc}") from exc


def _oauth_redirect_uri(request: Request) -> str:
    """Callback URL derived from the live host, so no env config is needed.

    Cloud proxies terminate TLS, so anything that isn't localhost is forced to
    https (Tesla also refuses plain-http redirect URIs).
    """
    host = request.url.hostname or "localhost"
    if host in ("localhost", "127.0.0.1"):
        return str(request.base_url).rstrip("/") + "/api/link/oauth/callback"
    return f"https://{request.url.netloc}/api/link/oauth/callback"


@router.get("/link/oauth/start")
def oauth_start(request: Request, session: Session = Depends(get_session)):
    """Button 2 (OAuth flow) — redirect to Tesla's sign-in page."""
    if not auth.oauth_configured():
        raise HTTPException(
            400,
            "Tesla OAuth is not configured. Set TESLA_CLIENT_ID / TESLA_CLIENT_SECRET, "
            "or use the access-token option instead.",
        )
    # One-time Fleet API requirement: register this domain with Tesla. Tesla
    # fetches the public key the app serves under /.well-known/ during the call.
    if state.get(session, "partner_registered") != "yes":
        domain = request.url.hostname or ""
        try:
            auth.register_partner(domain)
            state.put(session, "partner_registered", "yes")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                400,
                f"Tesla app registration for domain '{domain}' failed: {exc}. "
                "Check TESLA_CLIENT_ID / TESLA_CLIENT_SECRET and that this domain "
                "is listed under Allowed Origins in your Tesla developer app.",
            ) from exc
    url, _state = auth.authorize_url(_oauth_redirect_uri(request))
    return RedirectResponse(url)


@router.get("/link/oauth/callback")
def oauth_callback(
    request: Request,
    code: str | None = None, error: str | None = None,
    session: Session = Depends(get_session),
):
    if error:
        raise HTTPException(400, f"Tesla sign-in failed: {error}")
    if not code:
        raise HTTPException(400, "Missing authorization code.")
    try:
        tokens = auth.exchange_code(code, _oauth_redirect_uri(request))
        result = services.link_with_token(
            session,
            tokens["access_token"],
            refresh_token=tokens.get("refresh_token", ""),
            base_url=get_settings().tesla_oauth_audience,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"OAuth exchange failed: {exc}") from exc
    # Land the user back on the dashboard.
    return RedirectResponse(f"/?linked={result['source']}")


def _process_vehicle(
    session: Session, data: dict, v_summary: dict, settings, migrate_legacy: bool = False
) -> tuple:
    """Log drives/charges for one car from its vehicle_data snapshot.

    Snapshot / open-trip / open-charge state is namespaced by VIN, so each car
    on the account advances its own independent session state machine. Returns
    ``(vehicle, snapshot, n_drives, n_charges, open_trip)``.

    ``migrate_legacy`` (set for the active car) folds in the pre-multi-car global
    ``last_snapshot`` state a one time, so a drive taken around the upgrade to
    per-VIN state isn't dropped.
    """
    import json as _json

    from .. import sync as sync_mod

    vin = data.get("vin") or v_summary.get("vin") or "LINKED-UNKNOWN"
    vehicle = session.query(Vehicle).filter(Vehicle.vin == vin).first()
    if vehicle is None:
        vehicle = Vehicle(vin=vin, name=data.get("display_name") or "My Tesla", model="Tesla")
        session.add(vehicle)
        session.flush()

    # Enrich from the car's own config (real model / trim / colour / wheels).
    cfg = data.get("vehicle_config") or {}
    car_map = {"model3": "Model 3", "modely": "Model Y",
               "models": "Model S", "modelx": "Model X"}
    real_model = (
        car_map.get((cfg.get("car_type") or "").lower().replace(" ", ""))
        or vin_mod.decode(vin).get("model")
    )
    if real_model and vehicle.model in ("Tesla", ""):
        vehicle.model = real_model
    if not vehicle.trim:
        trim_bits = [
            (cfg.get("trim_badging") or "").upper(),
            (cfg.get("exterior_color") or ""),
        ]
        vehicle.trim = " ".join(b for b in trim_bits if b)
    wheel = cfg.get("wheel_type") or ""
    if wheel and wheel.upper() not in vehicle.trim.upper():
        vehicle.trim = f"{vehicle.trim} {wheel}".strip()[:60]
    if data.get("display_name") and vehicle.name in ("My Tesla", ""):
        vehicle.name = data["display_name"]
    # Seed usable capacity from the variant spec so the EMA starts from the
    # right pack size (an LR Model 3 is ~78 kWh, not the generic 75 default)
    # and per-drive kWh is right from the first synced drive. Only when the
    # column is still untouched — a measured value is never overwritten.
    if vehicle.battery_capacity_kwh == 75.0:
        spec_cap = battery_analysis.usable_capacity_for(
            vehicle.model, vehicle.trim, vin_mod.decode(vin).get("year"))
        if spec_cap:
            vehicle.battery_capacity_kwh = spec_cap

    snap = sync_mod.snapshot_from_vehicle_data(data)
    sk = state.scoped(state.SNAPSHOT_KEY, vin)
    tk = state.scoped(state.OPEN_TRIP_KEY, vin)
    ck = state.scoped(state.OPEN_CHARGE_KEY, vin)
    # Usable pack capacity: override > measured charge EMA (seeded from the
    # variant spec) > spec > default. The EMA is a smoothed average over
    # measured charges — robust to a single contaminated charge reading that
    # a "last full charge" figure would swallow whole.
    capacity_kwh, _ = _usable_capacity(session, vehicle, settings)

    # One-time migration from the pre-multi-car global keys (only the active car
    # inherits them), so a drive taken around the upgrade isn't lost.
    recovered: list[dict] = []
    if migrate_legacy:
        legacy_snap = state.get(session, state.SNAPSHOT_KEY)  # bare/global key
        if legacy_snap:
            if not state.get(session, sk):
                # Never synced under the scoped key yet — adopt the legacy state so
                # the normal gap-fallback below logs the missed drive.
                state.put(session, sk, legacy_snap)
                if not state.get(session, tk):
                    state.put(session, tk, state.get(session, state.OPEN_TRIP_KEY) or "")
                if not state.get(session, ck):
                    state.put(session, ck, state.get(session, state.OPEN_CHARGE_KEY) or "")
            else:
                # Already synced under the scoped key once (the transition drive
                # slipped through) — reconstruct it from the legacy → scoped gap.
                try:
                    legacy = _json.loads(legacy_snap)
                    scoped = _json.loads(state.get(session, sk))
                    d = sync_mod._drive_from(legacy, scoped, capacity_kwh)
                    if d:
                        # The car slept between the two snapshots, so the true drive
                        # time is unknown; give it a sensible duration from the
                        # distance (~40 km/h) anchored at the later snapshot, rather
                        # than the whole multi-hour gap.
                        dur = max(round(d["distance_km"] / 40.0 * 60.0), 1)
                        end = sync_mod._dt(scoped["ts"])
                        d["end_time"] = end
                        d["start_time"] = end - timedelta(minutes=dur)
                        d["duration_min"] = float(dur)
                        d["avg_speed_kmh"] = round(d["distance_km"] / (dur / 60.0), 1)
                        d["max_speed_kmh"] = max(d["max_speed_kmh"], d["avg_speed_kmh"])
                        recovered.append(d)
                except (ValueError, KeyError, TypeError):
                    pass
            state.delete(
                session, state.SNAPSHOT_KEY, state.OPEN_TRIP_KEY, state.OPEN_CHARGE_KEY
            )

    prev_raw = state.get(session, sk)
    prev = _json.loads(prev_raw) if prev_raw else None
    open_trip = _json.loads(state.get(session, tk) or "null")
    open_charge = _json.loads(state.get(session, ck) or "null")

    # A trip closed on sustained "offline" (see LAST_SLEEP_CLOSE_KEY) may have
    # been cut short by the same dead zone that caused the closure — this poll
    # is the first fresh reading since, so it's the only chance to tell.
    # Further movement while the car now reads parked belongs to that same
    # drive continuing through the dead zone, not a new one; extend the trip
    # that already closed rather than let process_snapshot() log the rest as
    # an unrelated phantom trip. Two different guards for two different risks:
    # a small amount (GAP_CREEP_MAX_KM) merges regardless of how long the
    # reconnect took — that's just final parking creep. A larger amount still
    # merges as long as it's within SLEEP_CLOSE_MERGE_MAX_MIN of the close —
    # sustained "offline" is only 3 minutes, routinely exceeded by an active
    # drive through a real dead zone, so a few km turning up minutes later is
    # still more likely the same continuing drive than a genuine second one.
    # Past both guards, or if the car's already driving again, don't guess —
    # record the real amount instead of leaving the false 0.0 from close time.
    sleep_close_key = state.scoped(state.LAST_SLEEP_CLOSE_KEY, vin)
    sleep_close_raw = state.get(session, sleep_close_key)
    if sleep_close_raw and prev:
        marker = _json.loads(sleep_close_raw)
        est_credited = marker.get("est_km") or 0.0
        # Kept unclamped for the calibration sample below. est_credited is
        # about to be trimmed to whatever the car actually covered, which is
        # the right value for correcting the trip and exactly the wrong one for
        # scoring the model — a prediction trimmed to fit the outcome always
        # scores perfectly.
        est_predicted = est_credited
        est_sec = marker.get("est_sec") or 0.0
        # Everything the odometer has done since the close. The estimate was a
        # claim on part of it, and this is the first moment anything can check
        # that claim against a measurement.
        raw_moved = snap["odo_km"] - marker["odo_km"]
        # Zero is a measurement, not the absence of one, and on this path it is
        # the strongest one available: it proves the tail did not happen. A car
        # whose last reading really was its arrival is next read at the same
        # odometer, and requiring strictly positive movement excluded exactly
        # that case — the one where the estimate is most wrong and where
        # nothing else will ever revisit it, since this block runs once and
        # then clears its marker.
        if marker.get("corrected"):
            # Already reconciled by hand against the car's own trip meter, so
            # the closed trip needs nothing from this poll — but the HAND-OVER
            # still does. That is the half that was missed: this marker does
            # two jobs, correcting the trip that closed and telling the next
            # trip where to begin, and repair_arrival_tail used to clear it
            # outright, which cancelled both. Measured on trip 333, which
            # anchored to the pre-blackout reading and re-counted 0.320 km
            # already credited to 332 — 11.0 km against the car's 10.7.
            #
            # No calibration sample either: the prediction was superseded by a
            # human reading a screen, so scoring the model against what is left
            # would score it against an answer it did not produce.
            if est_credited and prev.get("odo_km") == marker["odo_km"]:
                prev = {**prev, "odo_km": prev["odo_km"] + est_credited}
        elif prev.get("odo_km") == marker["odo_km"] and raw_moved >= 0:
            closed_drive = session.get(Drive, marker["drive_id"])
            # An estimate larger than the ground the car actually covered is
            # simply wrong, and wrong in the direction that matters: left
            # standing it would also push the next trip's start past its own
            # beginning. Trimmed to fit before anything else reads it.
            if closed_drive is not None and est_credited > raw_moved:
                over = est_credited - raw_moved
                _retract_estimated_tail(
                    closed_drive, over,
                    est_sec * (over / est_credited) if est_credited else 0.0)
                est_sec *= raw_moved / est_credited
                est_credited = raw_moved
            # The boundary is where the estimate left it, not the raw reading —
            # the estimated tail already belongs to the closed trip.
            moved = raw_moved - est_credited
            close_ts = marker.get("ts")
            elapsed_min = (snap["ts"] - close_ts) / 60.0 if close_ts else float("inf")
            # Which report closed the trip decides how much this poll may
            # attribute to it. "asleep" is the trustworthy one — a car cannot
            # reach sleep while moving, so the drive really was over. But that
            # only proves the car had STOPPED, not that last_snapshot was taken
            # at the stop: that reading can still be a poll interval stale, and
            # a trip closed on it then reads short by whatever the car covered
            # in between (confirmed live, trip 314: 0.4 km and a minute of
            # arrival missing, with tail_trim_sec null marking the sleep-close
            # path). So a small tail still folds in. What must NOT apply is the
            # time-based branch, which exists because sustained "offline" can
            # fire mid-drive; after a genuine sleep any sizable movement is a
            # new trip, not this one continuing. Markers written before the
            # field default to the offline (less trusting) rules.
            asleep_close = marker.get("reason") == "asleep"
            fold_in = (
                closed_drive is not None and not sync_mod.is_driving(snap)
                and (moved <= sync_mod.GAP_CREEP_MAX_KM
                     or (not asleep_close
                         and elapsed_min <= sync_mod.SLEEP_CLOSE_MERGE_MAX_MIN))
            )
            if fold_in:
                # A poll can finally see this ground, so the estimate made at
                # close time is superseded — taken back whole before the
                # measurement goes in, or the trip would carry both. This is
                # the correction the estimate exists to wait for.
                #
                # Whole estimate out, whole measurement in: raw_moved, not
                # moved. The two are different quantities and only one of them
                # belongs here. `moved` is what is left over ONCE the estimate
                # is allowed to stand, which is the right question for the
                # branches that leave it standing — but this branch has just
                # revoked it, so the trip is short by the full stretch from the
                # marker's anchor to here. Subtracting the estimate and then
                # adding only the remainder credited the tail to nobody.
                _retract_estimated_tail(
                    closed_drive, closed_drive.end_est_km or 0.0, est_sec)
                closed_drive.distance_km = round(closed_drive.distance_km + raw_moved, 1)
                # Distance alone isn't the whole trip. wh_per_km is derived
                # (energy / distance) and soc_used_pct is derived from the
                # energy too, so growing distance while leaving energy at its
                # close-time value silently understates both — measured on the
                # 4 km case, +33% distance against +0.00 kWh dropped Wh/km by
                # 25%. prev is the close reading itself (the odo_km guard above
                # proves nothing has moved the snapshot since), so it's the
                # right "from" end for the stretch being folded in, and snap
                # carries the fresh soc/range to measure it against.
                #
                # Gated the same way the departure-side recovery is gated, for
                # the same reason: a car that reached its destination early in
                # the window sat parked for the rest of it, accruing standby
                # drain that is not this drive's. Past either bound the distance
                # still folds in — the odometer is measured either way — but the
                # energy stays as measured at close time.
                #
                # BOTH bounds, because efficiency alone cannot do this job.
                # Trip 319 proved it on the departure side: 0.21 kWh over
                # 0.517 km is 406 Wh/km, comfortably under the threshold,
                # because 400 Wh/km is ordinary for half a kilometre of
                # parking-lot crawl in the heat — so a 2.3 h park passed as
                # plausible driving. The identical hole was here, and this is
                # the likelier end for it to open: a sleep close means the car
                # went quiet, so a long gap before the next poll is the normal
                # case on this path rather than the exception. Duration is what
                # separates an arrival from a stale anchor.
                extra_kwh = sync_mod._energy_kwh(prev, snap, capacity_kwh)
                # raw_moved can now legitimately be zero — the car parked where
                # it was last read and never moved again. There is no distance
                # to price and no implied efficiency to test, so the whole
                # question is moot rather than failed: fall through, where a
                # blind distance of zero leaves the energy exactly as it is.
                if (raw_moved > 0
                        and extra_kwh * 1000.0 / raw_moved <= sync_mod.MAX_PLAUSIBLE_WH_PER_KM
                        and elapsed_min <= sync_mod.STALE_ANCHOR_MAX_MIN):
                    closed_drive.energy_used_kwh = round(
                        closed_drive.energy_used_kwh + extra_kwh, 2)
                    # end_soc moves with it or not at all — it is what
                    # soc_used_pct falls back on when energy is unknown, so a
                    # fresh end_soc against a stale energy figure would leave
                    # the two disagreeing about the same trip.
                    closed_drive.end_soc = snap["soc"]
                else:
                    # Refusing the measurement is not the same as refusing to
                    # account for the kilometres. The distance folded in above
                    # regardless — the odometer is measured at any staleness —
                    # so leaving energy untouched here grows the numerator's
                    # denominator and nothing else, diluting Wh/km by exactly
                    # the folded share. That is the identical defect
                    # energy_for_blind_distance was written for and that the
                    # sustained-offline top-up already had once (+33% distance
                    # against +0.00 kWh, Wh/km down a quarter); this path was
                    # the last place still carrying it.
                    #
                    # So the blind stretch gets priced at the trip's own
                    # measured efficiency, which is what "the SoC drop across a
                    # two-hour park is not this drive's energy" actually calls
                    # for: not zero, but the drive's own rate over ground the
                    # drive really covered. The estimated tail's share, taken
                    # out by the retraction above, comes back through the same
                    # arithmetic rather than separately — it is blind distance
                    # by the same definition. Refused outright once the blind
                    # part is more than half the trip (BLIND_DISTANCE_MAX_SHARE),
                    # where holding Wh/km constant would amplify an error
                    # instead of extending a measurement.
                    closed_drive.energy_used_kwh = round(
                        sync_mod.energy_for_blind_distance(
                            closed_drive.energy_used_kwh,
                            closed_drive.distance_km, raw_moved), 2)
                # end_time was anchored to the marker's own timestamp — the
                # last reading *before* the dead zone, same stale anchor
                # distance had. Folding in the distance without also moving
                # the clock forward would leave duration understated by
                # however long the dead zone lasted (confirmed live: a trip
                # read 2 minutes short with distance/energy both otherwise
                # clean). No pace evidence is available here — the car reads
                # parked now, so its current speed says nothing about how
                # fast it covered the extra distance — so this uses the same
                # CITY_SPEED_KMH floor the departure-side estimate falls back
                # on when it's equally in the dark. Clamped to snap's own
                # timestamp so the estimate can only move the stop closer to
                # the truth, never past the moment it was actually observed.
                # close_ts is only missing for a marker written before it was
                # added to LAST_SLEEP_CLOSE_KEY — skip the timestamp estimate
                # then rather than guess from nothing; distance still gets
                # the fold-in above regardless.
                if close_ts:
                    travel_sec = raw_moved / sync_mod.CITY_SPEED_KMH * 3600.0
                    est_end_ts = min(close_ts + travel_sec, snap["ts"])
                    closed_drive.end_time = sync_mod._dt(est_end_ts)
                    # start_time is naive but represents MYT wall-clock (see
                    # sync._dt) — re-attaching that tzinfo before .timestamp()
                    # is what makes this epoch math correct regardless of the
                    # server's own system timezone.
                    start_ts = closed_drive.start_time.replace(tzinfo=sync_mod.MYT).timestamp()
                    closed_drive.duration_min = round((est_end_ts - start_ts) / 60.0, 1)
                    if closed_drive.duration_min > 0:
                        closed_drive.avg_speed_kmh = round(
                            closed_drive.distance_km / (closed_drive.duration_min / 60.0), 1)
                # The arrival point moves with the distance, the mirror of the
                # departure-side rule in sync.py: growing a trip's odometer
                # while leaving its end coordinates at the pre-blackout anchor
                # makes the row claim two different places for one arrival. The
                # car reads parked right now, so snap IS where it came to rest,
                # however late the reading is — a stopped car doesn't move, the
                # same argument the departure side uses about a parked one.
                #
                # This matters most on the offline path, which has no distance
                # bound at all: a drive through a dead zone that reconnects
                # several km later folds every one of them in, and without this
                # would name the tunnel mouth as its destination.
                # And so does the odometer the trip records stopping at. It is
                # not decoration: odometer_continuity reads exactly this field
                # against the next trip's start_odo_km, so a trip whose
                # distance grew to cover the dead zone while its end_odo_km
                # stayed at the pre-blackout anchor reports the very ground it
                # just absorbed as missing. snap is the reading the fold-in
                # trusted for everything else here; it has to be trusted for
                # this too.
                closed_drive.end_odo_km = round(snap["odo_km"], 3)
                end_coords = sync_mod._coords(snap)
                if end_coords:
                    closed_drive.end_coords = end_coords
                    closed_drive.end_location, closed_drive.end_area = (
                        _place_and_area(end_coords, session))
                # This branch, and only this branch, has both halves of a
                # calibration pair: a prediction made when the car went dark,
                # and a poll that has just measured the very stretch it was
                # about. Recorded rather than acted on — the arrival model has
                # a free parameter that shipped set to the poller's own timeout,
                # and one trip's worth of evidence already proved enough to
                # mis-tune it once (see ArrivalTailSample).
                #
                # raw_moved, not moved: the prediction was about the whole
                # unseen stretch, so that is what it has to be scored against.
                _record_tail_sample(
                    session, closed_drive, est_predicted, raw_moved,
                    speed_kmh=marker.get("speed_kmh"),
                    est_sec=marker.get("est_sec"),
                    elapsed_min=round(elapsed_min, 1) if close_ts else None,
                    reason=marker.get("reason") or "")
                session.commit()
                # Tell process_snapshot() this ground is already covered, so its
                # own gap-reconstruction sees no movement here and stays quiet.
                prev = {**prev, "odo_km": snap["odo_km"]}
            elif (closed_drive and not asleep_close
                  and not sync_mod.is_driving(snap)
                  and elapsed_min <= sync_mod.SLEEP_CLOSE_MERGE_MAX_MIN):
                # Never for an asleep close: that anchor is only ever a poll
                # interval short, so movement big enough to be refused above
                # belongs to a later trip, and stamping it here would report a
                # loss this trip never had.
                # Only inside the same window the merge uses. Past it, movement
                # after the anchor is far more likely a genuinely separate
                # later departure than a tail this trip was cut short of — and
                # stamping it here would double-report distance the following
                # trip already accounts for, since process_snapshot runs next
                # against this same unmodified prev and either pulls the
                # movement into the new trip (departure recovery) or records it
                # as that trip's start_lost_km. Confirmed: a close, 8 h parked,
                # then a drive off logged end_lost_km 2.0 on the old trip while
                # the new trip's anchor was pulled back to cover the same 2 km.
                # Reporting the same distance twice under two names is exactly
                # what the blind-gap fold-in avoids (see sync.py's
                # GAP_CREEP_MAX_KM branch); this is the same rule.
                #
                # And never when the car is ALREADY DRIVING at this poll, which
                # the time window alone did not catch. `moved` then spans two
                # trips: the arrival this one was cut short of, and the
                # departure the next one is in the middle of, with no reading
                # between them to say where one ends. The whole of it is not
                # this trip's tail, and the next trip's departure recovery is
                # about to claim the same distance — which is precisely the
                # double-report above, arriving through a door the window left
                # open.
                #
                # Measured: trip 334 closed at 17:33, the next contact was
                # 27 minutes later with the car already driving, and it logged
                # end_lost_km 0.425 while trip 335 recorded start_recovered_km
                # 0.425 for the same ground. The real tail was about 0.198; the
                # rest was trip 335's own departure.
                #
                # Left as None — unknown — because that is what it is. The
                # distance is not lost: the next trip carries it, and says so
                # in start_recovered_km. What cannot be said is how much of it
                # belonged to this trip, and a number here would be asserting
                # exactly that.
                closed_drive.end_lost_km = round(moved, 3)
                session.commit()
            if not fold_in and est_credited:
                # The fold-in, when it runs, already hands the whole stretch
                # over. When it doesn't, the estimate stands and the departing
                # trip has to start past it — one fixed quantity, credited
                # once. Independent of the branches above, which decide what
                # the CLOSED trip records, not where the next one begins.
                prev = {**prev, "odo_km": prev["odo_km"] + est_credited}
        state.put(session, sleep_close_key, "")

    # Where the last closed trip ended, for the departure recovery — see
    # process_snapshot's prev_close_odo_km. Looked up only in the one shape
    # that can use it (no trip open, the car driving now, and the last
    # snapshot frozen mid-drive because a blackout hid its park), so an
    # ordinary poll still touches no extra rows.
    prev_close_odo = None
    if (open_trip is None and prev is not None and sync_mod.is_driving(prev)
            and sync_mod.is_driving(snap)):
        last_closed = session.scalars(
            select(Drive).where(Drive.vehicle_id == vehicle.id)
            .order_by(Drive.start_time.desc()).limit(1)
        ).first()
        # It has to end at prev's own position to stand in for it: far enough
        # back and it is some older trip with an unlogged journey since, which
        # is exactly the case the strict guard should keep refusing. Never past
        # where the car is now either — that ground is already claimed.
        if (last_closed is not None and last_closed.end_odo_km is not None
                and prev["odo_km"] - sync_mod.GAP_CREEP_MAX_KM
                <= last_closed.end_odo_km <= snap["odo_km"]):
            prev_close_odo = last_closed.end_odo_km

    # How fast this car gets away from wherever it was last parked, if that
    # spot is a Place with its own figure. Looked up only when the car is
    # moving now — that is the only shape the departure recovery runs in, so
    # an idle poll still touches no extra rows.
    place_pace = (
        _place_departure_pace(session, prev)
        if prev is not None and sync_mod.is_driving(snap) else None
    )

    drives, charges, open_trip, open_charge = sync_mod.process_snapshot(
        prev, snap, open_trip, open_charge,
        capacity_kwh, settings.energy_price_per_kwh, settings.drive_min_km,
        prev_close_odo_km=prev_close_odo,
        last_quiet_ts=float(state.get(session, state.QUIET_SEEN_KEY) or 0) or None,
        departure_pace_kmh=place_pace,
    )
    drives = recovered + drives  # include a drive recovered from the upgrade gap
    # A trimmed tail is time the car spent parked, so its standby draw is not
    # this drive's energy — but the trim only moves the clock, leaving the
    # stop snapshot's SoC where the late reading found it (see
    # sync.trim_standby_kwh). Corrected here rather than inside
    # process_snapshot because the rate comes from this car's own history,
    # which needs the session; and looked up only when a trim actually fired,
    # so the ordinary poll still touches no extra rows.
    #
    # The departure end needs the same correction for the mirror-image reason:
    # a recovery that reaches back over a blackout takes prev's SoC as this
    # trip's baseline, and the minutes the car was still parked in that gap
    # (start_park_min) drained it without this drive turning a wheel. Same
    # rate, same floor, same function — the only difference is which end of
    # the trip the parked minutes sit at.
    if any((d.get("tail_trim_sec") or 0) > 0 or (d.get("start_park_min") or 0) > 0
           for d in drives):
        past_drives = session.scalars(
            select(Drive).where(Drive.vehicle_id == vehicle.id).order_by(Drive.start_time)
        ).all()
        past_charges = session.scalars(
            select(Charge).where(Charge.vehicle_id == vehicle.id)
        ).all()
        rate_kw = _trim_rate_kw(
            list(past_drives), list(past_charges), capacity_kwh)
        for d in drives:
            d["energy_used_kwh"] = sync_mod.trim_standby_kwh(
                d["energy_used_kwh"], d["distance_km"],
                (d.get("tail_trim_sec") or 0.0) + (d.get("start_park_min") or 0.0) * 60.0,
                rate_kw)
    for d in drives:
        # Keep the raw coords (for map links) before geocoding replaces them.
        d["start_coords"], d["end_coords"] = d["start_location"], d["end_location"]
        d["start_location"], d["start_area"] = _place_and_area(d["start_location"], session)
        d["end_location"], d["end_area"] = _place_and_area(d["end_location"], session)
        _unoverlap_previous(session, vehicle.id, d["start_time"])
        session.add(Drive(vehicle_id=vehicle.id, **d))
        # Webhook-only (not routed through notify()'s push channel) — a
        # push alert per every single drive would be unwanted noise for
        # anyone who already has charge-complete/low-battery push enabled,
        # but a home-automation webhook consumer (arrive-home triggers,
        # trip logging, ...) very much wants this event.
        notifications.fire_webhook(
            "drive-complete", "Drive completed",
            f"{vehicle.name}: {d['distance_km']:.1f} km, {d['duration_min']:.0f} min, "
            f"{d['start_soc']:.0f}% → {d['end_soc']:.0f}%.",
        )
    for c in charges:
        cap = sync_mod.implied_capacity_kwh(c)
        _attach_curve_capacity(c)
        c.pop("energy_measured", None)  # transient flag, not a DB column
        if cap:
            old = vehicle.battery_capacity_kwh or 75.0
            vehicle.battery_capacity_kwh = round(0.8 * old + 0.2 * cap, 1)
        raw_coords = c.get("location", "")
        c["location"] = _place(raw_coords, session)
        # Re-price at the session's own start time and actual charger type —
        # auto-matched Home/Office location (or the saved default source)
        # takes priority; Public falls back to flat/ToU pricing.
        source, rate = pricing_prefs.resolve_source_and_rate(
            session, settings, raw_coords, c["charge_type"] == "DC", c["start_time"])
        c["cost"] = round(c["energy_added_kwh"] * rate, 2)
        c["price_source"] = source
        # A rate of zero IS the free flag on an auto-logged session. Nothing in
        # telemetry distinguishes a Tesla Destination Charger from a paid AC
        # one (see the manual-entry path), so is_free could only ever be set by
        # hand — and an automatic session that priced at zero was therefore
        # stored as a PAID charge costing nothing. The cost came out right and
        # the label came out wrong, which is worse than either: the charging
        # analytics separate free from paid on this flag, so a free session sat
        # in the paid group dragging its average toward zero.
        #
        # Reported live: a Tesla Destination Charger session logged rate 0,
        # cost 0, is_free false. The edit path has always used exactly this
        # rule (charge.is_free = rate == 0); only the automatic one did not.
        c["is_free"] = rate == 0
        session.add(Charge(vehicle_id=vehicle.id, **c))
        notifications.notify(
            session, "Charging complete",
            f"{vehicle.name}: {c['energy_added_kwh']:.1f} kWh added, now at {c['end_soc']:.0f}%.",
            tag="charge-complete",
        )
    if snap["soc"] > 0 and snap.get("range_km", 0) > 0:
        last_reading = session.scalars(
            select(BatteryReading)
            .where(BatteryReading.vehicle_id == vehicle.id)
            .order_by(BatteryReading.ts.desc())
        ).first()
        sentry_now = snap.get("sentry_mode")
        climate_now = snap.get("climate_on")
        cop_now = snap.get("cabin_overheat_protection")
        cop_cooling_now = snap.get("cabin_overheat_protection_actively_cooling")
        dashcam_now = snap.get("dashcam_state")
        display_now = snap.get("center_display_state")
        # Also write a row on a Sentry/climate/COP change even with SoC
        # unmoved — the whole point is catching the state right as the car
        # parks (before it sleeps and this polling stops seeing it), and SoC
        # usually hasn't dropped a full point yet by then. dashcam/display are
        # in this list for the same reason and more so: if a Sentry trigger is
        # visible through either, it's a brief flicker that SoC won't have
        # moved for at all, so keying the write on SoC alone would miss the
        # one sample that mattered.
        state_changed = last_reading is not None and (
            last_reading.sentry_mode != sentry_now or last_reading.climate_on != climate_now
            or last_reading.cabin_overheat_protection != cop_now
            or last_reading.cabin_overheat_protection_actively_cooling != cop_cooling_now
            or last_reading.dashcam_state != dashcam_now
            or last_reading.center_display_state != display_now
        )
        if last_reading is None or abs(last_reading.soc - snap["soc"]) >= 1.0 or state_changed:
            session.add(BatteryReading(
                vehicle_id=vehicle.id,
                ts=datetime.fromtimestamp(snap["ts"], sync_mod.MYT).replace(tzinfo=None),
                soc=snap["soc"],
                range_km=round(snap["range_km"], 1),
                odo_km=round(snap["odo_km"], 1),
                sentry_mode=sentry_now,
                climate_on=climate_now,
                cabin_overheat_protection=cop_now,
                cabin_overheat_protection_actively_cooling=cop_cooling_now,
                dashcam_state=dashcam_now,
                center_display_state=display_now,
            ))
        # Low-battery alert: fires once per low episode (a state.py flag,
        # cleared once SoC recovers past the threshold + a small hysteresis
        # band so it doesn't flicker on/off right at the line), not on every
        # sync tick while it stays low.
        threshold = settings.low_soc_notify_pct
        if threshold > 0:
            notified_key = state.scoped(state.LOW_SOC_NOTIFIED_KEY, vin)
            already_notified = state.get(session, notified_key) == "1"
            if snap["soc"] <= threshold and not already_notified:
                notifications.notify(
                    session, "Battery low",
                    f"{vehicle.name} is at {snap['soc']:.0f}% — time to plug in.",
                    tag="low-soc",
                )
                state.put(session, notified_key, "1")
            elif snap["soc"] > threshold + 5 and already_notified:
                state.put(session, notified_key, "")

        # Live Sentry-drain alert: Sentry Mode keeps the car online, so this
        # poll sees it draining in near-real-time (unlike a truly asleep car).
        # Anchor the SoC when a parked-with-Sentry episode starts, then fire
        # once it's cost at least sentry_drain_notify_pct points — a prompt
        # "turn it off" nudge, not a next-day retrospective. The episode resets
        # whenever the car drives/charges or Sentry goes off (below), so a real
        # short errand with Sentry on never trips it.
        sentry_pct = settings.sentry_drain_notify_pct
        if sentry_pct > 0:
            ep_key = state.scoped(state.SENTRY_DRAIN_EPISODE_KEY, vin)
            sentry_notified_key = state.scoped(state.SENTRY_DRAIN_NOTIFIED_KEY, vin)
            parked_sentry = bool(sentry_now) and not open_trip and not open_charge
            if parked_sentry:
                ep_raw = state.get(session, ep_key)
                if not ep_raw:
                    state.put(session, ep_key, _json.dumps({"soc": snap["soc"]}))
                else:
                    start_soc = _json.loads(ep_raw).get("soc", snap["soc"])
                    drop = start_soc - snap["soc"]
                    if drop >= sentry_pct and state.get(session, sentry_notified_key) != "1":
                        notifications.notify(
                            session, "Sentry Mode is draining your battery",
                            f"{vehicle.name} has lost {drop:.0f}% to Sentry Mode since it "
                            "parked. Turn Sentry off if the car's somewhere safe.",
                            tag="sentry-drain",
                        )
                        state.put(session, sentry_notified_key, "1")
            elif state.get(session, ep_key) or state.get(session, sentry_notified_key) == "1":
                # Drove off, started charging, or Sentry switched off — end the
                # episode so the next parked-with-Sentry stretch is judged fresh.
                state.put(session, ep_key, "")
                state.put(session, sentry_notified_key, "")

        # Parked-intrusion alert: a door, trunk or window opening while the car
        # sits with Sentry armed and nobody aboard. Unlike Sentry's own alarm
        # state — which Tesla's API doesn't expose at all — an opening persists
        # until someone shuts it, so this poll cadence catches it reliably
        # instead of having to land inside a ~1 min alarm window.
        #
        # Deliberately entry-only. There is no accelerometer, tilt or impact
        # field on vehicle_data, so a Sentry trigger from someone touching or
        # leaning on the car cannot be seen here at all; Tesla's own app stays
        # the only alert for those. Named for what it actually detects rather
        # than borrowing "Sentry alert", which would overstate it.
        if settings.intrusion_notify:
            intrusion_key = state.scoped(state.INTRUSION_NOTIFIED_KEY, vin)
            # Armed by Sentry OR simply by being locked — it's the locked
            # state that makes an opening anomalous, not Sentry, and requiring
            # Sentry meant a car parked locked without it was silently
            # unwatched. Widening costs nothing: both flags are already in the
            # payload, and the trip/charge/occupant guards still keep ordinary
            # use quiet.
            armed = (
                (bool(sentry_now) or bool(snap.get("locked")))
                and not open_trip and not open_charge
                and not snap["user_present"]
            )
            opened_doors = bool(snap.get("doors_open"))
            opened_windows = bool(snap.get("windows_open"))
            breached = armed and (opened_doors or opened_windows)
            if breached and state.get(session, intrusion_key) != "1":
                what = "A door or trunk" if opened_doors else "A window"
                notifications.notify(
                    session, "Car opened while parked",
                    f"{what} was opened on {vehicle.name} while it sat parked "
                    f"{'with Sentry Mode on' if sentry_now else 'and locked'} "
                    "with nobody aboard.",
                    tag="intrusion",
                )
                # Persisted as well as pushed. The alert alone left no trace
                # once dismissed, which is why the Sentry-visibility question
                # kept stalling on "when did one actually happen?" — see
                # SecurityEvent. The two fields under test are captured as
                # they read right now, at the moment of the opening.
                session.add(SecurityEvent(
                    vehicle_id=vehicle.id,
                    ts=sync_mod._dt(snap["ts"]),
                    kind="door" if opened_doors else "window",
                    sentry_mode=sentry_now,
                    locked=snap.get("locked"),
                    soc=snap.get("soc"),
                    dashcam_state=snap.get("dashcam_state"),
                    center_display_state=snap.get("center_display_state"),
                ))
                state.put(session, intrusion_key, "1")
            elif not breached and state.get(session, intrusion_key) == "1":
                # Everything shut again (or the car was driven/occupied) — arm
                # the alert for the next separate opening.
                state.put(session, intrusion_key, "")

    session.commit()
    state.put(session, sk, _json.dumps(snap))
    state.put(session, tk, _json.dumps(open_trip) if open_trip else "")
    state.put(session, ck, _json.dumps(open_charge) if open_charge else "")
    return vehicle, snap, len(drives), len(charges), open_trip


@router.get("/sync")  # GET so external cron/uptime services can trigger it
@router.post("/sync")
def sync_now(wake: bool = Query(False), session: Session = Depends(get_session)):
    """Snapshot the linked car and log what happened since the last snapshot.

    ``wake=1`` (the manual Sync button) nudges a sleeping car online first.
    The cron never wakes the car, so it can't drain the battery overnight.
    """
    try:
        return _sync_now_impl(wake, session)
    except HTTPException as exc:
        # Logged too. A tick that returned 401 because the token expired, or
        # 503 because Tesla was unreachable, achieved exactly as little as one
        # that crashed — and left exactly the same absence. Recording only
        # uncaught exceptions would leave the tidiest failures the least
        # visible, which is backwards.
        _log_tick_isolated(f"HTTP {exc.status_code}: {exc.detail}", session)
        raise
    except Exception as exc:
        # A tick that CRASHED and a tick that never happened leave the same
        # absence in the log otherwise, because the recorder further down
        # never runs — and those two have completely different fixes. Learned
        # the hard way: a run of 500s read exactly like a dead cron, and the
        # diagnosis went to the wrong place for two rounds.
        reason = f"{type(exc).__name__}: {exc}"
        _log_tick_isolated(reason, session)
        # And put it in the RESPONSE, not only the log. An uncaught exception
        # returns FastAPI's bare 500 with no body, so the dashboard could only
        # ever say "Sync unavailable (500)" — which is exactly what it said for
        # four rounds while the real error sat in host logs unreachable from a
        # phone. The banner already renders `detail` when there is one, so this
        # puts the fault where the fault is noticed.
        #
        # Type and message only, never a traceback: enough to fix from and
        # nothing more, on an endpoint that is passcode-gated anyway.
        raise HTTPException(500, f"sync failed — {reason}") from exc


def _sync_now_impl(wake: bool, session: Session):
    import time

    from .. import sync as sync_mod
    from ..tesla_client import TeslaClient
    import json as _json

    settings = get_settings()
    token = state.active_token(session)
    if not token:
        raise HTTPException(400, "No linked Tesla account — link your account first.")
    base = state.active_base_url(session)
    active_target = state.active_vin(session)

    now_ts = datetime.now().timestamp()

    def make_client(tok):
        return TeslaClient(access_token=tok, base_url=base)

    def refresh_or_401() -> str:
        refresh = state.get(session, state.REFRESH_KEY)
        if not refresh or not auth.oauth_configured():
            raise HTTPException(401, "Token expired — sign in with Tesla again.")
        try:
            tokens = auth.refresh_tokens(refresh)
        except httpx.HTTPStatusError as exc:
            # The refresh token itself was rejected (expired/revoked) — surface
            # a clean re-auth prompt instead of an unhandled 400/401 from Tesla.
            raise HTTPException(401, "Token expired — sign in with Tesla again.") from exc
        state.put(session, state.TOKEN_KEY, tokens["access_token"])
        if tokens.get("refresh_token"):
            state.put(session, state.REFRESH_KEY, tokens["refresh_token"])
        return tokens["access_token"]

    # Nothing on this account can have changed while every car is asleep, so
    # do not spend a request finding that out. The suspend window is set at the
    # end of a tick that found everything quiet, and cleared the moment
    # anything is not (see settings.sleep_recheck_min).
    #
    # A manual sync ignores it outright — the person pressing the button is
    # the reason the button exists.
    suspend_until = float(state.get(session, state.SUSPEND_KEY) or 0)
    # The back-off is an optimisation, and an optimisation must not be able to
    # hold the loop down on a fault it did not cause. state.put commits
    # immediately, so a tick that re-arms the window and THEN crashes leaves
    # the re-arm behind and nothing else — every following tick skips, the
    # next one to do real work crashes the same way, and the cycle is
    # self-sustaining with no way out.
    #
    # Measured: 353 consecutive skipped ticks over 6.2 hours, an unbroken run
    # with not one check in it, while the car sat unpolled. The suspend window
    # was 20 minutes at the time; it should never have survived one of them.
    #
    # So it only counts while a tick has actually COMPLETED recently. Past
    # that, ignore it and do the work — which either succeeds and clears the
    # condition, or fails and gets logged every tick instead of every
    # twentieth. Noisier, and the only version that recovers by itself.
    last_full = float(state.get(session, state.FULL_TICK_KEY) or 0)
    backoff_trusted = (now_ts - last_full) <= SUSPEND_MAX_QUIET_MIN * 60 if last_full else False
    if not wake and suspend_until and now_ts < suspend_until and backoff_trusted:
        # Logged like any other outcome: this tick DID run, it just chose to
        # spend nothing. Without that the back-off is indistinguishable from
        # the cron having stopped, which is the one thing the log is for.
        _log_tick(session, "backoff")
        last = _json.loads(state.get(
            session, state.scoped(state.LAST_STATUS_KEY, active_target)) or "{}")
        return {
            "status": last.get("status") or "asleep",
            "soc": last.get("soc"), "odo_km": last.get("odo_km"),
            "speed_kmh": last.get("speed_kmh") or 0,
            "trip_in_progress": False,
            "poll_fast": False,
            "skipped": "asleep",
            "next_check_sec": round(suspend_until - now_ts),
            "logged": {"drives": 0, "charges": 0},
        }

    # List every car on the account (with a single token-refresh retry). Tesla
    # returns 401 for an expired access token, but sometimes 403 instead —
    # both are treated as "try refreshing" rather than a hard failure.
    try:
        vehicles = make_client(token).list_vehicles()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            token = refresh_or_401()
            try:
                vehicles = make_client(token).list_vehicles()
            except httpx.HTTPStatusError as exc2:
                raise HTTPException(exc2.response.status_code, f"Tesla error: {exc2}") from exc2
        else:
            raise HTTPException(exc.response.status_code, f"Tesla error: {exc}") from exc
    except httpx.RequestError as exc:
        # Network/timeout reaching Tesla — return a clean 503, never a 500.
        raise HTTPException(503, "Couldn't reach Tesla right now — try again in a moment.") from exc
    if not vehicles:
        raise HTTPException(404, "No vehicles on this Tesla account.")

    # The car the dashboard follows (and the only one the manual button wakes) —
    # keep the current pick if it's still on the account, else default to first.
    account_vins = [vv.get("vin") for vv in vehicles]
    if active_target not in account_vins:
        active_target = account_vins[0]
    state.put(session, state.ACTIVE_VIN_KEY, active_target)
    state.put(session, state.LINKED_VIN_KEY, active_target)

    client = make_client(token)
    total = {"drives": 0, "charges": 0}
    purged = False
    active_snap = active_open_trip = active_vehicle = None
    active_cfg: dict = {}
    active_seen_online = False

    for vv in vehicles:
        vvin = vv.get("vin")
        vid = vv.get("id_s") or vv.get("id")
        vstate = vv.get("state")
        if vvin == active_target and vstate == "online":
            active_seen_online = True
        # Tesla's own reported state, captured before any wake_up() of ours below
        # — the pure signal of whether the car woke up on its own (phone-as-key,
        # a scheduled precondition, remote start) between this poll and the last.
        # list_vehicles() is a cached backend read that never touches the car, so
        # tracking it costs nothing extra regardless of polling frequency.
        raw_vstate = vstate
        vstate_key = state.scoped(state.LAST_VSTATE_KEY, vvin)
        prev_vstate = state.get(session, vstate_key)
        if prev_vstate and prev_vstate != "online" and raw_vstate == "online":
            # It came online without our help — possibly about to drive off.
            # Remember this so the caller can poll tightly for a short bounded
            # window instead of waiting up to a full cron tick to notice.
            state.put(session, state.scoped(state.WOKE_AT_KEY, vvin), str(now_ts))
        state.put(session, vstate_key, raw_vstate or "")

        # Only ever wake the active car, so a multi-car account is never woken
        # (and drained) all at once.
        if wake and vvin == active_target and vstate and vstate != "online":
            try:
                client.wake_up(vid)
                for _ in range(6):  # cars typically wake within ~15-30 s
                    time.sleep(5)
                    fresh = [x for x in client.list_vehicles() if x.get("vin") == vvin]
                    if fresh and fresh[0].get("state") == "online":
                        vstate = "online"
                        break
            except Exception:  # noqa: BLE001 — wake is best-effort
                pass
        if vstate and vstate != "online":
            # A car can only reach true "asleep" while parked and idle — never
            # mid-drive — so it's an immediate, definitive "the trip is over"
            # signal. "offline" is murkier: some accounts/cars report a
            # genuinely-sleeping car as "offline" rather than "asleep", but it
            # can *also* mean a momentary signal gap during an active drive (a
            # tunnel, a dead zone). So "offline" only counts once it's been
            # sustained for UNREACHABLE_CLOSE_MIN straight — long enough that a
            # brief blip would have already recovered, short enough that a
            # short trip still closes promptly rather than waiting hours for
            # the car to happen to wake up again.
            unreachable_key = state.scoped(state.UNREACHABLE_SINCE_KEY, vvin)
            unreachable_since = float(state.get(session, unreachable_key) or 0)
            if not unreachable_since:
                unreachable_since = now_ts
                state.put(session, unreachable_key, str(now_ts))
            sustained_offline = (now_ts - unreachable_since) >= UNREACHABLE_CLOSE_MIN * 60

            if vstate == "asleep" or sustained_offline:
                trip_key = state.scoped(state.OPEN_TRIP_KEY, vvin)
                trip_raw = state.get(session, trip_key)
                charge_key = state.scoped(state.OPEN_CHARGE_KEY, vvin)
                charge_raw = state.get(session, charge_key)
                last_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, vvin))
                vehicle_row = (
                    session.query(Vehicle).filter(Vehicle.vin == vvin).first()
                    if (trip_raw or charge_raw) and last_raw else None
                )
                row_capacity_kwh = _usable_capacity(session, vehicle_row, settings)[0] if vehicle_row else 75.0
                if trip_raw and last_raw and vehicle_row:
                    # Where the car went dark decides the tail, so resolve the
                    # place from the closing reading's own coordinates and ask
                    # what that car park has actually shown.
                    last_snap = _json.loads(last_raw)
                    place_tail = _place_tail_km(
                        session,
                        _place_and_area(sync_mod._coords(last_snap) or "", session)[0])
                    d = sync_mod.close_trip_on_sleep(
                        _json.loads(trip_raw), last_snap,
                        row_capacity_kwh, settings.drive_min_km,
                        place_tail_km=place_tail,
                    )
                    if d:
                        d["start_coords"], d["end_coords"] = d["start_location"], d["end_location"]
                        d["start_location"], d["start_area"] = _place_and_area(d["start_location"], session)
                        d["end_location"], d["end_area"] = _place_and_area(d["end_location"], session)
                        drive_row = Drive(vehicle_id=vehicle_row.id, **d)
                        session.add(drive_row)
                        session.commit()
                        total["drives"] += 1
                        # "sustained offline" (unlike a direct "asleep" report) doesn't
                        # guarantee the car had already stopped moving — a dead zone
                        # right at arrival can close this trip a little short. Mark it
                        # so the next successful poll can extend it instead of logging
                        # the remainder as a disconnected phantom trip (see
                        # state.LAST_SLEEP_CLOSE_KEY).
                        #
                        # Armed for BOTH reports, but recording which one, because
                        # they earn different amounts of trust downstream. An
                        # "asleep" close is anchored at a reading the car had
                        # genuinely stopped moving by — yet not necessarily AT
                        # the stop, since last_snapshot can be a poll interval
                        # old, so a small arrival tail can still be missing
                        # (trip 314). Sustained "offline" is weaker still: it can
                        # fire mid-drive, so movement well past the close can
                        # legitimately belong to the same trip. The reader uses
                        # this to allow a small tail in both cases but the
                        # time-based merge only for offline (see the top-up in
                        # _process_vehicle). Note sustained_offline can't stand in
                        # for this: unreachable_since is armed for anything not
                        # "online", so a car asleep past UNREACHABLE_CLOSE_MIN
                        # sets it too.
                        state.put(
                            session, state.scoped(state.LAST_SLEEP_CLOSE_KEY, vvin),
                            _json.dumps({
                                "drive_id": drive_row.id,
                                "odo_km": _json.loads(last_raw)["odo_km"],
                                "ts": _json.loads(last_raw)["ts"],
                                "reason": "asleep" if vstate == "asleep" else "offline",
                                # Distance already credited to this trip as an
                                # estimate. The blind stretch is one fixed
                                # quantity: whatever is given to the arriving
                                # trip has to be taken off the departing one,
                                # or both claim it. Carried on the marker so
                                # the next poll can do exactly that.
                                "est_km": d.get("end_est_km") or 0.0,
                                # And the clock the estimate moved, which has
                                # to come back with the kilometres or a
                                # corrected trip keeps time it never spent —
                                # duration and average speed both derive from
                                # it. Recomputed rather than carried out of
                                # close_trip_on_sleep because `d` becomes a
                                # Drive row and only real columns may ride in
                                # it; the inputs here are the same ones, so
                                # the answer is the same one.
                                "est_sec": (
                                    (sync_mod.arrival_tail_for_place(place_tail)
                                     or (0.0, 0.0))[1]
                                ),
                                # The speed the estimate was computed from.
                                # Carried purely so the calibration sample can
                                # record it (see ArrivalTailSample): the model
                                # is speed times window, so a pair of estimate
                                # and measurement says nothing about the window
                                # unless the speed is known too. Read here
                                # rather than reconstructed later, because by
                                # the time the measurement arrives this
                                # snapshot is long gone.
                                "speed_kmh": (
                                    _json.loads(last_raw).get("speed_kmh") or 0.0),
                            }),
                        )
                        notifications.fire_webhook(
                            "drive-complete", "Drive completed",
                            f"{vehicle_row.name}: {d['distance_km']:.1f} km, "
                            f"{d['duration_min']:.0f} min, {d['start_soc']:.0f}% → "
                            f"{d['end_soc']:.0f}% (car went offline/asleep).",
                        )
                if trip_raw:
                    state.put(session, trip_key, "")
                # Charging usually keeps the car awake, so this rarely fires —
                # but connectivity can still drop mid-session, and without
                # this an interrupted charge would sit open indefinitely
                # waiting for a reconnect, never logged to Neon at all.
                if charge_raw and last_raw and vehicle_row:
                    c = sync_mod.close_charge_on_sleep(
                        _json.loads(charge_raw), _json.loads(last_raw),
                        row_capacity_kwh, settings.energy_price_per_kwh, settings.drive_min_km,
                    )
                    if c:
                        cap = sync_mod.implied_capacity_kwh(c)
                        _attach_curve_capacity(c)
                        c.pop("energy_measured", None)
                        if cap:
                            old_cap = vehicle_row.battery_capacity_kwh or 75.0
                            vehicle_row.battery_capacity_kwh = round(0.8 * old_cap + 0.2 * cap, 1)
                        raw_coords = c.get("location", "")
                        c["location"] = _place(raw_coords, session)
                        source, rate = pricing_prefs.resolve_source_and_rate(
                            session, settings, raw_coords, c["charge_type"] == "DC", c["start_time"])
                        c["cost"] = round(c["energy_added_kwh"] * rate, 2)
                        c["price_source"] = source
                        c["is_free"] = rate == 0   # see the same rule above
                        session.add(Charge(vehicle_id=vehicle_row.id, **c))
                        session.commit()
                        total["charges"] += 1
                        # Closed via the car going offline/asleep rather than a
                        # clean "stopped charging" reading — could be complete
                        # or genuinely interrupted, so the message stays neutral.
                        notifications.notify(
                            session, "Charging session ended",
                            f"{vehicle_row.name}: {c['energy_added_kwh']:.1f} kWh added, "
                            f"now at {c['end_soc']:.0f}% (car went offline/asleep).",
                            tag="charge-complete",
                        )
                if charge_raw:
                    state.put(session, charge_key, "")
            continue  # asleep/offline — nothing readable right now

        # Back online — this unreachable episode (if any) is over; the next
        # one starts its own fresh clock.
        state.put(session, state.scoped(state.UNREACHABLE_SINCE_KEY, vvin), "")

        # The car is online, but that alone isn't reason enough to read it —
        # it may just not have fallen asleep yet from something unrelated to
        # us. Only actually call vehicle_data() (the read that resets Tesla's
        # own sleep countdown) when there's a concrete reason to: a trip or
        # charge is already open (need to track it live), it woke up
        # unprompted within the escalation window (may be about to drive
        # off), the normal base interval has elapsed anyway, or this is the
        # user's own manual sync.
        poll_key = state.scoped(state.LAST_POLL_KEY, vvin)
        last_poll_ts = float(state.get(session, poll_key) or 0)
        woke_at = float(state.get(session, state.scoped(state.WOKE_AT_KEY, vvin)) or 0)
        recently_woke = bool(woke_at) and (now_ts - woke_at) <= FAST_POLL_WINDOW_MIN * 60
        due = (now_ts - last_poll_ts) >= settings.sync_poll_interval_min * 60
        trip_in_progress = bool(state.get(session, state.scoped(state.OPEN_TRIP_KEY, vvin)))
        charge_in_progress = bool(state.get(session, state.scoped(state.OPEN_CHARGE_KEY, vvin)))
        manual_sync = wake and vvin == active_target
        if not (trip_in_progress or charge_in_progress or recently_woke or due or manual_sync):
            continue  # online but idle, not due yet — let it settle toward sleep
        state.put(session, poll_key, str(now_ts))

        try:
            data = client.vehicle_data(vid)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                token = refresh_or_401()
                client = make_client(token)
                try:
                    data = client.vehicle_data(vid)
                except (httpx.HTTPStatusError, httpx.RequestError):
                    continue
            elif code == 408:
                continue  # fell asleep between the list and the read
            else:
                raise HTTPException(code, f"Tesla error: {exc}") from exc
        except httpx.RequestError:
            continue  # transient network error for this car — skip it this round

        if not purged:
            services.purge_demo(session)  # retire the seeded sample on first real data
            purged = True
        vehicle, snap, nd, nc, open_trip = _process_vehicle(
            session, data, vv, settings, migrate_legacy=(vvin == active_target)
        )
        total["drives"] += nd
        total["charges"] += nc
        if vvin == active_target:
            active_snap, active_open_trip, active_vehicle = snap, open_trip, vehicle
            active_cfg = data.get("vehicle_config") or {}

    state.put(session, state.SOURCE_KEY, "linked")

    # Arm or disarm the sleep back-off for the next tick. Quiet means every
    # car on the account read as not online AND none has a trip or charge
    # open — the two things that could still be moving underneath us. Any
    # doubt clears the window rather than setting it: a missed departure
    # costs boundary precision, and being wrong in that direction is the one
    # this whole week was spent undoing.
    # A pending LAST_SLEEP_CLOSE_KEY is deliberately NOT counted. It looks like
    # unfinished business — an arrival waiting to be measured — but the poll it
    # waits for needs vehicle_data, and a sleeping car is never read. Treating
    # it as a reason to keep listing would hold the back-off off for exactly
    # the hours it exists for: a car parked overnight carries that marker the
    # whole time, and no amount of listing can act on it.
    any_open = any(
        state.get(session, state.scoped(k, v.get("vin")))
        for v in vehicles for k in (state.OPEN_TRIP_KEY, state.OPEN_CHARGE_KEY)
    )
    all_quiet = bool(vehicles) and not any_open and not any(
        v.get("state") == "online" for v in vehicles)
    state.put(session, state.SUSPEND_KEY,
              str(now_ts + settings.sleep_recheck_min * 60.0) if all_quiet else "")
    # Record that we LOOKED and found the car still, not merely that we intend
    # to look again. list_vehicles reporting no car online is proof of absence
    # of movement: a driving car is online. So each of these stamps closes the
    # window in which an unseen departure could have started, and the departure
    # recovery reads it to tell a rechecked overnight park from a blackout.
    if all_quiet:
        state.put(session, state.QUIET_SEEN_KEY, str(now_ts))

    # What this tick did for the car the dashboard follows. Placed before both
    # return paths below so every tick that reaches here is recorded exactly
    # once. "idle" is the throttle declining to read an online car; "asleep"
    # is the car being unreadable; "read" is a snapshot actually taken.
    _log_tick(session, "read" if active_snap is not None
              else ("idle" if active_seen_online else "asleep"))

    # The dashboard's live status reflects the active car specifically.
    if active_snap is None:
        # Distinguish "genuinely asleep/offline" from "online, but this tick
        # deliberately skipped reading it" (the poll-throttle above) — telling
        # a user their online car is "asleep" would be actively misleading.
        resp = {
            "status": "asleep" if not active_seen_online else "parked",
            "tried_wake": wake,
            "logged": total,
            "poll_fast": False,
            "note": ("Couldn't wake the car — it may be offline. Try again in a minute."
                     if wake else
                     "Car is asleep — try again while charging or right after a drive."
                     if not active_seen_online else
                     "Car is online but idle — skipping this read to let it settle to "
                     "sleep. It'll be read again shortly if that changes."),
        }
        last_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, active_target))
        if last_raw:
            last = _json.loads(last_raw)
            resp["last"] = {"soc": last.get("soc"), "ts": last.get("ts"),
                            "odo_km": round(last.get("odo_km", 0), 1)}
        _save_last_status(
            session, active_target, status=resp["status"], ts=now_ts,
            soc=resp.get("last", {}).get("soc"), odo_km=resp.get("last", {}).get("odo_km"),
            speed_kmh=None, note=resp["note"],
        )
        _mark_full_tick(session, now_ts)
        return resp

    snap, open_trip, vehicle = active_snap, active_open_trip, active_vehicle
    if snap["charging"]:
        activity = "charging"
    elif sync_mod.is_driving(snap):
        activity = "driving"
    elif open_trip:
        activity = "stopped"  # trip still open — parked briefly, driver present
    else:
        activity = "parked"

    # Tell the caller (the sync cron) whether it's worth polling again soon
    # instead of waiting for the next scheduled tick: a trip is actively in
    # progress, or the car just woke up on its own within the last few
    # minutes and may be about to drive off. Bounded so an online-but-idle
    # car isn't kept awake indefinitely — once the window lapses (or it goes
    # back to sleep) this drops to False and the normal cadence takes over.
    woke_at = float(state.get(session, state.scoped(state.WOKE_AT_KEY, active_target)) or 0)
    recently_woke = bool(woke_at) and (now_ts - woke_at) <= FAST_POLL_WINDOW_MIN * 60
    # Arrival is the one moment this most needs a prompt reading, and it was
    # exactly when polling used to slow down: the instant the car stops,
    # is_driving goes false, activity becomes "stopped", and the cadence
    # dropped back to the idle tick — leaving the trip's stop anchored at
    # whatever reading happened to be last. That is the direct cause of a
    # clipped arrival tail (trip 314 lost 0.4 km) and of the pace-based trim
    # having to reach 1002 s to undo a 17-minute-late reading (trip 316).
    #
    # So keep the tight cadence for a short settle window after the car first
    # reads stopped with a trip still open — long enough to catch the final
    # creep into a parking space and anchor the stop where the car actually
    # came to rest. Bounded deliberately: a trip stays open for PARK_END_MIN
    # after stopping, and polling hard for all of it would spend the Fleet
    # API budget this cadence exists to protect. A few readings settle the
    # anchor; the remaining wait does not need them.
    # stop_at is set to None outright while the car is moving, so the key
    # existing says nothing — `or {}` rather than a default argument.
    stop_ts = ((open_trip or {}).get("stop_at") or {}).get("ts")
    settling = bool(stop_ts) and (now_ts - stop_ts) <= ARRIVAL_SETTLE_MIN * 60
    poll_fast = activity == "driving" or settling or recently_woke

    _save_last_status(
        session, active_target, status=activity, ts=now_ts,
        soc=snap["soc"], odo_km=round(snap["odo_km"], 1),
        speed_kmh=round(snap.get("speed_kmh") or 0.0), note=None,
    )
    _mark_full_tick(session, now_ts)
    return {
        "status": activity,
        "soc": snap["soc"],
        "odo_km": round(snap["odo_km"], 1),
        "speed_kmh": round(snap.get("speed_kmh") or 0.0),
        "trip_in_progress": bool(open_trip),
        "poll_fast": poll_fast,
        # Tells the dashboard whether the token really has location access —
        # the 403 fallback makes a missing scope otherwise invisible.
        "location_access": snap.get("lat") is not None,
        # Config the active car reported this sync — makes wheel detection auditable.
        "wheel_type": active_cfg.get("wheel_type") or None,
        "trim": vehicle.trim,
        "logged": total,
    }


@router.post("/link/refresh")
def refresh_link(session: Session = Depends(get_session)):
    """Mint a fresh access token from the stored refresh token (OAuth links)."""
    refresh = state.get(session, state.REFRESH_KEY)
    if not refresh:
        raise HTTPException(400, "No refresh token stored — link the account first.")
    try:
        tokens = auth.refresh_tokens(refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(401, f"Token refresh failed: {exc}") from exc
    state.put(session, state.TOKEN_KEY, tokens["access_token"])
    if tokens.get("refresh_token"):
        state.put(session, state.REFRESH_KEY, tokens["refresh_token"])
    return {"status": "refreshed"}


@router.get("/vehicles", response_model=list[VehicleOut])
def list_vehicles(session: Session = Depends(get_session)):
    return session.scalars(select(Vehicle).order_by(Vehicle.id)).all()


@router.get("/compare")
def compare_vehicles(days: int = Query(30, ge=1, le=730), session: Session = Depends(get_session)):
    """One summary row per real (non-demo/import) car on the account, over
    the same window — a household with more than one Tesla can see at a
    glance which car is driven more, costs more to run, or is degrading
    faster, without switching the active car back and forth."""
    settings = get_settings()
    vehicles = session.scalars(
        select(Vehicle).where(~Vehicle.vin.startswith("DEMO"), ~Vehicle.vin.startswith("IMPORT"))
        .order_by(Vehicle.id)
    ).all()

    rows = []
    for vehicle in vehicles:
        capacity_kwh, _ = _usable_capacity(session, vehicle, settings)
        drives, charges = _window(session, vehicle.id, days)
        driving = driving_analysis.analyze(
            drives, settings.rated_wh_per_km, capacity_kwh,
            tariff.price_fn_from_settings(settings), charges=charges,
            trip_costs=_trip_cost_map(session, vehicle.id))
        charging = charging_analysis.analyze(charges, drives)
        readings = _newest_readings(
            session, vehicle.id,
            (BatteryReading.soc, BatteryReading.range_km,
             BatteryReading.ts, BatteryReading.odo_km))
        vin_info = vin_mod.decode(vehicle.vin)
        spec_km = settings.battery_new_range_km or battery_analysis.new_range_for(
            vehicle.model, vehicle.trim, year=vin_info.get("year"))
        battery = battery_analysis.analyze(
            [{"soc": soc, "range_km": rng, "ts": ts, "odo_km": odo}
             for soc, rng, ts, odo in readings],
            new_range_km=spec_km,
        )
        rows.append({
            "vin": vehicle.vin,
            "name": vehicle.name,
            "model": vehicle.model,
            "distance_km": driving.get("total_distance_km") if driving.get("available") else 0.0,
            "drives": driving.get("total_drives") if driving.get("available") else 0,
            "avg_wh_per_km": driving.get("avg_efficiency_wh_per_km") if driving.get("available") else None,
            "driving_cost": driving.get("total_cost") if driving.get("available") else None,
            "cost_per_km": driving.get("cost_per_km") if driving.get("available") else None,
            "charging_cost": charging.get("total_cost") if charging.get("available") else None,
            "energy_charged_kwh": charging.get("total_energy_kwh") if charging.get("available") else None,
            "health_pct": battery.get("health_pct") if battery.get("available") else None,
            "vs_fleet_pct": battery.get("vs_fleet_pct") if battery.get("available") else None,
        })
    return {"window_days": days, "currency": settings.currency, "vehicles": rows}


@router.post("/unlink")
def unlink_account(session: Session = Depends(get_session)):
    """Disconnect the linked Tesla account so a different one can be linked
    (keeps the logged history)."""
    return services.unlink(session)


@router.post("/active-vehicle")
def set_active_vehicle(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Pick which linked car the dashboard follows (multi-car accounts)."""
    vin = (payload.get("vin") or "").strip()
    vehicle = session.query(Vehicle).filter(Vehicle.vin == vin).first()
    if vehicle is None:
        raise HTTPException(404, "Unknown vehicle.")
    state.put(session, state.ACTIVE_VIN_KEY, vin)
    state.put(session, state.LINKED_VIN_KEY, vin)
    return {"status": "ok", "active_vin": vin, "name": vehicle.name}


@router.get("/drives", response_model=list[DriveOut])
def list_drives(
    days: int = Query(30, ge=1, le=730),
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    vehicle = _first_vehicle(session)
    drives, _ = _window(session, vehicle.id, days)
    return drives[-limit:]


@router.get("/charges", response_model=list[ChargeOut])
def list_charges(
    days: int = Query(30, ge=1, le=730),
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    vehicle = _first_vehicle(session)
    _, charges = _window(session, vehicle.id, days)
    return charges[-limit:]


@router.post("/charges/manual")
def add_manual_charge(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Log a charging session by hand — for a session the sync loop never
    saw (missed before the account was linked, or dropped by a bug that's
    since been fixed) and has no live snapshot data left to reconstruct
    from. Purely additive: inserts one Charge row for the active vehicle
    and touches nothing else, unlike /api/import (which replaces the
    entire dataset).
    """
    settings = get_settings()
    try:
        start_time = datetime.fromisoformat(payload["start_time"])
        end_time = datetime.fromisoformat(payload["end_time"])
    except (KeyError, ValueError):
        raise HTTPException(400, "Missing or invalid 'start_time'/'end_time' (expected ISO format).")
    if end_time <= start_time:
        raise HTTPException(400, "'end_time' must be after 'start_time'.")
    try:
        energy_added_kwh = float(payload["energy_added_kwh"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Missing or invalid 'energy_added_kwh'.")
    if energy_added_kwh <= 0:
        raise HTTPException(400, "'energy_added_kwh' must be greater than 0.")

    charge_type = str(payload.get("charge_type") or "AC").upper()
    if charge_type not in ("AC", "DC"):
        raise HTTPException(400, "'charge_type' must be 'AC' or 'DC'.")
    try:
        start_soc = float(payload.get("start_soc") or 0.0)
        end_soc = float(payload.get("end_soc") or 0.0)
        max_power_kw = float(payload.get("max_power_kw") or 0.0)
        outside_temp_c = float(payload.get("outside_temp_c") or 20.0)
        cost = payload.get("cost")
        cost = float(cost) if cost not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid numeric field.")
    location = str(payload.get("location") or "")[:120]
    # No telemetry field reliably marks a free session (e.g. a Tesla
    # Destination Charger) apart from a paid AC charger, so this is a
    # manual flag rather than something auto-detected.
    is_free = bool(payload.get("is_free"))

    # A source is only recorded when the rate was auto-resolved — a free
    # session or one given an explicit cost isn't tied to any of the three
    # presets, so the dashboard falls back to guessing from location text.
    price_source = ""
    if is_free:
        cost = 0.0
    elif cost is None:
        price_source, rate = pricing_prefs.resolve_source_and_rate(
            session, settings, location, charge_type == "DC", start_time)
        cost = round(energy_added_kwh * rate, 2)

    vehicle = _first_vehicle(session)
    charge = Charge(
        vehicle_id=vehicle.id, start_time=start_time, end_time=end_time,
        duration_min=round((end_time - start_time).total_seconds() / 60.0, 1),
        start_soc=start_soc, end_soc=end_soc, energy_added_kwh=round(energy_added_kwh, 2),
        charge_type=charge_type, max_power_kw=max_power_kw, location=location,
        cost=cost, outside_temp_c=outside_temp_c, is_free=is_free, price_source=price_source,
    )
    session.add(charge)
    session.commit()
    return {"id": charge.id}


@router.post("/charges/edit-rate")
def edit_charge_rate(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Recalculate one charging session's cost from a per-kWh rate you
    supply — for a session priced at something other than the configured
    AC/DC default (a promo rate, a pricier one-off public charger, ...).
    0 doubles as marking the session free.

    An optional 'source' (from the dashboard's 🌐/🏠/🏢/🏷️ quick-rate
    buttons — Public/Home/Office/Others) is persisted so the row's
    selected-icon indicator can show it later, and keeps showing it even
    after rates change, unlike re-deriving it from the numbers each time.
    """
    charge_id = payload.get("id")
    if not isinstance(charge_id, int):
        raise HTTPException(400, "Missing or invalid 'id'.")
    try:
        rate = float(payload["price_per_kwh"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Missing or invalid 'price_per_kwh'.")
    if rate < 0:
        raise HTTPException(400, "'price_per_kwh' must be >= 0.")
    source = payload.get("source") or ""
    if source and source not in pricing_prefs.EDIT_SOURCES:
        raise HTTPException(400, "'source' must be 'public', 'home', 'office', or 'other'.")

    charge = session.get(Charge, charge_id)
    if charge is None:
        raise HTTPException(404, "Charge not found.")
    charge.cost = round(charge.energy_added_kwh * rate, 2)
    charge.is_free = rate == 0
    charge.price_source = source
    session.commit()
    return {
        "id": charge.id, "cost": charge.cost, "is_free": charge.is_free,
        "source": charge.price_source or None,
    }


@router.post("/charges/edit-location")
def edit_charge_location(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Rename one charging session's location label.

    A charge is labeled with whatever the geocoder resolved when it was
    logged — sometimes the neighbouring shop rather than the charger, and
    just "lat, lon" when nothing resolved at all. This renames it by hand.

    With 'apply_all', every other session of the same vehicle carrying the
    *same stored label* is renamed too, so naming one charger fixes its
    whole history in one go. Matching is on the stored label, not the one on
    screen: a charge whose card shows a name inferred from a nearby trip has
    no stored label of its own, and a blank label isn't treated as something
    sessions have in common, so apply_all can't sweep up unrelated rows.

    A name is required — clearing one back to blank would throw away the
    raw coordinates, which are the only geographic record a Charge keeps.

    Cost and price source are left alone: what a place is called doesn't
    change what the session was billed at.
    """
    charge_id = payload.get("id")
    if not isinstance(charge_id, int):
        raise HTTPException(400, "Missing or invalid 'id'.")
    name = str(payload.get("location") or "").strip()[:120]
    if not name:
        raise HTTPException(400, "'location' must not be empty.")

    charge = session.get(Charge, charge_id)
    if charge is None:
        raise HTTPException(404, "Charge not found.")

    old = charge.location or ""
    charge.location = name
    updated = 1
    if payload.get("apply_all") and old and old != name:
        others = session.scalars(
            select(Charge).where(
                Charge.vehicle_id == charge.vehicle_id,
                Charge.location == old,
                Charge.id != charge.id,
            )
        ).all()
        for other in others:
            other.location = name
        updated += len(others)
    session.commit()
    return {"id": charge.id, "location": charge.location, "updated": updated}


@router.get("/pricing-prefs")
def get_pricing_prefs(session: Session = Depends(get_session)):
    """Current Public/Home/Office AC+DC rates and which source new charges
    default to — the Rates page reads this to populate its form."""
    settings = get_settings()
    return {
        "rates": pricing_prefs.get_rates(session, settings),
        "default_source": pricing_prefs.get_default_source(session),
        "match_radius_km": pricing_prefs.HOME_OFFICE_MATCH_RADIUS_KM,
        "updated_at": pricing_prefs.get_updated_at(session),
    }


@router.post("/pricing-prefs")
def save_pricing_prefs(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Save the Rates page. Only affects charges priced from now on — never
    retroactive (use the ✎ edit-rate button on a session to fix one already
    logged)."""
    raw_rates = payload.get("rates") or {}
    if not isinstance(raw_rates, dict):
        raise HTTPException(400, "'rates' must be an object.")
    rates: dict[str, float] = {}
    for key in ("public_ac", "public_dc", "home_ac", "home_dc", "office_ac", "office_dc"):
        if key not in raw_rates or raw_rates[key] in (None, ""):
            continue
        try:
            value = float(raw_rates[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{key}' must be a number.")
        if value < 0:
            raise HTTPException(400, f"'{key}' must be >= 0.")
        rates[key] = value

    default_source = str(payload.get("default_source") or "public")
    if default_source not in pricing_prefs.SOURCES:
        raise HTTPException(400, "'default_source' must be 'public', 'home', or 'office'.")

    pricing_prefs.save(session, rates, default_source)
    settings = get_settings()
    return {
        "rates": pricing_prefs.get_rates(session, settings),
        "default_source": pricing_prefs.get_default_source(session),
        "updated_at": pricing_prefs.get_updated_at(session),
    }


@router.get("/export")
def export_data(
    days: int = Query(730, ge=1, le=3650), session: Session = Depends(get_session)
):
    """Export stored drives & charges as JSON (re-importable via /api/import)."""
    vehicle = _first_vehicle(session)
    drives, charges = _window(session, vehicle.id, days)
    return {
        "vehicle": VehicleOut.model_validate(vehicle).model_dump(),
        "drives": [DriveOut.model_validate(d).model_dump() for d in drives],
        "charges": [ChargeOut.model_validate(c).model_dump() for c in charges],
    }


def _csv_text(headers, rows) -> str:
    """One CSV sheet as text — shared by the ZIP export and the per-section
    downloads so both quote/escape identically."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue()


def _build_export_zip(drives: list[Drive], charges: list[Charge],
                      extras: dict[str, str] | None = None) -> bytes:
    """The drives.csv + charges.csv ZIP bytes, shared by the download
    endpoint and the backup webhook so both produce an identically
    re-importable archive.

    ``extras`` adds further ``{filename: csv_text}`` sheets alongside those
    two — the dashboard's full export uses it to ship the analysis sections
    (battery readings, per-section breakdowns, ...) in the same archive,
    while the backup webhook keeps to the lean re-importable pair. Callers
    put those under ``analysis/``, which the importer skips (see
    importer._is_junk) so re-importing this ZIP restores the raw rows without
    the derived sheets double-counting them.
    """
    import io
    import zipfile

    sheet = _csv_text
    ts = lambda t: t.isoformat(sep=" ", timespec="minutes")  # noqa: E731
    drives_csv = sheet(
        # start/end_coords are the raw fixes each trip's boundary was actually
        # recorded at, kept alongside the resolved names — without them an
        # export can't answer "did this trip really start where it says", which
        # is the only way to tell a wrong *label* from a genuinely misplaced
        # split point between two consecutive trips.
        ["start_time", "end_time", "distance_km", "duration_min", "start_soc",
         "end_soc", "energy_used_kwh", "avg_speed_kmh", "max_speed_kmh",
         "outside_temp_c", "start_location", "end_location",
         "start_coords", "end_coords"],
        [[ts(d.start_time), ts(d.end_time), d.distance_km, d.duration_min,
          d.start_soc, d.end_soc, d.energy_used_kwh, d.avg_speed_kmh,
          d.max_speed_kmh, d.outside_temp_c, d.start_location, d.end_location,
          d.start_coords, d.end_coords]
         for d in drives],
    )
    charges_csv = sheet(
        ["start_time", "end_time", "duration_min", "start_soc", "end_soc",
         "energy_added_kwh", "charge_type", "max_power_kw", "location",
         "cost", "outside_temp_c"],
        [[ts(c.start_time), ts(c.end_time), c.duration_min, c.start_soc,
          c.end_soc, c.energy_added_kwh, c.charge_type, c.max_power_kw,
          c.location, c.cost, c.outside_temp_c] for c in charges],
    )

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("drives.csv", drives_csv)
        z.writestr("charges.csv", charges_csv)
        for fname, text in (extras or {}).items():
            z.writestr(fname, text)
    return zbuf.getvalue()


# Every exportable dashboard section: key -> (filename stem, human label).
# The per-section download buttons and the full export's extra sheets both
# read from this one table, so a section can never appear in one and not the
# other.
EXPORT_SECTIONS: dict[str, tuple[str, str]] = {
    # Stems must not collide with the raw drives.csv / charges.csv sheets the
    # ZIP always carries — these are the analysed views, not the raw tables.
    "kpis": ("summary-kpis", "Summary KPIs"),
    "trips": ("recent-trips", "Recent trips"),
    "charges": ("recent-charges", "Recent charges"),
    "routes": ("top-routes", "Top routes"),
    "speed": ("distance-by-speed", "Distance by speed band"),
    "hours": ("trips-by-hour", "Trips by hour"),
    "efficiency": ("efficiency-by-temp", "Efficiency vs temperature"),
    "eff-trend": ("efficiency-trend", "Efficiency trend"),
    "charging": ("charging-ac-dc", "Charging AC vs DC"),
    "soc-target": ("charge-targets", "Charge target (end SoC)"),
    "battery": ("battery-health", "Battery health"),
    "tips": ("recommendations", "Assessment & recommendations"),
}


def _section_csv(name: str, data: dict) -> str | None:
    """One dashboard section rendered as CSV, built from the same ``/api/summary``
    payload the dashboard draws from — so an export always matches exactly what
    was on screen for that window. None for an unknown section."""
    drv = data.get("driving") or {}
    chg = data.get("charging") or {}
    eff = data.get("efficiency") or {}
    bat = data.get("battery") or {}
    cur = data.get("currency") or ""

    if name == "kpis":
        rows = [
            ["Window", data.get("window_label") or f"{data.get('window_days')} days"],
            ["Distance (km)", drv.get("total_distance_km")],
            ["Drives", drv.get("total_drives")],
            ["Avg efficiency (Wh/km)", eff.get("avg_efficiency_wh_per_km")],
            ["Energy used (kWh)", drv.get("total_energy_used_kwh")],
            ["Avg speed (km/h)", drv.get("avg_speed_kmh")],
            [f"Driving cost ({cur})", drv.get("total_cost")],
            [f"Cost per km ({cur})", drv.get("cost_per_km")],
            ["Eco score", drv.get("eco_score")],
            ["Eco grade", drv.get("eco_grade")],
            ["Energy charged (kWh)", chg.get("total_energy_kwh")],
            [f"Charging cost ({cur})", chg.get("total_cost")],
            ["Battery health (%)", bat.get("health_pct")],
            ["Degradation (%)", bat.get("degradation_pct")],
        ]
        return _csv_text(["metric", "value"], [r for r in rows if r[1] is not None])

    if name == "trips":
        trips = drv.get("recent_trips") or []
        return _csv_text(
            ["start_time", "end_time", "route", "distance_km", "duration_min",
             "avg_speed_kmh", "max_speed_kmh", "energy_kwh", "wh_per_km",
             "soc_used_pct", "cost", "tag", "conditions", "eco_score"],
            [[t.get("start_time"), t.get("end_time"), t.get("route"),
              t.get("distance_km"), t.get("duration_min"), t.get("avg_speed_kmh"),
              t.get("max_speed_kmh"), t.get("energy_kwh"), t.get("wh_per_km"),
              t.get("soc_used_pct"), t.get("cost"), t.get("tag"),
              t.get("conditions"), t.get("eco_score")] for t in trips])

    if name == "charges":
        rows = chg.get("recent_charges") or []
        return _csv_text(
            ["start_time", "end_time", "location", "charge_type", "energy_added_kwh",
             "duration_min", "start_soc", "end_soc", "max_power_kw", "cost",
             "rate_per_kwh"],
            [[c.get("start_time"), c.get("end_time"), c.get("location"),
              c.get("charge_type"), c.get("energy_added_kwh"), c.get("duration_min"),
              c.get("start_soc"), c.get("end_soc"), c.get("max_power_kw"),
              c.get("cost"), c.get("rate_per_kwh")] for c in rows])

    if name == "routes":
        return _csv_text(["route", "trips"],
                         [[r[0], r[1]] for r in (drv.get("top_routes") or [])])

    if name == "speed":
        band = drv.get("distance_by_speed_band") or {}
        return _csv_text(["speed_band_kmh", "distance_km"], list(band.items()))

    if name == "hours":
        return _csv_text(["hour", "trips"],
                         list((drv.get("trips_by_hour") or {}).items()))

    if name == "efficiency":
        by_temp = eff.get("efficiency_by_temp") or {}
        return _csv_text(
            ["temp_band_c", "wh_per_km", "trips", "avg_speed_kmh"],
            [[k, v.get("wh_per_km"), v.get("n"), v.get("avg_speed_kmh")]
             for k, v in by_temp.items()])

    if name == "eff-trend":
        weekly = eff.get("weekly_efficiency") or {}
        daily = eff.get("daily_efficiency") or {}
        rows = [["weekly", k, v] for k, v in weekly.items()]
        rows += [["daily", k, v] for k, v in daily.items()]
        return _csv_text(["series", "period", "wh_per_km"], rows)

    if name == "charging":
        rows = [
            ["AC energy (kWh)", chg.get("ac_energy_kwh")],
            ["DC energy (kWh)", chg.get("dc_energy_kwh")],
            ["DC share (%)", chg.get("dc_energy_share_pct")],
            [f"AC cost ({cur})", chg.get("ac_cost")],
            [f"DC cost ({cur})", chg.get("dc_cost")],
            ["Sessions", chg.get("total_sessions")],
            [f"Avg cost per kWh ({cur})", chg.get("avg_cost_per_kwh")],
        ]
        rows = [r for r in rows if r[1] is not None]
        rows += [[f"Charges started at {h}:00", n]
                 for h, n in (chg.get("charges_by_hour") or {}).items() if n]
        return _csv_text(["metric", "value"], rows)

    if name == "soc-target":
        return _csv_text(["end_soc_pct", "charges"],
                         list((chg.get("end_soc_targets") or {}).items()))

    if name == "battery":
        rows = [
            ["Health (%)", bat.get("health_pct")],
            ["Degradation (%)", bat.get("degradation_pct")],
            ["Estimated full range (km)", bat.get("est_full_range_km")],
            ["Reference (km)", bat.get("reference_km")],
            ["Reference basis", bat.get("reference")],
            ["Readings", bat.get("n_readings")],
            ["Odometer (km)", bat.get("current_odo_km")],
            ["Fleet degradation (%)", bat.get("fleet_degradation_pct")],
            ["vs fleet (pt)", bat.get("vs_fleet_pct")],
        ]
        rows = [r for r in rows if r[1] is not None]
        rows += [[f"Trend {p.get('month')} (km)", p.get("full_range_km")]
                 for p in (bat.get("trend") or [])]
        return _csv_text(["metric", "value"], rows)

    if name == "tips":
        recs = (data.get("assessment") or {}).get("recommendations") \
            or data.get("recommendations") or []
        return _csv_text(
            ["priority", "category", "title", "detail", "estimated_saving",
             "saving_kwh", "saving_cost"],
            [[r.get("priority"), r.get("category"), r.get("title"), r.get("detail"),
              r.get("estimated_saving"), r.get("saving_kwh"), r.get("saving_cost")]
             for r in recs])

    return None


@router.get("/export/csv")
def export_csv(
    days: int = Query(3650, ge=1, le=3650),
    since_charge: bool = Query(False),
    current_drive: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Download the whole dashboard as a ZIP of CSVs.

    drives.csv + charges.csv are the raw, re-importable pair; alongside them
    every analysis section the dashboard shows (KPIs, top routes, speed bands,
    efficiency, charging, battery health, recommendations, ...) is written as
    its own sheet, so the archive is a complete extract rather than just the
    two raw tables. Defaults to everything; pass ``days``, ``since_charge`` or
    ``current_drive`` to export only the currently viewed window.
    """
    import json as _json

    from fastapi.responses import Response

    vehicle = _first_vehicle(session)
    since = None
    label = "all"
    if current_drive:
        open_trip = _json.loads(
            state.get(session, state.scoped(state.OPEN_TRIP_KEY, vehicle.vin)) or "null")
        if open_trip:
            from .. import sync as sync_mod

            since = datetime.fromtimestamp(open_trip["ts"], sync_mod.MYT).replace(tzinfo=None)
        else:
            since = session.scalar(
                select(func.max(Drive.start_time)).where(Drive.vehicle_id == vehicle.id)
            )
        if since is not None:
            label = "current-drive"
    elif since_charge:
        last_end = session.scalar(
            select(func.max(Charge.end_time)).where(Charge.vehicle_id == vehicle.id)
        )
        if last_end is not None:
            since = last_end
            label = "since-charge"
    elif days < 3650:
        label = f"{days}d"
    drives, charges = _window(session, vehicle.id, days, since=since)

    # Every analysis section as its own sheet, from the same payload the
    # dashboard renders — so the archive matches what was on screen.
    extras: dict[str, str] = {}
    try:
        data = summary(days=days, since_charge=since_charge,
                       current_drive=current_drive, trips_limit=500, session=session)
        for key, (stem, _label) in EXPORT_SECTIONS.items():
            text = _section_csv(key, data)
            if text:
                # Under analysis/ so the importer ignores them on re-import.
                extras[f"analysis/{stem}.csv"] = text
    except Exception:  # noqa: BLE001 — a section failing must never block the
        # raw drives/charges export, which is the part people re-import.
        extras = {}

    zip_bytes = _build_export_zip(drives, charges, extras)
    name = f"tesla-analyzer-{vehicle.vin[-6:]}-{label}.zip"
    return Response(
        zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/export/section")
def export_section(
    name: str = Query(..., min_length=1),
    days: int = Query(90, ge=1, le=730),
    since_charge: bool = Query(False),
    current_drive: bool = Query(False),
    session: Session = Depends(get_session),
):
    """One dashboard section as a single CSV — the per-card download buttons.

    Uses the same window parameters as /api/summary so a section exports
    exactly the rows currently on screen.
    """
    from fastapi.responses import Response

    if name not in EXPORT_SECTIONS:
        raise HTTPException(404, f"Unknown export section “{name}”.")
    data = summary(days=days, since_charge=since_charge,
                   current_drive=current_drive, trips_limit=500, session=session)
    text = _section_csv(name, data)
    if text is None:
        raise HTTPException(404, f"Unknown export section “{name}”.")
    stem = EXPORT_SECTIONS[name][0]
    vehicle = _first_vehicle(session)
    window = ("current-drive" if current_drive else
              "since-charge" if since_charge else f"{days}d")
    filename = f"tesla-analyzer-{vehicle.vin[-6:]}-{stem}-{window}.csv"
    return Response(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/backup")
def backup_now(session: Session = Depends(get_session)):
    """POST the full-history export ZIP to the configured backup webhook.

    Cron-callable the same way /api/sync is (the sync_key query param passes
    the passcode gate) — call this on whatever schedule you want a backup
    (daily/weekly) from the same external cron that already hits /api/sync.
    No internal scheduling: every call sends a fresh full backup, so the
    calling cron's own interval is the backup interval.
    """
    settings = get_settings()
    url = settings.backup_webhook_url.strip()
    if not url:
        raise HTTPException(400, "No BACKUP_WEBHOOK_URL configured.")

    vehicle = _first_vehicle(session)
    drives, charges = _window(session, vehicle.id, days=3650)
    zip_bytes = _build_export_zip(drives, charges)
    name = f"tesla-analyzer-{vehicle.vin[-6:]}-backup.zip"

    try:
        resp = httpx.post(
            url, content=zip_bytes,
            headers={"Content-Type": "application/zip",
                    "Content-Disposition": f'attachment; filename="{name}"'},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Backup webhook delivery failed: {exc}") from exc

    return {
        "sent": True, "bytes": len(zip_bytes),
        "drives": len(drives), "charges": len(charges),
        "webhook_status": resp.status_code,
    }


def _monthly_report_payload(session: Session, vehicle: Vehicle, settings, days: int) -> dict:
    """Driving/charging/efficiency summary for the last ``days`` days, plus a
    plain-text rendering — Slack and Discord incoming webhooks both read a
    top-level "text" field directly, so the same payload works untouched
    there while still carrying the structured figures for anything else."""
    capacity_kwh, _ = _usable_capacity(session, vehicle, settings)
    drives, charges = _window(session, vehicle.id, days)
    price_fn = tariff.price_fn_from_settings(settings)
    trip_costs = _trip_cost_map(session, vehicle.id)
    driving = driving_analysis.analyze(
        drives, settings.rated_wh_per_km, capacity_kwh, price_fn, charges=charges,
        trip_costs=trip_costs)
    charging = charging_analysis.analyze(charges, drives)
    efficiency = efficiency_analysis.analyze(drives, settings.rated_wh_per_km)
    cur = settings.currency

    lines = [f"📊 {vehicle.name} — last {days} days"]
    if driving.get("available"):
        lines.append(
            f"🚗 {driving['total_distance_km']} km over {driving['total_drives']} drives "
            f"({driving['total_duration_h']} h)"
        )
        if driving.get("total_cost") is not None:
            lines.append(f"💵 {cur} {driving['total_cost']} in driving energy cost")
    else:
        lines.append("🚗 No drives logged in this period.")
    if efficiency.get("available") and efficiency.get("avg_efficiency_wh_per_km"):
        vs = efficiency["vs_rated_pct"]
        lines.append(
            f"📈 {efficiency['avg_efficiency_wh_per_km']} Wh/km "
            f"({'+' if vs >= 0 else ''}{vs}% vs rated)"
        )
    if charging.get("available"):
        lines.append(
            f"⚡ {charging['total_energy_kwh']} kWh charged across "
            f"{charging['total_sessions']} sessions — {cur} {charging['total_cost']}"
        )
    else:
        lines.append("⚡ No charging sessions logged in this period.")

    # Data-driven narrative: this period vs the equal-length one immediately
    # before it, so "you drove more/less than usual" is a real comparison
    # instead of a stats table with no context.
    now = sync_mod.now_local()   # MYT wall-clock, to match stored start_time
    since = now - timedelta(days=days)
    prev_since = since - timedelta(days=days)
    prev_drives, prev_charges = _window(session, vehicle.id, days, since=prev_since, until=since)
    prev_driving = driving_analysis.analyze(
        prev_drives, settings.rated_wh_per_km, capacity_kwh, price_fn, charges=prev_charges,
        trip_costs=trip_costs)
    prev_charging = charging_analysis.analyze(prev_charges, prev_drives)
    prev_efficiency = efficiency_analysis.analyze(prev_drives, settings.rated_wh_per_km)
    narrative_lines = narrative_engine.build(
        {"driving": driving, "charging": charging, "efficiency": efficiency},
        {"driving": prev_driving, "charging": prev_charging, "efficiency": prev_efficiency},
        cur,
    )

    return {
        "text": "\n".join(lines) + "\n\n📝 " + " ".join(narrative_lines),
        "narrative": narrative_lines,
        "vehicle": vehicle.name,
        "period_days": days,
        "driving": driving,
        "charging": charging,
        "efficiency": efficiency,
    }


@router.get("/reports/monthly")
def monthly_report(days: int = Query(30, ge=1, le=365), session: Session = Depends(get_session)):
    """POST a driving/charging/efficiency summary to the configured report
    webhook — cron-callable the same way /api/sync and /api/backup are (the
    sync_key query param passes the passcode gate). No internal scheduling:
    call this on whatever schedule you want the report at (monthly is the
    intended use — see README) from the same external cron that already
    hits /api/sync; ``days`` controls how far back each report looks.
    """
    settings = get_settings()
    url = settings.report_webhook_url.strip()
    if not url:
        raise HTTPException(400, "No REPORT_WEBHOOK_URL configured.")

    vehicle = _first_vehicle(session)
    payload = _monthly_report_payload(session, vehicle, settings, days)

    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Report webhook delivery failed: {exc}") from exc

    return {"sent": True, "webhook_status": resp.status_code, "period_days": days}


def _standby_longest(session: Session, vehicle: Vehicle, drives, charges,
                     capacity_kwh: float, price_fn) -> dict | None:
    """The single biggest recent parked-drain event, for the standby alert:
    the charge-free gap that lost the most kWh, with its %/kWh/hours, the
    likely inducer, and a cost. None when there's no material parked drain."""
    vd = driving_analysis.vampire_drain(drives, charges, capacity_kwh)
    gaps = vd.get("gap_list") or []
    if not gaps:
        return None
    top = max(gaps, key=lambda g: g.get("kwh", 0.0))
    if not top.get("kwh"):
        return None
    inducer = _idle_inducer(session, vehicle.id, top["start"], top["end"])
    rate = price_fn(datetime.fromisoformat(top["end"])) if price_fn else None
    return {
        "kwh": top["kwh"], "pct": top.get("pct", 0.0), "hours": top.get("hours", 0.0),
        "end": top["end"], "inducer": inducer,
        "cost": round(top["kwh"] * rate, 2) if rate else None,
    }


# GET *and* POST: the other cron endpoints (/api/sync, /api/reports/monthly)
# are GET so an external cron — or a browser address bar, for a quick test —
# can hit them without configuring a request method. Accept both here for the
# same ergonomics; the de-dup below keeps a stray GET from re-notifying.
@router.api_route("/alerts/check", methods=["GET", "POST"])
def alerts_check(days: int = Query(30, ge=7, le=90),
                 session: Session = Depends(get_session)):
    """Evaluate proactive alerts and push any that fire — cron-callable the
    same way /api/sync and /api/reports/monthly are (the sync_key query param
    passes the passcode gate). Meant to run daily. De-duplicates internally,
    so calling it more often is harmless: a standing condition is re-sent only
    when it changes or after a cooldown (see alerts.py). ``days`` is the window
    each trend compares against the equal-length window before it."""
    now = sync_mod.now_local()   # MYT wall-clock, to match stored start_time
    vehicle = _first_vehicle(session)
    settings = get_settings()
    capacity_kwh, _ = _usable_capacity(session, vehicle, settings)
    price_fn = tariff.price_fn_from_settings(settings)

    cur_since = now - timedelta(days=days)
    prev_since = cur_since - timedelta(days=days)
    drives, charges = _window(session, vehicle.id, days)
    prev_drives, _ = _window(session, vehicle.id, days, since=prev_since, until=cur_since)
    efficiency = efficiency_analysis.analyze(drives, settings.rated_wh_per_km)
    prev_efficiency = efficiency_analysis.analyze(prev_drives, settings.rated_wh_per_km)

    readings = _newest_readings(
        session, vehicle.id,
        (BatteryReading.soc, BatteryReading.range_km,
         BatteryReading.ts, BatteryReading.odo_km))
    vin_info = vin_mod.decode(vehicle.vin)
    spec_km = settings.battery_new_range_km or battery_analysis.new_range_for(
        vehicle.model, vehicle.trim, year=vin_info.get("year"))
    battery = battery_analysis.analyze(
        [{"soc": s, "range_km": r, "ts": t, "odo_km": o} for s, r, t, o in readings],
        new_range_km=spec_km)

    records = session.scalars(
        select(ServiceRecord).where(ServiceRecord.vehicle_id == vehicle.id)).all()
    current_odo = session.scalar(
        select(func.max(BatteryReading.odo_km)).where(BatteryReading.vehicle_id == vehicle.id))
    service_rows = service_analysis.due_status(
        [{"type": r.type, "date": r.date, "odo_km": r.odo_km} for r in records],
        current_odo_km=current_odo, now=now)

    candidates = alerts.evaluate(
        now=now, efficiency=efficiency, prev_efficiency=prev_efficiency,
        battery=battery, service_rows=service_rows,
        standby_longest=_standby_longest(session, vehicle, drives, charges,
                                         capacity_kwh, price_fn),
        currency=settings.currency,
    )
    sent = alerts.dispatch(
        session, candidates,
        notify=lambda title, body, tag: notifications.notify(session, title, body, tag),
        now=now,
    )
    return {"fired": [c["key"] for c in candidates], "sent": sent}


@router.get("/plan/route")
def plan_route(to: str = Query(..., min_length=1),
               depart: str = Query("", description="HH:MM local; next occurrence is used"),
               session: Session = Depends(get_session)):
    """Resolve a typed destination to a driving distance for the Trip Planner.

    Origin is where the car is now — its last parked location (the most recent
    trip's end point) — falling back to a Place named "Home" if there are no
    trips yet. Destination is geocoded (Google when a key is set, else OSM);
    distance is Google Directions driving distance when available, else a
    straight-line estimate scaled for real roads. ``method`` says which, so the
    planner can label a rough estimate honestly."""
    dest = _forward_geocode(to)
    if not dest:
        raise HTTPException(404, f"Couldn't find a place matching “{to}”.")
    dest_lat, dest_lon, dest_label = dest
    dest_coords = f"{dest_lat}, {dest_lon}"

    vehicle = _first_vehicle(session)
    last_drive = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id, Drive.end_coords != "")
        .order_by(Drive.start_time.desc()).limit(1)
    ).first()
    origin_coords = last_drive.end_coords if last_drive else None
    origin_label = (last_drive.end_location or "last parked location") if last_drive else None
    if not origin_coords:
        home = session.scalars(
            select(Place).where(func.lower(Place.name) == "home")).first()
        if home:
            origin_coords, origin_label = f"{home.lat}, {home.lon}", "Home"
    if not origin_coords:
        raise HTTPException(
            409, "No known starting point yet — log a trip or add a Home place first, "
                 "or just type the distance in the planner.")

    # A departure time asks Google for its traffic prediction. Google's own
    # clock is UTC-based epoch seconds and it rejects a departure in the past,
    # so an HH:MM that's already gone today is read as tomorrow.
    #
    # The HH:MM is the *driver's* wall clock (the planner's "Leaving" field),
    # which is what makes both steps below timezone-sensitive: building the
    # datetime on datetime.now() dated it by the server's clock, and
    # .timestamp() then read it back as the server's zone too. Nothing pins
    # the server to MYT, so on the deployed host an 18:00 departure reached
    # Google as 02:00 the next morning — a peak-hour trip priced off
    # dead-of-night traffic, and optimistically, since app.js prefers the
    # traffic basis over the (correctly zoned) departure-hour history one.
    # Both steps go through the MYT helpers now.
    depart_epoch = None
    hhmm = (depart or "").strip()
    if hhmm:
        try:
            hh, mm = (int(p) for p in hhmm.split(":", 1))
            now = sync_mod.now_local()
            when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if when <= now:
                when += timedelta(days=1)
            depart_epoch = int(sync_mod.to_epoch(when))
        except (ValueError, TypeError):
            depart_epoch = None

    result = _driving_distance_km(origin_coords, dest_coords, depart_epoch)
    if result is None:
        raise HTTPException(502, "Couldn't work out the distance to there.")
    km, method, traffic_kmh = result
    # What this direction of this route has actually cost, when it's been
    # driven enough times. Every other basis the planner has is an average
    # over other roads — this one is the road itself, which is also the only
    # thing that prices the climb, since a route's elevation cancels out of
    # any figure that pools both directions (see driving.route_asymmetry).
    route_eff = None
    if last_drive is not None:
        _dest_place, dest_area = _place_and_area(dest_coords, session)
        past = session.scalars(
            select(Drive).where(Drive.vehicle_id == vehicle.id)
        ).all()
        route_eff = driving_analysis.direction_wh_per_km(
            list(past), last_drive.end_area or last_drive.end_location, dest_area)
    return {
        "km": km, "method": method,
        "origin_label": origin_label, "dest_label": dest_label,
        "dest_coords": dest_coords,
        # None unless this direction has enough history of its own; the
        # planner keeps its broader basis rather than trading down to a
        # thinner measurement.
        "route_wh_per_km": route_eff["wh_per_km"] if route_eff else None,
        "route_trips": route_eff["n"] if route_eff else None,
        # Google's predicted average speed for this route at this departure —
        # the planner converts it to Wh/km through the driver's OWN measured
        # speed/efficiency slope. None when no departure was given, no key is
        # set, or Google returned no traffic estimate.
        "traffic_kmh": traffic_kmh,
    }


# --- Web push notifications -------------------------------------------------


@router.get("/push/vapid-public-key")
def push_vapid_public_key():
    """The VAPID application server key the browser needs to call
    PushManager.subscribe(). 404 (not just an empty value) when push isn't
    configured, so the frontend can cleanly hide the "Enable notifications"
    control rather than offer a subscribe button that would fail."""
    key = notifications.public_key_b64()
    if not key:
        raise HTTPException(404, "Push notifications aren't configured on this server.")
    return {"key": key}


@router.post("/push/subscribe")
def push_subscribe(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Register a browser's push subscription. Body matches the browser's
    own PushSubscription.toJSON() shape: {endpoint, keys: {p256dh, auth}}."""
    if not notifications.enabled():
        raise HTTPException(404, "Push notifications aren't configured on this server.")
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "Malformed subscription payload.")
    notifications.subscribe(session, endpoint, keys["p256dh"], keys["auth"])
    return {"subscribed": True}


@router.post("/push/unsubscribe")
def push_unsubscribe(payload: dict = Body(...), session: Session = Depends(get_session)):
    endpoint = payload.get("endpoint")
    if endpoint:
        notifications.unsubscribe(session, endpoint)
    return {"unsubscribed": True}


@router.post("/push/test")
def push_test(session: Session = Depends(get_session)):
    """Send a test notification to every subscribed device — the "does this
    actually reach my phone" check. Returns how many devices it was delivered
    to (0 means push isn't configured or nothing is subscribed yet)."""
    if not notifications.enabled():
        raise HTTPException(404, "Push notifications aren't configured on this server.")
    sent = notifications.notify(
        session,
        "Tesla Analyzer",
        "Test notification — if you can see this, alerts are working. 🎉",
        "test",
    )
    return {"sent": sent}


# GET as well as POST: these are hand-run repair tools, and the person
# running them is on a phone where the only way to issue a request is the
# address bar — which sends GET. POST-only made them look like they had
# run while returning 405 and changing nothing. Mutating on GET is
# normally wrong; here nothing writes without an explicit apply=true that
# no prefetcher will ever guess, and the app is passcode-gated.
@router.api_route("/repair-trip-boundary", methods=["GET", "POST"])
def repair_trip_boundary(
    closed_id: int = Query(...),
    open_id: int = Query(...),
    boundary_odo_km: float = Query(...),
    closed_end_time: str | None = Query(None),
    closed_end_coords: str | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Move the odometer boundary between two consecutive trips.

    Written to undo a specific piece of damage: the place-split reverted in
    cc40c8e credited an arriving trip 1.311 km it never drove and starved the
    departing one by the same amount. Nothing self-heals that — both rows are
    already written — so the boundary has to be put back by hand.

    Kept general and explicit rather than hard-coding those two trips: the
    caller supplies where the boundary belongs, and every derived figure
    follows from it. Energy moves with distance, holding each trip's own
    Wh/km constant, which is the same rule energy_for_blind_distance uses and
    the exact inverse of how the bad figure was produced. soc_used_pct is left
    alone — it is measured from SoC readings, not derived from distance, and
    was never touched by the fault.

    Dry run by default.
    """
    closed = session.get(Drive, closed_id)
    opened = session.get(Drive, open_id)
    if closed is None or opened is None:
        raise HTTPException(404, "One of those trips doesn't exist.")
    if closed.end_odo_km is None or opened.start_odo_km is None:
        raise HTTPException(409, "Those trips predate odometer instrumentation.")
    if abs(closed.end_odo_km - opened.start_odo_km) > 0.002:
        raise HTTPException(
            409, f"Those trips don't share a boundary: {closed.end_odo_km} vs "
                 f"{opened.start_odo_km}. Repairing a gap needs a different fix.")

    delta = round(boundary_odo_km - closed.end_odo_km, 3)
    # Where a poll last actually saw the car: the recorded end minus whatever
    # of it the arrival model had estimated rather than read.
    last_seen_odo = closed.end_odo_km - (closed.end_est_km or 0.0)
    closed_new_dist = round(closed.distance_km + delta, 1)
    opened_new_dist = round(opened.distance_km - delta, 1)
    if closed_new_dist <= 0 or opened_new_dist <= 0:
        raise HTTPException(409, "That boundary would leave a trip with no distance.")

    def rescaled(drive, new_dist):
        """Energy at the trip's own Wh/km over its corrected distance."""
        if not drive.energy_used_kwh or drive.distance_km <= 0:
            return drive.energy_used_kwh
        return round(drive.energy_used_kwh * new_dist / drive.distance_km, 2)

    plan = {
        "delta_km": delta,
        "closed": {
            "id": closed.id, "route": f"{closed.start_location} → {closed.end_location}",
            "distance_km": [closed.distance_km, closed_new_dist],
            "energy_kwh": [closed.energy_used_kwh, rescaled(closed, closed_new_dist)],
            "end_odo_km": [closed.end_odo_km, boundary_odo_km],
            "end_coords": [closed.end_coords, closed_end_coords or closed.end_coords],
        },
        "open": {
            "id": opened.id, "route": f"{opened.start_location} → {opened.end_location}",
            "distance_km": [opened.distance_km, opened_new_dist],
            "energy_kwh": [opened.energy_used_kwh, rescaled(opened, opened_new_dist)],
            "start_odo_km": [opened.start_odo_km, boundary_odo_km],
        },
    }
    if apply:
        closed.energy_used_kwh = rescaled(closed, closed_new_dist)
        closed.distance_km, closed.end_odo_km = closed_new_dist, boundary_odo_km
        opened.energy_used_kwh = rescaled(opened, opened_new_dist)
        opened.distance_km, opened.start_odo_km = opened_new_dist, boundary_odo_km
        # The stretch handed back to the departing trip arrived without a
        # reading of its own, which is what start_recovered_km records.
        if delta < 0:
            opened.start_recovered_km = round(
                (opened.start_recovered_km or 0.0) - delta, 3)
        # The fault also restamped the arrival with whichever place it matched,
        # which is how a trip home came to read "-> QBM". Coordinates are what
        # the label is derived from, so restoring them and re-deriving is the
        # only way to put the name back without asserting it directly.
        if closed_end_coords:
            closed.end_coords = closed_end_coords
            closed.end_location, closed.end_area = _place_and_area(
                closed_end_coords, session)
        if closed_end_time:
            closed.end_time = datetime.fromisoformat(closed_end_time)
            start = closed.start_time
            closed.duration_min = round(
                (closed.end_time - start).total_seconds() / 60.0, 1)
        for d, dist in ((closed, closed_new_dist), (opened, opened_new_dist)):
            if d.duration_min and d.duration_min > 0:
                d.avg_speed_kmh = round(dist / (d.duration_min / 60.0), 1)
        # Moving the boundary FORWARD measures the closed trip's arrival tail:
        # the ground between the last reading a poll actually took and where
        # the car turned out to have stopped. That is the quantity the arrival
        # model now runs on (see _place_tail_km), and the only source of it at
        # a place with no signal is a check like this one — so record it rather
        # than let a measurement be spent on one row and thrown away.
        #
        # Measured from the last real reading, which is the old end minus
        # whatever of it was estimated: 337 ended at 29008.311 with 0.04
        # estimated, so a poll last saw 29008.271 and the true tail was 0.193.
        if delta > 0:
            seen = last_seen_odo
            _record_tail_sample(session, closed, closed.end_est_km or 0.0,
                                max(boundary_odo_km - seen, 0.0),
                                reason="verified")
            # And it is no longer a guess: still unseen by any poll, which is
            # what end_est_km means, but now checked against the car itself.
            closed.end_est_km = round(boundary_odo_km - seen, 3) or None
            closed.end_est_verified = True
        session.commit()
    return {"applied": apply, **plan}


@router.api_route("/repair-departure-start", methods=["GET", "POST"])
def repair_departure_start(
    drive_id: int = Query(...),
    true_duration_min: float | None = Query(None),
    true_wh_per_km: float | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Move a trip's start off a park it swallowed, and take the drain with it.

    The mirror of the trimmed tail, and the same correction: a trip anchored
    before the car actually set off carries both the standing time and the
    standby drain of the interval it swallowed. Measured, trip 340: an
    11-minute park fell just under the threshold that would have re-anchored
    it, so the trip read 16 minutes against the car's own 5 and 0.50 kWh
    against 0.38.

    Fixed in sync for everything logged since, but a row already written keeps
    it, and neither odometer repair can touch this: the distance and the
    boundary are right, it is the clock that is wrong.

    Distance is never altered — the odometer measured it and the park added
    none of it. Energy is, because the swallowed minutes really did draw power,
    and taking them off at this car's own measured parked rate is exactly what
    trim_standby_kwh does at the other end.

    Dry run by default.
    """
    drive = session.get(Drive, drive_id)
    if drive is None:
        raise HTTPException(404, "No such trip.")
    if true_duration_min is None and true_wh_per_km is None:
        raise HTTPException(
            409, "Give true_duration_min, true_wh_per_km, or both — there is "
                 "nothing to correct from otherwise.")
    if true_duration_min is not None and true_duration_min <= 0:
        raise HTTPException(409, "A trip cannot have no duration.")
    shift_sec = (round((drive.duration_min - true_duration_min) * 60.0, 1)
                 if true_duration_min is not None else 0.0)
    if shift_sec < 0:
        raise HTTPException(
            409, f"Trip {drive_id} already runs {drive.duration_min} min, which "
                 f"is shorter than the {true_duration_min} given. This moves a "
                 f"start FORWARD off a swallowed park; a trip reading short is "
                 f"a different fault.")
    past_drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == drive.vehicle_id)).all()
    past_charges = session.scalars(
        select(Charge).where(Charge.vehicle_id == drive.vehicle_id)).all()
    vehicle = session.get(Vehicle, drive.vehicle_id)
    capacity_kwh = _usable_capacity(session, vehicle, get_settings())[0]
    rate_kw = _trim_rate_kw(list(past_drives), list(past_charges), capacity_kwh)
    if true_wh_per_km is not None:
        # The car's own figure for the drive, which beats charging the park at
        # an average rate. That rate is this car's mean across every park it
        # has taken, and a specific one can be far from it: trip 340's eleven
        # minutes in a 34 degree car park, awake with climate running, drew
        # about 1.07 kW where the average said 0.323 — so the drain correction
        # left it a third high. Where the car has measured the drive itself,
        # measurement wins, exactly as true_distance_km does for the arrival.
        if not (sync_mod.MIN_PLAUSIBLE_WH_PER_KM <= true_wh_per_km
                <= sync_mod.MAX_PLAUSIBLE_WH_PER_KM):
            raise HTTPException(
                409, f"{true_wh_per_km} Wh/km is outside what a real drive can "
                     f"average ({sync_mod.MIN_PLAUSIBLE_WH_PER_KM:.0f}-"
                     f"{sync_mod.MAX_PLAUSIBLE_WH_PER_KM:.0f}). Refusing rather "
                     f"than writing a figure no drive could produce.")
        new_energy = true_wh_per_km * drive.distance_km / 1000.0
    else:
        new_energy = sync_mod.trim_standby_kwh(
            drive.energy_used_kwh, drive.distance_km, shift_sec, rate_kw)
    new_start = drive.start_time + timedelta(seconds=shift_sec)
    new_duration = (true_duration_min if true_duration_min is not None
                    else drive.duration_min)
    new_speed = (round(drive.distance_km / (new_duration / 60.0), 1)
                 if new_duration else drive.avg_speed_kmh)
    plan = {
        "drive_id": drive_id,
        "route": f"{drive.start_location} → {drive.end_location}",
        "moved_sec": shift_sec,
        # Null when the car's own Wh/km was given instead — nothing was
        # charged at a modelled rate, so reporting one would mislead.
        "standby_rate_kw": (round(rate_kw, 3) if rate_kw and true_wh_per_km is None
                            else None),
        "energy_source": "car" if true_wh_per_km is not None else "standby model",
        "start_time": [drive.start_time.isoformat(), new_start.isoformat()],
        "duration_min": [drive.duration_min, new_duration],
        "energy_kwh": [drive.energy_used_kwh, round(new_energy, 2)],
        "avg_speed_kmh": [drive.avg_speed_kmh, new_speed],
        # Untouched, and stated so rather than left to be noticed: the park
        # added no distance, so none comes off.
        "distance_km": drive.distance_km,
    }
    if apply:
        drive.start_time = new_start
        drive.duration_min = new_duration
        drive.energy_used_kwh = round(new_energy, 2)
        drive.avg_speed_kmh = new_speed
        # Idle time cannot exceed a duration that just shrank.
        drive.idle_min = round(min(drive.idle_min or 0.0, new_duration), 1)
        session.commit()
    return {"applied": apply, **plan}


@router.api_route("/clear-duplicated-loss", methods=["GET", "POST"])
def clear_duplicated_loss(
    drive_id: int | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Clear an end_lost_km that the FOLLOWING trip already accounts for.

    A trip closed offline could be stamped with the whole odometer movement
    since its last reading, even when the next contact caught the car already
    driving — in which case that movement spans two trips and the next one's
    departure recovery claims the same ground. Both then describe it, one as
    lost and one as recovered. Fixed going forward in 41db2e3; rows written
    before that keep the claim.

    Deliberately not a field-blanker. It re-derives the duplication from the
    two rows and refuses when it does not hold: the next trip must start
    exactly where this one ended, and must have recovered at least as much as
    this one claims to have lost. A trip that really did lose distance keeps
    saying so — erasing that would destroy the only record of a genuine
    boundary error, which is the opposite of what this instrumentation is for.

    With no drive_id, reports every candidate and changes nothing.
    """
    def candidate(d: Drive) -> dict | None:
        lost = d.end_lost_km or 0.0
        if lost <= 0 or d.end_odo_km is None:
            return None
        nxt = session.scalars(
            select(Drive).where(Drive.vehicle_id == d.vehicle_id,
                                Drive.start_time > d.start_time)
            .order_by(Drive.start_time).limit(1)
        ).first()
        if nxt is None or nxt.start_odo_km is None:
            return None
        # Same boundary, and the next trip pulled back over at least the
        # stretch this one calls lost. Either failing means the two rows are
        # describing different ground and this is not a duplication.
        if abs(nxt.start_odo_km - d.end_odo_km) > 0.002:
            return None
        if (nxt.start_recovered_km or 0.0) + 0.002 < lost:
            return None
        return {
            "drive_id": d.id,
            "route": f"{d.start_location} → {d.end_location}",
            "end_lost_km": lost,
            "next_drive_id": nxt.id,
            "next_start_recovered_km": nxt.start_recovered_km,
            "shared_odo_km": d.end_odo_km,
        }

    if drive_id is None:
        rows = session.scalars(
            select(Drive).where(Drive.end_lost_km.isnot(None))
            .order_by(Drive.start_time.desc()).limit(200)
        ).all()
        found = [c for c in (candidate(d) for d in rows) if c]
        return {"applied": False, "count": len(found), "candidates": found,
                "note": "Re-run with drive_id=<id>&apply=true to clear one."}

    drive = session.get(Drive, drive_id)
    if drive is None:
        raise HTTPException(404, "No such trip.")
    found = candidate(drive)
    if found is None:
        raise HTTPException(
            409, f"Trip {drive_id}'s end_lost_km is not duplicated by the "
                 f"following trip — either they don't share a boundary, or the "
                 f"next trip didn't recover that ground. Refusing: this tool "
                 f"clears a double-report, not a real loss.")
    if apply:
        drive.end_lost_km = None
        session.commit()
    return {"applied": apply, **found}


@router.api_route("/repair-trip-overlap", methods=["GET", "POST"])
def repair_trip_overlap(
    closed_id: int = Query(...),
    open_id: int = Query(...),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Pull a trip's start forward off ground the previous trip already holds.

    repair_trip_boundary cannot do this and refuses to try: it MOVES a shared
    boundary, conserving distance by giving one trip what it takes from the
    other, and it checks the two anchors agree before touching anything. An
    overlap is the case where they do not agree, and it is not a transfer —
    the earlier trip is already correct, so the later trip's excess belongs to
    nobody and simply leaves.

    That is trip 333: it anchored to the last reading before the car park dead
    zone, 0.320 km behind where trip 332 had already been verified to stop, and
    so counted that stretch twice — 11.0 km against the car's own 10.7.

    The closed trip is treated as the authority and is not touched. Only use
    this when that is actually true; where the LATER trip is the trustworthy
    one, its start is right and the earlier trip's end is what needs moving.

    Dry run by default.
    """
    closed = session.get(Drive, closed_id)
    opened = session.get(Drive, open_id)
    if closed is None or opened is None:
        raise HTTPException(404, "One of those trips doesn't exist.")
    if closed.end_odo_km is None or opened.start_odo_km is None:
        raise HTTPException(409, "Those trips predate odometer instrumentation.")
    overlap = round(closed.end_odo_km - opened.start_odo_km, 3)
    if overlap <= 0:
        raise HTTPException(
            409, f"Trip {open_id} starts at {opened.start_odo_km} and trip "
                 f"{closed_id} ends at {closed.end_odo_km} — no overlap. "
                 f"A gap between them is the opposite fault and needs "
                 f"/api/repair-trip-boundary.")
    if opened.end_odo_km is None:
        raise HTTPException(409, "The later trip has no end odometer to measure from.")
    new_dist = round(opened.end_odo_km - closed.end_odo_km, 1)
    if new_dist <= 0:
        raise HTTPException(
            409, f"Removing {overlap} km would leave trip {open_id} with no "
                 f"distance. These two are not simply overlapping.")
    # Scaled on the odometer spans, not on distance_km. distance_km is stored
    # to 0.1, so the ratio of two rounded figures carries rounding twice over —
    # here it prices the trip at 1.91 kWh where the spans give 1.90, which is
    # the car's own reading. Same argument repair_arrival_tail makes for
    # measuring the difference from the anchors.
    old_span = opened.end_odo_km - opened.start_odo_km
    new_span = opened.end_odo_km - closed.end_odo_km
    new_energy = (round(opened.energy_used_kwh * new_span / old_span, 2)
                  if opened.energy_used_kwh and old_span > 0 else
                  opened.energy_used_kwh)
    # What the departure recovery reclaimed is measured from the anchor it
    # reclaimed to, so moving the anchor moves this by the same amount. Left at
    # its old value it would claim to have pulled back ground that now belongs
    # to the previous trip.
    new_recovered = (round(max((opened.start_recovered_km or 0.0) - overlap, 0.0), 3)
                     if opened.start_recovered_km is not None else None)
    new_speed = (round(new_dist / (opened.duration_min / 60.0), 1)
                 if opened.duration_min else opened.avg_speed_kmh)
    plan = {
        "overlap_km": overlap,
        "closed": {"id": closed.id, "end_odo_km": closed.end_odo_km,
                   "note": "unchanged — treated as the authority"},
        "open": {
            "id": opened.id,
            "route": f"{opened.start_location} → {opened.end_location}",
            "start_odo_km": [opened.start_odo_km, closed.end_odo_km],
            "distance_km": [opened.distance_km, new_dist],
            "energy_kwh": [opened.energy_used_kwh, new_energy],
            "avg_speed_kmh": [opened.avg_speed_kmh, new_speed],
            "start_recovered_km": [opened.start_recovered_km, new_recovered],
        },
    }
    if apply:
        opened.start_odo_km = closed.end_odo_km
        opened.distance_km = new_dist
        opened.energy_used_kwh = new_energy
        opened.avg_speed_kmh = new_speed
        if new_recovered is not None:
            opened.start_recovered_km = new_recovered
        session.commit()
    return {"applied": apply, **plan}


@router.get("/arrival-estimates")
def arrival_estimates(
    limit: int = Query(100),
    session: Session = Depends(get_session),
):
    """The arrival model's predictions scored against what was measured.

    Every pair the app has: what it predicted a no-network arrival would still
    cover, and what a later reading or a check against the car's own trip meter
    showed it actually did.

    The ``places`` block is the model itself rather than a summary of it. The
    estimate runs on the median tail each car park has shown (see
    _place_tail_km), so those medians ARE what a future arrival will be
    credited, and ``in_use`` says whether a place has enough measurements to be
    trusted yet.

    There is no window left to suggest. The speed-and-window model this
    replaced needed 17, 51, 119 and 868 seconds to fit four arrivals, so the
    per-sample error here is a scorecard, not a parameter waiting to be tuned.
    """
    rows = session.scalars(
        select(ArrivalTailSample).order_by(ArrivalTailSample.ts.desc()).limit(limit)
    ).all()
    out = []
    for r in rows:
        out.append({
            "ts": r.ts.isoformat(timespec="minutes"),
            "drive_id": r.drive_id,
            "est_km": r.est_km,
            "measured_km": r.measured_km,
            "error_km": round(r.est_km - r.measured_km, 3),
            "speed_kmh": r.speed_kmh,
            "est_sec": round(r.est_sec, 1) if r.est_sec else r.est_sec,
            "place": _sample_place(session, r) or "(unknown)",
            "elapsed_min": r.elapsed_min,
            "reason": r.reason,
        })
    # What each place has actually shown, which is what the estimate now runs
    # on. The medians here ARE the model (see _place_tail_km), so this is not a
    # summary of the samples but a readout of what they have decided.
    by_place: dict[str, list[float]] = {}
    for r in rows:
        if r.measured_km is not None:
            by_place.setdefault(_sample_place(session, r) or "(unknown)",
                                []).append(r.measured_km)
    places = [{
        "place": p,
        "samples": len(v),
        "median_tail_km": round(percentile(sorted(v), 0.5), 3),
        "min_km": min(v), "max_km": max(v),
        "in_use": len(v) >= PLACE_TAIL_MIN_SAMPLES,
    } for p, v in sorted(by_place.items(), key=lambda kv: -len(kv[1]))]
    summary = {
        "samples": len(out),
        "min_samples_per_place": PLACE_TAIL_MIN_SAMPLES,
        "places_estimating": sum(1 for p in places if p["in_use"]),
        "places_short_of_data": sum(1 for p in places if not p["in_use"]),
    }
    if out:
        # The model's scorecard, not a parameter: positive means it has been
        # crediting more ground than the car covered.
        summary["mean_error_km"] = round(
            sum(r["error_km"] for r in out) / len(out), 3)
        summary["over_predicting"] = summary["mean_error_km"] > 0
    return {"summary": summary, "places": places, "samples": out}


@router.get("/estimated-tails")
def estimated_tails(
    limit: int = Query(50),
    session: Session = Depends(get_session),
):
    """Every trip still carrying an estimated arrival tail, worst share first.

    end_est_km made a single trip's estimate visible; this makes the set of
    them visible. Without it the only way to ask "which of my trips read long?"
    is to open them one at a time, which is how trip 332 went a day unnoticed.

    Sorted by the share of the trip that is estimated rather than the raw
    kilometres: 0.5 km on a 2 km trip distorts its Wh/km twelve times as much
    as the same 0.5 km on a 25 km one, and Wh/km is what these figures are
    mostly read through.

    Read-only. Each row carries what repair_arrival_tail needs, so checking one
    against the car's own trip meter and correcting it are the same two steps.
    """
    rows = session.scalars(
        select(Drive).where(Drive.end_est_km.isnot(None))
        .where(Drive.end_est_verified.isnot(True))
        .order_by(Drive.start_time.desc()).limit(max(limit, 1) * 4)
    ).all()
    out = []
    for d in rows:
        est = d.end_est_km or 0.0
        if est <= 0 or not d.distance_km:
            continue
        out.append({
            "drive_id": d.id,
            "route": f"{d.start_location} → {d.end_location}",
            "start_time": d.start_time.isoformat(timespec="minutes"),
            "distance_km": d.distance_km,
            "end_est_km": round(est, 3),
            "estimated_share_pct": round(est / d.distance_km * 100.0, 1),
            # What the trip would read if the whole estimate turned out to be
            # wrong — the other end of the range the true figure sits in, so
            # the car's own number can be placed against both.
            "distance_if_no_tail": round(d.distance_km - est, 1),
            "wh_per_km": (round(d.energy_used_kwh * 1000.0 / d.distance_km, 1)
                          if d.energy_used_kwh else None),
        })
    out.sort(key=lambda r: r["estimated_share_pct"], reverse=True)
    return {
        "count": len(out[:limit]),
        # An estimate is not a defect — it is the honest reading for an arrival
        # no poll could see. This lists what to CHECK, not what to fix.
        "note": "Estimated distance awaiting a measurement. Check against the "
                "car's own trip meter; correct with /api/repair-arrival-tail.",
        "trips": out[:limit],
    }


@router.api_route("/repair-arrival-tail", methods=["GET", "POST"])
def repair_arrival_tail(
    drive_id: int = Query(...),
    true_distance_km: float = Query(...),
    true_duration_min: float | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Take back an estimated arrival tail the car's own screen disproves.

    repair_trip_boundary can't do this job: it trades distance BETWEEN two
    trips that share a boundary, and an over-estimated tail has no counterpart
    to trade with — the kilometres belong to nobody, which is exactly the
    complaint. Here the distance simply leaves.

    The correction that runs automatically (see LAST_SLEEP_CLOSE_KEY) only ever
    gets one chance, at the first poll after the close. A trip whose estimate
    was already wrong before that correction existed, or whose marker has since
    been cleared, is past it — the row is written and nothing revisits it. This
    is that repair, driven by the one authority that settles it: the car's own
    trip meter.

    Both figures come from the same screen and are applied together, because
    the estimate produced both from one assumption. Give distance alone and the
    clock stays where the estimate left it, leaving a trip whose average speed
    now disagrees with its own distance and duration.

    Dry run by default.
    """
    drive = session.get(Drive, drive_id)
    if drive is None:
        raise HTTPException(404, "No such trip.")
    # Measured from the odometer span, not from distance_km. distance_km is
    # stored to 0.1 and the car's screen reads to 0.1, so their difference
    # carries up to 0.1 km of pure rounding — enough on its own to fail the
    # "is this really the estimate?" check below and refuse a correct repair.
    # The odometer anchors are stored to 0.001 and are what the estimate
    # actually moved, so they give the difference exactly (trip 332: 14.063
    # against the car's 13.9 is 0.163, precisely the estimate; the rounded
    # 14.1 would have said 0.2).
    span = (drive.end_odo_km - drive.start_odo_km
            if drive.end_odo_km is not None and drive.start_odo_km is not None
            else drive.distance_km)
    # `or 0.0` collapses negative zero, which float subtraction produces
    # whenever the two agree exactly (28957.009 - 28943.109 lands a hair under
    # 13.9). It is arithmetically fine and reads terribly: this endpoint uses
    # the sign of this number to mean "the trip is SHORTER than the car says",
    # so reporting a match as -0.0 shows the reader the one symbol that has
    # been given the opposite meaning.
    km = round(span - true_distance_km, 3) or 0.0
    # "It already matches" is a real outcome of checking a trip, and the common
    # one — most estimates are close, and this tool is how a trip gets checked
    # at all. Refusing it as an error left trip 332 stuck: repaired before
    # end_est_verified existed, then unable to be marked, because asking again
    # produced a difference of exactly zero and got a 409. A check that cannot
    # return "correct" is not a check.
    #
    # The tolerance is half the car's own display resolution: the screen reads
    # to 0.1 km, so anything inside 0.05 is agreement, not a discrepancy worth
    # rewriting a row over.
    ARRIVAL_MATCH_KM = 0.05
    confirmed_only = abs(km) <= ARRIVAL_MATCH_KM
    if km < -ARRIVAL_MATCH_KM:
        raise HTTPException(
            409, f"Trip {drive_id} reads {drive.distance_km} km, SHORTER than "
                 f"the {true_distance_km} km given. This tool only removes "
                 f"estimated distance; a trip reading short is a different "
                 f"fault (see end_lost_km).")
    est = est_before = drive.end_est_km or 0.0
    if not confirmed_only and km > max(est, 0.0) + 0.002:
        raise HTTPException(
            409, f"Trip {drive_id} carries {est} km of estimated tail but the "
                 f"correction asks for {km} km. The excess isn't the estimate's "
                 f"to give back, so removing it here would hide a real "
                 f"measurement error rather than fix an estimate.")
    sec = (max(drive.duration_min - true_duration_min, 0.0) * 60.0
           if true_duration_min is not None else 0.0)
    before = {
        "distance_km": drive.distance_km, "energy_used_kwh": drive.energy_used_kwh,
        "duration_min": drive.duration_min, "avg_speed_kmh": drive.avg_speed_kmh,
        "end_odo_km": drive.end_odo_km, "end_est_km": drive.end_est_km,
        "end_time": drive.end_time.isoformat(),
    }
    # Applied to a throwaway copy for the dry run, so the preview is produced
    # by the same code that would do the work rather than by a second
    # description of it that can drift from it.
    target = drive if apply else _DetachedDrive(before)
    if confirmed_only and sec > 0:
        # The distance already agrees, but the clock can still be carrying the
        # minutes the estimate invented alongside it. Those are two halves of
        # one assumption and the retraction below moves both — which is no help
        # once the distance has been fixed by an earlier call, because it
        # returns early when there is nothing left to take off. Trip 341 kept
        # the three minutes its estimate added and read 9 against the car's 6.
        #
        # Distance and energy stay put: they already match, and the minutes
        # being removed were never driven, so there is nothing to reprice.
        target.end_time = target.end_time - timedelta(seconds=sec)
        target.duration_min = round(true_duration_min, 1)
        if target.duration_min > 0:
            target.avg_speed_kmh = round(
                target.distance_km / (target.duration_min / 60.0), 1)
    if not confirmed_only:
        _retract_estimated_tail(target, km, sec)
        # _retract_estimated_tail declines rather than raises when the
        # retraction would consume the whole trip — right for the automatic
        # caller, which should leave a row alone rather than mangle it, but
        # wrong to report as a successful repair. Caught here so a repair that
        # reports a change has made one.
        if target.distance_km == before["distance_km"]:
            raise HTTPException(
                409, f"That would take {km} km off a {before['distance_km']} km "
                     f"trip, which is the whole of it. Refusing rather than "
                     f"leaving a trip with no distance.")
    after = {
        "distance_km": target.distance_km, "energy_used_kwh": target.energy_used_kwh,
        "duration_min": target.duration_min, "avg_speed_kmh": target.avg_speed_kmh,
        "end_odo_km": target.end_odo_km, "end_est_km": target.end_est_km,
        "end_time": target.end_time.isoformat(),
    }
    # The automatic correction may still be pending on this very trip — the
    # marker survives until the next poll reaches the car, which for a sleeping
    # car in a car park can be hours. Both are the same correction from
    # different evidence, and running them both would apply it twice: the
    # marker still carries the ORIGINAL est_km, so the clamp would measure the
    # overshoot against a row that has already given it back.
    #
    # The car's own trip meter is the better authority of the two, so this one
    # wins and the pending one is stood down. Only when it names this drive —
    # a marker for any other trip is not ours to clear.
    import json as _json

    pending = None
    vehicle = session.get(Vehicle, drive.vehicle_id)
    if vehicle is not None:
        sleep_key = state.scoped(state.LAST_SLEEP_CLOSE_KEY, vehicle.vin)
        raw = state.get(session, sleep_key)
        if raw:
            try:
                pending = _json.loads(raw).get("drive_id")
            except ValueError:
                pending = None
        if apply and pending == drive_id:
            # Updated, not cleared. Clearing it stopped the double-correction
            # it was meant to stop and also threw away the hand-over — the
            # instruction that tells the NEXT trip to start past the tail this
            # one keeps. Trip 333 then anchored to the pre-blackout reading and
            # re-counted 0.320 km that trip 332 already held.
            #
            # est_km becomes whatever survived the repair, which is exactly the
            # amount the next trip must not claim. odo_km is left alone: it is
            # the last reading the poller actually took, a fact no repair
            # changes.
            marker = _json.loads(raw)
            old_est = marker.get("est_km") or 0.0
            new_est = round(drive.end_est_km or 0.0, 3)
            if old_est:
                marker["est_sec"] = (marker.get("est_sec") or 0.0) * (new_est / old_est)
            marker["est_km"] = new_est
            marker["corrected"] = True
            state.put(session, sleep_key, _json.dumps(marker))
    # A trip that already has a successor cannot have its end moved alone. The
    # correction proves the car parked EARLIER than recorded, so the next trip
    # set off from there too — and if it was logged before this repair ran, its
    # start still sits at the old, fictional end. Measured: trip 341's end came
    # back 0.269 km and trip 342, logged hours earlier, kept starting where the
    # estimate had wrongly put the car, leaving 0.269 km of real driving
    # belonging to no trip at all. It read 11.1 km against the car's own 11.3.
    #
    # Keyed on the gap that is actually there rather than on this call having
    # created it. Checking a trip a second time retracts nothing — the first
    # check already did — but the successor is just as misaligned, and a repair
    # that only works the first time is a trap for anyone who runs it twice.
    #
    # The mirror of repair_trip_overlap: there the later trip gives ground back,
    # here it takes ground on. Both exist because a boundary is one position
    # shared by two rows, and moving it in one is never enough.
    #
    # Bounded by the largest tail this app will ever estimate: past that, a hole
    # between two trips is not a misplaced boundary but a journey nobody logged,
    # and quietly handing it to the next trip would bury that.
    nxt = session.scalars(
        select(Drive).where(Drive.vehicle_id == drive.vehicle_id,
                            Drive.start_time > drive.start_time)
        .order_by(Drive.start_time).limit(1)
    ).first()
    gap_plan = None
    if (nxt is not None and nxt.start_odo_km is not None and nxt.end_odo_km
            and nxt.distance_km > 0):
        gap = round(nxt.start_odo_km - target.end_odo_km, 3)
        new_span = nxt.end_odo_km - target.end_odo_km
        if 0 < gap <= sync_mod.ARRIVAL_EST_MAX_KM and new_span > 0:
            new_energy = (round(nxt.energy_used_kwh * new_span
                                / (nxt.end_odo_km - nxt.start_odo_km), 2)
                          if nxt.energy_used_kwh else nxt.energy_used_kwh)
            gap_plan = {
                "drive_id": nxt.id, "gap_km": gap,
                "start_odo_km": [nxt.start_odo_km, round(target.end_odo_km, 3)],
                "distance_km": [nxt.distance_km, round(new_span, 1)],
                "energy_kwh": [nxt.energy_used_kwh, new_energy],
            }
            if apply:
                nxt.energy_used_kwh = new_energy
                nxt.start_odo_km = round(target.end_odo_km, 3)
                nxt.distance_km = round(new_span, 1)
                # The ground it gains was never seen by a poll — it is the first
                # metres of that trip's own departure, which start_recovered_km
                # is what records.
                nxt.start_recovered_km = round(
                    (nxt.start_recovered_km or 0.0) + gap, 3)
                if nxt.duration_min:
                    nxt.avg_speed_kmh = round(
                        nxt.distance_km / (nxt.duration_min / 60.0), 1)
    if apply:
        # Whatever estimate survives the retraction has now been checked
        # against the car's own trip meter and found right — still unseen by
        # any poll, which is what end_est_km records, but no longer an open
        # question. Marked so the review list can be worked down rather than
        # showing the same reconciled rows forever.
        drive.end_est_verified = True
        # And it is a MEASUREMENT of that place's tail, which is the one thing
        # that predicts the next one (see _place_tail_km). The automatic path
        # needs a poll that finds the car parked after a no-network arrival —
        # the exact case a car park defeats — so at Home it has never once
        # fired. Checking a trip against the car's own screen is the only
        # source there is, and this makes that check feed the model instead of
        # only correcting one row.
        _record_tail_sample(
            session, drive, est_before,
            # What the trip's own anchors say was never seen: the ground
            # between the last reading and where the car really stopped.
            max(true_distance_km - (span - est_before), 0.0),
            reason="verified")
        session.commit()
    return {
        "applied": apply, "drive_id": drive_id,
        "route": f"{drive.start_location} → {drive.end_location}",
        "retracted_km": 0.0 if confirmed_only else km,
        "retracted_sec": 0.0 if confirmed_only else sec,
        # Which of the two outcomes this was. "The trip already agrees with the
        # car" and "the trip was wrong and has been corrected" both end with a
        # verified trip, and a caller reading only the numbers cannot tell them
        # apart when the numbers did not move.
        "outcome": "already_matches" if confirmed_only else "corrected",
        "difference_km": km,
        # So a dry run says whether it is racing the automatic correction, and
        # an applied one says it stood it down.
        "pending_auto_correction": pending == drive_id,
        # The successor, when moving this end left a hole under it.
        "next_trip": gap_plan,
        "before": before, "after": after,
    }


class _DetachedDrive:
    """A stand-in carrying only the fields _retract_estimated_tail touches.

    So the dry run can run the real function without a session anywhere near
    it — no row to accidentally leave dirty, and no second implementation of
    the arithmetic to keep in step with the first.
    """

    def __init__(self, before: dict):
        self.distance_km = before["distance_km"]
        self.energy_used_kwh = before["energy_used_kwh"]
        self.duration_min = before["duration_min"]
        self.avg_speed_kmh = before["avg_speed_kmh"]
        self.end_odo_km = before["end_odo_km"]
        self.end_est_km = before["end_est_km"]
        self.end_time = datetime.fromisoformat(before["end_time"])


@router.api_route("/repair-arrivals", methods=["GET", "POST"])
def repair_arrivals(
    days: int = Query(30, ge=1, le=730),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Give every short arrival back the ground the parked readings saw.

    /api/continuity finds these; repair-trip-boundary fixes one. Ten findings
    is ten hand-built URLs, and the whole point of the check was that nobody
    had ever looked at its output — so making the fix tedious is a good way to
    ensure it goes unused again.

    Measured live: 10 of 78 trips over 30 days, 4.49 km. Nine cluster tightly
    at 0.15-0.40 km, median 0.32, which is one mechanism firing repeatedly
    rather than scattered noise.

    The cap is the code's own: ARRIVAL_EST_MAX_KM, how far an arrival can
    plausibly go on after the last reading. Under it, a short close is a tail
    the poll missed. Over it, the likelier story is a journey nobody logged —
    measured, a 1.82 km overnight gap at Home — and quietly folding that into
    the arriving trip would bury the evidence that it happened. Those are
    reported for a person to judge, never applied.

    Each repair also RECORDS the tail it measured (see repair_trip_boundary's
    apply path), which matters more than the distance. The arrival model learns
    only from arrivals a later poll could measure in time, so it never sees the
    long ones and its median came out 0.152 km against a true ~0.47. Every fix
    here feeds it a measurement from the unbiased source, so the estimate that
    caused these stops being wrong in the same direction.

    Dry run by default.
    """
    vehicle = _first_vehicle(session)
    since = sync_mod.now_local() - timedelta(days=days)
    drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id,
                            Drive.start_time >= since)
        .order_by(Drive.start_time)
    ).all()
    readings = session.scalars(
        select(BatteryReading).where(BatteryReading.vehicle_id == vehicle.id,
                                     BatteryReading.ts >= since)
        .order_by(BatteryReading.ts)
    ).all()
    found = driving_analysis.odometer_continuity(list(drives), list(readings))

    repaired: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    for g in found.get("gaps", []):
        closed_id, open_id = g.get("drive_id"), g.get("next_drive_id")
        boundary = g.get("boundary_odo_km")
        if not (closed_id and open_id and boundary):
            continue
        if g["unrecorded_km"] > sync_mod.ARRIVAL_EST_MAX_KM:
            manual.append({
                **g,
                "why": (f"{g['unrecorded_km']} km is past ARRIVAL_EST_MAX_KM "
                        f"({sync_mod.ARRIVAL_EST_MAX_KM}) — likelier a drive "
                        f"nobody logged than an arrival tail"),
                "run": (f"/api/repair-trip-boundary?closed_id={closed_id}"
                        f"&open_id={open_id}&boundary_odo_km={boundary}"
                        f"  (only if the car was merely repositioned)"),
            })
            continue
        try:
            plan = repair_trip_boundary(
                closed_id=closed_id, open_id=open_id, boundary_odo_km=boundary,
                closed_end_time=None, closed_end_coords=None,
                apply=apply, session=session)
        except HTTPException as exc:
            manual.append({**g, "why": f"refused: {exc.detail}", "run": None})
            continue
        repaired.append(plan)

    return {
        "days": days,
        "trips_checked": found.get("trips_checked", 0),
        "readings_checked": len(readings),
        "applied": apply,
        "repaired": len(repaired),
        "reclaimed_km": round(sum(r["delta_km"] for r in repaired), 3),
        "needs_a_human": len(manual),
        "repairs": repaired,
        "manual": manual,
        "note": ("No parked readings in this window — nothing could be checked."
                 if not readings else
                 "Every trip stopped where the car was seen resting."
                 if not repaired and not manual else
                 ("Dry run. Add &apply=true to write these." if not apply else None)),
    }


@router.get("/capacity-evidence")
def capacity_evidence(
    min_swing_pct: float = Query(15.0, ge=1.0, le=90.0),
    session: Session = Depends(get_session),
):
    """What this car's own charges say its usable pack is.

    The constant in use comes from spec minus measured degradation, and four
    readings off the car's energy screen have disagreed with it the same way
    every time — 69.01, 68.69, 68.14, 68.82 against 69.5. Four screenshots is
    a thin basis for changing a number every kWh and every ringgit depends on,
    and the arithmetic behind them has already been revised twice.

    Every charge is an independent measurement of the same quantity and none
    have been looked at. A session adds a known energy and moves SoC a known
    amount; the ratio is the pack. No screenshots, no window bookkeeping, and
    the sample grows on its own.

    The precision is the whole story, so it is reported rather than assumed.
    SoC is whole percent, so a session's ratio is only as sharp as its SWING:
    a 40-point charge resolves the pack to about +/-1%, a 5-point charge to
    +/-10% and says nothing. That is the same quantisation lesson the parked
    rate had to learn the hard way — a fit over samples too small to measure
    returns a confident number built from rounding.

    ``min_swing_pct`` sets the floor for the headline figure. Every session is
    listed regardless, with its own precision, so the excluded ones can be
    seen rather than silently dropped.

    Read-only, and deliberately not wired into the capacity actually used:
    this is evidence for a decision, not the decision.
    """
    vehicle = _first_vehicle(session)
    in_use, source = _usable_capacity(session, vehicle, get_settings())
    charges = session.scalars(
        select(Charge).where(Charge.vehicle_id == vehicle.id)
        .order_by(Charge.start_time.desc())
    ).all()

    rows: list[dict[str, Any]] = []
    for c in charges:
        swing = (c.end_soc or 0) - (c.start_soc or 0)
        if swing <= 0 or not c.energy_added_kwh:
            continue
        implied = c.energy_added_kwh / (swing / 100.0)
        # A charger reports what went IN; the AC efficiency correction is what
        # reached the pack. sync.capacity_from_charge has always applied this,
        # and reporting the uncorrected figure here made the evidence disagree
        # with the estimator it is evidence for.
        if (c.charge_type or "AC") != "DC":
            implied *= sync_mod.AC_CHARGE_EFFICIENCY
        # One SoC point at each end, so the swing is +/-1 point in total.
        precision = 1.0 / swing * 100.0
        rows.append({
            "charge_id": c.id, "at": c.start_time.isoformat(timespec="minutes"),
            # Which correction was applied, and the only way to read the
            # result: DC rows are already pack-side, AC rows carry the
            # efficiency factor, and mixing them without knowing which is which
            # makes the number uninterpretable.
            "charge_type": c.charge_type or "AC",
            "soc": [c.start_soc, c.end_soc], "swing_pct": round(swing, 1),
            "energy_added_kwh": round(c.energy_added_kwh, 3),
            "implied_capacity_kwh": round(implied, 2),
            "precision_pct": round(precision, 1),
            "counts": swing >= min_swing_pct,
        })

    good = sorted(r["implied_capacity_kwh"] for r in rows if r["counts"])
    headline = round(percentile(good, 0.5), 2) if good else None
    # The widest sessions on their own. Measured on this car's history the
    # precision column predicts the scatter almost exactly — sessions of 50
    # points and up agreed to 1.3%, 15-49 points to 3.0%, and under 15 points
    # spanned 22.3% — so the tightest subset is worth reporting separately
    # rather than being averaged in with samples an order of magnitude vaguer.
    widest = sorted(r["implied_capacity_kwh"] for r in rows if r["swing_pct"] >= 50)
    return {
        "in_use": {"kwh": in_use, "source": source},
        "min_swing_pct": min_swing_pct,
        "charges_seen": len(rows),
        "charges_counted": len(good),
        "median_implied_kwh": headline,
        "range_kwh": [good[0], good[-1]] if good else None,
        "widest_sessions": {
            "count": len(widest),
            "median_kwh": round(percentile(widest, 0.5), 2) if widest else None,
            "range_kwh": [widest[0], widest[-1]] if widest else None,
        },
        # The energy a charger reports is what went IN. Any of it lost to heat
        # never reached the pack, so this reads HIGH by exactly that much —
        # which matters, because the constant it is being compared against is
        # already suspected of reading high.
        "caveat": (f"AC sessions carry the {sync_mod.AC_CHARGE_EFFICIENCY} "
                   f"efficiency correction, same as sync.capacity_from_charge, "
                   f"so these are pack-side figures rather than charger-side."),
        "screen_readings": [69.01, 68.69, 68.14, 68.82],
        "charges": rows[:40],
        "note": ("No charge has a swing wide enough to measure with — raise the "
                 "sample by charging in bigger sessions, or lower min_swing_pct "
                 "and read the precision column."
                 if not good else None),
    }


@router.get("/continuity")
def continuity(
    days: int = Query(30, ge=1, le=730),
    session: Session = Depends(get_session),
):
    """Each trip's recorded stop against where the car was actually seen resting.

    The check trip-gaps cannot perform. Boundary continuity asks whether one
    trip's end equals the next one's start, and a trip that closes short passes
    that test perfectly whenever the following departure recovery reaches back
    over the same ground: every metre is claimed exactly once, just by the
    wrong trip. Distance stays continuous, totals stay right, and the energy
    lands on the wrong side of a boundary.

    Only the readings taken while the car sat parked can expose it, because
    they are the one measurement here that no derived figure feeds. The car
    came to rest where the odometer says it did, whatever the trip recorded.

    This has been computed on every dashboard load since it was written and
    never displayed, so nothing has ever looked at the result — which is why
    it is an endpoint now. Measured live and pointing straight at it: trip 391
    logged 0.56 km short at its arrival, and across 134 km the app accounted
    for 1.9% less driving energy than the car did, while every odometer
    boundary agreed.
    """
    vehicle = _first_vehicle(session)
    since = sync_mod.now_local() - timedelta(days=days)
    drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id,
                            Drive.start_time >= since)
        .order_by(Drive.start_time)
    ).all()
    readings = session.scalars(
        select(BatteryReading).where(BatteryReading.vehicle_id == vehicle.id,
                                     BatteryReading.ts >= since)
        .order_by(BatteryReading.ts)
    ).all()
    out = driving_analysis.odometer_continuity(list(drives), list(readings))
    out["days"] = days
    out["readings_checked"] = len(readings)
    # Same discipline as trip-gaps: a verdict has to say what it could see.
    # With no parked readings this check is blind, and blind is not clean.
    out["note"] = (
        "No parked readings in this window — nothing could be checked."
        if not readings else
        "Every trip stopped where the car was seen resting."
        if not out.get("gaps") else
        f"{len(out['gaps'])} trip(s) recorded a stop short of where the car was "
        f"actually seen. The distance is not missing — the next trip most "
        f"likely claimed it — so this is misattribution, not a hole.")
    return out


@router.get("/energy-reconcile")
def energy_reconcile(session: Session = Depends(get_session)):
    """Where the battery went since the last charge, and how much is unexplained.

    The car reports this two ways and they do not agree. Its battery meter is a
    raw SoC delta — 38% on the reading that prompted this. Its energy screen
    sums attributed categories instead, driving on one tab and parked draw on
    another, and those came to 36.7%. Both are correct; they answer different
    questions, and the 1.3 points between them is energy the car measured
    leaving the pack without filing it anywhere, plus the BMS quietly revising
    its estimate of what was there.

    This app has the same two quantities and has never compared them. Trips
    plus vampire drain is the attributed side; the SoC the last charge ended
    on, minus the SoC now, is the raw side. Their difference is the residual,
    and it is the one number that can catch a whole class of fault at once —
    a drive nobody logged, a parked gap mis-measured, an energy figure
    modelled too high — without knowing in advance which happened.

    A residual of a point or two is ordinary and is what the car itself shows.
    Several points is a missing trip or a broken figure, and worth chasing.
    Signed, because the direction says which way: positive means the battery
    fell further than anything here accounts for, negative means this app has
    claimed more energy than actually left the pack.
    """
    vehicle = _first_vehicle(session)
    capacity_kwh = _usable_capacity(session, vehicle, get_settings())[0]
    last_charge = session.scalar(
        select(Charge).where(Charge.vehicle_id == vehicle.id)
        .order_by(Charge.end_time.desc())
    )
    if last_charge is None or not capacity_kwh:
        raise HTTPException(
            409, "No charge on record to measure from, so there is no window.")
    import json as _json
    live = _json.loads(
        state.get(session, state.scoped(state.LAST_STATUS_KEY, vehicle.vin)) or "{}")
    now_soc = live.get("soc")
    if now_soc is None:
        raise HTTPException(
            409, "No current SoC reading, so the raw side cannot be measured.")

    drives_since = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id,
                            Drive.start_time >= last_charge.end_time)
        .order_by(Drive.start_time)
    ).all()
    # No charges list: by definition nothing has charged since the most recent
    # charge, so every gap in this window is a pure-drain measurement.
    vampire = driving_analysis.vampire_drain(
        drives_since, [], capacity_kwh,
        anchor=(last_charge.end_time, last_charge.end_soc))
    trips_kwh = round(sum(d.energy_used_kwh or 0.0 for d in drives_since), 3)
    parked_kwh = round(vampire["kwh"], 3)
    attributed_kwh = round(trips_kwh + parked_kwh, 3)

    raw_pct = round(last_charge.end_soc - float(now_soc), 2)
    attributed_pct = round(attributed_kwh / capacity_kwh * 100.0, 2)
    residual_pct = round(raw_pct - attributed_pct, 2)
    return {
        "since_charge_id": last_charge.id,
        "since": last_charge.end_time.isoformat(timespec="minutes"),
        "capacity_kwh": capacity_kwh,
        "raw": {"start_soc": last_charge.end_soc, "now_soc": now_soc,
                "pct": raw_pct, "kwh": round(raw_pct / 100.0 * capacity_kwh, 3)},
        "attributed": {"trips": trips_kwh, "trips_count": len(drives_since),
                       "parked": parked_kwh, "kwh": attributed_kwh,
                       "pct": attributed_pct},
        "residual": {"pct": residual_pct,
                     "kwh": round(residual_pct / 100.0 * capacity_kwh, 3),
                     "reading": ("battery fell further than anything here "
                                 "accounts for" if residual_pct > 0 else
                                 "this app claims more energy than left the pack"
                                 if residual_pct < 0 else "exact")},
        "distance_km": round(sum(d.distance_km or 0.0 for d in drives_since), 1),
        # The car's own gap between its two figures was 1.3 points over a
        # comparable window, so that is the bar, not zero.
        "note": ("Within the car's own attribution gap." if abs(residual_pct) <= 2.0
                 else "Larger than the car's own gap — worth checking "
                      "/api/trip-gaps for a drive nobody logged."),
    }


@router.api_route("/repair-all", methods=["GET", "POST"])
def repair_all(
    days: int = Query(730, ge=1, le=3650),
    min_km: float = Query(0.02, ge=0.001),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Reclaim every lost departure the trips themselves already measured.

    The single-trip repairs each need a number off the car's screen, because
    a hole in the odometer has two possible owners: the next trip's missing
    head, or a whole journey nobody logged. Two odometer readings cannot tell
    those apart, so a human has been reading the answer off the dash one trip
    at a time.

    For one class of hole that is unnecessary, and it is the class that keeps
    recurring. ``start_lost_km`` is the sync's OWN record of distance it
    watched this trip cover before its anchor and then declined to claim. When
    that figure matches the hole in front of the trip, nothing is being
    inferred: the loss was measured at the time, attributed to this trip at
    the time, and merely not acted on. Reclaiming it applies a decision the
    data already contains.

    A drive nobody logged looks different and is left alone — measured, the
    4.15 km Home->Bayan Mutiara leg left no trip AND no start_lost_km on its
    successor, so the hole sits there with nothing claiming it. Overlaps are
    left alone too: two trips claiming one stretch is a question about which
    owns it, and guessing would bury the evidence.

    ENERGY for the reclaimed stretch is priced at the trip's own flat average,
    with no departure premium. That differs from the single-trip repair, and
    deliberately. The premium prices a crawl (see DEPARTURE_BLIND_LOAD), the
    single repair keeps it because a human is watching and can correct it
    against the car, and this sweep has neither a pace to test nor anyone
    reading the result. The measured heads long enough to reach this sweep
    have come back at the flat average anyway — 378 at 1.10, 366 at 0.92, 359
    at 1.10, 382 at ~1.0 — while the 1.55 the premium encodes was fitted on
    two heads of about a kilometre. Flat is the better expectation unwatched.

    Dry run by default. Everything it declines is returned with the exact
    single-trip repair to run by hand.
    """
    vehicle = _first_vehicle(session)
    since = sync_mod.now_local() - timedelta(days=days)
    drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id, Drive.start_time >= since)
        .order_by(Drive.start_time)
    ).all()

    repaired: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    # Pairs with a missing odometer at either end are not checked, and saying
    # "every boundary agrees" without saying how many were LOOKED at is the
    # same false reassurance that cost this project a week elsewhere — a sync
    # log that read silent while it was really erroring, a data_quality that
    # said "measured" over a trip half inferred. An unchecked pair is not a
    # clean one.
    skipped = 0
    for prev_d, nxt in zip(drives, drives[1:]):
        if prev_d.end_odo_km is None or nxt.start_odo_km is None:
            skipped += 1
            continue
        gap = round(nxt.start_odo_km - prev_d.end_odo_km, 3)
        if abs(gap) < min_km:
            continue
        parked_min = round(
            max((nxt.start_time - prev_d.end_time).total_seconds(), 0.0) / 60.0, 1)
        lost = nxt.start_lost_km or 0.0
        base = {"drive_id": nxt.id, "gap_km": gap, "parked_min": parked_min,
                "at": nxt.start_time.isoformat(timespec="minutes")}
        if gap < 0:
            manual.append({**base, "why": "overlap — two trips claim this ground",
                           "run": f"/api/repair-trip-overlap?drive_id={nxt.id}"})
            continue
        # The sweep's whole warrant: the trip measured this exact loss itself.
        # Tolerance is the odometer's own rounding, not a margin for argument.
        if abs(gap - lost) > 0.05:
            manual.append({
                **base, "start_lost_km": nxt.start_lost_km,
                "why": ("no trip claims this ground — likely a drive that was "
                        "never logged" if lost <= 0 else
                        f"the hole ({gap} km) and the trip's own recorded loss "
                        f"({lost} km) disagree"),
                "run": (f"/api/repair-missing-trip?start_time="
                        f"{prev_d.end_time.isoformat(timespec='minutes')}"
                        f"&end_time={nxt.start_time.isoformat(timespec='minutes')}"
                        if lost <= 0 else
                        f"/api/repair-lost-departure?drive_id={nxt.id}"
                        f"&true_distance_km=<car's figure>"),
            })
            continue

        new_span = round(nxt.end_odo_km - prev_d.end_odo_km, 3)
        new_distance = round(new_span, 1)
        new_energy = (round(sync_mod.energy_for_blind_distance(
            nxt.energy_used_kwh, new_span, gap), 2)
            if nxt.energy_used_kwh else nxt.energy_used_kwh)
        repaired.append({
            **base,
            "from_trip": {"id": prev_d.id, "end_location": prev_d.end_location},
            "start_odo_km": [nxt.start_odo_km, round(prev_d.end_odo_km, 3)],
            "distance_km": [nxt.distance_km, new_distance],
            "energy_kwh": [nxt.energy_used_kwh, new_energy],
            "start_location": [nxt.start_location, prev_d.end_location],
        })
        if apply:
            nxt.start_odo_km = round(prev_d.end_odo_km, 3)
            nxt.distance_km = new_distance
            nxt.energy_used_kwh = new_energy      # wh_per_km derives from this
            nxt.start_recovered_km = round((nxt.start_recovered_km or 0.0) + gap, 3)
            nxt.start_lost_km = 0.0
            if prev_d.end_coords:
                nxt.start_coords = prev_d.end_coords
                nxt.start_location = prev_d.end_location
                nxt.start_area = prev_d.end_area
            if nxt.duration_min:
                nxt.avg_speed_kmh = round(new_distance / (nxt.duration_min / 60.0), 1)
    if apply and repaired:
        session.commit()

    # The CLOCK is deliberately not touched. A start time is only wrong by the
    # minutes the reclaimed stretch took, that duration was never observed, and
    # the pace constant that would estimate it is itself under review (11 to 41
    # km/h across four measured heads). Moving every start in the history on
    # that basis would be the one change here that cannot be checked afterwards.
    pairs = max(len(drives) - 1, 0)
    return {
        "trips_checked": len(drives),
        "boundaries_checked": pairs - skipped,
        # Not a footnote: a boundary with no odometer at one end was never
        # examined, so it can be neither clean nor broken here.
        "boundaries_unchecked": skipped,
        "days": days,
        "applied": apply,
        "repaired": len(repaired),
        "reclaimed_km": round(sum(r["gap_km"] for r in repaired), 3),
        "needs_a_human": len(manual),
        "repairs": sorted(repaired, key=lambda r: -r["gap_km"]),
        "manual": sorted(manual, key=lambda m: -abs(m["gap_km"])),
        "note": (("Nothing to reclaim — every boundary the trips measured agrees."
                  + (f" {skipped} boundary(s) had no odometer to check."
                     if skipped else ""))
                 if not repaired and not manual else
                 ("Dry run. Add &apply=true to write these." if not apply else None)),
    }


def _gaps_note(skipped: int, bad_blocks: int, unreachable: int = 0) -> str:
    """The verdict, qualified by how much of the history it actually covers.

    "Every trip boundary agrees" over a table half of which has no odometer is
    true and useless. Where anchors are missing, the reconciliation between the
    readings either side is what the sentence has to rest on instead — and
    where there is no reading on one side, nothing rests on anything and the
    sentence has to say so. A run of unanchored trips at the start of the
    history is the ordinary case for that, and silently reporting no failing
    blocks across it would be the reassuring reading of an ambiguity again.
    """
    if not skipped:
        return "Every trip boundary agrees."
    parts = ["Every anchored boundary agrees"]
    reconciled = skipped - unreachable
    if bad_blocks:
        parts.append(f"but {bad_blocks} of the spans without anchors do NOT "
                     f"reconcile against the readings either side")
    elif reconciled > 0:
        parts.append(f"and {reconciled} boundary(s) without anchors sit in "
                     f"spans that reconcile against the readings either side")
    if unreachable:
        parts.append(f"but {unreachable} boundary(s) have no anchor and no "
                     f"reading on one side to reconcile against — nothing has "
                     f"checked those")
    return ", ".join(parts) + "."


@router.get("/trip-gaps")
def trip_gaps(
    days: int = Query(30, ge=1, le=730),
    min_km: float = Query(0.01, ge=0.001),
    from_odo_km: float | None = Query(None),
    from_time: str | None = Query(None),
    session: Session = Depends(get_session),
):
    """Where consecutive trips disagree about the odometer.

    The odometer only counts forward, so one trip's end and the next one's
    start must be the same reading. Every fault this project has spent weeks
    on shows up here as a number: a departure recovered short (42 m, 90 m), a
    departure never recovered at all (4.16 km), a drive that was never logged
    (4.15 km), a trip that swallowed two journeys (an overlap). All of them
    were found by scrolling the trip list days later, because nothing checked.

    A HOLE is distance the car really covered that belongs to no trip. An
    OVERLAP is the opposite and worse: two trips claiming the same ground, so
    every window total counts it twice.

    ``parked_min`` is the discriminator the numbers can't supply on their own.
    A hole with minutes between the trips is almost always a departure the
    poll missed; a hole with hours between them is more likely a whole drive
    nobody logged. It is a hint, not a verdict — only the owner knows.
    """
    vehicle = _first_vehicle(session)
    since = sync_mod.now_local() - timedelta(days=days)
    drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id, Drive.start_time >= since)
        .order_by(Drive.start_time)
    ).all()

    findings: list[dict[str, Any]] = []
    # See repair-all: a pair with no odometer at one end is never examined, and
    # folding it into "every trip boundary agrees" makes an absence of evidence
    # read as evidence of absence.
    skipped = 0
    for prev_d, nxt in zip(drives, drives[1:]):
        if prev_d.end_odo_km is None or nxt.start_odo_km is None:
            skipped += 1
            continue
        gap = round(nxt.start_odo_km - prev_d.end_odo_km, 3)
        if abs(gap) < min_km:
            continue
        parked_min = round(
            max((nxt.start_time - prev_d.end_time).total_seconds(), 0.0) / 60.0, 1)
        if gap < 0:
            fix = ("overlap — two trips claim the same ground; "
                   "repair-trip-overlap")
        elif (nxt.start_lost_km or 0) > 0:
            fix = ("the later trip measured this loss and didn't reclaim it; "
                   "repair-lost-departure")
        elif parked_min >= 45:
            fix = ("hours parked between them — likely a drive that was never "
                   "logged; repair-missing-trip")
        else:
            fix = ("minutes between them — likely a departure the poll missed; "
                   "repair-lost-departure, or repair-missing-trip if it was a "
                   "separate journey")
        findings.append({
            "gap_km": gap,
            "parked_min": parked_min,
            "after": {"id": prev_d.id, "at": prev_d.end_time.isoformat(timespec="minutes"),
                      "place": prev_d.end_location, "odo_km": prev_d.end_odo_km},
            "before": {"id": nxt.id, "at": nxt.start_time.isoformat(timespec="minutes"),
                       "place": nxt.start_location, "odo_km": nxt.start_odo_km},
            # What the later trip itself recorded about its own start, which is
            # evidence about whether the loss was even noticed at the time.
            "start_lost_km": nxt.start_lost_km,
            "start_recovered_km": nxt.start_recovered_km,
            "suggested": fix,
        })

    # The boundary check above can only speak for trips that carry odometer
    # anchors, and start_odo_km/end_odo_km were added to an existing table
    # (database.py) — so every trip written before that migration has none.
    # Measured live: 61 of 131 boundaries, 47% of the history, unexaminable.
    #
    # Chaining odometers backwards through those from their distances would
    # make the gap check pass by construction, since it would be ASSUMING the
    # continuity the check exists to test. Worthless, and worse than worthless
    # because it reads as a clean result.
    #
    # This is the check that does work on them. Between any two trips that do
    # carry real readings, the odometer moved a known amount, and the trips in
    # between claim a known total. Those must agree whatever the trips in the
    # middle recorded about themselves — a shortfall is ground no trip claims,
    # an excess is ground claimed twice. Nothing is assumed about the block;
    # it is measured from outside.
    #
    # It reconciles a block only when anchors exist on BOTH sides of it. A run
    # of unanchored trips at the very start of the history — which is exactly
    # what a migration that added the columns leaves behind — has no earlier
    # reading to measure from, so no pair of anchors brackets it and it is
    # never examined. Counted, never folded into the verdict: reporting zero
    # failing blocks over trips nothing could look at is the same false
    # all-clear as before, one level further in.
    # ``from_odo_km`` supplies the reading history does not have: the odometer
    # when the oldest trip in this window began. One number off a delivery
    # record or a dated photo of the dash, and the whole leading run becomes
    # measurable — it is an anchor on the missing side, so reconciling against
    # it assumes nothing the trips themselves recorded.
    #
    # This is the one legitimate version of "supply the missing odometer". The
    # illegitimate one is deriving it from the trips' own distances, which
    # would make every check downstream pass by construction.
    blocks: list[dict[str, Any]] = []
    anchored = [i for i, d in enumerate(drives)
                if d.start_odo_km is not None and d.end_odo_km is not None]
    unbracketed = 0
    if anchored:
        for i in range(len(drives) - 1):
            if (drives[i].start_odo_km is not None
                    and drives[i].end_odo_km is not None
                    and drives[i + 1].start_odo_km is not None
                    and drives[i + 1].end_odo_km is not None):
                continue
            if i < anchored[0] or i > anchored[-1] - 1:
                unbracketed += 1
    else:
        unbracketed = max(len(drives) - 1, 0)
    implied = None
    if anchored and anchored[0] > 0:
        implied = round(drives[anchored[0]].start_odo_km
                        - sum(d.distance_km or 0.0 for d in drives[:anchored[0]]), 1)
    if from_odo_km is not None and anchored and anchored[0] > 0:
        # ``from_time`` places the reading in history. Without it the reading is
        # taken to be at the oldest trip's start; with it, only the trips after
        # that moment are counted against the span.
        #
        # It matters because the readings people actually have are dated photos
        # of the dash, and one from BEFORE the first recorded trip cannot
        # anchor anything: the ground between it and the app's first trip was
        # driven before the app existed, so it would surface as a hole that is
        # not a fault. Measured: a photo reading 27,716 against an oldest trip
        # starting at 28,291.4 — 575 km of pre-history that no record covers.
        lead = drives[:anchored[0]]
        if from_time:
            try:
                cutoff = datetime.fromisoformat(from_time)
            except ValueError:
                raise HTTPException(
                    400, "from_time must be ISO 8601, e.g. 2026-07-05T15:12")
            lead = [d for d in lead if d.start_time >= cutoff]
        claimed = round(sum(d.distance_km or 0.0 for d in lead), 3)
        span = round(drives[anchored[0]].start_odo_km - from_odo_km, 3)
        diff = round(span - claimed, 3)
        unbracketed = max(unbracketed - len(lead), 0)
        if abs(diff) >= min_km:
            blocks.append({
                "trips": len(lead),
                "from": {"id": None,
                         "at": from_time or "start of history (from_odo_km)",
                         "odo_km": from_odo_km},
                "to": {"id": drives[anchored[0]].id,
                       "at": drives[anchored[0]].start_time.isoformat(timespec="minutes"),
                       "odo_km": drives[anchored[0]].start_odo_km},
                "odometer_moved_km": span,
                "trips_claim_km": claimed,
                "difference_km": diff,
                "reading": ("distance no trip claims" if diff > 0
                            else "distance claimed twice"),
            })
    for a, b in zip(anchored, anchored[1:]):
        if b == a + 1:
            continue                       # adjacent: the boundary check has it
        between = drives[a + 1:b]
        claimed = round(sum(d.distance_km or 0.0 for d in between), 3)
        span = round(drives[b].start_odo_km - drives[a].end_odo_km, 3)
        diff = round(span - claimed, 3)
        if abs(diff) < min_km:
            continue
        blocks.append({
            "trips": len(between),
            "from": {"id": drives[a].id, "at": drives[a].end_time.isoformat(timespec="minutes"),
                     "odo_km": drives[a].end_odo_km},
            "to": {"id": drives[b].id, "at": drives[b].start_time.isoformat(timespec="minutes"),
                   "odo_km": drives[b].start_odo_km},
            "odometer_moved_km": span,
            "trips_claim_km": claimed,
            "difference_km": diff,
            "reading": ("distance no trip claims" if diff > 0
                        else "distance claimed twice"),
        })

    holes = [f for f in findings if f["gap_km"] > 0]
    overlaps = [f for f in findings if f["gap_km"] < 0]
    return {
        "trips_checked": len(drives),
        "boundaries_checked": max(len(drives) - 1, 0) - skipped,
        "boundaries_unchecked": skipped,
        "days": days,
        "holes": len(holes),
        "overlaps": len(overlaps),
        "unaccounted_km": round(sum(f["gap_km"] for f in holes), 3),
        "double_counted_km": round(-sum(f["gap_km"] for f in overlaps), 3),
        # Biggest first — the one worth fixing is rarely the most recent.
        "findings": sorted(findings, key=lambda f: -abs(f["gap_km"]))[:50],
        # Reconciliation of the spans the boundary check cannot see into.
        "unanchored_blocks": len(blocks),
        "unanchored_difference_km": round(sum(b["difference_km"] for b in blocks), 3),
        "blocks": sorted(blocks, key=lambda b: -abs(b["difference_km"]))[:50],
        # Boundaries with no anchor AND no anchored trip on one side to
        # reconcile against. Neither check reaches these; nothing here has
        # ever looked at them.
        "boundaries_unreachable": unbracketed,
        # How to make the unreachable ones reachable, when there are any.
        "hint": (None if not unbracketed else
                 f"Pass &from_odo_km=NUMBER (the odometer when the oldest trip "
                 f"here began) to reconcile the leading run. Its own trips "
                 f"imply {implied}; if a real record says otherwise, the "
                 f"difference is the discrepancy. A reading from BEFORE the "
                 f"oldest trip anchors nothing — pass &from_time=ISO with it "
                 f"if it sits mid-history."),
        # Which reading would be usable, in the terms a person can go and look
        # for one: a dated photo of the dash reading MORE than this, from AFTER
        # this moment. Anything earlier is pre-history and anchors nothing.
        "oldest_trip_at": (drives[0].start_time.isoformat(timespec="minutes")
                           if drives else None),
        # ...and the upper bound. Trips from here on carry their own anchors,
        # so a reading after this proves nothing that is not already proven.
        # The pair turns "find an old photo" into a dated window to search.
        "anchors_begin_at": (drives[anchored[0]].start_time.isoformat(timespec="minutes")
                             if anchored and anchored[0] > 0 else None),
        # What the leading run's own distances imply the opening reading was.
        # NOT a verification — it is derived from the very trips in question,
        # so feeding it back would reconcile by construction. It is a figure to
        # COMPARE a delivery record or a dated photo of the dash against, which
        # is the one thing the data cannot supply for itself.
        "implied_start_odo_km": implied,
        "note": (_gaps_note(skipped, len(blocks), unbracketed)
                 if not findings else None),
    }


@router.api_route("/repair-missing-trip", methods=["GET", "POST"])
def repair_missing_trip(
    start_time: str = Query(...),
    end_time: str = Query(...),
    distance_km: float | None = Query(None),
    energy_kwh: float | None = Query(None),
    start_location: str | None = Query(None),
    end_location: str | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Record a drive that happened but was never seen.

    Every other repair edits a trip that exists. This one fills a hole: a
    stretch where the odometer moved between two logged trips and nothing was
    written for it. Measured — a 4.15 km Home->Bayan Mutiara leg that left no
    trip, and not even a start_lost_km on its successor.

    Only the TIMES are required, because everything else is already implied by
    the hole. The trip before it ended somewhere, at some odometer; the trip
    after it began somewhere, at some odometer; and this drive is exactly what
    joined them. So the odometer span, the distance and both place names all
    default to the neighbours' own boundary, which is also the only way the
    result can leave the odometer continuous — the property every trip-boundary
    check downstream depends on.

    ENERGY is unknown unless given. Nothing measured this drive, and a plausible
    number here would be indistinguishable from a reading. Given one, the end
    SoC follows from it; omitted, the SoC pair is flattened so the trip cannot
    report a drop it can't account for, and the drain stays with the parked gap
    after it — the same treatment repair_split_trip gives an unwatched leg.

    Dry run by default.
    """
    def _when(raw: str) -> datetime:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            raise HTTPException(409, f"'{raw}' is not an ISO datetime.")

    begin, finish = _when(start_time), _when(end_time)
    if finish <= begin:
        raise HTTPException(409, "'end_time' must be after 'start_time'.")

    vehicle = _first_vehicle(session)
    # Checked FIRST, ahead of everything else. An overlap is the one thing no
    # later repair can undo — two trips claiming the same minutes make every
    # window total wrong — and it also produces the clearest message: times
    # landing inside an existing trip would otherwise fail further down as
    # "no trip on both sides", which describes the symptom, not the mistake.
    clash = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id,
                            Drive.start_time < finish, Drive.end_time > begin)
        .limit(1)
    ).first()
    if clash is not None:
        raise HTTPException(
            409, f"Those times overlap trip {clash.id} "
                 f"({clash.start_time:%Y-%m-%d %H:%M} - {clash.end_time:%H:%M}).")
    prev_d = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id, Drive.end_time <= begin)
        .order_by(Drive.end_time.desc()).limit(1)
    ).first()
    next_d = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id, Drive.start_time >= finish)
        .order_by(Drive.start_time).limit(1)
    ).first()
    if prev_d is None or next_d is None:
        raise HTTPException(
            409, "Need a logged trip on both sides to place this one between — "
                 "the hole is defined by its edges.")

    gap_km = (round(next_d.start_odo_km - prev_d.end_odo_km, 3)
              if prev_d.end_odo_km is not None and next_d.start_odo_km is not None
              else None)
    if gap_km is None:
        raise HTTPException(
            409, "The neighbouring trips have no odometer anchors, so there is "
                 "no hole to measure.")
    if gap_km <= 0:
        raise HTTPException(
            409, f"Trip {prev_d.id} ends where trip {next_d.id} begins "
                 f"(gap {gap_km} km). There is no missing distance between them.")
    span = round(distance_km if distance_km is not None else gap_km, 3)
    if span <= 0 or span > gap_km + 0.05:
        raise HTTPException(
            409, f"{span} km doesn't fit the {gap_km} km hole between trips "
                 f"{prev_d.id} and {next_d.id}.")

    mins = round((finish - begin).total_seconds() / 60.0, 1)
    start_odo = round(prev_d.end_odo_km, 3)
    end_odo = round(start_odo + span, 3)
    energy = round(energy_kwh, 2) if energy_kwh else 0.0
    capacity_kwh = _usable_capacity(session, vehicle, get_settings())[0]
    end_soc = (round(prev_d.end_soc - energy / capacity_kwh * 100.0, 1)
               if energy and capacity_kwh else prev_d.end_soc)
    start_place = start_location if start_location is not None else prev_d.end_location
    end_place = end_location if end_location is not None else next_d.start_location

    plan = {
        "between": {"after": prev_d.id, "before": next_d.id},
        "hole_km": gap_km,
        "route": [start_place, end_place],
        "odo": [start_odo, end_odo],
        "distance_km": round(span, 1),
        "duration_min": mins,
        "energy_kwh": energy or None,
        "start_time": begin.isoformat(timespec="minutes"),
        "end_time": finish.isoformat(timespec="minutes"),
        "leaves_gap_km": round(gap_km - span, 3),
        "applied": apply,
    }
    if apply:
        drive = Drive(
            vehicle_id=vehicle.id, start_time=begin, end_time=finish,
            distance_km=round(span, 1), duration_min=mins,
            start_soc=prev_d.end_soc, end_soc=end_soc, energy_used_kwh=energy,
            avg_speed_kmh=round(span / max(mins / 60.0, 1e-9), 1),
            # No speed was ever observed, so the average is the honest floor —
            # the same fallback _drive_from uses for a trip with no mid-drive
            # reading.
            max_speed_kmh=round(span / max(mins / 60.0, 1e-9), 1),
            outside_temp_c=prev_d.outside_temp_c,
            start_location=start_place, start_area=prev_d.end_area,
            start_coords=prev_d.end_coords,
            end_location=end_place, end_area=next_d.start_area,
            end_coords=next_d.start_coords,
            start_odo_km=start_odo, end_odo_km=end_odo,
            # Nothing about this trip was polled, so every field that records
            # how well a boundary was seen stays empty rather than claiming a
            # window that never existed.
            start_lost_km=0.0, start_recovered_km=0.0, end_lost_km=0.0,
            idle_tracked=False, tag=prev_d.tag,
        )
        session.add(drive)
        session.commit()
        plan["drive_id"] = drive.id
    return plan


@router.api_route("/repair-split-trip", methods=["GET", "POST"])
def repair_split_trip(
    drive_id: int = Query(...),
    boundary_odo_km: float = Query(...),
    first_start: str | None = Query(None),
    first_end: str | None = Query(None),
    second_start: str | None = Query(None),
    boundary_coords: str | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Cut one logged trip into the two journeys it actually was.

    A departure recovery that reached across a long blind gap can swallow a
    whole separate drive, the stop after it, and the start of the next one —
    measured, trip 368: Office->Home, a stop, and half of Home->Penang
    Retirement Resort logged as a single 15.665 km trip. Sync no longer does
    that, but a row already written keeps it, and no other repair can help:
    the boundary tools trade distance BETWEEN two trips, and here there is
    only one where two belong.

    ``boundary_odo_km`` is the odometer at the intermediate stop — the one
    quantity that is a measured fact rather than a guess, since it is where
    the two legs actually meet.

    TIMES cannot be derived. The gap is blind precisely because nothing was
    recorded in it, so the first leg's clock exists only in your memory: give
    ``first_start``/``first_end``/``second_start`` (ISO, e.g.
    2026-08-10T16:40). Omitted, the trip's own span is divided by distance and
    the result is an estimate wearing a measurement's clothes — fine for
    ordering, not for auditing.

    ENERGY follows what was actually watched. The leg with no measurement
    behind it gets none: energy unknown, shown as a dash, rather than a share
    invented by splitting a number that never covered it. The other keeps the
    measured energy, re-priced over its own blind head the way sync would.

    Dry run by default.
    """
    drive = session.get(Drive, drive_id)
    if drive is None:
        raise HTTPException(404, "No such trip.")
    if drive.start_odo_km is None or drive.end_odo_km is None:
        raise HTTPException(
            409, f"Trip {drive_id} has no odometer anchors, so there is nothing "
                 f"to cut against.")
    if not (drive.start_odo_km < boundary_odo_km < drive.end_odo_km):
        raise HTTPException(
            409, f"The boundary must fall inside the trip: "
                 f"{drive.start_odo_km} < {boundary_odo_km} < {drive.end_odo_km}.")

    def _when(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            raise HTTPException(409, f"'{raw}' is not an ISO datetime.")

    a_start = _when(first_start) or drive.start_time
    b_end = drive.end_time
    a_km = round(boundary_odo_km - drive.start_odo_km, 3)
    b_km = round(drive.end_odo_km - boundary_odo_km, 3)
    # Only used when the real times aren't given: divide the trip's own span
    # in proportion to distance, which at least orders the legs correctly.
    span_sec = max((b_end - a_start).total_seconds(), 0.0)
    a_end = _when(first_end) or (a_start + timedelta(
        seconds=span_sec * a_km / max(a_km + b_km, 1e-9)))
    b_start = _when(second_start) or a_end
    if not (a_start < a_end <= b_start < b_end):
        raise HTTPException(
            409, "Times must run in order: first_start < first_end <= "
                 "second_start < the trip's own end.")

    # Blind distance splits by where it sat: it is all at the trip's start, so
    # the first leg absorbs it until it runs out, and whatever is left belongs
    # to the second leg's own departure.
    blind = drive.start_recovered_km or 0.0
    a_blind = round(min(blind, a_km), 3)
    b_blind = round(max(blind - a_km, 0.0), 3)
    a_measured, b_measured = a_km - a_blind, b_km - b_blind
    # The trip's measured energy was carried entirely by whatever was watched,
    # and with the blind stretch at the front that is the tail of the second
    # leg. A leg with nothing measured gets no energy rather than a share of a
    # figure that never covered it.
    a_energy = 0.0 if a_measured <= 0 else round(
        (drive.energy_used_kwh or 0.0) * a_measured / max(a_measured + b_measured, 1e-9), 2)
    b_energy = round(sync_mod.energy_for_blind_distance(
        (drive.energy_used_kwh or 0.0) - a_energy, b_km, b_blind,
        departure_blind_km=b_blind), 2) if drive.energy_used_kwh else 0.0

    mid_coords = (boundary_coords or "").strip()
    mid_place, mid_area = (_place_and_area(mid_coords, session) if mid_coords
                           else ("", ""))

    def _mins(a: datetime, b: datetime) -> float:
        return round(max((b - a).total_seconds(), 0.0) / 60.0, 1)

    plan = {
        "drive_id": drive.id,
        "boundary_odo_km": round(boundary_odo_km, 3),
        "legs": [
            {"leg": 1, "route": [drive.start_location, mid_place or "(unknown)"],
             "odo": [drive.start_odo_km, round(boundary_odo_km, 3)],
             "distance_km": round(a_km, 1), "energy_kwh": a_energy or None,
             "blind_km": a_blind,
             "start_time": a_start.isoformat(timespec="minutes"),
             "end_time": a_end.isoformat(timespec="minutes"),
             "times_given": bool(first_start or first_end)},
            {"leg": 2, "route": [mid_place or "(unknown)", drive.end_location],
             "odo": [round(boundary_odo_km, 3), drive.end_odo_km],
             "distance_km": round(b_km, 1), "energy_kwh": b_energy or None,
             "blind_km": b_blind,
             "start_time": b_start.isoformat(timespec="minutes"),
             "end_time": b_end.isoformat(timespec="minutes"),
             "times_given": bool(second_start)},
        ],
        "was": {"distance_km": drive.distance_km,
                "energy_kwh": drive.energy_used_kwh,
                "route": [drive.start_location, drive.end_location]},
        "applied": apply,
    }
    if apply:
        second = Drive(
            vehicle_id=drive.vehicle_id,
            start_time=b_start, end_time=b_end,
            distance_km=round(b_km, 1), duration_min=_mins(b_start, b_end),
            # The measured SoC pair belongs to the watched stretch, which is in
            # this leg; energy_used_kwh above is the figure that actually gets
            # read, and it is set explicitly.
            start_soc=drive.start_soc, end_soc=drive.end_soc,
            energy_used_kwh=b_energy,
            avg_speed_kmh=round(b_km / max(_mins(b_start, b_end) / 60.0, 1e-9), 1),
            max_speed_kmh=drive.max_speed_kmh,
            outside_temp_c=drive.outside_temp_c,
            start_location=mid_place, start_area=mid_area, start_coords=mid_coords,
            end_location=drive.end_location, end_area=drive.end_area,
            end_coords=drive.end_coords,
            start_odo_km=round(boundary_odo_km, 3), end_odo_km=drive.end_odo_km,
            start_lost_km=0.0, start_recovered_km=b_blind,
            end_lost_km=drive.end_lost_km, end_est_km=drive.end_est_km,
            tail_trim_sec=drive.tail_trim_sec, end_gap_sec=drive.end_gap_sec,
            idle_tracked=False, tag=drive.tag,
        )
        session.add(second)
        # The original becomes the FIRST leg: it already carries the correct
        # start, so rewriting its end is the smaller change — and keeping its
        # id means anything referring to this trip still points at a real one.
        drive.end_time = a_end
        drive.start_time = a_start
        drive.distance_km = round(a_km, 1)
        drive.duration_min = _mins(a_start, a_end)
        drive.energy_used_kwh = a_energy
        drive.avg_speed_kmh = round(a_km / max(_mins(a_start, a_end) / 60.0, 1e-9), 1)
        drive.end_odo_km = round(boundary_odo_km, 3)
        drive.end_location, drive.end_area = mid_place, mid_area
        drive.end_coords = mid_coords
        # Its own SoC pair no longer describes anything measured — this leg was
        # never watched — so flatten it rather than leave a drop that would be
        # read as this drive's consumption.
        drive.end_soc = drive.start_soc
        drive.start_recovered_km, drive.start_lost_km = a_blind, 0.0
        drive.end_lost_km, drive.end_est_km, drive.tail_trim_sec = 0.0, None, None
        # Its arrival instrumentation belonged to the trip's OLD end, which is
        # now the second leg's — this leg ends at a repair boundary no poll
        # ever saw, so there is no polling window to report.
        drive.end_gap_sec = None
        # And a peak speed nothing on this leg measured. When the whole leg was
        # blind the observed maximum came from the other one, so fall back to
        # the same honest floor _drive_from uses when no mid-drive snapshot
        # exists: the average, which the car demonstrably reached.
        if a_measured <= 0:
            drive.max_speed_kmh = drive.avg_speed_kmh
        session.commit()
        plan["new_drive_id"] = second.id
    return plan


@router.get("/sync-log")
def sync_log(session: Session = Depends(get_session)):
    """What the polling loop has actually been doing, newest last.

    Exists because a gap in the record has several causes that look identical
    afterwards, and picking the wrong one wastes a day. Measured, trip 368:
    12.4 hours with no reading, two real drives inside it, and no way to tell
    whether the loop was skipping on the sleep back-off, finding the car
    unreadable, or simply not being called.

    Each run is an unbroken stretch of ticks that did the same thing:

      read     a snapshot was taken (vehicle_data)
      idle     the car was online but the read throttle declined it
      asleep   the car reported asleep/offline — nothing to read
      backoff  the sleep back-off skipped the tick before spending anything

    And ``no-tick``, which is not an outcome but a HOLE — a stretch longer
    than SYNC_SILENCE_MIN with nothing recorded at all. No tick can write
    that, which is exactly what makes it diagnostic: it means the request
    never arrived, so the fault is the cron or the host, not this app.
    """
    import json as _json

    runs = _json.loads(state.get(session, state.SYNC_LOG_KEY) or "[]")
    out: list[dict[str, Any]] = []
    prev_end = None

    def _clock(ts: float) -> str:
        # MYT, like every other timestamp this app stores or shows — see
        # sync._dt. Rendered with the server's own timezone instead, this read
        # eight hours off on the deployed host, and a night of back-off looked
        # like an afternoon of it while a drive that WAS being polled looked
        # like a gap. Exactly the confusion sync.now_local's docstring warns
        # about, in the one place built to end confusion.
        return datetime.fromtimestamp(ts, sync_mod.MYT).isoformat(timespec="minutes")

    def _silence(a: float, b: float) -> dict[str, Any]:
        return {
            "outcome": "no-tick", "minutes": round((b - a) / 60.0, 1),
            "from": _clock(a), "to": _clock(b),
            "note": "nothing ran — /api/sync was not called at all",
        }

    for r in runs:
        start, end = float(r.get("a") or 0), float(r.get("b") or 0)
        if prev_end is not None and start - prev_end > SYNC_SILENCE_MIN * 60:
            out.append(_silence(prev_end, start))
        row = {
            "outcome": r.get("o"),
            "ticks": r.get("n"),
            "minutes": round((end - start) / 60.0, 1),
            "from": _clock(start),
            "to": _clock(end),
        }
        if r.get("e"):
            row["error"] = r["e"]
        out.append(row)
        prev_end = end
    # A silence running up to NOW is the one that matters most — it is the
    # fault still happening — and it was the only one this couldn't see,
    # because a gap was only ever measured BETWEEN two runs. The log simply
    # stopped, and stopping is what it was built to report.
    if prev_end is not None and time.time() - prev_end > SYNC_SILENCE_MIN * 60:
        out.append({**_silence(prev_end, time.time()), "ongoing": True})
    silent = [r for r in out if r["outcome"] == "no-tick"]
    failed = [r for r in out if r["outcome"] == "error"]
    return {
        "runs": out,
        "silences": len(silent),
        "longest_silence_min": max((r["minutes"] for r in silent), default=0.0),
        # Surfaced separately because a run of these looks exactly like a dead
        # cron from the outside — the requests arrive and get a 500 — and the
        # two want opposite fixes.
        "errors": len(failed),
        "last_error": failed[-1]["error"] if failed else None,
        "note": ("No history yet — the log starts filling on the next tick."
                 if not runs else None),
    }


@router.api_route("/repair-trip-energy", methods=["GET", "POST"])
def repair_trip_energy(
    drive_id: int = Query(...),
    true_wh_per_km: float | None = Query(None),
    true_consumed_pct: float | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Replace an ESTIMATED trip energy with the car's own measurement.

    A departure recovered from a blackout brings its distance back but not
    always its energy: when the gap is longer than STALE_ANCHOR_MAX_MIN the
    SoC at the far end carries the park's standby drain, so taking it would
    move hours of idling into the drive. The blind stretch is priced instead —
    the trip's own measured rate, plus the departure premium over the opening
    kilometres (see energy_for_blind_distance).

    That is an extrapolation, and it is only as good as the assumption that the
    unseen stretch drove like the rest. Measured, both ways: trip 359's blind
    head cost 1.10x the rest of its drive, trip 366's 0.91x, against a premium
    that assumes 1.55x over the opening kilometres. Where the car has measured
    the whole drive, its figure beats any rate we can project.

    Give ``true_wh_per_km`` (the car's Current Drive readout) or
    ``true_consumed_pct`` (its energy-app percentage). Prefer the percentage
    where the two disagree — Tesla's trip meter has read ~2% below its own
    percentage on several trips here — though it is worth knowing that the
    percentage route multiplies by the capacity constant while Wh/km does not.

    Refuses a trip whose energy was measured end to end: there the reading is
    not an estimate, and a disagreement is a different fault that this must not
    bury. Dry run by default.
    """
    drive = session.get(Drive, drive_id)
    if drive is None:
        raise HTTPException(404, "No such trip.")
    if not (drive.start_recovered_km and drive.start_recovered_km > 0):
        raise HTTPException(
            409, f"Trip {drive_id} has no recovered distance, so its energy is "
                 f"a direct measurement rather than a projection. A "
                 f"disagreement there is a different fault.")
    if (true_wh_per_km is None) == (true_consumed_pct is None):
        raise HTTPException(
            409, "Give exactly one of 'true_wh_per_km' or 'true_consumed_pct'.")

    if true_wh_per_km is not None:
        new_energy = round(true_wh_per_km * drive.distance_km / 1000.0, 2)
    else:
        vehicle = session.get(Vehicle, drive.vehicle_id)
        capacity_kwh = _usable_capacity(session, vehicle, get_settings())[0]
        new_energy = round(true_consumed_pct / 100.0 * capacity_kwh, 2)

    rate = new_energy * 1000.0 / drive.distance_km if drive.distance_km else 0.0
    if not (sync_mod.MIN_PLAUSIBLE_WH_PER_KM <= rate <= sync_mod.MAX_PLAUSIBLE_WH_PER_KM):
        raise HTTPException(
            409, f"That works out to {rate:.0f} Wh/km over {drive.distance_km} km, "
                 f"outside the plausible {sync_mod.MIN_PLAUSIBLE_WH_PER_KM:.0f}-"
                 f"{sync_mod.MAX_PLAUSIBLE_WH_PER_KM:.0f} range. Check the figure.")

    plan = {
        "drive_id": drive.id,
        "recovered_km": drive.start_recovered_km,
        "energy_kwh": [drive.energy_used_kwh, new_energy],
        "wh_per_km": [drive.wh_per_km, round(rate, 1)],
        "applied": apply,
    }
    if apply:
        drive.energy_used_kwh = new_energy   # wh_per_km and cost derive from this
        session.commit()
    return plan


@router.api_route("/repair-lost-departure", methods=["GET", "POST"])
def repair_lost_departure(
    drive_id: int = Query(...),
    true_distance_km: float = Query(...),
    true_duration_min: float | None = Query(None),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Give a trip back the departure a blackout took, from the previous trip's
    own end.

    ``start_lost_km`` records distance the car really covered before this trip's
    anchor. Sync recovers it where it can, but a row already written keeps
    whatever it was given — and the case that most needs this is exactly the one
    sync used to decline: a long blackout that hid the previous arrival AND the
    departure after it (see process_snapshot's prev_close_odo_km).

    Neither odometer repair fits. repair_trip_boundary trades distance between
    two trips that share a boundary; here the ground belongs to nobody, sitting
    in the hole between one trip's end and the next one's start.
    repair_departure_start moves a start the other way, forward off a park it
    swallowed.

    ``true_distance_km`` is the car's own trip meter and is required, not
    advisory: the repair is only allowed when moving the start back to the
    previous trip's end reproduces the figure the car itself reports. That makes
    it self-validating — the hole is proven to be this trip's missing head
    rather than a journey nobody logged, which is the one thing two odometer
    readings cannot tell apart.

    Energy comes with the distance, priced by the same model sync would have
    used (see energy_for_blind_distance), with the opening kilometres carrying
    their departure premium. The clock moves only when the car's own duration
    says so — a row written before this repair existed already had its start
    back-estimated, and moving it twice would be worse than leaving it.

    Dry run by default.
    """
    drive = session.get(Drive, drive_id)
    if drive is None:
        raise HTTPException(404, "No such trip.")
    if drive.start_odo_km is None or drive.end_odo_km is None:
        raise HTTPException(
            409, f"Trip {drive_id} has no odometer anchors, so there is nothing "
                 f"to measure the hole against.")
    prev_d = session.scalars(
        select(Drive).where(Drive.vehicle_id == drive.vehicle_id,
                            Drive.start_time < drive.start_time)
        .order_by(Drive.start_time.desc()).limit(1)
    ).first()
    if prev_d is None or prev_d.end_odo_km is None:
        raise HTTPException(
            409, "No previous trip with an odometer reading, so there is no "
                 "origin to move this one back to.")
    recovered = round(drive.start_odo_km - prev_d.end_odo_km, 3)
    if recovered <= 0:
        raise HTTPException(
            409, f"Trip {drive_id} already starts where trip {prev_d.id} ended "
                 f"(gap {recovered} km). Nothing was lost between them.")
    new_span = round(drive.end_odo_km - prev_d.end_odo_km, 3)
    # The car reads to 0.1 km and so does the span it is compared against, so
    # allow both roundings — but no more. Past that the hole is not this trip's
    # missing head and must not be handed to it.
    if abs(new_span - true_distance_km) > 0.2:
        raise HTTPException(
            409, f"Moving the start back to trip {prev_d.id}'s end would make "
                 f"trip {drive_id} {new_span} km, but the car reports "
                 f"{true_distance_km} km. The hole between them is not this "
                 f"trip's missing departure.")

    new_energy = (round(sync_mod.energy_for_blind_distance(
        drive.energy_used_kwh, new_span, recovered, departure_blind_km=recovered), 2)
        if drive.energy_used_kwh else drive.energy_used_kwh)
    new_duration = drive.duration_min if true_duration_min is None else true_duration_min
    new_start_time = (drive.start_time if true_duration_min is None
                      else drive.end_time - timedelta(minutes=true_duration_min))
    new_distance = round(new_span, 1)
    plan = {
        "drive_id": drive.id,
        "from_trip": {"id": prev_d.id, "end_location": prev_d.end_location,
                      "end_odo_km": prev_d.end_odo_km},
        "recovered_km": recovered,
        "start_odo_km": [drive.start_odo_km, round(prev_d.end_odo_km, 3)],
        "distance_km": [drive.distance_km, new_distance],
        "energy_kwh": [drive.energy_used_kwh, new_energy],
        "start_location": [drive.start_location, prev_d.end_location],
        "start_time": [drive.start_time.isoformat(timespec="minutes"),
                       new_start_time.isoformat(timespec="minutes")],
        "duration_min": [drive.duration_min, new_duration],
        "applied": apply,
    }
    if apply:
        drive.start_odo_km = round(prev_d.end_odo_km, 3)
        drive.distance_km = new_distance
        drive.energy_used_kwh = new_energy   # wh_per_km derives from this
        # The ground was real but no poll ever saw it, which is precisely what
        # start_recovered_km means — and it is no longer lost.
        drive.start_recovered_km = round((drive.start_recovered_km or 0.0) + recovered, 3)
        drive.start_lost_km = 0.0
        if prev_d.end_coords:
            drive.start_coords = prev_d.end_coords
            drive.start_location = prev_d.end_location
            drive.start_area = prev_d.end_area
        drive.start_time = new_start_time
        drive.duration_min = new_duration
        if new_duration:
            drive.avg_speed_kmh = round(new_distance / (new_duration / 60.0), 1)
        session.commit()
    return plan


# GET as well as POST: these are hand-run repair tools, and the person
# running them is on a phone where the only way to issue a request is the
# address bar — which sends GET. POST-only made them look like they had
# run while returning 405 and changing nothing. Mutating on GET is
# normally wrong; here nothing writes without an explicit apply=true that
# no prefetcher will ever guess, and the app is passcode-gated.
@router.api_route("/backfill-place-names", methods=["GET", "POST"])
def backfill_place_names(
    limit: int = Query(40, ge=1, le=200),
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Name the stops whose geocode failed and left raw coordinates behind.

    _place_and_area falls back to the coordinate string when both geocoders
    miss, which keeps the trip logged and the spot searchable in a maps app —
    the right call at the time. What was wrong was that the fallback got
    CACHED, so a single Nominatim timeout named that spot after its latitude
    forever, including every later visit within GPS drift of it.

    That is fixed at the source, but rows already written keep what they were
    given. They are recognisable without guessing: the label is exactly the
    coordinate string it was derived from, which no successful geocode ever
    returns. A geofence added since (Home, Office) resolves them for free.

    Rate-limited by the geocoder itself, so this works in batches — ``limit``
    caps how many distinct coordinates are looked up per call. Re-run until it
    reports nothing left. Dry run by default.
    """
    vehicle = _first_vehicle(session)
    drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id)
        .order_by(Drive.start_time.desc())
    ).all()

    # One lookup per distinct coordinate, however many rows share it — the
    # commonest case here is exactly that, one unnamed stop appearing as an
    # arrival and then the next trip's departure.
    wanted: list[str] = []
    for d in drives:
        for label, coords in ((d.start_location, d.start_coords),
                              (d.end_location, d.end_coords)):
            if coords and label and label.strip() == coords.strip():
                if coords not in wanted:
                    wanted.append(coords)
    resolved: dict[str, tuple[str, str]] = {}
    for coords in wanted[:limit]:
        label, area = _place_and_area(coords, session)
        if label.strip() != coords.strip():
            resolved[coords] = (label, area)

    changed: list[dict[str, Any]] = []
    for d in drives:
        for end in ("start", "end"):
            coords = getattr(d, f"{end}_coords")
            label = getattr(d, f"{end}_location")
            if not (coords and label and label.strip() == coords.strip()):
                continue
            got = resolved.get(coords)
            if not got:
                continue
            changed.append({"drive_id": d.id, "end": end, "coords": coords,
                            "location": [label, got[0]]})
            if apply:
                setattr(d, f"{end}_location", got[0])
                setattr(d, f"{end}_area", got[1])
    if apply and changed:
        session.commit()
    return {
        "unnamed_coords": len(wanted),
        "looked_up": len(wanted[:limit]),
        "resolved": len(resolved),
        # A geocoder that misses twice will miss again; these are not errors so
        # much as places OpenStreetMap has nothing for. A geofence names them.
        "still_unnamed": len(wanted[:limit]) - len(resolved),
        "remaining": max(len(wanted) - limit, 0),
        "applied": apply,
        "changes": changed[:100],
        "note": ("Nothing to name — every stop has a place." if not wanted else
                 ("Dry run. Add &apply=true to write these." if not apply else None)),
    }


@router.api_route("/backfill-start-locations", methods=["GET", "POST"])
def backfill_start_locations(
    apply: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Repair trips whose recorded origin is where the network came back.

    Until 10e8423, a departure recovered after a blackout pulled its odometer
    back to the last parked reading but left its coordinates wherever the
    first poll caught the car — so the row named a place it had already
    driven away from (measured live: 725 m onto a highway).

    Those rows are repairable because the odometer says so. A trip with
    ``start_recovered_km > 0`` whose ``start_odo_km`` equals the previous
    trip's ``end_odo_km`` began exactly where that trip ended, and that
    previous trip's end location is on record. Trips whose coordinates
    already agree are left alone.

    Defaults to a dry run: returns what it would change and changes nothing.
    Pass ``apply=true`` to write.
    """
    vehicle = _first_vehicle(session)
    if vehicle is None:
        return {"available": False, "reason": "no vehicle linked"}
    drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id).order_by(Drive.start_time)
    ).all()

    changes: list[dict[str, Any]] = []
    for prev_d, d in zip(drives, drives[1:]):
        if not (d.start_recovered_km and d.start_recovered_km > 0):
            continue
        if d.start_odo_km is None or prev_d.end_odo_km is None:
            continue
        # Same odometer reading at both ends of the handover is what proves
        # the two trips are contiguous. Tolerance because these are floats
        # written through a round(), not because the values are approximate.
        if abs(d.start_odo_km - prev_d.end_odo_km) > 0.002:
            continue
        if not prev_d.end_coords or prev_d.end_coords == d.start_coords:
            continue
        changes.append({
            "drive_id": d.id,
            "start_time": d.start_time.isoformat(timespec="minutes"),
            "recovered_km": d.start_recovered_km,
            "from": {"coords": d.start_coords, "place": d.start_location},
            "to": {"coords": prev_d.end_coords, "place": prev_d.end_location},
        })
        if apply:
            d.start_coords = prev_d.end_coords
            d.start_location = prev_d.end_location
            d.start_area = prev_d.end_area
    if apply and changes:
        session.commit()
    return {
        "available": True,
        "applied": apply,
        "trips_scanned": len(drives),
        "would_change" if not apply else "changed": len(changes),
        "changes": changes[-50:],
        "note": (
            "The previous trip's own end may itself be short by whatever its "
            "arrival lost to a blackout, so this moves each origin to the best "
            "record there is rather than to a certainty. It is the same "
            "parking spot either way, which is what the route grouping keys on."
        ),
    }


# How close a field transition has to sit to a confirmed opening to count as
# coinciding with it. Wide enough to cover a poll interval either side, narrow
# enough that a car waking on its own schedule doesn't land inside it by luck.
SENTRY_NEAR_MIN = 15.0


@router.get("/sentry-check")
def sentry_check(
    days: int = Query(60, ge=1, le=730),
    session: Session = Depends(get_session),
):
    """Evidence for one open question: does a Sentry trigger show in the API?

    Tesla publishes no accelerometer, tilt or alarm-state field, so the only
    candidates are indirect — ``dashcam_state`` (a clip being written) and
    ``center_display_state`` (the screen waking). Both are logged on every
    change, so their transitions are already on record; what was missing was
    anything to line them up against.

    This returns the two side by side: confirmed physical openings, and every
    transition in those fields while the car sat parked. It deliberately draws
    no conclusion. If the theory holds, transitions cluster around openings
    and are rare otherwise; if they fire constantly, they are measuring
    something else entirely (the car waking for its own reasons) and the idea
    is dead. Both readings are useful; neither is the endpoint's to make.
    """
    vehicle = _first_vehicle(session)
    if vehicle is None:
        return {"available": False, "reason": "no vehicle linked"}
    since = sync_mod.now_local() - timedelta(days=days)

    events = session.scalars(
        select(SecurityEvent)
        .where(SecurityEvent.vehicle_id == vehicle.id, SecurityEvent.ts >= since)
        .order_by(SecurityEvent.ts.desc())
    ).all()

    readings = session.scalars(
        select(BatteryReading)
        .where(BatteryReading.vehicle_id == vehicle.id, BatteryReading.ts >= since)
        .order_by(BatteryReading.ts)
    ).all()

    # Only changes, and only while parked. A reading during a drive says
    # nothing — the screen is on and the dashcam is recording because someone
    # is sitting in the car.
    drives = session.scalars(
        select(Drive).where(Drive.vehicle_id == vehicle.id, Drive.end_time >= since)
    ).all()
    spans = [(d.start_time, d.end_time) for d in drives]

    def driving_at(ts: datetime) -> bool:
        return any(a <= ts <= b for a, b in spans)

    transitions: list[dict[str, Any]] = []
    prev_r = None
    for r in readings:
        if prev_r is not None and not driving_at(r.ts):
            for field in ("dashcam_state", "center_display_state"):
                was, now = getattr(prev_r, field), getattr(r, field)
                if was != now and now is not None:
                    transitions.append({
                        "ts": r.ts.isoformat(timespec="minutes"),
                        "field": field, "from": was, "to": now,
                        "sentry_mode": r.sentry_mode, "soc": r.soc,
                        # How close the nearest confirmed opening was. This is
                        # the whole correlation, per row, so it can be read
                        # without cross-referencing the two lists by eye.
                        "minutes_from_opening": min(
                            (round(abs((r.ts - e.ts).total_seconds()) / 60.0, 1)
                             for e in events), default=None),
                    })
        prev_r = r

    # `or` would be wrong here: a transition landing in the same minute as an
    # opening has minutes_from_opening == 0.0, which is falsy, so it would be
    # substituted away and counted as infinitely distant — losing exactly the
    # coincidence this endpoint exists to detect.
    near = [t for t in transitions
            if t["minutes_from_opening"] is not None
            and t["minutes_from_opening"] <= SENTRY_NEAR_MIN]
    return {
        "available": True,
        "window_days": days,
        "reported": bool(readings) and readings[-1].dashcam_state is not None,
        "openings": [
            {"ts": e.ts.isoformat(timespec="minutes"), "kind": e.kind,
             "sentry_mode": e.sentry_mode, "locked": e.locked, "soc": e.soc,
             "dashcam_state": e.dashcam_state,
             "center_display_state": e.center_display_state}
            for e in events
        ],
        "parked_transitions": transitions[-40:],
        # The two numbers that decide it, stated plainly rather than judged.
        # A signal worth using would be many transitions near openings and few
        # away from them; a field that flips constantly while parked is
        # tracking the car's own wake cycle, not an intruder.
        "transitions_total": len(transitions),
        "transitions_within_15min_of_an_opening": len(near),
        "openings_total": len(events),
        "note": (
            "Openings are physical entries (a door, trunk or window opened "
            "while parked, armed and unoccupied), not Sentry triggers — Tesla "
            "exposes no alarm state. Nothing in the app acts on these fields."
        ),
    }


@router.get("/summary")
def summary(
    days: int = Query(90, ge=1, le=730),
    since_charge: bool = Query(False),
    current_drive: bool = Query(False),
    trips_limit: int | None = Query(None, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """The single endpoint the dashboard consumes: full analysis + recommendations.

    With ``since_charge`` the window starts when the most recent charging
    session ended. With ``current_drive`` it covers the drive in progress
    (plus a live-trip readout) or, if the car is parked, the last drive.
    ``trips_limit`` raises how many trips ``driving.recent_trips`` lists
    (5 by default for any window, including since_charge) — the "Show
    more" button reissues this same request with a bigger number rather
    than a separate paginated endpoint.
    """
    import json as _json

    from .. import sync as sync_mod

    settings = get_settings()
    vehicle = _first_vehicle(session)
    # Usable pack capacity (override > measured EMA > variant spec > default),
    # used everywhere below that turns kWh into % or range delta into kWh.
    capacity_kwh, capacity_source = _usable_capacity(session, vehicle, settings)
    # The cron's own last determination of what the car was doing (including
    # "found it asleep") — written every /api/sync tick, read here purely
    # from the database. This is what lets the dashboard show a near-live
    # status on page load without itself ever pinging Tesla: the background
    # cron already did, and left the answer sitting in Neon.
    last_status_raw = state.get(session, state.scoped(state.LAST_STATUS_KEY, vehicle.vin))
    last_status = _json.loads(last_status_raw) if last_status_raw else None
    # Computed fresh on every request from the server's own clock (not the
    # browser's), so a stale/wrong client clock can't mask or fake this.
    if last_status is not None:
        age_min = (datetime.now().timestamp() - (last_status.get("ts") or 0)) / 60.0
        last_status["stale"] = age_min > CRON_STALE_MIN
    since = None
    window_label = None
    live = None
    if current_drive:
        open_trip = _json.loads(
            state.get(session, state.scoped(state.OPEN_TRIP_KEY, vehicle.vin)) or "null")
        snap_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, vehicle.vin))
        snap = _json.loads(snap_raw) if snap_raw else None
        if open_trip and snap:
            live = sync_mod.live_trip(open_trip, snap, capacity_kwh, settings.drive_min_km)
            live["eta"] = _live_eta(session, snap, live, capacity_kwh)
            since = datetime.fromtimestamp(open_trip["ts"], sync_mod.MYT).replace(tzinfo=None)
            window_label = "current drive"
        else:
            last_start = session.scalar(
                select(func.max(Drive.start_time)).where(Drive.vehicle_id == vehicle.id)
            )
            if last_start is not None:
                since = last_start
                window_label = "last drive"
    # Fetched unconditionally (not just for since_charge) so the dashboard can
    # always pin a "last charge" row atop the Recent Charges list — the
    # charge itself is otherwise invisible in the since-charge view (it ended
    # right at the window's start, so it's excluded from every list below by
    # definition), which reads as "my charge went missing" rather than "this
    # window starts after it"; showing it in every window keeps the format
    # (and the at-a-glance context) consistent regardless of which is picked.
    # Same field shape as charging_analysis.recent_charges' rows (id/rate_per_kwh/
    # is_free included) so the frontend can render and dedupe both with one
    # template instead of two.
    last_charge_summary = None
    last_charge = session.scalar(
        select(Charge).where(Charge.vehicle_id == vehicle.id)
        .order_by(Charge.end_time.desc())
    )
    if last_charge is not None:
        # kWh used since this charge ended — independent of whatever window
        # is currently selected, matching the rest of last_charge_summary.
        # Includes vampire drain in any parked gap since (see
        # vampire_drain()) — no charges list needed for that call, since by
        # definition nothing has charged since last_charge (it's the most
        # recent one on record).
        drives_since = session.scalars(
            select(Drive).where(Drive.vehicle_id == vehicle.id, Drive.start_time >= last_charge.end_time)
            .order_by(Drive.start_time)
        ).all()
        vampire_since = driving_analysis.vampire_drain(
            drives_since, [], capacity_kwh, anchor=(last_charge.end_time, last_charge.end_soc))
        used_since_last_charge_kwh = (
            sum(d.energy_used_kwh for d in drives_since) + vampire_since["kwh"]
        )
        last_charge_summary = {
            "id": last_charge.id,
            "start_time": last_charge.start_time.isoformat(timespec="minutes"),
            "end_time": last_charge.end_time.isoformat(timespec="minutes"),
            "energy_added_kwh": last_charge.energy_added_kwh,
            "start_soc": last_charge.start_soc,
            "end_soc": last_charge.end_soc,
            "cost": last_charge.cost,
            "charge_type": last_charge.charge_type,
            "location": last_charge.location,
            # Same string here (this summary doesn't infer a name from
            # nearby trips the way recent_charges does), but the pinned row
            # shares chargeRowHtml with that list, so it needs the field.
            "location_raw": last_charge.location,
            "rate_per_kwh": (
                round(last_charge.cost / last_charge.energy_added_kwh, 3)
                if last_charge.energy_added_kwh else None
            ),
            "is_free": bool(last_charge.is_free),
            "used_since_kwh": round(used_since_last_charge_kwh, 2),
            "source": last_charge.price_source or None,
            # What was actually IN the pack when this charge finished (end SoC
            # × usable capacity) — the real "fuel in the tank" figure, unlike
            # energy_added_kwh which is just what this one session topped up
            # and says nothing about what was already there if it didn't
            # start from empty. The since-charge Battery Used % anchors to
            # this instead.
            "battery_kwh_at_end": round((last_charge.end_soc or 0.0) / 100.0 * capacity_kwh, 2),
        }
        if since_charge:
            since = last_charge.end_time
            window_label = "since last charge"
    drives, charges = _window(session, vehicle.id, days, since=since)

    # Anchor the vampire-drain gap search at this charge's own end when the
    # window starts there — otherwise the parked stretch before the window's
    # first drive (often the single longest charge-free gap of all) is
    # invisible to vampire_drain() (see its docstring).
    vampire_anchor = (
        (last_charge.end_time, last_charge.end_soc)
        if since_charge and last_charge is not None else None
    )
    # Price every trip against the charge-layer history that actually
    # supplied its energy (see driving_analysis.layered_trip_costs), not a
    # flat "whatever the latest charge cost" assumption — the flat/ToU
    # tariff below only prices vampire drain and any trip the charge-layer
    # stack can't reach.
    price_fn = tariff.price_fn_from_settings(settings)
    trip_costs = _trip_cost_map(session, vehicle.id)
    driving = driving_analysis.analyze(
        drives, settings.rated_wh_per_km, capacity_kwh, price_fn,
        charges=charges, vampire_anchor=vampire_anchor,
        trip_costs=trip_costs,
        # Every window (including since-charge) caps recent_trips at 5 by
        # default — a since-charge cycle can still run past 5 drives, and
        # the "Show more" button (trips_limit) is how a caller sees the
        # rest, same as any other window, rather than an uncapped window
        # dumping the whole cycle into one long list.
        recent_trips_limit=trips_limit or 5)
    # A since-charge window's own `charges` list is always empty by
    # definition (it starts right where last_charge ends, so no charge can
    # have happened "since" yet) — without this, Energy Charged/AC-DC
    # Energy/Charging Cost (and Driving Cost, nested under the same
    # chg.available gate in the frontend) would all vanish for every
    # since-charge view. Fall back to the one charge that's actually fueling
    # this window's driving, so those KPIs read "what did my last charge
    # cost, and what's it powering" instead of going blank.
    charging = (
        charging_analysis.analyze([last_charge], drives)
        if since_charge and not charges and last_charge is not None
        else charging_analysis.analyze(charges, drives)
    )
    efficiency = efficiency_analysis.analyze(drives, settings.rated_wh_per_km)

    # This week vs last week (rolling 7-day windows anchored at now), regardless
    # of the display window — a steady, comparable pulse of usage.
    now = sync_mod.now_local()   # MYT wall-clock, to match stored start_time
    wk_drives = [d for d in drives if d.start_time >= now - timedelta(days=7)] \
        if since is None and days >= 14 else None
    week_compare = None
    if wk_drives is not None:
        last_wk = [d for d in drives
                   if now - timedelta(days=14) <= d.start_time < now - timedelta(days=7)]
        if wk_drives and last_wk:
            def _wk(ds):
                dist = sum(d.distance_km for d in ds)
                energy = sum(d.energy_used_kwh for d in ds)
                return {
                    "drives": len(ds),
                    "distance_km": round(dist, 1),
                    "energy_kwh": round(energy, 1),
                    "wh_per_km": round(energy * 1000.0 / dist) if dist and energy > 0 else None,
                }
            week_compare = {"this": _wk(wk_drives), "last": _wk(last_wk)}

    # Data-driven narrative: this window vs the equal-length one immediately
    # before it. Same gating as week_compare — only meaningful for a plain
    # days-based window (not "since last charge"/"current drive", which have
    # no natural "period before" to compare against).
    narrative_lines = None
    prev_driving = prev_efficiency = None  # also feeds the assessment trend below
    if since is None and days >= 14:
        cur_since = now - timedelta(days=days)
        prev_since = cur_since - timedelta(days=days)
        prev_drives, prev_charges = _window(session, vehicle.id, days, since=prev_since, until=cur_since)
        prev_driving = driving_analysis.analyze(
            prev_drives, settings.rated_wh_per_km, capacity_kwh, price_fn,
            charges=prev_charges, trip_costs=trip_costs)
        prev_charging = charging_analysis.analyze(prev_charges, prev_drives)
        prev_efficiency = efficiency_analysis.analyze(prev_drives, settings.rated_wh_per_km)
        narrative_lines = narrative_engine.build(
            {"driving": driving, "charging": charging, "efficiency": efficiency},
            {"driving": prev_driving, "charging": prev_charging, "efficiency": prev_efficiency},
            settings.currency,
        )

    # Battery Balance: how much charge is actually left in the pack right now
    # (the latest logged SoC reading) — the "fuel gauge", not a derived delta.
    current_soc = session.scalar(
        select(BatteryReading.soc)
        .where(BatteryReading.vehicle_id == vehicle.id)
        .order_by(BatteryReading.ts.desc())
        .limit(1)
    )
    # Battery Used: % of the full (degradation-adjusted) pack, same basis as
    # every other %-of-battery figure in the app (km/1% Battery's
    # soc_used_pct, each trip's own soc_used_pct) so they're all directly
    # comparable and summable. Still only shown for the since-charge window —
    # any other window (7/30/90 days, ...) can span several charge/discharge
    # cycles with cumulative use exceeding one pack, so only the raw kWh is
    # shown there.
    full_charge_kwh = capacity_kwh
    charged_kwh = charging.get("total_energy_kwh") or 0.0
    # For "since charge" specifically we have real ground truth for the total
    # drop — the charge's own end SoC minus the latest reading — so anchor to
    # that instead of driving_analysis.analyze()'s bottom-up per-trip
    # estimate. Each trip there takes max(measured kWh, integer SoC drop) to
    # rescue a trip with a range-reading gap from being undercounted (see
    # driving.py's _trip_kwh()) — but always taking the larger is a
    # one-directional bias that, summed across several trips, visibly drifts
    # the total past what the pack's own SoC already reports directly (e.g.
    # "89% left, 11.7% used" not summing to 100%). Only applied when there's
    # at least one drive in the window (driving.available) — vampire_kwh
    # below needs a real vampire_drain() result to split against, and with
    # zero drives there isn't one.
    ground_truth_used_kwh = (
        max((last_charge.end_soc - current_soc) / 100.0 * capacity_kwh, 0.0)
        if (since_charge and last_charge is not None and current_soc is not None
            and capacity_kwh > 0 and driving.get("available"))
        else None
    )
    used_kwh = (
        ground_truth_used_kwh if ground_truth_used_kwh is not None
        else ((driving.get("total_energy_used_kwh") or 0.0) if driving.get("available") else 0.0)
    )
    used_pct = None
    if since_charge and capacity_kwh > 0:
        used_pct = round(used_kwh / capacity_kwh * 100.0, 1)
    # Same window's used_kwh split into trip vs. vampire (standby drain
    # between drives — see driving_analysis.vampire_drain()); the two always
    # sum to used_kwh exactly. When used_kwh is the ground-truth SoC delta
    # above (not analyze()'s own bottom-up total), trip is re-derived by
    # subtraction from THAT total instead of taken directly from analyze() —
    # same "round the total once, derive the split by subtraction" principle
    # analyze() itself uses, just anchored to the more accurate total.
    vd = (driving.get("vampire_drain") or {}) if driving.get("available") else {}
    vampire_kwh = vd.get("kwh", 0.0)
    if ground_truth_used_kwh is not None:
        # Cap the idle share at the (rounded) total before deriving trip by
        # subtraction: SoC can rebound a little while parked (BMS re-reading
        # a rested pack), letting measured vampire exceed the window's total
        # drop — without the cap, trip clamps to 0 and the displayed
        # "X (trip + idle)" breakdown sums to more than X.
        used_rounded = round(used_kwh, 1)
        vampire_kwh = min(round(vampire_kwh, 2), used_rounded)
        trip_kwh = max(round(used_rounded - vampire_kwh, 1), 0.0)
    else:
        trip_kwh = (
            round(driving.get("trip_energy_used_kwh") or 0.0, 1)
            if driving.get("available") else 0.0
        )
    # Driving Cost (drv.total_cost/cost_per_km) is still driving_analysis.
    # analyze()'s own bottom-up total_energy_used_kwh, which can disagree
    # with the ground-truth Battery Used above by the same one-directional
    # bias described there (e.g. a Driving Cost RM/km that implies more
    # energy was used than the last charge even added). The per-trip figures
    # in Recent Trips already use the true charge-layer rate for each trip
    # (see driving_analysis.layered_trip_costs, cascading to an older charge
    # if this one's kWh was exceeded), but for this since-charge *aggregate*
    # specifically — where every trip in the window is by definition powered
    # from this one charge onward — re-anchor to the ground-truth total at
    # this charge's own rate, so Battery Used, Driving Cost and Charging
    # Cost all reconcile exactly rather than drifting apart by that bias.
    if (ground_truth_used_kwh is not None and driving.get("total_distance_km")
            and last_charge.energy_added_kwh):
        rate = last_charge.cost / last_charge.energy_added_kwh
        driving["total_cost"] = round(used_kwh * rate, 2)
        driving["cost_per_km"] = round(used_kwh * rate / driving["total_distance_km"], 3)
    vd_longest = vd.get("longest")
    longest_inducer = (
        _idle_inducer(session, vehicle.id, vd_longest["start"], vd_longest["end"])
        if vd_longest else None
    )
    battery_balance = {
        "full_charge_kwh": full_charge_kwh,
        "charged_kwh": round(charged_kwh, 1),
        "used_kwh": round(used_kwh, 1),
        "used_pct": used_pct,
        "current_soc_pct": round(current_soc, 1) if current_soc is not None else None,
        "trip_kwh": trip_kwh,
        "vampire_kwh": vampire_kwh,
        "vampire_hours": vd.get("hours", 0.0),
        "vampire_gaps": vd.get("gaps", 0),
        # The single longest qualifying parked gap this window, separate from
        # the aggregate above — e.g. "that's when I was away for the
        # weekend" vs. day-to-day idle drain.
        "vampire_longest_hours": vd_longest["hours"] if vd_longest else None,
        "vampire_longest_start": vd_longest["start"] if vd_longest else None,
        "vampire_longest_end": vd_longest["end"] if vd_longest else None,
        # What was likely running when the car parked into that gap, from
        # BatteryReading rows logged before it slept — see _idle_inducer().
        # None whenever there's nothing to positively report (no reading in
        # range, or Sentry/climate both off in every reading seen).
        "vampire_longest_inducer": longest_inducer,
    }

    # Petrol comparator (TCO): what an equivalent petrol car would have cost
    # to run this window's distance, at the configured price/consumption.
    # Both settings default to 0 (disabled) — no assumed "average car" figure
    # is guessed, since a wrong one would misinform rather than just be absent.
    petrol_comparison = None
    if (
        settings.petrol_price_per_liter > 0 and settings.petrol_l_per_100km > 0
        and driving.get("available")
    ):
        distance_km = driving["total_distance_km"]
        petrol_cost = round(
            distance_km / 100.0 * settings.petrol_l_per_100km * settings.petrol_price_per_liter, 2
        )
        ev_cost = driving.get("total_cost")
        petrol_comparison = {
            "distance_km": distance_km,
            "petrol_cost": petrol_cost,
            "ev_cost": ev_cost,
            "savings": round(petrol_cost - ev_cost, 2) if ev_cost is not None else None,
            "petrol_price_per_liter": settings.petrol_price_per_liter,
            "petrol_l_per_100km": settings.petrol_l_per_100km,
        }

    # Battery health uses the full reading history, not the display window.
    # Column-only select: analyze() needs four fields, not 2000 hydrated ORM
    # rows on every dashboard load.
    readings = _newest_readings(
        session, vehicle.id,
        (BatteryReading.soc, BatteryReading.range_km,
         BatteryReading.ts, BatteryReading.odo_km))
    # 100% reference: explicit override, else the factory figure for this
    # exact variant — model+badge+wheel from the trim, generation from the
    # VIN's model-year letter (74D means 536 km in 2023 but 549 km in 2024).
    vin_info = vin_mod.decode(vehicle.vin)
    spec_km = settings.battery_new_range_km or battery_analysis.new_range_for(
        vehicle.model, vehicle.trim, year=vin_info.get("year")
    )
    battery = battery_analysis.analyze(
        [{"soc": soc, "range_km": rng, "ts": ts, "odo_km": odo}
         for soc, rng, ts, odo in readings],
        new_range_km=spec_km,
    )

    tou = None
    if settings.energy_price_peak_kwh > 0 and settings.energy_price_offpeak_kwh > 0:
        tou = {
            "peak_price": settings.energy_price_peak_kwh,
            "offpeak_price": settings.energy_price_offpeak_kwh,
            "peak_start_hour": settings.tariff_peak_start_hour,
            "peak_end_hour": settings.tariff_peak_end_hour,
        }
    assessment = recommendations_engine.assess(
        driving,
        charging,
        efficiency,
        battery,
        energy_price=settings.energy_price_per_kwh,
        currency=settings.currency,
        tou=tou,
        prev=({"driving": prev_driving, "efficiency": prev_efficiency}
              if prev_driving is not None else None),
        # What likely drew power during the biggest parked gap (Sentry/climate),
        # so the standby-drain tip can name it — same figure the Battery
        # Balance card's vampire_longest_inducer uses.
        standby_inducer=longest_inducer,
    )
    # Keep the flat list under its original key for any consumer that still
    # reads it (older cached frontends, external scripts); the assessment
    # carries the same list plus the scorecard.
    recs = assessment["recommendations"]

    vehicle_out = VehicleOut.model_validate(vehicle).model_dump()
    vehicle_out.update({k: v for k, v in vin_info.items() if v})  # year, plant
    # Current odometer, for the header strip next to the window/LIVE badge —
    # the true latest reading (not the battery-health readings above, which
    # are capped/ordered for the degradation curve, not "most recent").
    current_odo = session.scalar(
        select(func.max(BatteryReading.odo_km)).where(BatteryReading.vehicle_id == vehicle.id)
    )
    vehicle_out["current_odo_km"] = round(current_odo, 1) if current_odo is not None else None
    # The pack size actually used to turn range/SoC deltas into kWh, and where
    # it came from — surfaced so a wrong figure (which scales every drive's
    # kWh) is visible and diagnosable rather than hidden.
    vehicle_out["usable_capacity_kwh"] = round(capacity_kwh, 1)
    vehicle_out["capacity_source"] = capacity_source
    # What the car's own charge sessions imply that figure should be — the
    # only independent check on it, since a drive's kWh is derived FROM the
    # capacity and would just confirm itself (see battery.implied_capacity).
    # Shown beside the figure in use rather than tucked in a separate view:
    # capacity scales every kWh and Wh/km in the app at once, so a quiet
    # disagreement here is exactly the kind that goes unnoticed while every
    # trip reads a few percent off. Whole history, not the display window —
    # charges big enough to calibrate from are rare, so a short window would
    # usually have none.
    all_charges = session.scalars(
        select(Charge).where(Charge.vehicle_id == vehicle.id).order_by(Charge.start_time)
    ).all()
    vehicle_out["capacity_check"] = battery_analysis.implied_capacity(list(all_charges))
    # Ground the odometer says was covered that no trip claims. The one check
    # in the app that doesn't depend on any of its own derived figures — it
    # compares each trip's recorded stop against the readings taken while the
    # car sat parked afterwards, which is where the car actually came to rest.
    # Scoped to the displayed window's drives, but against every reading in
    # that span, since a short close is only visible in the parked readings
    # that follow it.
    # since is None for the default window — _window derives that bound itself,
    # so mirror it here rather than reaching for a value that isn't set yet.
    readings_since = since if since is not None else (
        sync_mod.now_local() - timedelta(days=days))
    window_readings = session.scalars(
        select(BatteryReading).where(
            BatteryReading.vehicle_id == vehicle.id,
            BatteryReading.ts >= readings_since,
        ).order_by(BatteryReading.ts)
    ).all()
    vehicle_out["continuity"] = driving_analysis.odometer_continuity(
        list(drives), list(window_readings))
    # Whether time-of-use pricing is active, so it's clear why cost figures
    # vary by time of day instead of using the flat rate.
    vehicle_out["tou_enabled"] = bool(
        settings.energy_price_peak_kwh > 0 and settings.energy_price_offpeak_kwh > 0)
    # The account's cars, so the header can offer a picker when more than one is
    # linked. Only real (account-linked) cars, not demo/imported placeholders.
    garage = []
    if state.data_source(session) == "linked":
        garage = [
            {"vin": v.vin, "name": v.name, "model": v.model}
            for v in session.scalars(select(Vehicle).order_by(Vehicle.id)).all()
            if not v.vin.startswith(("DEMO", "IMPORT"))
        ]
    return {
        "vehicle": vehicle_out,
        "active_vin": vehicle.vin,
        "garage": garage,
        "window_days": days,
        "window_label": window_label,
        "last_charge": last_charge_summary,
        "generated_at": sync_mod.now_local().isoformat(timespec="seconds"),
        "currency": settings.currency,
        "last_status": last_status,
        "live_trip": live,
        "driving": driving,
        "charging": charging,
        "efficiency": efficiency,
        "battery": battery,
        "battery_balance": battery_balance,
        "petrol_comparison": petrol_comparison,
        "week_compare": week_compare,
        "narrative": narrative_lines,
        "recommendations": recs,
        "assessment": assessment,
    }
