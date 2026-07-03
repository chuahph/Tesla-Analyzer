"""Charging pattern analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..models import Charge
from . import mean, safe_div


def analyze(charges: list[Charge]) -> dict[str, Any]:
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

    # Charging start hour distribution (off-peak overnight is cheapest).
    by_hour: dict[int, int] = defaultdict(int)
    for c in charges:
        by_hour[c.start_time.hour] += 1

    # Locations.
    by_location = Counter(c.location for c in charges if c.location)

    return {
        "available": True,
        "total_sessions": len(charges),
        "total_energy_kwh": round(total_energy, 1),
        "total_cost": round(total_cost, 2),
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
        "top_locations": by_location.most_common(5),
    }
