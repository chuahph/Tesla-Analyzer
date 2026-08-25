"""Tests for the analytics engine and helpers."""
import pytest
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


def test_drain_a_recovered_departure_gave_up_lands_on_the_gap_not_nowhere():
    """Energy conserved across a trip boundary.

    A departure recovered from a blackout takes the PRE-gap SoC as the trip's
    baseline, so the gap before it measures only up to that reading while its
    own clock runs on to the trip's start. The minutes in between
    (Drive.start_park_min) had their standby drain subtracted from the trip's
    energy — real drain that would otherwise be in neither, making trip kWh +
    vampire kWh fall short of the battery actually used, which is the very
    thing this function counts every short gap to avoid."""
    from datetime import datetime

    from app.analysis.driving import vampire_drain
    from app.models import Drive

    def d(start, end, ssoc, esoc, park_min=None):
        return Drive(id=None, start_time=start, end_time=end, distance_km=3.0,
                     duration_min=10.0, avg_speed_kmh=30, max_speed_kmh=45,
                     start_soc=ssoc, end_soc=esoc, energy_used_kwh=0.0,
                     outside_temp_c=28.0, start_park_min=park_min)

    # Gaps long enough, and enough of them, that a rate can actually be fitted
    # (STANDBY_MIN_GAP_HOURS 6, STANDBY_MIN_TOTAL_HOURS 24) — without one
    # nothing is subtracted at sync time either, so there is nothing to hand
    # back and this correction correctly does nothing.
    times = [(4, 6), (4, 18), (5, 6), (5, 18)]
    socs = [(90, 88), (86, 84), (82, 80), (78, 76)]
    base = [d(datetime(2026, 7, day, hr, 0), datetime(2026, 7, day, hr, 10), s, e)
            for (day, hr), (s, e) in zip(times, socs)]
    plain = vampire_drain(base, [], 75.0)

    # The last trip's departure gave up 30 parked minutes.
    given_up = list(base)
    given_up[-1] = d(datetime(2026, 7, 5, 18, 0), datetime(2026, 7, 5, 18, 10),
                     78, 76, park_min=30.0)
    restored = vampire_drain(given_up, [], 75.0)

    # The same SoC readings, so the measured drops are identical — the whole
    # difference is the drain handed back, and it can only be positive.
    assert restored["kwh"] > plain["kwh"]
    from app.analysis.driving import parked_rate_kw
    rate = parked_rate_kw(given_up, [], 75.0)
    assert rate, "this history should support a rate"
    assert restored["kwh"] - plain["kwh"] == pytest.approx(rate * 0.5, abs=0.01)

    # A window where nothing was given up is untouched, and never pays for
    # fitting a rate it has no use for.
    assert vampire_drain(base, [], 75.0) == plain


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
    """The driving figure must not be guessed from the speed spread.

    Real-world case: 8.0 km / 25.5 min, avg 18.9 / max 74 km/h (heavy stop-go
    with many short lights), 1.52 kWh -> 190 Wh/km gross. The old avg/max
    heuristic inferred idle time from the speed spread alone and produced
    ~144; the model now subtracts a climate load measured against duration and
    temperature instead, so the answer no longer depends on how spiky the
    speed was.

    Note the driving figure is deliberately BELOW the gross even though this
    trip had zero sustained stops: climate runs while the car moves, so idle
    was never the right thing to gate it on (the car's own breakdown shows a
    Climate line on every trip).
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
    # Not the old heuristic's ~144, and not the gross either: climate over
    # 25.5 min at 31C is stripped whatever the stop pattern was. The exact
    # figure tracks ACCESSORY_KW and CLIMATE_BASE_KW, which moved to their own
    # measured means (107 before that); what this test pins is that a
    # confirmed-zero-idle trip is still stripped, not the constants.
    assert trip["driving_wh_per_km"] == 102
    assert trip["driving_wh_per_km"] != 144

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


def test_efficiency_by_speed_band_captures_the_u_shape():
    """Measured Wh/km per speed regime. This exists because consumption vs
    speed is U-shaped — a stop-go crawl and a highway run both cost more per km
    than a moderate cruise — so the linear speed_efficiency_slope inverts when
    extrapolated down to a jam, predicting *less* consumption than average.
    Anything asking "what does this driver use at N km/h" must read the
    measured band, and this pins that the bands really do show the U."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    def trip(day, speed, whkm, km=10.0):
        return Drive(
            start_time=datetime(2026, 7, day, 9, 0), end_time=datetime(2026, 7, day, 10, 0),
            distance_km=km, duration_min=km / speed * 60, avg_speed_kmh=speed,
            max_speed_kmh=speed * 1.5, start_soc=80, end_soc=70,
            energy_used_kwh=km * whkm / 1000.0, outside_temp_c=28.0,
        )

    drives = [
        trip(1, 20, 210), trip(2, 22, 195),      # City: crawling, expensive
        trip(3, 45, 140), trip(4, 50, 135),      # Urban: the sweet spot
        trip(5, 100, 190), trip(6, 110, 200),    # Highway: aero drag, expensive
    ]
    bands = driving_analysis.analyze(drives, 150.0, 75.0)["efficiency_by_speed_band"]

    assert bands["City (<30)"] > bands["Urban (30-60)"]
    assert bands["Highway (90+)"] > bands["Urban (30-60)"]
    # The crawl really is dearer than the overall average — the thing a linear
    # extrapolation gets backwards.
    overall = driving_analysis.analyze(drives, 150.0, 75.0)["avg_efficiency_wh_per_km"]
    assert bands["City (<30)"] > overall

    # A regime with no trips is absent rather than guessed at.
    assert "Rural (60-90)" not in bands


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


def test_layered_trip_costs_cascades_through_older_charges():
    """Each completed charge is a stack layer (its own rate + kWh) that
    trips drain most-recently-first; once a layer is exhausted, trips fall
    back to the layer beneath, and a new charge always resets consumption
    to a fresh top layer even if older ones still have kWh left."""
    from datetime import datetime

    from app.analysis.driving import layered_trip_costs
    from app.models import Charge, Drive

    def charge(id_, end, kwh, cost):
        return Charge(id=id_, start_time=end, end_time=end,
                      energy_added_kwh=kwh, cost=cost)

    def trip(id_, start, kwh):
        return Drive(
            id=id_, start_time=start, end_time=start,
            distance_km=10.0, duration_min=15.0, avg_speed_kmh=40.0, max_speed_kmh=60.0,
            start_soc=80, end_soc=70, energy_used_kwh=kwh, outside_temp_c=28.0,
        )

    # Charge A: 2 kWh at RM1.00/kWh. Charge B (older, still has room): 10 kWh
    # at RM0.50/kWh — never touched until A runs dry.
    charge_a = charge(1, datetime(2026, 7, 1, 8, 0), 2.0, 2.00)
    charge_b_older = charge(2, datetime(2026, 6, 25, 8, 0), 10.0, 5.00)

    # Trip 1 (1 kWh) fits entirely inside A's layer -> RM1.00.
    t1 = trip(1, datetime(2026, 7, 1, 9, 0), 1.0)
    # Trip 2 (2 kWh) exhausts A's remaining 1 kWh (RM1.00) then spills 1 kWh
    # into the older B layer at RM0.50 -> RM1.50 total.
    t2 = trip(2, datetime(2026, 7, 1, 10, 0), 2.0)

    costs = layered_trip_costs([t1, t2], [charge_a, charge_b_older])
    assert costs[1]["cost"] == 1.00
    assert costs[2]["cost"] == 1.50

    # Trip 1 sat entirely inside A, so it reports a single layer...
    assert [(p["kwh"], p["rate"], p["charge_id"]) for p in costs[1]["parts"]] == [(1.0, 1.0, 1)]
    # ...while trip 2 straddles the boundary and must report BOTH, so a
    # blended figure is auditable rather than unexplained.
    assert [(p["kwh"], p["rate"], p["charge_id"]) for p in costs[2]["parts"]] == [
        (1.0, 1.0, 1), (1.0, 0.5, 2),
    ]

    # A brand-new charge (C) resets consumption to itself, even though B
    # still has 9 kWh left untouched underneath.
    charge_c = charge(3, datetime(2026, 7, 1, 11, 0), 3.0, 4.50)  # RM1.50/kWh
    t3 = trip(3, datetime(2026, 7, 1, 12, 0), 1.0)
    costs2 = layered_trip_costs([t1, t2, t3], [charge_a, charge_b_older, charge_c])
    assert costs2[3]["cost"] == 1.50  # priced off C (RM1.50/kWh), not B's RM0.50/kWh
    assert [p["charge_id"] for p in costs2[3]["parts"]] == [3]

    # A trip that outruns every layer in the whole charge history (no
    # charge on record at all) prices as unknown, not a guess — and reports
    # no parts, since there's nothing to attribute it to.
    lone_trip = trip(4, datetime(2026, 7, 1, 8, 0), 1.0)
    assert layered_trip_costs([lone_trip], []) == {4: {"cost": None, "parts": []}}


def test_free_charge_running_out_mid_trip_splits_the_cost():
    """The real case this model exists for: a free session tops the stack, so
    trips cost nothing until its kWh runs out — and the trip that spans the
    boundary is billed partly free, partly at the older charge's real rate.
    That blended figure is the observable proof the cascade works; a binary
    flip from free to full price could be explained by other things."""
    from datetime import datetime

    from app.analysis.driving import layered_trip_costs
    from app.models import Charge, Drive

    def charge(id_, end, kwh, cost):
        return Charge(id=id_, start_time=end, end_time=end,
                      energy_added_kwh=kwh, cost=cost)

    def trip(id_, start, kwh):
        return Drive(
            id=id_, start_time=start, end_time=start,
            distance_km=10.0, duration_min=15.0, avg_speed_kmh=40.0, max_speed_kmh=60.0,
            start_soc=80, end_soc=70, energy_used_kwh=kwh, outside_temp_c=28.0,
        )

    paid = charge(1, datetime(2026, 7, 20, 8, 0), 30.0, 33.90)  # RM1.13/kWh
    free = charge(2, datetime(2026, 7, 26, 8, 0), 2.0, 0.0)     # FOC, 2 kWh

    inside = trip(1, datetime(2026, 7, 26, 9, 0), 1.2)    # wholly free
    straddle = trip(2, datetime(2026, 7, 26, 10, 0), 2.0)  # 0.8 free + 1.2 paid
    after = trip(3, datetime(2026, 7, 26, 11, 0), 1.0)     # wholly paid

    costs = layered_trip_costs([inside, straddle, after], [paid, free])

    assert costs[1]["cost"] == 0.0
    assert [p["rate"] for p in costs[1]["parts"]] == [0.0]

    # 0.8 x 0 + 1.2 x 1.13 = 1.356 -> 1.36, and both layers are itemised.
    assert costs[2]["cost"] == 1.36
    assert [(p["kwh"], p["rate"]) for p in costs[2]["parts"]] == [(0.8, 0.0), (1.2, 1.13)]

    assert costs[3]["cost"] == 1.13
    assert [p["charge_id"] for p in costs[3]["parts"]] == [1]
    """analyze()'s per-trip cost, total_cost/cost_per_km and by_tag all use
    the layered figure when trip_costs is supplied, and a trip the stack
    can't reach shows an unknown (None) cost rather than a guessed one."""
    from datetime import datetime

    from app.analysis import driving as driving_analysis
    from app.models import Drive

    priced_trip = Drive(
        id=1, start_time=datetime(2026, 7, 1, 9, 0), end_time=datetime(2026, 7, 1, 9, 20),
        distance_km=10.0, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
        start_soc=80, end_soc=78, energy_used_kwh=1.0, outside_temp_c=28.0,
    )
    unpriced_trip = Drive(
        id=2, start_time=datetime(2026, 7, 1, 10, 0), end_time=datetime(2026, 7, 1, 10, 20),
        distance_km=10.0, duration_min=20.0, avg_speed_kmh=30.0, max_speed_kmh=60.0,
        start_soc=78, end_soc=76, energy_used_kwh=1.0, outside_temp_c=28.0,
    )
    trip_costs = {
        1: {"cost": 1.50, "parts": [{"kwh": 1.0, "rate": 1.5, "charge_id": 7}]},
        2: {"cost": None, "parts": []},
    }
    r = driving_analysis.analyze(
        [priced_trip, unpriced_trip], 150.0, 75.0, energy_price=0.0,
        trip_costs=trip_costs)
    rows = {row["id"]: row for row in r["recent_trips"]}
    assert rows[1]["cost"] == 1.50
    assert rows[1]["cost_source"] == "auto"
    # The layer breakdown is surfaced so a blended figure stays checkable.
    assert rows[1]["cost_parts"] == [{"kwh": 1.0, "rate": 1.5, "charge_id": 7}]
    assert rows[2]["cost"] is None
    assert rows[2]["cost_parts"] is None
    # Total reflects only what's known — the unpriced trip contributes 0,
    # not a hard block on the whole window's total.
    assert r["total_cost"] == 1.50


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
    """driving_energy_kwh sits below the gross (which is what matches Tesla's
    "Current Drive"), and does so whether or not the trip had sustained stops
    — climate is a whole-trip load, so a steady drive is charged for it too."""
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
        idle_min=0.0, idle_tracked=True,   # zero sustained stops...
    )
    row = driving_analysis.analyze([steady], 150.0, 72.0)["recent_trips"][0]
    # ...but climate still ran, so this is stripped too. Under the old
    # idle-gated model this trip reported driving == gross, which is exactly
    # the case that made the figure useless in stop-go traffic.
    assert row["driving_energy_kwh"] < row["energy_kwh"]


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

def test_empty_window_still_reports_the_rated_basis():
    """Regression: the Trip Planner vanished for the whole stretch between a
    charge finishing and the next drive. The since-charge window has no drives
    in it yet, and this branch dropped rated_wh_per_km, leaving the planner
    with no efficiency basis at all — so it hid itself, exactly when you'd be
    planning the drive. rated_wh_per_km is a setting, not a measurement; an
    empty window doesn't make it unknown."""
    out = efficiency_analysis.analyze([], rated_wh_per_km=150)
    assert out["available"] is False
    assert out["rated_wh_per_km"] == 150


def test_a_window_with_drives_but_no_energy_also_reports_it():
    """The neighbouring early return already did this — the two must agree, or
    the planner's fallback works in one empty case and not the other."""
    from types import SimpleNamespace

    no_energy = SimpleNamespace(distance_km=10.0, energy_used_kwh=0.0, wh_per_km=0.0)
    out = efficiency_analysis.analyze([no_energy], rated_wh_per_km=150)
    assert out["available"] is False
    assert out["rated_wh_per_km"] == 150


def test_efficiency_analysis(seeded):
    drives = seeded.scalars(select(Drive)).all()
    result = efficiency_analysis.analyze(list(drives), rated_wh_per_km=150)
    assert result["available"]
    assert result["worst_efficiency_wh_per_km"] >= result["best_efficiency_wh_per_km"]
    # The slope's *sign* is asserted on constructed data below, not here:
    # generate() anchors its window at datetime.now(), so the seasonal-temp
    # slice this dataset covers depends on the real calendar date — and the
    # sample efficiency model penalizes distance from 21°C in both
    # directions, so a mostly-warm window (tests run in summer) legitimately
    # flattens or flips the cold-is-worse slope. Only its existence is a
    # stable property of this dataset.
    assert isinstance(result["temp_efficiency_slope_wh_per_c"], float)
    # Each temperature bucket carries its trip count and average speed
    # alongside Wh/km, so a thin or slow (traffic-skewed) bucket can be
    # told apart from a genuine temperature effect.
    for bucket in result["efficiency_by_temp"].values():
        assert set(bucket) == {"wh_per_km", "n", "avg_speed_kmh"}
        assert bucket["n"] >= 1


def test_temp_efficiency_slope_negative_when_cold_trips_cost_more():
    """Cold trips burning more Wh/km than warm ones must yield a negative
    Wh/km-vs-temp slope — asserted on constructed drives so the expected
    sign is unambiguous (the seeded dataset's sign shifts with the real
    calendar date; see test_efficiency_analysis)."""
    from datetime import datetime

    def mk(hour, wh_per_km, temp):
        return Drive(start_time=datetime(2026, 1, 5, hour, 0),
                     end_time=datetime(2026, 1, 5, hour, 30),
                     distance_km=10.0, duration_min=30, avg_speed_kmh=40,
                     max_speed_kmh=60, start_soc=80, end_soc=75,
                     energy_used_kwh=wh_per_km * 10.0 / 1000.0,
                     outside_temp_c=temp)
    drives = [
        mk(7, 210.0, 2.0),    # freezing morning, battery heater on
        mk(9, 190.0, 8.0),
        mk(12, 165.0, 15.0),
        mk(15, 150.0, 22.0),  # mild afternoon, rated-ish
    ]
    result = efficiency_analysis.analyze(drives, rated_wh_per_km=150)
    assert result["temp_efficiency_slope_wh_per_c"] < 0


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
    assert set(advisor) == {"category", "priority", "title", "detail",
                            "estimated_saving", "saving_kwh", "saving_cost", "bucket"}

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


# --- assessment ------------------------------------------------------------

def test_assessment_total_does_not_double_count_driving_tips():
    """The headline addressable saving must be the best-quartile driving lever
    PLUS independent charging savings — never the sum of every overlapping
    driving tip (speeding + stop-go + vs-rated + best-quartile), which would
    wildly overstate it."""
    driving = {
        "available": True, "eco_score": 72, "eco_grade": "C",
        "total_drives": 40, "total_distance_km": 500.0,
        "speed_efficiency_slope_wh_per_kmh": 0.0,
        "behaviour": {
            "available": True, "score": 70, "potential_saving_kwh": 10.0,
            "best_quartile_wh_per_km": 150.0,
            # Two overlapping factor tips fire, each with its own kWh.
            "speeding_share_pct": 30, "speeding_penalty_wh": 20, "speeding_saving_kwh": 6.0,
            "stopgo_share_pct": 25, "stopgo_penalty_wh": 15, "stopgo_saving_kwh": 4.0,
        },
    }
    # vs-rated also fires (another overlapping view of the same driving money).
    efficiency = {"available": True, "vs_rated_pct": 20.0,
                  "total_energy_kwh": 120.0, "temp_efficiency_slope_wh_per_c": 0.0}
    # One independent charging saving: 30 kWh DC @ 0.60 vs 0.20 home = 12.00.
    charging = {
        "available": True, "full_charge_share_pct": 0.0, "dc_energy_share_pct": 30.0,
        "total_sessions": 5, "charges_by_hour": {}, "dc_cost": 18.0,
        "dc_energy_kwh": 30.0, "ac_energy_kwh": 70.0,
    }
    a = recommendations_engine.assess(
        driving, charging, efficiency, energy_price=0.20, currency="RM",
    )
    # driving lever = 10 kWh * 0.20 = 2.00; charging = 12.00; total = 14.00.
    # NOT 2 + 12 + (6*.2) + (4*.2) + vs_rated_extra*.2 (~20) = far higher.
    assert a["addressable_saving"]["cost"] == 14.0
    assert a["addressable_saving"]["kwh"] == 40.0  # 10 driving + 30 charging
    assert a["grade"] == "C"
    assert a["score"] == 72
    assert "RM 14.00" in a["verdict"]


def test_assessment_reports_strengths_and_grade_when_all_good():
    """A clean account with a high eco-score, low degradation and disciplined
    charging surfaces strengths and no addressable saving."""
    driving = {
        "available": True, "eco_score": 92, "eco_grade": "A",
        "total_drives": 50, "total_distance_km": 800.0,
        "behaviour": {"available": True, "score": 97, "potential_saving_kwh": 0.2},
    }
    efficiency = {"available": True, "vs_rated_pct": -3.0, "total_energy_kwh": 100.0}
    charging = {"available": True, "full_charge_share_pct": 0.0,
                "dc_energy_share_pct": 2.0, "total_sessions": 10, "charges_by_hour": {}}
    battery = {"available": True, "degradation_pct": 2.0}
    a = recommendations_engine.assess(
        driving, charging, efficiency, battery, energy_price=0.30, currency="USD",
    )
    assert a["addressable_saving"]["cost"] == 0.0
    titles = {s["title"] for s in a["strengths"]}
    assert "Efficient driving" in titles
    assert "Battery health strong" in titles
    assert a["grade"] == "A"
    assert a["confidence"] == "high"


def test_standby_drain_tip_from_vampire_data_not_in_headline_total():
    """A material parked/standby drain surfaces its own tip (with the inducer
    named when known), but is deliberately kept OUT of the headline
    'recoverable' total — only the Sentry/climate share is avoidable, so
    claiming the whole figure would overstate it."""
    driving = {
        "available": True, "eco_score": 80, "eco_grade": "B",
        "total_drives": 30, "total_distance_km": 400.0,
        "vampire_drain": {"kwh": 4.0, "hours": 60.0, "gaps": 5},
    }
    recs = recommendations_engine.build(
        driving, {"available": False}, {"available": False},
        energy_price=0.90, currency="RM",
        standby_inducer="Sentry Mode (maybe)",
    )
    tip = next(r for r in recs if r["category"] == "Standby")
    assert "4.0 kWh" in tip["title"]
    assert "Sentry Mode" in tip["detail"]
    assert tip["saving_cost"] == round(4.0 * 0.90, 2)  # 3.60
    assert tip["bucket"] == "standby"

    a = recommendations_engine.assess(
        driving, {"available": False}, {"available": False},
        energy_price=0.90, currency="RM", standby_inducer="Sentry Mode (maybe)",
    )
    # standby is shown as a tip but excluded from the recoverable total.
    assert a["addressable_saving"]["cost"] == 0.0
    assert any(r["category"] == "Standby" for r in a["recommendations"])

    # Negligible drain -> no tip.
    quiet = {**driving, "vampire_drain": {"kwh": 0.1, "hours": 12.0, "gaps": 2}}
    recs2 = recommendations_engine.build(
        quiet, {"available": False}, {"available": False},
        energy_price=0.90, currency="RM",
    )
    assert not any(r["category"] == "Standby" for r in recs2)


def test_assessment_trend_direction_and_confidence():
    """Trend compares this window's efficiency to the previous one (lower
    Wh/km = better), and thin windows are flagged low-confidence."""
    driving = {"available": True, "eco_score": 80, "eco_grade": "B",
               "total_drives": 3, "total_distance_km": 40.0, "cost_per_km": 0.05}
    efficiency = {"available": True, "vs_rated_pct": 5.0,
                  "avg_efficiency_wh_per_km": 160.0, "total_energy_kwh": 50.0}
    prev = {"driving": {"available": True, "cost_per_km": 0.06},
            "efficiency": {"available": True, "avg_efficiency_wh_per_km": 180.0}}
    a = recommendations_engine.assess(
        driving, {"available": False}, efficiency,
        energy_price=0.30, currency="USD", prev=prev,
    )
    # 160 vs 180 -> ~11% lower -> better.
    assert a["trend"]["wh_per_km"]["dir"] == "better"
    assert a["confidence"] == "low"
    assert "indicative" in a["verdict"]


# --- Measured standby rate --------------------------------------------------

def _d(start, end, start_soc, end_soc):
    """A drive stub: only the four fields the standby measurement reads."""
    from types import SimpleNamespace
    from datetime import datetime
    return SimpleNamespace(
        start_time=datetime.fromisoformat(start), end_time=datetime.fromisoformat(end),
        start_soc=start_soc, end_soc=end_soc,
    )


def test_standby_kw_measures_the_rate_from_parked_gaps():
    """SoC lost between one trip ending and the next starting, over the hours
    between — a 2% drop across 10 parked hours of a 70 kWh pack is 0.14 kW."""
    from app.analysis.driving import standby_kw

    drives = [
        _d("2026-07-01T08:00", "2026-07-01T09:00", 90, 88),
        _d("2026-07-01T19:00", "2026-07-01T20:00", 86, 84),   # 10 h gap, 2% lost
        _d("2026-07-02T06:00", "2026-07-02T07:00", 82, 80),   # 10 h gap, 2% lost
        _d("2026-07-02T17:00", "2026-07-02T18:00", 78, 76),   # 10 h gap, 2% lost
    ]
    rate = standby_kw(drives, [], 70.0)
    assert rate == round(4.2 / 30.0, 3)      # 3 x (2% of 70) over 3 x 10 h


def test_standby_kw_skips_gaps_containing_a_charge():
    """A charge mid-gap makes the endpoints say nothing about drain — the SoC
    went up, not down."""
    from app.analysis.driving import standby_kw
    from types import SimpleNamespace
    from datetime import datetime

    drives = [
        _d("2026-07-01T08:00", "2026-07-01T09:00", 90, 88),
        _d("2026-07-01T19:00", "2026-07-01T20:00", 86, 84),
        _d("2026-07-02T06:00", "2026-07-02T07:00", 82, 80),
        _d("2026-07-02T17:00", "2026-07-02T18:00", 78, 76),
    ]
    charged = SimpleNamespace(start_time=datetime.fromisoformat("2026-07-01T12:00"))
    # With that gap excluded only 20 h remain, below the total-hours floor.
    assert standby_kw(drives, [charged], 70.0) is None


def test_standby_kw_needs_enough_observed_hours():
    """One qualifying gap is a sample, not a rate — whole-percent SoC leaves
    each individual gap uncertain by most of a point."""
    from app.analysis.driving import standby_kw

    drives = [
        _d("2026-07-01T08:00", "2026-07-01T09:00", 90, 88),
        _d("2026-07-01T19:00", "2026-07-01T20:00", 86, 84),   # one 10 h gap
    ]
    assert standby_kw(drives, [], 70.0) is None


def test_standby_kw_ignores_the_hours_where_awake_and_asleep_are_mixed():
    """Every park opens awake at several times the sleeping rate, so a 3 h gap
    is mostly that burst and a 12 h one is mostly sleep. Averaging both lands
    between the two and describes neither — this car read 0.22 kW that way,
    while a measured 12.3 h overnight park lost under 0.06 kW. The middle band
    belongs to neither rate, because nothing here says when sleep began."""
    from app.analysis.driving import standby_kw

    drives = [
        _d("2026-07-01T08:00", "2026-07-01T09:00", 90, 88),
        _d("2026-07-01T12:00", "2026-07-01T13:00", 87, 85),   # 3 h gap
        _d("2026-07-01T16:00", "2026-07-01T17:00", 84, 82),   # 3 h gap
        _d("2026-07-01T20:00", "2026-07-01T21:00", 81, 79),   # 3 h gap
        _d("2026-07-02T00:00", "2026-07-02T01:00", 78, 76),   # 3 h gap
    ]
    # 12 h of gaps, every one of them in the mixed band.
    assert standby_kw(drives, [], 70.0) is None


def test_standby_kw_rejects_an_implausible_rate():
    """Way outside a parked car's range is a measurement artifact, and the
    caller must get None rather than a number that would reshape trip energy."""
    from app.analysis.driving import standby_kw

    drives = [
        _d("2026-07-01T08:00", "2026-07-01T09:00", 90, 88),
        _d("2026-07-01T21:00", "2026-07-01T22:00", 40, 38),   # 48% over 12 h
        _d("2026-07-02T10:00", "2026-07-02T11:00", 20, 18),   # 18% over 12 h
    ]
    assert standby_kw(drives, [], 70.0) is None


# --- Measured just-parked rate ----------------------------------------------

def _errands(gap_hours, n, lost_pct=1):
    """n back-to-back errand stops of gap_hours each, losing lost_pct per stop."""
    from datetime import datetime, timedelta
    t = datetime.fromisoformat("2026-07-01T08:00")
    soc = 90
    out = []
    for _ in range(n + 1):
        out.append(_d(t.isoformat(), (t + timedelta(minutes=30)).isoformat(),
                      soc, soc - 1))
        soc -= 1 + lost_pct
        t += timedelta(minutes=30) + timedelta(hours=gap_hours)
    return out


def test_trim_rate_ignores_the_awake_fit_it_cannot_measure():
    """This pinned the opposite for a while: a trimmed tail is the first
    minutes after arrival, so it "must" be priced at the awake rate rather than
    the deep-sleep one.

    The physics is still right and the measurement was never possible. One
    whole-percent SoC point is ~0.7 kWh, about eighteen hours of parked drain
    on this car, while parked_awake_kw samples gaps of 0.15-2 h and asks for
    only 6 h in total — under half a point of real signal. What it fits is
    mostly rounding, and _gap_rate_kw's max(drop, 0) keeps the upward halves of
    that noise while clipping the downward ones.

    Measured live: the awake fit returned 0.348 kW while the car's own screen
    put ALL parked drain since the last charge at 2.0%, about 0.034 kW. Ten
    times over, and stated confidently, which is worse than the noisy zero it
    replaced. So the trim is priced at standby_kw, thin but an order of
    magnitude better placed against the quantum.
    """
    from app.api.routes import _trim_rate_kw
    from app.analysis.driving import standby_kw

    short = _errands(1.5, 4)                       # awake sample only
    long_gaps = _errands(10.0, 3)                  # sleeping sample only

    # With no gap long enough to measure there is no rate, and therefore no
    # correction — rather than a rate fitted from gaps that cannot resolve it.
    assert _trim_rate_kw(short, [], 70.0) is None

    assert _trim_rate_kw(long_gaps, [], 70.0) == standby_kw(long_gaps, [], 70.0)
    assert _trim_rate_kw(long_gaps, [], 70.0) is not None


def test_trim_rate_is_none_when_neither_rate_has_evidence():
    """No history, no correction — trim_standby_kwh must get None rather than a
    guessed rate that would reshape real trip energy."""
    from app.api.routes import _trim_rate_kw

    assert _trim_rate_kw([], [], 70.0) is None


# --- Route directional cost -------------------------------------------------

def _leg(start, end, km, kwh, speed=30.0):
    from types import SimpleNamespace
    return SimpleNamespace(
        start_location=start, end_location=end, start_area=start, end_area=end,
        distance_km=km, energy_used_kwh=kwh, avg_speed_kmh=speed,
        wh_per_km=kwh / km * 1000.0,
    )


def _both_ways(out_kwh, back_kwh, n=3, out_speed=30.0, back_speed=30.0, km=10.0):
    return ([_leg("Home", "Office", km, out_kwh, out_speed) for _ in range(n)]
            + [_leg("Office", "Home", km, back_kwh, back_speed) for _ in range(n)])


def test_route_asymmetry_measures_what_the_direction_costs():
    """The climb out costs what the roll back returns, while drag, climate and
    accessories are the same both ways — so the difference between a route's
    two directions is the one handle this app has on elevation."""
    from app.analysis.driving import route_asymmetry

    rows = route_asymmetry(_both_ways(2.0, 1.6))     # 200 vs 160 Wh/km
    assert len(rows) == 1
    assert rows[0]["delta_wh_per_km"] == 40.0
    assert rows[0]["out"]["wh_per_km"] == 200.0
    assert rows[0]["back"]["wh_per_km"] == 160.0
    assert rows[0]["comparable"] is True


def test_route_asymmetry_flags_directions_driven_at_different_speeds():
    """A commute runs out in morning traffic and back in evening traffic, so a
    difference between its directions may be congestion rather than terrain.
    That cannot be separated here, so it must not be presented as if it had
    been — the row still reports, marked not comparable."""
    from app.analysis.driving import route_asymmetry

    rows = route_asymmetry(_both_ways(2.0, 1.6, out_speed=18.0, back_speed=34.0))
    assert len(rows) == 1
    assert rows[0]["speed_gap_kmh"] == 16.0
    assert rows[0]["comparable"] is False


def test_route_asymmetry_needs_both_directions():
    """One direction is just a route average; the subtraction is the whole
    measurement."""
    from app.analysis.driving import route_asymmetry

    one_way = [_leg("Home", "Office", 10.0, 2.0) for _ in range(6)]
    assert route_asymmetry(one_way) == []


def test_route_asymmetry_needs_repeats_in_each_direction():
    """A single trip each way is two samples of a noisy quantity, not a rate."""
    from app.analysis.driving import route_asymmetry

    assert route_asymmetry(_both_ways(2.0, 1.6, n=2)) == []


def test_route_asymmetry_skips_trips_too_short_to_measure():
    """Under a few km the fixed boundary rounding is a bigger share of the trip
    than the elevation term being looked for."""
    from app.analysis.driving import route_asymmetry

    assert route_asymmetry(_both_ways(0.4, 0.32, km=2.0)) == []


def test_route_asymmetry_skips_a_round_trip_within_one_area():
    """An area paired with itself is its own reverse, so it would be compared
    against itself for a guaranteed zero — a row saying nothing while taking
    one of the five slots a real pair could have used."""
    from app.analysis.driving import route_asymmetry

    loop = [_leg("Home", "Home", 10.0, 2.0) for _ in range(6)]
    assert route_asymmetry(loop) == []
    # A real pair alongside it still reports.
    assert len(route_asymmetry(loop + _both_ways(2.0, 1.6))) == 1


def test_direction_wh_per_km_is_the_road_not_an_average_of_roads():
    """The planner's other bases average over other roads — every route at
    this hour, every route at this speed. This one is the route itself in the
    direction about to be driven, which is also the only basis that carries
    its elevation: a climb cancels out of anything pooling both ways."""
    from app.analysis.driving import direction_wh_per_km

    out = direction_wh_per_km(_both_ways(2.0, 1.6), "Home", "Office")
    assert out["wh_per_km"] == 200.0        # not the 180 both-ways average
    assert out["n"] == 3
    assert direction_wh_per_km(_both_ways(2.0, 1.6), "Office", "Home")["wh_per_km"] == 160.0


def test_direction_wh_per_km_holds_back_on_thin_history():
    """A basis is only worth swapping to if it's better. Two trips is a
    thinner measurement than the broad average it would replace, so the
    caller keeps what it had."""
    from app.analysis.driving import direction_wh_per_km

    assert direction_wh_per_km(_both_ways(2.0, 1.6, n=2), "Home", "Office") is None
    assert direction_wh_per_km(_both_ways(2.0, 1.6), "Home", "Nowhere") is None
    assert direction_wh_per_km(_both_ways(2.0, 1.6), "", "Office") is None


def test_route_asymmetry_reports_each_pair_once():
    """Both orientations key the same unordered pair; emitting both would show
    the same finding twice with opposite signs."""
    from app.analysis.driving import route_asymmetry

    rows = route_asymmetry(_both_ways(2.0, 1.6) + _both_ways(2.0, 1.6))
    assert len(rows) == 1


# --- Odometer continuity ----------------------------------------------------

def _drv(did, start, end, end_odo, end_lost=0.0, start_odo=None):
    from types import SimpleNamespace
    from datetime import datetime
    return SimpleNamespace(
        id=did, start_time=datetime.fromisoformat(start),
        end_time=datetime.fromisoformat(end),
        start_odo_km=start_odo, end_odo_km=end_odo, end_lost_km=end_lost,
        start_location="A", end_location="B",
    )


def _rd(ts, odo):
    from types import SimpleNamespace
    from datetime import datetime
    return SimpleNamespace(ts=datetime.fromisoformat(ts), odo_km=odo)


def test_continuity_flags_a_trip_that_closed_before_the_car_stopped():
    """Regression for trip 314: the trip recorded its stop at 10000.0 but the
    readings taken while it sat parked show 10000.4 — that 0.4 km happened and
    belongs to this trip's arrival, and nothing recorded it."""
    from app.analysis.driving import odometer_continuity

    drives = [_drv(1, "2026-07-01T08:00", "2026-07-01T09:00", 10000.0)]
    readings = [_rd("2026-07-01T09:05", 10000.4), _rd("2026-07-01T10:00", 10000.4)]
    out = odometer_continuity(drives, readings)
    assert out["available"] is True
    assert out["unattributed_km"] == 0.4
    assert out["gaps"][0]["drive_id"] == 1
    assert out["gaps"][0]["recorded_end_odo_km"] == 10000.0
    assert out["gaps"][0]["observed_odo_km"] == 10000.4


def test_continuity_accepts_a_trip_that_reported_its_own_shortfall():
    """A trip that already recorded end_lost_km has hidden nothing — the same
    0.4 km, declared, must not be reported again as unattributed."""
    from app.analysis.driving import odometer_continuity

    drives = [_drv(1, "2026-07-01T08:00", "2026-07-01T09:00", 10000.0, end_lost=0.4)]
    readings = [_rd("2026-07-01T09:05", 10000.4)]
    assert odometer_continuity(drives, readings)["unattributed_km"] == 0.0


def test_continuity_ignores_parking_shuffle():
    """Sub-tolerance movement is odometer resolution and parking manoeuvres,
    not a boundary the app got wrong."""
    from app.analysis.driving import odometer_continuity

    drives = [_drv(1, "2026-07-01T08:00", "2026-07-01T09:00", 10000.0)]
    readings = [_rd("2026-07-01T09:05", 10000.1)]
    assert odometer_continuity(drives, readings)["gaps"] == []


def test_continuity_only_counts_readings_before_the_next_trip_sets_off():
    """Movement after the next trip has begun is that trip's, not this one's
    — otherwise every trip would be blamed for the one after it."""
    from app.analysis.driving import odometer_continuity

    drives = [
        _drv(1, "2026-07-01T08:00", "2026-07-01T09:00", 10000.0),
        _drv(2, "2026-07-01T12:00", "2026-07-01T13:00", 10050.0),
    ]
    readings = [_rd("2026-07-01T10:00", 10000.0),   # parked, no drift
                _rd("2026-07-01T12:30", 10025.0)]   # mid second trip
    assert odometer_continuity(drives, readings)["unattributed_km"] == 0.0


def test_continuity_needs_odometer_anchors_and_readings():
    """Trips logged before end_odo_km was recorded can't be checked, and must
    not be silently counted as clean."""
    from app.analysis.driving import odometer_continuity

    legacy = [_drv(1, "2026-07-01T08:00", "2026-07-01T09:00", None)]
    assert odometer_continuity(legacy, [_rd("2026-07-01T09:05", 10000.4)])["available"] is False
    good = [_drv(1, "2026-07-01T08:00", "2026-07-01T09:00", 10000.0)]
    assert odometer_continuity(good, [])["available"] is False


def test_short_parked_gaps_are_modelled_not_read_off_integer_soc():
    """SoC is stored to whole percent, so one point is the smallest drain a gap
    can express — 0.7 kWh on a 69.5 kWh pack. A parked car draws about 0.04 kW,
    which takes ~18 hours to move a single point, so anything shorter resolves
    the drain no better than "0 or 0.7 kWh".

    Measured against the car: a 1.7-hour gap read 2 points and was reported as
    1.39 kWh — on its own more than the 0.90 kWh the car attributed to ALL
    parked drain since the last charge, a window containing that gap and
    others. Where a short gap lands is rounding, not measurement.
    """
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    from app.analysis.driving import vampire_drain

    cap = 69.5
    base = datetime.fromisoformat("2026-08-13T08:00")

    def drive(start, mins, s_soc, e_soc, odo, dist=10.0):
        return SimpleNamespace(
            id=int(odo), start_time=start, end_time=start + timedelta(minutes=mins),
            start_soc=s_soc, end_soc=e_soc, distance_km=dist, duration_min=mins,
            start_odo_km=odo, end_odo_km=odo + dist, start_park_min=None,
            energy_used_kwh=1.4, avg_speed_kmh=30.0)

    # Long gaps to fit the parked rate from (STANDBY_MIN_TOTAL_HOURS), each
    # ~20 h apart losing 1 SoC point — about 0.035 kW.
    drives = []
    soc = 90.0
    for i in range(5):
        start = base + timedelta(hours=20 * i)
        drives.append(drive(start, 20, soc, soc - 2, 1000.0 + 20 * i))
        soc -= 3.0                       # 2 driving + 1 parked over the gap

    long_only = vampire_drain(drives, [], cap)
    assert long_only["kwh"] > 0

    # Now insert a SHORT gap that happens to straddle two integer boundaries:
    # 1.7 hours reading a 2-point drop, which no 1.7-hour park can really be.
    short_start = drives[-1].end_time + timedelta(hours=1.7)
    drives.append(drive(short_start, 20, drives[-1].end_soc - 2.0,
                        drives[-1].end_soc - 4.0, 1200.0))
    got = vampire_drain(drives, [], cap)

    added = got["kwh"] - long_only["kwh"]
    # The naive reading would add 2 points = 1.39 kWh. The modelled drain over
    # 1.7 h is under a tenth of that.
    assert added < 0.2, f"short gap contributed {added:.3f} kWh"
    assert added > 0, "a real park still costs something"

    # And a gap long enough to measure keeps its measurement rather than being
    # replaced by the model — the substitution is about resolution, not a
    # preference for the fit.
    far = drives[-1].end_time + timedelta(hours=40)
    drives.append(drive(far, 20, drives[-1].end_soc - 3.0,
                        drives[-1].end_soc - 5.0, 1300.0))
    with_long = vampire_drain(drives, [], cap)
    added_long = with_long["kwh"] - got["kwh"]
    # Its measured 3 points, not the ~1.5 kWh the fitted rate would give over
    # 40 h. (Loose tolerance: adding this gap also re-fits the rate slightly,
    # which nudges the modelled short gap above.)
    assert added_long == pytest.approx(3.0 / 100.0 * cap, rel=0.05)


def test_data_quality_will_not_call_a_reconstructed_trip_measured():
    """Measured live and the reason this exists: trip 397 recovered 7.078 km of
    a 10.339 km drive, had its energy replaced by hand from the car's own
    screen, and still reported "measured" — because the label only ever looked
    at whether idle was live-tracked.

    Distance counts too. Ground no poll saw has its energy projected from the
    rest of the trip, not read off it.
    """
    from types import SimpleNamespace

    from app.analysis.driving import _data_quality

    def trip(distance, recovered=0.0, est=None, idle=True, energy=1.5):
        # wh_per_km is a computed property on the real model, so the stand-in
        # has to carry it — has_valid_energy reads it before anything else.
        return SimpleNamespace(
            distance_km=distance, start_recovered_km=recovered, end_est_km=est,
            idle_tracked=idle, energy_used_kwh=energy, duration_min=25.0,
            avg_speed_kmh=25.0,
            wh_per_km=(energy * 1000.0 / distance) if distance else 0.0)

    # Trip 397's shape: two thirds of it never observed.
    assert _data_quality(trip(10.339, recovered=7.078)) == "estimated"
    # Ordinary boundary recovery of a few hundred metres is still a measured
    # trip — the threshold has to leave room for the normal case.
    assert _data_quality(trip(10.339, recovered=0.3)) == "measured"
    # An estimated arrival counts the same way a recovered start does.
    assert _data_quality(trip(4.0, est=1.2)) == "estimated"
    assert _data_quality(trip(4.0, est=0.15)) == "measured"
    # And the existing rules are untouched.
    assert _data_quality(trip(10.0, idle=False)) == "estimated"
    assert _data_quality(trip(10.0, energy=0.0)) == "incomplete"


def _chain(legs, gap_hours: float = 10.0):
    """Drives laid end to end with a fixed park between each pair.

    ``legs`` is (place the drive ENDS at, start_soc, end_soc), so the gap
    after drive i is worth ``legs[i].end_soc - legs[i+1].start_soc`` points and
    belongs to legs[i]'s place. Written out explicitly because a helper that
    built pairs left spurious gaps BETWEEN the pairs, with SoC deltas nobody
    had chosen.
    """
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    base = datetime(2026, 7, 1)
    out = []
    for i, (place, start_soc, end_soc) in enumerate(legs):
        at = base + timedelta(hours=(gap_hours + 0.5) * i)
        out.append(SimpleNamespace(
            id=i, start_time=at, end_time=at + timedelta(minutes=30),
            start_soc=start_soc, end_soc=end_soc,
            end_location=place, start_park_min=None))
    return out


def test_standby_rate_is_fitted_per_place():
    """A single whole-history rate described nowhere this car parks: Home
    measured 0.035 kW and the places where Sentry stays armed 0.230, and the
    blend of 0.079 was wrong in both directions. Each place gets its own."""
    drives = _chain([
        ("Home",   100.0, 99.0),
        ("Resort",  99.0, 98.0),   # Home gap:   0 points
        ("Home",    94.0, 93.0),   # Resort gap: 4 points
        ("Resort",  92.0, 91.0),   # Home gap:   1 point
        ("Home",    87.0, 86.0),   # Resort gap: 4 points
        ("Resort",  86.0, 85.0),   # Home gap:   0 points
        ("Home",    81.0, 80.0),   # Resort gap: 4 points
    ])
    home = driving_analysis.place_standby_kw(drives, [], 68.4, "Home")
    resort = driving_analysis.place_standby_kw(drives, [], 68.4, "Resort")
    blended = driving_analysis.standby_kw(drives, [], 68.4)

    assert home is not None and resort is not None and blended is not None
    assert resort > blended > home            # the blend fits neither
    assert resort > home * 3

    # A place with no history of its own is not given someone else's.
    assert driving_analysis.place_standby_kw(drives, [], 68.4, "Nowhere") is None
    assert driving_analysis.place_standby_kw(drives, [], 68.4, None) is None


def test_a_soc_rise_disqualifies_a_gap_from_the_standby_fit():
    """An unlogged charge shows up as SoC reading higher at the far end of a
    park. max(drop, 0) used to score it as zero drain while keeping every one
    of its hours in the denominator, so a missed charge diluted the rate
    instead of being excluded the way a logged one is."""
    legs = [("Home", 100.0 - 2 * i, 99.0 - 2 * i) for i in range(6)]
    baseline = driving_analysis.standby_kw(_chain(legs), [], 68.4)
    assert baseline is not None

    # Same history, then a park that gained 15 points — a charge the log never
    # saw (measured: a real one, 30 July at the Office, worth 68 points).
    charged = driving_analysis.standby_kw(
        _chain(legs + [("Home", 104.0, 103.0)]), [], 68.4)
    assert charged == baseline

    # A single point the other way is the pack's own estimate wandering, and
    # is still a park: kept, and scored as no measurable drain.
    wobble = driving_analysis.standby_kw(
        _chain(legs + [("Home", 90.0, 89.0)]), [], 68.4)
    assert wobble is not None and wobble < baseline


def test_the_parked_rate_is_fitted_from_the_car_not_from_the_window():
    """A standby rate is a property of the car. Fitted from whatever window is
    on screen it needs 24 hours of qualifying gaps inside that window, which a
    short one has not got — so the same overnight park reported a modelled
    figure on a 30-day window and a whole raw SoC point on "since charge".

    Measured, trip 415: a 10.7-hour park at Home read 0.68 kWh where Home's
    own 0.035 kW models 0.37, because the since-charge window held six trips
    and could fit no rate at all."""
    # Home as it actually reads: most overnight parks move no whole point at
    # all, and the rate is only visible as a sum across many of them.
    history = _chain([
        ("Home", 100.0, 99.0), ("Home", 98.0, 97.0),   # gap 1 point
        ("Home", 97.0, 96.0),                          # gap 0
        ("Home", 95.0, 94.0),                          # gap 1
        ("Home", 94.0, 93.0),                          # gap 0
        ("Home", 92.0, 91.0),                          # gap 1
        ("Home", 91.0, 90.0), ("Home", 90.0, 89.0),    # gaps 0, 0
        ("Home", 89.0, 88.0),                          # gap 0
    ])
    assert driving_analysis.standby_kw(history, [], 68.4) is not None

    # The reported window is two drives: one gap, nowhere near enough to fit
    # anything, and the one gap in it happens to have moved a whole point.
    window = history[:2]
    assert driving_analysis.standby_kw(window, [], 68.4) is None

    windowed = driving_analysis.vampire_drain(window, [], 68.4)
    from_car = driving_analysis.vampire_drain(
        window, [], 68.4, rate_history=(history, []))

    # Same gap, same hours — only the rate behind it differs.
    assert windowed["hours"] == from_car["hours"] > 0
    # Unable to fit, the window reports the raw whole SoC point. Given the
    # car's own history the modelled drain is smaller, and is used instead.
    assert windowed["kwh"] == pytest.approx(0.684, rel=0.01)
    assert from_car["kwh"] < windowed["kwh"]


def test_a_place_can_be_told_what_it_draws_when_the_fit_cannot_measure_it():
    """The fit reads whole-percent SoC across parked gaps. Where Sentry is off
    that is not an instrument: seven Home nights from seven read a full point
    where the car's own screen implies 14 W and predicts one in five, and the
    residue is the pack's SoC estimate settling as it cools — a bias, so more
    gaps make it no better. A figure read off the car outranks the fit."""
    history = _chain([("Home", 100.0 - 2 * i, 99.0 - 2 * i) for i in range(8)])
    window = history[:2]                       # one 10-hour gap
    fitted = driving_analysis.vampire_drain(
        window, [], 68.6, rate_history=(history, []))
    told = driving_analysis.vampire_drain(
        window, [], 68.6, rate_history=(history, []),
        place_rates={"Home": 0.014})

    assert told["hours"] == fitted["hours"] > 0        # same gap
    assert told["kwh"] < fitted["kwh"]                 # cheaper, and measured
    assert told["kwh"] == pytest.approx(0.014 * told["hours"], rel=0.02)

    # A place with no figure of its own is unaffected by another place's.
    elsewhere = driving_analysis.vampire_drain(
        window, [], 68.6, rate_history=(history, []),
        place_rates={"Office": 0.014})
    assert elsewhere["kwh"] == fitted["kwh"]


def test_a_park_is_priced_by_its_sentry_state_not_by_the_place_it_happened_at():
    """Place was standing in for Sentry, and a stand-in is what it was. Trip
    448 parked at the resort — a place fitted entirely from armed parks at
    220 W — with Sentry off. The gap was priced at 220 W, which put the
    modelled drain past a whole SoC point, so the raw point was reported
    instead: 0.69 kWh, more than the car attributed to every park since its
    last charge."""
    from datetime import timedelta
    from types import SimpleNamespace

    # A resort where every logged park armed Sentry and lost 4 points a night.
    legs, soc = [], 100.0
    for _ in range(4):
        legs += [("Resort", soc, soc - 1.0)]
        soc -= 5.0                                  # 4 points across the gap
    history = _chain(legs, gap_hours=11.0)

    def state_after(drives, armed_upto):
        """One reading inside each gap — armed for the first ``armed_upto``.

        Keyed off the drive that OPENS each gap, so no two land on the same
        timestamp: an earlier version put an armed and a disarmed reading on
        the same instant and any() quietly called the gap armed.
        """
        return [SimpleNamespace(ts=d.end_time + timedelta(hours=1),
                                sentry_mode=i < armed_upto)
                for i, d in enumerate(drives[:-1])]

    armed = driving_analysis.sentry_standby_kw(
        history, [], 68.6, state_after(history, len(history)), True)
    assert armed is not None and armed > 0.15

    # Now one more park at the same place with Sentry off.
    # 84 -> 83 across the extra gap: one point, a quiet night.
    quiet = _chain(legs + [("Resort", 83.0, 82.0)], gap_hours=11.0)
    quiet_readings = state_after(quiet, len(quiet) - 2)
    window = quiet[-2:]

    by_place = driving_analysis.vampire_drain(
        window, [], 68.6, rate_history=(quiet, []))
    by_state = driving_analysis.vampire_drain(
        window, [], 68.6, rate_history=(quiet, []),
        place_rates={"Resort": 0.014}, readings=quiet_readings)

    assert by_state["hours"] == by_place["hours"] > 0
    # The armed rate is too big for the substitution to fire, so the place
    # route reports the raw quantised point; the state route models it.
    assert by_place["kwh"] == pytest.approx(0.686, rel=0.02)
    # kwh is reported to two decimals, so compare at that resolution.
    assert by_state["kwh"] == pytest.approx(0.014 * by_state["hours"], abs=0.01)


def test_recovered_parked_minutes_are_not_charged_twice():
    """Two mechanisms cover a parked gap and they were overlapping. The
    short-gap substitution replaces an unmeasurable SoC drop with rate x hours;
    the add-back restores minutes the trip after the gap gave up when its
    departure recovery took the pre-gap SoC as its baseline. Those minutes are
    INSIDE the gap, so substituting across all of it and then adding them again
    bills them twice — trip 451, a 1.9 h park at Home with 113 of its 114
    minutes recovered, reported 0.05 kWh where 14 W over 1.9 h is 0.027."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    base = datetime(2026, 8, 25, 17, 26)
    a = SimpleNamespace(id=1, start_time=base - timedelta(minutes=30), end_time=base,
                        start_soc=72.0, end_soc=70.0, end_location="Home",
                        start_park_min=None)
    b_start = base + timedelta(hours=1.9)
    b = SimpleNamespace(id=2, start_time=b_start,
                        end_time=b_start + timedelta(minutes=18),
                        start_soc=70.0, end_soc=68.0, end_location="Resort",
                        start_park_min=113.0)          # nearly the whole gap

    out = driving_analysis.vampire_drain([a, b], [], 68.6,
                                         place_rates={"Home": 0.014})
    assert out["hours"] == pytest.approx(1.9, abs=0.05)
    # rate x the WHOLE gap, once: the substitution takes the sliver the drop
    # spans and the add-back takes the recovered minutes.
    assert out["kwh"] == pytest.approx(0.014 * 1.9, abs=0.01)

    # With nothing recovered the substitution still covers the whole gap.
    b_plain = SimpleNamespace(**{**b.__dict__, "start_park_min": None})
    plain = driving_analysis.vampire_drain([a, b_plain], [], 68.6,
                                           place_rates={"Home": 0.014})
    assert plain["kwh"] == pytest.approx(0.014 * 1.9, abs=0.01)
