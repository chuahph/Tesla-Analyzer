"""Tests for the battery health estimator (app/analysis/battery.py)."""
import pytest
from datetime import datetime

from app.analysis.battery import analyze, new_range_for


@pytest.fixture(autouse=True)
def _db_ready():
    """Most tests here are pure functions, but a couple reach the database —
    and without this they pass only when an earlier test file happened to
    build the schema first, failing whenever this file runs alone."""
    from app.database import init_db

    init_db()
    yield


def mk(soc, full_range_km, odo_km=None, ts=None):
    """A reading of a pack whose true full range is ``full_range_km``."""
    r = {"soc": soc, "range_km": full_range_km * soc / 100.0}
    if odo_km is not None:
        r["odo_km"] = odo_km
    if ts is not None:
        r["ts"] = ts
    return r


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


def test_health_trend_groups_projections_by_month():
    """Readings with timestamps produce a monthly median-projection trend;
    months with fewer than 3 readings are too noisy to plot and are skipped."""
    from datetime import datetime

    def mk_ts(soc, full_range_km, when):
        r = mk(soc, full_range_km)
        r["ts"] = when
        return r

    readings = (
        [mk_ts(60, 500, datetime(2026, 1, 10 + i)) for i in range(5)]   # Jan: 500 km
        + [mk_ts(60, 490, datetime(2026, 2, 10 + i)) for i in range(5)]  # Feb: 490 km
        + [mk_ts(60, 480, datetime(2026, 3, 15))]                        # Mar: 1 reading only
    )
    r = analyze(readings)
    months = {p["month"]: p["full_range_km"] for p in r["trend"]}
    assert months == {"2026-01": 500.0, "2026-02": 490.0}   # Mar skipped

    # Readings without timestamps (e.g. the degradation helper) -> empty trend.
    r2 = analyze([mk(60, 500) for _ in range(10)])
    assert r2["trend"] == []


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


def test_fleet_degradation_curve_interpolates_and_extrapolates():
    from app.analysis.battery import fleet_degradation_pct

    assert fleet_degradation_pct(0) == 0.0
    assert fleet_degradation_pct(-500) == 0.0            # clamped, not negative
    assert fleet_degradation_pct(25_000) == 2.0           # exact table point
    # Halfway between the 50k (3.5%) and 100k (5.5%) points.
    assert abs(fleet_degradation_pct(75_000) - 4.5) < 1e-9
    # Past the last table point (400k, 11%): extrapolated using the last
    # segment's slope, not flatlined.
    beyond = fleet_degradation_pct(500_000)
    assert beyond > 11.0


def test_vs_fleet_benchmark_present_only_with_odometer_data():
    # No odo_km on any reading -> nothing to anchor the comparison to.
    no_odo = analyze([mk(50 + (i % 40), 500) for i in range(30)])
    assert no_odo["vs_fleet_pct"] is None
    assert no_odo["current_odo_km"] is None
    assert no_odo["fleet_degradation_pct"] is None

    # A pack degraded ~10% at 25,000 km (fleet typical: 2%) reads clearly
    # worse than typical, with a positive vs_fleet_pct (percentage points
    # worse than the fleet curve at the same mileage).
    old = [mk(60, 500, odo_km=20_000 + i * 100) for i in range(15)]
    new = [mk(60, 450, odo_km=24_000 + i * 10) for i in range(15)]
    worse = analyze(old + new)
    assert worse["current_odo_km"] == 24_140.0
    assert abs(worse["fleet_degradation_pct"] - 1.9) < 0.2   # fleet curve near 25k km
    assert worse["vs_fleet_pct"] > 5                # ~10% actual vs ~2% typical

    # A healthy pack at the same mileage reads at/below the fleet curve.
    healthy = analyze([mk(50 + (i % 40), 500, odo_km=24_000 + i * 10) for i in range(30)])
    assert healthy["vs_fleet_pct"] <= 0


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


# --- degradation forecast --------------------------------------------------

def test_forecast_projects_declining_trend_to_milestones():
    """A pack losing range month over month projects a finite horizon to the
    80% health milestone and 70% warranty floor."""
    readings = []
    # 6 months, full range sliding 500 -> ~475 km (a clear ~5% decline).
    for i in range(6):
        full = 500 - i * 5.0
        ts = datetime(2026, 1 + i, 15, 12, 0)
        readings += [mk(60, full, ts=ts) for _ in range(4)]  # >=3/month to plot
    r = analyze(readings, new_range_km=500)
    f = r["forecast"]
    assert f["available"] is True
    assert f["slope_km_per_year"] < 0            # losing range
    assert f["health_milestone_pct"] == 80
    assert f["warranty_floor_pct"] == 70
    # Current ~475 km, ref 500: 80% = 400 km is still years out at this rate.
    assert f["years_to_health_milestone"] > 0
    assert f["years_to_warranty_floor"] > f["years_to_health_milestone"]


def test_forecast_holds_off_on_a_steady_pack():
    """A pack whose range is flat within noise reports no forecast rather than
    a spurious 'centuries to 80%' number."""
    readings = []
    for i in range(6):
        ts = datetime(2026, 1 + i, 15, 12, 0)
        readings += [mk(60, 500, ts=ts) for _ in range(4)]
    r = analyze(readings, new_range_km=500)
    f = r["forecast"]
    assert f["available"] is False
    assert "no measurable decline" in f["note"].lower()


def test_forecast_unavailable_without_enough_months():
    """Too few monthly trend points -> no forecast, plain note."""
    readings = [mk(60, 500 - i, ts=datetime(2026, 1, 15)) for i in range(20)]
    r = analyze(readings, new_range_km=500)
    assert r["forecast"]["available"] is False


# --- Charge-derived capacity cross-check -----------------------------------

def _chg(start_soc, end_soc, kwh, charge_type="DC", cid=1):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=cid, start_time=datetime(2026, 7, 1, 8, 0), charge_type=charge_type,
        start_soc=start_soc, end_soc=end_soc, energy_added_kwh=kwh,
    )


def test_implied_capacity_inverts_a_charge_to_a_pack_size():
    """energy_added = (SoC gain / 100) x capacity, so a DC charge inverts
    straight back to the pack size with no efficiency correction."""
    from app.analysis.battery import implied_capacity

    # 30% gain, 20.0 kWh in -> 66.7 kWh usable.
    out = implied_capacity([_chg(40, 70, 20.0)])
    assert out["available"] is True
    assert out["count"] == 1
    assert out["median_kwh"] == 66.7


def test_implied_capacity_derates_ac_for_charging_losses():
    """The wall meter counts energy the onboard charger never gets into the
    pack, so an AC session overstates capacity without the correction."""
    from app.analysis.battery import implied_capacity

    dc = implied_capacity([_chg(40, 70, 20.0, "DC")])["median_kwh"]
    ac = implied_capacity([_chg(40, 70, 20.0, "AC")])["median_kwh"]
    assert ac < dc
    # 20.0 / 0.30 = 66.667, x0.95 = 63.333 -> 63.3. Rounded once at the end,
    # not applied to the already-rounded DC figure (which would give 63.4).
    assert ac == 63.3


def test_implied_capacity_ignores_gains_too_small_to_be_precise():
    """start/end SoC are whole percents, so a small gain carries a rounding
    error that swamps the answer — those samples say nothing and must not
    dilute the ones that do."""
    from app.analysis.battery import implied_capacity

    assert implied_capacity([_chg(60, 70, 6.7)])["available"] is False   # 10% gain
    assert implied_capacity([_chg(40, 70, 20.0)])["available"] is True   # 30% gain


def test_implied_capacity_drops_impossible_samples():
    """A mis-recorded session (an SoC reset mid-charge, a merged pair) is a
    data error, not a pack — it must be discarded rather than dragging the
    spread and making a real disagreement look like noise."""
    from app.analysis.battery import implied_capacity

    good = [_chg(40, 70, 20.0, cid=1), _chg(30, 80, 33.3, cid=2)]
    absurd = _chg(20, 90, 100.0, cid=3)          # implies ~143 kWh
    out = implied_capacity(good + [absurd])
    assert out["count"] == 2
    assert all(s["implied_kwh"] < 95 for s in out["samples"])


def test_implied_capacity_median_survives_one_wild_sample():
    """The headline is the median precisely so a single odd session can't
    move it — a mean would be dragged by exactly the sample least worth
    trusting."""
    from app.analysis.battery import implied_capacity

    charges = [_chg(40, 70, 20.0, cid=1),      # 66.7
               _chg(40, 70, 20.1, cid=2),      # 67.0
               _chg(20, 95, 35.0, cid=3)]      # 46.7 — an outlier, still plausible
    out = implied_capacity(charges)
    assert out["median_kwh"] == 66.7           # unmoved by the outlier
    assert out["spread_kwh"] == round(67.0 - 46.7, 1)   # but the scatter shows it


def test_implied_capacity_reports_nothing_without_qualifying_charges():
    from app.analysis.battery import implied_capacity

    assert implied_capacity([])["available"] is False
    assert implied_capacity([])["count"] == 0


# --- Capacity measured from the charging curve ------------------------------

def _curve(start_soc, end_soc, capacity_kwh, step=2, ac=True):
    """A clean session's (SoC, kWh-added) samples for a given pack."""
    eff = 0.95 if ac else 1.0
    return [[s, (s - start_soc) / 100.0 * capacity_kwh / eff]
            for s in range(start_soc, end_soc + 1, step)]


def test_capacity_from_curve_recovers_the_pack_from_the_slope():
    """energy_added = SoC/100 x capacity holds throughout a session, so the
    slope through its samples IS the pack size."""
    from app.analysis.battery import capacity_from_curve

    fit = capacity_from_curve(_curve(30, 75, 69.5), "AC")
    assert fit["kwh"] == 69.5
    assert fit["samples"] == 23
    assert fit["soc_span_pct"] == 44.0     # 30..74 in steps of 2


def test_capacity_from_curve_beats_the_endpoints_on_a_short_charge():
    """The point of fitting the curve: a small gain is hopeless from the two
    ends, because whole-percent SoC at each is a large share of it, but a
    slope through the samples is barely troubled."""
    from app.analysis.battery import CAPACITY_MIN_GAIN_PCT, capacity_from_curve

    # A 12-point charge — below the gain the endpoint method insists on.
    assert 12 < CAPACITY_MIN_GAIN_PCT + 1
    fit = capacity_from_curve(_curve(50, 62, 69.5, step=1), "AC")
    assert fit is not None
    assert fit["kwh"] == 69.5


def test_capacity_from_curve_refuses_a_session_that_is_not_a_line():
    """A BMS recalibration or a paused-and-resumed session breaks the
    proportionality the whole method rests on — better to say nothing."""
    from app.analysis.battery import capacity_from_curve

    broken = _curve(30, 75, 69.5)
    broken[12][1] += 4.0          # a step change mid-session
    assert capacity_from_curve(broken, "AC") is None


def test_capacity_from_curve_needs_samples_and_spread():
    from app.analysis.battery import capacity_from_curve

    assert capacity_from_curve(_curve(30, 75, 69.5)[:3], "AC") is None   # too few
    assert capacity_from_curve(                                          # no spread
        [[50, 1.0], [50.5, 1.3], [51, 1.6], [51.5, 1.9], [52, 2.2], [52.5, 2.5]], "DC") is None


def test_implied_capacity_prefers_the_curve_figure_over_the_endpoints():
    """A curve-fitted session is used on its own terms — including when its
    SoC gain is below what the endpoint method would require."""
    from types import SimpleNamespace

    from app.analysis.battery import implied_capacity

    fitted = SimpleNamespace(
        id=1, start_time=datetime(2026, 8, 1, 8, 0), charge_type="AC",
        start_soc=50, end_soc=62, energy_added_kwh=8.8,     # only a 12% gain
        implied_capacity_kwh=67.8, capacity_samples=13,
    )
    out = implied_capacity([fitted])
    assert out["available"] is True
    assert out["median_kwh"] == 67.8
    assert out["samples"][0]["method"] == "curve"
    assert out["samples"][0]["curve_samples"] == 13
