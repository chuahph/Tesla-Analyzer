"""Service/maintenance interval tracking (tyre rotation, brake fluid, ...).

Deliberately simple: fixed, widely-published interval guidance per type,
compared against the most recent logged record of that type. Not a
telemetry feature — the car doesn't report service history over the API,
so this only knows what the user has logged here.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# type -> (interval_km, interval_months). Either may be None (that dimension
# doesn't apply to this item). Tyre rotation is due at whichever comes first;
# the rest are purely time-based (rubber/fluid ages regardless of mileage).
# Figures are Tesla's own published general guidance, not model-year-exact.
SERVICE_INTERVALS: dict[str, tuple[float | None, float | None]] = {
    "Tire Rotation": (10_000.0, 12.0),
    "Cabin Air Filter": (None, 24.0),
    "Brake Fluid": (None, 24.0),
    "A/C Desiccant Bag": (None, 48.0),
    "Annual Service": (None, 12.0),
}

# A due date/odometer within this margin counts as "due soon" rather than
# "ok" — enough notice to book it before it's actually overdue.
DUE_SOON_KM = 1_000.0
DUE_SOON_DAYS = 30


def due_status(
    records: list[dict[str, Any]], current_odo_km: float | None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One row per known service type: last done (date/odo), next due
    (date/odo where the interval applies), and a status.

    ``records``: dicts with type/date/odo_km, any order — only the most
    recent per type is used. ``status`` is one of:
      - "unknown" — never logged. Deliberately not "overdue": a car that
        just started being tracked may well have had it done before, and
        assuming otherwise would cry wolf for every car on day one.
      - "ok" / "due_soon" / "overdue" — the usual meaning, by whichever of
        date/odometer is more pressing.
    """
    now = now or datetime.now()
    by_type: dict[str, dict] = {}
    for r in records:
        t = r.get("type")
        if t not in SERVICE_INTERVALS:
            continue
        if t not in by_type or r["date"] > by_type[t]["date"]:
            by_type[t] = r

    rows = []
    for t, (interval_km, interval_months) in SERVICE_INTERVALS.items():
        last = by_type.get(t)
        if last is None:
            rows.append({
                "type": t, "last_date": None, "last_odo_km": None,
                "due_date": None, "due_odo_km": None, "status": "unknown",
            })
            continue

        due_date = last["date"] + timedelta(days=interval_months * 30.44) if interval_months else None
        due_odo = (last.get("odo_km") or 0.0) + interval_km if interval_km else None

        overdue = due_soon = False
        if due_date is not None:
            days_left = (due_date - now).days
            overdue = overdue or days_left < 0
            due_soon = due_soon or (0 <= days_left <= DUE_SOON_DAYS)
        if due_odo is not None and current_odo_km is not None:
            km_left = due_odo - current_odo_km
            overdue = overdue or km_left < 0
            due_soon = due_soon or (0 <= km_left <= DUE_SOON_KM)

        rows.append({
            "type": t,
            "last_date": last["date"],
            "last_odo_km": last.get("odo_km"),
            "due_date": due_date,
            "due_odo_km": due_odo,
            "status": "overdue" if overdue else "due_soon" if due_soon else "ok",
        })
    return rows
