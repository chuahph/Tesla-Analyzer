"""Pydantic schemas for API responses."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class VehicleOut(BaseModel):
    id: int
    vin: str
    name: str
    model: str
    trim: str
    rated_range_km: float
    battery_capacity_kwh: float

    class Config:
        from_attributes = True


class DriveOut(BaseModel):
    id: int
    start_time: datetime
    end_time: datetime
    distance_km: float
    duration_min: float
    start_soc: float
    end_soc: float
    energy_used_kwh: float
    avg_speed_kmh: float
    max_speed_kmh: float
    outside_temp_c: float
    start_location: str
    end_location: str
    wh_per_km: float
    idle_min: float
    idle_tracked: bool

    class Config:
        from_attributes = True


class ChargeOut(BaseModel):
    id: int
    start_time: datetime
    end_time: datetime
    duration_min: float
    start_soc: float
    end_soc: float
    energy_added_kwh: float
    charge_type: str
    max_power_kw: float
    location: str
    cost: float

    class Config:
        from_attributes = True
