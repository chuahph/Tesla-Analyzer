"""Tests for the driving-behaviour study (driving.py _behaviour + recs)."""
from datetime import datetime, timedelta

from app.analysis import driving as driving_analysis
from app.analysis import recommendations as recommendations_engine
from app.models import Drive

T0 = datetime(2026, 7, 1, 10, 0)


def drive(km, kwh, avg, vmax, hour=10, temp=28, offset=0):
    t = T0.replace(hour=hour) + timedelta(days=offset)
    return Drive(
        vehicle_id=1, start_time=t, end_time=t + timedelta(minutes=30),
        distance_km=km, duration_min=30, start_soc=80, end_soc=70,
        energy_used_kwh=kwh, avg_speed_kmh=avg, max_speed_kmh=vmax,
        outside_temp_c=temp, start_location="", end_location="",
    )


def fleet():
    calm = [drive(20, 20 * 0.150, 60, 90, offset=i) for i in range(10)]      # 150 Wh/km
    fast = [drive(20, 20 * 0.200, 95, 130, offset=10 + i) for i in range(5)]  # 200 Wh/km
    return calm + fast


def test_behaviour_measures_speeding_penalty():
    beh = driving_analysis.analyze(fleet())["behaviour"]
    assert beh["available"]
    # 5 of 15 equal-length drives above 110 km/h -> a third of the km.
    assert 30 <= beh["speeding_share_pct"] <= 37
    assert 45 <= beh["speeding_penalty_wh"] <= 55        # 200 vs 150 Wh/km
    assert beh["speeding_saving_kwh"] > 2
    assert beh["score"] < 100                            # not at personal best
    assert beh["potential_saving_kwh"] > 0


def test_behaviour_recommendation_generated():
    driving = driving_analysis.analyze(fleet())
    recs = recommendations_engine.build(
        driving, {"available": False}, {"available": False},
        energy_price=0.90, currency="RM",
    )
    titles = [r["title"] for r in recs]
    assert any("Fast highway driving" in t for t in titles)
    speeding = next(r for r in recs if "Fast highway driving" in r["title"])
    assert "RM" in speeding["estimated_saving"]


def test_behaviour_unavailable_with_few_drives():
    beh = driving_analysis.analyze([drive(20, 3, 60, 90)])["behaviour"]
    assert beh["available"] is False


def test_clean_fleet_has_no_behaviour_recs():
    calm = [drive(20, 20 * 0.150, 60, 90, offset=i) for i in range(12)]
    driving = driving_analysis.analyze(calm)
    recs = recommendations_engine.build(
        driving, {"available": False}, {"available": False},
        energy_price=0.90, currency="RM",
    )
    assert not any(r["category"] == "Driving behaviour" for r in recs)
