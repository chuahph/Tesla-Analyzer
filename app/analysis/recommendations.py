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
) -> list[dict[str, Any]]:
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

        # Off-peak shifting.
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
