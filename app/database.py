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


def init_db() -> None:
    """Create all tables. Models must be imported before calling this."""
    from . import models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _ensure_column("drives", "idle_min", "FLOAT", "0.0")
    _ensure_column("drives", "idle_tracked", "BOOLEAN", "FALSE")
    _ensure_column("drives", "start_area", "VARCHAR(120)", "''")
    _ensure_column("drives", "end_area", "VARCHAR(120)", "''")
    _ensure_column("drives", "start_coords", "VARCHAR(40)", "''")
    _ensure_column("drives", "end_coords", "VARCHAR(40)", "''")
    _ensure_column("drives", "tag", "VARCHAR(20)", "''")
    _ensure_column("charges", "is_free", "BOOLEAN", "FALSE")
    _ensure_column("charges", "price_source", "VARCHAR(10)", "''")
    # NULL default (not FALSE) — "unknown" (older reading, car didn't report
    # it) must stay distinguishable from a confirmed off.
    _ensure_column("battery_readings", "sentry_mode", "BOOLEAN", "NULL")
    _ensure_column("battery_readings", "climate_on", "BOOLEAN", "NULL")


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
