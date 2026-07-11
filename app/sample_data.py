"""Generate realistic sample driving & charging data for DEMO mode.

The generator deliberately bakes in real-world relationships so the analysis
engine has genuine signal to surface:
  * efficiency (Wh/km) degrades in cold weather and at high speed,
  * most charging happens at home on AC overnight, with occasional DC trips,
  * a handful of "bad habits" (frequent 100% charges, hard commutes) so the
    recommendation engine has something concrete to flag.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import Charge, Drive, Vehicle

LOCATIONS = ["Home", "Office", "Gym", "Supermarket", "Parents", "Airport", "Mall"]
SUPERCHARGERS = ["Supercharger - Highway 1", "Supercharger - Downtown", "Supercharger - Rest Stop"]


def _seasonal_temp(day_of_year: int) -> float:
    """A simple yearly temperature curve, coldest in January."""
    base = 15.0
    swing = 14.0
    return base - swing * math.cos(2 * math.pi * (day_of_year - 15) / 365.0)


def _efficiency_wh_per_km(speed_kmh: float, temp_c: float, rated: float) -> float:
    """Model Wh/km as a function of speed and temperature, around a rated value."""
    # Aerodynamic drag dominates above ~60 km/h.
    speed_penalty = max(0.0, (speed_kmh - 55) ** 2) * 0.015
    # HVAC + cold battery penalty when far from ~21 C comfort point.
    temp_penalty = abs(temp_c - 21) * 1.6
    if temp_c < 5:
        temp_penalty += (5 - temp_c) * 3.0  # cold-battery hit
    noise = random.uniform(-8, 8)
    return max(90.0, rated + speed_penalty + temp_penalty + noise)


def generate(session: Session, days: int = 120, seed: int = 42) -> Vehicle:
    """Populate the database with ``days`` of sample data. Returns the vehicle."""
    rng = random.Random(seed)
    random.seed(seed)

    vehicle = Vehicle(
        vin="DEMO0SAMPLE0000001",
        name="Demo Model 3",
        model="Model 3",
        trim="Long Range AWD",
        rated_range_km=560.0,
        battery_capacity_kwh=75.0,
    )
    session.add(vehicle)
    session.flush()

    rated = 148.0  # Wh/km nominal for this model
    capacity = vehicle.battery_capacity_kwh
    # Price demo charges at the app's configured AC/DC rates so the demo
    # dashboard matches what a real synced charge would cost — instead of
    # drifting from the config defaults (which it silently did before).
    from .config import get_settings
    _settings = get_settings()
    ac_price = _settings.energy_price_ac_kwh or _settings.energy_price_per_kwh
    dc_price = _settings.energy_price_dc_kwh or _settings.energy_price_per_kwh

    soc = 80.0  # start state of charge (%)
    start = datetime.now() - timedelta(days=days)

    for d in range(days):
        day = start + timedelta(days=d)
        temp = _seasonal_temp(day.timetuple().tm_yday)
        is_weekday = day.weekday() < 5

        # --- Drives -------------------------------------------------------
        # Each drive's own timing/route/speed is drawn here, in whatever
        # order rng.choice happens to pick — but SoC only ever moves forward
        # through *wall-clock* time, so it's chained below in a second pass
        # sorted by t0, not this draw order. (Keeping the draws themselves in
        # this original order/count means the seeded dataset's overall shape
        # — distances, speeds, locations — is unchanged; only which drive
        # gets soc-chained first now matches when it actually happened.)
        n_drives = rng.choice([2, 2, 3] if is_weekday else [1, 2, 2])
        pending = []
        for _ in range(n_drives):
            hour = rng.choice([7, 8, 12, 17, 18, 19]) + rng.randint(0, 1)
            t0 = day.replace(hour=min(hour, 22), minute=rng.randint(0, 59), second=0, microsecond=0)

            if is_weekday and hour in (7, 8, 17, 18):
                distance = rng.uniform(18, 32)       # commute
                avg_speed = rng.uniform(55, 80)
                origin, dest = "Home", "Office"
            else:
                distance = rng.uniform(4, 45)        # errands / trips
                avg_speed = rng.uniform(30, 95)
                origin = rng.choice(LOCATIONS)
                dest = rng.choice([l for l in LOCATIONS if l != origin])

            max_speed = min(avg_speed + rng.uniform(15, 45), 135)
            duration = (distance / max(avg_speed, 1)) * 60.0
            wh_km = _efficiency_wh_per_km(avg_speed, temp, rated)
            energy = wh_km * distance / 1000.0
            out_temp = round(temp + rng.uniform(-2, 2), 1)
            pending.append((t0, distance, duration, avg_speed, max_speed, energy, out_temp, origin, dest))

        for t0, distance, duration, avg_speed, max_speed, energy, out_temp, origin, dest in sorted(
            pending, key=lambda p: p[0]
        ):
            end_soc = soc - (energy / capacity) * 100.0
            if end_soc < 12:
                # Too low to drive — skip and let a charge happen first.
                break

            # The final day's late-evening events can roll past midnight into
            # what's now "today" — clamp so nothing seeded ever claims to end
            # after the real current moment (breaks any "X <= now" check).
            end_time = min(t0 + timedelta(minutes=duration), datetime.now())
            duration_min = (end_time - t0).total_seconds() / 60.0
            session.add(
                Drive(
                    vehicle_id=vehicle.id,
                    start_time=t0,
                    end_time=end_time,
                    distance_km=round(distance, 1),
                    duration_min=round(duration_min, 1),
                    start_soc=round(soc, 1),
                    end_soc=round(end_soc, 1),
                    energy_used_kwh=round(energy, 2),
                    avg_speed_kmh=round(avg_speed, 1),
                    max_speed_kmh=round(max_speed, 1),
                    outside_temp_c=out_temp,
                    start_location=origin,
                    end_location=dest,
                )
            )
            soc = end_soc

        # --- Charging -----------------------------------------------------
        # Charge overnight at home when SoC drops, occasionally fast-charge.
        if soc < 45 or rng.random() < 0.5:
            dc = rng.random() < 0.18  # ~18% of charges are DC fast charging
            # Some users habitually charge to 100% — flag-worthy behaviour.
            target = rng.choice([80, 80, 90, 100]) if not dc else rng.choice([80, 90])
            if soc >= target:
                continue

            energy_added = (target - soc) / 100.0 * capacity
            if dc:
                power = rng.uniform(120, 250)
                duration = energy_added / power * 60.0 * rng.uniform(1.2, 1.6)
                location = rng.choice(SUPERCHARGERS)
                price = dc_price   # RM per kWh (DC / Supercharger)
                t0 = day.replace(hour=rng.choice([13, 14, 15]), minute=rng.randint(0, 59))
            else:
                power = rng.uniform(7, 11)
                duration = energy_added / power * 60.0
                location = "Home"
                price = ac_price
                t0 = day.replace(hour=rng.choice([22, 23]), minute=rng.randint(0, 59))

            # Same clamp as drives above — an overnight AC charge on the
            # final day can otherwise end after the real current moment.
            end_time = min(t0 + timedelta(minutes=duration), datetime.now())
            duration_min = (end_time - t0).total_seconds() / 60.0
            session.add(
                Charge(
                    vehicle_id=vehicle.id,
                    start_time=t0,
                    end_time=end_time,
                    duration_min=round(duration_min, 1),
                    start_soc=round(soc, 1),
                    end_soc=float(target),
                    energy_added_kwh=round(energy_added, 2),
                    charge_type="DC" if dc else "AC",
                    max_power_kw=round(power, 1),
                    location=location,
                    cost=round(energy_added * price, 2),
                    outside_temp_c=round(temp, 1),
                )
            )
            soc = float(target)

    session.commit()
    return vehicle
