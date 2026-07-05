"""Runtime application state stored in the ``settings`` table.

This lets the dashboard switch a running instance between demo, an imported
data set, and a linked Tesla account without restarting or editing ``.env``.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Setting

# Keys
TOKEN_KEY = "tesla_access_token"
REFRESH_KEY = "tesla_refresh_token"
BASE_URL_KEY = "tesla_api_base_url"
SOURCE_KEY = "data_source"  # one of: demo | imported | linked
SNAPSHOT_KEY = "last_snapshot"  # JSON of the last synced vehicle snapshot
LINKED_VIN_KEY = "linked_vin"  # VIN of the account-linked vehicle
ACTIVE_VIN_KEY = "active_vin"  # VIN of the car the dashboard shows / the manual sync wakes
OPEN_TRIP_KEY = "open_trip"  # JSON of a trip in progress (car in gear)
OPEN_CHARGE_KEY = "open_charge"  # JSON of a charge in progress
LAST_ACTIVE_KEY = "last_active_ts"  # epoch of the last driving/charging/occupied snapshot
SUSPEND_KEY = "suspend_until_ts"  # epoch until which cron polling stays quiet (car sleep window)


def get(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def put(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.commit()


def scoped(base_key: str, vin: str) -> str:
    """Per-car state key namespaced by VIN, so each linked car keeps its own
    snapshot / open-trip / open-charge without clobbering the others. Falls back
    to the bare key when no VIN is known (keeps single-car behaviour identical)."""
    return f"{base_key}::{vin}" if vin else base_key


def active_vin(session: Session) -> str:
    """VIN of the car the dashboard follows — the explicit pick, else the link."""
    return get(session, ACTIVE_VIN_KEY) or get(session, LINKED_VIN_KEY)


def active_token(session: Session) -> str:
    """A token linked at runtime takes precedence over the .env token."""
    return get(session, TOKEN_KEY) or get_settings().tesla_access_token


def active_base_url(session: Session) -> str:
    return get(session, BASE_URL_KEY) or get_settings().tesla_api_base_url


def data_source(session: Session) -> str:
    explicit = get(session, SOURCE_KEY)
    if explicit:
        return explicit
    return "live" if active_token(session) else "demo"


def is_live(session: Session) -> bool:
    return bool(active_token(session))
