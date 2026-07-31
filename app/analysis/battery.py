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

from . import linregress, mean, percentile

MIN_READINGS = 5      # below this the estimate is too noisy to show
MIN_SOC = 20.0        # low-SoC readings project unreliably
RELIABLE_SOC = 40.0   # rated range is most linear in the mid/high SoC band
RECENT_N = 12         # recent projections summarised as the "current" estimate
IMPLAUSIBLE_DEGRADATION_PCT = 20.0  # beyond this, a modern Tesla pack under
# normal use almost certainly hasn't degraded that much -- more likely the
# spec figure itself is wrong than the pack being unrealistically degraded.
SPEC_OVERRIDE_MIN_READINGS = 30     # readings needed before real data is
# trusted enough to override a documented spec figure downward.

# Degradation forecast: only projected once there are enough monthly trend
# points spanning enough time, and only when a real decline is measurable
# above the month-to-month noise. A lone quarter of near-flat readings can't
# be linearly extrapolated to "years until 80%" without inventing precision.
FORECAST_MIN_MONTHS = 4          # trend points needed before forecasting
FORECAST_MIN_SPAN_YEARS = 0.25   # and they must span at least this long
FORECAST_NOISE_KM_PER_YEAR = 1.0 # slower loss than this reads as "no decline yet"
# Capacity-retention milestones to project toward (% of the when-new
# reference). 80% is the common "health" milestone; 70% is the floor Tesla's
# battery warranty guarantees over 8 years / 192,000 km on Model 3/Y.
FORECAST_HEALTH_MILESTONE = 0.80
FORECAST_WARRANTY_FLOOR = 0.70

# Factory rated range at 100% when new, in km (EPA figures — the same scale
# the API's battery_range field uses). Each entry needs the model substring,
# ALL listed tokens (badge, optionally wheel type) in the model+trim text,
# and — when given — a model-year window (from the VIN). First match wins,
# so keep the most specific entries first.
# "@19" means: any wheel-name token carrying that diameter (Nova19,
# Nova19DarkTinted, Stiletto19, ...), so unexpected wheel names still match.
NEW_RANGE_KM: list[tuple[str, tuple[str, ...], tuple[int, int] | None, float]] = [
    # 2024+ Model 3 "Highland"
    ("MODEL 3", ("P74D",), (2024, 2100), 476.0),        # Performance (296 mi)
    ("MODEL 3", ("74D", "@19"), (2024, 2100), 491.0),   # LR AWD, 19" (305 mi)
    ("MODEL 3", ("74D",), (2024, 2100), 549.0),         # LR AWD, 18" (341 mi)
    # Pre-Highland Model 3
    ("MODEL 3", ("P74D",), (2017, 2023), 507.0),        # Performance (315 mi)
    ("MODEL 3", ("74D",), (2017, 2023), 536.0),         # LR AWD (333 mi)
    # Year-agnostic fallbacks (no VIN year available)
    ("MODEL 3", ("P74D",), None, 476.0),
    ("MODEL 3", ("74D", "@19"), None, 491.0),
    ("MODEL 3", ("74D",), None, 549.0),
    ("MODEL 3", ("74",), None, 549.0),
    ("MODEL 3", ("50",), None, 438.0),                  # RWD (272 mi)
    ("MODEL Y", ("P74D",), None, 459.0),                # Performance (285 mi)
    ("MODEL Y", ("74D",), None, 531.0),                 # LR AWD (330 mi)
    ("MODEL Y", ("50",), None, 418.0),                  # RWD (260 mi)
]


# Nominal usable pack capacity (kWh) — the 100%->0% energy of a healthy pack —
# by variant. Wheel size doesn't change the pack, so no @-tokens here. Used to
# seed/sanity-check the measured capacity; the charge EMA and an explicit
# override refine it. Figures are the widely-cited usable (not gross) values.
USABLE_KWH: list[tuple[str, tuple[str, ...], tuple[int, int] | None, float]] = [
    ("MODEL 3", ("P74D",), None, 75.0),   # Performance / LR-pack (82 kWh gross)
    ("MODEL 3", ("74D",), None, 75.0),    # Long Range AWD (82 kWh gross)
    ("MODEL 3", ("74",), None, 75.0),
    ("MODEL 3", ("50",), None, 57.5),     # RWD (standard-range pack)
    ("MODEL Y", ("P74D",), None, 75.0),
    ("MODEL Y", ("74D",), None, 75.0),    # Long Range AWD (82 kWh gross)
    ("MODEL Y", ("50",), None, 60.0),     # RWD
]


def _wheel_diameter(tokens: set[str]) -> int | None:
    """Wheel diameter from a wheel-name token like NOVA19 / PHOTON18DARK."""
    for t in tokens:
        m = re.match(r"^[A-Z]+(1[89]|2[012])", t)
        if m:
            return int(m.group(1))
    return None


def _variant_lookup(table, model: str, trim: str, year: int | None):
    """First matching value in a variant table (model substring + all required
    badge/wheel tokens + optional VIN-year window). Shared by the range and
    capacity lookups so they identify a car identically."""
    text = f"{model or ''} {trim or ''}".upper()
    tokens = set(re.split(r"[^A-Z0-9]+", text))

    def has(req: str) -> bool:
        if req.startswith("@"):
            return _wheel_diameter(tokens) == int(req[1:])
        # Exact token, or token prefix for wheel names ("Nova19DarkTinted").
        return req in tokens or any(t.startswith(req) for t in tokens if t)

    for m, required, years, value in table:
        if years is not None and (year is None or not years[0] <= year <= years[1]):
            continue
        if m in text and all(has(r) for r in required):
            return value
    return None


# Approximate fleet-average degradation vs. odometer (km), from widely-cited
# aggregate studies of real-world Tesla packs (e.g. Tesloop's high-mileage
# fleet, Recurrent Auto's aggregated telemetry across tens of thousands of
# vehicles). This is deliberately rough — a "faster/slower than a typical
# pack" yardstick, not per-VIN precision — and normalised on distance
# travelled rather than calendar age, since usage (fast charging habits,
# climate, annual mileage) varies far more than age does across the fleet.
# Piecewise-linear; each point is (odometer_km, typical degradation_pct).
FLEET_DEGRADATION_KM_PCT: list[tuple[float, float]] = [
    (0.0, 0.0),
    (25_000.0, 2.0),
    (50_000.0, 3.5),
    (100_000.0, 5.5),
    (160_000.0, 7.0),
    (250_000.0, 9.0),
    (400_000.0, 11.0),
]


def fleet_degradation_pct(odo_km: float) -> float:
    """Typical fleet degradation (%) at this odometer, interpolated from
    FLEET_DEGRADATION_KM_PCT. Clamped at 0 below the first point; extrapolated
    past the last point using its final segment's slope (packs keep degrading,
    just more slowly, well past 400,000 km — flatlining there would understate
    it for the highest-mileage cars)."""
    pts = FLEET_DEGRADATION_KM_PCT
    if odo_km <= pts[0][0]:
        return pts[0][1]
    if odo_km >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        slope = (y1 - y0) / (x1 - x0)
        return y1 + slope * (odo_km - x1)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= odo_km <= x1:
            frac = (odo_km - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return pts[-1][1]  # unreachable given the bounds above


def _month_to_year(key: str) -> float:
    """'YYYY-MM' -> a fractional year (2024-07 -> 2024.5), so a monthly trend
    can be regressed on a real time axis rather than an even index (which would
    distort the slope when a month is missing)."""
    try:
        y, m = key.split("-")
        return int(y) + (int(m) - 1) / 12.0
    except (ValueError, AttributeError):
        return 0.0


def _forecast(trend: list[dict[str, Any]], reference_km: float,
              current_km: float) -> dict[str, Any]:
    """Project the monthly full-range trend forward to the health milestone
    (80%) and warranty floor (70%). Deliberately conservative: returns
    available=False (with a plain-language note) until there's enough of a
    real, measurable decline to extrapolate honestly."""
    if len(trend) < FORECAST_MIN_MONTHS:
        return {"available": False,
                "note": "Not enough history yet to project a trend — a few "
                        "months of readings are needed first."}
    xs = [_month_to_year(t["month"]) for t in trend]
    ys = [t["full_range_km"] for t in trend]
    span = max(xs) - min(xs)
    if span < FORECAST_MIN_SPAN_YEARS:
        return {"available": False,
                "note": "Readings don't span enough time yet to project a trend."}
    slope, _ = linregress(xs, ys)  # km per year (negative = losing range)
    loss_per_year = -slope
    if loss_per_year < FORECAST_NOISE_KM_PER_YEAR:
        return {"available": False,
                "slope_km_per_year": round(slope, 1),
                "note": "No measurable decline yet — range is holding steady "
                        "within the month-to-month noise, which is good news."}

    def _years_to(fraction: float) -> float | None:
        target = reference_km * fraction
        if current_km <= target:
            return 0.0  # already at/below it
        return round((current_km - target) / loss_per_year, 1)

    return {
        "available": True,
        "slope_km_per_year": round(slope, 1),
        "loss_pct_per_year": (round(loss_per_year / reference_km * 100.0, 2)
                              if reference_km else None),
        "years_to_health_milestone": _years_to(FORECAST_HEALTH_MILESTONE),
        "years_to_warranty_floor": _years_to(FORECAST_WARRANTY_FLOOR),
        "health_milestone_pct": int(FORECAST_HEALTH_MILESTONE * 100),
        "warranty_floor_pct": int(FORECAST_WARRANTY_FLOOR * 100),
        "note": "Straight-line projection of the current trend — real "
                "degradation slows as the pack ages, so treat these as a "
                "worst-case 'no faster than now' horizon, not a promise.",
    }


def new_range_for(model: str, trim: str, year: int | None = None) -> float | None:
    """Factory new range for this exact variant, if we recognise the badge.

    ``year`` (decoded from the VIN) picks the right generation — e.g. a 74D
    badge means 536 km on a 2023 Model 3 but 549 km on a 2024 Highland.
    """
    return _variant_lookup(NEW_RANGE_KM, model, trim, year)


# Charge-derived capacity calibration. A charge must add at least this much
# SoC before it says anything useful: start/end SoC are whole percents, so a
# small gain carries a ±1-point rounding error that swamps the answer (a 5%
# gain is ±20% on the implied capacity; a 40% gain is ±2.5%).
CAPACITY_MIN_GAIN_PCT = 15.0
# Charging is lossy on the AC side — the wall meter counts energy the onboard
# charger never gets into the pack — so an AC session overstates capacity
# without this. DC feeds the pack directly and needs no correction.
CAPACITY_AC_EFFICIENCY = 0.95
# Outside this a sample is a data error (a mis-parsed session, an SoC reset
# mid-charge), not a real pack, and would drag the median around.
CAPACITY_PLAUSIBLE_KWH = (45.0, 95.0)


def implied_capacity(charges: list[Any]) -> dict[str, Any]:
    """Usable pack capacity implied by real charge sessions, for checking the
    figure that turns every SoC delta into kWh.

    ``energy_added = (SoC gain / 100) x usable_capacity``, so each charge
    inverts to a capacity estimate. This is the only *non-circular* source
    available: a drive's ``energy_used_kwh`` is itself computed by multiplying
    a range/SoC delta BY the capacity constant, so calibrating from trips
    would just hand the same number back. Charge energy comes from Tesla's own
    charge meter and owes nothing to any assumption of ours.

    The median is the headline rather than the mean — one mis-recorded session
    (a charge interrupted and resumed, an SoC jump while plugged in) is a wild
    outlier, and the median ignores it where a mean would be dragged.

    Reports rather than decides: nothing here changes the capacity in use.
    A disagreement can mean the spec figure is wrong for this variant, the
    range-derived degradation estimate has drifted, or simply that too few
    sessions have been logged to say. That judgement is the owner's.
    """
    samples = []
    for c in charges:
        gain = (getattr(c, "end_soc", 0) or 0) - (getattr(c, "start_soc", 0) or 0)
        energy = getattr(c, "energy_added_kwh", 0) or 0.0
        if gain < CAPACITY_MIN_GAIN_PCT or energy <= 0:
            continue
        cap = energy / (gain / 100.0)
        if (getattr(c, "charge_type", "AC") or "AC") != "DC":
            cap *= CAPACITY_AC_EFFICIENCY
        lo, hi = CAPACITY_PLAUSIBLE_KWH
        if not (lo <= cap <= hi):
            continue
        samples.append({
            "charge_id": getattr(c, "id", None),
            "date": getattr(c, "start_time", None).isoformat(timespec="minutes")
            if getattr(c, "start_time", None) else None,
            "charge_type": getattr(c, "charge_type", "AC"),
            "soc_gain_pct": round(gain, 1),
            "energy_added_kwh": round(energy, 2),
            "implied_kwh": round(cap, 1),
        })
    if not samples:
        return {"available": False, "samples": [], "count": 0}
    vals = sorted(s["implied_kwh"] for s in samples)
    return {
        "available": True,
        "count": len(vals),
        "median_kwh": round(percentile(vals, 0.5), 1),
        "min_kwh": vals[0],
        "max_kwh": vals[-1],
        # Spread matters as much as the middle: tightly-clustered samples make
        # a disagreement with the figure in use worth acting on, while a wide
        # scatter says the evidence isn't there yet whatever the median says.
        "spread_kwh": round(vals[-1] - vals[0], 1),
        "samples": samples[-20:],
    }


def usable_capacity_for(model: str, trim: str, year: int | None = None) -> float | None:
    """Nominal usable pack capacity (kWh) for the variant, if recognised."""
    return _variant_lookup(USABLE_KWH, model, trim, year)


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

    # Current estimate: the median of the most recent projections, taken from
    # the reliable mid/high-SoC band (rated range is least linear near a full or
    # near-empty pack). Fall back to all projections if too few in that band.
    reliable = [(r, p) for r, p in projections if r["soc"] >= RELIABLE_SOC]
    pool = reliable if len(reliable) >= MIN_READINGS else projections
    recent = pool[-RECENT_N:]
    current_km = percentile([p for _, p in recent], 0.5)  # median resists outliers

    # 100% reference: the factory when-new figure (EPA-published, looked up
    # by exact variant) if it plausibly matches this car's own history;
    # otherwise the car's own actual best-observed range — a continuously
    # updating real measurement, not a number pinned indefinitely just
    # because it was asserted once. Two-sided guard so a wrong spec figure
    # can't dominate forever in either direction:
    #  - real data reading well ABOVE spec (baseline > spec + 3%) means a
    #    scale/region mismatch (different range convention) — spec is
    #    meaningless here, so it's discarded outright;
    #  - real data reading implausibly far BELOW spec (> 20% implied
    #    degradation), once there's enough history to trust it (>= 30
    #    readings), more likely means the spec itself overstates the true
    #    when-new figure than the pack being unrealistically degraded — also
    #    discarded, so real accumulated data can correct a wrong spec.
    # Short of those, spec anchors the estimate: most tracking starts after
    # a car has already lost some range, and our own data alone can't tell
    # that apart from a healthy pack — only the factory figure can.
    reference_km, reference = baseline_km, "best seen"
    if new_range_km:
        scale_mismatch = baseline_km > new_range_km * 1.03
        implied_degradation = 100.0 * (new_range_km - baseline_km) / new_range_km
        implausible = (
            implied_degradation > IMPLAUSIBLE_DEGRADATION_PCT
            and len(projections) >= SPEC_OVERRIDE_MIN_READINGS
        )
        if not scale_mismatch and not implausible:
            reference_km, reference = new_range_km, "factory spec"

    degradation = (
        max(0.0, 100.0 * (reference_km - current_km) / reference_km)
        if reference_km else 0.0
    )
    health = min(100.0, 100.0 - degradation)

    # Health trend: median projected full range per month, so degradation
    # reads as a curve over time instead of a single number. Only months with
    # a few readings (>= 3) qualify — a lone reading is too noisy to plot.
    by_month: dict[str, list[float]] = {}
    for r, p in projections:
        ts = r.get("ts")
        if ts is None:
            continue
        key = ts.strftime("%Y-%m") if hasattr(ts, "strftime") else str(ts)[:7]
        by_month.setdefault(key, []).append(p)
    trend = [
        {"month": m, "full_range_km": round(percentile(vals, 0.5), 0)}
        for m, vals in sorted(by_month.items())
        if len(vals) >= 3
    ]

    forecast = _forecast(trend, reference_km, current_km)

    socs = [r["soc"] for r, _ in projections]
    recent_socs = [r["soc"] for r, _ in recent]

    # Fleet benchmark: how this car's own degradation compares to a typical
    # pack at the same odometer. Needs at least one reading with an odometer
    # value (rows logged before that column existed have none) — falls back
    # to unavailable rather than comparing against odo_km=0, which would
    # always read as "much worse than a brand-new car".
    odo_values = [r.get("odo_km") for r, _ in projections if r.get("odo_km")]
    current_odo_km = max(odo_values) if odo_values else None
    fleet_pct = fleet_degradation_pct(current_odo_km) if current_odo_km else None
    vs_fleet_pct = round(degradation - fleet_pct, 1) if fleet_pct is not None else None

    return {
        "available": True,
        "n_readings": len(projections),
        "trend": trend,
        "forecast": forecast,
        "health_pct": round(health, 1),
        "degradation_pct": round(degradation, 1),
        "est_full_range_km": round(current_km, 0),
        "baseline_full_range_km": round(baseline_km, 0),
        "reference_km": round(reference_km, 0),
        # For the "show the computation" panel:
        "est_from_n": len(recent),
        "est_soc_band": (f"{round(min(recent_socs))}–{round(max(recent_socs))}%"
                         if recent_socs else ""),
        "reliable_band": len(reliable) >= MIN_READINGS,
        "reference": reference,
        "new_range_km": round(new_range_km, 0) if new_range_km else None,
        "min_soc_seen": round(min(socs), 0),
        "avg_soc": round(mean(socs), 0),
        # Fleet comparison (see fleet_degradation_pct): None when there's no
        # odometer reading to anchor it to.
        "current_odo_km": round(current_odo_km, 0) if current_odo_km else None,
        "fleet_degradation_pct": round(fleet_pct, 1) if fleet_pct is not None else None,
        "vs_fleet_pct": vs_fleet_pct,
    }
