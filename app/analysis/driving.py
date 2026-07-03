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
        "p95_speed_kmh": round(percentile([d.max_speed_kmh for d in drives], 0.95), 1),
        "longest_trip_km": round(max(distances), 1),
        "distance_by_speed_band": {k: round(v, 1) for k, v in sorted(by_speed.items())},
        "trips_by_hour": {str(h): by_hour.get(h, 0) for h in range(24)},
        "trips_by_weekday": {weekdays[i]: by_weekday.get(i, 0) for i in range(7)},
        "top_routes": routes.most_common(5),
        "speed_efficiency_slope_wh_per_kmh": round(speed_slope, 3),
        "avg_efficiency_wh_per_km": round(mean(effs), 1),
        "recent_trips": [
            {
                "start_time": d.start_time.isoformat(timespec="minutes"),
                "distance_km": round(d.distance_km, 1),
                "duration_min": round(d.duration_min),
                "wh_per_km": round(d.wh_per_km),
                "route": f"{d.start_location} → {d.end_location}"
                if d.start_location and d.end_location else "",
            }
            for d in sorted(drives, key=lambda x: x.start_time, reverse=True)[:5]
        ],
    }
