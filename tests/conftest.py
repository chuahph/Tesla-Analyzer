"""Test fixtures: an in-memory database seeded with sample data."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.sample_data import generate


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


@pytest.fixture
def seeded(session):
    generate(session, days=120, seed=7)
    return session


@pytest.fixture(autouse=True)
def _no_sleep_backoff():
    """Disable the asleep back-off unless a test asks for it.

    Real crons tick minutes apart, so a window that suspends polling while the
    car sleeps is invisible to them. Tests fire /api/sync back to back, where a
    20-minute window would swallow every follow-up call — the second sync in a
    fixture would return "skipped" instead of exercising the code under test.

    Off by default, so the feature is opt-in for the one test that is about it
    rather than silently shaping the fifty that are not.
    """
    from app.config import get_settings

    settings = get_settings()
    was = settings.sleep_recheck_min
    settings.sleep_recheck_min = 0.0
    try:
        yield
    finally:
        settings.sleep_recheck_min = was
