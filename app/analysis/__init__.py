"""Analytics engine: driving, charging, efficiency and recommendations."""
from __future__ import annotations

from collections.abc import Sequence


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
