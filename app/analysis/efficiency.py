"""Efficiency analysis: Wh/km vs temperature, speed and over time."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..models import Drive
from . import linregress, mean


def _temp_bucket(temp: float) -> str:
    if temp < 0:
        return "<0"
    if temp < 10:
        return "0-10"
    if temp < 20:
        return "10-20"
    if temp < 30:
        return "20-30"
    return "30+"


def analyze(drives: list[Drive], rated_wh_per_km: float) -> dict[str, Any]:
    drives = [d for d in drives if d.distance_km > 0]
    if not drives:
        return {"available": False}

    effs = [d.wh_per_km for d in drives]
    avg_eff = mean(effs)

    # Efficiency vs outside temperature.
    by_temp: dict[str, list[float]] = defaultdict(list)
    for d in drives:
        by_temp[_temp_bucket(d.outside_temp_c)].append(d.wh_per_km)
    eff_by_temp = {k: round(mean(v), 1) for k, v in by_temp.items()}

    temp_slope, _ = linregress(
        [d.outside_temp_c for d in drives], effs
    )

    # Weekly efficiency trend (detect seasonal drift / degradation).
    weekly: dict[str, list[float]] = defaultdict(list)
    for d in drives:
        iso = d.start_time.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        weekly[key].append(d.wh_per_km)
    weekly_eff = {k: round(mean(v), 1) for k, v in sorted(weekly.items())}

    # Energy that "should have" been used at the rated figure vs actual.
    total_distance = sum(d.distance_km for d in drives)
    actual_energy = sum(d.energy_used_kwh for d in drives)
    rated_energy = rated_wh_per_km * total_distance / 1000.0
    overshoot_pct = 100 * (actual_energy - rated_energy) / rated_energy if rated_energy else 0.0

    # Range estimate at observed efficiency for a typical 75 kWh usable pack.
    best = sorted(effs)[: max(1, len(effs) // 10)]  # best decile of drives

    return {
        "available": True,
        "avg_efficiency_wh_per_km": round(avg_eff, 1),
        "rated_wh_per_km": rated_wh_per_km,
        "vs_rated_pct": round(overshoot_pct, 1),
        "best_efficiency_wh_per_km": round(min(effs), 1),
        "worst_efficiency_wh_per_km": round(max(effs), 1),
        "efficiency_by_temp": dict(sorted(eff_by_temp.items())),
        "temp_efficiency_slope_wh_per_c": round(temp_slope, 2),
        "weekly_efficiency": weekly_eff,
        "best_decile_efficiency_wh_per_km": round(mean(best), 1),
        "total_distance_km": round(total_distance, 1),
        "total_energy_kwh": round(actual_energy, 1),
    }
