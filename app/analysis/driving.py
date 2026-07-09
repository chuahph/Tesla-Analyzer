"""Driving pattern analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .. import sync as sync_mod
from ..models import Drive
from . import has_valid_energy, linregress, mean, percentile


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


def eco_score(wh_per_km: float, rated_wh_per_km: float) -> int:
    """0-100 efficiency grade for a Wh/km figure against the car's rated one.

    Calibrated so ~15% below rated scores 100, exactly rated scores 85, and it
    falls ~1 point per 1% over rated — a simple, absolute driving grade that
    works per trip and per window.
    """
    if not rated_wh_per_km or wh_per_km <= 0:
        return 0
    ratio = wh_per_km / rated_wh_per_km
    return max(0, min(100, round(100 - (ratio - 0.85) * 100)))


def score_grade(score: int) -> str:
    """A / B / C / D / E band for a 0-100 score."""
    return "A" if score >= 85 else "B" if score >= 70 else \
        "C" if score >= 55 else "D" if score >= 40 else "E"


def _trip_conditions(d: Drive) -> str:
    """Route/traffic character inferred from the trip's own signals.

    The speed profile tells the story: high peak with a high average is open
    highway; high peak with a low average means congestion; a low average
    with spiky peaks is stop-go traffic. Peak-hour timing and heat are added
    as context tags.
    """
    avg, mx = d.avg_speed_kmh or 0.0, d.max_speed_kmh or 0.0
    if mx >= 90:
        base = "highway + congestion" if avg < 50 else "highway cruise"
    elif avg < 50 and mx > 2.2 * avg > 0:
        base = "stop-go traffic"
    elif avg < 40:
        base = "city driving"
    else:
        base = "steady flow"
    parts = [base]
    if d.start_time.hour in (7, 8, 17, 18, 19):
        parts.append("peak hour")
    if d.outside_temp_c >= 33:
        parts.append(f"hot {round(d.outside_temp_c)}°C")
    return " · ".join(parts)


def analyze(drives: list[Drive], rated_wh_per_km: float = 150.0,
            capacity_kwh: float = 75.0) -> dict[str, Any]:
    if not drives:
        return {"available": False}

    distances = [d.distance_km for d in drives]
    durations = [d.duration_min for d in drives]
    speeds = [d.avg_speed_kmh for d in drives]
    # Efficiency-bearing drives only: a drive whose range reading was missing
    # logs 0 kWh. Including its distance (but no energy) would understate Wh/km
    # and inflate the eco score, so every efficiency/behaviour figure below is
    # computed from these — while distance/duration/counts use every drive.
    eff_drives = [d for d in drives if d.distance_km > 0 and has_valid_energy(d)]
    effs = [d.wh_per_km for d in eff_drives]
    eff_distance = sum(d.distance_km for d in eff_drives)
    eff_energy = sum(d.energy_used_kwh for d in eff_drives)

    total_distance = sum(distances)
    total_duration_h = sum(durations) / 60.0
    total_energy = sum(d.energy_used_kwh for d in drives)
    # Real-world range yardstick: km per 1% of battery used. Three sources, in
    # order of robustness — take the largest so short trips still yield a value:
    #   • net drop from the first drive's start SoC to the last drive's end SoC
    #     (best for a "since charge" window: no charging in between, so the
    #     cumulative battery use shows even when each trip is sub-1%);
    #   • measured energy ÷ pack capacity (fractional, from the range delta);
    #   • the sum of per-trip integer SoC deltas.
    ordered = sorted(drives, key=lambda x: x.start_time)
    soc_net = max(ordered[0].start_soc - ordered[-1].end_soc, 0.0) if ordered else 0.0
    soc_from_int = sum(max(d.start_soc - d.end_soc, 0.0) for d in drives)
    soc_from_energy = (total_energy / capacity_kwh * 100.0) if capacity_kwh else 0.0
    soc_used = max(soc_net, soc_from_int, soc_from_energy)
    km_per_soc = round(total_distance / soc_used, 1) if soc_used >= 0.2 and total_distance else None
    # Gross battery energy drawn over the window — the real drain from the pack,
    # so it *includes* parking, climate-while-stopped and overnight vampire loss,
    # not just the driving energy summed per trip. (Per-trip Wh/km and the Avg
    # Efficiency figure stay driving-only; this is the "kWh used" headline that
    # should reflect everything the battery actually lost.)
    total_energy_used = round(soc_used / 100.0 * capacity_kwh, 1) if capacity_kwh else round(total_energy, 1)

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
    speed_slope, _ = linregress([d.avg_speed_kmh for d in eff_drives], effs)

    # Distance-weighted window efficiency (energy-bearing drives only), and its
    # absolute driving score. Zero energy means the range reading was missing (a
    # data gap), not a real 0 Wh/km — leave efficiency and the score as unknown
    # so the UI shows "—" instead of a misleading 0 / grade E.
    window_eff = round(eff_energy * 1000.0 / eff_distance, 1) if eff_distance and eff_energy > 0 else None
    window_score = eco_score(window_eff, rated_wh_per_km) if window_eff else None

    return {
        "available": True,
        "total_drives": len(drives),
        "total_distance_km": round(total_distance, 1),
        "total_duration_h": round(total_duration_h, 1),
        "total_energy_kwh": round(total_energy, 1),
        # Gross drain including parking/idle/overnight (see above) — the KPI's
        # "kWh used" headline. total_energy_kwh stays the driving-only sum.
        "total_energy_used_kwh": total_energy_used,
        "avg_trip_distance_km": round(mean(distances), 1),
        "avg_trip_duration_min": round(mean(durations), 1),
        "avg_speed_kmh": round(mean(speeds), 1),
        "km_per_soc_pct": km_per_soc,
        "soc_used_pct": round(soc_used, 1),
        "p95_speed_kmh": round(percentile([d.max_speed_kmh for d in drives], 0.95), 1),
        "max_speed_kmh": round(max((d.max_speed_kmh for d in drives), default=0.0), 1),
        "longest_trip_km": round(max(distances), 1),
        "distance_by_speed_band": {k: round(v, 1) for k, v in sorted(by_speed.items())},
        "trips_by_hour": {str(h): by_hour.get(h, 0) for h in range(24)},
        "trips_by_weekday": {weekdays[i]: by_weekday.get(i, 0) for i in range(7)},
        "top_routes": routes.most_common(5),
        "speed_efficiency_slope_wh_per_kmh": round(speed_slope, 3),
        # Distance-weighted (total energy over total km): one noisy short trip
        # can't skew it the way a plain mean of per-trip ratios does.
        "avg_efficiency_wh_per_km": window_eff,
        # Absolute driving score for the whole window (efficiency vs rated).
        "eco_score": window_score,
        "eco_grade": score_grade(window_score) if window_score is not None else None,
        "behaviour": _behaviour(eff_drives, eff_distance, eff_energy, effs),
        "recent_trips": [
            {
                "id": getattr(d, "id", None),
                "start_time": d.start_time.isoformat(timespec="minutes"),
                "end_time": d.end_time.isoformat(timespec="minutes"),
                "distance_km": round(d.distance_km, 1),
                "duration_min": round(d.duration_min),
                "avg_speed_kmh": round(d.avg_speed_kmh),
                "max_speed_kmh": round(d.max_speed_kmh),
                "wh_per_km": round(d.wh_per_km) if has_valid_energy(d) else None,
                "energy_kwh": round(d.energy_used_kwh, 2) if has_valid_energy(d) else None,
                "driving_wh_per_km": (
                    # Prefer real tracked idle time (from live 1-min-cron
                    # sampling while the trip was open) over the avg/max-speed
                    # estimate, when it's available — a genuine measurement,
                    # not a guess. Gated on idle_tracked, not just idle_min
                    # being truthy: a trip with confirmed zero sustained stops
                    # and a trip nobody ever measured both read idle_min=0.0,
                    # and only idle_tracked tells them apart. Trust a real
                    # zero; only fall back to the estimate when it's unknown.
                    driving_wh_val := (
                        sync_mod._subtract_idle_energy(
                            d.energy_used_kwh, d.distance_km, getattr(d, "idle_min", 0.0) or 0.0,
                            d.outside_temp_c)
                        if getattr(d, "idle_tracked", False) else
                        sync_mod.driving_wh_per_km(
                            d.energy_used_kwh, d.distance_km, d.duration_min, d.outside_temp_c,
                            d.avg_speed_kmh, d.max_speed_kmh)
                    )
                    if has_valid_energy(d) else None
                ),
                "eco_score": eco_score(driving_wh_val, rated_wh_per_km) if has_valid_energy(d) and driving_wh_val else None,
                "conditions": _trip_conditions(d),
                "route": f"{d.start_location} → {d.end_location}"
                if d.start_location and d.end_location else "",
                # % of the battery this trip drew (start_soc -> end_soc), the
                # per-trip counterpart to the window-level soc_used_pct below.
                "soc_used_pct": round(max(d.start_soc - d.end_soc, 0.0), 1),
            }
            for d in sorted(drives, key=lambda x: x.start_time, reverse=True)[:5]
        ],
    }
