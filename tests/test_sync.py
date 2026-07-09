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
    from app.sync import AC_CHARGE_EFFICIENCY, implied_capacity_kwh

    # Tesla measured 18.5 kWh for a 55->80% (25%) charge on a Supercharger
    # (DC, no onboard-charger conversion loss) => 74 kWh usable, unadjusted.
    c = {"energy_measured": True, "start_soc": 55, "end_soc": 80,
         "energy_added_kwh": 18.5, "charge_type": "DC"}
    assert implied_capacity_kwh(c) == 74.0
    # The identical session on AC (home/destination) loses ~5% to the
    # onboard charger's AC->DC conversion, so the raw energy_added figure
    # overstates what actually reached the pack — corrected down.
    ac = {**c, "charge_type": "AC"}
    assert implied_capacity_kwh(ac) == round(74.0 * AC_CHARGE_EFFICIENCY, 1)
    # Charge type missing (legacy data) is treated as AC (the common case,
    # and the safer assumption — DC should always be tagged explicitly).
    assert implied_capacity_kwh({k: v for k, v in c.items() if k != "charge_type"}) == \
        round(74.0 * AC_CHARGE_EFFICIENCY, 1)
    # SoC-estimate charges are ignored (calibrating from them is circular).
    assert implied_capacity_kwh({**c, "energy_measured": False}) is None
    # Small gains are too quantised to trust.
    assert implied_capacity_kwh({"energy_measured": True, "start_soc": 70,
                                 "end_soc": 78, "energy_added_kwh": 6.0,
                                 "charge_type": "AC"}) is None
    # Implausible results are clamped out (e.g. a metering glitch).
    assert implied_capacity_kwh({"energy_measured": True, "start_soc": 20,
                                 "end_soc": 80, "energy_added_kwh": 90.0,
                                 "charge_type": "AC"}) is None


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
    d, c, trip, charge = step(c1, c2, charge=charge)   # opens
    d, c, trip, charge = step(c2, c3, charge=charge)
    d, c, trip, charge = step(c3, c4, charge=charge)
    (chg,) = c
    # Full meter reading (15.4 kWh), not 15.4-3.2=12.2: the 3.2 kWh already on
    # the meter when we first saw charging=True was delivered during the poll
    # gap before plug-in was noticed — it's real energy of *this* session
    # (Tesla resets the meter to ~0 at the true plug-in), not a stale prior
    # reading to subtract away.
    assert abs(chg["energy_added_kwh"] - 15.4) < 1e-6
    assert abs(chg["cost"] - 15.4 * 0.90) < 1e-6


def test_fast_dc_charge_missed_at_plugin_is_not_undercounted():
    """A DC session caught a few minutes late must not lose the energy
    delivered before we noticed — at 100+ kW that's several kWh per missed
    minute, the biggest real-world case of this class of bug."""
    c1 = snap(T0, 10_000.0, 20)                                          # parked, unplugged
    # 5-min poll gap; by the time we see charging=True the fast charger has
    # already put ~8.3 kWh on the meter (100 kW for ~5 min).
    c2 = snap(T0 + 300, 10_000.0, 27, charging=True, kw=100, fast=True, energy_added=8.3)
    c3 = snap(T0 + 900, 10_000.0, 55, energy_added=25.0)                 # session ends

    d, c, trip, charge = step(None, c1)
    d, c, trip, charge = step(c1, c2, charge=charge)
    d, c, trip, charge = step(c2, c3, charge=charge)
    (chg,) = c
    # The full 25.0 kWh delivered, not 25.0-8.3=16.7 — the pre-poll 8.3 kWh
    # actually reached the battery and must count.
    assert abs(chg["energy_added_kwh"] - 25.0) < 1e-6
    assert chg["charge_type"] == "DC"


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


def test_track_idle_only_counts_sustained_stops():
    """A stop only counts as idle once sustained >= IDLE_STREAK_MIN (3 min) —
    a brief red-light stop must not be counted, only a genuine sustained one."""
    from app.sync import _track_idle

    # Short stop: 90s at 0 km/h, then moving again — must NOT count.
    open_trip = {"idle_min": 0.0, "zero_since": None}
    _track_idle(open_trip, snap(T0, 10_000.0, 80, shift="D", speed=0))
    assert open_trip["zero_since"] == T0
    _track_idle(open_trip, snap(T0 + 90, 10_000.0, 80, shift="D", speed=50))
    assert open_trip["idle_min"] == 0.0          # too short to count
    assert open_trip["zero_since"] is None        # streak reset

    # Sustained stop: 4 minutes at 0 km/h, then moving again — must count.
    _track_idle(open_trip, snap(T0 + 200, 10_005.0, 79, shift="D", speed=0))
    assert open_trip["zero_since"] == T0 + 200
    _track_idle(open_trip, snap(T0 + 200 + 240, 10_005.0, 79, shift="D", speed=40))
    assert open_trip["idle_min"] == 4.0           # 240s = 4 min, confirmed
    assert open_trip["zero_since"] is None


def test_live_trip_uses_real_tracked_idle_not_speed_heuristic():
    """live_trip's driving_wh_per_km should reflect actually-observed stopped
    time (from _track_idle), not the old avg/max-speed estimate — a genuine
    sustained stop lowers it below the raw wh_per_km."""
    from app.sync import live_trip, process_snapshot

    s1 = snap(T0, 10_000.0, 80, shift="D", speed=60, range_km=400.0)
    _, _, trip, _ = process_snapshot(None, s1, None, None, 60.0, 0.90)
    assert trip is not None

    # Sustained 4-minute stop mid-trip.
    s2 = snap(T0 + 300, 10_005.0, 79, shift="D", speed=0, range_km=395.0)
    _, _, trip, _ = process_snapshot(s1, s2, trip, None, 60.0, 0.90)
    s3 = snap(T0 + 300 + 240, 10_005.0, 79, shift="D", speed=50, range_km=395.0)
    _, _, trip, _ = process_snapshot(s2, s3, trip, None, 60.0, 0.90)
    assert trip["idle_min"] == 4.0

    now = snap(T0 + 300 + 240 + 300, 10_030.0, 74, shift="D", speed=60, range_km=380.0)
    lt = live_trip(trip, now, capacity_kwh=60.0)
    assert lt["wh_per_km"] is not None
    assert lt["driving_wh_per_km"] < lt["wh_per_km"]   # idle energy subtracted


def test_arrival_after_signal_gap_does_not_inflate_duration():
    """Poor signal on arrival: the car is only polled well after it parked, so
    the first parked reading's timestamp is the sync time, not the real stop.
    The trip must end near the actual stop (estimated from the distance driven
    after the last poll), not balloon out to the sync time."""
    s1 = snap(T0, 10_000.0, 80)                              # home, parked
    s2 = snap(T0 + 300, 10_005.0, 78, shift="D", speed=60)   # driving
    s3 = snap(T0 + 600, 10_010.0, 76, shift="D", speed=60)   # still driving (last poll)
    # No poll on arrival (~T0+900, weak signal). First sync 28 min later: parked.
    s4 = snap(T0 + 2280, 10_013.0, 75, locked=True)
    _, _, trip, _ = step(s1, s2)
    _, _, trip, _ = step(s2, s3, trip)
    d, _, trip, _ = step(s3, s4, trip)
    assert trip is None and len(d) == 1
    drive = d[0]
    assert drive["distance_km"] == 13.0
    # Duration from the 3 km driven after the last poll, not the full ~38 min
    # to sync time; average speed stays a realistic road pace, not a parked ~20.
    assert drive["duration_min"] < 20
    assert drive["avg_speed_kmh"] > 40


def test_power_on_polled_late_does_not_inflate_start():
    """Poor signal at power-on: the first driving reading arrives long after the
    car set off. The start must be estimated from the odometer, not stretched
    back to the stale parked reading's timestamp."""
    s1 = snap(T0, 10_000.0, 80)                               # parked at home
    # 20-min gap, then the first driving reading: car moved 7 km (drove part of
    # it, parked the rest) — implied ~21 km/h, below a steady city pace.
    s2 = snap(T0 + 1200, 10_007.0, 78, shift="D", speed=55)
    s3 = snap(T0 + 1800, 10_012.0, 76, locked=True)           # arrives & parks
    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    # Start moved forward from the 20-min-old parked reading toward power-on,
    # by ~7 km / 30 km/h ≈ 14 min back from the first driving reading — not the
    # full 20 min that anchoring to the stale parked reading would have counted.
    assert trip["ts"] > s1["ts"]
    assert (s2["ts"] - trip["ts"]) / 60.0 <= 15
    d, _, trip, _ = step(s2, s3, trip)
    assert trip is None and len(d) == 1
    assert d[0]["avg_speed_kmh"] > 20             # realistic, not a parked crawl


def test_power_on_estimate_uses_observed_speed_not_flat_assumption():
    """The pace used to back-estimate power-on time should reflect the actual
    speed seen at the first driving reading, not always a flat 30 km/h — the
    same real-evidence-over-assumption model already used on the arrival side.
    A car already doing 90 km/h when first seen implies a faster pace across
    the gap, and therefore a *later* (more accurate) power-on estimate."""
    s1 = snap(T0, 10_000.0, 80)                                # parked at home
    # 20-min gap, then first driving reading already at 90 km/h — clearly on a
    # fast road, not crawling out of the driveway.
    s2 = snap(T0 + 1200, 10_007.0, 78, shift="D", speed=90)
    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    # pace = max(90*0.65, 30) = 58.5 km/h -> 7 km / 58.5 km/h ≈ 7.2 min back,
    # tighter than the flat-30 estimate's ~14 min.
    back_min = (s2["ts"] - trip["ts"]) / 60.0
    assert 5 < back_min < 10


def test_stale_prev_does_not_backdate_open_trip_start():
    """A drive seen right after an overnight park must anchor its start to *now*,
    not to last night's stale snapshot (which would add hours of idle time)."""
    prev = snap(T0, 10_000.0, 80, range_km=400.0)               # parked last night
    # 10 hours later the car is seen driving (barely moved since = it was parked).
    cur = snap(T0 + 36_000, 10_000.3, 79, shift="D", speed=40, range_km=393.0)
    _, _, trip, _ = step(prev, cur)
    assert trip is not None
    assert trip["odo_km"] == cur["odo_km"]       # started here, not at prev
    assert trip["ts"] == cur["ts"]               # start time is now, not 10h ago


def test_stale_gap_fallback_reestimates_timing_and_energy():
    """A short morning drive reconstructed across an overnight gap must not read
    as hours long, nor count the night's vampire drain as trip energy."""
    prev = snap(T0, 10_000.0, 80, range_km=400.0)               # parked 8pm
    # Next morning: drove 4.3 km and is parked again. The range fell 400->393,
    # but most of that 7 km of range is overnight drain, not the 4.3 km drive.
    cur = snap(T0 + 36_000, 10_004.3, 79, range_km=393.0)
    d, _, trip, _ = step(prev, cur)
    assert trip is None and len(d) == 1
    drive = d[0]
    assert drive["distance_km"] == 4.3
    # Duration re-estimated from distance (~4.3 km at city pace), not 600 min.
    assert drive["duration_min"] < 30
    # Energy from current rated consumption, well under the drain-inflated 0.84.
    assert 0 < drive["energy_used_kwh"] < 0.7
    # Start back-dated only by the estimated drive time, not to last night.
    assert drive["start_time"].hour == drive["end_time"].hour


def test_gap_fallback_keeps_real_timing_when_prev_is_fresh():
    """A genuine drive-through-gap (car actually moving, recent prev) is left
    intact — only stale overnight anchors get re-estimated."""
    prev = snap(T0, 10_000.0, 80, range_km=400.0)
    cur = snap(T0 + 1800, 10_030.0, 76, range_km=380.0)  # 30 km in 30 min, real
    d, _, _, _ = step(prev, cur)
    assert len(d) == 1
    assert d[0]["distance_km"] == 30.0
    assert d[0]["duration_min"] == 30.0          # untouched, not re-estimated


def test_no_change_logs_nothing():
    prev = snap(T0, 10_000.0, 80)
    cur = snap(T0 + 600, 10_000.0, 80)
    assert step(prev, cur) == ([], [], None, None)


def test_trip_closes_immediately_when_car_locks():
    """A trip closes immediately when the car locks, even if parked for 0 seconds.
    This covers the user scenario: arrive at destination, lock the car, sync.
    Trip must close and appear in the drive list."""
    s1 = snap(T0, 10_000.0, 80)                                    # parked at home
    s2 = snap(T0 + 600, 10_010.0, 77, shift="D", speed=70)         # driving
    s3 = snap(T0 + 1200, 10_015.0, 75, shift="D", speed=60)        # still driving
    s4 = snap(T0 + 1800, 10_017.5, 73, shift="P", locked=True)     # arrive & lock

    _, _, trip, _ = step(None, s1)
    assert trip is None

    _, _, trip, _ = step(s1, s2)
    assert trip is not None

    _, _, trip, _ = step(s2, s3, trip)
    assert trip is not None

    # The critical test: car locks immediately after arriving.
    d, _, trip, _ = step(s3, s4, trip)
    assert trip is None, "Trip should close when car locks"
    assert len(d) == 1, "Trip should be logged as a completed drive"
    assert d[0]["distance_km"] == 17.5
    assert 0 < d[0]["energy_used_kwh"]  # Should have valid energy data


def test_trip_tracks_unlock_event_sequence():
    """Track door unlock → shift sequence to confirm driving intent.

    Unlock-before-drive flag signals high confidence that the trip is real
    (not accidental gear shifting or brief driveway movement)."""
    s1 = snap(T0, 10_000.0, 80, locked=True)                   # locked at home
    s2 = snap(T0 + 60, 10_000.0, 80, locked=False, shift="D")  # unlocked → shift D
    s3 = snap(T0 + 300, 10_010.0, 77, shift="D", speed=70)     # driving

    _, _, trip, _ = step(None, s1)
    assert trip is None

    # Transition from locked to unlocked with shift change
    _, _, trip, _ = step(s1, s2)
    assert trip is not None, "Trip should open on unlock + shift"
    assert trip.get("unlocked_before_drive") is True, "Should detect unlock event"

    _, _, trip, _ = step(s2, s3, trip)
    assert trip is not None
    assert trip.get("unlocked_before_drive") is True, "Should preserve unlock flag"


def test_close_trip_on_sleep_uses_last_snapshot_as_the_end():
    """A car can't reach true sleep mid-drive, so an open trip is definitely
    over once it does — close using the last successful read as the end,
    not a guess."""
    from app.sync import _dt, close_trip_on_sleep

    open_trip = {"ts": T0, "odo_km": 10_000.0, "soc": 80, "max_speed": 70.0}
    last_snapshot = snap(T0 + 900, 10_012.0, 76)  # last read before it went asleep
    d = close_trip_on_sleep(open_trip, last_snapshot, 60.0)
    assert d is not None
    assert d["distance_km"] == 12.0
    assert d["duration_min"] == 15.0
    # Anchored at the last real reading's own timestamp, not a guess.
    assert d["end_time"] == _dt(T0 + 900)
    assert d["start_time"] == _dt(T0)
