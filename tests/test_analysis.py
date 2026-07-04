"""Tests for the analytics engine and helpers."""
from sqlalchemy import select

from app.analysis import linregress, mean, percentile
from app.analysis import charging as charging_analysis
from app.analysis import driving as driving_analysis
from app.analysis import efficiency as efficiency_analysis
from app.analysis import recommendations as recommendations_engine
from app.models import Charge, Drive


# --- helpers ---------------------------------------------------------------

def test_mean_and_percentile():
    assert mean([1, 2, 3, 4]) == 2.5
    assert mean([]) == 0.0
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([10], 0.95) == 10


def test_linregress_recovers_slope():
    xs = list(range(10))
    ys = [3 * x + 5 for x in xs]
    slope, intercept = linregress(xs, ys)
    assert abs(slope - 3) < 1e-6
    assert abs(intercept - 5) < 1e-6


# --- data generation -------------------------------------------------------

def test_sample_data_seeded(seeded):
    drives = seeded.scalars(select(Drive)).all()
    charges = seeded.scalars(select(Charge)).all()
    assert len(drives) > 50
    assert len(charges) > 10
    # SoC stays in a plausible band.
    assert all(5 <= d.end_soc <= 100 for d in drives)
    assert all(c.energy_added_kwh > 0 for c in charges)


# --- driving ---------------------------------------------------------------

def test_driving_analysis(seeded):
    drives = seeded.scalars(select(Drive)).all()
    result = driving_analysis.analyze(list(drives))
    assert result["available"]
    assert result["total_distance_km"] > 0
    assert sum(result["trips_by_weekday"].values()) == result["total_drives"]
    assert result["avg_efficiency_wh_per_km"] > 0


def test_driving_empty():
    assert driving_analysis.analyze([]) == {"available": False}


def test_eco_score_grades_efficiency():
    from app.analysis.driving import eco_score, score_grade

    assert eco_score(150, 150) == 85          # exactly rated
    assert eco_score(127.5, 150) == 100       # 15% under rated → capped 100
    assert eco_score(195, 150) == 55          # 30% over rated
    assert eco_score(0, 150) == 0
    assert score_grade(90) == "A" and score_grade(72) == "B"
    assert score_grade(58) == "C" and score_grade(45) == "D" and score_grade(20) == "E"


def test_driving_analysis_reports_scores(seeded):
    drives = seeded.scalars(select(Drive)).all()
    result = driving_analysis.analyze(list(drives), 150.0)
    assert 0 <= result["eco_score"] <= 100
    assert result["eco_grade"] in ("A", "B", "C", "D", "E")
    assert all("eco_score" in t for t in result["recent_trips"])


def test_trip_conditions_infer_character():
    from datetime import datetime

    from app.analysis.driving import _trip_conditions
    from app.models import Drive

    def drive(avg, mx, hour=12, temp=25.0):
        return Drive(
            start_time=datetime(2026, 7, 4, hour, 0), end_time=datetime(2026, 7, 4, hour, 30),
            distance_km=20.0, duration_min=30.0, avg_speed_kmh=avg, max_speed_kmh=mx,
            outside_temp_c=temp,
        )

    assert _trip_conditions(drive(95, 115)) == "highway cruise"
    assert _trip_conditions(drive(35, 100)) == "highway + congestion"
    assert _trip_conditions(drive(20, 60)) == "stop-go traffic"
    assert _trip_conditions(drive(30, 45)) == "city driving"
    assert _trip_conditions(drive(55, 70)) == "steady flow"
    assert "peak hour" in _trip_conditions(drive(30, 45, hour=8))
    assert "hot 35°C" in _trip_conditions(drive(30, 45, temp=35.0))


# --- charging --------------------------------------------------------------

def test_charging_analysis(seeded):
    charges = seeded.scalars(select(Charge)).all()
    result = charging_analysis.analyze(list(charges))
    assert result["available"]
    assert result["ac_sessions"] + result["dc_sessions"] == result["total_sessions"]
    assert 0 <= result["dc_energy_share_pct"] <= 100
    assert result["total_cost"] > 0


# --- efficiency ------------------------------------------------------------

def test_efficiency_analysis(seeded):
    drives = seeded.scalars(select(Drive)).all()
    result = efficiency_analysis.analyze(list(drives), rated_wh_per_km=150)
    assert result["available"]
    assert result["worst_efficiency_wh_per_km"] >= result["best_efficiency_wh_per_km"]
    # Cold weather should be less efficient -> negative slope of Wh/km vs temp.
    assert result["temp_efficiency_slope_wh_per_c"] < 0


# --- recommendations -------------------------------------------------------

def test_recommendations_built(seeded):
    drives = list(seeded.scalars(select(Drive)).all())
    charges = list(seeded.scalars(select(Charge)).all())
    driving = driving_analysis.analyze(drives)
    charging = charging_analysis.analyze(charges)
    efficiency = efficiency_analysis.analyze(drives, rated_wh_per_km=150)
    recs = recommendations_engine.build(
        driving, charging, efficiency, energy_price=0.30, currency="USD"
    )
    assert recs
    assert all({"category", "priority", "title", "detail"} <= set(r) for r in recs)
    # Priorities are sorted high -> low.
    order = {"high": 0, "medium": 1, "low": 2}
    vals = [order[r["priority"]] for r in recs]
    assert vals == sorted(vals)


def test_recommendations_empty_data():
    recs = recommendations_engine.build(
        {"available": False}, {"available": False}, {"available": False},
        energy_price=0.30, currency="USD",
    )
    assert len(recs) == 1
    assert recs[0]["category"] == "Overall"
