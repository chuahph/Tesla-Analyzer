"""Proactive alerts: turn the analysis the dashboard already computes into
push/webhook notifications, so a worsening trend or a due service reaches the
owner without them opening the app.

Two halves, kept separate so both are unit-testable without a DB or network:
  - ``evaluate(...)`` is pure: given the current + previous window analysis it
    returns the alert candidates that fire, each with a stable ``key`` and a
    ``signature`` (what specifically triggered it).
  - ``dispatch(...)`` handles de-duplication against the last-sent state and
    calls the injected ``notify`` — an alert only re-sends if its signature
    changes or a cooldown has passed, so a standing condition doesn't spam the
    owner every time the check runs.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from . import state

# How much worse this window's Wh/km must be than the previous equal window
# before it's worth a notification (below this is normal week-to-week noise).
EFFICIENCY_DROP_PCT = 12.0
# A modern pack loses ~1–2%/yr early; a projected rate above this is worth
# flagging as "faster than typical".
FAST_DEGRADATION_PCT_PER_YEAR = 3.0
# A single parked gap losing at least this much is an unusual standby event
# (Sentry left on, cabin-overheat cooling in the heat, ...), not routine drain.
STANDBY_GAP_KWH = 1.5
STANDBY_GAP_PCT = 2.0
# Don't re-send a still-true alert more often than this.
COOLDOWN_DAYS = 7

_STATE_PREFIX = "alert::"


def evaluate(
    *,
    now: datetime,
    efficiency: dict[str, Any] | None,
    prev_efficiency: dict[str, Any] | None,
    battery: dict[str, Any] | None,
    service_rows: list[dict[str, Any]] | None,
    standby_longest: dict[str, Any] | None,
    currency: str = "",
) -> list[dict[str, str]]:
    """Return every alert that currently fires. Each is
    ``{key, signature, title, body}``. Pure — no I/O."""
    out: list[dict[str, str]] = []

    # 1. Efficiency worsening vs the previous equal-length window.
    eff = efficiency or {}
    peff = prev_efficiency or {}
    if eff.get("available") and peff.get("available"):
        now_wh = eff.get("avg_efficiency_wh_per_km")
        prev_wh = peff.get("avg_efficiency_wh_per_km")
        if now_wh and prev_wh:
            delta_pct = (now_wh - prev_wh) / prev_wh * 100.0
            if delta_pct >= EFFICIENCY_DROP_PCT:
                out.append({
                    "key": "efficiency_drop",
                    "signature": f"{round(delta_pct)}",
                    "title": "Efficiency is slipping",
                    "body": f"Your Wh/km is up {delta_pct:.0f}% vs the period before "
                            f"({prev_wh:.0f} → {now_wh:.0f}). Colder weather, more "
                            "highway or heavier traffic are the usual causes.",
                })

    # 2. Degradation projected to run faster than typical.
    forecast = (battery or {}).get("forecast") or {}
    if forecast.get("available") and forecast.get("loss_pct_per_year") is not None:
        rate = forecast["loss_pct_per_year"]
        if rate >= FAST_DEGRADATION_PCT_PER_YEAR:
            yrs = forecast.get("years_to_health_milestone")
            horizon = f" — about {yrs} yr to {forecast.get('health_milestone_pct', 80)}% health" if yrs else ""
            out.append({
                "key": "fast_degradation",
                "signature": f"{rate:.1f}",
                "title": "Battery losing range faster than usual",
                "body": f"Projected ~{rate:.1f}%/yr at the current trend{horizon}. "
                        "Favour AC over DC charging, avoid long spells at very high "
                        "or very low charge, and keep the daily limit at 80–90%.",
            })

    # 3. Service due or overdue.
    rows = service_rows or []
    overdue = sorted(r["type"] for r in rows if r.get("status") == "overdue")
    due_soon = sorted(r["type"] for r in rows if r.get("status") == "due_soon")
    if overdue or due_soon:
        which = overdue or due_soon
        word = "overdue" if overdue else "due soon"
        out.append({
            "key": "service_due",
            "signature": ("overdue:" if overdue else "soon:") + ",".join(which),
            "title": f"Service {word}",
            "body": f"{', '.join(t.replace('_', ' ') for t in which)} {word}. "
                    "Log it in the Service tracker once done to reset the reminder.",
        })

    # 4. An unusually large single standby / parked-drain event.
    gap = standby_longest or {}
    if (gap.get("kwh", 0.0) >= STANDBY_GAP_KWH
            and gap.get("pct", 0.0) >= STANDBY_GAP_PCT):
        cost = f" / {currency} {gap['cost']:.2f}" if gap.get("cost") is not None else ""
        cause = f" {gap['inducer']} was the likely draw." if gap.get("inducer") else ""
        out.append({
            "key": "standby_drain",
            # End timestamp -> one alert per distinct parked event.
            "signature": str(gap.get("end", "")),
            "title": "Unusual standby drain while parked",
            "body": f"Lost {gap['pct']:.1f}% ({gap['kwh']:.1f} kWh{cost}) parked over "
                    f"{gap.get('hours', 0):.0f} h with no charging.{cause} "
                    "Turn off Sentry Mode when parked somewhere safe to cut it.",
        })

    return out


def _key(alert_key: str) -> str:
    return f"{_STATE_PREFIX}{alert_key}"


def dispatch(
    session,
    candidates: list[dict[str, str]],
    notify: Callable[[str, str, str], Any],
    *,
    now: datetime,
    cooldown_days: int = COOLDOWN_DAYS,
) -> list[str]:
    """Send the alerts whose signature is new or whose last send is older than
    the cooldown; skip the rest. Persists ``{sig, sent}`` per alert key in the
    Setting KV store. ``notify(title, body, tag)`` is injected so this is
    testable without push/network. Returns the keys actually sent."""
    sent: list[str] = []
    for a in candidates:
        prev_raw = state.get(session, _key(a["key"]), "")
        prev = {}
        if prev_raw:
            try:
                prev = json.loads(prev_raw)
            except ValueError:
                prev = {}
        same_sig = prev.get("sig") == a["signature"]
        within_cooldown = False
        if same_sig and prev.get("sent"):
            try:
                age_days = (now - datetime.fromisoformat(prev["sent"])).total_seconds() / 86400.0
                within_cooldown = age_days < cooldown_days
            except ValueError:
                within_cooldown = False
        if same_sig and within_cooldown:
            continue  # already told them this, recently
        notify(a["title"], a["body"], a["key"])
        state.put(session, _key(a["key"]),
                  json.dumps({"sig": a["signature"], "sent": now.isoformat(timespec="seconds")}))
        sent.append(a["key"])
    return sent
