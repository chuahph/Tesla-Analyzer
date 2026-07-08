"""SQLAlchemy ORM models for vehicles, drives and charging sessions."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String
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
    start_location: Mapped[str] = mapped_column(String(120), default="")
    end_location: Mapped[str] = mapped_column(String(120), default="")

    # Real (not estimated) minutes spent stopped >= sync.IDLE_STREAK_MIN,
    # tracked live while the trip was open. 0.0 means either genuinely no
    # sustained stop, or (for trips logged before this field existed, or
    # reconstructed across an unpolled gap) unknown — analysis code falls
    # back to the avg/max-speed estimate in that case.
    idle_min: Mapped[float] = mapped_column(Float, default=0.0)

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

    vehicle: Mapped["Vehicle"] = relationship(back_populates="charges")
