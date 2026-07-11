"""Higher-level actions shared by the API: importing data and linking accounts."""
from __future__ import annotations

from sqlalchemy import delete

from . import pricing_prefs, state
from .config import get_settings
from .models import BatteryReading, Charge, Drive, ServiceRecord, Vehicle


def _wipe(session) -> None:
    # Every table keyed by vehicle_id must be cleared together with Vehicle:
    # SQLite reuses freed integer ids, so any row left behind silently
    # attaches to whatever NEW car later lands on the same id (the exact
    # cross-vehicle leak once shipped with BatteryReading).
    session.execute(delete(Drive))
    session.execute(delete(Charge))
    session.execute(delete(BatteryReading))
    session.execute(delete(ServiceRecord))
    session.execute(delete(Vehicle))
    session.commit()


def purge_demo(session) -> None:
    """Remove the seeded demo vehicle and its data (kept until real data arrives)."""
    demo_ids = [
        v.id for v in session.query(Vehicle).filter(Vehicle.vin.like("DEMO%"))
    ]
    if not demo_ids:
        return
    session.execute(delete(Drive).where(Drive.vehicle_id.in_(demo_ids)))
    session.execute(delete(Charge).where(Charge.vehicle_id.in_(demo_ids)))
    session.execute(delete(BatteryReading).where(BatteryReading.vehicle_id.in_(demo_ids)))
    session.execute(delete(ServiceRecord).where(ServiceRecord.vehicle_id.in_(demo_ids)))
    session.execute(delete(Vehicle).where(Vehicle.id.in_(demo_ids)))
    session.commit()


def clear_drives(session) -> int:
    """Delete every recorded trip (charges and battery readings are kept).

    Also drops any half-open trip state so the next sync starts a fresh,
    clean trip instead of closing one anchored in the deleted history.
    """
    n = session.query(Drive).count()
    session.execute(delete(Drive))
    session.commit()
    state.put(session, state.OPEN_TRIP_KEY, "")
    return n


def delete_drives(session, ids: list[int]) -> int:
    """Delete only the trips whose ids are given (charges/battery kept)."""
    if not ids:
        return 0
    n = session.query(Drive).filter(Drive.id.in_(ids)).count()
    session.execute(delete(Drive).where(Drive.id.in_(ids)))
    session.commit()
    return n


# Fixed quick-tag categories the UI cycles through; the column itself accepts
# any short string, so a future free-text tag entered another way still
# round-trips fine — this is just what the one-tap cycle offers.
TAG_CYCLE = ("", "work", "personal")


def tag_drive(session, drive_id: int, tag: str) -> bool:
    """Set (or clear, with tag="") a single trip's category. Returns whether
    the trip was found."""
    drive = session.get(Drive, drive_id)
    if drive is None:
        return False
    drive.tag = tag[:20]
    session.commit()
    return True


def replace_with_import(
    session, drives: list[dict], charges: list[dict], *, name: str = "Imported Tesla"
) -> dict:
    """Replace all stored data with an imported data set. Returns a small summary."""
    settings = get_settings()
    _wipe(session)

    vehicle = Vehicle(vin=f"IMPORT-{name[:16]}", name=name, model="Imported")
    session.add(vehicle)
    session.flush()

    for d in drives:
        session.add(Drive(vehicle_id=vehicle.id, **d))

    for c in charges:
        c = dict(c)
        if not c.get("cost") and c.get("energy_added_kwh"):
            rate = pricing_prefs.rate_for_charge(
                session, settings, c.get("location", ""), c.get("charge_type") == "DC", c["start_time"])
            c["cost"] = round(c["energy_added_kwh"] * rate, 2)
        session.add(Charge(vehicle_id=vehicle.id, **c))

    session.commit()
    state.put(session, state.SOURCE_KEY, "imported")
    return {
        "source": "imported",
        "vehicle": name,
        "imported_drives": len(drives),
        "imported_charges": len(charges),
    }


def unlink(session) -> dict:
    """Disconnect the currently-linked Tesla account so a different one can be
    linked. The logged history is kept (marked as imported); the token and all
    live-session state — including every car's per-VIN snapshot — are cleared."""
    state.delete(
        session,
        state.TOKEN_KEY,
        state.REFRESH_KEY,
        state.BASE_URL_KEY,
        state.ACTIVE_VIN_KEY,
        state.LINKED_VIN_KEY,
        state.SNAPSHOT_KEY,
        state.OPEN_TRIP_KEY,
        state.OPEN_CHARGE_KEY,
        state.LAST_ACTIVE_KEY,
        state.SUSPEND_KEY,
    )
    state.delete_scoped(
        session, state.SNAPSHOT_KEY, state.OPEN_TRIP_KEY, state.OPEN_CHARGE_KEY
    )
    # Keep the history visible, but out of "live" mode until a car is linked again.
    state.put(session, state.SOURCE_KEY, "imported")
    return {"status": "unlinked"}


def link_with_token(
    session, access_token: str, refresh_token: str = "", base_url: str | None = None
) -> dict:
    """Validate a Tesla token, register the vehicle and switch to linked mode."""
    from .tesla_client import TeslaClient

    settings = get_settings()
    base_url = base_url or settings.tesla_api_base_url
    client = TeslaClient(access_token=access_token, base_url=base_url)

    vehicles = client.list_vehicles()  # raises on an invalid token
    if not vehicles:
        raise ValueError("Token is valid but no vehicles are associated with this account.")

    # Real data replaces the seeded sample; the dashboard follows a linked car.
    purge_demo(session)

    # Register EVERY car on the account (not just the first) so a multi-car
    # account can switch between them from the dashboard's car picker.
    account_vins = []
    for i, v in enumerate(vehicles):
        vin = v.get("vin") or f"LINKED-{i}"
        account_vins.append(vin)
        existing = session.query(Vehicle).filter(Vehicle.vin == vin).first()
        if existing is None:
            session.add(Vehicle(
                vin=vin,
                name=v.get("display_name") or "My Tesla",
                model="Tesla",
            ))
    session.commit()

    # Keep the currently-active car if it's still on the account; else default
    # to the first. LINKED_VIN mirrors it for older single-car code paths.
    prev_active = state.get(session, state.ACTIVE_VIN_KEY)
    active = prev_active if prev_active in account_vins else account_vins[0]
    state.put(session, state.ACTIVE_VIN_KEY, active)
    state.put(session, state.LINKED_VIN_KEY, active)

    state.put(session, state.TOKEN_KEY, access_token)
    if refresh_token:
        state.put(session, state.REFRESH_KEY, refresh_token)
    state.put(session, state.BASE_URL_KEY, base_url)
    state.put(session, state.SOURCE_KEY, "linked")

    return {
        "source": "linked",
        "vehicles": [
            {"vin": x.get("vin"), "name": x.get("display_name")} for x in vehicles
        ],
        "note": "Account linked. Run the collector (python run.py collect) to log new "
        "drives and charges over time.",
    }
