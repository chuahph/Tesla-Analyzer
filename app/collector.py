"""Data collection.

In DEMO mode this seeds the database with generated sample data. In live mode
it polls the Tesla API, detecting drive and charge sessions from successive
vehicle_data snapshots and persisting completed sessions.

The live collector is intentionally compact: it tracks state transitions
(parked → driving → parked, unplugged → charging → unplugged) and writes a row
when a session completes. A full-fidelity GPS/telemetry logger is out of scope,
but this captures everything the analytics need.
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import func, select

from . import pricing_prefs
from .config import get_settings
from .database import SessionLocal, init_db
from .models import Charge, Drive, Vehicle
from .sample_data import generate


def seed_demo_if_empty(days: int = 120) -> None:
    init_db()
    with SessionLocal() as session:
        count = session.scalar(select(func.count()).select_from(Vehicle))
        if count:
            return
        generate(session, days=days)
        print(f"[collector] Seeded {days} days of demo data.")


def _ensure_vehicle(session, vin, name, model) -> Vehicle:
    vehicle = session.scalars(select(Vehicle).where(Vehicle.vin == vin)).first()
    if vehicle is None:
        vehicle = Vehicle(vin=vin, name=name, model=model)
        session.add(vehicle)
        session.commit()
    return vehicle


def run_live(poll_interval: int | None = None) -> None:  # pragma: no cover - needs API
    """Poll the Tesla API and persist drive/charge sessions as they complete."""
    from .tesla_client import TeslaClient

    settings = get_settings()
    poll_interval = poll_interval or settings.poll_interval_seconds
    init_db()
    client = TeslaClient()

    vehicles = client.list_vehicles()
    if not vehicles:
        print("[collector] No vehicles on this account.")
        return
    api_id = vehicles[0].get("id_s") or vehicles[0].get("id")

    drive_state: dict | None = None
    charge_state: dict | None = None

    print("[collector] Live polling started. Ctrl-C to stop.")
    while True:
        try:
            data = client.vehicle_data(api_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[collector] poll error: {exc}")
            time.sleep(poll_interval)
            continue

        with SessionLocal() as session:
            vehicle = _ensure_vehicle(
                session,
                data.get("vin", vehicles[0].get("vin", "UNKNOWN")),
                data.get("display_name", "My Tesla"),
                (data.get("vehicle_config") or {}).get("car_type", "Model 3"),
            )
            drive_state, charge_state = _process_snapshot(
                session, vehicle, data, drive_state, charge_state
            )
        time.sleep(poll_interval)


def _process_snapshot(session, vehicle, data, drive_state, charge_state):  # pragma: no cover
    """Detect session boundaries from a snapshot. Simplified state machine."""
    ds = data.get("drive_state", {}) or {}
    cs = data.get("charge_state", {}) or {}
    clim = data.get("climate_state", {}) or {}
    now = datetime.now()
    shift = ds.get("shift_state")

    # --- Drive boundaries ---
    if shift in ("D", "R", "N") and drive_state is None:
        drive_state = {
            "start_time": now,
            "start_soc": cs.get("battery_level", 0),
            "start_odo": ds.get("odometer", 0),
            "max_speed": ds.get("speed") or 0,
            "temp": clim.get("outside_temp", 20),
        }
    elif shift in (None, "P") and drive_state is not None:
        dist = (ds.get("odometer", 0) - drive_state["start_odo"]) * 1.60934
        dur = (now - drive_state["start_time"]).total_seconds() / 60.0
        end_soc = cs.get("battery_level", drive_state["start_soc"])
        energy = (drive_state["start_soc"] - end_soc) / 100.0 * vehicle.battery_capacity_kwh
        if dist > 0.1:
            session.add(
                Drive(
                    vehicle_id=vehicle.id,
                    start_time=drive_state["start_time"],
                    end_time=now,
                    distance_km=round(dist, 1),
                    duration_min=round(dur, 1),
                    start_soc=drive_state["start_soc"],
                    end_soc=end_soc,
                    energy_used_kwh=round(max(energy, 0), 2),
                    avg_speed_kmh=round(dist / (dur / 60.0), 1) if dur else 0,
                    max_speed_kmh=round(drive_state["max_speed"] * 1.60934, 1),
                    outside_temp_c=drive_state["temp"],
                )
            )
            session.commit()
        drive_state = None
    elif drive_state is not None:
        spd = (ds.get("speed") or 0)
        drive_state["max_speed"] = max(drive_state["max_speed"], spd)

    # --- Charge boundaries ---
    charging = cs.get("charging_state") == "Charging"
    if charging and charge_state is None:
        charge_state = {
            "start_time": now,
            "start_soc": cs.get("battery_level", 0),
            "max_power": cs.get("charger_power", 0),
            "fast": cs.get("fast_charger_present", False),
            "temp": clim.get("outside_temp", 20),
        }
    elif not charging and charge_state is not None:
        dur = (now - charge_state["start_time"]).total_seconds() / 60.0
        end_soc = cs.get("battery_level", charge_state["start_soc"])
        energy = (end_soc - charge_state["start_soc"]) / 100.0 * vehicle.battery_capacity_kwh
        settings = get_settings()
        is_dc = charge_state["fast"]
        if energy > 0.1:
            source, rate = pricing_prefs.resolve_source_and_rate(session, settings, "", is_dc, now)
            session.add(
                Charge(
                    vehicle_id=vehicle.id,
                    start_time=charge_state["start_time"],
                    end_time=now,
                    duration_min=round(dur, 1),
                    start_soc=charge_state["start_soc"],
                    end_soc=end_soc,
                    energy_added_kwh=round(energy, 2),
                    charge_type="DC" if is_dc else "AC",
                    max_power_kw=charge_state["max_power"],
                    cost=round(energy * rate, 2),
                    outside_temp_c=charge_state["temp"],
                    price_source=source,
                )
            )
            session.commit()
        charge_state = None
    elif charge_state is not None:
        charge_state["max_power"] = max(charge_state["max_power"], cs.get("charger_power", 0))

    return drive_state, charge_state
