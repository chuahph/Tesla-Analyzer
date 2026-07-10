"""Time-of-use electricity pricing.

A single flat energy_price_per_kwh is the default and the only thing most
owners need. When peak/off-peak rates are configured, price_at() picks the
rate in effect at a given timestamp instead — Malaysian residential TOU
tariffs (e.g. TNB) typically price daytime higher and treat the whole
weekend as off-peak, which is the shape modelled here.
"""
from __future__ import annotations

from datetime import datetime


def price_at(
    dt: datetime,
    flat: float,
    peak: float = 0.0,
    off_peak: float = 0.0,
    peak_start_hour: int = 8,
    peak_end_hour: int = 22,
    weekend_off_peak: bool = True,
) -> float:
    """The RM/kWh rate in effect at ``dt``.

    Falls back to ``flat`` whenever TOU isn't configured (either rate <= 0),
    so a plain single-rate setup behaves exactly as before.
    """
    if peak <= 0 or off_peak <= 0:
        return flat
    if weekend_off_peak and dt.weekday() >= 5:
        return off_peak
    return peak if peak_start_hour <= dt.hour < peak_end_hour else off_peak


def price_fn_from_settings(settings):
    """A ``datetime -> RM/kWh`` function bound to the app's configured
    tariff, for callers that need to price several timestamps (a window of
    trips) without re-reading settings each time."""
    return lambda dt: price_at(
        dt, settings.energy_price_per_kwh,
        settings.energy_price_peak_kwh, settings.energy_price_offpeak_kwh,
        settings.tariff_peak_start_hour, settings.tariff_peak_end_hour,
        settings.tariff_weekend_offpeak,
    )
