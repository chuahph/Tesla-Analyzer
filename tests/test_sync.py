"""Tests for the snapshot session state machine (app/sync.py)."""
import pytest

from app.sync import (_energy_kwh, close_trip_on_sleep, is_driving,
                      process_snapshot, snapshot_from_vehicle_data)

T0 = 1_760_000_000.0  # seconds epoch


def snap(ts, odo_km, soc, shift="P", speed=0.0, charging=False, kw=0.0,
         fast=False, present=False, locked=False, lat=None, lon=None,
         range_km=None, energy_added=0.0, car_wash_mode=False):
    return {
        "ts": ts, "odo_km": odo_km, "soc": soc, "shift": shift,
        "speed_kmh": speed, "charging": charging, "charger_kw": kw,
        "fast": fast, "out_temp": 28.0, "user_present": present,
        "locked": locked, "lat": lat, "lon": lon, "range_km": range_km,
        "energy_added_kwh": energy_added, "car_wash_mode": car_wash_mode,
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


def test_snapshot_parses_sentry_and_climate_as_none_when_unreported():
    """sentry_mode/climate_on/cabin_overheat_protection(_actively_cooling)
    are None (not False) when Tesla's payload omits the field entirely — an
    older car/software or a permission gap — so a caller can tell "unknown"
    apart from a confirmed off. Present and true when Tesla does report
    them. cabin_overheat_protection ("Off"/"On"/"FanOnly") is the COP
    *setting*; cabin_overheat_protection_actively_cooling is the separate
    live "is it really running" flag — they're independent fields, so a car
    with COP left enabled as a setting but not currently triggered reports
    "On" alongside actively_cooling=False."""
    unreported = snapshot_from_vehicle_data({
        "drive_state": {"timestamp": 1_760_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72},
        "climate_state": {},
        "vehicle_state": {},
    })
    assert unreported["sentry_mode"] is None
    assert unreported["climate_on"] is None
    assert unreported["cabin_overheat_protection"] is None
    assert unreported["cabin_overheat_protection_actively_cooling"] is None

    reported = snapshot_from_vehicle_data({
        "drive_state": {"timestamp": 1_760_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72},
        "climate_state": {
            "is_climate_on": True, "cabin_overheat_protection": "FanOnly",
            "cabin_overheat_protection_actively_cooling": True,
        },
        "vehicle_state": {"sentry_mode": True},
    })
    assert reported["sentry_mode"] is True
    assert reported["cabin_overheat_protection"] == "FanOnly"
    assert reported["cabin_overheat_protection_actively_cooling"] is True
    assert reported["climate_on"] is True

    # The common real-world case this bug was about: COP left "On" as a
    # permanent setting but not actually triggered right now.
    enabled_but_idle = snapshot_from_vehicle_data({
        "drive_state": {"timestamp": 1_760_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72},
        "climate_state": {
            "is_climate_on": False, "cabin_overheat_protection": "On",
            "cabin_overheat_protection_actively_cooling": False,
        },
        "vehicle_state": {"sentry_mode": True},
    })
    assert enabled_but_idle["cabin_overheat_protection"] == "On"
    assert enabled_but_idle["cabin_overheat_protection_actively_cooling"] is False


def test_snapshot_parses_door_and_window_openings():
    """doors_open/windows_open summarise Tesla's per-door and per-window ints
    (0 = shut) for the parked-intrusion alert, and stay None when the payload
    omits them entirely so "unknown" is distinguishable from "all shut"."""
    def snap_vs(vs):
        return snapshot_from_vehicle_data({
            "drive_state": {"timestamp": 1_760_000_000, "shift_state": "P"},
            "charge_state": {"battery_level": 72},
            "climate_state": {},
            "vehicle_state": vs,
        })

    unreported = snap_vs({})
    assert unreported["doors_open"] is None
    assert unreported["windows_open"] is None

    all_shut = snap_vs({"df": 0, "dr": 0, "pf": 0, "pr": 0, "ft": 0, "rt": 0,
                        "fd_window": 0, "fp_window": 0, "rd_window": 0, "rp_window": 0})
    assert all_shut["doors_open"] is False
    assert all_shut["windows_open"] is False

    # Any single opening flips its own summary, and only its own.
    rear_door = snap_vs({"df": 0, "dr": 1, "pf": 0, "pr": 0, "fd_window": 0})
    assert rear_door["doors_open"] is True
    assert rear_door["windows_open"] is False

    trunk = snap_vs({"df": 0, "rt": 1})
    assert trunk["doors_open"] is True

    window = snap_vs({"df": 0, "rp_window": 1})
    assert window["doors_open"] is False
    assert window["windows_open"] is True


def test_snapshot_parses_dashcam_and_display_state():
    """Both are logged-only probes (see BatteryReading) for whether a Sentry
    trigger shows up in the API at all — captured when reported, None when
    not, so a later look-back can tell "unknown" from a real value."""
    absent = snapshot_from_vehicle_data({
        "drive_state": {"timestamp": 1_760_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72},
        "climate_state": {},
        "vehicle_state": {},
    })
    assert absent["dashcam_state"] is None
    assert absent["center_display_state"] is None

    present = snapshot_from_vehicle_data({
        "drive_state": {"timestamp": 1_760_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72},
        "climate_state": {},
        "vehicle_state": {"dashcam_state": "Recording", "center_display_state": 4},
    })
    assert present["dashcam_state"] == "Recording"
    assert present["center_display_state"] == 4


def test_snapshot_parses_car_wash_mode():
    off = snapshot_from_vehicle_data({
        "drive_state": {"timestamp": 1_760_000_000, "shift_state": "P"},
        "charge_state": {"battery_level": 72},
        "climate_state": {},
        "vehicle_state": {},
    })
    assert off["car_wash_mode"] is False

    on = snapshot_from_vehicle_data({
        "drive_state": {"timestamp": 1_760_000_000, "shift_state": "N"},
        "charge_state": {"battery_level": 72},
        "climate_state": {},
        "vehicle_state": {"car_wash_mode": True},
    })
    assert on["car_wash_mode"] is True


def test_is_driving_ignores_shift_and_speed_during_car_wash_mode():
    """Car Wash Mode shifts to Neutral (and the conveyor can nudge the car a
    little) purely so it can be moved through the wash — that's never a real
    drive, however shift/speed reads while it's active."""
    assert not is_driving(snap(T0, 10_000.0, 80, shift="N", speed=3.0, car_wash_mode=True))
    assert is_driving(snap(T0, 10_000.0, 80, shift="N", speed=3.0))  # same, mode off: still driving


def test_car_wash_mode_after_parking_does_not_reopen_the_trip():
    """Park, lock, then run Car Wash Mode (shift -> N, a few metres of
    conveyor creep) — must stay parked, not read as a new drive starting."""
    s1 = snap(T0, 10_000.0, 80)                                            # parked at home
    s2 = snap(T0 + 600, 10_010.0, 77, shift="D", speed=60)                 # driving
    s3 = snap(T0 + 1200, 10_017.5, 73, shift="P", locked=True)             # arrive & lock
    s4 = snap(T0 + 1500, 10_017.6, 73, shift="N", car_wash_mode=True)      # car wash creep

    _, _, trip, _ = step(None, s1)
    assert trip is None
    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    d, _, trip, _ = step(s2, s3, trip)
    assert trip is None and len(d) == 1               # closed on the lock
    d, _, trip, _ = step(s3, s4, trip)
    assert trip is None and d == []                    # wash creep opened nothing new


def test_charge_from_rejects_soc_recalibration_blip_with_negligible_real_energy():
    """A BMS SoC recalibration (Tesla's SoC is an estimate, not a direct
    measurement — it can nudge by a whole integer point right after a
    vehicle software reset, with ~0 real energy added) alone clears
    CHARGE_MIN_PCT on a raw-SoC-delta session, but the session's own
    measured kWh (Tesla's persistent charge_energy_added meter) stays
    negligible — the independent CHARGE_MIN_KWH floor rejects it, so a
    reboot-adjacent blip can't masquerade as a real (and "since last
    charge"-anchoring) charge session."""
    from app.sync import _charge_from

    start = {"ts": T0, "soc": 58, "energy_added_kwh": 0.0, "odo_km": 10_000.0}
    # 1 minute later, parked the whole time: raw SoC reads one point higher
    # (recalibration, not real charging) while Tesla's own session meter
    # shows only a negligible real draw (a charge-port test pulse).
    cur = {"ts": T0 + 60, "soc": 59, "energy_added_kwh": 0.03,
           "odo_km": 10_000.0, "out_temp": 28.0}
    assert _charge_from(start, cur, 75.0, 0.90) is None

    # Same shape, but a real session (meter proves several real kWh) must
    # still be accepted — the new floor only catches negligible sessions.
    real_cur = {**cur, "energy_added_kwh": 5.0}
    real = _charge_from(start, real_cur, 75.0, 0.90)
    assert real is not None
    assert real["energy_added_kwh"] == 5.0


def test_charging_flag_ignored_mid_drive_regen_soc_uptick():
    """Charging can never coincide with the car actively driving. A stray
    charging=True reading mid-trip — the reported real-world cause: a
    regen-braking SoC uptick (Tesla's SoC readout isn't perfectly
    monotonic while driving) briefly misread as "started charging", which
    logged a phantom AC session at neither trip endpoint with SoC going
    the wrong way — must not open/log a charge, and the trip must stay
    one uninterrupted entry rather than being corrupted by the glitch."""
    s1 = snap(T0, 10_000.0, 96, shift="D", speed=40)          # driving
    # Momentary glitch: still driving, but charging flips true and SoC
    # ticks up 1 point (regen) — a physically impossible combination.
    s2 = snap(T0 + 60, 10_010.0, 97, shift="D", speed=35, charging=True)
    s3 = snap(T0 + 600, 10_020.0, 94, shift="D", speed=60)    # driving resumes normally
    s4 = snap(T0 + 900, 10_030.0, 92)                         # parked, trip ends

    _, _, trip, charge = step(None, s1)
    assert trip is not None and charge is None

    d, c, trip, charge = step(s1, s2, trip, charge)
    assert d == [] and c == []       # no phantom charge logged
    assert charge is None            # never opened despite charging=True
    assert trip is not None          # trip stays open, uninterrupted

    d, c, trip, charge = step(s2, s3, trip, charge)
    assert d == [] and c == [] and charge is None

    d, c, trip, charge = step(s3, s4, trip, charge)
    assert trip is None and len(d) == 1   # one continuous trip, not split
    assert d[0]["distance_km"] == 30.0    # 10000 -> 10030, uncorrupted by the glitch
    assert c == []


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


def test_short_gap_departure_keeps_pre_poll_stretch():
    """Reported live (checked against the car's own trip meter): a 4.1 km
    drive logged as 3.6 km, its kWh short by the same first stretch.

    The last parked reading was only a short poll gap old when the first
    *driving* reading caught the car already 0.4 km out at a low implied
    speed (still pulling out of the neighbourhood) — was_parked rightly
    starts the trip's clock at cur, but anchoring odometer/SoC/range there
    too discards that pull-out. A parked car cannot move, so the odometer
    gain between prev and cur is this trip's own first stretch and must be
    kept: baseline from prev, clock from cur.
    """
    s1 = snap(T0, 28_163.0, 52, range_km=234.0)                # parked
    # 2-min gap: first driving poll lands 0.4 km out (implied 12 km/h < 15).
    s2 = snap(T0 + 120, 28_163.4, 52, shift="D", speed=45, range_km=233.5)
    s3 = snap(T0 + 300, 28_165.5, 51, shift="D", speed=50, range_km=231.5)
    s4 = snap(T0 + 420, 28_167.1, 51, locked=True, range_km=230.9)

    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    assert trip["odo_km"] == 28_163.0            # baseline at the parked reading
    assert trip["soc"] == 52
    assert trip["range_km"] == 234.0             # energy baseline moves with it

    _, _, trip, _ = step(s2, s3, trip)
    d, _, trip, _ = step(s3, s4, trip)
    (drive,) = d
    assert drive["distance_km"] == 4.1           # full 28163.0 -> 28167.1 span
    # Energy from the parked baseline's range delta, not the mid-departure one.
    full = 100.0 * (234.0 + 230.9) / (52 + 51)
    assert abs(drive["energy_used_kwh"] - (234.0 - 230.9) / full * 60.0) < 0.01


def test_parked_reanchor_carries_range_km_for_energy():
    """The ≥5-min-gap start correction restored odo and SoC from the parked
    prev but forgot range_km — and _energy_kwh derives energy from the range
    delta *first*, so the trip's energy stayed anchored at the first driving
    reading (and mixed prev's SoC with cur's range when projecting the full
    pack). The whole baseline must move together."""
    s1 = snap(T0, 10_000.0, 60, range_km=270.0)                # parked
    # 6-min gap, 1.2 km out (implied 12 km/h): the est-start correction runs.
    s2 = snap(T0 + 360, 10_001.2, 60, shift="D", speed=45, range_km=268.9)

    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    assert trip["odo_km"] == 10_000.0
    assert trip["soc"] == 60
    assert trip["range_km"] == 270.0             # was left at 268.9 before
    # Clock back-estimated from the 1.2 km at the departure pace, not left at
    # the first driving reading. Derived from the constant rather than a
    # literal: what this pins is that the estimate RUNS, and pinning the pace
    # here as well made it look like a second measurement of it.
    from app.sync import DEPARTURE_PACE_KMH
    pace = max(45 * 0.65, DEPARTURE_PACE_KMH)
    assert trip["ts"] == pytest.approx(s2["ts"] - 1.2 / pace * 3600.0)
    assert trip["ts"] < s2["ts"]


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


def test_drive_min_km_is_configurable():
    """A genuinely tiny move (a car nudged while parked) sits below the
    trip floor and is filtered as jitter; a real short move (e.g. charger to
    parking spot) clears it and logs — but raising drive_min_km must be able
    to filter it too, for anyone who'd rather not see moves that short at all."""
    from app.sync import _drive_from

    jitter_start = snap(T0, 10_000.0, 91, range_km=453.0)
    jitter_end = snap(T0 + 60, 10_000.05, 91, range_km=452.97)  # 0.05 km jitter

    short_start = snap(T0, 10_000.0, 91, range_km=453.0)
    short_end = snap(T0 + 180, 10_000.4, 91, range_km=452.8)    # 0.4 km, 3 min

    assert _drive_from(jitter_start, jitter_end, 75.0) is None             # below the trip floor -> filtered
    d = _drive_from(short_start, short_end, 75.0)
    assert d is not None and d["distance_km"] == 0.4                       # a real short move logs
    assert _drive_from(short_start, short_end, 75.0, drive_min_km=0.5) is None  # raised floor filters it again


def test_wake_and_lock_nudge_is_not_logged_as_a_trip():
    """Unlocking/accessing a parked car (phone-as-key wake) can tick the
    odometer a couple of tenths with no real driving. That must NOT log as a
    phantom "0 min, Home -> Home" trip — it stays in the parked gap, counted
    as standby drain, not a drive. Regression for the reported 0.2 km trip."""
    from app.sync import _drive_from

    # 0.2 km odometer nudge over ~1 min, essentially no SoC change.
    start = snap(T0, 28_437.0, 66, range_km=300.0)
    end = snap(T0 + 60, 28_437.2, 66, range_km=300.0)
    assert _drive_from(start, end, 75.0) is None

    # And through the whole-gap reconstruction path process_snapshot uses.
    d, _, _, _ = process_snapshot(start, end, None, None, 75.0, 0.90)
    assert d == []


def test_short_real_trip_rounding_to_0_3km_is_not_discarded():
    """A genuine short trip (charger bay to parking spot) that Tesla's own
    screen displays as "0.3 km" must log, even when the true odometer delta
    underneath that rounded figure is a hair below 0.3 — the floor test must
    compare against the same rounded distance shown in the result, not the
    raw float. Regression for a real 0.3/0.1kWh/1min trip the analyzer
    dropped entirely (Tesla's own "Since Charge" panel confirmed it)."""
    from app.sync import _drive_from

    start = snap(T0, 28_466.0, 81, range_km=453.0)
    end = snap(T0 + 60, 28_466.298, 81, range_km=452.9)  # rounds to 0.3 km, true value < 0.3
    d = _drive_from(start, end, 75.0)
    assert d is not None
    assert d["distance_km"] == 0.3

    d, _, _, _ = process_snapshot(start, end, None, None, 75.0, 0.90)
    assert len(d) == 1 and d[0]["distance_km"] == 0.3


def test_tail_trim_seconds_are_recorded_on_the_drive():
    """When the pace-based correction back-dates a trip's stop, the seconds it
    trimmed must be recorded on the drive. That correction takes distance and
    energy down with the timestamp, so a trip reading short against the car's
    own screen needs to be answerable after the fact instead of unknowable."""
    s1 = snap(T0, 10_000.0, 80, shift="P", speed=0.0, range_km=400.0)
    s2 = snap(T0 + 300, 10_010.0, 79, shift="D", speed=50.0, range_km=395.0)
    # 10-min gap covering only 2 km (12 km/h implied — well under a driving
    # pace), then found parked and locked: the correction should decide the
    # car actually stopped early in that gap.
    s3 = snap(T0 + 900, 10_012.0, 78, shift="P", speed=0.0, locked=True, range_km=394.0)

    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    drives, _, trip, _ = step(s2, s3, trip)

    assert trip is None and len(drives) == 1
    trimmed = drives[0]["tail_trim_sec"]
    assert trimmed is not None and trimmed >= 60
    # The recorded trim must match the gap actually removed from the tail:
    # the trip ends that many seconds before the reading that closed it.
    from app.sync import _dt
    gap_removed = (_dt(T0 + 900) - drives[0]["end_time"]).total_seconds()
    assert abs(gap_removed - trimmed) < 1.0


def test_departure_during_a_blackout_keeps_its_distance_in_the_trip():
    """A car that sets off during an unpolled gap is anchored at the first
    *driving* reading, which would leave the pre-departure stretch stranded in
    the parked gap as standby drain. The odo/SoC recovery pulls it back onto
    the trip using prev's own real reading — no projection, totals conserved.

    Guard check behind this: was_parked can only be true when the gap's implied
    speed is under PARK_SPEED_KMH (15), while the recovery needs it under
    CITY_SPEED_KMH (30) — so the guard can never block a case the stale anchor
    itself created."""
    s1 = snap(T0, 1000.0, 80, range_km=400.0)
    # 20-min blackout; the car departs partway and covers 3 km before it's seen.
    s2 = snap(T0 + 1200, 1003.0, 79, shift="D", speed=45.0, range_km=396.0)
    s3 = snap(T0 + 1800, 1010.0, 78, shift="P", speed=0.0, locked=True, range_km=392.0)

    _, _, trip, _ = step(s1, s2)
    assert trip["odo_km"] == 1000.0          # recovered to prev, not left at 1003
    drives, _, _, _ = step(s2, s3, trip)
    assert drives[0]["distance_km"] == 10.0  # full s1->s3 delta, nothing stranded


def test_start_lost_km_records_distance_dropped_before_the_anchor():
    """The counterpart to tail_trim_sec, and the harder end to see: distance
    driven before a trip's start anchor is simply absent from it, and because
    the odometer is continuous nothing anywhere looks wrong — the trip just
    reads short against the car's own meter.

    0.0 must mean "nothing lost", including the case where the departure
    recovery pulled a blackout departure back in, so a nonzero value is
    unambiguous evidence rather than something needing interpretation."""
    # Blackout departure: 3 km driven before the first driving reading, which
    # the recovery restores — so nothing is lost and it must say so.
    s1 = snap(T0, 1000.0, 80, range_km=400.0)
    s2 = snap(T0 + 1200, 1003.0, 79, shift="D", speed=45.0, range_km=396.0)
    s3 = snap(T0 + 1800, 1010.0, 78, shift="P", speed=0.0, locked=True, range_km=392.0)

    _, _, trip, _ = step(s1, s2)
    drives, _, _, _ = step(s2, s3, trip)
    assert drives[0]["distance_km"] == 10.0        # full delta, recovery worked
    assert drives[0]["start_lost_km"] == 0.0       # and it reports nothing lost

    # Ordinary case: anchored at the previous reading, nothing can precede it.
    p1 = snap(T0, 2000.0, 80, range_km=400.0)
    p2 = snap(T0 + 60, 2000.5, 80, shift="D", speed=30.0, range_km=399.8)
    p3 = snap(T0 + 600, 2005.0, 79, shift="P", speed=0.0, locked=True, range_km=398.0)
    _, _, trip2, _ = step(p1, p2)
    drives2, _, _, _ = step(p2, p3, trip2)
    assert drives2[0]["start_lost_km"] == 0.0

    # A poor-signal departure: the previous reading itself already read as
    # driving (stale/glitched, not a confirmed park), then a long low-movement
    # gap, then driving again for real. Bounded by DEPARTURE_GAP_MAX_KM, so a
    # small amount like this (1.0 km) folds back in — nothing lost, distance
    # and energy both intact.
    q1 = snap(T0, 1000.0, 80, shift="D", speed=30.0, range_km=400.0)
    q2 = snap(T0 + 1200, 1001.0, 79, shift="D", speed=40.0, range_km=396.0)
    q3 = snap(T0 + 1800, 1006.0, 78, shift="P", speed=0.0, locked=True, range_km=393.0)
    _, _, trip3, _ = step(q1, q2)
    drives3, _, _, _ = step(q2, q3, trip3)
    assert drives3[0]["start_lost_km"] == 0.0
    assert drives3[0]["distance_km"] == 6.0        # full q1->q3 delta, recovered

    # The case that motivated raising the cap past GAP_CREEP_MAX_KM in the
    # first place: confirmed live, a departure through a hillside stretch
    # (poor coverage from the driveway itself, not just at the destination)
    # lost 1.11 km before the first tracked reading arrived — comfortably past
    # the old 1.0 km bound, still well short of a genuine second trip. Must
    # now recover fully, matching the real production case rather than
    # reporting a loss for a trip that was never actually split.
    u1 = snap(T0, 3000.0, 80, shift="D", speed=20.0, range_km=400.0)
    u2 = snap(T0 + 1200, 3001.11, 78, shift="D", speed=45.0, range_km=394.0)
    u3 = snap(T0 + 2700, 3009.61, 76, shift="P", speed=0.0, locked=True, range_km=388.0)
    _, _, trip_u, _ = step(u1, u2)
    drives_u, _, _, _ = step(u2, u3, trip_u)
    assert drives_u[0]["start_lost_km"] == 0.0
    assert drives_u[0]["distance_km"] == 9.6        # full u1->u3 delta, recovered (rounded)

    # Right at the new cap: still recovers (inclusive boundary).
    v1 = snap(T0, 4000.0, 80, shift="D", speed=20.0, range_km=400.0)
    v2 = snap(T0 + 1200, 4003.0, 78, shift="D", speed=45.0, range_km=394.0)
    v3 = snap(T0 + 2700, 4009.0, 76, shift="P", speed=0.0, locked=True, range_km=388.0)
    _, _, trip_v, _ = step(v1, v2)
    drives_v, _, _, _ = step(v2, v3, trip_v)
    assert drives_v[0]["start_lost_km"] == 0.0
    assert drives_v[0]["distance_km"] == 9.0        # full v1->v3 delta, recovered

    # Past the cap, recovery must NOT fire — a large amount looking "parked" by
    # average speed alone is more likely a genuinely separate, still-open
    # earlier trip the gap never caught closing, not a missed departure.
    # Asserting the loss rather than papering over it: this is the case
    # start_lost_km exists to expose, not one this fix is meant to touch.
    r1 = snap(T0, 2000.0, 80, shift="D", speed=25.0, range_km=400.0)
    r2 = snap(T0 + 7200, 2003.5, 78, shift="D", speed=35.0, range_km=396.0)
    r3 = snap(T0 + 7800, 2009.5, 76, shift="P", speed=0.0, locked=True, range_km=392.0)
    _, _, trip4, _ = step(r1, r2)
    drives4, _, _, _ = step(r2, r3, trip4)
    assert drives4[0]["start_lost_km"] == 3.5
    assert drives4[0]["distance_km"] == 6.0
    assert drives4[0]["distance_km"] + drives4[0]["start_lost_km"] == 9.5


def test_closed_trip_at_prevs_odometer_unblocks_a_long_blackout_departure():
    """One blackout can cost both ends of a boundary: it hides the arrival, so
    prev stays frozen mid-drive, and that frozen prev then disarms the recovery
    at the NEXT departure — past DEPARTURE_GAP_MAX_KM the trip starts wherever
    the network came back.

    Trip 359, measured against the car: arriving Home the park was never seen,
    the next departure lost 10.092 km, and the trip read 17.2 km from a street
    10 km downroad against the car's own 27.2 km from Home.

    A closed trip ending at prev's odometer settles what is_driving(prev) could
    only guess at — the earlier journey IS accounted for — so the cap doesn't
    apply."""
    home = (5.4100, 100.3000)
    # Home, in Drive: the poll that would have seen P never arrived.
    p = snap(T0, 29_318.155, 84, shift="D", speed=0.0, range_km=380.0,
             lat=home[0], lon=home[1])
    # 49 minutes later, seen driving 10.092 km away — implied 12 km/h, so the
    # car sat for most of the gap and drove the rest.
    c = snap(T0 + 2947.5, 29_328.247, 82, shift="D", speed=60.0, range_km=368.0,
             lat=5.3754, lon=100.2980)
    end = snap(T0 + 2947.5 + 2600, 29_345.414, 79.8, shift="P", speed=0.0,
               locked=True, range_km=352.0)

    # Without the closed trip's end there is nothing to distinguish this from an
    # unlogged journey, so it must still decline — and, having declined, must
    # not backdate the clock over the distance it just refused either.
    _, _, strict, _ = process_snapshot(p, c, None, None, 60.0, 0.90)
    assert strict["odo_km"] == 29_328.247
    assert strict["start_lost_km"] == 10.092
    assert strict["ts"] == c["ts"], "a declined recovery must not move the clock"

    # With it, the departure is recovered whole: odometer, position and clock.
    _, _, trip, _ = process_snapshot(p, c, None, None, 60.0, 0.90,
                                     prev_close_odo_km=29_318.155)
    assert trip["odo_km"] == 29_318.155
    assert trip["start_lost_km"] == 0.0
    assert trip["start_recovered_km"] == 10.092
    assert (trip["lat"], trip["lon"]) == home, "start must move back to Home too"
    assert trip["ts"] < c["ts"], "clock follows the distance it now owns"

    drives, _, trip, _ = process_snapshot(c, end, trip, None, 60.0, 0.90)
    assert trip is None and len(drives) == 1
    # The car's own screen: 27.2 km. 29,345.414 - 29,318.155 = 27.259.
    assert drives[0]["distance_km"] == pytest.approx(27.3, abs=0.05)
    assert drives[0]["start_lost_km"] == 0.0


def test_departure_recovery_anchors_past_an_estimated_tail_not_behind_it():
    """When the closed trip took an estimated tail beyond prev's own reading,
    ITS end is where unclaimed ground starts. Anchoring at prev's raw odometer
    would hand this trip metres the previous one already counted — the same
    double-count the boundary repairs exist to undo."""
    p = snap(T0, 1000.0, 80, shift="D", speed=0.0, range_km=400.0)
    c = snap(T0 + 2400, 1008.0, 78, shift="D", speed=50.0, range_km=392.0)
    # The closed trip estimated 0.4 km of arrival tail past prev's reading.
    _, _, trip, _ = process_snapshot(p, c, None, None, 60.0, 0.90,
                                     prev_close_odo_km=1000.4)
    assert trip["odo_km"] == 1000.4                 # not 1000.0
    assert trip["start_recovered_km"] == 7.6        # 1008.0 - 1000.4


def test_departure_premium_does_not_scale_with_a_long_blind_stretch():
    """The opening-minutes premium is a fixed cost — hot cabin pulled down,
    cold drivetrain, car park crawl — and is over long before a 10 km blind
    stretch is. Applying 1.55x across the whole stretch prices a front-loaded
    cost as if it scaled with distance.

    Trip 359 against the car: 10.092 km blind of a 27.26 km drive, 2.91 kWh
    measured over the rest, and the car's own 6.9% of a 69.5 kWh pack = 4.79
    kWh."""
    from app.sync import DEPARTURE_PREMIUM_MAX_KM, energy_for_blind_distance

    priced = energy_for_blind_distance(2.91, 27.26, 10.092, departure_blind_km=10.092)
    assert priced == pytest.approx(4.79, abs=0.15)

    # Three trips with a blind head, each against the car's own consumption.
    # The premium is confined to the first kilometre because the whole-stretch
    # ratio collapses as the stretch lengthens — 1.10, 0.92, 1.10 at 3-10 km
    # against 1.54/1.56 at ~1 km, which is a fixed front-load, not a
    # proportional one.
    for raw, span, blind, car in (
            (2.91, 27.258, 10.091, 4.796),   # trip 359
            (0.873, 11.332, 4.791, 1.460),   # trip 366
            (1.50, 11.406, 2.981, 2.085)):   # trip 378
        got = energy_for_blind_distance(raw, span, blind, departure_blind_km=blind)
        assert got == pytest.approx(car, rel=0.10), f"{got:.2f} vs the car's {car}"

    # Short departures — every trip the 1.55 was fitted on — are under the cap
    # and must price exactly as they did before it existed.
    short = energy_for_blind_distance(10.0, 20.0, 1.0, departure_blind_km=1.0)
    assert short == pytest.approx(10.0 * (19.0 + 1.55) / 19.0, abs=1e-9)
    assert DEPARTURE_PREMIUM_MAX_KM >= 1.0, "must not re-price the calibration set"

    # And the premium never applies to more than the cap, however long the
    # blind stretch: past it, extra distance is priced flat.
    a = energy_for_blind_distance(5.0, 40.0, 8.0, departure_blind_km=8.0)
    b = energy_for_blind_distance(5.0, 40.0, 8.0, departure_blind_km=DEPARTURE_PREMIUM_MAX_KM)
    assert a == pytest.approx(b, abs=1e-9)


def test_pulling_out_of_a_bay_is_reclaimed_even_below_the_trip_floor():
    """Distance recovered at a departure is floored on whether the car MOVED,
    not on whether the movement would have been a trip on its own. Nothing is
    being created here — the trip already exists and already clears the trip
    floor — so applying it a second time only strands real metres.

    Trip 367: 0.09 km of pulling out of a parking bay, ten metres under
    DRIVE_MIN_KM's 0.1. It was recorded as lost and never given back, so the
    trip read 14.2 km against the car's own 14.3 and its start sat 90 m past
    where the previous trip had ended."""
    from app.sync import DEPARTURE_STILL_MAX_KM, DRIVE_MIN_KM

    assert DEPARTURE_STILL_MAX_KM < 0.09 < DRIVE_MIN_KM, "the band this covers"

    # 3-minute gap, 0.09 km of creep, then driving for real.
    p = snap(T0, 29_418.046, 66, range_km=300.0)
    c = snap(T0 + 180, 29_418.136, 66, shift="D", speed=20.0, range_km=300.0)
    end = snap(T0 + 180 + 2160, 29_432.359, 63, shift="P", speed=0.0,
               locked=True, range_km=286.0)

    _, _, trip, _ = step(p, c)
    assert trip["odo_km"] == 29_418.046          # back to where the last trip ended
    assert trip["start_recovered_km"] == 0.09
    assert trip["start_lost_km"] == 0.0
    drives, _, trip, _ = step(c, end, trip)
    assert trip is None and len(drives) == 1
    # 29,432.359 - 29,418.046 = 14.313, which is the car's own 14.3.
    assert drives[0]["distance_km"] == 14.3

    # Smaller still comes back too. Trip 369 lost 42 m to the 0.05 floor that
    # replaced the 0.1 one, and started 42 m past where trip 370 had ended —
    # the same boundary disagreement, one threshold further down. An odometer
    # does not jitter, so any positive delta between two real readings is
    # ground the car covered.
    q = snap(T0, 29_448.024, 66, range_km=300.0)
    r = snap(T0 + 124, 29_448.066, 66, shift="D", speed=20.0, range_km=300.0)
    _, _, trip2, _ = step(q, r)
    assert trip2["start_recovered_km"] == 0.042
    assert trip2["odo_km"] == 29_448.024, "starts where the last trip ended"

    # A car that truly has not moved still reclaims nothing — there is no
    # ground, not merely a little of it.
    u = snap(T0, 6_000.0, 66, range_km=300.0)
    v = snap(T0 + 180, 6_000.0, 66, shift="D", speed=20.0, range_km=300.0)
    _, _, trip3, _ = step(u, v)
    assert trip3["start_recovered_km"] == 0.0
    assert trip3["start_lost_km"] == 0.0


def test_a_days_gap_is_not_one_departure_however_parked_it_looks():
    """Trip 368, and the worst failure the departure recovery has produced.

    prev was the previous trip's clean park at the Office, so is_driving(prev)
    was false and DEPARTURE_GAP_MAX_KM never applied — the cap only guarded the
    mid-drive case. The recovery reached back 9.448 km across 12.4 hours and
    swallowed an entire Office->Home drive, the stop after it, and the first
    half of Home->Penang Retirement Resort: one trip logged where two happened.

    A confirmed park earns an uncapped reach only while the park was SHORT. The
    longer the car sat unseen, the likelier the gap holds whole journeys rather
    than this trip's opening minutes — and a day's worth of gap holds anything.

    Then the energy: 9.448 km of a 15.665 km span is 60%, past what
    energy_for_blind_distance will project across, so the distance was folded
    in with no energy behind it. The measured part alone carried 0.88 kWh over
    6.217 km — 142 Wh/km, ordinary — while the trip reported 56, which no car
    does."""
    # Parked at the Office overnight, 12.4 h unseen, then caught mid-drive
    # 9.448 km along with two journeys already behind it.
    p = snap(T0, 29_432.359, 74, shift="P", speed=0.0, locked=True, range_km=380.0)
    c = snap(T0 + 44_509, 29_441.807, 73, shift="D", speed=40.0, range_km=375.0)
    # 0.876 kWh over the 6.217 km actually watched — trip 368's own figures.
    end = snap(T0 + 44_509 + 1300, 29_448.024, 71.5, shift="P", speed=0.0,
               locked=True, range_km=367.5)

    _, _, trip, _ = step(p, c)
    assert trip["odo_km"] == c["odo_km"], "must not reach back across the day"
    assert trip["start_recovered_km"] == 0.0
    assert trip["start_lost_km"] == 9.448, "reported, not buried"

    drives, _, trip, _ = step(c, end, trip)
    assert trip is None and len(drives) == 1
    # Only the stretch actually watched, at an efficiency that exists.
    assert drives[0]["distance_km"] == 6.2
    assert drives[0]["energy_used_kwh"] > 0
    rate = drives[0]["energy_used_kwh"] * 1000.0 / drives[0]["distance_km"]
    assert 100 < rate < 300, f"{rate:.0f} Wh/km should be an ordinary figure"


def test_blind_distance_too_big_to_price_leaves_energy_unknown_not_diluted():
    """When the blind stretch is too large a share of a trip to project across,
    the pricing declines — but the distance has already been folded in, so the
    trip keeps ground it has no energy for and Wh/km is diluted by exactly the
    folded share. That is the artifact the pricing exists to prevent, produced
    by the pricing declining.

    A dash beats a number that looks like a reading: the distance is measured,
    the energy genuinely isn't. Same answer this already gives to a mid-trip
    range refill."""
    from app.sync import BLIND_DISTANCE_MAX_SHARE, _drive_from

    start = {"ts": T0, "odo_km": 1000.0, "soc": 80, "range_km": 400.0,
             "start_recovered_km": 7.0, "start_energy_recovered": False,
             "start_lost_km": 0.0}
    cur = {"ts": T0 + 1800, "odo_km": 1010.0, "soc": 79, "range_km": 396.0,
           "out_temp": 30.0, "lat": None, "lon": None}
    assert 7.0 > 10.0 * BLIND_DISTANCE_MAX_SHARE      # 70% — past what it will project
    d = _drive_from(start, cur, 60.0)
    assert d["distance_km"] == 10.0                   # measured, kept
    assert d["energy_used_kwh"] == 0.0                # unknown, not fabricated

    # Inside the share it is priced as before, not blanked.
    ok = dict(start, start_recovered_km=3.0)
    d2 = _drive_from(ok, cur, 60.0)
    assert d2["distance_km"] == 10.0
    assert d2["energy_used_kwh"] > 0


def test_departure_energy_is_bounded_by_parked_minutes_not_the_whole_gap():
    """Only the PARKED part of a departure gap carries standby drain, so that
    is what the staleness bound was always trying to measure — the gap is just
    a proxy, and a poor one once a real departure hides inside it.

    Trip 366: a 48-minute gap of which ~38 were parked and ~10 were the drive
    itself. Refused on the gap, the trip fell back to projecting its blind head
    from the rest of the drive and came out 13.7% above the car's own figure —
    a head that in fact cost 0.91x the trip average, against a premium assuming
    1.55x. Measuring the whole drive and subtracting 38 minutes of this car's
    own parked draw is a bounded correction where that was an unbounded guess.
    """
    from app.sync import STALE_ANCHOR_MAX_MIN

    # 48-minute gap, 4.791 km covered before the car was first seen driving.
    p = snap(T0, 29_406.714, 74, range_km=380.0)
    c = snap(T0 + 2880, 29_411.505, 73, shift="D", speed=45.0, range_km=374.0)
    _, _, trip, _ = step(p, c)
    assert trip["start_recovered_km"] == 4.791
    assert trip["start_energy_recovered"] is True
    assert trip["soc"] == p["soc"]              # measured, not projected
    # ~48 min gap less ~9.6 min of driving at the 30 km/h floor.
    assert trip["start_park_min"] == pytest.approx(38.4, abs=0.5)
    assert trip["start_park_min"] < STALE_ANCHOR_MAX_MIN

    # Push the parked portion past the bound and it must refuse — the SoC
    # baseline, and past DEPARTURE_GAP_MAX_KM the distance with it. The longer
    # the car sat unseen, the likelier the gap holds a whole journey rather
    # than this trip's opening minutes, and 4.791 km is well past what a
    # departure can hide (trip 368 lost an entire drive to exactly this).
    from app.sync import DEPARTURE_GAP_MAX_KM

    p2 = snap(T0, 9_000.0, 74, range_km=380.0)
    c2 = snap(T0 + 4800, 9_004.791, 73, shift="D", speed=45.0, range_km=374.0)
    _, _, trip2, _ = step(p2, c2)
    assert 4.791 > DEPARTURE_GAP_MAX_KM
    assert trip2["start_recovered_km"] == 0.0        # not folded in
    assert trip2["start_lost_km"] == 4.791           # reported, not buried
    assert trip2["start_energy_recovered"] is False  # nor the SoC baseline
    assert trip2["soc"] == c2["soc"]
    assert trip2.get("start_park_min") is None

    # Under the cap it still comes back, however stale: that much really is
    # the opening metres of one departure, and leaving it out only strands it.
    p3 = snap(T0, 9_000.0, 74, range_km=380.0)
    c3 = snap(T0 + 4800, 9_002.4, 73, shift="D", speed=45.0, range_km=374.0)
    _, _, trip3, _ = step(p3, c3)
    assert trip3["start_recovered_km"] == 2.4
    assert trip3["start_energy_recovered"] is False  # still no SoC baseline


def test_start_lost_km_is_never_null_on_a_reconstructed_drive():
    """A null start_lost_km must mean one thing only: the row predates the
    instrumentation. The gap-reconstruction paths build their own start dict
    rather than carrying an open trip's, so without an explicit 0.0 they'd
    emit null forever and be indistinguishable from an old row — exactly the
    ambiguity that made a real audit read (trip 294) inconclusive.

    Both paths anchor on the previous reading's odometer and span the whole
    gap, so nothing can precede the anchor: 0.0 is measured, not assumed."""
    # A whole drive inside one poll gap (car asleep / cron gap).
    g1 = snap(T0, 5000.0, 80, shift="P", speed=0.0, locked=True, range_km=400.0)
    g2 = snap(T0 + 5400, 5030.0, 74, shift="P", speed=0.0, locked=True, range_km=370.0)
    drives, _, _, _ = step(g1, g2)
    assert drives, "a drive should be reconstructed across the gap"
    assert drives[0]["distance_km"] == 30.0          # the full odometer delta
    assert drives[0]["start_lost_km"] == 0.0         # and nothing precedes it

    # Charge-then-drive split inside a single gap: same guarantee on the drive
    # half, which is anchored to the pre-charge odometer.
    c1 = snap(T0, 6000.0, 40, shift="P", speed=0.0, locked=True,
              range_km=200.0, charging="Charging", kw=7.0)
    c2 = snap(T0 + 7200, 6020.0, 70, shift="P", speed=0.0, locked=True,
              range_km=330.0, energy_added=25.0)
    drives2, charges2, _, _ = step(c1, c2)
    assert charges2 and drives2, "the gap should split into a charge and a drive"
    assert drives2[0]["start_lost_km"] == 0.0


def test_blind_gap_close_folds_parking_creep_into_the_trip_that_ended():
    """The blind-gap close used to end a trip at the last seen reading while
    the next opened at the current one, so odometer movement across the gap
    belonged to neither. The trip then read short against the car's own display
    with its energy intact — a clipped tail, which looks nothing like a clipped
    start (that loses both together).

    The creep is now folded into the trip that ended. Only the odometer is
    extended: duration and energy stay anchored at the last reading so the nap
    itself — unbounded in length — contributes neither time nor standby drain.
    """
    # Drive, then a long quiet gap with a little creep, then driving again.
    d1 = snap(T0, 3000.0, 80, shift="D", speed=40.0, range_km=400.0)
    d2 = snap(T0 + 600, 3010.0, 78, shift="D", speed=35.0, range_km=392.0)
    # 25 min later (> PARK_GAP_MIN) only 0.3 km moved -> ~0.7 km/h implied,
    # well under PARK_SPEED_KMH, so this reads as "parked and slept".
    d3 = snap(T0 + 2100, 3010.3, 77, shift="D", speed=20.0, range_km=389.0)

    _, _, trip, _ = step(d1, d2)
    drives, _, _, _ = step(d2, d3, trip)
    assert drives, "the first drive should close across the gap"
    assert drives[0]["distance_km"] == 10.3        # creep included
    assert drives[0]["end_lost_km"] == 0.0         # nothing left behind
    # Duration stays at the last real reading — the nap contributes none of it,
    # which is the whole reason only the odometer is extended.
    assert drives[0]["duration_min"] == 10.0
    # The creep's own energy IS counted, priced at the trip's measured
    # efficiency. Taking the later reading's SoC instead would drag the whole
    # nap's standby drain in, which is what this close exists to avoid — but
    # dropping it entirely diluted Wh/km by the folded share, the same defect
    # the sustained-offline top-up had.
    raw = _energy_kwh(d1, d2, 60.0)
    assert drives[0]["energy_used_kwh"] == round(raw * 10.3 / 10.0, 2)
    assert drives[0]["energy_used_kwh"] > round(raw, 2)
    # Wh/km is what stays put — that is the point of pricing it this way.
    assert round(drives[0]["energy_used_kwh"] * 1000 / 10.3) == round(raw * 1000 / 10.0)
    # A real 0.0 rather than null: this path closes at a known reading, so
    # nothing was trimmed off the tail — distinct from never having considered.
    assert drives[0]["tail_trim_sec"] == 0.0

    # A low implied speed over a long gap can still cover real distance, which
    # would be an unobserved drive rather than creep. Past GAP_CREEP_MAX_KM it
    # must NOT be folded in on a guess — record it and leave the trip alone.
    b1 = snap(T0, 7000.0, 80, shift="D", speed=40.0, range_km=400.0)
    b2 = snap(T0 + 600, 7010.0, 78, shift="D", speed=35.0, range_km=392.0)
    # 4 h later, 12 km covered -> 3 km/h implied: still "parked" by rate, but
    # far too much distance to call it pulling into a spot.
    b3 = snap(T0 + 15000, 7022.0, 70, shift="D", speed=30.0, range_km=360.0)
    _, _, trip_b, _ = step(b1, b2)
    drives_b, _, _, _ = step(b2, b3, trip_b)
    assert drives_b, "the first drive should still close"
    assert drives_b[0]["distance_km"] == 10.0      # unchanged, not extended
    assert drives_b[0]["end_lost_km"] == 12.0      # and the amount is recorded

    # The ordinary parked close tracks the odometer forward, so it loses
    # nothing and must say so rather than leaving the question open.
    p1 = snap(T0, 4000.0, 80, shift="D", speed=40.0, range_km=400.0)
    p2 = snap(T0 + 600, 4008.0, 78, shift="D", speed=30.0, range_km=394.0)
    # Locked in P is a definitive end-of-drive, so this closes the trip here.
    p3 = snap(T0 + 1500, 4008.4, 78, shift="P", speed=0.0, locked=True, range_km=393.6)
    _, _, trip2, _ = step(p1, p2)
    drives2, _, _, _ = step(p2, p3, trip2)
    assert drives2, "the parked close should end the trip"
    assert drives2[0]["end_lost_km"] == 0.0
    # The close point tracked the odometer to the latest reading, so the trip
    # keeps the final 0.4 km of parking creep rather than dropping it.
    assert drives2[0]["distance_km"] == 8.4


def test_close_trip_on_sleep_leaves_the_lost_tail_unknown():
    """This asserted a real 0.0, on the reasoning that sleep is only reachable
    once the car has stopped so nothing can follow the last reading. Sleep does
    prove the car stopped; it says nothing about where the last reading sits
    relative to that stop. Measured on a car parking on level 1 of a
    multi-storey: signal dies at the ramp, and the closing reading is the
    street outside with 0.05-0.36 km still to drive. Unknown is what this path
    can support. tail_trim_sec stays null for its own reason — no pace-based
    stop estimate is ever evaluated here."""
    open_trip = {
        "ts": T0, "odo_km": 8000.0, "soc": 80, "range_km": 400.0,
        "max_speed": 50.0, "idle_min": 0.0, "still_run": 0.0, "still_since": None,
    }
    last_snapshot = snap(T0 + 600, 8006.0, 78, shift="P", speed=0.0,
                         locked=True, range_km=394.0)
    d = close_trip_on_sleep(open_trip, last_snapshot, 60.0)
    assert d is not None
    assert d["distance_km"] == 6.0
    assert d["end_lost_km"] is None        # unknown, not a measured zero
    assert d["tail_trim_sec"] is None


def test_the_arrival_tail_comes_from_the_place_not_from_the_speed():
    """The speed-based model is gone. Four arrivals measured against the car's
    own trip meter needed windows of 17, 51, 119 and 868 seconds to fit, and
    the two slowest readings produced the largest and smallest tails — speed at
    the last reading says nothing about what follows it.

    A place does. The caller supplies what that car park has measured, and this
    only turns it into the (km, seconds) pair the close needs."""
    from app.sync import (ARRIVAL_CRAWL_KMH, ARRIVAL_EST_MAX_KM,
                          ARRIVAL_EST_MAX_MIN, arrival_tail_for_place)

    km, sec = arrival_tail_for_place(0.193)
    assert km == 0.193
    # Seconds follow the distance at a car-park crawl, not from how long we
    # took to notice the car had gone quiet.
    assert sec == pytest.approx(0.193 / ARRIVAL_CRAWL_KMH * 3600.0)

    # No measurements, no estimate — an honest absence, and the one the
    # evidence prefers: the speed model averaged 0.200 km of error against
    # 0.208 km for estimating nothing at all.
    assert arrival_tail_for_place(None) is None
    assert arrival_tail_for_place(0.0) is None

    # Still bounded, so one freak measurement cannot run away with a trip.
    big_km, big_sec = arrival_tail_for_place(50.0)
    assert big_km == ARRIVAL_EST_MAX_KM
    assert big_sec == ARRIVAL_EST_MAX_MIN * 60.0


def test_a_sleep_close_folds_its_estimated_tail_in_and_records_it_as_estimated():
    """The estimate goes into distance so the trip reads closer to the car's own
    figure, and into end_est_km so it can never pass for a measurement — and so
    the correction that supersedes it knows exactly how much to take back."""
    from app.sync import close_trip_on_sleep

    open_trip = {
        "ts": T0, "odo_km": 8000.0, "soc": 80, "range_km": 400.0,
        "max_speed": 50.0, "idle_min": 0.0, "still_run": 0.0, "still_since": None,
    }
    last = snap(T0 + 600, 8006.0, 78, shift="D", speed=20.0, range_km=394.0)
    plain = close_trip_on_sleep(open_trip, last, 60.0)
    assert plain["end_est_km"] is None            # no measurements, no estimate
    assert plain["distance_km"] == 6.0

    est = close_trip_on_sleep(open_trip, last, 60.0, place_tail_km=0.5)
    assert est["end_est_km"] == 0.5               # what the place has measured
    assert est["distance_km"] == 6.5              # folded in
    # The clock moved with it, so the trip reads as one that slowed to a stop
    # rather than one that covered more ground in the same time.
    assert est["duration_min"] == pytest.approx(plain["duration_min"] + 3.0)
    assert est["avg_speed_kmh"] < plain["avg_speed_kmh"]
    assert est["end_lost_km"] is None             # still nothing measured
    # Energy came with it, so Wh/km doesn't collapse by the folded share.
    assert est["energy_used_kwh"] > plain["energy_used_kwh"]
    def whkm(d):
        return d["energy_used_kwh"] * 1000.0 / d["distance_km"]
    assert abs(whkm(est) - whkm(plain)) < 0.5


def test_tail_trim_changes_duration_only_never_distance_or_energy():
    """The stop-time correction rewrites the recorded timestamp and nothing
    else — the stop snapshot keeps the real reading's odometer and range, so
    distance and energy stay the full measured deltas. Locking this down
    because the opposite was briefly believed: a trim cannot shrink a trip's
    kWh, so a trip reading short on energy is never explained by one."""
    s1 = snap(T0, 10_000.0, 80, shift="P", speed=0.0, range_km=400.0)
    s2 = snap(T0 + 300, 10_010.0, 79, shift="D", speed=50.0, range_km=395.0)
    s3 = snap(T0 + 900, 10_012.0, 78, shift="P", speed=0.0, locked=True, range_km=394.0)

    _, _, trip, _ = step(s1, s2)
    drives, _, _, _ = step(s2, s3, trip)
    d = drives[0]

    assert d["tail_trim_sec"] >= 60                      # a trim really happened
    assert d["distance_km"] == 12.0                      # full s1->s3 odometer delta
    assert d["duration_min"] < 15.0                      # but the clock was cut
    # Energy matches what the untrimmed endpoints imply, not a shortened tail.
    from app.sync import _energy_kwh
    assert d["energy_used_kwh"] == round(_energy_kwh(s1, s3, 60.0), 2)


def test_no_trim_records_zero_not_none():
    """A trip closed by a path that evaluated the correction but didn't apply
    it records 0.0 — "considered and didn't fire" has to stay distinguishable
    from "never evaluated" (None), or the field can't be trusted as evidence."""
    s1 = snap(T0, 10_000.0, 80, shift="P", speed=0.0, range_km=400.0)
    s2 = snap(T0 + 60, 10_002.0, 79, shift="D", speed=50.0, range_km=398.0)
    # Closing reading arrives promptly, so there is no tail to trim.
    s3 = snap(T0 + 120, 10_003.0, 79, shift="P", speed=0.0, locked=True, range_km=397.5)

    _, _, trip, _ = step(s1, s2)
    drives, _, trip, _ = step(s2, s3, trip)
    assert len(drives) == 1
    assert drives[0]["tail_trim_sec"] == 0.0


def test_drive_min_km_threaded_through_process_snapshot():
    """The configurable floor must reach the whole-gap trip reconstruction
    that process_snapshot uses, not just _drive_from in isolation."""
    j1 = snap(T0, 10_000.0, 91)
    j2 = snap(T0 + 60, 10_000.05, 91)    # 0.05 km jitter, no snapshot in between
    d, _, _, _ = process_snapshot(j1, j2, None, None, 75.0, 0.90)
    assert d == []                        # default 0.1 km floor: filtered as jitter

    s1 = snap(T0, 10_000.0, 91)
    s2 = snap(T0 + 180, 10_000.4, 91)    # 0.4 km real short move
    d2, _, _, _ = process_snapshot(s1, s2, None, None, 75.0, 0.90)
    assert len(d2) == 1 and d2[0]["distance_km"] == 0.4   # default floor: logged

    d3, _, _, _ = process_snapshot(s1, s2, None, None, 75.0, 0.90, drive_min_km=0.5)
    assert d3 == []                       # raised floor: filtered again


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


def test_live_tracked_charge_survives_a_drive_before_the_close_poll():
    """A charge opened live (via a real poll) must not be dropped just
    because the *next* poll — the one that finally notices charging
    stopped — only arrives after a short drive has already happened too.

    Regression: the close-time SoC gate (`cur.soc - start.soc`) used cur's
    SoC as-is, but a drive after the charge finished consumes SoC on top of
    what the charge added — here enough to net the SoC right back to where
    it started. The old gate then saw ~0% net gain and silently dropped a
    real, fully Tesla-meter-measured 7.2 kWh session. Tesla's own session
    meter doesn't move for driving, so it's used to detect the gain (and to
    estimate the true end-of-charge SoC) whenever the odometer shows a
    drive happened before the close poll caught up.
    """
    before = snap(T0, 10_000.0, 40)                                    # parked, pre-charge
    opened = snap(T0 + 600, 10_000.0, 40, charging=True, kw=7)          # plugged in, charging
    # By the time the cron catches "charging stopped", a 4 km errand has
    # already happened too: the charge added 12% (7.2 kWh of 60 kWh) but
    # the drive used it right back down, so soc reads unchanged overall.
    closed = snap(T0 + 7200, 10_004.0, 40, energy_added=7.2)

    _, _, _, charge = step(None, before)
    _, c, _, charge = step(before, opened, charge=charge)
    assert c == [] and charge is not None                              # opened normally

    d, c, trip, charge = step(opened, closed, charge=charge)
    assert charge is None                                               # closed, not left open
    assert len(c) == 1                                                  # NOT silently dropped
    (chg,) = c
    assert abs(chg["energy_added_kwh"] - 7.2) < 1e-6                    # the real meter reading
    assert chg["start_soc"] == 40
    assert abs(chg["end_soc"] - 52.0) < 1e-6                            # 40% + 12% implied by the meter
    assert abs(chg["cost"] - 7.2 * 0.90) < 1e-6

    # The drive itself is still reconstructed independently from the same
    # gap (odometer delta is unaffected by any of the charge confusion).
    assert len(d) == 1
    assert d[0]["distance_km"] == 4.0


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


def test_energy_averages_full_range_projection_from_both_endpoints():
    """Reported live: a short trip's kWh/Wh-per-km read noticeably low
    against the car's own display. battery_level is only whole-percent
    precision, so the "full pack range" projection (range / (soc/100)) used
    to derive fine-grained energy is only as precise as *one* rounded SoC
    reading -- trusting only the trip's start reading lets that single
    rounding skew the whole trip. Deriving the projection from *both*
    endpoints and averaging them can only match or reduce that noise, never
    make it worse, since each reading's own rounding is at least partly
    independent of the other's."""
    from app.sync import _energy_kwh

    capacity_kwh = 60.0
    # Same true ~500 km full-pack range at both ends, but SoC rounded down at
    # the start (62.3% -> 62) and rounded up at the end (60.6% -> 61) --
    # opposite-direction noise that a start-only projection can't see.
    frm = {"range_km": 311.0, "soc": 62}    # true 62.3% -> full ~= 499.2 km
    to = {"range_km": 300.0, "soc": 61}     # true 60.6% -> full ~= 495.0 km
    energy = _energy_kwh(frm, to, capacity_kwh)

    full_start_only = frm["range_km"] / (frm["soc"] / 100.0)
    energy_start_only = max(frm["range_km"] - to["range_km"], 0.0) / full_start_only * capacity_kwh
    full_end_only = to["range_km"] / (to["soc"] / 100.0)
    energy_end_only = max(frm["range_km"] - to["range_km"], 0.0) / full_end_only * capacity_kwh

    # The combined result sits strictly between what either endpoint alone
    # would have given -- neither fully trusting the (rounded-down) start nor
    # the (rounded-up) end reading.
    lo, hi = sorted([energy_start_only, energy_end_only])
    assert lo < energy < hi


def test_energy_precision_weights_toward_higher_soc_endpoint():
    """On a wide-SoC-span trip the two endpoints' full-range projections
    disagree: the same absolute ±0.5-point integer rounding is a much larger
    *fraction* of a low-SoC reading, so its projection is the noisier one.
    Combining as total-range / total-SoC (100*(r0+r1)/(soc0+soc1)) leans on
    the higher-SoC, more reliable endpoint -- landing closer to the true full
    range than a plain average of the two projections would."""
    from app.sync import _energy_kwh

    capacity_kwh = 60.0
    true_full = 500.0
    # A long trip 80% -> 20%, true SoCs .4 above each integer -> both ranges
    # come from the same true 500 km pack; the low-SoC (20%) endpoint's
    # projection is far noisier than the high-SoC (80%) one.
    frm = {"range_km": true_full * 0.804, "soc": 80}   # proj 80% -> 502.5 km
    to = {"range_km": true_full * 0.204, "soc": 20}    # proj 20% -> 510.0 km
    energy = _energy_kwh(frm, to, capacity_kwh)

    proj_hi = frm["range_km"] / (frm["soc"] / 100.0)    # reliable endpoint
    proj_lo = to["range_km"] / (to["soc"] / 100.0)      # noisy endpoint
    full_plain_mean = (proj_hi + proj_lo) / 2.0
    energy_plain_mean = max(frm["range_km"] - to["range_km"], 0.0) / full_plain_mean * capacity_kwh
    energy_true = max(frm["range_km"] - to["range_km"], 0.0) / true_full * capacity_kwh

    # Precision-weighted lands closer to the truth than the plain average --
    # both overshoot slightly (both projections read high here), but the
    # weighted one overshoots less because it trusts the 80% reading more.
    assert abs(energy - energy_true) < abs(energy_plain_mean - energy_true)


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


def test_gap_fallback_splits_charge_then_short_drive():
    """A charge finishing and a short drive right after it, both missed by
    the cron in one gap, must NOT vanish or corrupt each other.

    Regression: previously the whole-gap fallback sized the charge from the
    net prev->cur SoC delta, so a drive right after the charge could eat
    enough of that gain to sink it below CHARGE_MIN_PCT and drop the charge
    entirely — while the drive's own energy read off a range delta that was
    really measuring the charge. Tesla's own session meter (energy_added_kwh)
    now detects and sizes the charge independently of what happened after.
    """
    prev = snap(T0, 10_000.0, 40, energy_added=0.0)
    # +12 kWh charge (20% of the 60 kWh test pack), then a 4 km errand that
    # used ~1.2 kWh (2%) — net SoC only rose 18%, but the real charge was 12
    # kWh and must be reported in full, and the drive must still appear.
    cur = snap(T0 + 7200, 10_004.0, 58, energy_added=12.0)
    d, c, trip, charge = step(prev, cur)

    assert len(c) == 1
    assert c[0]["energy_added_kwh"] == 12.0     # the real meter reading, not net SoC
    assert c[0]["start_soc"] == 40 and c[0]["end_soc"] == 60
    assert c[0]["cost"] == 10.8                 # 12 kWh * 0.90/kWh

    assert len(d) == 1
    assert d[0]["distance_km"] == 4.0
    assert d[0]["start_soc"] == 60 and d[0]["end_soc"] == 58
    assert trip is None and charge is None
    # Charge happened before the drive, in this order.
    assert c[0]["end_time"] <= d[0]["start_time"]
    assert d[0]["end_time"] == d[0]["end_time"]  # anchored at cur, sanity


def test_gap_charge_survives_stale_meter_from_a_bigger_previous_session():
    """The exact reported failure: the car was unreachable for the whole
    charge AND the drive after it, so both had to be reconstructed from one
    gap — and the charge vanished whenever the PREVIOUS session had added
    more kWh than this one.

    charge_energy_added resets at plug-in, so the parked pre-charge snapshot
    still carries the previous session's total (here 30.0). The old detector
    computed this session's energy as cur - prev = 12.0 - 30.0 < 0 and
    concluded "no meter evidence of a charge" — then the plain fallback
    dropped the session outright because the post-charge drive had eaten the
    net SoC gain. A changed meter on a parked-gap IS this session's total.
    """
    # Previous session (long ago) left 30.0 on the meter; car parked at 40%.
    prev = snap(T0, 10_000.0, 40, energy_added=30.0)
    # Unreachable gap: charged 12 kWh (+20% of the 60 kWh pack), then a 4 km
    # errand used most of it back down — SoC nets out at 42%.
    cur = snap(T0 + 7200, 10_004.0, 42, energy_added=12.0)

    d, c, trip, charge = step(prev, cur)
    assert len(c) == 1, "charge must not vanish behind the stale meter value"
    assert abs(c[0]["energy_added_kwh"] - 12.0) < 1e-6   # THIS session's total
    assert c[0]["start_soc"] == 40 and c[0]["end_soc"] == 60
    assert len(d) == 1
    assert d[0]["distance_km"] == 4.0
    assert trip is None and charge is None


def test_gap_charge_only_uses_meter_total_not_estimate_despite_stale_prev():
    """Charge-only gap (no drive): a changed meter across a parked gap gives
    the session's real measured total — better than the SoC estimate, and
    never the bogus (cur - stale_prev) difference."""
    prev = snap(T0, 10_000.0, 40, energy_added=30.0)   # stale meter from an old session
    cur = snap(T0 + 7200, 10_000.0, 52, energy_added=7.4)

    d, c, trip, charge = step(prev, cur)
    assert d == []
    (chg,) = c
    assert abs(chg["energy_added_kwh"] - 7.4) < 1e-6    # measured, not 12% * 60 = 7.2 estimate
    assert abs(chg["cost"] - 7.4 * 0.90) < 1e-6


def test_gap_after_sleep_closed_charge_logs_only_the_remainder():
    """If the last snapshot before the gap was taken MID-charge (the open
    session got sleep-closed at that reading), the meter never reset since —
    only the portion beyond that reading is new. Logging cur's full total
    again would double-count what the sleep-close already recorded."""
    prev = snap(T0, 10_000.0, 50, charging=True, kw=7, energy_added=9.0)
    cur = snap(T0 + 7200, 10_004.0, 53, energy_added=12.0)   # finished + short drive

    d, c, trip, charge = step(prev, cur)
    (chg,) = c
    assert abs(chg["energy_added_kwh"] - 3.0) < 1e-6    # 12.0 - 9.0, not 12.0 again
    assert len(d) == 1 and d[0]["distance_km"] == 4.0


def test_gap_fallback_plain_drive_and_charge_unaffected_without_meter():
    """Without a usable energy_added_kwh signal (e.g. legacy/imported data),
    the original net-delta whole-gap reconstruction still applies."""
    prev = snap(T0, 10_000.0, 80)
    cur = snap(T0 + 7200, 10_030.0, 85)
    d, c, trip, charge = step(prev, cur)
    assert len(d) == 1 and len(c) == 1
    assert d[0]["distance_km"] == 30.0
    assert c[0]["energy_added_kwh"] == 3.0
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


def test_charge_cost_uses_dc_rate_for_fast_charge_ac_rate_otherwise():
    """price_per_kwh_dc, when given, prices a DC session instead of the plain
    price_per_kwh AC/flat rate — same energy, different charger type."""
    ac_prev = snap(T0, 10_000.0, 40, charging=True, kw=7, fast=False)
    ac_cur = snap(T0 + 3600, 10_000.0, 60)
    _, ac_charges, _, _ = process_snapshot(
        ac_prev, ac_cur, None, None, 60.0, 0.99, price_per_kwh_dc=1.29)
    assert ac_charges[0]["charge_type"] == "AC"
    assert ac_charges[0]["cost"] == round(ac_charges[0]["energy_added_kwh"] * 0.99, 2)

    dc_prev = snap(T0, 20_000.0, 40, charging=True, kw=150, fast=True)
    dc_cur = snap(T0 + 1500, 20_000.0, 75)
    _, dc_charges, _, _ = process_snapshot(
        dc_prev, dc_cur, None, None, 60.0, 0.99, price_per_kwh_dc=1.29)
    assert dc_charges[0]["charge_type"] == "DC"
    assert dc_charges[0]["cost"] == round(dc_charges[0]["energy_added_kwh"] * 1.29, 2)

    # None (the default) means both charger types share price_per_kwh.
    _, dc_default, _, _ = process_snapshot(dc_prev, dc_cur, None, None, 60.0, 0.99)
    assert dc_default[0]["cost"] == round(dc_default[0]["energy_added_kwh"] * 0.99, 2)


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


def test_track_idle_counts_sustained_stationary_from_odometer():
    """Idle is measured from the odometer between snapshots, so a sustained
    stationary stretch is caught even when polling is sparse and never samples
    zero speed mid-stop; brief stops stay below the threshold and don't count."""
    from app.sync import _confirmed_idle_min, _track_idle

    # Brief stationary interval (90s, odometer unchanged) then the car moves on
    # — below IDLE_STREAK_MIN, so it must not count.
    open_trip = {"idle_min": 0.0, "still_run": 0.0}
    prev = snap(T0, 10_000.0, 80, shift="D", speed=0)
    cur = snap(T0 + 90, 10_000.0, 80, shift="D", speed=0)          # same odometer
    _track_idle(open_trip, prev, cur)
    assert open_trip["still_run"] == 1.5
    moving = snap(T0 + 150, 10_001.0, 80, shift="D", speed=40)      # odo advanced
    _track_idle(open_trip, cur, moving)
    assert open_trip["idle_min"] == 0.0          # brief stop dropped
    assert open_trip["still_run"] == 0.0

    # A single sparse 6-minute interval with no odometer movement — the case
    # the old speed-only tracker missed — is caught as 6 min of idle.
    p2 = snap(T0 + 200, 10_005.0, 79, shift="D", speed=30)
    c2 = snap(T0 + 560, 10_005.0, 79, shift="D", speed=30)          # 6 min, odo unchanged
    _track_idle(open_trip, p2, c2)
    assert _confirmed_idle_min(open_trip, c2["ts"]) == 6.0          # in-progress, already long enough
    m2 = snap(T0 + 620, 10_006.0, 79, shift="D", speed=45)          # moves on -> commit
    _track_idle(open_trip, c2, m2)
    assert open_trip["idle_min"] == 6.0


def test_track_idle_dense_sampling_builds_a_run():
    """Many short still intervals accumulate into one run, so a stop sampled
    every minute still crosses the threshold and counts."""
    from app.sync import _track_idle

    ot = {"idle_min": 0.0, "still_run": 0.0}
    base = snap(T0, 10_000.0, 80, shift="D", speed=0)
    for i in range(1, 7):                                           # 6 x 1-min still intervals
        nxt = snap(T0 + 60 * i, 10_000.0, 80, shift="D", speed=0)
        _track_idle(ot, base, nxt)
        base = nxt
    assert ot["still_run"] == 6.0
    _track_idle(ot, base, snap(T0 + 420, 10_001.0, 80, shift="D", speed=40))
    assert ot["idle_min"] == 6.0


def test_queue_creep_breaks_the_still_run():
    """Stop-go traffic chains: a light-wait, then queue creep (moving, but
    only ~50 m in a minute), then another light. The creep interval implies
    ~3 km/h — moving traffic, not idling — so it must BREAK the run instead
    of chaining the two waits into one long 'idle'. Ground truth from a real
    commute: a 29-min stop-go trip with only short light-waits was getting
    ~5 phantom idle minutes from exactly this chaining."""
    from app.sync import _confirmed_idle_min, _track_idle

    ot = {"idle_min": 0.0, "still_run": 0.0}
    a = snap(T0, 10_000.0, 80, shift="D", speed=0)
    b = snap(T0 + 120, 10_000.0, 80, shift="D", speed=0)     # 2-min light wait
    _track_idle(ot, a, b)
    c = snap(T0 + 180, 10_000.05, 80, shift="D", speed=2)    # 50 m creep in 1 min (3 km/h)
    _track_idle(ot, b, c)
    d = snap(T0 + 300, 10_000.05, 80, shift="D", speed=0)    # next 2-min light wait
    _track_idle(ot, c, d)
    e = snap(T0 + 360, 10_001.0, 80, shift="D", speed=40)    # traffic clears
    _track_idle(ot, d, e)
    # Neither wait reached the threshold on its own, and the creep broke the
    # chain — no idle recorded for a normal stop-go stretch.
    assert ot["idle_min"] == 0.0
    assert _confirmed_idle_min(ot, e["ts"]) == 0.0


def test_trailing_parked_wait_is_not_counted_as_in_drive_idle():
    """A trip that ends by sitting parked closes backdated to stop_at, but the
    stationary run keeps accumulating through the trailing parked wait (up to
    PARK_END_MIN before the timeout close). Only the part of the run before
    the trip's end may count as in-drive idle — the trailing parked minutes
    are post-trip parking, and counting them over-strips idle energy."""
    from app.sync import _confirmed_idle_min, process_snapshot

    # Drive off, then park (shift P, driver aboard) and sit for 16 minutes of
    # 1-minute polls until the PARK_END_MIN timeout closes the trip.
    s0 = snap(T0, 10_000.0, 80, shift="D", speed=50, range_km=400.0)
    _, _, trip, _ = process_snapshot(None, s0, None, None, 60.0, 0.90)
    s1 = snap(T0 + 600, 10_008.0, 79, shift="D", speed=45, range_km=394.0)
    _, _, trip, _ = process_snapshot(s0, s1, trip, None, 60.0, 0.90)

    prev = s1
    drives = []
    for i in range(1, 17):  # parked, polled every minute, odometer frozen
        cur = snap(T0 + 600 + 60 * i, 10_008.0, 79, present=True, range_km=394.0)
        drives, _, trip, _ = process_snapshot(prev, cur, trip, None, 60.0, 0.90)
        prev = cur
        if drives:
            break

    assert len(drives) == 1                      # closed by the parked timeout
    d = drives[0]
    # The trip ends at stop_at (the first parked reading, one poll after the
    # last driving one), and none of the trailing 15 parked minutes leak in
    # as in-drive idle.
    assert d["idle_min"] == 0.0
    assert d["duration_min"] == 11.0             # power-on to stop_at only


def test_track_idle_ignores_park_nap_gaps():
    """An interval long enough to be a park/nap (a trip boundary handled
    elsewhere) must not be folded into in-drive idle."""
    from app.sync import _track_idle

    ot = {"idle_min": 0.0, "still_run": 0.0}
    prev = snap(T0, 10_000.0, 80, shift="D", speed=0)
    cur = snap(T0 + 1500, 10_000.0, 80, shift="D", speed=0)         # 25-min gap, no movement
    _track_idle(ot, prev, cur)
    assert ot["idle_min"] == 0.0
    assert ot["still_run"] == 0.0


def test_live_trip_uses_real_tracked_idle_not_speed_heuristic():
    """live_trip's driving_wh_per_km should reflect actually-observed stopped
    time (from _track_idle), not the old avg/max-speed estimate — a genuine
    sustained stop lowers it below the raw wh_per_km."""
    from app.sync import _confirmed_idle_min, live_trip, process_snapshot

    s1 = snap(T0, 10_000.0, 80, shift="D", speed=60, range_km=400.0)
    _, _, trip, _ = process_snapshot(None, s1, None, None, 60.0, 0.90)
    assert trip is not None

    # Sustained 6-minute stationary stretch mid-trip (odometer unchanged).
    s2 = snap(T0 + 300, 10_005.0, 79, shift="D", speed=0, range_km=395.0)
    _, _, trip, _ = process_snapshot(s1, s2, trip, None, 60.0, 0.90)
    s3 = snap(T0 + 300 + 360, 10_005.0, 79, shift="D", speed=50, range_km=395.0)
    _, _, trip, _ = process_snapshot(s2, s3, trip, None, 60.0, 0.90)
    assert _confirmed_idle_min(trip, s3["ts"]) == 6.0

    now = snap(T0 + 300 + 360 + 300, 10_030.0, 74, shift="D", speed=60, range_km=380.0)
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


def test_arrival_stop_estimate_uses_last_seen_speed_not_trip_peak():
    """Reported live: a drive that cruised fast earlier (highway) but had
    already slowed onto a local road by the last live reading, then lost
    signal, slowed further and parked ~1-2 min later in a no-coverage spot.
    The stop estimate must use *prev*'s own last-seen speed as the pace
    evidence for that final stretch — the same real-evidence-over-assumption
    model already used on the power-on side — not the trip's much faster
    earlier peak, which would imply the short remaining distance was covered
    almost instantly and record the stop mere seconds after the last live
    reading instead of the genuine slow-down-and-park it actually was."""
    from app.sync import _dt
    s0 = snap(T0, 10_000.0, 80)                                 # parked
    s1 = snap(T0 + 300, 10_002.0, 79, shift="D", speed=100)     # driving fast, opens trip
    s2 = snap(T0 + 600, 10_012.0, 76, shift="D", speed=40)      # slowed, last live reading
    # 17-min no-signal gap: only 0.3 km further -- the car finished slowing
    # and parked within the first couple of minutes, then sat still.
    s3 = snap(T0 + 600 + 17 * 60, 10_012.3, 75.8, locked=True)
    _, _, trip, _ = step(s0, s1)
    assert trip is not None
    _, _, trip, _ = step(s1, s2, trip)
    assert trip.get("max_speed") == 100          # trip's peak, well above prev's own 40
    d, _, trip, _ = step(s2, s3, trip)
    assert trip is None and len(d) == 1
    drive = d[0]
    # Stop estimate lands meaningfully after the last live reading (using its
    # 40 km/h pace, not the earlier 100 km/h peak) -- not mere seconds later.
    end_delta_sec = (drive["end_time"] - _dt(s2["ts"])).total_seconds()
    assert end_delta_sec > 25


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


def test_power_on_backdate_reanchors_odo_and_soc_not_just_timestamp():
    """Reported live: parked-gap "vampire drain" kWh reading noticeably
    higher than expected, "should belong to trip kWh" instead. Cause: when
    a blind gap's own implied speed is low enough to count as "was parked"
    (_was_parked_since), the new trip's start *timestamp* gets backdated
    from cur (the first driving reading) toward when driving plausibly
    began -- but odo_km/soc stayed anchored to cur's already-driven values,
    so the "catch-up" distance/energy covered before cur arrived silently
    fell out of the trip and surfaced one gap earlier as parked drain
    instead of driving energy. odo/soc must be re-anchored to prev's
    (genuinely still-parked, unmoved) values right alongside the timestamp,
    the same anchor the was_parked=False branch already uses by default."""
    s1 = snap(T0, 10_000.0, 60.0, locked=True)                    # parks & locks
    # 60-min blind gap (no network) -- but the car only actually set off in
    # the last ~2 min of it, covering 1 km before this first driving
    # reading (a real, nonzero speed) arrives.
    s2 = snap(T0 + 3600, 10_001.0, 59.7, shift="D", speed=40)
    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    assert trip["ts"] < s2["ts"]                     # timestamp backdated, as before
    # The odometer is no longer silently pinned to cur's already-driven value:
    # the 1 km covered just before cur counts toward the trip, which is the
    # complaint this test was written for.
    assert trip["odo_km"] == s1["odo_km"]
    # Its ENERGY comes back by a different route now. Only ~2 of these 60
    # minutes were departure; the other 58 were a parked car, and taking s1's
    # SoC as the trip's baseline would charge the trip for all of it (the
    # defect trip 319 exposed at 2.3 hours, present here in miniature). Past
    # DEPARTURE_STALE_MAX_MIN the baseline stays at cur and the recovered
    # kilometre is priced at the trip's own measured efficiency instead — so
    # the catch-up energy still lands on the trip, without the standby drain
    # riding in with it.
    assert trip["soc"] == s2["soc"]
    assert trip["start_recovered_km"] == 1.0
    assert trip["start_energy_recovered"] is False


def test_power_on_backdate_recovers_odo_even_when_shift_too_short_to_estimate():
    """A short pre-departure stretch (well under the ~0.5 km the timestamp
    estimate's own 60s floor requires) must still be recovered onto the
    trip -- the distance is a measured fact from two real odometer
    readings, not an estimate, so it doesn't need the same confidence bar
    the clock guess does. Before this was fixed, this exact case fell
    through with neither the timestamp nor the odo/SoC corrected."""
    s1 = snap(T0, 20_000.0, 55.0, locked=True)                     # parked
    # 40-min blind gap, but only 0.2 km covered by the time this first
    # driving reading arrives -- pace stays at the 30 km/h floor, so the
    # implied shift (24s) never clears the 60s "worth backdating" bar.
    s2 = snap(T0 + 2400, 20_000.2, 54.9, shift="D", speed=15)
    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    assert trip["odo_km"] == s1["odo_km"]        # the 0.2 km recovered, not dropped
    assert trip["soc"] == s1["soc"]
    assert trip["ts"] == s2["ts"]                # too little evidence to backdate the clock


def test_short_blind_gap_arrival_does_not_inflate_duration():
    """Reported live: a real trip ended, the car parked within about a
    minute, but the next poll only landed 7 minutes later — logged as one
    7-minute "trip" covering just 0.2 km (avg 2 km/h, 800 Wh/km — an
    impossible reading for real driving). The prior fix
    (test_arrival_after_signal_gap_does_not_inflate_duration) only
    triggered past a 15-minute blind gap; a shorter-but-still-real gap like
    this one needs the same correction, gated on the gap's own implied
    speed reading too slow to be real driving (not just its length)."""
    s1 = snap(T0, 10_000.0, 80, shift="D", speed=60)              # driving
    s2 = snap(T0 + 60, 10_001.0, 79.9, shift="D", speed=55)       # still driving, 1 km on
    # 7-min blind gap: only 0.2 km further -- the car parked within the
    # first ~minute and sat still the rest of the gap.
    s3 = snap(T0 + 60 + 420, 10_001.2, 79.8, locked=True)
    _, _, trip, _ = step(s1, s2)
    d, _, trip, _ = step(s2, s3, trip)
    assert trip is None and len(d) == 1
    drive = d[0]
    # Duration ends near the real stop (s2 + ~a few tens of seconds at a
    # realistic pace for 0.2 km), not stretched out to the full 7-min gap.
    assert drive["duration_min"] < 2
    assert drive["distance_km"] == 1.2


def test_stop_at_keeps_extending_through_continued_creep():
    """Reported live: two consecutive trips sharing the same (large, named)
    parking area read a matched short/long pair against the car's own
    display -- one trip a bit short, the very next a bit long, roughly
    cancelling out. Cause: the very first "not driving" reading freezes
    stop_at right there, even when the car is still genuinely creeping
    forward (settling into a spot in a large lot, not yet actually
    stationary) -- real, odometer-confirmed distance/energy after that
    point silently falls out of the trip, and (since the next trip's own
    opening anchor correctly starts from wherever the car is *actually*
    resting once fully stopped) never resurfaces in the next trip either --
    it's just gone. stop_at must keep extending forward through readings
    where the odometer is still climbing, and only truly freeze once it
    stops."""
    s0 = snap(T0, 10_000.0, 80)                                     # parked at start
    s1 = snap(T0 + 5, 10_000.0, 80, shift="D", speed=40)            # opens the trip
    # First "not driving" reading -- but the car is still easing forward
    # into a large parking area over the next couple of polls.
    s2 = snap(T0 + 600, 10_004.6, 76, present=True)
    s3 = snap(T0 + 660, 10_004.8, 76, present=True)                 # still creeping
    s4 = snap(T0 + 720, 10_004.9, 76, locked=True)                  # truly at rest -> closes

    _, _, trip, _ = step(s0, s1)
    assert trip is not None
    _, _, trip, _ = step(s1, s2, trip)
    _, _, trip, _ = step(s2, s3, trip)
    d, _, trip, _ = step(s3, s4, trip)
    assert trip is None and len(d) == 1
    # The full distance, including the creep after the first "parked"
    # reading -- not frozen 0.3 km short at s2.
    assert d[0]["distance_km"] == 4.9


def test_short_blind_gap_power_on_does_not_delay_start():
    """Reported live: the 2nd-to-last trip's logged start was ~4-5 minutes
    later than when the car actually set off — a real head start before the
    first driving reading arrived, on a gap too short (under 15 min) for the
    existing "was parked since" anchor-at-cur logic to back-estimate at
    all. A real (nonzero) speed on the first driving reading is itself
    direct evidence of a head start; the correction now applies at that
    reading regardless of which anchor (prev or cur) was initially picked."""
    s1 = snap(T0, 10_000.0, 80)                                    # parked
    # 12-min gap, then first driving reading already at a real road speed,
    # having covered 5 km -- implies driving started partway through the
    # gap, not right at either endpoint.
    s2 = snap(T0 + 720, 10_005.0, 78, shift="D", speed=50)
    _, _, trip, _ = step(s1, s2)
    assert trip is not None
    # Backdated from s2 toward s1, not pinned to s2 (a delayed start) nor
    # stretched all the way back to s1 (12 min, which would fold in the
    # whole parked gap).
    assert trip["ts"] > s1["ts"]
    back_min = (s2["ts"] - trip["ts"]) / 60.0
    assert 1 < back_min < 12


def test_confirmed_park_then_short_gap_does_not_start_new_trip_at_zero_gap():
    """Reported live: a trip ended with the car genuinely parked and locked;
    a network gap of a few minutes then passed before the next poll caught
    it already driving again (not a slow, still-in-progress departure — the
    park was real and confirmed). Because that gap was well under
    STALE_ANCHOR_MIN, _was_parked_since alone said "no", so the new trip's
    start got backdated straight to the parked reading -- showing zero gap
    against the previous trip's end and an impossibly slow, stretched-out
    2nd trip. Confirmed-parked prev + a real (nonzero) speed already on the
    first driving reading (direct evidence it wasn't just starting to
    creep) should anchor the new trip at/near cur instead."""
    s1 = snap(T0, 10_000.0, 80, shift="D", speed=20)               # driving
    s2 = snap(T0 + 30, 10_000.2, 79.8, locked=True)                # parks & locks
    # Short (2-min) network gap, then already driving again at a real pace.
    s3 = snap(T0 + 30 + 120, 10_000.4, 79.6, shift="D", speed=25)
    _, _, trip, _ = step(s1, s2)
    assert trip is None                                            # 1st trip closed
    _, _, trip, _ = step(s2, s3, trip)
    assert trip is not None
    assert trip["ts"] > s2["ts"]           # not backdated into the park
    assert trip["ts"] == s3["ts"]          # anchored at (or right by) the resume


def test_stale_prev_does_not_backdate_open_trip_start():
    """A drive seen right after an overnight park must anchor its *clock* to
    now, not to last night's stale snapshot (which would add hours of idle
    time to the duration) — but the small real distance covered before this
    first driving reading arrived (a measured odometer delta, not a guess)
    still belongs to the trip, however short: the 60s "worth it" floor gates
    the *timestamp* estimate only, not the distance recovery.

    The energy baseline is the one thing that must NOT come back with it.
    Odometers only count forward, so prev's reading stays a valid distance
    anchor however stale it is; SoC and range fall while the car merely sits,
    so last night's pair would charge the whole night's vampire drain to
    whatever few hundred metres the car moved before it was seen. Here that
    would be 7 km of rated range against 0.3 km driven — an implied
    ~2800 Wh/km, well past MAX_PLAUSIBLE_WH_PER_KM, so the trip keeps cur's
    own SoC/range and measures only the driving (regression test for trip
    309: a 2.5 h sleep inflated a 5.9 km drive's energy and Wh/km by ~17%,
    while the parked gap before it reported an impossible 0.0 kWh)."""
    prev = snap(T0, 10_000.0, 80, range_km=400.0)               # parked last night
    # 10 hours later the car is seen driving, having covered just 0.3 km --
    # too little for the timestamp estimate to clear its own 60s floor.
    cur = snap(T0 + 36_000, 10_000.3, 79, shift="D", speed=40, range_km=393.0)
    _, _, trip, _ = step(prev, cur)
    assert trip is not None
    assert trip["odo_km"] == prev["odo_km"]      # the 0.3 km recovered, not dropped
    assert trip["soc"] == cur["soc"]             # but NOT the night's drain
    assert trip["range_km"] == cur["range_km"]   # and the pair stays consistent
    assert trip["ts"] == cur["ts"]               # start time is now, not 10h ago


def test_short_poor_signal_departure_still_recovers_its_energy():
    """The counterpart to the test above: MAX_PLAUSIBLE_WH_PER_KM must not
    have thrown out the case the recovery exists for. A genuine poor-signal
    departure covers real ground at a real efficiency, so pulling the
    baseline back implies a normal Wh/km and the trip keeps both the
    distance and the energy — only an implausible figure (standby drain
    masquerading as driving) is refused."""
    prev = snap(T0, 5_000.0, 70, range_km=350.0)                # parked, good reading
    # 20-min dead zone off the driveway: 1.5 km covered, 1.5 km of rated
    # range gone with it -> ~233 Wh/km at the 60 kWh test capacity, an
    # entirely ordinary departure figure.
    cur = snap(T0 + 1200, 5_001.5, 70, shift="D", speed=35, range_km=348.5)
    _, _, trip, _ = step(prev, cur)
    assert trip is not None
    assert trip["odo_km"] == prev["odo_km"]      # distance recovered
    assert trip["soc"] == prev["soc"]            # and so is the energy baseline
    assert trip["range_km"] == prev["range_km"]


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


def test_a_trip_opens_on_the_shift_not_on_the_unlock():
    """The ordinary departure sequence: locked, then unlocked and into D, then
    moving. The trip opens at the SHIFT — unlocking is not consulted.

    Worth pinning because the opposite is the intuitive guess, and the code
    once carried an `unlocked_before_drive` flag whose docstring called it "a
    strong signal of driving intent" while nothing read it. It could never
    have worked either: the lock state lives in vehicle_data, which is not
    called while the car is asleep, so an unlock is invisible until the car
    wakes — at which point list_vehicles reports it online and the existing
    wake escalation already forces a read."""
    s1 = snap(T0, 10_000.0, 80, locked=True)                   # locked at home
    s2 = snap(T0 + 60, 10_000.0, 80, locked=False, shift="D")  # unlocked → shift D
    s3 = snap(T0 + 300, 10_010.0, 77, shift="D", speed=70)     # driving

    _, _, trip, _ = step(None, s1)
    assert trip is None, "an unlocked-but-parked car is not a trip"

    _, _, trip, _ = step(s1, s2)
    assert trip is not None, "shifting into D opens it"
    assert trip["odo_km"] == s2["odo_km"], "anchored where the car actually was"

    _, _, trip, _ = step(s2, s3, trip)
    assert trip is not None, "and stays open while it drives"


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


# --- Trimmed-tail standby correction ---------------------------------------

def test_trim_standby_removes_the_parked_tail_from_trip_energy():
    """Regression for trip 316: a 4.2 km arrival into a dead zone was trimmed
    by 1002 s, but the trim moves only the clock — the stop snapshot keeps the
    late reading's SoC, so the trip carried ~17 min of post-arrival standby.
    Against the car's own screen it read 1.11 kWh for a 0.97 kWh drive."""
    from app.sync import trim_standby_kwh

    # 0.50 kW measured standby over the 1001.8 s trim = 0.139 kWh.
    out = trim_standby_kwh(1.11, 4.2, 1001.8, 0.50)
    assert out == 0.971                      # lands on the car's own figure
    assert round(1.11 - out, 3) == 0.139


def test_trim_standby_does_nothing_without_a_measured_rate():
    """No history, no correction. Leaving the energy slightly high is the
    honest failure; inventing a rate would reshape real trip energy."""
    from app.sync import trim_standby_kwh

    assert trim_standby_kwh(1.11, 4.2, 1001.8, None) == 1.11
    assert trim_standby_kwh(1.11, 4.2, 1001.8, 0.0) == 1.11


def test_trim_standby_ignores_trips_that_were_not_trimmed():
    """The ordinary parked close records a real 0.0 trim — nothing to take
    back, and every trip must not quietly lose energy."""
    from app.sync import trim_standby_kwh

    assert trim_standby_kwh(2.0, 10.0, 0.0, 0.5) == 2.0
    assert trim_standby_kwh(2.0, 10.0, None, 0.5) == 2.0


def test_trim_standby_cannot_drain_a_real_drive_to_nothing():
    """An implausibly long trim must not eat the drive itself — floored at
    what the distance alone had to cost."""
    from app.sync import MIN_PLAUSIBLE_WH_PER_KM, trim_standby_kwh

    out = trim_standby_kwh(1.0, 5.0, 36000.0, 1.0)   # 10 h of trim at 1 kW
    assert out == round(5.0 * MIN_PLAUSIBLE_WH_PER_KM / 1000.0, 3)
    assert out > 0


def test_start_recovered_km_says_which_kind_of_zero_a_start_is():
    """start_lost_km reads 0.0 both when nothing could be lost and when the
    departure recovery reclaimed real distance — the two cases most worth
    telling apart, since the second means the trip's distance and energy
    include ground driven before its own start time. start_recovered_km is
    what separates them."""
    # Recovery fires: a 20-min blind gap covering 3 km before the first
    # driving reading, which the recovery pulls back into the trip.
    s1 = snap(T0, 1000.0, 80, range_km=400.0)
    s2 = snap(T0 + 1200, 1003.0, 79, shift="D", speed=45.0, range_km=396.0)
    s3 = snap(T0 + 1800, 1010.0, 78, shift="P", speed=0.0, locked=True, range_km=392.0)
    _, _, trip, _ = step(s1, s2)
    drives, _, _, _ = step(s2, s3, trip)
    assert drives[0]["start_lost_km"] == 0.0        # nothing left lost...
    assert drives[0]["start_recovered_km"] == 3.0   # ...because 3 km came back
    assert drives[0]["distance_km"] == 10.0

    # Ordinary start: anchored at the previous reading, nothing could precede
    # it. Same 0.0 loss, but nothing was reclaimed either.
    p1 = snap(T0, 2000.0, 80, range_km=400.0)
    p2 = snap(T0 + 60, 2000.5, 80, shift="D", speed=30.0, range_km=399.8)
    p3 = snap(T0 + 600, 2005.0, 79, shift="P", speed=0.0, locked=True, range_km=398.0)
    _, _, trip2, _ = step(p1, p2)
    drives2, _, _, _ = step(p2, p3, trip2)
    assert drives2[0]["start_lost_km"] == 0.0
    assert drives2[0]["start_recovered_km"] == 0.0


def test_drive_records_its_odometer_anchors():
    """distance_km says how far a trip ran but not where it sat, and without
    that it can't be reconciled against the readings around it."""
    s1 = snap(T0, 5000.0, 80, range_km=400.0)
    s2 = snap(T0 + 60, 5000.4, 80, shift="D", speed=30.0, range_km=399.8)
    s3 = snap(T0 + 900, 5006.0, 79, shift="P", speed=0.0, locked=True, range_km=398.0)
    _, _, trip, _ = step(s1, s2)
    drives, _, _, _ = step(s2, s3, trip)
    assert drives[0]["start_odo_km"] == 5000.0
    assert drives[0]["end_odo_km"] == 5006.0
    assert round(drives[0]["end_odo_km"] - drives[0]["start_odo_km"], 1) == drives[0]["distance_km"]


# --- Whole-trip climate model ----------------------------------------------

def test_climate_is_stripped_across_the_whole_trip_not_just_stops():
    """The gap this closes: climate runs while the car moves, so gating it on
    sustained stops meant stop-go traffic had nothing stripped at all and the
    driving figure came out equal to the gross (trips 313 and 317 both did,
    while the car's own screen put a fifth of each trip under Climate)."""
    from app.sync import driving_only_wh_per_km

    # Trip 317: 11.2 km, 20 min at 29C, 1.73 kWh gross -> 154 Wh/km.
    out = driving_only_wh_per_km(1.73, 11.2, 20.0, 29.0)
    assert out == 113                       # was 154, i.e. no strip at all
    # Tesla's own Driving line for that trip works out near 108 Wh/km, so the
    # model now lands the right side of the gross rather than on top of it.
    assert 100 < out < 154


def test_driving_energy_is_floored_by_the_distance_not_a_share_of_the_trip():
    """The old cap held the subtraction to a fixed share of the gross, which is
    the wrong shape: non-propulsion load scales with time, so as a fraction of
    a trip it is small on a fast run and large on a slow one. Measured at 65%
    of a 45-minute 8.9 km crawl, where a 40% cap blocked a subtraction the
    car's own numbers said should have been LARGER. The floor is what the
    distance alone must have cost."""
    from app.sync import MIN_PLAUSIBLE_WH_PER_KM, driving_only_kwh

    # A slow crawl: the model wants more than 40% of the gross, and gets it.
    slow = driving_only_kwh(2.26, 45.0, 31.0, distance_km=8.9)
    assert slow < 2.26 * 0.6
    # But never below what the distance itself had to cost.
    absurd = driving_only_kwh(1.0, 600.0, 40.0, distance_km=5.0)
    assert absurd == round(5.0 * MIN_PLAUSIBLE_WH_PER_KM / 1000.0, 10)


def test_accessory_load_is_subtracted_as_well_as_climate():
    """Tesla's breakdown reports climate and "Everything Else" separately, and
    both sit between the gross figure and its Driving line — so modelling only
    climate left this structurally unable to reach it. Accessories measured
    0.40-0.63 kW across the audited trips, the steadiest figure in the set."""
    from app.sync import ACCESSORY_KW, driving_only_kwh

    with_acc = driving_only_kwh(2.0, 60.0, 22.0)      # mild, so climate is minimal
    assert round(2.0 - with_acc, 3) >= round(ACCESSORY_KW, 3)


def test_climate_flag_gates_the_strip_rather_than_prorating_it():
    """A trip driven with climate off must not be charged for it — but a trip
    where the flag was seen on for only part of the drive must be charged in
    full, not pro rata.

    Prorating was measured against the car's own energy breakdown and read far
    too low: on trips 363/359/360 the fraction came out 0.28/0.67/0.88, giving
    0.85/1.32/1.37 kW against a car reporting 1.69/1.77/1.76 — a fraction that
    tracked trip length rather than anything physical, since a cycling
    compressor under a continuous cabin load reads as intermittent through a
    boolean sampled at poll rate."""
    from app.sync import driving_only_kwh

    off = driving_only_kwh(1.73, 20.0, 29.0, climate_min=0.0)
    half = driving_only_kwh(1.73, 20.0, 29.0, climate_min=10.0)
    always = driving_only_kwh(1.73, 20.0, 29.0, climate_min=20.0)
    assert half == always < off      # seen on at all == on throughout
    # Not the full gross even with climate off: accessories run whenever the
    # car is on, and are not gated on the climate flag.
    assert off < 1.73


def test_non_propulsion_load_matches_the_cars_own_breakdown():
    """The three trips the gate was fixed against, checked end to end.

    Each is (gross kWh, distance, our duration, ambient, the car's own
    climate + battery conditioning + everything else in kWh). The car's figures
    fit a flat 1.82 kW with a fixed term of -0.06 kWh — a pure rate — so the
    model's own rate x duration shape is right and only the fraction was
    wrong."""
    from app.sync import driving_only_kwh

    cases = [   # trip, gross, km, min, degC, car non-propulsion kWh, tolerance
        ("363", 2.54, 17.843, 36.0, 33.0, 1.016, 0.05),
        ("359", 4.81, 27.258, 66.0, 33.0, 1.946, 0.05),
        # 30C is the loosest, and the residual is the temperature curve, not
        # the gate: the car's own climate line reads 1.23 kW here against the
        # 1.20 it read at 33C — flat — while CLIMATE_KW_PER_DEGREE swings the
        # model 0.99 -> 1.23 across that span. One sample at 30C is not enough
        # to re-fit a slope on, so it is left alone and pinned loose.
        ("360", 4.74, 26.660, 80.0, 30.0, 2.353, 0.20),
    ]
    for name, gross, km, mins, temp, car, tol in cases:
        ours = gross - driving_only_kwh(gross, mins, temp, climate_min=1.0, distance_km=km)
        assert ours == pytest.approx(car, rel=tol), (
            f"trip {name}: modelled {ours:.3f} kWh against the car's {car:.3f}")


def test_climate_unknown_is_not_read_as_off():
    """None means the car never reported the flag. Treating that as "off"
    would silently switch the correction off for those cars."""
    from app.sync import driving_only_kwh

    assert driving_only_kwh(1.73, 20.0, 29.0, climate_min=None) < 1.73
    assert (driving_only_kwh(1.73, 20.0, 29.0, climate_min=None)
            == driving_only_kwh(1.73, 20.0, 29.0, climate_min=20.0))


def test_climate_minutes_are_tracked_from_the_reported_flag():
    """Tracked off the same interval walk idle uses, and only where the car
    actually reported the flag."""
    from app.sync import _track_climate, climate_on_fraction

    trip = {}
    a = snap(T0, 100.0, 80, shift="D", speed=40)
    b = snap(T0 + 120, 101.0, 80, shift="D", speed=40)
    b["climate_on"] = True
    _track_climate(trip, a, b)
    assert trip["climate_min"] == 2.0
    assert climate_on_fraction(trip) == 1.0

    c = snap(T0 + 240, 102.0, 80, shift="D", speed=40)
    c["climate_on"] = False
    _track_climate(trip, b, c)
    assert trip["climate_min"] == 2.0        # unchanged
    assert climate_on_fraction(trip) == 0.5  # on for half the observed time

    # A car that never reports it reads as unknown, not off.
    blank = {}
    d2 = snap(T0 + 360, 103.0, 80, shift="D", speed=40)
    d2["climate_on"] = None
    _track_climate(blank, c, d2)
    assert climate_on_fraction(blank) == 1.0


# --- Blind folded distance carries its own energy ---------------------------

def test_recovered_departure_distance_is_priced_not_left_at_zero():
    """The departure recovery moves the start anchor back over real ground but
    only brings the SoC/range with it when that pair looks like driving. When
    it doesn't — a long park, where the gap's implied Wh/km is mostly standby
    drain — the distance used to arrive with no energy attached, diluting the
    trip's Wh/km by exactly the recovered share. Same defect the
    sustained-offline top-up had."""
    from app.sync import energy_for_blind_distance

    # 7.9 km logged, 0.5 of it recovered blind, 1.71 kWh measured over the 7.4
    # that carried a reading.
    out = energy_for_blind_distance(1.71, 7.9, 0.5)
    assert round(out, 3) == round(1.71 * 7.9 / 7.4, 3)
    # Wh/km is what's preserved — that is the point.
    assert round(out * 1000 / 7.9) == round(1.71 * 1000 / 7.4)


def test_blind_distance_pricing_is_refused_when_it_would_carry_the_trip():
    """Past half the trip the assumption is doing more work than the
    measurement, and a wrong efficiency would be amplified rather than
    extended."""
    from app.sync import energy_for_blind_distance

    assert energy_for_blind_distance(1.0, 10.0, 6.0) == 1.0     # 60% blind
    assert energy_for_blind_distance(1.0, 10.0, 4.0) > 1.0      # 40% blind


def test_blind_distance_pricing_ignores_trips_with_nothing_folded():
    from app.sync import energy_for_blind_distance

    assert energy_for_blind_distance(1.5, 10.0, 0.0) == 1.5
    assert energy_for_blind_distance(0.0, 10.0, 1.0) == 0.0


def test_trip_records_the_polling_window_at_each_boundary():
    """Every anchor is an estimate placed inside a polling window, so the
    window's width is that end's uncertainty — and nothing previously
    distinguished a trip anchored 30 s apart from one anchored 8 min apart."""
    s1 = snap(T0, 5000.0, 80, range_km=400.0)
    s2 = snap(T0 + 45, 5000.4, 80, shift="D", speed=30.0, range_km=399.8)
    s3 = snap(T0 + 900, 5006.0, 79, shift="P", speed=0.0, locked=True, range_km=398.0)
    _, _, trip, _ = step(s1, s2)
    drives, _, _, _ = step(s2, s3, trip)
    assert drives[0]["start_gap_sec"] == 45.0          # departure window
    assert drives[0]["end_gap_sec"] == round((900 - 45) * 1.0, 1)


def test_trim_pace_trusts_a_real_low_speed_over_the_floor():
    """Nosing into a car park, the last reading really is 5-10 km/h. Forcing
    that up to the 30 km/h floor puts the estimated stop earlier than it
    happened, so the trim under-corrects and the trip still reads long (trip
    316 kept +3 min after a 1002 s trim). The floor is for absent evidence,
    not for overruling it."""
    from app.sync import CITY_SPEED_KMH

    # A slow final approach: last seen at 8 km/h, then a long silent gap.
    a = snap(T0, 100.0, 80, shift="D", speed=8.0, range_km=400.0)
    b = snap(T0 + 60, 100.6, 80, shift="D", speed=8.0, range_km=399.9)
    c = snap(T0 + 1800, 100.9, 79, shift="P", speed=0.0, locked=True, range_km=399.5)
    d = snap(T0 + 3600, 100.9, 79, shift="P", speed=0.0, locked=True, range_km=399.4)
    _, _, trip, _ = step(a, b)
    d1, _, trip, _ = step(b, c, trip)
    d2, _, _, _ = step(c, d, trip) if trip else ([], None, None, None)
    drives = list(d1) + list(d2)
    assert drives, "the trip should close"
    # 0.3 km at 8*0.65 = 5.2 km/h is ~3.5 min of travel, so the stop lands far
    # earlier than the 30 km/h floor's ~36 s would have put it — a bigger,
    # more honest trim.
    assert drives[0]["tail_trim_sec"] > 0
    at_floor = 0.3 / CITY_SPEED_KMH * 3600.0
    at_real = 0.3 / (8.0 * 0.65) * 3600.0
    assert at_real > at_floor


def test_a_stale_park_is_not_a_departure_baseline_however_plausible_it_looks():
    """Regression for trip 319. A 2.3 h park offered 0.52 km at ~406 Wh/km —
    under MAX_PLAUSIBLE_WH_PER_KM, because 400 Wh/km is ordinary for half a
    kilometre of parking-lot crawl in the heat. So the efficiency gate passed
    it, the SoC baseline came back with the odometer, and the park's standby
    drain went into the trip, putting it 5% over the car's own figure.

    Duration is what separates a departure from a stale anchor: minutes versus
    hours. The odometer still comes back either way — that part is measured."""
    from app.sync import DEPARTURE_STALE_MAX_MIN

    # 2.3 h parked (well past the bound), then driving, 0.52 km covered.
    p1 = snap(T0, 9000.0, 70, range_km=350.0)
    p2 = snap(T0 + 8220, 9000.52, 70, shift="D", speed=35.0, range_km=348.9)
    _, _, trip, _ = step(p1, p2)
    assert trip["odo_km"] == p1["odo_km"]            # distance still recovered
    assert trip["start_recovered_km"] == 0.52
    assert trip["soc"] == p2["soc"]                  # but not the stale baseline
    assert trip["start_energy_recovered"] is False

    # The same movement inside a plausible departure window keeps its energy.
    q1 = snap(T0, 9000.0, 70, range_km=350.0)
    q2 = snap(T0 + int(DEPARTURE_STALE_MAX_MIN * 60) - 60, 9000.52, 70,
              shift="D", speed=35.0, range_km=348.9)
    _, _, trip2, _ = step(q1, q2)
    assert trip2["odo_km"] == q1["odo_km"]
    assert trip2["soc"] == q1["soc"]
    assert trip2["start_energy_recovered"] is True


def test_a_recovered_departure_starts_where_the_car_was_parked():
    """Regression for trip 322. The car left home during a network blackout and
    the first poll caught it 1.579 km along. The odometer was pulled back
    correctly, but the coordinates weren't, so the trip recorded an odometer
    reading from the driveway and a position 725 m away on a highway — and
    reverse-geocoded to the highway. A parked car doesn't move, so prev's fix
    is exactly where the trip began no matter how late the poll arrived."""
    home = (5.3430, 100.3107)
    highway = (5.3494, 100.3095)
    # Parked overnight, exactly as trip 322 was: the long gap is what makes
    # this a departure recovery at all (a short one anchors at prev already).
    p1 = snap(T0, 9000.0, 70, range_km=350.0, lat=home[0], lon=home[1])
    p2 = snap(T0 + 44400, 9001.579, 70, shift="D", speed=40.0, range_km=349.0,
              lat=highway[0], lon=highway[1])
    _, _, trip, _ = step(p1, p2)
    assert trip["odo_km"] == p1["odo_km"]
    assert (trip["lat"], trip["lon"]) == home     # not where the poll found it

    # And the whole way through to the logged trip's start_location.
    p3 = snap(T0 + 46200, 9010.0, 68, shift="P", speed=0.0, locked=True,
              range_km=340.0, lat=5.40, lon=100.33)
    drives, _, _, _ = step(p2, p3, trip)
    assert drives and drives[0]["start_location"] == "5.3430, 100.3107"


def test_a_departure_recovery_keeps_a_known_fix_when_prev_has_none():
    """Blanking a position we have to adopt one we don't would lose information
    rather than correct it — the odometer still comes back either way."""
    p1 = snap(T0, 9000.0, 70, range_km=350.0)                  # no lat/lon
    p2 = snap(T0 + 44400, 9001.579, 70, shift="D", speed=40.0, range_km=349.0,
              lat=5.3494, lon=100.3095)
    _, _, trip, _ = step(p1, p2)
    assert trip["odo_km"] == p1["odo_km"]
    assert (trip["lat"], trip["lon"]) == (5.3494, 100.3095)


def test_a_blind_departure_costs_more_than_the_trip_average():
    """A blind stretch at the START of a trip is not an average piece of it —
    it is the first minutes, with the cabin being pulled down from a hot park,
    a cold drivetrain, and a crawl out of a car park. Pricing it at the trip's
    average understates the trip by the blind share times the difference.

    Measured against the car's own percent-consumed (which cancels the capacity
    constant): a trip with no blind distance read +0.6%, one with 1.7% blind
    read -0.9%, one with 9.2% blind read -5.2%. The last two independently
    imply 1.54x and 1.56x."""
    from app.sync import DEPARTURE_BLIND_LOAD, energy_for_blind_distance

    # 10 km trip, 1 km of it blind, 2.0 kWh measured over the other 9.
    flat = energy_for_blind_distance(2.0, 10.0, 1.0)
    dep = energy_for_blind_distance(2.0, 10.0, 1.0, departure_blind_km=1.0)
    assert flat == pytest.approx(2.0 * 10.0 / 9.0)          # unchanged default
    assert dep == pytest.approx(2.0 * (9.0 + DEPARTURE_BLIND_LOAD) / 9.0)
    assert dep > flat

    # An arrival-side blind stretch keeps the flat rate: the same physics runs
    # the other way at the end of a drive, and nothing has measured it.
    assert energy_for_blind_distance(2.0, 10.0, 1.0, departure_blind_km=0.0) == flat

    # Mixed: only the departure share is loaded.
    both = energy_for_blind_distance(2.0, 10.0, 1.0, departure_blind_km=0.4)
    assert both == pytest.approx(2.0 * (9.0 + 0.4 * DEPARTURE_BLIND_LOAD + 0.6) / 9.0)
    assert flat < both < dep

    # The refusal threshold still measures how much of the trip is INFERRED
    # rather than measured, so it reads the real blind distance, not the
    # weighted one — weighting cannot add measurements.
    assert energy_for_blind_distance(1.0, 10.0, 6.0, departure_blind_km=6.0) == 1.0
    # And a departure share can never exceed the blind distance it is part of.
    assert (energy_for_blind_distance(2.0, 10.0, 1.0, departure_blind_km=99.0)
            == pytest.approx(dep))


def test_a_recovered_departure_prices_its_blind_kilometres_high():
    """End to end through _drive_from: the departure recovery pulls the anchor
    back over ground with no SoC/range reading of its own, and that ground now
    costs 1.55x rather than the trip average. Trip 333 read 5.2% under the car
    on exactly this shape."""
    from app.sync import DEPARTURE_BLIND_LOAD, _drive_from

    def trip(recovered):
        start = {"ts": T0, "odo_km": 8000.0, "soc": 80, "range_km": 400.0,
                 "max_speed": 60.0, "idle_min": 0.0, "still_run": 0.0,
                 "still_since": None, "start_recovered_km": recovered,
                 "start_energy_recovered": False}
        stop = snap(T0 + 1800, 8010.0, 77, shift="P", speed=0.0, range_km=385.0)
        return _drive_from(start, stop, 70.0, 60.0, 0.0, idle_tracked=True)

    clean, blind = trip(0.0), trip(1.0)
    assert clean["distance_km"] == blind["distance_km"] == 10.0
    # 9 km carried a reading; the blind km is priced at 1.55 of that rate.
    assert blind["energy_used_kwh"] == pytest.approx(
        clean["energy_used_kwh"] * (9.0 + DEPARTURE_BLIND_LOAD) / 9.0, abs=0.01)


def test_a_short_park_is_not_swallowed_by_the_next_trip():
    """Trip 340. The car sat parked 11 minutes, then drove. The gap fell just
    under STALE_ANCHOR_MIN (15 min) and cur read shift D at zero speed, so
    neither existing check fired and the trip anchored back at the parked
    reading: 16 minutes against the car's own 5, and 0.50 kWh against 0.38 —
    the extra being eleven minutes of standby drain counted as driving.

    The odometer settles it. The exception for a still-creeping departure is
    about a car that has COVERED GROUND; one that has not moved has not left,
    whatever its gear reads."""
    from app.sync import DEPARTURE_STILL_MAX_KM

    parked = snap(T0, 9000.0, 80, shift="P", speed=0.0, locked=True, range_km=400.0)
    # 11 minutes later, in gear, not yet moving, odometer untouched.
    rolling = snap(T0 + 660, 9000.0, 79, shift="D", speed=0.0, range_km=396.0)
    drives, charges, trip, _c = process_snapshot(parked, rolling, None, None, 70.0, 0.5)
    assert trip is not None
    # Anchored at cur, so the park is outside the trip on both counts.
    assert trip["ts"] == rolling["ts"]
    assert trip["soc"] == rolling["soc"]
    assert trip["range_km"] == rolling["range_km"]

    # A car that HAS covered ground in the gap still anchors back at prev —
    # that distance belongs to the trip and this must not discard it.
    crept = snap(T0 + 660, 9000.0 + DEPARTURE_STILL_MAX_KM * 4, 79,
                 shift="D", speed=0.0, range_km=396.0)
    _d, _c2, trip2, _c3 = process_snapshot(parked, crept, None, None, 70.0, 0.5)
    assert trip2 is not None and trip2["ts"] == parked["ts"]


def test_rechecked_overnight_park_recovers_a_long_departure():
    """Measured, trip 382: an overnight park at Home, polled every ten minutes
    the whole night, then a departure first seen 3.4 km downroad on a highway.

    The old guard measured staleness by how long the CAR SAT — nine hours — so
    it refused to reach back, and the trip logged 5.9 km of a 9.3 km drive
    beginning from a road it had already been on for five minutes. But nothing
    about that gap was unobserved: every recheck reported the car not online,
    and a driving car is online. What the guard needs to bound is how long the
    car could have been MOVING unseen, which those rechecks pin to minutes.
    """
    park = T0
    depart = T0 + 9 * 3600                        # nine hours later
    s1 = snap(park, 29_597.086, 60, range_km=270.0)                   # parked Home
    # First driving reading, already 3.4 km out — past DEPARTURE_GAP_MAX_KM,
    # so this recovers only because the gap was confirmed quiet throughout.
    s2 = snap(depart, 29_600.5, 58, shift="D", speed=60, range_km=261.0)

    # Last confirmed-quiet poll landed 8 minutes before the car was seen moving.
    _, _, trip, _ = process_snapshot(
        s1, s2, None, None, 60.0, 0.90,
        last_quiet_ts=depart - 8 * 60,
    )
    assert trip is not None
    assert trip["odo_km"] == 29_597.086          # reached back to the Home anchor
    assert trip["start_recovered_km"] == pytest.approx(3.414, abs=0.001)
    assert trip["start_lost_km"] == 0.0
    # The ENERGY baseline is a separate question and stays refused: the car
    # really did sit nine hours, and that standby drain is not this trip's.
    assert trip["start_energy_recovered"] is False
    assert trip["soc"] == 58                      # cur's, not prev's


def test_unobserved_long_gap_still_refuses_to_reach_back():
    """The counterpart, and why the fix is not simply a wider distance cap.

    Trip 368: the poller was DEAD for 12.4 hours, and the ground on the far
    side held an entire Office->Home drive, the stop after it, and the first
    half of the next journey. Recovering across it logged one trip where two
    had happened.

    Identical snapshots to the test above — same anchor, same shape — and the
    only difference is that no poll confirmed the car still during the gap. A
    hole in the observations has to read as a hole.
    """
    park = T0
    depart = T0 + 9 * 3600
    s1 = snap(park, 29_597.086, 60, range_km=270.0)
    s2 = snap(depart, 29_600.5, 58, shift="D", speed=60, range_km=261.0)

    for quiet in (None,                    # nothing ever recorded
                  park - 600,              # a confirmation from BEFORE the gap
                  park + 60):              # one early in it, then silence
        _, _, trip, _ = process_snapshot(
            s1, s2, None, None, 60.0, 0.90, last_quiet_ts=quiet,
        )
        assert trip is not None
        assert trip["odo_km"] == 29_600.5, quiet          # anchored where seen
        assert trip["start_recovered_km"] == 0.0, quiet
        assert trip["start_lost_km"] == pytest.approx(3.414, abs=0.001), quiet


def _recovered_departure(speed_kmh, measured_min=22):
    """Trip 382's shape: nine-hour park, rechecked throughout, first seen 3.366
    km out. Returns (open_trip, closed_drive). ``speed_kmh`` sets the recovered
    head's assumed pace; ``measured_min`` how long the WATCHED part took. Same
    odometer and same energy in every combination — only the pace comparison
    the premium is gated on changes.
    """
    park, depart = T0, T0 + 9 * 3600
    s1 = snap(park, 29_597.086, 60, range_km=270.0)
    s2 = snap(depart, 29_600.452, 58, shift="D", speed=speed_kmh, range_km=261.0)
    s3 = snap(depart + measured_min * 60, 29_606.381, 56, range_km=255.0, locked=True)
    _, _, trip, _ = process_snapshot(s1, s2, None, None, 60.0, 0.90,
                                     last_quiet_ts=depart - 8 * 60)
    drives, _, _, _ = process_snapshot(s2, s3, trip, None, 60.0, 0.90)
    (drive,) = drives
    return trip, drive


def test_departure_premium_skipped_when_the_head_outran_the_trip():
    """Measured, trip 382: 3.366 km recovered at 41 km/h while the watched part
    of the same drive averaged 16. The premium prices a hot cabin, a cold
    drivetrain and a crawl out of a car park — time-based costs that only read
    as a high Wh/km when little ground is covered while they run. This head
    left faster than the drive it opened, and billing it 1.55x per kilometre
    added 0.088 kWh: 1.58 against the car's own 1.46, where dropping it gives
    1.49.
    """
    trip, drive = _recovered_departure(63)
    assert trip["start_recovered_km"] == pytest.approx(3.366, abs=0.001)
    assert trip["start_blind_kmh"] == pytest.approx(41.0, abs=0.1)
    assert trip["start_energy_recovered"] is False   # nine hours is still stale

    measured = 29_606.381 - 29_600.452
    base = _energy_kwh({"soc": 58, "range_km": 261.0},
                       {"soc": 56, "range_km": 255.0}, 60.0)
    # Flat average across the whole head: no premium term.
    assert drive["energy_used_kwh"] == pytest.approx(
        base * (measured + 3.366) / measured, abs=0.01)


def test_departure_premium_kept_when_the_head_really_did_crawl():
    """The other half, and why this is a gate rather than a deletion.

    Same trip, same distance, same energy — only the pace of the head against
    the pace of the drive it opened. A departure that dawdles out at the
    DEPARTURE_PACE_KMH floor while the rest of the drive runs freely IS the
    car-park crawl the premium was fitted on (trips 333/334, ~1 km blind,
    measured 1.54 and 1.56), and it keeps it.
    """
    trip, drive = _recovered_departure(10, measured_min=6)   # 5.9 km in 6 min
    assert trip["start_blind_kmh"] == pytest.approx(20.0, abs=0.1)

    measured = 29_606.381 - 29_600.452
    base = _energy_kwh({"soc": 58, "range_km": 261.0},
                       {"soc": 56, "range_km": 255.0}, 60.0)
    weighted = 1.0 * 1.55 + (3.366 - 1.0)          # premium on the first km only
    assert drive["energy_used_kwh"] == pytest.approx(
        base * (measured + weighted) / measured, abs=0.01)
    # And it is strictly the dearer of the two readings.
    assert drive["energy_used_kwh"] > _recovered_departure(63)[1]["energy_used_kwh"]


def test_place_departure_pace_shortens_the_back_dated_head():
    """A blind departure is back-dated by blind distance / an assumed pace, so
    the pace is the only thing standing between the logged start and the real
    one — and it is a property of the road out, not of cars in general.

    Three Home departures (trips 397/402/407) ran their blind heads at 45-55
    km/h while the default assumed 20, putting every start 9-12 minutes early.
    Passing the place's own figure has to move the start, and by the ratio.
    """
    s1 = snap(T0, 1000.0, 80, range_km=400.0)
    # Seen 40 min later, 6 km further on and already at speed: the car left at
    # some point inside the window and nothing observed when.
    s2 = snap(T0 + 2400, 1006.0, 78, shift="D", speed=50.0, range_km=392.0)

    _, _, slow, _ = process_snapshot(s1, s2, None, None, 60.0, 0.90)
    _, _, fast, _ = process_snapshot(s1, s2, None, None, 60.0, 0.90,
                                     departure_pace_kmh=45.0)

    # Same ground either way — the odometer anchor does not depend on the pace.
    assert slow["odo_km"] == fast["odo_km"] == 1000.0
    # 6 km at 32.5 (speed*0.65 beats the 20 floor) vs at 45: ~11.1 min vs 8.0.
    assert round((s2["ts"] - slow["ts"]) / 60.0, 1) == 11.1
    assert round((s2["ts"] - fast["ts"]) / 60.0, 1) == 8.0
    assert fast["start_blind_kmh"] == 45.0

    # 0 / None mean "no opinion", not "instantly", and fall back to the default.
    _, _, unset, _ = process_snapshot(s1, s2, None, None, 60.0, 0.90,
                                      departure_pace_kmh=0.0)
    assert unset["ts"] == slow["ts"]


def test_place_departure_pace_never_undercuts_the_observed_speed():
    """The floor is a floor. A place set slower than the speed the car was
    actually doing when first seen must not drag the estimate back down — that
    reading is evidence and the setting is only a prior."""
    s1 = snap(T0, 1000.0, 80, range_km=400.0)
    s2 = snap(T0 + 2400, 1006.0, 78, shift="D", speed=60.0, range_km=392.0)

    _, _, slow_place, _ = process_snapshot(s1, s2, None, None, 60.0, 0.90,
                                           departure_pace_kmh=10.0)
    # 60 * 0.65 = 39 wins over the 10.
    assert slow_place["start_blind_kmh"] == 39.0
