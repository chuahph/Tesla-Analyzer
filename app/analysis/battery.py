"""Battery health / degradation assessment.

Signal: each sync stores the car's rated remaining range at a known SoC.
``range_km / (soc/100)`` projects the full-pack rated range; comparing recent
projections against the best the pack has shown gives a degradation estimate
without needing the (trim-specific) factory figure. Charging behaviour factors
(100% charges, DC share, average target) come from the charging analysis and
are folded into the assessment text.
"""
from __future__ import annotations

from typing import Any

from . import mean, percentile

MIN_READINGS = 5     # below this the estimate is too noisy to show
MIN_SOC = 20.0       # low-SoC readings project unreliably
RECENT_N = 10        # projections averaged for the "current" estimate


def analyze(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """``readings``: dicts with soc / range_km, oldest first."""
    projections = [
        (r, r["range_km"] / (r["soc"] / 100.0))
        for r in readings
        if r.get("soc", 0) >= MIN_SOC and r.get("range_km", 0) > 0
    ]
    if len(projections) < MIN_READINGS:
        return {
            "available": False,
            "n_readings": len(projections),
            "note": f"Collecting data — {len(projections)}/{MIN_READINGS} usable "
                    "battery readings so far. The estimate appears after a few "
                    "days of syncing.",
        }

    values = [p for _, p in projections]
    baseline_km = percentile(values, 0.95)          # best the pack has shown
    current_km = mean(values[-RECENT_N:])           # where it is now
    degradation = max(0.0, 100.0 * (baseline_km - current_km) / baseline_km) if baseline_km else 0.0
    health = 100.0 - degradation

    socs = [r["soc"] for r, _ in projections]
    return {
        "available": True,
        "n_readings": len(projections),
        "health_pct": round(health, 1),
        "degradation_pct": round(degradation, 1),
        "est_full_range_km": round(current_km, 0),
        "baseline_full_range_km": round(baseline_km, 0),
        "min_soc_seen": round(min(socs), 0),
        "avg_soc": round(mean(socs), 0),
    }
