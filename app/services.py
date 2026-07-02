"""Higher-level actions shared by the API: importing data and linking accounts."""
from __future__ import annotations

from sqlalchemy import delete

from . import state
from .config import get_settings
from .models import Charge, Drive, Vehicle


def _wipe(session) -> None:
    session.execute(delete(Drive))
    session.execute(delete(Charge))
    session.execute(delete(Vehicle))
    session.commit()


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
            c["cost"] = round(c["energy_added_kwh"] * settings.energy_price_per_kwh, 2)
        session.add(Charge(vehicle_id=vehicle.id, **c))

    session.commit()
    state.put(session, state.SOURCE_KEY, "imported")
    return {
        "source": "imported",
        "vehicle": name,
        "imported_drives": len(drives),
        "imported_charges": len(charges),
    }


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

    v = vehicles[0]
    vin = v.get("vin", "LINKED-UNKNOWN")
    existing = session.query(Vehicle).filter(Vehicle.vin == vin).first()
    if existing is None:
        existing = Vehicle(
            vin=vin,
            name=v.get("display_name") or "My Tesla",
            model="Tesla",
        )
        session.add(existing)
        session.commit()

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
