"""Tests for the battery health estimator (app/analysis/battery.py)."""
from app.analysis.battery import analyze, new_range_for


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
    # Computation fields for the "how it's computed" panel.
    assert r["est_from_n"] >= 5
    assert r["reliable_band"] is True         # all readings at 60% SoC


def test_estimate_prefers_reliable_soc_band():
    # Recent low-SoC noise shouldn't move the estimate: 20 good mid-SoC readings
    # plus a couple of noisy 22%-SoC ones at the end.
    good = [mk(60, 490) for _ in range(20)]
    noisy = [mk(22, 300) for _ in range(2)]   # low SoC, wild projection
    r = analyze(good + noisy)
    assert abs(r["est_full_range_km"] - 490) < 5   # noise excluded
    assert r["reliable_band"] is True


def test_factory_spec_anchors_health():
    # A pack that consistently projects 520 km on a car whose when-new figure
    # is 549 km: without the spec health looks ~100%, with it ~94.7%.
    readings = [mk(50 + (i % 40), 520) for i in range(30)]
    naive = analyze(readings)
    assert naive["health_pct"] > 99
    anchored = analyze(readings, new_range_km=549.0)
    assert anchored["reference"] == "factory spec"
    assert anchored["reference_km"] == 549
    assert 94 <= anchored["health_pct"] <= 96


def test_spec_ignored_when_scale_mismatch():
    # Projections far above the spec mean the car reports a different range
    # scale (e.g. WLTP firmware) — fall back to the measured baseline.
    readings = [mk(60, 620) for _ in range(10)]
    r = analyze(readings, new_range_km=549.0)
    assert r["reference"] == "best seen"
    assert r["health_pct"] == 100


def test_spec_trusted_for_moderate_degradation_even_with_lots_of_history():
    # A pack that's genuinely lost a plausible amount of range (5%) keeps
    # trusting spec no matter how much history accumulates — most tracking
    # starts after a car has already lost some range, and the car's own data
    # alone can't distinguish that from a healthy pack; only spec can.
    readings = [mk(50 + (i % 40), 520) for i in range(50)]  # lots of history
    r = analyze(readings, new_range_km=549.0)
    assert r["reference"] == "factory spec"


def test_spec_overridden_when_implied_degradation_is_implausible():
    # A pack consistently projecting 35% below spec, with plenty of history
    # to trust it, more likely means the spec figure itself is wrong than a
    # normal Tesla pack having degraded that much — real data wins instead of
    # reporting an alarming (and probably wrong) degradation figure forever.
    readings = [mk(50 + (i % 40), 357) for i in range(40)]  # 357/549 ~= -35%
    r = analyze(readings, new_range_km=549.0)
    assert r["reference"] == "best seen"
    assert r["health_pct"] > 99   # own data treated as its own 100% baseline


def test_spec_not_overridden_without_enough_history():
    # The same implausible-vs-spec gap, but too few readings to trust it yet
    # — spec still anchors the estimate rather than an early, noisy reading
    # cluster overriding a documented figure.
    readings = [mk(50 + (i % 40), 357) for i in range(10)]
    r = analyze(readings, new_range_km=549.0)
    assert r["reference"] == "factory spec"


def test_new_range_lookup_by_badge():
    assert new_range_for("Model 3", "74D QUICKSILVER") == 549.0
    assert new_range_for("Model 3", "P74D") == 476.0
    assert new_range_for("Model Y", "74D") == 531.0
    assert new_range_for("Model 3", "") is None
    assert new_range_for("Tesla", "unknown") is None


def test_new_range_19in_nova_wheels():
    # 2024 Model 3 Highland LR AWD on 19" Nova wheels: EPA 305 mi = 491 km.
    assert new_range_for("Model 3", "74D QUICKSILVER Nova19") == 491.0
    assert new_range_for("Model 3", "74D Nova19DarkTinted") == 491.0
    # Any 19" wheel name counts — the diameter is what matters. Tesla reports
    # the Highland Nova 19" by its internal name "Helix19".
    assert new_range_for("Model 3", "74D Stiletto19", year=2024) == 491.0
    assert new_range_for("Model 3", "74D QUICKSILVER Helix19", year=2024) == 491.0
    # 18" Photon (or unknown wheels) keeps the 341 mi / 549 km figure.
    assert new_range_for("Model 3", "74D QUICKSILVER Photon18") == 549.0


def test_usable_capacity_lookup_by_variant():
    from app.analysis.battery import usable_capacity_for

    # LR / Performance Model 3 & Y share the 82 kWh gross / 75 kWh usable
    # pack; wheel size doesn't change the pack, so a 19" LR still reads 75.
    assert usable_capacity_for("Model 3", "74D QUICKSILVER Nova19") == 75.0
    assert usable_capacity_for("Model 3", "P74D") == 75.0
    assert usable_capacity_for("Model Y", "74D") == 75.0
    # Standard-range packs are smaller.
    assert usable_capacity_for("Model 3", "50") == 57.5
    assert usable_capacity_for("Model Y", "50") == 60.0
    # Unknown variant -> no guess.
    assert usable_capacity_for("Model 3", "") is None
    assert usable_capacity_for("Tesla", "unknown") is None


def test_usable_capacity_uses_spec_minus_degradation_as_the_primary_method():
    """The primary path is factory spec for the variant minus the car's own
    range-measured degradation (the same figure the Battery Health card
    shows) — not a charge-derived figure, so it's right immediately instead
    of waiting for many charges to converge, and it can't silently disagree
    with the degradation the app already displays."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from app.api.routes import _usable_capacity
    from app.database import SessionLocal
    from app.models import BatteryReading, Vehicle

    with SessionLocal() as s:
        v = Vehicle(vin="LRW3F7EK3RC000001", model="Model 3", trim="74D Nova19",
                    battery_capacity_kwh=75.0)
        s.add(v)
        s.flush()
        # ~7% range-based degradation vs the 491 km spec for this variant
        # (74D + 19" wheels): readings consistently project ~457 km.
        base = datetime(2026, 1, 1)
        for i in range(20):
            soc = 50 + (i % 40)
            s.add(BatteryReading(vehicle_id=v.id, ts=base + timedelta(hours=i),
                                  soc=soc, range_km=457.0 * soc / 100.0))
        s.commit()
        settings = SimpleNamespace(battery_capacity_kwh=0.0, battery_new_range_km=0.0)

        cap, source = _usable_capacity(s, v, settings)
        assert source == "spec - degradation"
        assert 68.5 <= cap <= 71.0   # 75 kWh spec x (1 - ~7%) ~= 69.75

        # An explicit config override still beats the computed figure.
        override = SimpleNamespace(battery_capacity_kwh=73.0, battery_new_range_km=0.0)
        assert _usable_capacity(s, v, override) == (73.0, "override")


def test_usable_capacity_falls_back_without_degradation_history():
    """A freshly-linked car has no battery-reading history yet: falls back to
    the measured charge EMA if it's moved off the default, else the spec."""
    from types import SimpleNamespace

    from app.api.routes import _usable_capacity
    from app.database import SessionLocal
    from app.models import Vehicle

    with SessionLocal() as s:
        v = Vehicle(vin="LRW3F7EK3RC000002", model="Model 3", trim="74D Nova19",
                    battery_capacity_kwh=75.0)
        s.add(v)
        s.commit()
        settings = SimpleNamespace(battery_capacity_kwh=0.0, battery_new_range_km=0.0)

        # Untouched default + no degradation data yet -> the spec (75).
        assert _usable_capacity(s, v, settings) == (75.0, "variant spec")

        # A measured EMA that has moved off the default is trusted instead.
        v.battery_capacity_kwh = 72.4
        assert _usable_capacity(s, v, settings) == (72.4, "measured")


def test_new_range_uses_vin_year_generation():
    # Same 74D badge, different generation: 2023 pre-Highland vs 2024 Highland.
    assert new_range_for("Model 3", "74D", year=2023) == 536.0
    assert new_range_for("Model 3", "74D", year=2024) == 549.0
    assert new_range_for("Model 3", "74D Nova19", year=2024) == 491.0
    # No year (no decodable VIN) falls back to the year-agnostic entries.
    assert new_range_for("Model 3", "74D") == 549.0


def test_vin_decode():
    from app.vin import decode

    info = decode("LRW3F7EK3RC309372")  # 2024 Model 3, Giga Shanghai
    assert info["model"] == "Model 3"
    assert info["year"] == 2024
    assert info["plant"] == "Shanghai"
    assert decode("DEMO12345") == {}
    assert decode("") == {}
