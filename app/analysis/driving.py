"""Driving pattern analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..models import Drive
from . import linregress, mean, percentile


def _speed_bucket(speed: float) -> str:
    if speed < 30:
        return "City (<30)"
    if speed < 60:
        return "Urban (30-60)"
    if speed < 90:
        return "Rural (60-90)"
    return "Highway (90+)"


def _behaviour(drives: list[Drive], total_distance: float, total_energy: float,
               effs: list[float]) -> dict[str, Any]:
    """Study the driver's own patterns and measure what each habit costs.

    Every factor is measured from this driver's data (penalty = mean Wh/km of
    the habit's drives minus the rest), so the advice is personal, not generic.
    """
    w = [d for d in drives if d.distance_km > 0]
    if len(w) < 5 or not total_distance:
        return {"available": False, "n_drives": len(w)}

    def eff(sub):
        return mean([d.wh_per_km for d in sub])

    def km_share(sub):
        return 100.0 * sum(d.distance_km for d in sub) / total_distance

    def factor(sub, rest):
        """(share of km, measured Wh/km penalty, kWh it cost in this window)."""
        if not sub or not rest:
            return 0.0, 0.0, 0.0
        pen = eff(sub) - eff(rest)
        kwh = sum(d.distance_km for d in sub) * max(pen, 0.0) / 1000.0
        return round(km_share(sub), 1), round(pen, 1), round(kwh, 2)

    fast = [d for d in w if d.max_speed_kmh > 110]
    stopgo = [d for d in w if d.avg_speed_kmh < 50
              and d.max_speed_kmh > 2.2 * d.avg_speed_kmh]
    short = [d for d in w if d.distance_km < 3]
    peak = [d for d in w if d.start_time.hour in (7, 8, 17, 18, 19)]
    hot = [d for d in w if d.outside_temp_c >= 33]

    speeding = factor(fast, [d for d in w if d not in fast])
    sg = factor(stopgo, [d for d in w if d not in stopgo])
    st = factor(short, [d for d in w if d not in short])
    pk = factor(peak, [d for d in w if d not in peak])
    ht = factor(hot, [d for d in w if d not in hot])

    # Personal-best benchmark: the driver's own most efficient quartile.
    best_q = percentile(effs, 0.25)
    overall = mean(effs)
    potential_kwh = max(0.0, total_energy - best_q * total_distance / 1000.0)
    score = round(min(100.0, 100.0 * best_q / overall)) if overall else 0

    return {
        "available": True,
        "n_drives": len(w),
        "score": score,  # 100 = typical driving matches your personal best
        "best_quartile_wh_per_km": round(best_q, 1),
        "potential_saving_kwh": round(potential_kwh, 1),
        "speeding_share_pct": speeding[0], "speeding_penalty_wh": speeding[1],
        "speeding_saving_kwh": speeding[2],
        "stopgo_share_pct": sg[0], "stopgo_penalty_wh": sg[1],
        "stopgo_saving_kwh": sg[2],
        "short_trip_share_pct": st[0], "short_trip_penalty_wh": st[1],
        "short_trip_saving_kwh": st[2],
        "peak_hour_share_pct": pk[0], "peak_hour_penalty_wh": pk[1],
        "peak_hour_saving_kwh": pk[2],
        "hot_weather_share_pct": ht[0], "hot_weather_penalty_wh": ht[1],
        "hot_weather_saving_kwh": ht[2],
    }


def analyze(drives: list[Drive]) -> dict[str, Any]:
    if not drives:
        return {"available": False}

    distances = [d.distance_km for d in drives]
    durations = [d.duration_min for d in drives]
    speeds = [d.avg_speed_kmh for d in drives]
    effs = [d.wh_per_km for d in drives if d.distance_km > 0]

    total_distance = sum(distances)
    total_duration_h = sum(durations) / 60.0
    total_energy = sum(d.energy_used_kwh for d in drives)
    # Real-world range yardstick: km covered per 1% of battery used.
    soc_used = sum(max(d.start_soc - d.end_soc, 0.0) for d in drives)
    km_per_soc = round(total_distance / soc_used, 1) if soc_used >= 1 else None

    # Distribution of distance driven across speed regimes.
    by_speed: dict[str, float] = defaultdict(float)
    for d in drives:
        by_speed[_speed_bucket(d.avg_speed_kmh)] += d.distance_km

    # Trips per hour-of-day and per weekday for usage patterns.
    by_hour = Counter(d.start_time.hour for d in drives)
    by_weekday = Counter(d.start_time.weekday() for d in drives)
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Most frequent routes.
    routes = Counter(
        f"{d.start_location} → {d.end_location}"
        for d in drives
        if d.start_location and d.end_location
    )

    # How strongly speed affects efficiency (Wh/km per km/h).
    speed_slope, _ = linregress(
        [d.avg_speed_kmh for d in drives if d.distance_km > 0], effs
    )

    return {
        "available": True,
        "total_drives": len(drives),
        "total_distance_km": round(total_distance, 1),
        "total_duration_h": round(total_duration_h, 1),
        "total_energy_kwh": round(total_energy, 1),
        "avg_trip_distance_km": round(mean(distances), 1),
        "avg_trip_duration_min": round(mean(durations), 1),
        "avg_speed_kmh": round(mean(speeds), 1),
        "km_per_soc_pct": km_per_soc,
        "soc_used_pct": round(soc_used, 1),
        "p95_speed_kmh": round(percentile([d.max_speed_kmh for d in drives], 0.95), 1),
        "longest_trip_km": round(max(distances), 1),
        "distance_by_speed_band": {k: round(v, 1) for k, v in sorted(by_speed.items())},
        "trips_by_hour": {str(h): by_hour.get(h, 0) for h in range(24)},
        "trips_by_weekday": {weekdays[i]: by_weekday.get(i, 0) for i in range(7)},
        "top_routes": routes.most_common(5),
        "speed_efficiency_slope_wh_per_kmh": round(speed_slope, 3),
        "avg_efficiency_wh_per_km": round(mean(effs), 1),
        "behaviour": _behaviour(drives, total_distance, total_energy, effs),
        "recent_trips": [
            {
                "start_time": d.start_time.isoformat(timespec="minutes"),
                "end_time": d.end_time.isoformat(timespec="minutes"),
                "distance_km": round(d.distance_km, 1),
                "duration_min": round(d.duration_min),
                "avg_speed_kmh": round(d.avg_speed_kmh),
                "wh_per_km": round(d.wh_per_km),
                "route": f"{d.start_location} → {d.end_location}"
                if d.start_location and d.end_location else "",
            }
            for d in sorted(drives, key=lambda x: x.start_time, reverse=True)[:5]
        ],
    }
