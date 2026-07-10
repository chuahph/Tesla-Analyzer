"""Tests for time-of-use electricity pricing (app/tariff.py)."""
from datetime import datetime

from app.tariff import price_at


def test_flat_rate_when_tou_not_configured():
    weekday_peak = datetime(2026, 7, 6, 14, 0)   # Monday 2pm
    weekday_night = datetime(2026, 7, 6, 2, 0)   # Monday 2am
    assert price_at(weekday_peak, flat=0.90) == 0.90
    assert price_at(weekday_night, flat=0.90) == 0.90
    # Only one of peak/off_peak set -> still flat (both required to enable TOU).
    assert price_at(weekday_peak, flat=0.90, peak=1.20) == 0.90


def test_tou_peak_and_offpeak_by_hour():
    monday_2pm = datetime(2026, 7, 6, 14, 0)     # inside 8-22 peak window
    monday_11pm = datetime(2026, 7, 6, 23, 0)    # outside peak window
    monday_7am = datetime(2026, 7, 6, 7, 0)      # before peak starts
    assert price_at(monday_2pm, flat=0.90, peak=1.20, off_peak=0.45,
                    peak_start_hour=8, peak_end_hour=22) == 1.20
    assert price_at(monday_11pm, flat=0.90, peak=1.20, off_peak=0.45,
                    peak_start_hour=8, peak_end_hour=22) == 0.45
    assert price_at(monday_7am, flat=0.90, peak=1.20, off_peak=0.45,
                    peak_start_hour=8, peak_end_hour=22) == 0.45


def test_weekend_treated_as_offpeak():
    saturday_2pm = datetime(2026, 7, 4, 14, 0)   # Sat, inside the "peak" hour window
    assert price_at(saturday_2pm, flat=0.90, peak=1.20, off_peak=0.45,
                    weekend_off_peak=True) == 0.45
    # With weekend_off_peak disabled, weekend hours follow the normal split.
    assert price_at(saturday_2pm, flat=0.90, peak=1.20, off_peak=0.45,
                    weekend_off_peak=False) == 1.20


def test_price_fn_from_settings():
    from types import SimpleNamespace

    from app.tariff import price_fn_from_settings

    settings = SimpleNamespace(
        energy_price_per_kwh=0.90, energy_price_peak_kwh=1.20,
        energy_price_offpeak_kwh=0.45, tariff_peak_start_hour=8,
        tariff_peak_end_hour=22, tariff_weekend_offpeak=True,
    )
    fn = price_fn_from_settings(settings)
    assert fn(datetime(2026, 7, 6, 14, 0)) == 1.20    # Monday peak
    assert fn(datetime(2026, 7, 4, 14, 0)) == 0.45     # Saturday
