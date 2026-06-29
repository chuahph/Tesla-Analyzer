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
