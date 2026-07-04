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
# the API's battery_range field uses). Each entry needs the model substring,
# ALL listed tokens (badge, optionally wheel type) in the model+trim text,
# and — when given — a model-year window (from the VIN). First match wins,
# so keep the most specific entries first.
NEW_RANGE_KM: list[tuple[str, tuple[str, ...], tuple[int, int] | None, float]] = [
    # 2024+ Model 3 "Highland"
    ("MODEL 3", ("P74D",), (2024, 2100), 476.0),          # Performance (296 mi)
    ("MODEL 3", ("74D", "NOVA19"), (2024, 2100), 491.0),  # LR AWD, 19" Nova (305 mi)
    ("MODEL 3", ("74D",), (2024, 2100), 549.0),           # LR AWD, 18" Photon (341 mi)
    # Pre-Highland Model 3
    ("MODEL 3", ("P74D",), (2017, 2023), 507.0),          # Performance (315 mi)
    ("MODEL 3", ("74D",), (2017, 2023), 536.0),           # LR AWD (333 mi)
    # Year-agnostic fallbacks (no VIN year available)
    ("MODEL 3", ("P74D",), None, 476.0),
    ("MODEL 3", ("74D", "NOVA19"), None, 491.0),
    ("MODEL 3", ("74D",), None, 549.0),
    ("MODEL 3", ("74",), None, 549.0),
    ("MODEL 3", ("50",), None, 438.0),                    # RWD (272 mi)
    ("MODEL Y", ("P74D",), None, 459.0),                  # Performance (285 mi)
    ("MODEL Y", ("74D",), None, 531.0),                   # LR AWD (330 mi)
    ("MODEL Y", ("50",), None, 418.0),                    # RWD (260 mi)
]


def new_range_for(model: str, trim: str, year: int | None = None) -> float | None:
    """Factory new range for this exact variant, if we recognise the badge.

    ``year`` (decoded from the VIN) picks the right generation — e.g. a 74D
    badge means 536 km on a 2023 Model 3 but 549 km on a 2024 Highland.
    """
    text = f"{model or ''} {trim or ''}".upper()
    tokens = set(re.split(r"[^A-Z0-9]+", text))

    def has(req: str) -> bool:
        # Exact token, or token prefix for wheel names ("Nova19DarkTinted").
        return req in tokens or any(t.startswith(req) for t in tokens if t)

    for m, required, years, km in NEW_RANGE_KM:
        if years is not None and (year is None or not years[0] <= year <= years[1]):
            continue
        if m in text and all(has(r) for r in required):
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
