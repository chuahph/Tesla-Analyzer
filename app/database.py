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


def init_db() -> None:
    """Create all tables. Models must be imported before calling this."""
    from . import models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
