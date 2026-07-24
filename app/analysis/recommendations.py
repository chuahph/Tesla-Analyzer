"""Turn the raw analysis into a graded assessment: an overall verdict, the
strengths worth keeping, the money actually recoverable, and prioritised
recommendations.

``build()`` returns the flat recommendation list (each a dict with category,
priority, title, detail, estimated_saving text, and structured saving_kwh/
saving_cost/bucket for anything with a real figure). ``assess()`` wraps that
into the full scorecard the dashboard leads with.
"""
from __future__ import annotations

from typing import Any


def _rec(category, priority, title, detail, saving=None, *,
         kwh=None, cost=None, bucket=None):
    """A single recommendation.

    ``saving`` is the human display string (unchanged from before). ``kwh``/
    ``cost`` are the same figure structured so the assessment can total and
    rank by it; ``bucket`` groups a saving as "driving" (overlapping with the
    other driving tips — never additive) or "charging" (independent, safe to
    add), so the header total can avoid double-counting. All three are None
    for qualitative tips (e.g. "slower degradation") that carry no figure.
    """
    return {
        "category": category,
        "priority": priority,
        "title": title,
        "detail": detail,
        "estimated_saving": saving,
        "saving_kwh": round(kwh, 1) if kwh is not None else None,
        "saving_cost": round(cost, 2) if cost is not None else None,
        "bucket": bucket,
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
    standby_inducer: str | None = None,
) -> list[dict[str, Any]]:
    """``tou``, when a real time-of-use tariff is configured: {peak_price,
    offpeak_price, peak_start_hour, peak_end_hour} — sizes the smart
    charging advisor's saving from the account's own peak-hour energy
    instead of a generic heuristic. None (the default) keeps the old
    session-count-based hint for accounts on a flat rate.

    ``standby_inducer``: what was likely running during the biggest parked
    gap (e.g. "Sentry Mode (maybe)"), from routes._idle_inducer — named in
    the standby-drain tip when known."""
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
    # One consolidated card rather than a separate one per factor: speeding,
    # stop-go, short trips, peak hour and hot weather are overlapping
    # contributors to the same "above your own best" inefficiency, so listing
    # them as five near-duplicate tips read as noise. They fold into a single
    # "driving style" card whose contributors are named inline and whose
    # recoverable figure is the (non-double-counted) best-quartile lever.
    beh = (driving or {}).get("behaviour") or {}
    if beh.get("available"):
        # (key, short phrase used inline in the consolidated detail)
        factor_labels = [
            ("speeding", "fast highway driving (over 110 km/h)"),
            ("stopgo", "stop-and-go traffic"),
            ("short_trip", "short cold-start trips (under 3 km)"),
            ("peak_hour", "peak-hour congestion"),
            ("hot_weather", "hot-weather A/C load (33°C+)"),
        ]
        fired = []
        for key, label in factor_labels:
            share = beh.get(f"{key}_share_pct", 0)
            pen = beh.get(f"{key}_penalty_wh", 0)
            kwh = beh.get(f"{key}_saving_kwh", 0)
            if share >= 10 and pen >= 8 and kwh >= 0.5:
                fired.append((label, share, pen, kwh))

        pk = beh.get("potential_saving_kwh", 0)
        best_q = beh.get("best_quartile_wh_per_km")
        lever_ok = pk >= 1 and beh.get("score", 100) < 90

        if fired or lever_ok:
            contributors = "; ".join(
                f"{label} (+{pen} Wh/km over {share}% of km)"
                for label, share, pen, _ in fired
            )
            if fired:
                detail = (
                    "Your everyday driving runs above your own efficient baseline. "
                    f"The measurable contributors this window: {contributors}. "
                )
            else:
                detail = "Your everyday driving runs a little above your own best quartile. "
            if lever_ok:
                detail += (
                    f"Matching your most efficient quartile ({best_q:.0f} Wh/km) — a "
                    f"level you already hit regularly — would recover about {pk:.1f} kWh: "
                    "smoother acceleration, easing motorway cruise speed, and letting "
                    "regen do the braking are the levers."
                )
                saving = f"{currency} {pk * energy_price:.2f} in this window"
                kwh_val, cost_val, bucket = pk, pk * energy_price, "driving_lever"
            else:
                # Factors fired but the overall best-quartile gap is small
                # (score >= 90): size the recoverable from the single biggest
                # contributor rather than summing the overlapping factors, so
                # the figure is honest and never inflates. bucket="driving"
                # keeps it out of the headline total (only the lever counts).
                detail += "Smoother, steadier driving trims most of it."
                top_kwh = max(k for _, _, _, k in fired)
                saving = f"~{top_kwh:.1f} kWh / {currency} {top_kwh * energy_price:.2f} " \
                         "from the biggest single contributor"
                kwh_val, cost_val, bucket = top_kwh, top_kwh * energy_price, "driving"
            recs.append(_rec(
                "Driving behaviour",
                "medium" if fired else "low",
                "Driving style is adding avoidable consumption",
                detail, saving, kwh=kwh_val, cost=cost_val, bucket=bucket,
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
                    # bucket="driving": the vs-rated gap is another view of the
                    # same driving inefficiency, not money on top of it.
                    kwh=extra_kwh, cost=cost, bucket="driving",
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
            # DC's own rate, not the AC+DC blended avg_cost_per_kwh -- blending
            # in (cheaper) AC sessions understates DC's real premium over home
            # charging, sometimes by a lot (e.g. mostly-AC account with one DC
            # top-up: the blended average sits close to the AC rate, making
            # "switch DC to home AC" look like almost no saving at all).
            dc_rate = charging["dc_cost"] / charging["dc_energy_kwh"] if charging["dc_energy_kwh"] else 0.0
            dc_saving = max(dc_rate - energy_price, 0.0) * charging["dc_energy_kwh"]
            recs.append(
                _rec(
                    "Battery health",
                    "medium",
                    f"{dc_share:.0f}% of energy comes from DC fast charging",
                    "Heavy reliance on Superchargers/DC adds heat and stress to the pack "
                    "and is more expensive per kWh than home AC. Shifting routine charging "
                    "to overnight AC at home extends battery life and lowers cost.",
                    f"Up to {currency} {dc_saving:.0f}"
                    " saved by moving DC energy to home AC",
                    # bucket="charging": independent of driving style and of the
                    # peak-shift saving below, so it adds into the header total.
                    kwh=charging["dc_energy_kwh"], cost=dc_saving, bucket="charging",
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
                        kwh=peak_kwh, cost=savings, bucket="charging",
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

    # --- Standby / vampire drain (energy lost while parked) ------------------
    # The parked-drain equivalent of Tesla's own Energy > Park screen, from
    # the vampire_drain measurement already on the driving dict. Sized in
    # kWh/cost, but deliberately NOT added to the assessment's headline
    # "recoverable" total (see assess): only the Sentry/climate share is
    # actually avoidable — baseline self-discharge isn't — so claiming the
    # whole figure as recoverable would overstate it.
    vd = (driving or {}).get("vampire_drain") or {}
    vk = vd.get("kwh", 0.0)
    vh = vd.get("hours", 0.0)
    vg = vd.get("gaps", 0)
    if vk >= 0.5 and vg >= 1:
        cost = vk * energy_price
        cause = ""
        if standby_inducer:
            # _idle_inducer already hedges with "(maybe)"; fold it in plainly.
            cause = f" {standby_inducer} was the likely main draw."
        recs.append(_rec(
            "Standby",
            "medium" if cost >= 5 else "low",
            f"~{vk:.1f} kWh drained while parked (standby)",
            f"About {vk:.1f} kWh / {currency} {cost:.2f} was lost to standby drain "
            f"across {vg} parked gap{'s' if vg > 1 else ''} ({vh:.0f} h total) with no "
            f"charging in between.{cause} Much of it is avoidable — turn off Sentry Mode "
            "when parked somewhere safe, and switch off cabin-overheat cooling or "
            "preconditioning when you don't need them. Some baseline self-discharge is "
            "normal and can't be removed.",
            f"Up to {currency} {cost:.2f} if the avoidable (Sentry/climate) share is cut",
            kwh=vk, cost=cost, bucket="standby",
        ))

    # --- Usage patterns ------------------------------------------------------
    if driving.get("available"):
        avg_trip = driving.get("avg_trip_distance_km", 99)
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

    # Primary sort by priority tier, then by real money within a tier — so a
    # RM50 tip outranks a RM2 tip both labelled "medium", while a qualitative
    # high-priority tip (no figure) still leads its tier.
    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: (order[r["priority"]], -(r["saving_cost"] or 0.0)))
    return recs


def _strengths(
    driving: dict[str, Any], charging: dict[str, Any],
    efficiency: dict[str, Any], battery: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """What this account is already doing well — an assessment is balanced,
    not just a list of problems. Only genuinely good signals, each with the
    number that earned it."""
    out: list[dict[str, str]] = []
    eco = (driving or {}).get("eco_score")
    grade = (driving or {}).get("eco_grade")
    if eco is not None and eco >= 85:
        out.append({"title": "Efficient driving",
                    "detail": f"Eco-score {eco}/100 (grade {grade}) — consistently at or "
                              "below rated consumption."})
    beh = (driving or {}).get("behaviour") or {}
    if beh.get("available") and beh.get("score", 0) >= 95:
        out.append({"title": "Consistent driving style",
                    "detail": "Your day-to-day driving already tracks your own best "
                              "quartile closely — little variance to recover."})
    if battery and battery.get("available") and battery.get("degradation_pct", 100) < 4:
        out.append({"title": "Battery health strong",
                    "detail": f"Only {battery['degradation_pct']:.0f}% projected "
                              "degradation — better than typical for the mileage."})
    if charging.get("available"):
        if charging.get("dc_energy_share_pct", 100) < 10:
            out.append({"title": "Mostly home/AC charging",
                        "detail": "Little DC fast-charging — gentler on the pack and "
                                  "cheaper per kWh."})
        if charging.get("full_charge_share_pct", 100) < 5:
            out.append({"title": "Rarely charges to 100%",
                        "detail": "Keeping the daily ceiling below full is exactly what "
                                  "preserves long-term capacity."})
    if efficiency.get("available") and efficiency.get("vs_rated_pct", 100) <= 0:
        out.append({"title": "Beating rated efficiency",
                    "detail": "Your average Wh/km is at or under the EPA/rated figure "
                              "for this car."})
    return out


def _trend(
    driving: dict[str, Any], efficiency: dict[str, Any],
    prev: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """This window vs the equal-length one before it, for the two figures a
    driver actually steers by: efficiency (Wh/km) and cost per km. None when
    there's no comparable previous period (short/since-charge windows) or too
    little data either side."""
    if not prev:
        return None
    prev_eff = prev.get("efficiency") or {}
    prev_drv = prev.get("driving") or {}
    if not (efficiency.get("available") and prev_eff.get("available")):
        return None

    def _one(now_val, prev_val, lower_is_better=True):
        if not now_val or not prev_val:
            return None
        delta_pct = round((now_val - prev_val) / prev_val * 100.0, 1)
        # A move under ~2% either way is noise, not a trend.
        if abs(delta_pct) < 2:
            direction = "flat"
        elif (delta_pct < 0) == lower_is_better:
            direction = "better"
        else:
            direction = "worse"
        return {"now": round(now_val, 1), "prev": round(prev_val, 1),
                "delta_pct": delta_pct, "dir": direction}

    out: dict[str, Any] = {}
    eff = _one(efficiency.get("avg_efficiency_wh_per_km"),
               prev_eff.get("avg_efficiency_wh_per_km"))
    if eff:
        out["wh_per_km"] = eff
    cpk = _one(driving.get("cost_per_km"), prev_drv.get("cost_per_km"))
    if cpk:
        out["cost_per_km"] = cpk
    return out or None


def _confidence(driving: dict[str, Any]) -> str:
    """How much to trust the window's figures, from how much driving is in it.
    A tip fired on 3 drives is not the same evidence as one on 300."""
    n = (driving or {}).get("total_drives") or (driving or {}).get("n_drives") or 0
    dist = (driving or {}).get("total_distance_km") or 0
    if n >= 20 and dist >= 200:
        return "high"
    if n >= 5:
        return "medium"
    return "low"


def assess(
    driving: dict[str, Any],
    charging: dict[str, Any],
    efficiency: dict[str, Any],
    battery: dict[str, Any] | None = None,
    *,
    energy_price: float,
    currency: str,
    tou: dict[str, Any] | None = None,
    prev: dict[str, Any] | None = None,
    standby_inducer: str | None = None,
) -> dict[str, Any]:
    """The graded scorecard the dashboard leads with: an overall grade and
    one-line verdict, the total money genuinely recoverable this window (from
    non-overlapping levers — never the double-counted sum of every tip), what
    the account already does well, the vs-last-period trend, and the
    money-ranked recommendations underneath.

    ``prev`` (optional): {"driving":…, "efficiency":…} for the equal-length
    window before this one, used only for the trend line.
    """
    recs = build(driving, charging, efficiency, battery,
                 energy_price=energy_price, currency=currency, tou=tou,
                 standby_inducer=standby_inducer)

    # Total addressable saving, WITHOUT double-counting. The driving tips
    # (speeding/stop-go/vs-rated/…) are all views of the same inefficiency,
    # so only the single best-quartile lever represents the driving money;
    # charging savings (DC→AC, peak-shift) are independent and add on top.
    driving_lever = next(
        (r["saving_cost"] for r in recs
         if r["bucket"] == "driving_lever" and r["saving_cost"]), 0.0)
    charging_saving = sum(
        r["saving_cost"] or 0.0 for r in recs if r["bucket"] == "charging")
    total_cost = round(driving_lever + charging_saving, 2)
    # kWh mirrors the same cost-bearing contributions, so the "~N kWh / RM X"
    # pairing stays consistent — a charging tip that moves energy but saves
    # nothing at the current rates adds no cost and no kWh here.
    total_kwh = round(
        (next((r["saving_kwh"] for r in recs
               if r["bucket"] == "driving_lever" and r["saving_kwh"]), 0.0)
         + sum(r["saving_kwh"] or 0.0 for r in recs
               if r["bucket"] == "charging" and (r["saving_cost"] or 0.0) > 0)),
        1,
    )

    score = (driving or {}).get("eco_score")
    grade = (driving or {}).get("eco_grade")
    strengths = _strengths(driving, charging, efficiency, battery)
    trend = _trend(driving, efficiency, prev)
    confidence = _confidence(driving)

    # One-line verdict, synthesised rather than templated per-branch so it
    # always leads with the single most useful takeaway.
    if score is None:
        verdict = "Not enough driving logged yet to grade this window."
    else:
        head = (f"Grade {grade}" if grade else f"Eco-score {score}/100")
        if total_cost > 0:
            # "mostly …" names the bigger of the two independent levers by
            # actual money, not whichever tip happens to sort first.
            lever = (" mostly from smarter charging" if charging_saving > driving_lever
                     else " mostly from driving style") if total_cost else ""
            verdict = (f"{head} — about {currency} {total_cost:.2f} recoverable "
                       f"this window,{lever}.")
        elif strengths:
            verdict = f"{head} — no material savings on the table; solid habits."
        else:
            verdict = f"{head} — nothing obvious to improve in this window."
    if confidence == "low" and score is not None:
        verdict += " (Thin data — treat as indicative.)"

    return {
        "score": score,
        "grade": grade,
        "verdict": verdict,
        "confidence": confidence,
        "addressable_saving": {"kwh": total_kwh, "cost": total_cost,
                               "currency": currency},
        "strengths": strengths,
        "trend": trend,
        "recommendations": recs,
    }
