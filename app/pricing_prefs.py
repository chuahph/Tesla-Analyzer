"""User-editable charging price preferences: Public / Home / Office, each
with its own AC and DC rate, plus which one to assume by default.

Persisted in the ``settings`` key-value table (see state.py) so they can be
changed from the dashboard's Rates page without touching .env or restarting
— unlike the ENERGY_PRICE_* env vars, which stay the fallback default for
Public until the user first saves this page.

A new charge auto-prices by matching its location against any Place named
"Home" or "Office" within HOME_OFFICE_MATCH_RADIUS_KM (wider than the tight
geofence radius used for trip labelling, so a car park near the office still
counts) — falling back to the saved default source when nothing matches.
Either way, the result is just a starting cost: the ✎ edit button (and the
🏠/🏢 quick-fill shortcuts) always let a session be corrected afterwards.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from . import state, tariff
from .analysis import haversine_km
from .models import Place

# Wider than a Place's own (typically ~150m) geofence radius — this is about
# "did this charge plausibly happen at/near home or the office", not the
# precise trip-labelling match, so a car park a couple of streets over still
# counts as "home" for pricing purposes.
HOME_OFFICE_MATCH_RADIUS_KM = 2.0

# Sensible starting points shown the first time the Rates page is opened —
# never applied silently to an already-saved preference.
DEFAULT_RATES = {
    "home_ac": 0.44,    # ~TNB residential ToU blended average
    "home_dc": 0.44,
    "office_ac": 0.57,
    "office_dc": 0.57,
}

_RATE_KEYS = {
    "public_ac": state.PRICE_PUBLIC_AC_KEY,
    "public_dc": state.PRICE_PUBLIC_DC_KEY,
    "home_ac": state.PRICE_HOME_AC_KEY,
    "home_dc": state.PRICE_HOME_DC_KEY,
    "office_ac": state.PRICE_OFFICE_AC_KEY,
    "office_dc": state.PRICE_OFFICE_DC_KEY,
}

SOURCES = ("public", "home", "office")


def get_rates(session: Session, settings) -> dict[str, float]:
    """All six rates: a DB-saved value if the user has ever saved the Rates
    page, else a fallback — Public falls back to the ENERGY_PRICE_AC/DC_KWH
    env vars (0 there means "use flat/ToU", same as today); Home/Office fall
    back to DEFAULT_RATES."""
    out = {}
    for key, setting_key in _RATE_KEYS.items():
        raw = state.get(session, setting_key, "")
        if raw:
            out[key] = float(raw)
        elif key == "public_ac":
            out[key] = settings.energy_price_ac_kwh
        elif key == "public_dc":
            out[key] = settings.energy_price_dc_kwh
        else:
            out[key] = DEFAULT_RATES[key]
    return out


def get_default_source(session: Session) -> str:
    return state.get(session, state.DEFAULT_PRICE_SOURCE_KEY, "public") or "public"


def save(session: Session, rates: dict[str, float], default_source: str) -> None:
    for key, setting_key in _RATE_KEYS.items():
        if key in rates and rates[key] is not None:
            state.put(session, setting_key, str(rates[key]))
    if default_source in SOURCES:
        state.put(session, state.DEFAULT_PRICE_SOURCE_KEY, default_source)


def _is_coords(text: str) -> bool:
    if "," not in text:
        return False
    try:
        lat, lon = (float(p.strip()) for p in text.split(",", 1))
    except ValueError:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def match_source(location_or_coords: str, session: Session) -> str | None:
    """"home"/"office" if this charge's location plausibly matches a Place
    the user has actually named that, else None (caller falls back to the
    saved default source) — never a blind guess off arbitrary text (a
    manually-typed "Home Depot Parking" shouldn't price as home charging).

    Accepts either raw "lat, lon" (the live sync path, before _place()
    resolves it to a display name — matched by distance) or a plain place
    name (manual entry / CSV import, which has no coordinates — matched by
    a case-insensitive substring against the registered Place's own name)."""
    text = (location_or_coords or "").strip()
    if not text:
        return None
    homes_and_offices = [
        p for p in session.query(Place).all() if p.name.strip().lower() in ("home", "office")
    ]
    if not homes_and_offices:
        return None
    if _is_coords(text):
        best_source, best_km = None, None
        for p in homes_and_offices:
            d = haversine_km(text, f"{p.lat}, {p.lon}")
            if d is not None and d <= HOME_OFFICE_MATCH_RADIUS_KM and (best_km is None or d < best_km):
                best_source, best_km = p.name.strip().lower(), d
        return best_source
    low = text.lower()
    for p in homes_and_offices:
        name = p.name.strip().lower()
        if name in low:
            return name
    return None


def rate_for_charge(
    session: Session, settings, location_or_coords: str, dc: bool, dt: datetime,
) -> float:
    """The RM/kWh rate for a newly-priced charge — auto-matched Home/Office
    location wins, else the saved default source. Public still falls back to
    flat/ToU pricing (tariff.price_at) when its own rate is 0, exactly as
    tariff.charge_price_at already does for the non-preference-aware path."""
    rates = get_rates(session, settings)
    source = match_source(location_or_coords, session) or get_default_source(session)
    if source != "public":
        return rates[f"{source}_{'dc' if dc else 'ac'}"]
    override = rates["public_dc"] if dc else rates["public_ac"]
    if override > 0:
        return override
    return tariff.price_at(
        dt, settings.energy_price_per_kwh,
        settings.energy_price_peak_kwh, settings.energy_price_offpeak_kwh,
        settings.tariff_peak_start_hour, settings.tariff_peak_end_hour,
        settings.tariff_weekend_offpeak,
    )
