"""Analytics engine: driving, charging, efficiency and recommendations."""
from __future__ import annotations

from collections.abc import Sequence

# A real drive can't average below this over its whole distance; a lower figure
# means the range reading was refilled mid-trip (charge / BMS recalibration), so
# the drive's energy is treated as unknown wherever efficiency is computed.
MIN_PLAUSIBLE_WH_PER_KM = 40.0


def has_valid_energy(drive) -> bool:
    """True when a drive's energy is real enough to feed efficiency figures."""
    return drive.energy_used_kwh > 0 and drive.wh_per_km >= MIN_PLAUSIBLE_WH_PER_KM


def mean(values: Sequence[float]) -> float:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else 0.0


def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def linregress(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float]:
    """Ordinary least squares. Returns (slope, intercept).

    Implemented without numpy to keep dependencies light.
    """
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx = mean(xs)
    my = mean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    intercept = my - slope * mx
    return slope, intercept


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
