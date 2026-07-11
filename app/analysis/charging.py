"""Charging pattern analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from ..models import Charge, Drive
from . import mean, safe_div


def _infer_location(charge: Charge, drives: list[Drive]) -> str:
    """Best guess for where a charge happened when it has no GPS of its own.

    A car charges where its last drive ended, so borrow the end location of
    the drive that finished closest to the charge's start (within 2 hours).
    """
    best, best_gap = "", None
    for d in drives:
        if not d.end_location:
            continue
        gap = abs((charge.start_time - d.end_time).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = d.end_location, gap
    return best if best_gap is not None and best_gap <= 7200 else ""


def analyze(charges: list[Charge], drives: list[Drive] | None = None) -> dict[str, Any]:
    if not charges:
        return {"available": False}

    ac = [c for c in charges if c.charge_type == "AC"]
    dc = [c for c in charges if c.charge_type == "DC"]

    total_energy = sum(c.energy_added_kwh for c in charges)
    total_cost = sum(c.cost for c in charges)
    ac_energy = sum(c.energy_added_kwh for c in ac)
    dc_energy = sum(c.energy_added_kwh for c in dc)

    # How often each end-SoC target is used (100% charges are notable).
    end_soc_targets = Counter(int(round(c.end_soc / 5.0) * 5) for c in charges)
    full_charges = sum(1 for c in charges if c.end_soc >= 99)

    # Charging start hour distribution (off-peak overnight is cheapest), by
    # session count and by energy — the smart charging advisor needs the
    # latter to size a real currency saving, not just "many sessions".
    by_hour: dict[int, int] = defaultdict(int)
    energy_by_hour: dict[int, float] = defaultdict(float)
    for c in charges:
        by_hour[c.start_time.hour] += 1
        energy_by_hour[c.start_time.hour] += c.energy_added_kwh

    # Locations. Charges logged without GPS (or before location capture existed)
    # have no place name — infer it from the trip that ended nearby, else group
    # by charger type so the card still fills meaningfully.
    drives = drives or []

    def _place(c: Charge) -> str:
        # A named place has letters; a raw "lat, lon" is all digits/punctuation.
        if c.location and any(ch.isalpha() for ch in c.location):
            return c.location
        inferred = _infer_location(c, drives)
        if inferred:
            return inferred
        if c.location:
            return c.location  # raw coords — better than nothing
        return ""

    def _loc(c: Charge) -> str:
        place = _place(c)
        # Tag each spot with its charger type (AC/DC); the type alone is the
        # fallback label when there's no place at all.
        return f"{place} · {c.charge_type}" if place \
            else ("DC fast charger" if c.charge_type == "DC" else "AC / home charger")

    by_location = Counter(_loc(c) for c in charges)
    # Energy delivered and the most recent charge time at each spot, so the card
    # can show "12.4 kWh · 3× · 04 Jul 16:20" and the sequence is clear.
    loc_energy: dict[str, float] = defaultdict(float)
    loc_last: dict[str, Any] = {}
    for c in charges:
        name = _loc(c)
        loc_energy[name] += c.energy_added_kwh
        if name not in loc_last or c.start_time > loc_last[name]:
            loc_last[name] = c.start_time
    # Most recently charged spot first, so the latest session is at the top.
    ordered_names = sorted(
        by_location, key=lambda n: loc_last.get(n) or datetime.min, reverse=True
    )[:5]
    top_locations = [
        [name, by_location[name], round(loc_energy[name], 1),
         loc_last[name].isoformat(timespec="minutes") if loc_last.get(name) else None]
        for name in ordered_names
    ]

    # "Fuel cost" view: what the window's charging cost per 100 km actually
    # driven — the EV counterpart of a petrol car's RM/100km figure.
    drive_km = sum(d.distance_km for d in drives)
    cost_per_100km = round(safe_div(total_cost, drive_km) * 100.0, 2) if drive_km else None

    # Most recent first, so the session someone's most likely trying to fix
    # (the one they just noticed a wrong cost on) is right at the top.
    recent_charges = [
        {
            "id": getattr(c, "id", None),
            "start_time": c.start_time.isoformat(timespec="minutes"),
            "end_time": c.end_time.isoformat(timespec="minutes"),
            "charge_type": c.charge_type,
            "start_soc": c.start_soc,
            "end_soc": c.end_soc,
            "energy_added_kwh": round(c.energy_added_kwh, 2),
            "cost": round(c.cost, 2),
            # The rate actually applied to this session — editable per-session
            # when it doesn't match the configured AC/DC default (a promo
            # rate, a one-off higher public-charger price, etc.).
            "rate_per_kwh": (
                round(c.cost / c.energy_added_kwh, 3) if c.energy_added_kwh else None
            ),
            "location": _place(c),
            "is_free": bool(getattr(c, "is_free", False)),
            # Which of Public/Home/Office this was actually priced against,
            # persisted at charge-price time — None for a custom rate or a
            # session logged before this existed (the frontend falls back
            # to guessing from location text in that case).
            "source": getattr(c, "price_source", "") or None,
        }
        for c in sorted(charges, key=lambda c: c.start_time, reverse=True)
    ]

    return {
        "available": True,
        "total_sessions": len(charges),
        "total_energy_kwh": round(total_energy, 1),
        "total_cost": round(total_cost, 2),
        "ac_cost": round(sum(c.cost for c in ac), 2),
        "dc_cost": round(sum(c.cost for c in dc), 2),
        "cost_per_100km": cost_per_100km,
        "avg_cost_per_kwh": round(safe_div(total_cost, total_energy), 3),
        "ac_sessions": len(ac),
        "dc_sessions": len(dc),
        "ac_energy_kwh": round(ac_energy, 1),
        "dc_energy_kwh": round(dc_energy, 1),
        "dc_energy_share_pct": round(100 * safe_div(dc_energy, total_energy), 1),
        "avg_energy_per_session_kwh": round(mean([c.energy_added_kwh for c in charges]), 1),
        "avg_dc_power_kw": round(mean([c.max_power_kw for c in dc]), 1) if dc else 0.0,
        "full_charges": full_charges,
        "full_charge_share_pct": round(100 * safe_div(full_charges, len(charges)), 1),
        "avg_end_soc": round(mean([c.end_soc for c in charges if c.end_soc > 0]), 0),
        "end_soc_targets": dict(sorted(end_soc_targets.items())),
        "charges_by_hour": {str(h): by_hour.get(h, 0) for h in range(24)},
        "energy_by_hour": {str(h): round(energy_by_hour.get(h, 0.0), 2) for h in range(24)},
        "top_locations": top_locations,
        "recent_charges": recent_charges,
    }
