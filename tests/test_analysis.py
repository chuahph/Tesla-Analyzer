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


def test_charge_location_inferred_from_nearby_trip():
    from datetime import datetime

    from app.analysis.charging import analyze
    from app.models import Charge, Drive

    # A drive ends at "Juru" at 16:16; a charge (no GPS) starts at 16:20.
    drive = Drive(start_time=datetime(2026, 7, 4, 16, 1), end_time=datetime(2026, 7, 4, 16, 16),
                  distance_km=7.1, duration_min=15, avg_speed_kmh=28, max_speed_kmh=60,
                  start_soc=60, end_soc=55, energy_used_kwh=1.0, outside_temp_c=34,
                  start_location="Seberang Jaya", end_location="Juru")
    charge = Charge(start_time=datetime(2026, 7, 4, 16, 20), end_time=datetime(2026, 7, 4, 16, 55),
                    duration_min=35, start_soc=55, end_soc=80, energy_added_kwh=18.0,
                    charge_type="DC", max_power_kw=120, location="", cost=16.2, outside_temp_c=34)
    r = analyze([charge], [drive])
    # [name, count, kWh] — inferred place + charger type + energy delivered.
    assert r["top_locations"] == [["Juru · DC", 1, 18.0]]
    # Without any nearby drive it falls back to the charger type.
    assert analyze([charge], [])["top_locations"] == [["DC fast charger", 1, 18.0]]
    # A real named place with a comma is kept (not mistaken for coordinates).
    charge.location = "Bayan Mutiara, George Town"
    assert analyze([charge], [])["top_locations"] == [["Bayan Mutiara, George Town · DC", 1, 18.0]]


def test_km_per_soc_from_net_drop_on_short_trips():
    """Several short sub-1% trips still yield km/1% via the net SoC drop."""
    from datetime import datetime

    from app.analysis.driving import analyze
    from app.models import Drive

    # Three 3 km trips, each end_soc == start_soc (no integer tick), but the
    # net battery use across them is 80 -> 77 = 3% over 9 km => 3 km/1%.
    def d(hour, ssoc, esoc):
        return Drive(start_time=datetime(2026, 7, 4, hour, 0),
                     end_time=datetime(2026, 7, 4, hour, 10),
                     distance_km=3.0, duration_min=10.0, avg_speed_kmh=30,
                     max_speed_kmh=45, start_soc=ssoc, end_soc=esoc,
                     energy_used_kwh=0.0, outside_temp_c=30.0)
    drives = [d(8, 80, 80), d(12, 79, 79), d(18, 78, 77)]
    r = analyze(drives, 150.0, 75.0)
    assert r["km_per_soc_pct"] == 3.0


def test_zero_energy_drive_does_not_dilute_efficiency():
    """A 0-kWh drive (range gap) must not lower Wh/km or inflate the score."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.analysis import efficiency as efficiency_analysis
    from app.models import Drive

    def mk(hour, dist, kwh):
        return Drive(start_time=datetime(2026, 7, 4, hour, 0),
                     end_time=datetime(2026, 7, 4, hour, 30),
                     distance_km=dist, duration_min=30, avg_speed_kmh=60,
                     max_speed_kmh=90, start_soc=80, end_soc=75,
                     energy_used_kwh=kwh, outside_temp_c=28)
    real = mk(8, 40.0, 6.0)          # 150 Wh/km
    gap = mk(12, 40.0, 0.0)          # data gap — no energy
    drv = driving_analysis.analyze([real, gap], 150.0, 75.0)
    eff = efficiency_analysis.analyze([real, gap], 150.0)
    # 6 kWh / 40 km = 150 Wh/km — the phantom 40 km of the gap drive excluded.
    assert drv["avg_efficiency_wh_per_km"] == 150.0
    assert eff["avg_efficiency_wh_per_km"] == 150.0   # both engines agree
    assert drv["eco_score"] == 85                      # exactly rated -> 85
    # Distance/count still include every drive.
    assert drv["total_distance_km"] == 80.0
    assert drv["total_drives"] == 2


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
