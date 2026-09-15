"""Tests for the telemetry shadow machine and snapshot helpers (app/sync.py)."""
import pytest

from app.sync import (_energy_kwh, is_driving,
                      snapshot_from_telemetry, advance_shadow,
                      snapshot_from_vehicle_data,
                      recover_sleep_gap, ENERGY_QUANTUM_KWH, ENERGY_SAMPLE_SEC, MILES_TO_KM,
                      advance_charge, settle_charge, CHARGE_GAP_SEC,
                      SHADOW_GAP_SEC, SHADOW_SETTLE_SEC)

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


def test_implied_capacity_from_measured_charge():
    from app.sync import CHARGE_EFFICIENCY, implied_capacity_kwh

    # Tesla measured 18.5 kWh for a 55->80% (25%) charge => 74 kWh of energy
    # delivered per 100%, before any of it is lost getting back out.
    c = {"energy_measured": True, "start_soc": 55, "end_soc": 80,
         "energy_added_kwh": 18.5, "charge_type": "DC"}
    assert implied_capacity_kwh(c) == round(74.0 * CHARGE_EFFICIENCY, 1)
    # The identical session on AC lands identically. It did not always: DC was
    # exempt from the correction until this car had enough wide Supercharger
    # sessions to check, and its six highest-precision charges then turned out
    # to interleave AC and DC with no separation at all. Charge type stops
    # changing the answer here, and a regression that reintroduced the split
    # would show up as these two disagreeing.
    ac = {**c, "charge_type": "AC"}
    assert implied_capacity_kwh(ac) == implied_capacity_kwh(c)
    # Charge type missing (legacy data) lands there too, which is now true by
    # construction rather than by defaulting to the commoner case.
    assert implied_capacity_kwh({k: v for k, v in c.items() if k != "charge_type"}) == \
        implied_capacity_kwh(c)
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


# --- Whole-trip climate model ----------------------------------------------

def test_climate_is_stripped_across_the_whole_trip_not_just_stops():
    """The gap this closes: climate runs while the car moves, so gating it on
    sustained stops meant stop-go traffic had nothing stripped at all and the
    driving figure came out equal to the gross (trips 313 and 317 both did,
    while the car's own screen put a fifth of each trip under Climate)."""
    from app.sync import driving_only_wh_per_km

    # Trip 317: 11.2 km, 20 min at 29C, 1.73 kWh gross -> 154 Wh/km.
    out = driving_only_wh_per_km(1.73, 11.2, 20.0, 29.0)
    assert out == 110                       # was 154, i.e. no strip at all
    # 106 rather than the 113 this first pinned because the two rate constants
    # were later set to their own measured means. What is under test is that
    # the gross is stripped at all, not where the constants sit.
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
        ("363", 2.54, 17.843, 36.0, 33.0, 1.016, 0.09),
        ("359", 4.81, 27.258, 66.0, 33.0, 1.946, 0.05),
        # Two more read straight off the car's drive-level split. Both sit 19%
        # under, and both are pinned loose for the reason ACCESSORY_KW's note
        # sets out: 406 and 363 are the SAME ambient and the car's own total
        # reads 2.13 kW for one against 1.69 for the other, so no single rate
        # reaches both. Pinned at all so that a future re-fit has to answer to
        # them rather than to the two trips the model already happens to suit.
        ("407", 1.78, 11.287, 19.0, 31.0, 0.616, 0.17),
        ("406", 1.65, 7.925, 25.0, 33.0, 0.889, 0.17),
        # 408 is the cleanest case in the set and pinned tight because of it:
        # the car's own duration and ours agree exactly (24 min), so nothing
        # about the clock is standing in for the rate. -4%.
        ("408", 1.66, 12.533, 24.0, 32.0, 0.684, 0.05),
        # 409 is why the temperature slope stays where it is. It is 30C, the
        # same ambient as 360, and it reads +0.5% where 360 reads -16% — so
        # the residual there was never the curve, and re-fitting a slope to
        # close it would be fitting one trip's scatter.
        ("409", 2.02, 11.215, 36.0, 30.0, 0.889, 0.08),
        # 29C, the coldest sample in the set and one of the HIGHEST draws — which
        # is the clearest single argument against the temperature slope.
        ("418", 2.68, 15.179, 38.0, 29.0, 1.163, 0.21),
        # 419 is the first trip logged AFTER the two rates moved, so it is the
        # only genuinely out-of-sample check on them. Its propulsion figure
        # came out 78 Wh/km against the car's own Driving+Elevation of 79.1 —
        # 0.4% — where the old constants would have read 88.2, +11.5%.
        ("419", 2.35, 14.771, 40.0, 31.0, 1.094, 0.05),
        # 29C, and the OTHER 29C sample (418) reads 40% higher. The two coldest
        # trips in the set disagree by more than the whole band does, which is
        # what keeps CLIMATE_KW_PER_DEGREE where it is.
        ("420", 2.97, 18.560, 47.0, 29.0, 1.026, 0.17),
        ("422", 1.61, 10.223, 25.0, 32.0, 0.616, 0.20),
        # 423's own tolerance is loose for a reason that is about the CHECK,
        # not the model: the car reports each component to 0.1% of pack, and
        # this trip's whole non-propulsion is 0.5%. That is +/-10% before
        # anything is compared. 419 and 420 carry +/-3%, 406 and 407 +/-6-8%.
        # Short trips are weak evidence here however cleanly they log.
        ("423", 1.05, 9.214, 13.0, 34.0, 0.342, 0.23),
        ("447", 1.66, 11.185, 23.0, 31.0, 0.617, 0.05),
        # 30C is the loosest, and the residual is the temperature curve, not
        # the gate: the car's own climate line reads 1.23 kW here against the
        # 1.20 it read at 33C — flat — while CLIMATE_KW_PER_DEGREE swings the
        # model 0.99 -> 1.23 across that span. One sample at 30C is not enough
        # to re-fit a slope on, so it is left alone and pinned loose.
        ("360", 4.74, 26.660, 80.0, 30.0, 2.353, 0.12),
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

    assert energy_for_blind_distance(1.0, 10.0, 8.0) == 1.0     # 80% blind
    assert energy_for_blind_distance(1.0, 10.0, 4.0) > 1.0      # 40% blind


def test_blind_distance_pricing_ignores_trips_with_nothing_folded():
    from app.sync import energy_for_blind_distance

    assert energy_for_blind_distance(1.5, 10.0, 0.0) == 1.5
    assert energy_for_blind_distance(0.0, 10.0, 1.0) == 0.0


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
    assert energy_for_blind_distance(1.0, 10.0, 8.0, departure_blind_km=8.0) == 1.0
    # And a departure share can never exceed the blind distance it is part of.
    assert (energy_for_blind_distance(2.0, 10.0, 1.0, departure_blind_km=99.0)
            == pytest.approx(dep))


def test_every_place_that_corrects_a_charge_uses_the_same_rule():
    """The efficiency correction lives in three places and they must agree.

    Measured, twice now in this project: a constant changed in the sites that
    DISPLAY a figure and missed in the one that SETS it reads exactly like the
    change not working, and costs a deploy to find. When the correction
    stopped being AC-only, _measured_capacity — the path that actually
    supplies capacity_kwh to every kWh and every ringgit — kept its
    ``!= "DC"`` guard while the two evidence endpoints did not.

    So this walks the source. Any surviving charge-type condition around the
    constant is the bug, whatever the tests around it say."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("app/sync.py", "app/api/routes.py"):
        text = (root / name).read_text()
        for match in re.finditer(r"CHARGE_EFFICIENCY", text):
            # The 200 characters before each use, minus comments — a charge
            # type mentioned there means the correction is still conditional.
            before = text[max(0, match.start() - 200):match.start()]
            code = "\n".join(ln for ln in before.splitlines()
                             if not ln.strip().startswith("#"))
            assert '"DC"' not in code and "'DC'" not in code, (
                f"{name}: charge type still gates the efficiency correction "
                f"near offset {match.start()}")


def test_a_mostly_blind_trip_is_priced_from_the_fleet_not_from_its_own_sliver():
    """Holding Wh/km constant is sound while most of a trip carried a reading.
    It stops being sound when the reading covers a sliver: the sliver is then a
    small, biased sample of one trip rather than an estimate of it.

    Measured, trip 500 — 2.96 km of a 4.12 km drive unseen, so the rate came
    from 1.16 km of the slowest, most congested part and was marked up 1.55x
    on top. It read 207 Wh/km against the car's 161.9. Priced at this car's own
    median instead, the same trip comes to 0.72 kWh against the car's 0.70."""
    from app.sync import BLIND_RATE_FALLBACK_SHARE, energy_for_blind_distance

    # A lightly blind trip is unchanged: its own rate is still the best
    # estimate of itself, and the departure premium still applies.
    light = energy_for_blind_distance(4.0, 30.0, 3.0, departure_blind_km=3.0)
    assert light == energy_for_blind_distance(
        4.0, 30.0, 3.0, departure_blind_km=3.0, fleet_wh_per_km=173.0)
    assert light > 4.0

    # Trip 500's shape: 1.16 km measured at 0.211 kWh, 2.96 km unseen.
    own = energy_for_blind_distance(0.211, 4.118, 2.96, departure_blind_km=2.96)
    fleet = energy_for_blind_distance(0.211, 4.118, 2.96, departure_blind_km=2.96,
                                      fleet_wh_per_km=173.0)
    assert own == pytest.approx(0.85, abs=0.02)     # what it used to report
    assert fleet == pytest.approx(0.72, abs=0.02)   # the car said 0.70
    # And the premium is gone with it — a fleet median already contains
    # everybody's departures, so charging one again counts it twice.
    assert fleet == pytest.approx(
        energy_for_blind_distance(0.211, 4.118, 2.96, departure_blind_km=0.0,
                                  fleet_wh_per_km=173.0), abs=1e-9)

    # The boundary is the share, and either side of it behaves differently.
    just_under = 4.118 * BLIND_RATE_FALLBACK_SHARE - 0.01
    assert (energy_for_blind_distance(0.211, 4.118, just_under, fleet_wh_per_km=173.0)
            == energy_for_blind_distance(0.211, 4.118, just_under))

    # No fleet rate yet (a young history) leaves the old behaviour exactly.
    assert energy_for_blind_distance(0.211, 4.118, 2.96, departure_blind_km=2.96,
                                     fleet_wh_per_km=None) == own

    # PAST the refusal threshold the fleet rate still applies, and the
    # ordering is the point. BLIND_DISTANCE_MAX_SHARE exists because
    # projecting the trip's own rate across a large blind share is unsound; a
    # fleet rate is not the trip's own rate. Behind the refusal this branch
    # could never run for the trips it was written for.
    #
    # Measured, trip 505: 6.278 km of an 8.0 km drive unseen — 78.5%, three
    # points past the refusal — reported no energy and so no cost at all,
    # which is the outcome the pricing exists to prevent, produced by the
    # pricing declining.
    assert energy_for_blind_distance(0.30, 8.0, 6.278) == 0.30
    priced = energy_for_blind_distance(0.30, 8.0, 6.278, fleet_wh_per_km=173.0)
    assert priced == pytest.approx(1.386, abs=0.01)
    assert priced * 1000.0 / 8.0 == pytest.approx(173.0, abs=1.0)

    # A trip with no measured energy at all is still priceable from the fleet
    # — every kilometre of it is blind, so there was never a rate of its own
    # to refuse in the first place.
    assert energy_for_blind_distance(0.0, 8.0, 8.0, fleet_wh_per_km=173.0) == \
        pytest.approx(1.384, abs=0.01)


def test_snapshot_from_telemetry_uses_the_car_s_own_units():
    """Telemetry is imperial; the snapshot must be metric.

    Pinned to real values from this car, because the units are not documented
    and getting them wrong is the exact failure this migration exists to
    avoid: Odometer read as km rather than miles would make every trip 38%
    short, and it would look like the car disagreeing with itself.
    """
    fields = {
        "Odometer": 19326.78624168258,       # dashboard reads 31,103 km
        "RatedRange": 71.47359966327582,     # 452 km at 100% on 25.456% SoC
        "VehicleSpeed": 26.71896126620536,   # exactly 43 km/h in mph
        "Soc": 25.456076340162788,
        "EnergyRemaining": 20.499999541789293,
        "Gear": "ShiftStateD",
        "OutsideTemp": 30.5,                 # Celsius, unlike the distances
        "Location": {"latitude": 5.342846, "longitude": 100.310433},
        "Locked": True,
        "SentryMode": "SentryModeStateOff",
        "DetailedChargeState": "DetailedChargeStateDisconnected",
        "DoorState": {"DriverFront": False, "TrunkRear": False},
    }
    snap = snapshot_from_telemetry(fields, ts=1757260000.0)

    assert round(snap["odo_km"]) == 31103          # matches the dashboard
    assert round(snap["speed_kmh"]) == 43          # whole km/h, round-tripped
    assert round(snap["range_km"] / snap["soc"] * 100) == 452
    assert snap["out_temp"] == 30.5                # NOT converted
    assert snap["shift"] == "D"
    assert is_driving(snap)
    assert snap["charging"] is False
    assert snap["lat"] == 5.342846 and snap["lon"] == 100.310433
    assert snap["sentry_mode"] is False            # confirmed off, not unknown
    assert snap["doors_open"] is False
    # Energy measured, not derived from SoC and a capacity constant.
    assert snap["energy_kwh"] == 20.499999541789293


def test_snapshot_from_telemetry_keeps_unknown_distinct_from_off():
    """A field the car does not stream is None, never False.

    The parked-drain code distinguishes "Tesla did not report this" from "this
    is confirmed off"; inventing False would make a sleeping car look like one
    with everything verified quiet.
    """
    snap = snapshot_from_telemetry({"Soc": 50.0}, ts=1757260000.0)
    assert snap["sentry_mode"] is None
    assert snap["doors_open"] is None
    assert snap["climate_on"] is None
    assert snap["shift"] == "P"
    assert not is_driving(snap)


def _tel(ts, odo_mi, energy, gear="ShiftStateD", speed_mph=20.0, soc=50.0,
         door=False):
    return snapshot_from_telemetry({
        "Odometer": odo_mi, "EnergyRemaining": energy, "Gear": gear,
        "VehicleSpeed": speed_mph, "Soc": soc,
        "DoorState": {"DriverFront": door},
    }, ts=ts)


def test_shadow_trip_measures_energy_by_subtraction():
    """A shadow trip's energy comes from the pack, not from SoC x capacity.

    This is the point of the whole telemetry migration: polling must turn a
    whole-percent SoC into kWh using a capacity constant that has never been
    pinned down, while EnergyRemaining makes it a subtraction.
    """
    shadow: dict = {}
    assert advance_shadow(shadow, _tel(0, 100.0, 30.0, soc=50.0)) is None
    assert advance_shadow(shadow, _tel(600, 106.0, 28.5, soc=48.0)) is None
    # Stopped, but not yet long enough to count as arrived.
    assert advance_shadow(shadow, _tel(660, 106.0, 28.5, gear="ShiftStateP",
                                       speed_mph=0.0, soc=48.0, door=True)) is None
    trip = advance_shadow(shadow, _tel(900, 106.0, 28.5, gear="ShiftStateP",
                                       speed_mph=0.0, soc=48.0)) 
    assert trip is not None
    assert round(trip["distance_km"], 1) == 9.7      # 6 miles
    assert trip["energy_kwh"] == 1.5                 # 30.0 - 28.5, measured
    assert round(trip["wh_per_km"]) == 155
    # 11 minutes — when the car stopped, NOT when the settle window expired
    # three minutes later. Closing on the later snapshot would inflate every
    # duration and understate every average speed.
    assert trip["duration_min"] == 11.0
    assert trip["end_ts"] == 660


def test_shadow_trip_does_not_split_at_a_traffic_light():
    """Sitting still briefly is part of the journey, not the end of it."""
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0))
    # Two minutes stationary in D — a light, not an arrival.
    assert advance_shadow(shadow, _tel(60, 101.0, 29.8, speed_mph=0.0)) is None
    assert advance_shadow(shadow, _tel(180, 101.0, 29.8, speed_mph=0.0)) is None
    assert advance_shadow(shadow, _tel(240, 102.0, 29.6)) is None
    assert shadow.get("open") is not None            # still one trip


def test_shadow_trip_closes_at_the_last_motion_when_the_stream_stops():
    """A car that sleeps without a final ShiftStateP must not swallow the
    next journey — the trip ends where the evidence ends."""
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0))
    advance_shadow(shadow, _tel(300, 105.0, 28.8))
    # Nothing for three hours, then the car wakes elsewhere.
    trip = advance_shadow(shadow, _tel(11000, 105.0, 28.8, gear="ShiftStateP",
                                       speed_mph=0.0))
    assert trip is not None
    assert trip["end_ts"] == 300                     # not 11000
    assert round(trip["distance_km"], 1) == 8.0      # 5 miles


def _bms_tel(ts, odo_mi, energy, bms, gear="ShiftStateD", speed_mph=20.0,
             seat=True):
    return snapshot_from_telemetry({
        "Odometer": odo_mi, "EnergyRemaining": energy, "Gear": gear,
        "VehicleSpeed": speed_mph, "Soc": 50.0, "BMSState": bms,
        "DriverSeatOccupied": seat,
    }, ts=ts)


def test_the_car_s_own_bms_ends_a_trip_while_the_driver_is_still_seated():
    """BMSState leaving Drive is the end of the journey, timer or not.

    A driver who parks and stays in the seat is the case this app handled
    worst: no exit to see, so it waited SHADOW_SETTLE_NO_EXIT_SEC — ninety
    minutes — before believing a finished trip was finished. Measured
    against five real boundaries, the BMS left Drive within half a minute of
    every one of them, and it held Drive through a five-minute idle rather
    than being fooled by the gear.
    """
    shadow: dict = {}
    advance_shadow(shadow, _bms_tel(0, 100.0, 30.0, "BMSStateDrive"))
    advance_shadow(shadow, _bms_tel(600, 106.0, 28.5, "BMSStateDrive"))
    # Parked, driver still aboard. Nothing here says the journey is over.
    assert advance_shadow(shadow, _bms_tel(660, 106.0, 28.5, "BMSStateDrive",
                                           gear="ShiftStateP", speed_mph=0.0)) is None
    assert advance_shadow(shadow, _bms_tel(700, 106.0, 28.5, "BMSStateDrive",
                                           gear="ShiftStateP", speed_mph=0.0)) is None
    assert shadow.get("open") is not None, "the timer alone would wait 90 minutes"

    trip = advance_shadow(shadow, _bms_tel(720, 106.02, 28.4, "BMSStateSupport",
                                           gear="ShiftStateP", speed_mph=0.0))
    assert trip is not None
    assert trip["ended_on"] == "bms"
    # Ends when the car STOPPED, not when the BMS got round to saying so.
    assert trip["end_ts"] == 660
    # ...but measured with the newer odometer, which is what a settled
    # arrival reading is for.
    assert trip["end_odo_km"] == pytest.approx(106.02 * MILES_TO_KM, abs=0.01)


def test_park_ends_the_trip_without_waiting_for_the_speed_record():
    """Gear arrives on change, speed every ten seconds. Park wins.

    Trip 537 selected Park at 12:33:04 and this closed it at 12:33:14,
    because the composite still carried the speed from four seconds before
    the lever moved. Ten seconds on every arrival, in the same direction
    every time.
    """
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0, speed_mph=20.0))
    advance_shadow(shadow, _tel(600, 106.0, 28.5, speed_mph=20.0))
    # Park selected. The speed record for this instant has not arrived, so
    # the composite still reports 20 mph.
    advance_shadow(shadow, _tel(610, 106.0, 28.5, gear="ShiftStateP",
                                speed_mph=20.0, door=True))
    assert shadow["still_since"] == 610, "the stop is when Park was selected"

    trip = advance_shadow(shadow, _tel(900, 106.0, 28.4, gear="ShiftStateP",
                                       speed_mph=0.0))
    assert trip is not None
    assert trip["end_ts"] == 610


def test_a_stale_park_gear_still_cannot_stop_a_trip_from_opening():
    """The mirror of the rule above, and the reason it is ending-only.

    BMSState and Gear are sent on change, so a composite that has not yet
    heard a Gear record reads P. If a P gear could block a trip from
    opening, a car whose stream restarted mid-journey would never open one
    until the next time the lever moved — possibly at its destination.
    """
    shadow: dict = {}
    # No Gear record has ever arrived: the composite defaults to Park.
    advance_shadow(shadow, _tel(0, 100.0, 30.0, gear="ShiftStateP",
                                speed_mph=20.0))
    assert shadow.get("open") is not None, "real motion opens it regardless"


def test_plugging_in_ends_the_trip_without_anyone_getting_out():
    """A car drawing power has arrived, whoever is still sitting in it.

    Parking at a charger and staying in the car is ordinary, and it is the
    case where every other test says nothing: no exit to see, and the BMS may
    hold Drive for another ten minutes. Charging cannot be wrong about it.
    """
    shadow: dict = {}
    advance_shadow(shadow, _bms_tel(0, 100.0, 30.0, "BMSStateDrive"))
    advance_shadow(shadow, _bms_tel(600, 106.0, 28.5, "BMSStateDrive"))
    # Parked at the charger, driver still aboard, drivetrain still live.
    assert advance_shadow(shadow, _bms_tel(660, 106.0, 28.5, "BMSStateDrive",
                                           gear="ShiftStateP", speed_mph=0.0)) is None

    plugged = snapshot_from_telemetry({
        "Odometer": 106.02, "EnergyRemaining": 28.4, "Gear": "ShiftStateP",
        "VehicleSpeed": 0.0, "Soc": 50.0, "BMSState": "BMSStateDrive",
        "DriverSeatOccupied": True,
        "DetailedChargeState": "DetailedChargeStateCharging",
    }, ts=700)
    trip = advance_shadow(shadow, plugged)
    assert trip is not None
    assert trip["ended_on"] == "charging"
    # Still ends where the car stopped, not where it was plugged in.
    assert trip["end_ts"] == 660


def test_a_charge_state_left_over_from_the_last_session_cannot_end_a_trip():
    """The same stale-composite trap the gear and the BMS both set.

    DetailedChargeState is sent on change, so a composite still carrying
    Charging from the session the driver has just unplugged from would close
    the new trip the instant it opened. Being seen unplugged first makes this
    a transition rather than a reading.
    """
    def moving(ts, odo_mi, charging):
        fields = {"Odometer": odo_mi, "EnergyRemaining": 30.0,
                  "Gear": "ShiftStateD", "VehicleSpeed": 20.0, "Soc": 50.0}
        if charging:
            fields["DetailedChargeState"] = "DetailedChargeStateCharging"
        return snapshot_from_telemetry(fields, ts=ts)

    # Never seen unplugged: the composite has read Charging since before the
    # trip opened, and it must not be taken as an arrival.
    stale: dict = {}
    advance_shadow(stale, moving(0, 100.0, True))
    assert advance_shadow(stale, moving(60, 100.5, True)) is None
    assert stale.get("open") is not None

    # Seen unplugged, then charging: a real plug-in, and the trip ends.
    real: dict = {}
    advance_shadow(real, moving(0, 100.0, False))
    advance_shadow(real, moving(600, 106.0, False))
    trip = advance_shadow(real, moving(660, 106.0, True))
    assert trip is not None and trip["ended_on"] == "charging"


def _charge_tel(ts, ac, dc, kwh, soc=60.0, charging=True, kw=7.5):
    fields = {"EnergyRemaining": kwh, "Soc": soc, "Gear": "ShiftStateP",
              "VehicleSpeed": 0.0}
    if charging:
        fields.update({"DetailedChargeState": "DetailedChargeStateCharging",
                       "ACChargingEnergyIn": ac, "DCChargingEnergyIn": dc,
                       "ACChargingPower": kw})
    else:
        fields["DetailedChargeState"] = "DetailedChargeStateDisconnected"
    return snapshot_from_telemetry(fields, ts=ts)


def test_a_charge_records_all_three_meters_and_prefers_none_of_them():
    """The real rates off the AC session of 10 September.

    Wall 7.55 kW, pack 7.2 kW, and the pack's own level rising more slowly
    still. Three figures, one session, and no opinion about which the polled
    history has been storing — that is decided by the car's own Added figure,
    not here.
    """
    shadow: dict = {}
    assert advance_charge(shadow, _charge_tel(0, 4.715, 4.480, 44.44)) is None
    advance_charge(shadow, _charge_tel(120, 5.092, 4.840, 44.74, soc=61.06))
    advance_charge(shadow, _charge_tel(240, 5.218, 4.960, 44.88, soc=61.20, kw=7.6))

    charge = advance_charge(shadow, _charge_tel(300, 0, 0, 44.88, charging=False))
    assert charge is not None
    assert charge["kwh_wall"] == pytest.approx(0.503, abs=0.001)
    assert charge["kwh_pack_meter"] == pytest.approx(0.480, abs=0.001)
    assert charge["kwh_pack_level"] == pytest.approx(0.440, abs=0.001)
    # Both ratios, reported and applied to nothing. The counters against
    # each other, and either of them against the pack's own level — which is
    # where the loss actually turned out to be.
    assert charge["meters_agree_pct"] == pytest.approx(95.4, abs=0.5)
    assert charge["pack_vs_wall_pct"] == pytest.approx(87.5, abs=0.5)
    assert charge["peak_kw"] == pytest.approx(7.6)
    # And where the counters finished, so a session seen only in part can
    # still be matched against what a charging network billed for.
    assert charge["wall_meter_end"] == pytest.approx(5.218, abs=0.001)
    assert charge["pack_meter_end"] == pytest.approx(4.960, abs=0.001)
    assert charge["fast"] is False
    # Ends on the last snapshot that was still charging, not on the unplug.
    assert charge["end_ts"] == 240


def test_a_counter_that_goes_backwards_is_the_next_session_starting():
    """ACChargingEnergyIn is per-session: 16.70 days before this one, 4.72
    during it. So a counter that falls has not misread — a new session has
    begun, and carrying on would report one charge with a negative meter."""
    shadow: dict = {}
    advance_charge(shadow, _charge_tel(0, 4.0, 3.8, 40.0))
    advance_charge(shadow, _charge_tel(60, 5.0, 4.8, 41.0))
    first = advance_charge(shadow, _charge_tel(120, 0.4, 0.38, 41.0))
    assert first is not None
    assert first["kwh_wall"] == pytest.approx(1.0, abs=0.001)
    assert shadow.get("open") is not None, "the new session is already running"

    second = advance_charge(shadow, _charge_tel(240, 1.4, 1.33, 42.0))
    assert second is None
    done = advance_charge(shadow, _charge_tel(300, 0, 0, 42.0, charging=False))
    assert done["kwh_wall"] == pytest.approx(1.0, abs=0.001)


def test_a_car_that_sleeps_mid_charge_does_not_leave_the_session_open():
    """Otherwise it stays open until the next plug-in and reads as one
    enormous charge spanning both."""
    shadow: dict = {}
    advance_charge(shadow, _charge_tel(0, 4.0, 3.8, 40.0))
    advance_charge(shadow, _charge_tel(600, 9.0, 8.6, 44.5))
    assert settle_charge(shadow, 600 + 300) is None, "still talking recently"
    done = settle_charge(shadow, 600 + CHARGE_GAP_SEC + 1)
    assert done is not None
    assert done["end_ts"] == 600, "ends at the last thing the car said"
    assert done["kwh_wall"] == pytest.approx(5.0, abs=0.001)


def test_plugging_in_and_unplugging_again_is_not_a_charge():
    shadow: dict = {}
    advance_charge(shadow, _charge_tel(0, 4.0, 3.8, 40.0))
    advance_charge(shadow, _charge_tel(30, 4.01, 3.81, 40.0))
    assert advance_charge(shadow, _charge_tel(60, 0, 0, 40.0, charging=False)) is None


def test_the_charge_counters_are_carried_without_being_believed():
    """ACChargingEnergyIn read 16.70 on a car with 31,000 km behind it.

    Nobody knows whether that is a session or a lifetime, and a wrong guess
    would misprice every charge. Carried raw so one charging session settles
    it; energy_added_kwh stays at zero until it does.
    """
    # The real values off the AC session of 10 September, including the enum
    # the car actually sends — which is plain ...Charging, not the ...AC
    # variant this test first assumed.
    snap = snapshot_from_telemetry({
        "DetailedChargeState": "DetailedChargeStateCharging",
        "ACChargingEnergyIn": 5.218388966648919,
        "DCChargingEnergyIn": 4.83999989181757,
        "ACChargingPower": 7.500000111758709,
    }, ts=100)
    assert snap["charging"] is True
    assert snap["fast"] is False
    assert snap["charge_energy_in_raw"] == pytest.approx(5.218, abs=0.001)
    assert snap["dc_energy_in_raw"] == pytest.approx(4.840, abs=0.001)
    assert snap["charge_state_raw"] == "DetailedChargeStateCharging"
    assert snap["energy_added_kwh"] == 0.0, "not until one charge has shown what it means"


def test_a_supercharge_is_not_recorded_as_zero_kilowatts():
    """DC sessions report DCChargingPower and leave the AC field empty.

    Reading only the AC one puts a 250 kW session in the history at 0 kW, and
    a charge with no power is a charge whose duration and cost cannot be
    checked against anything.
    """
    dc = snapshot_from_telemetry({
        "DetailedChargeState": "DetailedChargeStateDCCharging",
        "DCChargingPower": 122.0,
    }, ts=100)
    assert dc["charging"] is True
    assert dc["fast"] is True
    assert dc["charger_kw"] == pytest.approx(122.0)

    ac = snapshot_from_telemetry({
        "DetailedChargeState": "DetailedChargeStateCharging",
        "ACChargingPower": 7.5,
    }, ts=100)
    assert ac["charger_kw"] == pytest.approx(7.5), "AC unchanged by the max"
    assert ac["fast"] is False


def test_a_dc_session_is_not_filed_as_ac_because_both_ends_missed_the_state():
    """A 41 kWh Supercharge stop, filed as AC.

    _charge_close used to read "fast" from only the shadow's ``open`` and
    ``last`` snapshots — the record the session started on and whichever
    charging record the machine most recently saw before it closed. A DC
    session can open on the same generic DetailedChargeStateCharging an AC
    one does (see the test above this one), taper into that same generic
    state as it approaches the target SoC, and only report
    DetailedChargeStateDCCharging in the stretch between — so a real session
    that spent its whole middle on DC could still be filed AC if the two
    records it was judged by, start and most-recent, both happened to be the
    generic one.

    "fast" now accumulates across every snapshot the session saw, the same
    way peak_kw already does — read once, at the end, off the shadow itself
    rather than off two records that were never guaranteed to be the ones
    that mattered.
    """
    shadow: dict = {}
    assert advance_charge(shadow, snapshot_from_telemetry({
        "DetailedChargeState": "DetailedChargeStateCharging",
        "EnergyRemaining": 40.0, "Soc": 60.0,
        "Gear": "ShiftStateP", "VehicleSpeed": 0.0,
    }, ts=0)) is None
    assert advance_charge(shadow, snapshot_from_telemetry({
        "DetailedChargeState": "DetailedChargeStateDCCharging",
        "DCChargingPower": 150.0, "DCChargingEnergyIn": 20.0,
        "EnergyRemaining": 60.0, "Soc": 74.0,
        "Gear": "ShiftStateP", "VehicleSpeed": 0.0,
    }, ts=300)) is None
    # Tapering off, back on the generic state — and now the record the
    # machine holds as "last" going into the close, exactly as a real
    # session's power curve rolls off approaching the target SoC.
    assert advance_charge(shadow, snapshot_from_telemetry({
        "DetailedChargeState": "DetailedChargeStateCharging",
        "DCChargingPower": 5.0, "DCChargingEnergyIn": 40.5,
        "EnergyRemaining": 80.5, "Soc": 79.5,
        "Gear": "ShiftStateP", "VehicleSpeed": 0.0,
    }, ts=550)) is None
    done = advance_charge(shadow, snapshot_from_telemetry({
        "DetailedChargeState": "DetailedChargeStateDisconnected",
        "DCChargingEnergyIn": 41.0,
        "EnergyRemaining": 81.0, "Soc": 80.0,
        "Gear": "ShiftStateP", "VehicleSpeed": 0.0,
    }, ts=600))
    assert done is not None
    assert done["fast"] is True, "the middle of the session was DC"


def test_a_bms_never_seen_in_drive_cannot_end_a_trip():
    """The composite holds the last value sent, and BMSState is sent on change.

    A trip that opens while the composite still reads Support from hours ago
    would be closed by that stale value at the first red light — the same
    trap as the stale gear that once invented a journey out of a car
    reversing into a bay.
    """
    shadow: dict = {}
    advance_shadow(shadow, _bms_tel(0, 100.0, 30.0, "BMSStateSupport"))
    out = advance_shadow(shadow, _bms_tel(60, 100.1, 29.9, "BMSStateSupport",
                                          gear="ShiftStateD", speed_mph=0.0))
    assert out is None
    assert shadow.get("open") is not None


def _idle_tel(ts, odo_mi, kmh, climate=False):
    return snapshot_from_telemetry({
        "Odometer": odo_mi, "EnergyRemaining": 30.0, "Gear": "ShiftStateD",
        "VehicleSpeed": kmh / MILES_TO_KM, "Soc": 50.0,
        "HvacPower": "HvacPowerStateOn" if climate else "HvacPowerStateOff",
    }, ts=ts)


def test_a_streamed_trip_measures_its_own_idle_instead_of_guessing_it():
    """idle_tracked was false on every streamed trip, so the dashboard marked
    the most accurate journeys it has recorded as estimates — correctly, since
    nothing had measured a stop.

    Telemetry is the better instrument for it. VehicleSpeed arrives every ten
    seconds; a poll sees the car once a minute at best. A stop counts only
    once it has lasted IDLE_STREAK_MIN, which is what separates a run of
    traffic lights from actually waiting somewhere.
    """
    shadow: dict = {}
    t, odo = 0.0, 100.0
    advance_shadow(shadow, _idle_tel(t, odo, 40.0, climate=True))
    # Two minutes moving.
    for _ in range(12):
        t += 10; odo += 0.07
        advance_shadow(shadow, _idle_tel(t, odo, 40.0, climate=True))
    # Ninety seconds at a light: too short to be idling.
    for _ in range(9):
        t += 10
        advance_shadow(shadow, _idle_tel(t, odo, 0.0, climate=True))
    # Moving again.
    for _ in range(6):
        t += 10; odo += 0.07
        advance_shadow(shadow, _idle_tel(t, odo, 40.0, climate=True))
    # Eight minutes genuinely waiting, climate off for the last half.
    for i in range(48):
        t += 10
        advance_shadow(shadow, _idle_tel(t, odo, 0.0, climate=i < 24))
    # Off again, which is what banks the wait.
    for _ in range(6):
        t += 10; odo += 0.07
        advance_shadow(shadow, _idle_tel(t, odo, 40.0))
    t += 10
    advance_shadow(shadow, snapshot_from_telemetry({
        "Odometer": odo, "EnergyRemaining": 28.8, "Gear": "ShiftStateP",
        "VehicleSpeed": 0.0, "Soc": 48.0,
        "DoorState": {"DriverFront": True}}, ts=t))
    trip = advance_shadow(shadow, snapshot_from_telemetry({
        "Odometer": odo, "EnergyRemaining": 28.8, "Gear": "ShiftStateP",
        "VehicleSpeed": 0.0, "Soc": 48.0}, ts=t + 240))

    assert trip is not None
    assert trip["idle_tracked"] is True
    # The eight-minute wait, not the ninety-second light.
    assert trip["idle_min"] == pytest.approx(8.0, abs=0.2)
    # Climate ran from the start until halfway through that wait: two
    # minutes moving, ninety seconds at the light, a minute moving again and
    # four minutes of the wait, which is 8.7 and not the 8.0 the shape of the
    # test suggests at a glance.
    assert trip["climate_min"] == pytest.approx(8.7, abs=0.2)


def test_a_blackout_is_not_counted_as_time_spent_idling():
    """A car that went quiet for an hour was not waiting for an hour.

    The gap is unmeasured, and putting it into the one figure this exists to
    measure would be worse than the estimate it replaces.
    """
    shadow: dict = {}
    advance_shadow(shadow, _idle_tel(0, 100.0, 40.0))
    advance_shadow(shadow, _idle_tel(10, 100.1, 40.0))
    # Stops, then says nothing for twenty minutes, then drives on.
    advance_shadow(shadow, _idle_tel(20, 100.1, 0.0))
    advance_shadow(shadow, _idle_tel(20 + 1200, 100.1, 40.0))
    for i in range(1, 7):
        advance_shadow(shadow, _idle_tel(20 + 1200 + i * 10, 100.1 + i * 0.07, 40.0))
    trip = advance_shadow(shadow, snapshot_from_telemetry({
        "Odometer": 100.6, "EnergyRemaining": 28.8, "Gear": "ShiftStateP",
        "VehicleSpeed": 0.0, "Soc": 48.0,
        "DoorState": {"DriverFront": True}}, ts=20 + 1200 + 300))
    trip = trip or advance_shadow(shadow, snapshot_from_telemetry({
        "Odometer": 100.6, "EnergyRemaining": 28.8, "Gear": "ShiftStateP",
        "VehicleSpeed": 0.0, "Soc": 48.0}, ts=20 + 1200 + 600))
    assert trip is not None
    assert trip["idle_min"] == 0.0, "the silence was counted as waiting"


def test_a_trip_is_not_charged_for_standing_still_after_it_ended():
    """Time and energy came from different snapshots, and it showed.

    A trip closed on a settle window takes its odometer from a later reading,
    because a car that has stopped may still roll a few metres and the
    30-second Odometer interval leaves the closing reading stale. Energy was
    being taken from that same later reading — and a car that has stopped is
    still drawing: screen, climate, staying awake. Three minutes of it charged
    to a journey that had already ended.

    Which made every trip internally contradictory, its duration ending when
    the car stopped while its energy went on accruing. Seven trips judged
    against the car's own figures, all seven over.
    """
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0))
    advance_shadow(shadow, _tel(600, 106.0, 28.5))
    # Stops here, driver gets out. 28.5 kWh remaining at that moment.
    advance_shadow(shadow, _tel(660, 106.0, 28.5, gear="ShiftStateP",
                                speed_mph=0.0, door=True))
    # Three minutes later the car is still awake and has spent 0.1 kWh doing
    # nothing, while its odometer reading has caught up by 20 metres.
    trip = advance_shadow(shadow, _tel(900, 106.0125, 28.4, gear="ShiftStateP",
                                       speed_mph=0.0))
    assert trip is not None
    # The later odometer is used: it measures the arrival better.
    assert trip["end_odo_km"] == pytest.approx(106.0125 * MILES_TO_KM, abs=0.01)
    # The energy is not: 1.5 kWh used driving, not 1.6.
    assert trip["energy_kwh"] == pytest.approx(1.5, abs=0.001)
    assert trip["end_energy_kwh"] == pytest.approx(28.5, abs=0.001)


def test_a_trip_records_the_temperature_it_was_driven_in_not_the_one_it_ended_in():
    """The climate model integrates a rate over the whole drive. Its input was
    one sample, taken at the instant the drive finished.

    That model costs 0.08 kW per degree, so a single degree of sampling error
    is worth about half the disagreement it is currently being judged on — an
    afternoon run that ends in an underground car park and an evening one that
    ends in the open are not sampling the same thing. OutsideTemp streams
    every five minutes throughout, so the mean is there for the taking.
    """
    shadow: dict = {}
    energy, odo, ts = 30.0, 100.0, 0.0

    # Thirty-six minutes of driving that cools from 36 to 30 as the sun goes
    # down: mean 33, final reading 30.
    for i in range(36):
        temp = 36.0 - i * (6.0 / 35.0)
        snap = _tel(ts, odo, round(energy, 3))
        snap["out_temp"] = round(temp, 2)
        advance_shadow(shadow, snap)
        ts += 60.0; odo += 0.5; energy -= 0.03
    stop = _tel(ts, odo, round(energy, 3), gear="ShiftStateP",
                speed_mph=0.0, door=True)
    stop["out_temp"] = 30.0
    advance_shadow(shadow, stop)
    final = _tel(ts + SHADOW_SETTLE_SEC + 10, odo, round(energy, 3),
                 gear="ShiftStateP", speed_mph=0.0)
    final["out_temp"] = 30.0
    trip = advance_shadow(shadow, final)

    assert trip is not None
    assert trip["out_temp"] == pytest.approx(33.0, abs=0.3), \
        f"recorded {trip['out_temp']} for a drive that averaged 33"
    # The closing reading is kept, because "what was it like when I arrived"
    # is a different question from "what should the model integrate".
    assert trip["out_temp_end"] == pytest.approx(30.0)


def test_a_departure_head_is_not_paid_to_the_arriving_trip():
    """Trip 740, 13 September, and the reason this guard exists.

    740 closed at 31405.714 with its stream lost. A continuity scan ten
    minutes later confirmed the car resting at exactly that odometer. Three
    hours later 741 began at 31405.922 — and a parked car's odometer cannot
    creep, so those 208 metres were driven at 741's DEPARTURE, before its
    first record arrived.

    recover_sleep_gap gave all of them to 740 anyway, because it read the next
    trip's starting odometer as evidence of where the previous one stopped.
    Those are different things, and the difference is exactly the ground the
    departure covered unseen. 740 went from 0.55% short of the car's own
    figure to 1.2% long.
    """
    prev = {"start_odo_km": 31393.680, "end_odo_km": 31405.714,
            "distance_km": 12.034, "energy_kwh": 1.640, "wh_per_km": 136.3,
            "ended_on": "stream_lost"}
    nxt = {"start_odo_km": 31405.922}

    # Seen resting at its own recorded stop: there is nothing to pay back.
    assert recover_sleep_gap(dict(prev), nxt, rested_odo_km=31405.714) is False

    # Seen resting PART of the way along: pay back only as far as the evidence
    # goes, never all the way to where the next trip was first noticed.
    half = dict(prev)
    assert recover_sleep_gap(half, nxt, rested_odo_km=31405.800) is True
    assert half["end_odo_km"] == pytest.approx(31405.800)
    assert half["distance_km"] == pytest.approx(12.120, abs=0.001)

    # No reading at all — a car that parks underground and sleeps — keeps the
    # behaviour it always had. Without evidence, assuming the gap was the
    # arrival still beats losing it.
    blind = dict(prev)
    assert recover_sleep_gap(blind, nxt) is True
    assert blind["end_odo_km"] == pytest.approx(31405.922)


def test_the_temperature_average_stops_when_the_car_does():
    """A drive spent entirely at 40 C records 40, however cool where it parks.

    The mean is accumulated as records arrive, but records keep arriving
    through the settle window after the car has stopped — so a plain
    accumulator integrates past the end of the journey, at whatever the
    temperature is where the car came to rest. That is the endpoint-sampling
    bias this averaging exists to remove, reintroduced inside a mean that
    looks principled, which is worse than the original for being harder to
    see.

    Measured before the fix: 36.5 for this drive, the accumulator having run
    to 1090 seconds against a 900-second trip. 3.5 C is 0.28 kW — larger than
    the whole disagreement the climate model is being judged on.

    The ramp test above could not catch it: on a smooth ramp the arrival
    temperature is close to the last driving one, so the contamination hides
    inside the rounding.
    """
    shadow: dict = {}
    energy, odo, ts = 30.0, 100.0, 0.0
    for _ in range(3):
        snap = _tel(ts, odo, round(energy, 3))
        snap["out_temp"] = 40.0
        advance_shadow(shadow, snap)
        ts += 300.0; odo += 3.0; energy -= 0.5

    # Parked somewhere twenty degrees cooler — underground, in shade, at night.
    stop = _tel(ts, odo, round(energy, 3), gear="ShiftStateP",
                speed_mph=0.0, door=True)
    stop["out_temp"] = 20.0
    advance_shadow(shadow, stop)
    final = _tel(ts + SHADOW_SETTLE_SEC + 10, odo, round(energy, 3),
                 gear="ShiftStateP", speed_mph=0.0)
    final["out_temp"] = 20.0
    trip = advance_shadow(shadow, final)

    assert trip is not None
    assert trip["out_temp"] == pytest.approx(40.0, abs=0.1), \
        f"recorded {trip['out_temp']} for a drive spent entirely at 40 C"
    # The closing reading is still kept, and still says where it ended up.
    assert trip["out_temp_end"] == pytest.approx(20.0)


def test_a_stop_during_the_journey_still_counts_toward_its_temperature():
    """Only the LAST stationary stretch is the arrival. A car sitting in
    traffic is under the same climate load and still on its journey, so
    dropping every stop would trade one bias for another — and a queue in the
    sun is exactly when the climate load is highest.
    """
    shadow: dict = {}
    energy, odo, ts = 30.0, 100.0, 0.0

    def moving(temp):
        nonlocal ts, odo, energy
        snap = _tel(ts, odo, round(energy, 3))
        snap["out_temp"] = temp
        advance_shadow(shadow, snap)
        ts += 300.0; odo += 3.0; energy -= 0.5

    def halted(temp):
        nonlocal ts, energy
        snap = _tel(ts, odo, round(energy, 3), speed_mph=0.0)
        snap["out_temp"] = temp
        advance_shadow(shadow, snap)
        ts += 300.0; energy -= 0.05

    moving(40.0)          # 300 s at 40 while under way
    halted(40.0)          # 300 s at 40, stopped in traffic — must count
    moving(40.0)          # moving again: the held stretch commits
    moving(40.0)

    stop = _tel(ts, odo, round(energy, 3), gear="ShiftStateP",
                speed_mph=0.0, door=True)
    stop["out_temp"] = 20.0
    advance_shadow(shadow, stop)
    final = _tel(ts + SHADOW_SETTLE_SEC + 10, odo, round(energy, 3),
                 gear="ShiftStateP", speed_mph=0.0)
    final["out_temp"] = 20.0
    trip = advance_shadow(shadow, final)

    assert trip is not None
    assert trip["out_temp"] == pytest.approx(40.0, abs=0.1), \
        f"recorded {trip['out_temp']} — the traffic stop was dropped with the arrival"


def test_a_trips_cadence_does_not_start_at_the_previous_trips_last_reading():
    """The tally is per trip. Its clock has to be too.

    A trip resets the gap histogram when it opens, but the anchor it measures
    from lived outside it — so the first gap of a journey was measured from
    the last EnergyRemaining change of the PREVIOUS one, across the park. Any
    park under the blackout threshold passes the guard, which is every park
    shorter than ten minutes.

    The median absorbs one outlier among many, so this shows where it cannot:
    a trip with only two gaps of its own has too few to report a cadence at
    all and must say None. Counting the park as a third manufactures a
    measurement out of the time the car spent standing still.
    """
    shadow: dict = {}
    energy, odo, ts = 30.0, 100.0, 0.0

    # A first trip, to leave an anchor behind.
    for _ in range(12):
        advance_shadow(shadow, _tel(ts, odo, round(energy, 3)))
        ts += 10.0; odo += 0.05; energy -= 0.005
    advance_shadow(shadow, _tel(ts, odo, round(energy, 3), gear="ShiftStateP",
                                speed_mph=0.0, door=True))
    assert advance_shadow(shadow, _tel(ts + SHADOW_SETTLE_SEC + 10, odo,
                                       round(energy, 3), gear="ShiftStateP",
                                       speed_mph=0.0)) is not None

    # Five minutes parked — short enough that nothing treats it as a
    # blackout — then a real journey whose ENERGY only moves three times, so
    # it owns two gaps: one short of the three this will report on. The first
    # move comes AFTER the trip opens, which is the shape that leaks: while
    # the opening record is itself a change the anchor is reset in passing and
    # nothing is counted.
    ts += 300.0
    for i in range(15):
        if i in (1, 6, 11):
            energy -= 0.02
        advance_shadow(shadow, _tel(ts, odo, round(energy, 3)))
        ts += 10.0; odo += 0.2
    advance_shadow(shadow, _tel(ts, odo, round(energy, 3), gear="ShiftStateP",
                                speed_mph=0.0, door=True))
    second = advance_shadow(shadow, _tel(ts + SHADOW_SETTLE_SEC + 10, odo,
                                         round(energy, 3), gear="ShiftStateP",
                                         speed_mph=0.0))
    assert second is not None
    assert second["energy_sample_sec"] is None, \
        "the park was counted as one of this trip's own intervals"


def test_a_wait_mid_trip_is_not_a_slow_sampling_interval():
    """The median, not the mean.

    A car waiting at a pickup with the drive still open sends speed and gear
    the whole time, but EnergyRemaining barely moves — it is sent when it
    CHANGES. So one gap between its values spans the whole wait, and the car
    was not sampling once every five minutes: it had nothing to say.

    Twenty-three gaps here, twenty-two of them ten seconds and one of three
    hundred. The median is ten and the mean is 22.6, so this fails if the
    figure is ever averaged.
    """
    shadow: dict = {}
    energy, odo, ts = 30.0, 100.0, 0.0
    for _ in range(12):                      # driving, ten seconds apart
        advance_shadow(shadow, _tel(ts, odo, round(energy, 3)))
        ts += 10.0
        odo += 0.05
        energy -= 0.005
    for _ in range(30):                      # five minutes stopped, energy still
        advance_shadow(shadow, _tel(ts, odo, round(energy, 3), speed_mph=0.0))
        ts += 10.0
    for _ in range(12):                      # and away again
        energy -= 0.005
        advance_shadow(shadow, _tel(ts, odo, round(energy, 3)))
        ts += 10.0
        odo += 0.05
    advance_shadow(shadow, _tel(ts, odo, round(energy, 3), gear="ShiftStateP",
                                speed_mph=0.0, door=True))
    trip = advance_shadow(shadow, _tel(ts + SHADOW_SETTLE_SEC + 60, odo,
                                       round(energy, 3), gear="ShiftStateP",
                                       speed_mph=0.0))
    assert trip is not None
    # One trip, not two: records kept arriving throughout, so nothing here
    # was a blackout and the wait belongs to the journey.
    assert trip["duration_min"] == pytest.approx(9.0, abs=0.2)
    assert trip["energy_sample_sec"] == pytest.approx(10.0)


def test_too_few_readings_says_nothing_rather_than_guessing():
    """Two readings make one gap, and one gap is an anecdote.

    A single record delayed by a reconnect would otherwise set the figure for
    the whole trip. None here means the reader falls back to the interval
    this car streamed at before any of it was measured, which is the wide one
    — overstating an error bar is the smaller sin.
    """
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0))
    advance_shadow(shadow, _tel(60, 106.0, 28.5))
    advance_shadow(shadow, _tel(120, 106.0, 28.5, gear="ShiftStateP",
                                speed_mph=0.0, door=True))
    trip = advance_shadow(shadow, _tel(400, 106.0, 28.5, gear="ShiftStateP",
                                       speed_mph=0.0))
    assert trip is not None
    assert trip["energy_sample_sec"] is None


def test_the_odometer_gives_back_what_a_sleeping_car_never_sent():
    """The real trip 535, and the 338 metres it drove after its last record.

    Measured: it ended stream_lost at 31,161.861 and the next trip opened at
    31,162.199. The car parked underground, lost signal, and slept before it
    could flush — so unlike a blackout it drove through, that arrival was
    never transmitted at all. Against the car's own 4.1 km this turns -7.4%
    into +0.8%.
    """
    prev = {"start_odo_km": 31158.066, "end_odo_km": 31161.861,
            "distance_km": 3.795, "energy_kwh": 0.88, "wh_per_km": 231.9,
            "duration_min": 17.2, "ended_on": "stream_lost"}
    assert recover_sleep_gap(prev, {"start_odo_km": 31162.199}) is True
    assert prev["distance_km"] == 4.133
    assert prev["recovered_km"] == 0.338
    # Energy follows the distance at the trip's own efficiency, so the three
    # figures stay consistent with each other.
    assert prev["recovered_kwh"] == 0.078
    assert prev["wh_per_km"] == pytest.approx(231.9, abs=0.2)
    # Duration is untouched: the odometer counts up, the clock does not.
    assert prev["duration_min"] == 17.2
    # And so is the average speed, which is why. 4.133 km over the 17.2
    # minutes that only covered 3.795 of them reads 14.4 km/h, and the car
    # was slower than the 13.2 it was measured at, not faster — the figure
    # from before the correction is short in both halves and so unbiased.
    assert prev.get("avg_speed_kmh") is None, "not recomputed against a short clock"

    # Recomputed from the bracket, so running it again finds nothing left.
    assert recover_sleep_gap(prev, {"start_odo_km": 31162.199}) is False
    assert prev["distance_km"] == 4.133


def test_a_whole_journey_driven_offline_is_not_glued_onto_the_last_arrival():
    """The cap is the difference between recovering an arrival and inventing one.

    An arrival roll is a few hundred metres. A journey is kilometres. Above
    the cap this refuses and the distance stays in unaccounted_km, where it
    can be seen rather than silently attributed to a trip that did not drive
    it.
    """
    def trip():
        return {"start_odo_km": 31158.066, "end_odo_km": 31161.861,
                "distance_km": 3.795, "energy_kwh": 0.88, "wh_per_km": 231.9,
                "duration_min": 17.2, "ended_on": "stream_lost"}

    over = trip()
    assert recover_sleep_gap(over, {"start_odo_km": 31173.2}) is False
    assert over["distance_km"] == 3.795

    # The cap sits just above what entering a carpark without signal actually
    # costs — 200 to 400 metres, on the driver's account and on every gap
    # recorded here. A roll inside that is paid back; one well beyond it is
    # left in unaccounted_km, where it is visible rather than silently
    # attributed to a trip that did not drive it.
    normal = trip()
    assert recover_sleep_gap(normal, {"start_odo_km": 31161.861 + 0.4}) is True
    assert normal["recovered_km"] == pytest.approx(0.4, abs=0.002)
    beyond = trip()
    assert recover_sleep_gap(beyond, {"start_odo_km": 31161.861 + 0.7}) is False

    # And only for an ending nobody confirmed. A trip the car said goodbye to
    # was measured; a gap after it belongs to whatever happened next.
    for ending in ("bms", "exit", "timeout"):
        clean = dict(trip(), ended_on=ending)
        assert recover_sleep_gap(clean, {"start_odo_km": 31162.199}) is False
        assert clean["distance_km"] == 3.795


def test_shadow_ignores_records_replayed_from_the_car_s_buffer():
    """A car out of coverage buffers and resends; those arrive out of order.

    Taken at face value they read as the odometer running backwards. Distance
    survives regardless because the odometer is cumulative — it is the trip
    boundaries that would be corrupted.
    """
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0))
    advance_shadow(shadow, _tel(600, 106.0, 28.5))
    # A replay of the tunnel, arriving after the records that followed it.
    assert advance_shadow(shadow, _tel(300, 103.0, 29.2)) is None
    assert shadow["out_of_order"] == 1
    assert shadow["last"]["odo_km"] > 106 * 1.6      # still the newer reading

    trip = advance_shadow(shadow, _tel(900, 106.0, 28.5, gear="ShiftStateP",
                                       speed_mph=0.0))
    assert trip is None or trip["end_ts"] != 300


def test_a_park_with_nobody_getting_out_is_not_an_arrival():
    """Parking without opening a door is a pause, not the end of a journey.

    Five minutes in P at a queue or on a phone call would otherwise split one
    journey into two, inventing a trip that never happened. Costs nothing when
    it guesses wrong, because the end is backdated to the moment P was reached
    either way.
    """
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0))
    advance_shadow(shadow, _tel(300, 105.0, 28.8))
    # In P for five minutes, doors never opened.
    assert advance_shadow(shadow, _tel(360, 105.0, 28.8, gear="ShiftStateP",
                                       speed_mph=0.0)) is None
    assert advance_shadow(shadow, _tel(660, 105.0, 28.8, gear="ShiftStateP",
                                       speed_mph=0.0)) is None
    # Then drives on: still one trip.
    assert advance_shadow(shadow, _tel(700, 105.5, 28.6)) is None
    assert shadow.get("open") is not None
    assert shadow["open"]["ts"] == 0

    # Whereas a door opening settles it as an arrival on the short window.
    shadow2: dict = {}
    advance_shadow(shadow2, _tel(0, 100.0, 30.0))
    advance_shadow(shadow2, _tel(300, 105.0, 28.8))
    advance_shadow(shadow2, _tel(360, 105.0, 28.8, gear="ShiftStateP",
                                 speed_mph=0.0, door=True))
    trip = advance_shadow(shadow2, _tel(600, 105.0, 28.8, gear="ShiftStateP",
                                        speed_mph=0.0))
    assert trip is not None
    assert trip["end_ts"] == 360          # when it parked, not when we decided


def test_shadow_trip_reports_how_it_ended_and_carries_both_energy_measures():
    """Both energy measures travel together, and neither is trusted yet.

    LifetimeEnergyUsedDrive is the better measure — monotonic, traction only —
    but its units are undocumented. Carrying it beside the EnergyRemaining
    delta is what lets one real journey settle the ratio instead of a guess
    settling it silently.
    """
    def snap(ts, odo, energy, drive, regen, gear="ShiftStateD",
             speed=20.0, seat=True):
        return snapshot_from_telemetry({
            "Odometer": odo, "EnergyRemaining": energy, "Gear": gear,
            "VehicleSpeed": speed, "Soc": 50.0,
            "LifetimeEnergyUsedDrive": drive,
            "LifetimeEnergyGainedRegen": regen,
            "DriverSeatOccupied": seat, "ModuleTempMin": 28.5,
        }, ts=ts)

    shadow: dict = {}
    advance_shadow(shadow, snap(0, 100.0, 30.0, 4000.0, 900.0))
    advance_shadow(shadow, snap(600, 106.0, 28.5, 4001.5, 900.4))
    # Driver gets out — occupancy says so directly, no door event needed.
    advance_shadow(shadow, snap(660, 106.0, 28.5, 4001.5, 900.4,
                                gear="ShiftStateP", speed=0.0, seat=False))
    trip = advance_shadow(shadow, snap(900, 106.0, 28.5, 4001.5, 900.4,
                                       gear="ShiftStateP", speed=0.0, seat=False))
    assert trip is not None
    assert trip["ended_on"] == "exit"
    assert trip["energy_kwh"] == 1.5      # pack fell by this
    assert trip["drive_delta"] == 1.5     # and traction accounts for all of it
    assert trip["regen_delta"] == 0.4
    assert trip["pack_temp_c"] == 28.5


def test_arrival_readings_come_from_after_the_car_stopped():
    """The odometer that arrives after parking measures the arrival better.

    Odometer streams on a 30-second minimum, so the reading held at the
    instant P is reached can be half a minute stale — a quarter of a
    kilometre at city speed, lost off the end of every trip. The car has not
    moved since, so a later reading is the same moment, measured properly.
    """
    shadow: dict = {}
    advance_shadow(shadow, _tel(0, 100.0, 30.0))
    # Parks. The odometer here is stale — it last reported 25 seconds ago.
    advance_shadow(shadow, _tel(300, 105.0, 28.8, gear="ShiftStateP",
                                speed_mph=0.0, door=True))
    # A fresher reading lands while it sits there.
    advance_shadow(shadow, _tel(320, 105.2, 28.75, gear="ShiftStateP",
                                speed_mph=0.0))
    trip = advance_shadow(shadow, _tel(500, 105.2, 28.75, gear="ShiftStateP",
                                       speed_mph=0.0))
    assert trip is not None
    assert trip["end_ts"] == 300                      # when it stopped
    assert round(trip["distance_km"], 1) == 8.4       # 5.2 miles, not 5.0


def test_telemetry_snapshot_reads_cabin_overheat_and_climate_keeper():
    """Both are real fields in Tesla's proto (180 and 186), both are large
    parked draws, and neither was configured until now.

    The enum tail is taken as the value rather than a mapping being invented
    for it — CabinOverheatProtectionModeStateFanOnly is "FanOnly", which is
    the word the polled column already holds. That is the difference between
    this and CenterDisplay, whose enum has no documented correspondence to
    the integers polling stores and so is still not mapped.
    """
    from app.sync import snapshot_from_telemetry

    def snap(**fields):
        base = {"Soc": 70.0, "RatedRange": 250.0, "Odometer": 19337.0}
        return snapshot_from_telemetry({**base, **fields}, 1_789_000_000.0)

    s = snap(CabinOverheatProtectionMode="CabinOverheatProtectionModeStateFanOnly",
             HvacPower="HvacPowerStateOverheatProtect",
             ClimateKeeperMode="ClimateKeeperModeStateDog")
    assert s["cabin_overheat_protection"] == "FanOnly"
    assert s["cabin_overheat_protection_actively_cooling"] is True
    assert s["climate_keeper"] == "Dog"

    # Enabled is not the same as cooling: the mode says it is allowed to run,
    # HvacPower says whether it is running for that reason right now.
    s = snap(CabinOverheatProtectionMode="CabinOverheatProtectionModeStateOn",
             HvacPower="HvacPowerStateOff")
    assert s["cabin_overheat_protection"] == "On"
    assert s["cabin_overheat_protection_actively_cooling"] is False

    # "Unknown" is the car declining to answer, which must stay None rather
    # than becoming a confident value.
    s = snap(CabinOverheatProtectionMode="CabinOverheatProtectionModeStateUnknown")
    assert s["cabin_overheat_protection"] is None
    assert s["climate_keeper"] is None

    # CenterDisplay is kept as the car's own word for the state. The integer
    # column that used to sit beside it is gone: it held Tesla's POLLED code
    # on an undocumented scale, so no streamed value could become one without
    # a mapping being invented for it.
    assert "center_display_state" not in s


def test_shadow_charge_records_the_lifetime_counter_across_a_session():
    """LifetimeEnergyChargedKwh is the only charge counter that cannot reset
    under a session.

    ACChargingEnergyIn is per-session — measured at 16.70 days before the 10
    September charge and 4.72 partway through it — so a session read from it
    depends on having watched it from zero, and the first session this app
    ever recorded was joined halfway through. A lifetime total gives the same
    kWh as the difference of its ends whenever those ends are seen.
    """
    from app.sync import advance_charge

    def snap(ts, charging, ac_in, dc_in, lifetime, energy):
        return {"ts": ts, "charging": charging, "charger_kw": 7.5,
                "charge_energy_in_raw": ac_in, "dc_energy_in_raw": dc_in,
                "lifetime_charged_raw": lifetime, "energy_kwh": energy,
                "soc": 60.0, "fast": False, "shift": "P", "speed_kmh": 0.0}

    shadow: dict = {}
    # Joined mid-session on purpose: the per-session counters are already
    # partway up, and only the lifetime one is unaffected by that.
    assert advance_charge(shadow, snap(1000.0, True, 4.72, 4.48, 3120.5, 40.0)) is None
    assert advance_charge(shadow, snap(1300.0, True, 8.72, 8.46, 3124.5, 43.6)) is None
    done = advance_charge(shadow, snap(1600.0, False, 8.72, 8.46, 3124.5, 43.6))

    assert done is not None
    assert done["kwh_wall"] == pytest.approx(4.0)
    assert done["kwh_pack_meter"] == pytest.approx(3.98)
    assert done["kwh_lifetime"] == pytest.approx(4.0)
    # The final reading, not only the movement. Two sessions' readings of a
    # lifetime total are directly comparable, and the difference between them
    # is every kWh the pack took in between — including a charge the app
    # missed entirely, which is how it would learn that it had.
    assert done["lifetime_meter_end"] == pytest.approx(3124.5)


def test_shadow_charge_still_closes_when_only_the_lifetime_counter_moves():
    """A session where the per-session counters never arrive is still a
    session. The size check has to weigh every counter, not only the two it
    was written with."""
    from app.sync import advance_charge

    def snap(ts, charging, lifetime):
        return {"ts": ts, "charging": charging, "charger_kw": 7.0,
                "charge_energy_in_raw": None, "dc_energy_in_raw": None,
                "lifetime_charged_raw": lifetime, "energy_kwh": None,
                "soc": 60.0, "fast": False, "shift": "P", "speed_kmh": 0.0}

    shadow: dict = {}
    advance_charge(shadow, snap(1000.0, True, 3120.5))
    advance_charge(shadow, snap(1300.0, True, 3124.5))
    done = advance_charge(shadow, snap(1600.0, False, 3124.5))
    assert done is not None
    assert done["kwh_lifetime"] == pytest.approx(4.0)
    assert done["kwh_wall"] is None


def test_the_shadow_bands_distance_time_and_energy_by_speed():
    """A trip carrying two speed numbers can be put in one box and cannot be
    described as partly one thing and partly another. The stream sees a record
    every ten seconds, so where each kilometre happened is measurable.

    Energy is banded too, from the car's own EnergyRemaining over the same
    interval — not apportioned from the trip total by distance, which would
    hand highway and city the same Wh/km and destroy the one distinction the
    condition matrix exists to draw.
    """
    from app import sync as sync_mod

    shadow: dict = {}
    base = 1_789_000_000.0
    odo, energy = 1000.0, 60.0
    # Six minutes of motorway at ~144 km/h, then six of town at ~30, with the
    # car stopping twice. Records every 30 s, as a quiet stretch streams.
    plan = [(144.0, 12), (30.0, 12)]
    stop_at = {14, 20}
    ts = base
    for kmh, steps in plan:
        for i in range(steps):
            moving = (len(shadow.get("bands") or {}) >= 0)
            idx = int((ts - base) / 30.0)
            speed = 0.0 if idx in stop_at else kmh
            step_km = speed * (30.0 / 3600.0)
            odo += step_km
            energy -= step_km * (0.21 if kmh > 100 else 0.14)
            snap = {"ts": ts, "shift": "D" if speed else "P",
                    "speed_kmh": speed, "odo_km": odo, "energy_kwh": energy,
                    "soc": 80.0, "range_km": 300.0, "out_temp": 30.0,
                    "climate_on": False, "sentry_mode": False}
            sync_mod.advance_shadow(shadow, snap)
            ts += 30.0

    bands = shadow.get("bands") or {}
    assert bands, "nothing was banded"
    fast = sum(v[0] for k, v in bands.items() if float(k) >= 130.0)
    town = sum(v[0] for k, v in bands.items() if float(k) < 90.0)
    assert fast > 8.0, f"motorway distance not banded high: {bands}"
    assert town > 1.0, f"town distance not banded low: {bands}"

    # The fast band cost materially more per kilometre, which it could not have
    # done had energy been split by distance.
    fast_kwh = sum(v[2] for k, v in bands.items() if float(k) >= 130.0)
    town_kwh = sum(v[2] for k, v in bands.items() if float(k) < 90.0 and v[0] > 0)
    assert fast_kwh / fast > (town_kwh / town) * 1.3

    # And the stops were counted — the thing idle_min cannot see, since none of
    # these lasted the five minutes it requires.
    assert shadow.get("stops") == len(stop_at)
