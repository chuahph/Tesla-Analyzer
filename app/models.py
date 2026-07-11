"""SQLAlchemy ORM models for vehicles, drives and charging sessions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Setting(Base):
    """Simple key/value store for runtime configuration (e.g. a linked token)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(2048), default="")


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
    idle_tracked: Mapped[bool] = mapped_column(Boolean, default=False)

    # User-assigned category ("work" / "personal", or any free text) for
    # expense-claim/cost-splitting purposes. "" = untagged.
    tag: Mapped[str] = mapped_column(String(20), default="")

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
