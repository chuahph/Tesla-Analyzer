"""Tests for proactive alerts (app/alerts.py)."""
from datetime import datetime, timedelta

from app import alerts

NOW = datetime(2026, 7, 24, 9, 0)


def _eff(wh):
    return {"available": True, "avg_efficiency_wh_per_km": wh}


def test_efficiency_drop_fires_only_past_threshold():
    # 12%+ worse fires; a small rise doesn't.
    big = alerts.evaluate(now=NOW, efficiency=_eff(180), prev_efficiency=_eff(150),
                          battery=None, service_rows=None, standby_longest=None)
    assert any(a["key"] == "efficiency_drop" for a in big)

    small = alerts.evaluate(now=NOW, efficiency=_eff(153), prev_efficiency=_eff(150),
                            battery=None, service_rows=None, standby_longest=None)
    assert not any(a["key"] == "efficiency_drop" for a in small)


def test_fast_degradation_fires_from_forecast_rate():
    battery = {"forecast": {"available": True, "loss_pct_per_year": 4.2,
                            "years_to_health_milestone": 4.5, "health_milestone_pct": 80}}
    out = alerts.evaluate(now=NOW, efficiency=None, prev_efficiency=None,
                          battery=battery, service_rows=None, standby_longest=None)
    a = next(a for a in out if a["key"] == "fast_degradation")
    assert "4.2" in a["signature"]
    # A gentle, normal rate doesn't fire.
    slow = {"forecast": {"available": True, "loss_pct_per_year": 1.4}}
    out2 = alerts.evaluate(now=NOW, efficiency=None, prev_efficiency=None,
                           battery=slow, service_rows=None, standby_longest=None)
    assert not any(a["key"] == "fast_degradation" for a in out2)


def test_service_alert_prefers_overdue_and_lists_types():
    rows = [{"type": "tyre_rotation", "status": "overdue"},
            {"type": "brake_fluid", "status": "due_soon"},
            {"type": "cabin_filter", "status": "ok"}]
    a = next(a for a in alerts.evaluate(
        now=NOW, efficiency=None, prev_efficiency=None, battery=None,
        service_rows=rows, standby_longest=None) if a["key"] == "service_due")
    assert "overdue" in a["title"]
    assert "tyre rotation" in a["body"]
    assert a["signature"].startswith("overdue:")


def test_standby_alert_fires_on_a_big_parked_event():
    gap = {"kwh": 2.4, "pct": 3.1, "hours": 40, "end": "2026-07-23T22:00:00",
           "inducer": "Sentry Mode (maybe)", "cost": 2.16}
    a = next(a for a in alerts.evaluate(
        now=NOW, efficiency=None, prev_efficiency=None, battery=None,
        service_rows=None, standby_longest=gap, currency="RM") if a["key"] == "standby_drain")
    assert "Sentry Mode" in a["body"]
    assert a["signature"] == "2026-07-23T22:00:00"
    # Same day (11 h before NOW): dated, but no "days ago" — it just happened.
    assert "23 Jul" in a["body"] and "ago" not in a["body"]
    # A small routine gap doesn't fire.
    small = {"kwh": 0.4, "pct": 0.5, "hours": 12, "end": "x"}
    assert not any(a["key"] == "standby_drain" for a in alerts.evaluate(
        now=NOW, efficiency=None, prev_efficiency=None, battery=None,
        service_rows=None, standby_longest=small))


def test_standby_alert_says_how_old_a_resent_gap_is():
    """/api/alerts/check scans a rolling 30 days and re-sends the same worst
    gap once COOLDOWN_DAYS has passed — so a resend with no date on it reads
    as something that just happened, when it can be weeks old. Measured live:
    a 4.3%/2.9 kWh gap arrived as a plain "parked over 4 h" push with nothing
    to say it was from days earlier, read against the car's own "since last
    charge" screen (a different window entirely) as if it disagreed with it.
    """
    old_gap = {"kwh": 2.9, "pct": 4.3, "hours": 4, "end": "2026-07-19T10:00:00",
              "inducer": "Sentry Mode & climate (maybe)", "cost": 2.64}
    a = next(a for a in alerts.evaluate(
        now=NOW, efficiency=None, prev_efficiency=None, battery=None,
        service_rows=None, standby_longest=old_gap, currency="RM")
        if a["key"] == "standby_drain")
    assert "19 Jul, 4 days ago" in a["body"]
    # An unparsable end timestamp costs the date, not the whole alert.
    undated = dict(old_gap, end="not-a-date")
    b = next(a for a in alerts.evaluate(
        now=NOW, efficiency=None, prev_efficiency=None, battery=None,
        service_rows=None, standby_longest=undated, currency="RM")
        if a["key"] == "standby_drain")
    assert "Lost 4.3%" in b["body"] and "ago" not in b["body"]


def test_dispatch_dedups_within_cooldown_and_resends_on_change(session):
    fired = []
    notify = lambda title, body, tag: fired.append(tag)
    cand = [{"key": "efficiency_drop", "signature": "15",
             "title": "T", "body": "B"}]

    # First time -> sent.
    sent1 = alerts.dispatch(session, cand, notify, now=NOW)
    assert sent1 == ["efficiency_drop"] and fired == ["efficiency_drop"]

    # Same signature a day later -> suppressed (within 7-day cooldown).
    sent2 = alerts.dispatch(session, cand, notify, now=NOW + timedelta(days=1))
    assert sent2 == [] and fired == ["efficiency_drop"]

    # Signature changes (got worse) -> re-sent even within cooldown.
    worse = [{**cand[0], "signature": "22"}]
    sent3 = alerts.dispatch(session, worse, notify, now=NOW + timedelta(days=2))
    assert sent3 == ["efficiency_drop"]

    # Same signature but past the cooldown -> re-sent.
    sent4 = alerts.dispatch(session, worse, notify, now=NOW + timedelta(days=10))
    assert sent4 == ["efficiency_drop"]
