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
