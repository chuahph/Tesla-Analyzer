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


def test_haversine_km():
    from app.analysis import haversine_km

    # 0.1 deg latitude ~= 11.1 km at any longitude.
    d = haversine_km("5.30, 100.30", "5.40, 100.30")
    assert 10.9 <= d <= 11.3
    # Same point -> zero.
    assert haversine_km("5.30, 100.30", "5.30, 100.30") == 0.0
    # Malformed / missing input -> None, not a crash.
    assert haversine_km("", "5.30, 100.30") is None
    assert haversine_km("not-coords", "5.30, 100.30") is None


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


def test_charging_locations_sorted_latest_first():
    from datetime import datetime

    from app.analysis.charging import analyze
    from app.models import Charge

    def chg(day, place):
        return Charge(start_time=datetime(2026, 7, day, 10, 0),
                      end_time=datetime(2026, 7, day, 11, 0), duration_min=60,
                      start_soc=40, end_soc=70, energy_added_kwh=20, charge_type="AC",
                      max_power_kw=11, location=place, cost=18, outside_temp_c=30)
    charges = [chg(1, "Home"), chg(5, "Office"), chg(3, "Mall")]
    names = [row[0] for row in analyze(charges)["top_locations"]]
    assert names == ["Office · AC", "Mall · AC", "Home · AC"]  # 5 Jul, 3 Jul, 1 Jul


def test_recent_charges_sorted_latest_first_with_rate_and_free_flag():
    from datetime import datetime

    from app.analysis.charging import analyze
    from app.models import Charge

    older = Charge(
        start_time=datetime(2026, 7, 1, 10, 0), end_time=datetime(2026, 7, 1, 11, 0),
        duration_min=60, start_soc=40, end_soc=70, energy_added_kwh=20.0,
        charge_type="AC", max_power_kw=11, location="Home", cost=18.0, outside_temp_c=30,
    )
    older.id = 1
    newer_free = Charge(
        start_time=datetime(2026, 7, 5, 10, 0), end_time=datetime(2026, 7, 5, 11, 0),
        duration_min=60, start_soc=40, end_soc=70, energy_added_kwh=10.0,
        charge_type="AC", max_power_kw=11, location="Hotel", cost=0.0, outside_temp_c=30,
        is_free=True,
    )
    newer_free.id = 2

    charges = analyze([older, newer_free])["recent_charges"]
    assert [c["id"] for c in charges] == [2, 1]   # latest first
    assert charges[0]["is_free"] is True
    assert charges[0]["cost"] == 0.0
    assert charges[1]["is_free"] is False
    assert charges[1]["rate_per_kwh"] == round(18.0 / 20.0, 3)


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
    # [name, count, kWh, last_time] — place + type + energy + most recent charge.
    row = r["top_locations"][0]
    assert row[:3] == ["Juru · DC", 1, 18.0]
    assert row[3] == "2026-07-04T16:20"                # sequence timestamp
    # Without any nearby drive it falls back to the charger type.
    assert analyze([charge], [])["top_locations"][0][:3] == ["DC fast charger", 1, 18.0]
    # A real named place with a comma is kept (not mistaken for coordinates).
    charge.location = "Bayan Mutiara, George Town"
    assert analyze([charge], [])["top_locations"][0][:3] == ["Bayan Mutiara, George Town · DC", 1, 18.0]


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


def test_efficiency_by_hour_is_distance_weighted_with_gaps_for_quiet_hours():
    """Trips-by-hour's efficiency overlay: distance-weighted Wh/km per hour,
    None (not 0) for hours with no energy-bearing trip."""
    from datetime import datetime

    from app.analysis.driving import analyze
    from app.models import Drive

    def d(hour, dist, kwh):
        return Drive(start_time=datetime(2026, 7, 4, hour, 0),
                     end_time=datetime(2026, 7, 4, hour, 10),
                     distance_km=dist, duration_min=10.0, avg_speed_kmh=30,
                     max_speed_kmh=45, start_soc=80, end_soc=79,
                     energy_used_kwh=kwh, outside_temp_c=30.0)
    # Two trips both starting at hour 8: distance-weighted, not a plain mean
    # of the two per-trip ratios (150 and 200 Wh/km would average to 175;
    # distance-weighted over 1+4 km gives 190).
    drives = [d(8, 1.0, 0.15), d(8, 4.0, 0.8)]
    r = analyze(drives, 150.0, 75.0)
    eh = r["efficiency_by_hour"]
    assert eh["8"] == round((0.15 + 0.8) * 1000.0 / 5.0, 1)
    # Every other hour has no trips at all -> None, not 0.
    assert eh["0"] is None
    assert eh["23"] is None


def test_total_energy_used_includes_parking_drain():
    """The 'kWh used' headline reflects gross battery drain (parking/idle/
    overnight), not just the driving energy summed per trip."""
    from datetime import datetime

    from app.analysis.driving import analyze
    from app.models import Drive

    # Two short drives that together only *drove* ~0.8 kWh, but the battery fell
    # 80% -> 70% over the window (10% of a 75 kWh pack = 7.5 kWh) — the extra
    # came from a long overnight park between them. km_per_soc / gross energy
    # must capture the whole 7.5 kWh; per-trip efficiency stays driving-only.
    def d(hour, dist, ssoc, esoc, kwh):
        return Drive(start_time=datetime(2026, 7, 4, hour, 0),
                     end_time=datetime(2026, 7, 4, hour, 10),
                     distance_km=dist, duration_min=10.0, avg_speed_kmh=30,
                     max_speed_kmh=45, start_soc=ssoc, end_soc=esoc,
                     energy_used_kwh=kwh, outside_temp_c=30.0)
    drives = [d(8, 3.0, 80, 79, 0.4), d(20, 3.0, 72, 70, 0.4)]  # 8% drained parked
    r = analyze(drives, 150.0, 75.0)
    # Driving-only sum is ~0.8 kWh; gross drain is 10% of 75 = 7.5 kWh.
    assert r["total_energy_kwh"] == 0.8
    assert r["total_energy_used_kwh"] == 7.5
    # Efficiency (per-trip model) untouched by the parking drain.
    assert r["avg_efficiency_wh_per_km"] == round(0.8 * 1000.0 / 6.0, 1)
    # trip_energy_used_kwh + vampire_drain.kwh always sums back to the total
    # exactly — this window's whole 7% between-drive gap (79% -> 72%) is
    # vampire; the within-drive 1%+2% (worth 2.25 kWh at 75 kWh pack, more
    # than the drives' own under-measured 0.8 kWh) floors the trip side.
    assert r["vampire_drain"]["kwh"] == 5.2   # round(5.25, 1) -> 5.2 (banker's rounding)
    assert r["trip_energy_used_kwh"] == 2.3   # derived as total - vampire, not independently rounded
    assert round(r["trip_energy_used_kwh"] + r["vampire_drain"]["kwh"], 1) == r["total_energy_used_kwh"]
    assert r["vampire_drain"]["gaps"] == 1
    assert r["vampire_drain"]["hours"] == 11.8   # 8:10 -> 20:00


def test_total_battery_used_measures_each_trip_at_its_best_precision():
    """total_energy_used_kwh sums each trip's OWN best measurement (fractional
    range energy, or the integer SoC drop when a range gap logged ~0 kWh) —
    not a window-level max(sum_frac, sum_int), which drops a data-gap trip's
    real drain whenever another trip's fractional energy is the larger sum."""
    from datetime import datetime

    from app.analysis.driving import analyze
    from app.models import Drive

    def d(hour, ssoc, esoc, kwh):
        return Drive(start_time=datetime(2026, 7, 4, hour, 0),
                     end_time=datetime(2026, 7, 4, hour, 10),
                     distance_km=20.0, duration_min=10.0, avg_speed_kmh=40,
                     max_speed_kmh=60, start_soc=ssoc, end_soc=esoc,
                     energy_used_kwh=kwh, outside_temp_c=25.0)

    # Trip A best by its measured energy (5.0 kWh > its 1% = 0.75 kWh drop);
    # Trip B is a range-gap trip (0 kWh logged) that plainly dropped 5% =
    # 3.75 kWh. True total drawn = 5.0 + 3.75 = 8.75 kWh. A window-level
    # max(sum_frac=5.0, sum_int=4.5) would report only 5.0, losing trip B's
    # whole 3.75 kWh.
    tA = d(8, 80, 79, 5.0)
    tB = d(10, 79, 74, 0.0)
    r = analyze([tA, tB], 150.0, 75.0)
    assert r["total_energy_kwh"] == 5.0                     # driving-only sum unchanged
    assert r["total_energy_used_kwh"] == round(5.0 + 3.75, 1)   # 8.8
    assert r["vampire_drain"]["kwh"] == 0.0                 # the 10:00 gap qualifies (1h50m) but SoC didn't move (79 -> 79)
    # Invariant: trip + vampire always reconstructs the headline total.
    assert round(r["trip_energy_used_kwh"] + r["vampire_drain"]["kwh"], 1) == r["total_energy_used_kwh"]


def test_vampire_drain_function_thresholds_and_excludes_charged_gaps():
    """vampire_drain() in isolation: a short gap (below the threshold) still
    contributes its measured kWh to the total (any real drain counts) but
    doesn't count toward the "parked gaps/hours" narrative, which is
    reserved for genuine idle stretches; a gap with a charge inside it isn't
    a pure drain measurement and is skipped entirely — from both kWh and the
    narrative — rather than netted against the charge."""
    from datetime import datetime

    from app.analysis.driving import VAMPIRE_MIN_GAP_HOURS, vampire_drain
    from app.models import Charge, Drive

    def d(start, end, ssoc, esoc):
        return Drive(id=None, start_time=start, end_time=end, distance_km=3.0,
                     duration_min=10.0, avg_speed_kmh=30, max_speed_kmh=45,
                     start_soc=ssoc, end_soc=esoc, energy_used_kwh=0.0, outside_temp_c=28.0)

    assert VAMPIRE_MIN_GAP_HOURS == 1.0

    # A gap just under the threshold: real SoC drop, short enough to read as
    # a normal errand stop rather than genuine parked/idle time — so it
    # doesn't count toward gaps/hours, but its kWh still counts toward the
    # total (excluding it there would make trip + vampire kWh undercount
    # true battery used).
    short = [
        d(datetime(2026, 7, 4, 8, 0), datetime(2026, 7, 4, 8, 10), 80, 80),
        d(datetime(2026, 7, 4, 8, 55), datetime(2026, 7, 4, 9, 5), 79, 79),
    ]
    r = vampire_drain(short, [], 75.0)
    assert r == {"kwh": round(1 / 100.0 * 75.0, 2), "hours": 0.0, "gaps": 0,
                 "gap_list": [], "longest": None}

    # Same gap, now long enough (3h) — counts.
    long_gap = [
        d(datetime(2026, 7, 4, 8, 0), datetime(2026, 7, 4, 8, 10), 80, 80),
        d(datetime(2026, 7, 4, 11, 10), datetime(2026, 7, 4, 11, 20), 79, 79),
    ]
    r2 = vampire_drain(long_gap, [], 75.0)
    assert r2["gaps"] == 1
    assert r2["kwh"] == round(1 / 100.0 * 75.0, 2)
    assert r2["hours"] == 3.0
    assert r2["gap_list"][0]["start"] == "2026-07-04T08:10"
    assert r2["gap_list"][0]["end"] == "2026-07-04T11:10"
    assert r2["longest"] == {"hours": 3.0, "start": "2026-07-04T08:10", "end": "2026-07-04T11:10"}

    # A charge starting inside that same gap invalidates it as a pure-drain
    # measurement — excluded outright, not netted against the charge.
    mid_gap_charge = Charge(
        start_time=datetime(2026, 7, 4, 9, 0), end_time=datetime(2026, 7, 4, 9, 30),
        duration_min=30.0, start_soc=79, end_soc=85, energy_added_kwh=4.5,
        charge_type="AC", max_power_kw=7.0, cost=4.5,
    )
    r3 = vampire_drain(long_gap, [mid_gap_charge], 75.0)
    assert r3 == {"kwh": 0.0, "hours": 0.0, "gaps": 0, "gap_list": [], "longest": None}


def test_vampire_drain_longest_picks_the_biggest_gap_not_the_last():
    """``longest`` is the single biggest qualifying gap regardless of its
    position in the list — a real "I was away" stretch should stand out
    from ordinary daily gaps even when it isn't the most recent one."""
    from datetime import datetime

    from app.analysis.driving import vampire_drain
    from app.models import Drive

    def d(start, end, ssoc, esoc):
        return Drive(id=None, start_time=start, end_time=end, distance_km=3.0,
                     duration_min=10.0, avg_speed_kmh=30, max_speed_kmh=45,
                     start_soc=ssoc, end_soc=esoc, energy_used_kwh=0.0, outside_temp_c=28.0)

    drives = [
        d(datetime(2026, 7, 1, 8, 0), datetime(2026, 7, 1, 8, 10), 90, 89),
        # 3h gap.
        d(datetime(2026, 7, 1, 11, 10), datetime(2026, 7, 1, 11, 20), 88, 87),
        # 3-day gap in the middle — the real "away" stretch.
        d(datetime(2026, 7, 4, 11, 20), datetime(2026, 7, 4, 11, 30), 80, 79),
        # 2h gap after it.
        d(datetime(2026, 7, 4, 13, 30), datetime(2026, 7, 4, 13, 40), 78, 77),
    ]
    r = vampire_drain(drives, [], 75.0)
    assert r["gaps"] == 3
    assert r["longest"]["hours"] == 72.0
    assert r["longest"]["start"] == "2026-07-01T11:20"
    assert r["longest"]["end"] == "2026-07-04T11:20"


def test_vampire_drain_counts_hours_even_with_zero_measured_drop():
    """A qualifying (2h+, charge-free) gap counts toward gaps/hours even if
    SoC happened to read unchanged — SoC is only integer precision, so a
    real sub-1% loss over a few hours plausibly never crosses a whole point.
    Reported by a user whose real standby drain is ~0.3-0.4%/day: over a
    14h gap that's under half a percent, so it very likely wouldn't move
    the integer SoC reading at all — excluding the gap outright (the old
    behaviour) would keep silently undercounting "hours parked" for
    exactly this kind of low-drain car."""
    from datetime import datetime

    from app.analysis.driving import vampire_drain
    from app.models import Drive

    def d(start, end, ssoc, esoc):
        return Drive(id=None, start_time=start, end_time=end, distance_km=3.0,
                     duration_min=10.0, avg_speed_kmh=30, max_speed_kmh=45,
                     start_soc=ssoc, end_soc=esoc, energy_used_kwh=0.0, outside_temp_c=28.0)

    zero_drop = [
        d(datetime(2026, 7, 3, 20, 14), datetime(2026, 7, 3, 20, 14), 80, 80),
        # 14h16m later, same SoC — no measurable drop, but still parked.
        d(datetime(2026, 7, 4, 10, 30), datetime(2026, 7, 4, 11, 9), 80, 78),
    ]
    r = vampire_drain(zero_drop, [], 75.0)
    assert r["gaps"] == 1
    assert r["hours"] == round((14 * 60 + 16) / 60.0, 1)
    assert r["kwh"] == 0.0  # no measured drop, so no kWh attributed — but the gap still counts


def test_vampire_drain_anchor_measures_gap_before_first_drive():
    """Without an anchor, the gap before drives[0] is invisible (nothing
    earlier in the list to pair it with) — exactly the real scenario a user
    reported: last charge ended Fri 20:14, first drive since was Sat
    10:30-11:09, a charge-free ~14h16m overnight gap that a "since charge"
    window should count as vampire drain but silently didn't. Passing
    anchor=(charge_end_time, charge_end_soc) fixes it by giving that gap a
    "before" boundary to measure against, same as any other gap."""
    from datetime import datetime

    from app.analysis.driving import vampire_drain
    from app.models import Drive

    first_drive = [Drive(
        id=1, start_time=datetime(2026, 7, 4, 10, 30), end_time=datetime(2026, 7, 4, 11, 9),
        distance_km=20.0, duration_min=39.0, avg_speed_kmh=30, max_speed_kmh=60,
        start_soc=78, end_soc=74, energy_used_kwh=3.0, outside_temp_c=28.0,
    )]

    # No anchor: a single drive with nothing before it in the list — no gap
    # to measure at all, even though it was clearly preceded by ~14h parked.
    r_no_anchor = vampire_drain(first_drive, [], 75.0)
    assert r_no_anchor == {"kwh": 0.0, "hours": 0.0, "gaps": 0, "gap_list": [], "longest": None}

    # With the last charge's end as an anchor, that same ~14h16m gap (charge
    # ended Fri 20:14, drive started Sat 10:30) is now measured.
    charge_end = datetime(2026, 7, 3, 20, 14)
    r_anchored = vampire_drain(first_drive, [], 75.0, anchor=(charge_end, 80.0))
    assert r_anchored["gaps"] == 1
    assert r_anchored["hours"] == round((first_drive[0].start_time - charge_end).total_seconds() / 3600.0, 1)
    assert r_anchored["kwh"] == round(2 / 100.0 * 75.0, 2)  # 80% -> 78% = 2 points lost
    assert r_anchored["gap_list"][0]["before_drive_id"] == 1


def test_recent_trips_vampire_before_annotation():
    """Each of the (up to 5) most recent trips carries the qualifying parked
    gap that preceded it, if any — None for the very first drive in the
    window (nothing before it to measure) and for a trip that followed
    quickly (no qualifying gap)."""
    from datetime import datetime

    from app.analysis.driving import analyze
    from app.models import Drive

    def d(start, end, ssoc, esoc):
        return Drive(id=hash((start, end)) % 100000 + 1, start_time=start, end_time=end,
                     distance_km=3.0, duration_min=10.0, avg_speed_kmh=30, max_speed_kmh=45,
                     start_soc=ssoc, end_soc=esoc, energy_used_kwh=0.3, outside_temp_c=28.0)

    drives = [
        d(datetime(2026, 7, 4, 8, 0), datetime(2026, 7, 4, 8, 10), 80, 79),
        # 5h parked gap, 2% lost — qualifies.
        d(datetime(2026, 7, 4, 13, 10), datetime(2026, 7, 4, 13, 20), 77, 76),
        # Only 20 min later — no qualifying gap.
        d(datetime(2026, 7, 4, 13, 40), datetime(2026, 7, 4, 13, 50), 75, 74),
    ]
    r = analyze(drives, 150.0, 75.0)
    trips = {t["id"]: t for t in r["recent_trips"]}
    assert trips[drives[0].id]["vampire_before"] is None
    vb = trips[drives[1].id]["vampire_before"]
    assert vb is not None
    assert vb["hours"] == 5.0
    assert vb["pct"] == 2.0
    assert trips[drives[2].id]["vampire_before"] is None


def test_recent_trips_limit_defaults_to_5_but_none_means_uncapped():
    """Reported live: a "since charge" window with more than 5 drives since
    the last charge only ever showed the 5 most recent — every earlier trip
    that charge cycle silently vanished from Recent Trips, even though the
    window's own aggregate KPIs (Distance, Battery Used, ...) correctly
    covered all of them. recent_trips_limit=None lists every drive instead,
    for callers (a since-charge window) whose own natural bound already
    keeps the list reasonable; the default (5) is unchanged for callers
    that don't have such a bound (a plain day-count window)."""
    from datetime import datetime, timedelta

    from app.analysis.driving import analyze
    from app.models import Drive

    def d(i):
        start = datetime(2026, 7, 4, 8, 0) + timedelta(hours=i)
        return Drive(id=i + 1, start_time=start, end_time=start + timedelta(minutes=10),
                     distance_km=3.0, duration_min=10.0, avg_speed_kmh=30, max_speed_kmh=45,
                     start_soc=80 - i, end_soc=79 - i, energy_used_kwh=0.3, outside_temp_c=28.0)

    drives = [d(i) for i in range(7)]

    default = analyze(drives, 150.0, 75.0)
    assert len(default["recent_trips"]) == 5
    assert [t["id"] for t in default["recent_trips"]] == [7, 6, 5, 4, 3]  # most recent first

    uncapped = analyze(drives, 150.0, 75.0, recent_trips_limit=None)
    assert len(uncapped["recent_trips"]) == 7
    assert [t["id"] for t in uncapped["recent_trips"]] == [7, 6, 5, 4, 3, 2, 1]


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


def test_confirmed_zero_idle_is_trusted_not_re_estimated():
    """A trip with real live idle-tracking (idle_tracked=True) that measured
    genuinely zero sustained stops must show driving_wh_per_km == the raw
    figure — not silently re-estimated by the old avg/max-speed heuristic,
    which would guess idle time from the speed spread alone and produce a
    lower, wrong number despite having the real (zero) measurement in hand.

    Real-world case: 8.0 km / 25.5 min, avg 18.9 avg / max 74 km/h (heavy
    stop-go traffic with many short lights, none reaching the 3-min
    threshold), 1.52 kWh -> 190 Wh/km. Before idle_tracked existed, this
    silently fell back to the heuristic and showed ~144 instead of 190.
    """
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    tracked = Drive(
        start_time=datetime(2026, 7, 8, 19, 5), end_time=datetime(2026, 7, 8, 19, 30),
        distance_km=8.0, duration_min=25.5, avg_speed_kmh=18.9, max_speed_kmh=74.0,
        start_soc=44, end_soc=42, energy_used_kwh=1.52, outside_temp_c=31.0,
        idle_min=0.0, idle_tracked=True,
    )
    result = driving_analysis.analyze([tracked], 150.0, 75.0)
    trip = result["recent_trips"][0]
    assert trip["wh_per_km"] == 190
    assert trip["driving_wh_per_km"] == 190  # trusted zero, not re-estimated to ~144

    # An otherwise-identical *untracked* trip (idle_tracked=False, e.g. logged
    # before this feature or reconstructed across a gap) must still fall back
    # to the old heuristic, which does infer idle from the speed spread here.
    untracked = Drive(
        start_time=datetime(2026, 7, 8, 19, 5), end_time=datetime(2026, 7, 8, 19, 30),
        distance_km=8.0, duration_min=25.5, avg_speed_kmh=18.9, max_speed_kmh=74.0,
        start_soc=44, end_soc=42, energy_used_kwh=1.52, outside_temp_c=31.0,
        idle_min=0.0, idle_tracked=False,
    )
    result2 = driving_analysis.analyze([untracked], 150.0, 75.0)
    trip2 = result2["recent_trips"][0]
    assert trip2["wh_per_km"] == 190
    assert trip2["driving_wh_per_km"] < 190   # heuristic still applies here


def test_driving_cost_and_map_links():
    """With a tariff configured, the window reports total driving cost and
    cost/km (from gross energy used), each trip reports its own cost, and a
    trip with stored raw coords links out to Google Maps directions."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    trip = Drive(
        start_time=datetime(2026, 7, 9, 8, 0), end_time=datetime(2026, 7, 9, 8, 20),
        distance_km=10.0, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
        start_soc=60, end_soc=58, energy_used_kwh=1.5, outside_temp_c=28.0,
        start_coords="5.3312, 100.3060", end_coords="5.3500, 100.2800",
    )
    r = driving_analysis.analyze([trip], 150.0, 75.0, energy_price=0.90)
    row = r["recent_trips"][0]
    assert row["cost"] == round(1.5 * 0.90, 2)
    assert r["total_cost"] == round(r["total_energy_used_kwh"] * 0.90, 2)
    assert r["cost_per_km"] == round(r["total_energy_used_kwh"] * 0.90 / 10.0, 3)
    assert row["map_url"].startswith("https://www.google.com/maps/dir/?api=1")
    assert "origin=5.3312,100.3060" in row["map_url"]
    assert "destination=5.3500,100.2800" in row["map_url"]

    # No tariff -> no cost figures; no coords -> no map link.
    bare = Drive(
        start_time=datetime(2026, 7, 9, 9, 0), end_time=datetime(2026, 7, 9, 9, 20),
        distance_km=10.0, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
        start_soc=60, end_soc=58, energy_used_kwh=1.5, outside_temp_c=28.0,
        start_coords="", end_coords="",
    )
    r2 = driving_analysis.analyze([bare], 150.0, 75.0)
    assert r2["total_cost"] is None
    assert r2["recent_trips"][0]["cost"] is None
    assert r2["recent_trips"][0]["map_url"] is None


def test_by_tag_totals_and_per_trip_tag():
    """Tagged trips roll up into a per-tag distance/energy/cost breakdown;
    an all-untagged window reports by_tag as None (nothing to show)."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def trip(day, tag, distance=10.0, energy=1.5):
        return Drive(
            start_time=datetime(2026, 7, day, 8, 0), end_time=datetime(2026, 7, day, 8, 20),
            distance_km=distance, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
            start_soc=60, end_soc=58, energy_used_kwh=energy, outside_temp_c=28.0, tag=tag,
        )

    work = trip(1, "work")
    personal = trip(2, "personal", distance=5.0, energy=0.8)
    untagged = trip(3, "")

    r = driving_analysis.analyze([work, personal, untagged], 150.0, 75.0, energy_price=0.90)
    assert r["recent_trips"][0]["tag"] in ("work", "personal", "")   # present on every trip
    by_tag = {t["tag"] for t in r["recent_trips"]}
    assert by_tag == {"work", "personal", ""}

    assert r["by_tag"]["work"]["distance_km"] == 10.0
    assert r["by_tag"]["work"]["cost"] == round(1.5 * 0.90, 2)
    assert r["by_tag"]["personal"]["distance_km"] == 5.0
    assert r["by_tag"]["untagged"]["distance_km"] == 10.0

    # Nothing tagged -> by_tag stays None (no card worth showing).
    r2 = driving_analysis.analyze([untagged], 150.0, 75.0, energy_price=0.90)
    assert r2["by_tag"] is None


def test_driving_cost_accepts_time_of_use_price_function():
    """When energy_price is a callable (TOU pricing), each trip is priced at
    its own start_time's rate, and the window total blends those rates by
    energy — not a single flat number applied everywhere."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def price_at(dt):
        return 1.20 if 8 <= dt.hour < 22 else 0.45   # peak / off-peak

    peak_trip = Drive(
        start_time=datetime(2026, 7, 9, 14, 0), end_time=datetime(2026, 7, 9, 14, 20),
        distance_km=10.0, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
        start_soc=60, end_soc=58, energy_used_kwh=1.5, outside_temp_c=28.0,
    )
    night_trip = Drive(
        start_time=datetime(2026, 7, 9, 23, 0), end_time=datetime(2026, 7, 9, 23, 20),
        distance_km=10.0, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
        start_soc=60, end_soc=58, energy_used_kwh=1.5, outside_temp_c=28.0,
    )
    r = driving_analysis.analyze([peak_trip], 150.0, 75.0, energy_price=price_at)
    assert r["recent_trips"][0]["cost"] == round(1.5 * 1.20, 2)
    r2 = driving_analysis.analyze([night_trip], 150.0, 75.0, energy_price=price_at)
    assert r2["recent_trips"][0]["cost"] == round(1.5 * 0.45, 2)

    # Mixed window: the blended rate sits between peak and off-peak, not at
    # either extreme, and matches the actual weighted cost.
    r3 = driving_analysis.analyze([peak_trip, night_trip], 150.0, 75.0, energy_price=price_at)
    assert 0.45 < r3["total_cost"] / r3["total_energy_used_kwh"] < 1.20


def test_insights_report_material_patterns_only():
    """Peak-hour drives consistently 25% worse than off-peak (3+ each side)
    produce an insight; too few drives or immaterial differences stay silent."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def trip(day, hour, whkm):
        km = 10.0
        return Drive(
            start_time=datetime(2026, 7, day, hour, 0), end_time=datetime(2026, 7, day, hour, 30),
            distance_km=km, duration_min=30.0, avg_speed_kmh=20.0, max_speed_kmh=60.0,
            start_soc=60, end_soc=58, energy_used_kwh=km * whkm / 1000.0, outside_temp_c=28.0,
        )

    peak = [trip(d, 8, 190) for d in range(1, 5)]       # 4 peak drives, 190 Wh/km
    off = [trip(d, 21, 140) for d in range(1, 5)]       # 4 off-peak, 140 Wh/km
    r = driving_analysis.analyze(peak + off, 150.0, 75.0)
    assert any("peak-hour" in s.lower() for s in r["insights"])

    # Same split but a trivial 3% difference — no insight.
    quiet = [trip(d, 8, 145) for d in range(1, 5)] + [trip(d, 21, 141) for d in range(1, 5)]
    r2 = driving_analysis.analyze(quiet, 150.0, 75.0)
    assert not any("peak-hour" in s.lower() for s in r2["insights"])


def test_charging_cost_split_and_per_100km(seeded):
    charges = seeded.scalars(select(Charge)).all()
    drives = seeded.scalars(select(Drive)).all()
    r = charging_analysis.analyze(charges, drives)
    assert round(r["ac_cost"] + r["dc_cost"], 2) == r["total_cost"]
    km = sum(d.distance_km for d in drives)
    assert r["cost_per_100km"] == round(r["total_cost"] / km * 100.0, 2)


def test_recent_trips_report_data_quality():
    """measured (real tracked idle) / estimated (heuristic fallback) /
    incomplete (no valid energy) reflects how much to trust each trip."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def trip(**overrides):
        base = dict(
            start_time=datetime(2026, 7, 9, 8, 0), end_time=datetime(2026, 7, 9, 8, 20),
            distance_km=10.0, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
            start_soc=60, end_soc=58, energy_used_kwh=1.5, outside_temp_c=28.0,
        )
        base.update(overrides)
        return Drive(**base)

    measured = trip(idle_min=0.0, idle_tracked=True)
    estimated = trip(idle_min=0.0, idle_tracked=False)
    incomplete = trip(energy_used_kwh=0.0)   # no valid energy -> wh_per_km 0

    assert driving_analysis.analyze([measured], 150.0, 75.0)["recent_trips"][0]["data_quality"] == "measured"
    assert driving_analysis.analyze([estimated], 150.0, 75.0)["recent_trips"][0]["data_quality"] == "estimated"
    assert driving_analysis.analyze([incomplete], 150.0, 75.0)["recent_trips"][0]["data_quality"] == "incomplete"


def test_distance_flag_catches_implausibly_short_odometer_distance():
    """A trip whose logged distance is shorter than the straight-line
    distance between its own stored endpoints is flagged — physically that
    driven distance can never be shorter than a straight line. Trips with no
    stored coords, or with a sane distance, are left unflagged."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def trip(distance_km, start_coords, end_coords):
        return Drive(
            start_time=datetime(2026, 7, 9, 8, 0), end_time=datetime(2026, 7, 9, 8, 20),
            distance_km=distance_km, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
            start_soc=60, end_soc=58, energy_used_kwh=1.5, outside_temp_c=28.0,
            start_coords=start_coords, end_coords=end_coords,
        )

    # ~11 km straight-line between these two points (0.1 deg lat ~= 11.1 km).
    flagged = trip(2.0, "5.30, 100.30", "5.40, 100.30")   # 2 km logged, impossible
    sane = trip(15.0, "5.30, 100.30", "5.40, 100.30")     # 15 km logged, plausible
    no_coords = trip(2.0, "", "")

    r = driving_analysis.analyze([flagged], 150.0, 75.0)["recent_trips"][0]
    assert r["distance_flag"] == "distance_short"
    assert driving_analysis.analyze([sane], 150.0, 75.0)["recent_trips"][0]["distance_flag"] is None
    assert driving_analysis.analyze([no_coords], 150.0, 75.0)["recent_trips"][0]["distance_flag"] is None


def test_top_routes_group_by_area_but_show_specific_label():
    """Repeat trips to 'the same place' shouldn't fragment into many
    single-count Top Routes entries just because the exact matched POI/
    building differs a few metres apart between visits — grouped by the
    coarser area, but displaying the most common specific label seen."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def trip(day, start_loc, end_loc, start_area, end_area):
        return Drive(
            start_time=datetime(2026, 7, day, 8, 0), end_time=datetime(2026, 7, day, 8, 20),
            distance_km=5.0, duration_min=20.0, avg_speed_kmh=15.0, max_speed_kmh=40.0,
            start_soc=60, end_soc=58, energy_used_kwh=0.7, outside_temp_c=25.0,
            start_location=start_loc, end_location=end_loc,
            start_area=start_area, end_area=end_area,
        )

    drives = [
        # Three visits to "the mall": the exact POI label wobbles between
        # trips (GPS jitter matches a slightly different unit/entrance), but
        # the area stays the same suburb every time.
        trip(1, "Home, George Town", "Queensbay Mall, Bayan Lepas", "George Town", "Bayan Lepas"),
        trip(2, "Home, George Town", "Queensbay Mall, Bayan Lepas", "George Town", "Bayan Lepas"),
        trip(3, "Home, George Town", "Queensbay Mall Car Park, Bayan Lepas", "George Town", "Bayan Lepas"),
        # A single one-off trip to a genuinely different area.
        trip(4, "Home, George Town", "Airport, Bayan Lepas", "George Town", "Bayan Lepas Airport Zone"),
    ]
    routes = dict(driving_analysis.analyze(drives, 150.0, 75.0)["top_routes"])
    # The three mall visits count as ONE route (3x), not three separate
    # single-count entries — displayed using the most common specific label.
    assert routes.get("Home, George Town → Queensbay Mall, Bayan Lepas") == 3
    assert "Home, George Town → Queensbay Mall Car Park, Bayan Lepas" not in routes
    assert routes.get("Home, George Town → Airport, Bayan Lepas") == 1


def test_top_routes_falls_back_to_location_when_area_missing():
    """Rows logged before start_area/end_area existed (empty string) still
    group sensibly, using the specific location as their own grouping key."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def trip(day):
        return Drive(
            start_time=datetime(2026, 7, day, 8, 0), end_time=datetime(2026, 7, day, 8, 20),
            distance_km=5.0, duration_min=20.0, avg_speed_kmh=15.0, max_speed_kmh=40.0,
            start_soc=60, end_soc=58, energy_used_kwh=0.7, outside_temp_c=25.0,
            start_location="Home, George Town", end_location="Office, George Town",
            start_area="", end_area="",
        )

    routes = dict(driving_analysis.analyze([trip(1), trip(2)], 150.0, 75.0)["top_routes"])
    assert routes.get("Home, George Town → Office, George Town") == 2


def test_recent_trips_report_idle_stripped_driving_energy():
    """A trip with real tracked idle exposes driving_energy_kwh below the gross
    energy (Tesla-'Current Drive'-comparable); a trip with no idle reports the
    two equal."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    idled = Drive(
        start_time=datetime(2026, 7, 9, 21, 24), end_time=datetime(2026, 7, 9, 21, 47),
        distance_km=10.4, duration_min=23.0, avg_speed_kmh=27.0, max_speed_kmh=84.0,
        start_soc=73, end_soc=71, energy_used_kwh=1.78, outside_temp_c=31.0,
        idle_min=6.0, idle_tracked=True,
    )
    row = driving_analysis.analyze([idled], 150.0, 72.0)["recent_trips"][0]
    assert row["driving_energy_kwh"] is not None
    assert row["driving_energy_kwh"] < row["energy_kwh"]      # idle draw removed

    steady = Drive(
        start_time=datetime(2026, 7, 9, 8, 0), end_time=datetime(2026, 7, 9, 8, 25),
        distance_km=8.0, duration_min=25.5, avg_speed_kmh=18.9, max_speed_kmh=74.0,
        start_soc=44, end_soc=42, energy_used_kwh=1.52, outside_temp_c=31.0,
        idle_min=0.0, idle_tracked=True,   # confirmed zero idle -> no stripping
    )
    row = driving_analysis.analyze([steady], 150.0, 72.0)["recent_trips"][0]
    assert row["driving_energy_kwh"] == row["energy_kwh"]


def test_recent_trips_report_soc_used_pct():
    """Each recent_trips entry carries the % of battery that trip drew, at
    1-decimal precision. Because start_soc/end_soc are integer battery_level,
    the % is derived from the fractional energy (energy_used / capacity) when
    energy is valid, and only falls back to the integer SoC delta otherwise."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    # Integer SoC delta says 2% (44 -> 42), but the trip actually drew 1.9 kWh
    # of a 75 kWh pack = 2.5333...% -> reported as 2.5, a real sub-1% gain in
    # precision the whole-number SoC delta could never show.
    trip = Drive(
        start_time=datetime(2026, 7, 8, 19, 5), end_time=datetime(2026, 7, 8, 19, 30),
        distance_km=8.0, duration_min=25.5, avg_speed_kmh=18.9, max_speed_kmh=74.0,
        start_soc=44, end_soc=42, energy_used_kwh=1.9, outside_temp_c=31.0,
    )
    result = driving_analysis.analyze([trip], 150.0, 75.0)
    assert result["recent_trips"][0]["soc_used_pct"] == 2.5

    # No valid energy (range gap): fall back to the integer SoC delta.
    gap = Drive(
        start_time=datetime(2026, 7, 8, 20, 5), end_time=datetime(2026, 7, 8, 20, 30),
        distance_km=8.0, duration_min=25.0, avg_speed_kmh=19.2, max_speed_kmh=70.0,
        start_soc=42, end_soc=39, energy_used_kwh=0.0, outside_temp_c=31.0,
    )
    result = driving_analysis.analyze([gap], 150.0, 75.0)
    assert result["recent_trips"][0]["soc_used_pct"] == 3.0


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
    # Each temperature bucket carries its trip count and average speed
    # alongside Wh/km, so a thin or slow (traffic-skewed) bucket can be
    # told apart from a genuine temperature effect.
    for bucket in result["efficiency_by_temp"].values():
        assert set(bucket) == {"wh_per_km", "n", "avg_speed_kmh"}
        assert bucket["n"] >= 1


def test_efficiency_by_temp_bucket_reports_count_and_avg_speed():
    """A bucket with one slow trip and one fast trip: Wh/km averages the two,
    n counts them, and avg_speed_kmh is their mean speed — not conflated with
    a different bucket's drives."""
    from datetime import datetime

    def mk(hour, wh_per_km, speed, temp):
        kwh = wh_per_km * 10.0 / 1000.0
        return Drive(start_time=datetime(2026, 7, 4, hour, 0),
                     end_time=datetime(2026, 7, 4, hour, 20),
                     distance_km=10.0, duration_min=20, avg_speed_kmh=speed,
                     max_speed_kmh=speed * 1.3, start_soc=80, end_soc=75,
                     energy_used_kwh=kwh, outside_temp_c=temp)
    drives = [
        mk(8, 140.0, 60.0, 25.0),   # 20-30C bucket, fast
        mk(9, 160.0, 20.0, 25.0),   # 20-30C bucket, slow (traffic)
        mk(18, 200.0, 15.0, 12.0),  # 10-20C bucket, single slow trip
    ]
    result = efficiency_analysis.analyze(drives, rated_wh_per_km=150)
    by_temp = result["efficiency_by_temp"]
    assert by_temp["20-30"] == {"wh_per_km": 150.0, "n": 2, "avg_speed_kmh": 40.0}
    assert by_temp["10-20"] == {"wh_per_km": 200.0, "n": 1, "avg_speed_kmh": 15.0}


def test_daily_efficiency_groups_by_calendar_day_not_week():
    """Daily trend (unlike weekly) keeps two drives a few days apart in the
    same week as separate entries."""
    from datetime import datetime

    def mk(day, kwh_per_km):
        return Drive(start_time=datetime(2026, 7, day, 8, 0),
                     end_time=datetime(2026, 7, day, 8, 30),
                     distance_km=10.0, duration_min=30, avg_speed_kmh=40,
                     max_speed_kmh=60, start_soc=80, end_soc=75,
                     energy_used_kwh=kwh_per_km * 10.0 / 1000.0, outside_temp_c=28)
    # 6 and 8 July 2026 are both within ISO week 2026-W27, but should still
    # be two distinct keys in the daily trend.
    drives = [mk(6, 150.0), mk(8, 170.0)]
    result = efficiency_analysis.analyze(drives, rated_wh_per_km=150)
    assert result["daily_efficiency"] == {"2026-07-06": 150.0, "2026-07-08": 170.0}
    assert len(result["weekly_efficiency"]) == 1


def test_weekly_and_daily_distance_km_sum_alongside_efficiency():
    """Distance trends carry the same keys as their efficiency counterparts
    (same underlying drives, same grouping) but sum km rather than average
    Wh/km — two trips landing on the same day/week must add together."""
    from datetime import datetime

    def mk(day, distance_km, wh_per_km=150.0):
        return Drive(start_time=datetime(2026, 7, day, 8, 0),
                     end_time=datetime(2026, 7, day, 8, 30),
                     distance_km=distance_km, duration_min=30, avg_speed_kmh=40,
                     max_speed_kmh=60, start_soc=80, end_soc=75,
                     energy_used_kwh=wh_per_km * distance_km / 1000.0, outside_temp_c=28)
    # Two same-day trips (6 Jul) must sum; 8 Jul is a separate day but the
    # same ISO week, so the weekly total covers all three.
    drives = [mk(6, 10.0), mk(6, 5.0), mk(8, 20.0)]
    result = efficiency_analysis.analyze(drives, rated_wh_per_km=150)
    assert result["daily_distance_km"] == {"2026-07-06": 15.0, "2026-07-08": 20.0}
    assert list(result["weekly_distance_km"].values()) == [35.0]
    assert set(result["daily_distance_km"]) == set(result["daily_efficiency"])
    assert set(result["weekly_distance_km"]) == set(result["weekly_efficiency"])


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


def test_smart_charging_advisor_sizes_saving_from_peak_hour_energy():
    """With a real TOU tariff configured, the advisor must size its saving
    from the account's own peak-hour energy — not a generic heuristic —
    and must never touch anything beyond producing recommendation text
    (advisory only, no vehicle command)."""
    charging = {
        "available": True,
        "full_charge_share_pct": 0.0,
        "dc_energy_share_pct": 0.0,
        "total_sessions": 4,
        "charges_by_hour": {},
        # 10 kWh at 14:00 (peak, 08-22) + 5 kWh at 02:00 (off-peak).
        "energy_by_hour": {**{str(h): 0.0 for h in range(24)}, "14": 10.0, "2": 5.0},
    }
    tou = {"peak_price": 1.20, "offpeak_price": 0.45,
           "peak_start_hour": 8, "peak_end_hour": 22}
    recs = recommendations_engine.build(
        {"available": False}, charging, {"available": False},
        energy_price=0.90, currency="RM", tou=tou,
    )
    advisor = next(r for r in recs if r["title"].startswith("Smart charging"))
    assert "10.0 kWh" in advisor["title"]
    # 10 kWh * (1.20 - 0.45) = RM 7.50.
    assert "7.50" in advisor["estimated_saving"]
    # Purely a recommendation dict — no side effects, no vehicle-facing keys.
    assert set(advisor) == {"category", "priority", "title", "detail", "estimated_saving"}

    # No peak-hour energy at all -> no advisor recommendation fires.
    charging_no_peak = {**charging, "energy_by_hour": {str(h): 0.0 for h in range(24)}}
    charging_no_peak["energy_by_hour"]["2"] = 5.0
    recs2 = recommendations_engine.build(
        {"available": False}, charging_no_peak, {"available": False},
        energy_price=0.90, currency="RM", tou=tou,
    )
    assert not any(r["title"].startswith("Smart charging") for r in recs2)

    # Without a configured TOU tariff, falls back to the old generic hint
    # instead (never both at once).
    recs3 = recommendations_engine.build(
        {"available": False}, charging, {"available": False},
        energy_price=0.90, currency="RM", tou=None,
    )
    assert not any(r["title"].startswith("Smart charging") for r in recs3)


def test_dc_savings_uses_dc_own_rate_not_blended_average():
    """The "move DC energy to home AC" saving must be sized from DC's own
    rate, not the AC+DC blended avg_cost_per_kwh -- blending in (cheaper) AC
    sessions understates DC's real premium over home charging. Here: 70 kWh
    AC @ RM0.20/kWh (RM14) + 30 kWh DC @ RM0.60/kWh (RM18) = RM32 total,
    avg RM0.32/kWh. Sizing off the blend would say (0.32-0.20)*30 = RM3.60;
    sizing off DC's own RM0.60 rate says (0.60-0.20)*30 = RM12.00 -- the
    real gap."""
    charging = {
        "available": True,
        "full_charge_share_pct": 0.0,
        "dc_energy_share_pct": 30.0,  # > 25 -> triggers the recommendation
        "total_sessions": 5,
        "charges_by_hour": {},
        "ac_cost": 14.0,
        "dc_cost": 18.0,
        "ac_energy_kwh": 70.0,
        "dc_energy_kwh": 30.0,
        "avg_cost_per_kwh": round((14.0 + 18.0) / 100.0, 3),  # 0.32
    }
    recs = recommendations_engine.build(
        {"available": False}, charging, {"available": False},
        energy_price=0.20, currency="RM",
    )
    dc_rec = next(r for r in recs if "DC fast charging" in r["title"])
    assert "12" in dc_rec["estimated_saving"]
    assert "3.60" not in dc_rec["estimated_saving"] and "3.6" not in dc_rec["estimated_saving"]


def test_recommendations_empty_data():
    recs = recommendations_engine.build(
        {"available": False}, {"available": False}, {"available": False},
        energy_price=0.30, currency="USD",
    )
    assert len(recs) == 1
    assert recs[0]["category"] == "Overall"
