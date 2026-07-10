"""Turn a period's analysis, compared against the prior equal-length period,
into a short data-driven narrative — a handful of plain sentences a person
would actually write about their own month, rather than a bare stats table.

Every figure quoted here comes straight from driving/charging/efficiency
analyze() output already computed elsewhere (see app/api/routes.py) — this
module only picks which facts are worth saying and phrases them, it doesn't
compute anything new.
"""
from __future__ import annotations

from typing import Any

# A period-over-period change below this is noise, not worth a sentence.
NOTABLE_PCT_CHANGE = 5.0


def _pct_change(new: float, old: float) -> float | None:
    if not old:
        return None
    return round((new - old) / old * 100.0, 0)


def build(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    currency: str,
) -> list[str]:
    """``current``/``previous``: {"driving", "charging", "efficiency"} dicts,
    each analyze() output for its own period. ``previous`` is the equal-length
    period immediately before ``current``'s, or None when there isn't one
    (not enough history yet) — comparisons are simply omitted in that case,
    not guessed at.

    Returns a list of sentences, most notable/headline fact first.
    """
    drv, chg, eff = current["driving"], current["charging"], current["efficiency"]
    if not drv.get("available"):
        return ["No drives logged in this period yet."]

    prev_drv = (previous or {}).get("driving") or {}
    prev_chg = (previous or {}).get("charging") or {}

    lines: list[str] = []

    # Opening: headline distance + trip count, with a period-over-period
    # comparison when it's actually notable (a few % either way is normal
    # month-to-month noise, not a trend worth remarking on).
    dist = drv["total_distance_km"]
    opening = f"You drove {dist:,.0f} km across {drv['total_drives']} trips"
    if prev_drv.get("available"):
        delta = _pct_change(dist, prev_drv["total_distance_km"])
        if delta is not None and abs(delta) >= NOTABLE_PCT_CHANGE:
            direction = "up" if delta > 0 else "down"
            opening += (
                f", {direction} {abs(delta):.0f}% from "
                f"{prev_drv['total_distance_km']:,.0f} km the period before"
            )
    lines.append(opening + ".")

    # Efficiency vs the rated figure — the single most legible "how well
    # did I drive" number, so it comes right after the headline.
    if eff.get("available") and eff.get("avg_efficiency_wh_per_km"):
        vs_rated = eff.get("vs_rated_pct")
        eff_line = f"Average efficiency was {eff['avg_efficiency_wh_per_km']:.0f} Wh/km"
        if vs_rated is not None:
            eff_line += (
                f", {abs(vs_rated):.0f}% {'above' if vs_rated > 0 else 'below'} the rated figure"
            )
        lines.append(eff_line + ".")

    # Cost — what it actually cost to drive, in the account's own currency.
    if drv.get("total_cost") is not None:
        cost_line = f"Driving cost {currency} {drv['total_cost']:.2f}"
        if drv.get("cost_per_km") is not None:
            cost_line += f" ({currency} {drv['cost_per_km']:.3f}/km)"
        lines.append(cost_line + ".")

    # Charging habits, with a DC-share callout only when it's meaningful
    # (occasional Supercharging on a trip isn't worth flagging).
    if chg.get("available"):
        chg_line = (
            f"{chg['total_energy_kwh']:.0f} kWh added across "
            f"{chg['total_sessions']} charging session"
            f"{'s' if chg['total_sessions'] != 1 else ''}"
        )
        if chg.get("dc_energy_share_pct", 0) >= 15:
            chg_line += f", {chg['dc_energy_share_pct']:.0f}% from DC fast charging"
        lines.append(chg_line + ".")
    elif prev_chg.get("available"):
        lines.append("No charging sessions logged this period.")

    # Most frequent route — only worth naming when it actually repeated.
    top_routes = drv.get("top_routes") or []
    if top_routes and top_routes[0][1] >= 2:
        route, count = top_routes[0][0], top_routes[0][1]
        lines.append(f"Your most frequent route was {route} ({count} times).")

    # Longest single trip — a concrete, memorable data point.
    if drv.get("longest_trip_km"):
        lines.append(f"Longest single trip: {drv['longest_trip_km']:.0f} km.")

    return lines
