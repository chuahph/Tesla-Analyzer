"""Turn the raw analysis into concrete, prioritised recommendations.

Each recommendation is a dict with:
  category, priority (high|medium|low), title, detail,
  estimated_saving (free text, optional).
"""
from __future__ import annotations

from typing import Any


def _rec(category, priority, title, detail, saving=None):
    return {
        "category": category,
        "priority": priority,
        "title": title,
        "detail": detail,
        "estimated_saving": saving,
    }


def build(
    driving: dict[str, Any],
    charging: dict[str, Any],
    efficiency: dict[str, Any],
    battery: dict[str, Any] | None = None,
    *,
    energy_price: float,
    currency: str,
    tou: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """``tou``, when a real time-of-use tariff is configured: {peak_price,
    offpeak_price, peak_start_hour, peak_end_hour} — sizes the smart
    charging advisor's saving from the account's own peak-hour energy
    instead of a generic heuristic. None (the default) keeps the old
    session-count-based hint for accounts on a flat rate."""
    recs: list[dict[str, Any]] = []

    # --- Battery degradation --------------------------------------------------
    if battery and battery.get("available"):
        deg = battery["degradation_pct"]
        if deg >= 8:
            recs.append(
                _rec(
                    "Battery health",
                    "high",
                    f"Estimated battery degradation is {deg:.0f}%",
                    "The pack's projected full range has dropped noticeably from its "
                    "best observed value. Some loss is normal with age and mileage, "
                    "but you can slow it down: avoid sitting at very high or very low "
                    "charge for long periods, prefer AC charging, and minimise DC "
                    "fast-charging in hot conditions.",
                    None,
                )
            )
        elif deg >= 4:
            recs.append(
                _rec(
                    "Battery health",
                    "low",
                    f"Mild battery degradation (~{deg:.0f}%)",
                    "Projected full range is slightly below the best this pack has "
                    "shown — well within normal ageing. Current charging habits are "
                    "worth keeping an eye on but no action is needed.",
                    None,
                )
            )

    # --- Personal driving behaviour (measured from this driver's own data) ---
    beh = (driving or {}).get("behaviour") or {}
    if beh.get("available"):
        def _cost(kwh: float) -> str:
            return f"~{kwh:.1f} kWh / {currency} {kwh * energy_price:.2f} in this window"

        factors = [
            ("speeding", "medium", "Fast highway driving is costing you range",
             "In {share}% of your kilometres you exceeded 110 km/h, and those "
             "drives averaged +{pen} Wh/km versus your calmer ones. Easing the "
             "cruise speed by ~10 km/h recovers most of it."),
            ("stopgo", "medium", "Stop-and-go driving pattern detected",
             "{share}% of your kilometres show a stop-go signature (low average "
             "but high peak speed), costing +{pen} Wh/km. Smoother acceleration "
             "and letting regen do the braking (one-pedal style) narrows this."),
            ("short_trip", "low", "Short cold-start trips are inefficient",
             "Trips under 3 km make up {share}% of your kilometres at +{pen} "
             "Wh/km — the battery and cabin never reach efficient temperature. "
             "Chaining errands into one round trip helps."),
            ("peak_hour", "low", "Peak-hour congestion is measurable in your data",
             "Driving at 7–8 or 17–19 h costs you +{pen} Wh/km over {share}% of "
             "your kilometres. Shifting departures even 30 minutes can help."),
            ("hot_weather", "low", "Hot-weather driving penalty",
             "Drives at 33°C+ cost +{pen} Wh/km ({share}% of km) — mostly A/C "
             "load. Pre-cool the cabin while still plugged in and park in shade "
             "where possible."),
        ]
        for key, pri, title, detail in factors:
            share = beh.get(f"{key}_share_pct", 0)
            pen = beh.get(f"{key}_penalty_wh", 0)
            kwh = beh.get(f"{key}_saving_kwh", 0)
            if share >= 10 and pen >= 8 and kwh >= 0.5:
                recs.append(_rec(
                    "Driving behaviour", pri, title,
                    detail.format(share=share, pen=pen), _cost(kwh),
                ))

        if beh.get("potential_saving_kwh", 0) >= 1 and beh.get("score", 100) < 90:
            recs.append(_rec(
                "Driving behaviour", "low",
                f"Driving like your own best quartile would save "
                f"{beh['potential_saving_kwh']:.1f} kWh",
                f"Your most efficient quartile of drives averages "
                f"{beh['best_quartile_wh_per_km']:.0f} Wh/km — a benchmark you "
                "already achieve regularly. Matching it across all driving is the "
                "single biggest efficiency lever in your data.",
                f"{currency} {beh['potential_saving_kwh'] * energy_price:.2f} in this window",
            ))

    # --- Efficiency vs rated -------------------------------------------------
    if efficiency.get("available"):
        vs_rated = efficiency["vs_rated_pct"]
        if vs_rated > 12:
            extra_kwh = efficiency["total_energy_kwh"] * (vs_rated / (100 + vs_rated))
            cost = extra_kwh * energy_price
            recs.append(
                _rec(
                    "Efficiency",
                    "high",
                    f"Driving {vs_rated:.0f}% above rated consumption",
                    "Your average Wh/km is well above the EPA/rated figure. The gap is "
                    "usually a mix of high cruising speed, hard acceleration, climate use "
                    "and cold weather. Smoother acceleration and using scheduled "
                    "pre-conditioning while plugged in recovers most of this.",
                    f"~{extra_kwh:.0f} kWh / {currency} {cost:.0f} over the analysed period",
                )
            )

        # Speed sensitivity.
        slope = driving.get("speed_efficiency_slope_wh_per_kmh", 0)
        if slope > 0.6:
            recs.append(
                _rec(
                    "Driving",
                    "medium",
                    "High speed is costing significant range",
                    f"Each extra 1 km/h of average speed adds ~{slope:.2f} Wh/km. "
                    "Reducing motorway cruising speed by 10 km/h would noticeably cut "
                    "consumption on long trips, where aerodynamic drag dominates.",
                    f"~{slope * 10:.0f} Wh/km on highway legs",
                )
            )

        # Cold-weather sensitivity.
        tslope = efficiency.get("temp_efficiency_slope_wh_per_c", 0)
        if tslope < -1.0:
            recs.append(
                _rec(
                    "Efficiency",
                    "medium",
                    "Cold weather is hurting efficiency",
                    "Consumption climbs sharply as temperature drops. Pre-condition the "
                    "cabin and battery while still plugged in (so the energy comes from "
                    "the wall, not the pack), and use seat heaters instead of cabin heat "
                    "where possible.",
                    f"~{abs(tslope):.1f} Wh/km per °C colder",
                )
            )

    # --- Charging habits -----------------------------------------------------
    if charging.get("available"):
        full_share = charging["full_charge_share_pct"]
        if full_share > 15:
            recs.append(
                _rec(
                    "Battery health",
                    "high",
                    f"{full_share:.0f}% of charges go to 100%",
                    "Frequent charging to 100% accelerates calendar/cycle degradation on "
                    "the NCA/NMC pack. Unless you need the full range for a trip, set the "
                    "daily charge limit to 80–90% and only top up to 100% just before "
                    "departure.",
                    "Slower long-term battery degradation",
                )
            )

        dc_share = charging["dc_energy_share_pct"]
        if dc_share > 25:
            recs.append(
                _rec(
                    "Battery health",
                    "medium",
                    f"{dc_share:.0f}% of energy comes from DC fast charging",
                    "Heavy reliance on Superchargers/DC adds heat and stress to the pack "
                    "and is more expensive per kWh than home AC. Shifting routine charging "
                    "to overnight AC at home extends battery life and lowers cost.",
                    f"Up to {currency} "
                    f"{(charging['avg_cost_per_kwh'] - energy_price) * charging['dc_energy_kwh']:.0f}"
                    " saved by moving DC energy to home AC",
                )
            )

        # Smart charging advisor (advisory only — suggests a schedule, never
        # sets one): with a real time-of-use tariff configured, size the
        # actual currency saved by shifting the window's peak-hour energy to
        # off-peak, from the account's own energy-by-hour history. Falls
        # back to a generic session-count heuristic without TOU pricing,
        # since there's no real per-kWh rate delta to size a figure from.
        peak_start = tou.get("peak_start_hour") if tou else None
        peak_end = tou.get("peak_end_hour") if tou else None
        if tou and tou.get("peak_price") and tou.get("offpeak_price") and peak_start is not None:
            energy_by_hour = charging.get("energy_by_hour") or {}
            # Same peak-window test as tariff.price_at, so this matches
            # exactly what the account was actually charged for each hour.
            peak_kwh = sum(
                v for h, v in energy_by_hour.items() if peak_start <= int(h) < peak_end
            )
            rate_delta = tou["peak_price"] - tou["offpeak_price"]
            savings = round(peak_kwh * rate_delta, 2)
            if peak_kwh > 0 and savings > 0:
                recs.append(
                    _rec(
                        "Cost",
                        "high" if savings >= 20 else "medium",
                        f"Smart charging: shift {peak_kwh:.1f} kWh off peak hours",
                        f"{peak_kwh:.1f} kWh was charged between {peak_start:02d}:00–"
                        f"{peak_end:02d}:00 at the peak rate ({currency} "
                        f"{tou['peak_price']:.2f}/kWh) instead of the off-peak rate "
                        f"({currency} {tou['offpeak_price']:.2f}/kWh). Scheduling charging "
                        f"to start after {peak_end:02d}:00 (the car's own scheduled-charge "
                        "setting, in the Tesla app — this dashboard doesn't drive the car) "
                        "would have avoided this at no change to how much you drive.",
                        f"{currency} {savings:.2f} over this window",
                    )
                )
        else:
            by_hour = charging["charges_by_hour"]
            peak_charges = sum(v for h, v in by_hour.items() if 7 <= int(h) <= 21)
            if peak_charges > charging["total_sessions"] * 0.4:
                recs.append(
                    _rec(
                        "Cost",
                        "medium",
                        "A lot of charging happens during peak hours",
                        "Many sessions start between 07:00 and 21:00. If your utility has a "
                        "time-of-use tariff, scheduling charging to start after midnight (the "
                        "car supports a scheduled departure/charge time) can cut the per-kWh "
                        "price substantially.",
                        "10–40% off the electricity portion of your charging bill",
                    )
                )

    # --- Usage patterns ------------------------------------------------------
    if driving.get("available"):
        avg_trip = driving["avg_trip_distance_km"]
        if avg_trip < 6:
            recs.append(
                _rec(
                    "Usage",
                    "low",
                    "Many very short trips",
                    "Short hops never let the battery and cabin reach efficient operating "
                    "temperature, so the Wh/km on these is high. Combining errands into a "
                    "single round-trip improves overall efficiency.",
                    None,
                )
            )

    if not recs:
        recs.append(
            _rec(
                "Overall",
                "low",
                "Driving and charging look efficient",
                "No major inefficiencies detected in the analysed period. Keep charging "
                "mostly to 80–90% on AC and maintain your current driving style.",
                None,
            )
        )

    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: order[r["priority"]])
    return recs
