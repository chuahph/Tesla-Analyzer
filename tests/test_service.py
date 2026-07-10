"""Tests for the service/maintenance interval tracker (app/analysis/service.py)."""
from datetime import datetime, timedelta

from app.analysis.service import DUE_SOON_DAYS, DUE_SOON_KM, SERVICE_INTERVALS, due_status

NOW = datetime(2026, 7, 10)


def test_never_logged_type_is_unknown_not_overdue():
    """A type with no record ever should read 'unknown', not 'overdue' —
    guessing it's overdue on day one of tracking would cry wolf for every
    car, since it may well have been done before tracking started."""
    rows = due_status([], current_odo_km=20_000, now=NOW)
    assert len(rows) == len(SERVICE_INTERVALS)
    assert all(r["status"] == "unknown" for r in rows)
    assert all(r["last_date"] is None and r["due_date"] is None for r in rows)


def test_tire_rotation_due_by_odometer():
    # Rotated at 10,000 km; interval is 10,000 km / 12 months. Now at
    # 19,500 km — 500 km short of due (within DUE_SOON_KM) but well within
    # the 12-month window, so it's "due_soon" purely on distance.
    records = [{"type": "Tire Rotation", "date": NOW - timedelta(days=60), "odo_km": 10_000.0}]
    rows = due_status(records, current_odo_km=19_500.0, now=NOW)
    row = next(r for r in rows if r["type"] == "Tire Rotation")
    assert row["due_odo_km"] == 20_000.0
    assert row["status"] == "due_soon"

    # Past the odometer threshold -> overdue, even though it's not yet been
    # 12 months.
    rows2 = due_status(records, current_odo_km=20_500.0, now=NOW)
    row2 = next(r for r in rows2 if r["type"] == "Tire Rotation")
    assert row2["status"] == "overdue"


def test_time_only_item_ignores_odometer():
    # Cabin Air Filter has no km interval — only time matters.
    records = [{"type": "Cabin Air Filter", "date": NOW - timedelta(days=800), "odo_km": 5_000.0}]
    rows = due_status(records, current_odo_km=9_999_999.0, now=NOW)
    row = next(r for r in rows if r["type"] == "Cabin Air Filter")
    assert row["due_odo_km"] is None
    # ~800 days > 24 months (~730 days) -> overdue regardless of odometer.
    assert row["status"] == "overdue"


def test_ok_when_well_within_both_dimensions():
    records = [{"type": "Tire Rotation", "date": NOW - timedelta(days=30), "odo_km": 10_000.0}]
    rows = due_status(records, current_odo_km=11_000.0, now=NOW)
    row = next(r for r in rows if r["type"] == "Tire Rotation")
    assert row["status"] == "ok"


def test_unknown_type_in_records_is_ignored():
    records = [{"type": "Windshield Wipers", "date": NOW, "odo_km": 5_000.0}]
    rows = due_status(records, current_odo_km=5_000.0, now=NOW)
    assert all(r["status"] == "unknown" for r in rows)   # the bogus type never matched


def test_most_recent_record_per_type_wins():
    records = [
        {"type": "Tire Rotation", "date": NOW - timedelta(days=400), "odo_km": 1_000.0},
        {"type": "Tire Rotation", "date": NOW - timedelta(days=10), "odo_km": 15_000.0},
    ]
    rows = due_status(records, current_odo_km=15_500.0, now=NOW)
    row = next(r for r in rows if r["type"] == "Tire Rotation")
    assert row["last_odo_km"] == 15_000.0
    assert row["status"] == "ok"
