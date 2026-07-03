"""Tests for the battery health estimator (app/analysis/battery.py)."""
from app.analysis.battery import analyze


def mk(soc, full_range_km):
    """A reading of a pack whose true full range is ``full_range_km``."""
    return {"soc": soc, "range_km": full_range_km * soc / 100.0}


def test_insufficient_readings():
    r = analyze([mk(80, 500)] * 3)
    assert r["available"] is False
    assert "Collecting data" in r["note"]


def test_low_soc_readings_are_ignored():
    r = analyze([mk(10, 500)] * 20)  # all below the 20% floor
    assert r["available"] is False


def test_healthy_pack():
    readings = [mk(50 + (i % 40), 500) for i in range(30)]
    r = analyze(readings)
    assert r["available"]
    assert r["degradation_pct"] < 1.5
    assert r["health_pct"] > 98.5
    assert abs(r["est_full_range_km"] - 500) < 10


def test_degraded_pack():
    old = [mk(60, 500) for _ in range(15)]   # what the pack used to show
    new = [mk(60, 450) for _ in range(15)]   # what it shows now
    r = analyze(old + new)
    assert r["available"]
    assert 8 <= r["degradation_pct"] <= 12   # ~10% drop
    assert r["baseline_full_range_km"] == 500
    assert r["est_full_range_km"] == 450
