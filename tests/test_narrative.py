"""Tests for the data-driven monthly narrative (app/analysis/narrative.py)."""
from app.analysis.narrative import build


def _period(distance_km=100.0, drives=5, cost=20.0, cost_per_km=0.2,
            energy_kwh=15.0, sessions=2, dc_share=0.0, wh_per_km=150.0,
            vs_rated=0.0, top_routes=None, longest_km=30.0):
    return {
        "driving": {
            "available": True,
            "total_distance_km": distance_km,
            "total_drives": drives,
            "total_cost": cost,
            "cost_per_km": cost_per_km,
            "top_routes": top_routes or [],
            "longest_trip_km": longest_km,
        },
        "charging": {
            "available": sessions > 0,
            "total_energy_kwh": energy_kwh,
            "total_sessions": sessions,
            "dc_energy_share_pct": dc_share,
        },
        "efficiency": {
            "available": True,
            "avg_efficiency_wh_per_km": wh_per_km,
            "vs_rated_pct": vs_rated,
        },
    }


def test_no_drives_gives_a_plain_message():
    empty = {"driving": {"available": False}, "charging": {"available": False},
             "efficiency": {"available": False}}
    lines = build(empty, None, "RM")
    assert lines == ["No drives logged in this period yet."]


def test_headline_and_efficiency_and_cost_present_without_comparison():
    current = _period()
    lines = build(current, None, "RM")
    assert "100 km across 5 trips" in lines[0]
    assert "the period before" not in lines[0]   # no previous period to compare
    assert any("150 Wh/km" in l for l in lines)
    assert any("RM 20.00" in l for l in lines)


def test_notable_distance_change_is_called_out():
    current = _period(distance_km=150.0)
    previous = _period(distance_km=100.0)   # +50%, well above the 5% noise floor
    lines = build(current, previous, "RM")
    assert "up 50%" in lines[0]
    assert "100 km the period before" in lines[0]


def test_small_change_is_not_called_out_as_noise():
    current = _period(distance_km=103.0)
    previous = _period(distance_km=100.0)   # +3%, below the notable threshold
    lines = build(current, previous, "RM")
    assert "the period before" not in lines[0]


def test_efficiency_direction_phrased_correctly():
    worse = build(_period(vs_rated=12.0), None, "RM")
    assert "above the rated figure" in worse[1]
    better = build(_period(vs_rated=-8.0), None, "RM")
    assert "below the rated figure" in better[1]


def test_repeated_route_and_longest_trip_mentioned():
    current = _period(top_routes=[("Home → Office", 4)], longest_km=87.0)
    lines = build(current, None, "RM")
    assert any("Home → Office (4 times)" in l for l in lines)
    assert any("87 km" in l for l in lines)


def test_single_occurrence_route_not_mentioned():
    current = _period(top_routes=[("Home → Somewhere", 1)])
    lines = build(current, None, "RM")
    assert not any("Somewhere" in l for l in lines)


def test_dc_share_only_called_out_when_meaningful():
    low_dc = build(_period(dc_share=5.0), None, "RM")
    assert not any("DC fast charging" in l for l in low_dc)
    high_dc = build(_period(dc_share=40.0), None, "RM")
    assert any("40% from DC fast charging" in l for l in high_dc)


def test_no_charging_this_period_flagged_when_previous_period_had_some():
    current = _period(sessions=0)
    previous = _period(sessions=3)
    lines = build(current, previous, "RM")
    assert "No charging sessions logged this period." in lines
