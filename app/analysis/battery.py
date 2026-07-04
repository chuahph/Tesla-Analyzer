"""Battery health / degradation assessment.

Signal: each sync stores the car's rated remaining range at a known SoC.
``range_km / (soc/100)`` projects the full-pack rated range. Health compares
the recent projections against the range the car had when NEW — the factory
figure for the exact variant when we can identify it (e.g. a 2024 Model 3
Long Range AWD "74D"), otherwise the best the pack has shown in our data.
Charging behaviour factors (100% charges, DC share, average target) come from
the charging analysis and are folded into the assessment text.
"""
from __future__ import annotations

import re
from typing import Any

from . import mean, percentile

MIN_READINGS = 5     # below this the estimate is too noisy to show
MIN_SOC = 20.0       # low-SoC readings project unreliably
RECENT_N = 10        # recent projections summarised as the "current" estimate

# Factory rated range at 100% when new, in km (EPA figures — the same scale
# the API's battery_range field uses). Badges are matched as whole tokens in
# the vehicle's model+trim text; first match wins, so keep P-badges first.
NEW_RANGE_KM: list[tuple[str, str, float]] = [
    ("MODEL 3", "P74D", 476.0),  # 2024 Performance (EPA 296 mi)
    ("MODEL 3", "74D", 549.0),   # 2024 Long Range AWD Highland (EPA 341 mi)
    ("MODEL 3", "74", 549.0),    # Long Range RWD badge variations
    ("MODEL 3", "50", 438.0),    # RWD (EPA 272 mi)
    ("MODEL Y", "P74D", 459.0),  # Performance (EPA 285 mi)
    ("MODEL Y", "74D", 531.0),   # Long Range AWD (EPA 330 mi)
    ("MODEL Y", "50", 418.0),    # RWD (EPA 260 mi)
]


def new_range_for(model: str, trim: str) -> float | None:
    """Factory new range for this exact variant, if we recognise the badge."""
    text = f"{model or ''} {trim or ''}".upper()
    tokens = set(re.split(r"[^A-Z0-9]+", text))
    for m, badge, km in NEW_RANGE_KM:
        if m in text and badge in tokens:
            return km
    return None


def analyze(
    readings: list[dict[str, Any]], new_range_km: float | None = None
) -> dict[str, Any]:
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
    current_km = percentile(values[-RECENT_N:], 0.5)  # median resists outliers

    # 100% reference: the factory when-new figure if it plausibly matches the
    # scale of this car's readings; otherwise the measured best. (A region
    # whose firmware reports a different range scale would make the EPA spec
    # meaningless — measured projections beating spec by >3% flags that.)
    reference_km, reference = baseline_km, "best seen"
    if new_range_km and baseline_km <= new_range_km * 1.03:
        reference_km, reference = new_range_km, "factory spec"

    degradation = (
        max(0.0, 100.0 * (reference_km - current_km) / reference_km)
        if reference_km else 0.0
    )
    health = min(100.0, 100.0 - degradation)

    socs = [r["soc"] for r, _ in projections]
    return {
        "available": True,
        "n_readings": len(projections),
        "health_pct": round(health, 1),
        "degradation_pct": round(degradation, 1),
        "est_full_range_km": round(current_km, 0),
        "baseline_full_range_km": round(baseline_km, 0),
        "reference_km": round(reference_km, 0),
        "reference": reference,
        "new_range_km": round(new_range_km, 0) if new_range_km else None,
        "min_soc_seen": round(min(socs), 0),
        "avg_soc": round(mean(socs), 0),
    }
