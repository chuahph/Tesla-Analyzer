"""Database engine, session handling and schema creation."""
from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    settings = get_settings()
    url = settings.database_url
    # Render/Heroku-style URLs use the legacy "postgres://" scheme, which
    # SQLAlchemy 2.x no longer accepts — normalise it.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path == ":memory:":
            # Share one connection so every session sees the same in-memory DB.
            return create_engine(
                url, connect_args={"check_same_thread": False},
                poolclass=StaticPool, future=True,
            )
        # Ensure the parent directory exists for file-based SQLite DBs.
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        return create_engine(
            url, connect_args={"check_same_thread": False}, future=True
        )
    # Hosted databases drop idle connections; pre-ping revalidates them.
    return create_engine(url, pool_pre_ping=True, future=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def _ensure_column(table: str, column: str, ddl_type: str, default_sql: str) -> None:
    """Defensively add a column to an already-existing table if it's missing —
    a minimal stand-in for a migration tool (no Alembic in this project).
    create_all() only creates missing *tables*; it never alters ones that
    already exist, so a column added to a model after a database has already
    been created would otherwise silently never appear there. Only ever
    additive (a new column with a default) — never destructive."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table(table):
        return  # brand new table — create_all() above already gave it every column
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type} DEFAULT {default_sql}"))


def _widen_to_text(table: str, column: str) -> None:
    """Relax a bounded VARCHAR to unbounded TEXT on an existing table.

    The additive-only ``_ensure_column`` above can't do this: the column is
    already there, just too small. Widening is the one ALTER that is always
    safe — every value that fitted before still fits — and on Postgres a
    varchar-to-text change is metadata-only. SQLite never enforced the bound
    in the first place, so it is a no-op there.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if not inspector.has_table(table):
        return
    if engine.dialect.name != "postgresql":
        return
    current = {c["name"]: c["type"] for c in inspector.get_columns(table)}
    kind = current.get(column)
    if kind is None or getattr(kind, "length", None) is None:
        return  # missing, or already unbounded
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT"))


def init_db() -> None:
    """Create all tables. Models must be imported before calling this."""
    from . import models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    # Runtime state values outgrew their original 2048-char bound (see
    # models.Setting.value) — and the failure mode was a 500 on every sync,
    # not a truncated string.
    _widen_to_text("settings", "value")
    _ensure_column("drives", "idle_min", "FLOAT", "0.0")
    _ensure_column("drives", "idle_tracked", "BOOLEAN", "FALSE")
    _ensure_column("drives", "start_area", "VARCHAR(120)", "''")
    _ensure_column("drives", "end_area", "VARCHAR(120)", "''")
    _ensure_column("drives", "start_coords", "VARCHAR(40)", "''")
    _ensure_column("drives", "end_coords", "VARCHAR(40)", "''")
    _ensure_column("drives", "tag", "VARCHAR(20)", "''")
    _ensure_column("drives", "cost_override", "FLOAT", "NULL")
    _ensure_column("drives", "energy_estimated", "BOOLEAN", "FALSE")
    _ensure_column("drives", "tail_trim_sec", "FLOAT", "NULL")
    _ensure_column("drives", "start_lost_km", "FLOAT", "NULL")
    _ensure_column("drives", "end_lost_km", "FLOAT", "NULL")
    _ensure_column("drives", "start_recovered_km", "FLOAT", "NULL")
    _ensure_column("drives", "start_park_min", "FLOAT", "NULL")
    _ensure_column("drives", "climate_min", "FLOAT", "NULL")
    _ensure_column("drives", "start_gap_sec", "FLOAT", "NULL")
    _ensure_column("drives", "end_gap_sec", "FLOAT", "NULL")
    _ensure_column("drives", "end_est_km", "FLOAT", "NULL")
    _ensure_column("drives", "end_est_verified", "BOOLEAN", "NULL")
    _ensure_column("arrival_tail_samples", "place", "VARCHAR(120)", "''")
    _ensure_column("drives", "start_odo_km", "FLOAT", "NULL")
    _ensure_column("drives", "end_odo_km", "FLOAT", "NULL")
    _ensure_column("charges", "is_free", "BOOLEAN", "FALSE")
    _ensure_column("charges", "billed_kwh", "FLOAT", "0.0")
    _ensure_column("charges", "implied_capacity_kwh", "FLOAT", "NULL")
    _ensure_column("charges", "capacity_samples", "INTEGER", "NULL")
    _ensure_column("charges", "price_source", "VARCHAR(10)", "''")
    # 0, not NULL: "not set" and "set to zero" mean the same thing here (fall
    # back to the global pace), so there is nothing for NULL to carry.
    _ensure_column("places", "departure_pace_kmh", "FLOAT", "0.0")
    _ensure_column("places", "parked_draw_w", "FLOAT", "0.0")
    _ensure_column("places", "arrival_tail_km", "FLOAT", "0.0")
    # NULL default (not FALSE) — "unknown" (older reading, car didn't report
    # it) must stay distinguishable from a confirmed off.
    _ensure_column("battery_readings", "sentry_mode", "BOOLEAN", "NULL")
    _ensure_column("battery_readings", "climate_on", "BOOLEAN", "NULL")
    _ensure_column("battery_readings", "cabin_overheat_protection", "VARCHAR(10)", "NULL")
    _ensure_column("battery_readings", "cabin_overheat_protection_actively_cooling", "BOOLEAN", "NULL")
    _ensure_column("battery_readings", "dashcam_state", "VARCHAR(16)", "NULL")
    _ensure_column("battery_readings", "center_display_state", "INTEGER", "NULL")


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
