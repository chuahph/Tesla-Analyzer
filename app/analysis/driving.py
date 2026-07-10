"""Driving pattern analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .. import sync as sync_mod
from ..models import Drive
from . import has_valid_energy, haversine_km, linregress, mean, percentile, safe_div


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


def _data_quality(d: Drive) -> str:
    """How trustworthy this trip's efficiency figures are, so the dashboard
    can show which trips are real measurements vs a fallback estimate:
      - "measured": valid energy AND idle live-tracked while the trip was
        open — driving_wh_per_km reflects an actual observed stop, not a
        guess.
      - "estimated": valid energy but idle wasn't live-tracked (a trip
        logged before that existed, or reconstructed across an unpolled
        gap) — driving_wh_per_km falls back to the avg/max-speed heuristic.
      - "incomplete": no valid energy (a range-reading gap contaminated the
        trip) — Wh/km and cost are unavailable for it.
    """
    if not has_valid_energy(d):
        return "incomplete"
    return "measured" if getattr(d, "idle_tracked", False) else "estimated"


def _distance_flag(d: Drive) -> str | None:
    """Flags a trip whose logged odometer distance is implausibly short
    against the straight-line distance between its own stored endpoints — a
    real driven distance can never be shorter than a straight line between
    the same two points, so this catches an odometer/GPS data glitch that
    the energy math alone wouldn't reveal. None when there's nothing to
    compare (older trips with no stored coords) or the numbers are sane.
    """
    start = getattr(d, "start_coords", "") or ""
    end = getattr(d, "end_coords", "") or ""
    straight = haversine_km(start, end)
    if straight is None or straight < 0.3:   # too short to be meaningful either way
        return None
    if d.distance_km < straight * 0.9:
        return "distance_short"
    return None


def _insights(drives: list[Drive]) -> list[str]:
    """Data-driven observations from the raw drives — patterns the aggregate
    KPIs can't show. Only reports a pattern when there are enough drives on
    both sides of a comparison (>= 3) and the difference is material (>= 8%),
    so a single odd trip never masquerades as a trend."""
    out: list[str] = []
    eff = [d for d in drives if d.distance_km > 0 and has_valid_energy(d)]

    def median_whkm(subset: list[Drive]) -> float:
        return percentile([d.wh_per_km for d in subset], 0.5) if subset else 0.0

    def compare(a: list[Drive], b: list[Drive], a_name: str, b_name: str, verb: str):
        if len(a) < 3 or len(b) < 3:
            return
        ma, mb = median_whkm(a), median_whkm(b)
        if not ma or not mb:
            return
        diff = (ma - mb) / mb * 100.0
        if abs(diff) >= 8.0:
            worse, better, pct = (a_name, b_name, diff) if diff > 0 else (b_name, a_name, -diff)
            out.append(
                f"{worse.capitalize()} {verb} average {round(pct)}% more Wh/km "
                f"than {better} ({round(ma if diff > 0 else mb)} vs "
                f"{round(mb if diff > 0 else ma)})."
            )

    peak = [d for d in eff if d.start_time.hour in (7, 8, 17, 18, 19)]
    off = [d for d in eff if d.start_time.hour not in (7, 8, 17, 18, 19)]
    compare(peak, off, "peak-hour drives", "off-peak drives", "use on")

    weekend = [d for d in eff if d.start_time.weekday() >= 5]
    weekday = [d for d in eff if d.start_time.weekday() < 5]
    compare(weekend, weekday, "weekend drives", "weekday drives", "use on")

    hot = [d for d in eff if (d.outside_temp_c or 0) >= 33]
    mild = [d for d in eff if 0 < (d.outside_temp_c or 0) < 33]
    compare(hot, mild, "hot-day drives (33°C+)", "milder-day drives", "use on")

    short = [d for d in eff if d.distance_km < 5]
    longer = [d for d in eff if d.distance_km >= 5]
    compare(short, longer, "short hops (<5 km)", "longer drives", "use on")

    return out[:3]


def analyze(drives: list[Drive], rated_wh_per_km: float = 150.0,
            capacity_kwh: float = 75.0, energy_price: float = 0.0) -> dict[str, Any]:
    """``energy_price`` is either a flat RM/kWh float, or a
    ``datetime -> RM/kWh`` callable (time-of-use pricing — see app.tariff) for
    per-trip rates by when each drive happened."""
    if not drives:
        return {"available": False}
    price_at = energy_price if callable(energy_price) else (lambda _dt: energy_price)

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

    # Most frequent routes. Grouped by the coarser start/end *area* (a
    # district/suburb bucket, stable across GPS jitter between repeat visits
    # to "the same place" — the specific matched POI/building can legitimately
    # differ a few metres apart) rather than the specific location string, so
    # a real repeated route doesn't fragment into many near-duplicate
    # single-count entries. Each group still displays its most common
    # specific label, not the coarse area, so the list stays informative.
    # Rows logged before start_area/end_area existed fall back to the
    # specific location as their own grouping key.
    route_counts: Counter[tuple[str, str]] = Counter()
    route_labels: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for d in drives:
        if not (d.start_location and d.end_location):
            continue
        area_key = (
            getattr(d, "start_area", "") or d.start_location,
            getattr(d, "end_area", "") or d.end_location,
        )
        route_counts[area_key] += 1
        route_labels[area_key][f"{d.start_location} → {d.end_location}"] += 1
    routes = Counter({
        route_labels[key].most_common(1)[0][0]: count
        for key, count in route_counts.items()
    })

    # How strongly speed affects efficiency (Wh/km per km/h).
    speed_slope, _ = linregress([d.avg_speed_kmh for d in eff_drives], effs)

    # Distance-weighted window efficiency (energy-bearing drives only), and its
    # absolute driving score. Zero energy means the range reading was missing (a
    # data gap), not a real 0 Wh/km — leave efficiency and the score as unknown
    # so the UI shows "—" instead of a misleading 0 / grade E.
    window_eff = round(eff_energy * 1000.0 / eff_distance, 1) if eff_distance and eff_energy > 0 else None
    window_score = eco_score(window_eff, rated_wh_per_km) if window_eff else None

    # Blended RM/kWh actually paid across the window's priceable trips (each
    # priced at its own start_time under time-of-use), used for the window
    # cost total. Falls back to the flat rate (price_at applied to "now") when
    # there's no priceable energy yet, so a window with only a data-gap drive
    # doesn't silently show no cost.
    priced = [(d.energy_used_kwh, price_at(d.start_time)) for d in eff_drives]
    priced_energy = sum(e for e, _ in priced)
    window_price = (
        safe_div(sum(e * p for e, p in priced), priced_energy) if priced_energy
        else price_at(drives[-1].start_time)
    )

    # Per-tag totals (distance/energy/cost), keyed by whatever's in Drive.tag
    # ("" groups every untagged trip together) — the expense-claim view: how
    # much of this window's driving/cost was "work" vs "personal" etc.
    by_tag: dict[str, dict[str, float]] = defaultdict(lambda: {"distance_km": 0.0, "energy_kwh": 0.0, "cost": 0.0})
    for d in drives:
        row = by_tag[getattr(d, "tag", "") or ""]
        row["distance_km"] += d.distance_km
        if has_valid_energy(d):
            row["energy_kwh"] += d.energy_used_kwh
            row["cost"] += d.energy_used_kwh * price_at(d.start_time)
    tag_totals = {
        (tag or "untagged"): {
            "distance_km": round(v["distance_km"], 1),
            "energy_kwh": round(v["energy_kwh"], 1),
            "cost": round(v["cost"], 2) if window_price else None,
        }
        for tag, v in by_tag.items()
    }

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
        # What the window's gross battery drain cost. Priced at the blended
        # rate actually paid across the window's trips (their own energy at
        # their own timestamps' rates) rather than a single flat number — so
        # under time-of-use pricing, a window heavy on peak-hour driving costs
        # more per kWh here than one that's mostly off-peak, matching what a
        # driver actually paid. Vampire/idle-between-trips energy (the gap
        # between total_energy_used and the driving-only sum) isn't tied to a
        # specific timestamp, so it's priced at that same blended rate.
        "total_cost": round(total_energy_used * window_price, 2) if window_price else None,
        "cost_per_km": (
            round(total_energy_used * window_price / total_distance, 3)
            if window_price and total_distance else None
        ),
        "insights": _insights(drives),
        # Only surfaced if at least one trip in the window is tagged, so an
        # account nobody ever tags doesn't grow an "untagged: everything" card.
        "by_tag": tag_totals if any(k != "untagged" for k in tag_totals) else None,
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
                # Propulsion-only energy for this drive, the counterpart to
                # driving_wh_per_km (≈ Tesla's "Driving" energy-breakdown line).
                # NB the *gross* energy_kwh is what matches Tesla's "Current
                # Drive" total, which includes climate/idle; this strips that
                # out. Derived from the same driving Wh/km so the two agree;
                # equals the gross energy when no idle was found.
                "driving_energy_kwh": (
                    round(driving_wh_val * d.distance_km / 1000.0, 2)
                    if has_valid_energy(d) and driving_wh_val else
                    (round(d.energy_used_kwh, 2) if has_valid_energy(d) else None)
                ),
                "eco_score": eco_score(driving_wh_val, rated_wh_per_km) if has_valid_energy(d) and driving_wh_val else None,
                # What this trip's energy cost — at its own start time's rate
                # under time-of-use pricing, else the flat tariff.
                "cost": (
                    round(d.energy_used_kwh * price_at(d.start_time), 2)
                    if has_valid_energy(d) and price_at(d.start_time) else None
                ),
                "conditions": _trip_conditions(d),
                # "measured" (real tracked idle) / "estimated" (heuristic
                # fallback) / "incomplete" (no valid energy) — how much to
                # trust this trip's efficiency figures.
                "data_quality": _data_quality(d),
                # Set only when the odometer distance is implausibly short
                # against the trip's own stored endpoints — an odometer/GPS
                # glitch, independent of the energy math.
                "distance_flag": _distance_flag(d),
                # User-assigned category ("work"/"personal"/...); "" = untagged.
                "tag": getattr(d, "tag", "") or "",
                "route": f"{d.start_location} → {d.end_location}"
                if d.start_location and d.end_location else "",
                # Live directions link (Google Maps start -> end) when the raw
                # endpoints were kept; empty for rows logged before coords
                # were stored.
                "map_url": (
                    "https://www.google.com/maps/dir/?api=1"
                    f"&origin={getattr(d, 'start_coords', '').replace(' ', '')}"
                    f"&destination={getattr(d, 'end_coords', '').replace(' ', '')}"
                    if getattr(d, "start_coords", "") and getattr(d, "end_coords", "")
                    else None
                ),
                # % of the battery this trip drew. start_soc/end_soc come from
                # Tesla's integer battery_level, so their delta is whole-number
                # only — useless at 1 decimal. When the trip has valid energy
                # (from the fractional range delta) derive the % from that
                # instead, giving true sub-1% precision; fall back to the
                # integer delta only when energy is unknown (a range gap).
                "soc_used_pct": (
                    round(d.energy_used_kwh / capacity_kwh * 100.0, 1)
                    if has_valid_energy(d) and capacity_kwh
                    else round(max(d.start_soc - d.end_soc, 0.0), 1)
                ),
            }
            for d in sorted(drives, key=lambda x: x.start_time, reverse=True)[:5]
        ],
    }
