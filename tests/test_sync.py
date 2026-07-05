"""Tests for the snapshot session state machine (app/sync.py)."""
from app.sync import process_snapshot, snapshot_from_vehicle_data

T0 = 1_760_000_000.0  # seconds epoch


def snap(ts, odo_km, soc, shift="P", speed=0.0, charging=False, kw=0.0,
         fast=False, present=False, locked=False, lat=None, lon=None,
         range_km=None, energy_added=0.0):
    return {
        "ts": ts, "odo_km": odo_km, "soc": soc, "shift": shift,
        "speed_kmh": speed, "charging": charging, "charger_kw": kw,
        "fast": fast, "out_temp": 28.0, "user_present": present,
        "locked": locked, "lat": lat, "lon": lon, "range_km": range_km,
        "energy_added_kwh": energy_added,
    }


def step(prev, cur, trip=None, charge=None):
    return process_snapshot(prev, cur, trip, charge, 60.0, 0.90)


def test_snapshot_parses_vehicle_data_ms_timestamp_and_miles():
    data = {
        "drive_state": {"timestamp": 1_760_000_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72, "charging_state": "Disconnected"},
        "climate_state": {"outside_temp": 31.5},
        "vehicle_state": {"odometer": 6215.0},
    }
    s = snapshot_from_vehicle_data(data)
    assert s["ts"] == 1_760_000_000.0          # ms -> s
    assert abs(s["odo_km"] - 6215.0 * 1.60934) < 0.01
    assert s["soc"] == 72 and s["out_temp"] == 31.5


def test_trip_opens_spans_snapshots_and_closes_on_park():
    """One drive across four snapshots = exactly one logged entry."""
    s1 = snap(T0, 10_000.0, 80)                               # parked at home
    s2 = snap(T0 + 600, 10_005.0, 78, shift="D", speed=60)    # driving
    s3 = snap(T0 + 1200, 10_015.0, 75, shift="D", speed=90)   # still driving
    s4 = snap(T0 + 1800, 10_024.9, 72)                        # back to P

    d, c, trip, charge = step(None, s1)
    assert (d, c, trip, charge) == ([], [], None, None)

    d, c, trip, charge = step(s1, s2)
    assert d == [] and trip is not None          # trip opened, nothing logged
    assert trip["odo_km"] == 10_000.0            # anchored at the parked snapshot

    d, c, trip, charge = step(s2, s3, trip)
    assert d == [] and trip is not None          # still open
    assert trip["max_speed"] == 90               # max speed tracked

    d, c, trip, charge = step(s3, s4, trip)
    assert trip is None and len(d) == 1          # closed on P
    (drive,) = d
    assert drive["distance_km"] == 24.9          # full span, not fragments
    assert drive["duration_min"] == 30.0
    assert abs(drive["energy_used_kwh"] - 4.8) < 1e-6   # 8% of 60 kWh
    assert drive["max_speed_kmh"] == 90


def test_two_drives_split_across_an_unseen_nap():
    """Drive → park+lock+sleep (unpolled) → drive must be TWO trips, not one.

    The poller can't read a sleeping car, so it never sees the power-down; a long
    blind gap between two driving snapshots is treated as that missed stop.
    """
    s1 = snap(T0, 10_000.0, 80)                                  # parked at home
    s2 = snap(T0 + 300, 10_005.0, 78, shift="D", speed=60)       # drive 1 moving (5 km)
    # 25-min blind gap: car parked & slept — odometer unchanged (it didn't move).
    s3 = snap(T0 + 300 + 1500, 10_005.0, 76, shift="D", speed=40)  # drive 2 resumes
    s4 = snap(T0 + 300 + 1500 + 600, 10_013.0, 74, locked=True)    # drive 2 ends (8 km)

    _, _, trip, _ = step(s1, s2)
    assert trip is not None                       # drive 1 open
    d, _, trip, _ = step(s2, s3, trip)
    assert len(d) == 1                            # drive 1 closed at the last seen point
    assert d[0]["distance_km"] == 5.0            # 10000 -> 10005
    assert trip is not None                       # drive 2 now open
    assert trip["odo_km"] == 10_005.0            # started fresh at the resume snapshot
    d, _, trip, _ = step(s3, s4, trip)
    assert trip is None and len(d) == 1          # drive 2 closed on lock
    assert d[0]["distance_km"] == 8.0            # 10005 -> 10013


def test_trailing_park_excluded_even_with_driver_aboard():
    """A ~11 min drive then a long sit with the driver still aboard (A/C on) must
    log an ~11 min trip — not 30+ — with the parked idle time/energy excluded."""
    s1 = snap(T0, 10_000.0, 80)
    s2 = snap(T0 + 660, 10_010.0, 76, shift="D", speed=50, present=True)  # driving, 10 km
    s3 = snap(T0 + 720, 10_010.0, 76, present=True)          # parked, driver aboard (stop)
    s4 = snap(T0 + 720 + 1200, 10_010.0, 73, present=True)   # 20 min later, still parked

    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    d, _, trip, _ = step(s2, s3, trip)
    assert d == [] and trip is not None          # brief stop — trip stays open
    d, _, trip, _ = step(s3, s4, trip)
    assert trip is None and len(d) == 1          # sat past PARK_END_MIN → closed at the stop
    assert d[0]["distance_km"] == 10.0
    assert d[0]["duration_min"] == 12.0          # T0 -> stop(T0+720) = 12 min, not 32


def test_trip_survives_brief_stop_and_closes_on_power_down():
    """A stop with the driver still inside keeps the trip open; leaving ends it."""
    s1 = snap(T0, 10_000.0, 80, lat=3.10, lon=101.60)
    s2 = snap(T0 + 600, 10_010.0, 77, shift="D", speed=70, present=True)
    s3 = snap(T0 + 1200, 10_010.5, 77, present=True)  # parked, driver inside
    s4 = snap(T0 + 1800, 10_020.0, 74, shift="D", speed=80, present=True)
    s5 = snap(T0 + 2400, 10_025.0, 73, lat=3.15, lon=101.71)  # driver gone

    d, c, trip, charge = step(s1, s2)
    assert trip is not None
    d, c, trip, charge = step(s2, s3, trip)
    assert d == [] and trip is not None          # brief stop does NOT cut the trip
    d, c, trip, charge = step(s3, s4, trip)
    assert d == [] and trip is not None
    d, c, trip, charge = step(s4, s5, trip)
    assert trip is None and len(d) == 1          # closed only on power-down
    (drive,) = d
    assert drive["distance_km"] == 25.0          # the whole errand run, one entry
    assert drive["avg_speed_kmh"] == 37.5        # 25 km over 40 min
    assert drive["start_location"] == "3.1000, 101.6000"
    assert drive["end_location"] == "3.1500, 101.7100"


def test_locked_car_ends_the_trip_even_with_presence_lag():
    """Parked + locked = drive over, even if presence detection still says yes."""
    s1 = snap(T0, 10_000.0, 80)
    s2 = snap(T0 + 600, 10_010.0, 77, shift="D", speed=70, present=True)
    s3 = snap(T0 + 1200, 10_012.0, 76, present=True, locked=True)  # locked up

    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    d, _, trip, _ = step(s2, s3, trip)
    assert trip is None and len(d) == 1
    assert d[0]["distance_km"] == 12.0


def test_snapshot_parses_user_present_and_position():
    data = {
        "drive_state": {"timestamp": 1_760_000_000_000, "shift_state": "D",
                        "speed": 40, "latitude": 3.0733, "longitude": 101.6067},
        "charge_state": {"battery_level": 72},
        "vehicle_state": {"odometer": 6215.0, "is_user_present": True},
    }
    s = snapshot_from_vehicle_data(data)
    assert s["user_present"] is True
    assert s["lat"] == 3.0733 and s["lon"] == 101.6067


def test_live_trip_reports_progress():
    from app.sync import live_trip

    trip = {"ts": T0, "odo_km": 10_000.0, "soc": 80, "max_speed": 95}
    now = snap(T0 + 1800, 10_030.0, 74, shift="D", speed=80, present=True)
    lt = live_trip(trip, now, capacity_kwh=60.0)
    assert lt["distance_km"] == 30.0
    assert lt["duration_min"] == 30
    assert lt["avg_speed_kmh"] == 60.0
    assert lt["soc_used"] == 6
    assert lt["km_per_soc"] == 5.0
    assert lt["energy_kwh"] == 3.6                 # 6% of 60 kWh
    assert lt["wh_per_km"] == 120                  # 3.6 kWh over 30 km
    assert live_trip(None, now) is None


def test_live_trip_km_per_soc_from_energy_on_short_drive():
    """A short live drive (integer SoC unchanged) still reports km/1%."""
    from app.sync import live_trip

    # 6 km, range 400->395.2 km (fractional), SoC still reads 80.
    trip = {"ts": T0, "odo_km": 10_000.0, "soc": 80, "range_km": 400.0, "max_speed": 55}
    now = snap(T0 + 600, 10_006.0, 80, shift="D", speed=50, range_km=395.2)
    lt = live_trip(trip, now, capacity_kwh=75.0)
    assert lt["soc_used"] == 0.0                    # integer SoC didn't move
    assert lt["km_per_soc"] is not None and lt["km_per_soc"] > 0  # from energy


def test_trip_closes_when_charging_starts_not_merging_across_a_charge():
    """drive -> plug in -> drive must be two trips, not one merged 0-energy trip."""
    s1 = snap(T0, 10_000.0, 60, range_km=300.0)
    s2 = snap(T0 + 300, 10_004.0, 59, shift="D", speed=50, present=True, range_km=295.0)
    s3 = snap(T0 + 900, 10_004.0, 59, charging=True, kw=50, range_km=295.0)  # plugged in

    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    d, c, trip, charge = step(s2, s3, trip)
    # The 4 km drive closes cleanly at plug-in — energy from the pre-charge
    # range delta, so Wh/km is real (not diluted by the coming charge).
    assert trip is None and len(d) == 1
    assert d[0]["distance_km"] == 4.0
    assert d[0]["energy_used_kwh"] > 0


def test_contaminated_low_energy_drive_flagged_unknown():
    """A drive whose range was refilled mid-trip (Wh/km < 40) logs energy 0."""
    # 8 km but range only dropped 300.0 -> 299.5 (a charge refilled it): the
    # implied ~0.12 kWh / 8 km ≈ 15 Wh/km is impossible, so energy -> unknown.
    from app.sync import _drive_from

    start = snap(T0, 10_000.0, 60, range_km=300.0)
    end = snap(T0 + 1200, 10_008.0, 60, range_km=299.5)
    d = _drive_from(start, end, 75.0)
    assert d["distance_km"] == 8.0
    assert d["energy_used_kwh"] == 0.0     # flagged unknown, not a wrong 15 Wh/km


def test_charge_stays_open_until_it_stops():
    """A charge across snapshots = one entry, no 10-minute fragments."""
    c1 = snap(T0, 10_000.0, 60)
    c2 = snap(T0 + 600, 10_000.0, 65, charging=True, kw=11)
    c3 = snap(T0 + 1800, 10_000.0, 74, charging=True, kw=11)
    c4 = snap(T0 + 3600, 10_000.0, 78)

    d, c, trip, charge = step(None, c1)
    d, c, trip, charge = step(c1, c2, charge=charge)
    assert c == [] and charge is not None        # opened, anchored at c1
    d, c, trip, charge = step(c2, c3, charge=charge)
    assert c == [] and charge is not None        # still charging, nothing logged
    d, c, trip, charge = step(c3, c4, charge=charge)
    assert charge is None and len(c) == 1
    (chg,) = c
    assert abs(chg["energy_added_kwh"] - 10.8) < 1e-6   # 60 -> 78 = 18% of 60 kWh
    assert chg["charge_type"] == "AC"
    assert abs(chg["cost"] - 9.72) < 1e-6
    assert chg["duration_min"] == 60.0


def test_energy_prefers_fine_grained_range_delta():
    """A short trip must not be quantised to whole battery percents.

    7 km at ~120 Wh/km really uses ~1.4% of a 60 kWh pack, but the integer
    battery_level only ticks from 80 to 79 (= 1% = 0.6 kWh = 86 Wh/km).
    The fractional rated-range delta captures the true energy instead.
    """
    s1 = snap(T0, 10_000.0, 80, range_km=400.0)  # full pack projects 500 km
    s2 = snap(T0 + 300, 10_003.0, 80, shift="D", speed=50, present=True,
              range_km=396.5)
    s3 = snap(T0 + 900, 10_007.0, 79, locked=True, range_km=393.0)

    _, _, trip, _ = step(s1, s2)
    d, _, trip, _ = step(s2, s3, trip)
    (drive,) = d
    # Δrange = 7 km of rated range on a 500 km projection = 1.4% = 0.84 kWh.
    assert abs(drive["energy_used_kwh"] - 0.84) < 0.01
    # Without range data the same trip would read 0.6 kWh (1% of 60 kWh).

    # Charges gain the same precision: +2.5% by SoC but Δrange says +2.35%.
    c1 = snap(T0, 10_000.0, 60, charging=True, kw=11, range_km=300.0)
    c2 = snap(T0 + 1800, 10_000.0, 62, range_km=311.75)
    _, c, _, _ = step(c1, c2, charge={"ts": c1["ts"], "soc": 60,
                                      "range_km": 300.0, "max_kw": 11,
                                      "fast": False})
    (chg,) = c
    assert abs(chg["energy_added_kwh"] - 1.404) < 0.01  # 11.75/502.7*60.05... fine-grained


def test_max_speed_never_below_average():
    """A drive with no mid-drive snapshot must not report max speed 0."""
    s1 = snap(T0, 10_000.0, 80)                    # parked
    s2 = snap(T0 + 600, 10_001.0, 80, shift="D")   # in gear, speed not seen
    s3 = snap(T0 + 1800, 10_020.0, 76, locked=True)  # already parked & locked

    _, _, trip, _ = step(s1, s2)
    d, _, trip, _ = step(s2, s3, trip)
    (drive,) = d
    assert drive["avg_speed_kmh"] == 40.0          # 20 km over 30 min
    assert drive["max_speed_kmh"] == 40.0          # floored at the average

    from app.sync import live_trip
    lt = live_trip({"ts": T0, "odo_km": 10_000.0, "soc": 80},
                   snap(T0 + 1800, 10_020.0, 76, shift="D"))
    assert lt["max_speed_kmh"] == 40.0             # avg floors the live max too


def test_gap_fallback_logs_merged_sessions():
    """Everything missed between two parked snapshots still gets logged."""
    prev = snap(T0, 10_000.0, 80)
    cur = snap(T0 + 7200, 10_030.0, 85)  # drove 30 km AND charged while unseen
    d, c, trip, charge = step(prev, cur)
    assert len(d) == 1 and len(c) == 1
    assert d[0]["distance_km"] == 30.0
    assert c[0]["energy_added_kwh"] == 3.0       # +5% of 60 kWh
    assert trip is None and charge is None


def test_implied_capacity_from_measured_charge():
    from app.sync import implied_capacity_kwh

    # Tesla measured 18.5 kWh for a 55->80% (25%) charge => 74 kWh usable.
    c = {"energy_measured": True, "start_soc": 55, "end_soc": 80, "energy_added_kwh": 18.5}
    assert implied_capacity_kwh(c) == 74.0
    # SoC-estimate charges are ignored (calibrating from them is circular).
    assert implied_capacity_kwh({**c, "energy_measured": False}) is None
    # Small gains are too quantised to trust.
    assert implied_capacity_kwh({"energy_measured": True, "start_soc": 70,
                                 "end_soc": 78, "energy_added_kwh": 6.0}) is None
    # Implausible results are clamped out (e.g. a metering glitch).
    assert implied_capacity_kwh({"energy_measured": True, "start_soc": 20,
                                 "end_soc": 80, "energy_added_kwh": 90.0}) is None


def test_charge_records_location():
    """A charge session picks up the car's position (for the locations card)."""
    c1 = snap(T0, 10_000.0, 60)
    c2 = snap(T0 + 600, 10_000.0, 65, charging=True, kw=11, lat=3.16, lon=101.71)
    c3 = snap(T0 + 1800, 10_000.0, 74)
    d, c, trip, charge = step(None, c1)
    d, c, trip, charge = step(c1, c2, charge=charge)
    d, c, trip, charge = step(c2, c3, charge=charge)
    (chg,) = c
    assert chg["location"] == "3.1600, 101.7100"


def test_charge_location_falls_back_to_type_without_gps():
    """No GPS (no location scope) still groups by charger type, not blank."""
    a1 = snap(T0, 10_000.0, 60)
    a2 = snap(T0 + 600, 10_000.0, 65, charging=True, kw=7)      # AC, no lat/lon
    a3 = snap(T0 + 1800, 10_000.0, 74)
    _, _, _, charge = step(None, a1)
    _, _, _, charge = step(a1, a2, charge=charge)
    _, c, _, _ = step(a2, a3, charge=charge)
    assert c[0]["location"] == "AC / home charger"

    d1 = snap(T0, 10_000.0, 40)
    d2 = snap(T0 + 600, 10_000.0, 50, charging=True, kw=150, fast=True)
    d3 = snap(T0 + 1800, 10_000.0, 70)
    _, _, _, charge = step(None, d1)
    _, _, _, charge = step(d1, d2, charge=charge)
    _, c, _, _ = step(d2, d3, charge=charge)
    assert c[0]["location"] == "DC fast charger"


def test_charge_uses_teslas_measured_energy():
    """When Tesla reports charge_energy_added, use it instead of estimating."""
    c1 = snap(T0, 10_000.0, 60)
    c2 = snap(T0 + 600, 10_000.0, 65, charging=True, kw=11, energy_added=3.2)
    c3 = snap(T0 + 1800, 10_000.0, 74, charging=True, kw=11, energy_added=11.9)
    c4 = snap(T0 + 3600, 10_000.0, 78, energy_added=15.4)  # meter at session end

    d, c, trip, charge = step(None, c1)
    d, c, trip, charge = step(c1, c2, charge=charge)   # opens; baseline 3.2
    d, c, trip, charge = step(c2, c3, charge=charge)
    d, c, trip, charge = step(c3, c4, charge=charge)
    (chg,) = c
    # Measured: 15.4 − 3.2 = 12.2 kWh (not the ~10.8 the SoC estimate would give).
    assert abs(chg["energy_added_kwh"] - 12.2) < 1e-6
    assert abs(chg["cost"] - 12.2 * 0.90) < 1e-6


def test_fast_charge_flag_makes_dc():
    prev = snap(T0, 10_000.0, 40, charging=True, kw=150, fast=True)
    cur = snap(T0 + 1500, 10_000.0, 75)
    _, c, _, _ = step(prev, cur)
    assert c[0]["charge_type"] == "DC"
    assert c[0]["max_power_kw"] == 150


def test_driving_wh_per_km_removes_idle_load():
    from app.sync import driving_wh_per_km

    # Stop-go case (peak well above the average → real idle): 3.2 km / 18 min in
    # 33°C, 0.81 kWh total (253 Wh/km), avg 11 but peaked ~43 km/h. Stripping the
    # idle/AC load brings it near Tesla's ~150.
    est = driving_wh_per_km(0.81, 3.2, 18, 33, avg_speed_kmh=11, max_speed_kmh=43)
    assert 135 <= est <= 175          # around Tesla's 149.5, not 253
    assert est < 253

    # Steady crawl (no peak above the average → NO idle): a slow but continuous
    # trip must NOT be trimmed — driving == total.
    total = round(0.81 * 1000.0 / 3.2)
    steady = driving_wh_per_km(0.81, 3.2, 18, 33, avg_speed_kmh=11, max_speed_kmh=12)
    assert steady == total

    # Steady highway (no idle): unchanged, never inflated.
    hw = driving_wh_per_km(5.0, 33.0, 22, 25, avg_speed_kmh=90, max_speed_kmh=110)
    assert hw == round(5.0 * 1000.0 / 33.0)

    # Degenerate inputs return None.
    assert driving_wh_per_km(0, 5, 10, 25) is None
    assert driving_wh_per_km(1.0, 0, 10, 25) is None


def test_no_change_logs_nothing():
    prev = snap(T0, 10_000.0, 80)
    cur = snap(T0 + 600, 10_000.0, 80)
    assert step(prev, cur) == ([], [], None, None)
