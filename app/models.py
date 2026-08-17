"""SQLAlchemy ORM models for vehicles, drives and charging sessions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Setting(Base):
    """Simple key/value store for runtime configuration (e.g. a linked token)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Text, not a bounded VARCHAR. A generic key/value store has no business
    # guessing how long its longest value will ever be, and guessing 2048 cost
    # thirteen hours of polling: the tick log grew past it, every write raised
    # StringDataRightTruncation, and since that log is written on every sync
    # path it took /api/sync down with it. Bounds belong in the code that
    # knows what it is storing, not in a column shared by tokens, snapshots,
    # open trips and diagnostics alike.
    value: Mapped[str] = mapped_column(Text, default="")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    vin: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="My Tesla")
    model: Mapped[str] = mapped_column(String(40), default="Model 3")
    trim: Mapped[str] = mapped_column(String(60), default="")
    rated_range_km: Mapped[float] = mapped_column(Float, default=500.0)
    battery_capacity_kwh: Mapped[float] = mapped_column(Float, default=75.0)

    drives: Mapped[list["Drive"]] = relationship(back_populates="vehicle")
    charges: Mapped[list["Charge"]] = relationship(back_populates="vehicle")


class Drive(Base):
    """A single driving session (one trip from park to park)."""

    __tablename__ = "drives"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)

    start_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime)

    distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    duration_min: Mapped[float] = mapped_column(Float, default=0.0)

    start_soc: Mapped[float] = mapped_column(Float, default=0.0)  # %
    end_soc: Mapped[float] = mapped_column(Float, default=0.0)  # %
    energy_used_kwh: Mapped[float] = mapped_column(Float, default=0.0)

    avg_speed_kmh: Mapped[float] = mapped_column(Float, default=0.0)
    max_speed_kmh: Mapped[float] = mapped_column(Float, default=0.0)

    outside_temp_c: Mapped[float] = mapped_column(Float, default=20.0)
    # The specific spot (POI/street/address) for per-trip display.
    start_location: Mapped[str] = mapped_column(String(120), default="")
    end_location: Mapped[str] = mapped_column(String(120), default="")
    # Coarser district/suburb bucket, stable across GPS jitter between repeat
    # visits to "the same place" (the exact matched POI/building can flip a
    # few metres apart) — used to group Top Routes so a real repeated route
    # doesn't fragment into many near-duplicate single-count entries. Empty
    # on rows logged before this existed; analysis code falls back to the
    # specific location in that case.
    start_area: Mapped[str] = mapped_column(String(120), default="")
    end_area: Mapped[str] = mapped_column(String(120), default="")
    # Raw "lat, lon" endpoints, kept alongside the resolved names (which
    # replace the coords in start/end_location) so each trip can link out to
    # a live map. Empty on rows logged before this existed.
    start_coords: Mapped[str] = mapped_column(String(40), default="")
    end_coords: Mapped[str] = mapped_column(String(40), default="")

    # Real (not estimated) minutes spent stopped >= sync.IDLE_STREAK_MIN,
    # tracked live while the trip was open. idle_tracked distinguishes
    # "confirmed via live tracking, genuinely 0" from "unknown" (trips logged
    # before this existed, or reconstructed across an unpolled gap with no
    # live tracking) — 0.0 alone is ambiguous between those two, since a
    # trip with zero sustained stops and a trip nobody ever measured both
    # read the same. Analysis code trusts idle_min only when idle_tracked is
    # true; otherwise it falls back to the avg/max-speed estimate.
    idle_min: Mapped[float] = mapped_column(Float, default=0.0)

    # Minutes climate was observed running during this trip. Climate is a
    # whole-trip load rather than an idle one — it runs while moving just as
    # much as while stopped — so this, not idle_min, is what decides how much
    # of the energy was something other than propulsion (see
    # sync.driving_only_kwh). None means the car never reported the flag, which
    # must read as "unknown" and fall back to assuming it ran, not as "off".
    climate_min: Mapped[float | None] = mapped_column(Float, nullable=True)

    # The polling windows this trip's two boundaries were placed inside, in
    # seconds. Every anchor at either end is an estimate located somewhere in
    # its window, so the window's width IS that end's uncertainty — a trip
    # whose first driving reading arrived thirty seconds after the last parked
    # one is far better anchored than one where eight minutes passed, and
    # nothing previously distinguished them. Recorded so a discrepancy can be
    # weighed against how well the trip was observed rather than every trip
    # reading as equally authoritative. None where the path had no previous
    # reading to measure against (a trip opened with no prior snapshot, or a
    # close that never evaluated one).
    start_gap_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_gap_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    idle_tracked: Mapped[bool] = mapped_column(Boolean, default=False)

    # User-assigned category ("work" / "personal", or any free text) for
    # expense-claim/cost-splitting purposes. "" = untagged.
    tag: Mapped[str] = mapped_column(String(20), default="")

    # Seconds the trip's stop time was back-dated by sync.py's pace-based
    # correction, which assumes a low-implied-speed polling gap means the car
    # parked early in it. NB the correction rewrites the recorded timestamp
    # ONLY — the stop snapshot keeps the real reading's odo_km/soc/range_km, so
    # distance and energy stay the full measured deltas and just duration (and
    # therefore avg_speed) changes. A trim can't shrink a trip's kWh; if one
    # reads short on energy, look at the start anchor instead.
    # Recorded so a trip can be asked whether it happened: 0.0 means the
    # correction did not fire, None means the trip predates this field (or was
    # built by a path that never trims). Logging only — nothing reads it to
    # change behaviour, and the trim itself is unchanged.
    tail_trim_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Odometer distance driven before this trip's start anchor, and therefore
    # missing from its distance_km. The counterpart to tail_trim_sec at the
    # other end, and the harder of the two to see: the odometer is continuous,
    # so lost distance doesn't show up anywhere as an anomaly — it simply
    # belongs to no trip, and the trip quietly reads short against the car's
    # own meter. 0.0 means the anchor sat at the previous reading (nothing can
    # precede it) or the departure recovery pulled the movement back in; None
    # means the trip predates this field. When the recovery is what made it
    # zero, start_recovered_km carries the amount — the two together say which
    # of the two zeros this is.
    start_lost_km: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Odometer distance the departure recovery pulled back INTO this trip: the
    # ground covered between the last parked reading and the first tracked
    # driving one, which the trip would otherwise have started after.
    #
    # Exists because start_lost_km alone is ambiguous, and expensively so. It
    # reads 0.0 both when nothing could be lost (the anchor already sat at the
    # previous reading) and when the recovery reclaimed real distance — the
    # two cases most worth telling apart, since the second means the trip's
    # distance and energy include ground driven before its own start time.
    # Three separate investigations stalled on that ambiguity before this was
    # recorded. 0.0 means the recovery did not fire; None means the trip
    # predates the field.
    start_recovered_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Minutes of the pre-departure gap the car was still PARKED, when the
    # departure recovery took the pre-gap SoC as this trip's baseline. Their
    # standby drain sits in that reading and is not this drive's, so it is
    # taken back out at this car's measured parked rate — the same
    # correction tail_trim_sec drives at the other end. Kept because it is
    # the evidence for a correction that already happened: without it, a
    # trip whose energy was adjusted looks identical to one that never was.
    start_park_min: Mapped[float | None] = mapped_column(Float, nullable=True)

    # The odometer at this trip's own start and stop anchors. distance_km is
    # their difference, so on its own it says how far the trip ran but not
    # WHERE on the odometer it sat — and without that, a trip cannot be checked
    # against the readings taken around it. With them, the ground between one
    # trip's stop and the next one's start is measurable, and so is the
    # difference between where a trip recorded its stop and where the car was
    # actually observed resting afterwards (see driving.odometer_continuity).
    # None on trips logged before this was recorded.
    # The arrival tail this trip could not see, estimated rather than measured
    # (see sync.estimate_arrival_tail). Folded into distance_km and priced into
    # the energy, but kept here so it never passes for a measurement and can be
    # taken back whole the moment a real reading covers that ground. None when
    # nothing was estimated, which includes every trip that closed normally.
    end_est_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Whether that estimate has since been reconciled against the car's own
    # trip meter (see routes.repair_arrival_tail). The distinction matters
    # because end_est_km alone conflates two states that call for opposite
    # treatment: a guess still waiting to be checked, and a figure that has
    # been checked and found right. Both are estimates in the sense that no
    # poll ever saw the ground; only the first is an open question.
    #
    # Without this the review list can never be worked down — a trip stays on
    # it after being verified, with nothing able to clear it, and a checklist
    # that always shows the same rows stops being read.
    end_est_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    start_odo_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_odo_km: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Odometer distance driven after this trip's closing anchor, and therefore
    # missing from its distance_km — the same silent loss as start_lost_km, at
    # the other end. Three closes normally record 0.0: the parked one keeps
    # extending its stop point while the odometer climbs; the blind-gap one
    # folds the gap's movement in as the tail of the drive that just ended;
    # and a sustained-offline sleep-close gets topped up by routes.py on the
    # next successful poll if further movement turns up while the car now
    # reads parked — a small amount always folds in, and so does a larger one
    # within SLEEP_CLOSE_MERGE_MAX_MIN of the close, since sustained "offline"
    # is only 3 minutes and routinely fires mid-drive through a real dead zone
    # (see state.LAST_SLEEP_CLOSE_KEY). Nonzero where a fold is refused —
    # too far past that window to still be the same drive, so it is reported
    # rather than attributed on a guess. None means the trip predates this
    # field.
    end_lost_km: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Manually-entered cost, used only when the charge-layer cost model
    # (driving_analysis.layered_trip_costs) can't price this trip — every
    # charge session in the vehicle's history has already been fully
    # consumed by earlier trips with no new charge since. None means "price
    # it automatically"; set via /api/data/set-drive-cost, always wins over
    # the computed figure when present.
    cost_override: Mapped[float | None] = mapped_column(Float, nullable=True)

    vehicle: Mapped["Vehicle"] = relationship(back_populates="drives")

    @property
    def wh_per_km(self) -> float:
        if self.distance_km <= 0:
            return 0.0
        return (self.energy_used_kwh * 1000.0) / self.distance_km


class BatteryReading(Base):
    """A point-in-time battery reading captured on sync, for health trending.

    ``range_km`` is the car's rated remaining range at ``soc`` percent, so
    ``range_km / (soc/100)`` projects the full-pack range — its drift over
    time is the degradation signal.
    """

    __tablename__ = "battery_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    soc: Mapped[float] = mapped_column(Float)
    range_km: Mapped[float] = mapped_column(Float)
    odo_km: Mapped[float] = mapped_column(Float, default=0.0)
    # Nullable, not defaulted to False: None means "Tesla didn't report this
    # field on this poll" (older cars/software, or a permission gap),
    # meaningfully different from a confirmed off — see vampire_drain's
    # "likely inducer" lookup in routes.py.
    sentry_mode: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    climate_on: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Tri-state string ("Off"/"On"/"FanOnly"), not a bool — Tesla's own shape
    # for this field. None when unreported, same rule as the two above. This
    # is the car's *setting* for whether COP is allowed to run at all (most
    # owners leave it "On" permanently as a safety default) — NOT whether it
    # is actually running right now. Use cabin_overheat_protection_actively_
    # cooling below for that; checking this field alone flags COP as a drain
    # cause on almost every reading, whether it ever actually activated.
    cabin_overheat_protection: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The live "is it actually cooling right now" flag Tesla reports
    # alongside cabin_overheat_protection above — this is the one that means
    # COP is really drawing power, not just enabled as a setting.
    cabin_overheat_protection_actively_cooling: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Recorded purely to find out, empirically, whether Tesla leaks a Sentry
    # *trigger* through either field — the API has no documented alarm-state
    # or accelerometer signal, so nothing here is relied on yet. The theory
    # worth testing: an escalating Sentry event wakes the centre screen
    # (center_display_state) and writes a clip (dashcam_state). Both arrive in
    # the vehicle_state payload the sync already fetches, so logging them
    # costs no extra API calls. Check them against a known Sentry event before
    # building anything on top. None when unreported, as above.
    dashcam_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    center_display_state: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SecurityEvent(Base):
    """A physical opening while the car sat parked, armed and unoccupied.

    The alert for this has always been push-only, which meant the event left
    no trace once the notification was dismissed. Persisting it exists for one
    specific purpose: Tesla publishes no accelerometer, tilt or alarm-state
    field, so whether a Sentry trigger is visible in the API at all is an open
    question, and the only way to answer it is to compare a *known* real event
    against what the API was reporting at that moment.

    ``dashcam_state`` and ``center_display_state`` are therefore captured here
    as they read when the opening was detected — the same two fields
    BatteryReading logs on every change (see routes.py, which forces a row
    whenever either moves). One row here plus those transitions is the whole
    experiment: if an escalating Sentry event really does wake the screen or
    write a clip, it should show up around these timestamps and nowhere else.

    A door opening is not itself a Sentry trigger — it is a proxy, and a good
    one, since an opening on an armed car escalates Sentry. Nothing is built
    on top of this yet, and nothing should be until the correlation is real.
    """

    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    # "door" or "window" — which opening tripped it, matching the alert text.
    kind: Mapped[str] = mapped_column(String(16), default="")
    # The arming context, so a locked-but-no-Sentry opening stays tellable
    # apart from a Sentry-armed one when reading the correlation later.
    sentry_mode: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    locked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    soc: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The two fields under test, as they read at the moment of the opening.
    dashcam_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    center_display_state: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ArrivalTailSample(Base):
    """One estimate of an unseen arrival, paired with what it turned out to be.

    The arrival estimate (sync.estimate_arrival_tail) is a model with a free
    parameter — how long a car goes on driving after the last reading that saw
    it. It shipped set to three minutes, which is the poller's own unreachable
    timeout rather than anything about the car, and the first trip that could
    be checked said the model ran 51% long: 0.483 km predicted, 0.320 driven.

    One trip is not a calibration. Re-tuning on it produced a model that read
    70% short on that same trip, which is what re-tuning on a single point
    tends to do. So this records the pairs instead, and the parameter waits
    until there are enough of them to mean something.

    Written only where the pairing is real: the moment a later poll can measure
    that exact stretch (see the fold-in in routes._process_vehicle). If the car
    is already driving again when next seen, the odometer covers the new trip
    too and the tail is unmeasurable — no row, rather than a row that quietly
    conflates the two.

    ``measured_km`` of 0.0 is a genuine and important observation: the last
    reading really was the arrival, and any estimate over it was invented. It
    is not a missing value.
    """

    __tablename__ = "arrival_tail_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    drive_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # When the measurement landed, not when the trip closed.
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)

    # The two halves of the pair. est_km is 0.0 when the model declined to
    # estimate at all — also a prediction, and one worth scoring: a declined
    # estimate against a real tail is the model failing in the quiet direction.
    est_km: Mapped[float] = mapped_column(Float, default=0.0)
    measured_km: Mapped[float] = mapped_column(Float, default=0.0)

    # The inputs the estimate was computed from, so a replacement parameter can
    # be fitted offline against these rows without needing the raw snapshots
    # back. speed_kmh is the one that matters: the model is speed times window,
    # so with the pair and the speed the implied true window falls out.
    speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    est_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    # WHERE the arrival happened, which turned out to be the only thing that
    # predicts the tail at all. Stored rather than joined through drive_id so a
    # deleted trip cannot take the measurement with it — this is the
    # calibration set, and it has to outlive the rows it came from.
    place: Mapped[str] = mapped_column(String(120), default="")
    # How long after the close the measuring poll arrived, and which report
    # closed the trip — both bear on how much the measurement can be trusted.
    elapsed_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(String(16), default="")


class Charge(Base):
    """A single charging session."""

    __tablename__ = "charges"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)

    start_time: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime)
    duration_min: Mapped[float] = mapped_column(Float, default=0.0)

    start_soc: Mapped[float] = mapped_column(Float, default=0.0)  # %
    end_soc: Mapped[float] = mapped_column(Float, default=0.0)  # %
    energy_added_kwh: Mapped[float] = mapped_column(Float, default=0.0)

    # AC (home/destination) vs DC (supercharger/fast)
    charge_type: Mapped[str] = mapped_column(String(8), default="AC")
    max_power_kw: Mapped[float] = mapped_column(Float, default=0.0)
    location: Mapped[str] = mapped_column(String(120), default="")
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    outside_temp_c: Mapped[float] = mapped_column(Float, default=20.0)
    # Manually flagged free session (e.g. a Tesla Destination Charger) — no
    # telemetry field reliably distinguishes these from a paid AC charger, so
    # this is set by hand rather than auto-detected. Forces cost to 0.
    is_free: Mapped[bool] = mapped_column(Boolean, default=False)

    # Usable pack capacity this session implies, measured from the slope of
    # energy-added against SoC across the whole charge rather than from its two
    # endpoints (see battery.capacity_from_curve). The endpoint method carries a
    # whole-percent SoC error at each end — on a 20-point charge that is +/-10%
    # on the answer — which is why it needs a 15%+ gain before it says anything
    # and still scatters. A slope through the session's own samples is barely
    # troubled by it. None when the session couldn't support a fit: too few
    # samples, too little SoC covered, or residuals showing the line isn't one.
    implied_capacity_kwh: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How many samples that rests on, so a figure can be weighed rather than
    # taken at face value.
    capacity_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Which of Public/Home/Office this session was actually priced against —
    # set when a charge is first priced, and again whenever the dashboard's
    # 🌐/🏠/🏢 quick-rate buttons are used to fix one after the fact. Blank
    # for a fully custom rate (doesn't match any of the three) or a charge
    # logged before this column existed — the dashboard falls back to
    # guessing from location text in that case, since a *saved* source
    # keeps meaning "this was a home charge" even after rates change later,
    # unlike comparing the stored cost to today's configured rates.
    price_source: Mapped[str] = mapped_column(String(10), default="")

    vehicle: Mapped["Vehicle"] = relationship(back_populates="charges")


class Place(Base):
    """A user-named geofence (e.g. "Home", "Office") for trip display.

    A trip endpoint within ``radius_km`` of a place's centre shows this
    name instead of the geocoded POI/street name, since a user's own name
    for their driveway is more useful than whatever OSM happens to call it.
    """

    __tablename__ = "places"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    radius_km: Mapped[float] = mapped_column(Float, default=0.15)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # How fast the car actually gets away from HERE, km/h, over the first few
    # km — used only to back-date a departure the sleep window did not see
    # (sync.DEPARTURE_PACE_KMH). 0 = use the global default.
    #
    # This is a setting and not a measurement, which is the honest part. The
    # arrival tail could be learned because a later poll observes the true
    # stop; nothing ever observes the true START, so no amount of history
    # tells the app it guessed wrong. The evidence has to come from outside —
    # the car's own Trips list — and then it lives here.
    departure_pace_kmh: Mapped[float] = mapped_column(Float, default=0.0)


class ServiceRecord(Base):
    """A logged maintenance event (tyre rotation, brake fluid, ...).

    Purely user-logged — the car doesn't report service history over the
    API, so due/overdue tracking (app/analysis/service.py) only knows what's
    been entered here.
    """

    __tablename__ = "service_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    odo_km: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(String(200), default="")


class PushSubscription(Base):
    """A browser's Web Push subscription (one row per device/browser that
    tapped "Enable notifications"). Not per-vehicle — a device gets notified
    about whichever car is currently the account's active pick."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The push service URL (unique per browser subscription) — the natural
    # dedupe key when the same device re-subscribes.
    endpoint: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime)
