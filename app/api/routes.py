"""REST API endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth, notifications, pricing_prefs, services, state, tariff, vin as vin_mod
from ..analysis import haversine_km
from ..analysis import narrative as narrative_engine
from ..analysis import battery as battery_analysis
from ..analysis import charging as charging_analysis
from ..analysis import driving as driving_analysis
from ..analysis import efficiency as efficiency_analysis
from ..analysis import recommendations as recommendations_engine
from ..analysis import service as service_analysis
from ..config import get_settings
from ..database import get_session
from ..importer import ImportError_, parse_upload
from ..models import BatteryReading, Charge, Drive, Place, ServiceRecord, Vehicle
from ..schemas import ChargeOut, DriveOut, VehicleOut

router = APIRouter(prefix="/api", tags=["analytics"])

# How long after an unexpected wake (phone-as-key, precondition, remote start —
# not our own manual wake_up) the sync cron should treat the car as "worth
# polling tightly": long enough to catch a likely departure, short enough that
# an online-but-idle car isn't kept awake past this on our account.
FAST_POLL_WINDOW_MIN = 3.0

# A vehicle_data() read is itself an activity signal to the car — it resets
# Tesla's own inactivity countdown, delaying sleep, regardless of how the
# request got triggered. /api/sync may now be called every minute or so (an
# external cron, an uptime monitor), far more often than a car naturally needs
# reading. Outside an active trip or a just-woke escalation window, don't read
# more often than settings.sync_poll_interval_min (config.py) — hitting
# /api/sync more often than that does NOT force more frequent reads, the
# endpoint decides for itself.

# If /api/sync hasn't written a last_status update in this long, something's
# wrong upstream of the app itself — the external cron has stopped firing, or
# a request is failing before it even gets far enough to record a status
# (e.g. a database write failure). A healthy 1-minute cron refreshes this
# every tick regardless of whether the car itself is reachable, so a gap
# this large is a real signal, not normal jitter.
CRON_STALE_MIN = 10.0

# How long "offline" (as opposed to a clean "asleep") must be sustained before
# an open trip is closed on it. Some accounts/cars report a genuinely-sleeping
# car as "offline" rather than "asleep", so treating only "asleep" as
# definitive left those cars' trips open indefinitely. This threshold is long
# enough that a momentary signal gap mid-drive (a tunnel, a dead zone) will
# already have recovered, short enough that a real stop still closes promptly.
UNREACHABLE_CLOSE_MIN = 3.0


def _save_last_status(session: Session, vin: str, **fields) -> None:
    """Persist the cron's own last determination of what the car was doing.

    Written on every /api/sync tick (including "found it asleep") so the
    dashboard can show a near-live status straight from the database on page
    load — mirroring what a push-based telemetry feed would give you, but
    built from polling: the cron is the thing pinging Tesla, writing the
    result to Neon every time, and the dashboard only ever reads that back.
    """
    import json as _json

    state.put(session, state.scoped(state.LAST_STATUS_KEY, vin), _json.dumps(fields))


def _first_vehicle(session: Session) -> Vehicle:
    # The car the dashboard follows (the active pick, else the linked car) takes
    # precedence over demo/imported rows.
    active_vin = state.active_vin(session)
    if active_vin:
        vehicle = session.scalars(
            select(Vehicle).where(Vehicle.vin == active_vin)
        ).first()
        if vehicle is not None:
            return vehicle
    vehicle = session.scalars(select(Vehicle).order_by(Vehicle.id)).first()
    if vehicle is None:
        raise HTTPException(404, "No vehicle data. Run the collector or seed demo data.")
    return vehicle


def _degradation_pct(session: Session, vehicle: Vehicle, settings) -> float | None:
    """The car's own range-based degradation estimate (% capacity lost vs
    new) — the same figure the Battery Health card shows, computed purely
    from range-projection history, independent of any charge-derived kWh
    figure. None while there isn't yet enough reading history.

    Runs on every sync tick (via _usable_capacity), so it selects only the
    two columns the estimate needs rather than hydrating up to 2000 full
    ORM rows a minute — the width of what crosses the wire to a remote
    Postgres matters more here than in a once-per-page-load path.
    """
    rows = session.execute(
        select(BatteryReading.soc, BatteryReading.range_km)
        .where(BatteryReading.vehicle_id == vehicle.id)
        .order_by(BatteryReading.ts)
        .limit(2000)
    ).all()
    vin_info = vin_mod.decode(vehicle.vin)
    spec_km = settings.battery_new_range_km or battery_analysis.new_range_for(
        vehicle.model, vehicle.trim, year=vin_info.get("year"))
    health = battery_analysis.analyze(
        [{"soc": soc, "range_km": range_km} for soc, range_km in rows], new_range_km=spec_km)
    return health["degradation_pct"] if health.get("available") else None


def _usable_capacity(session: Session, vehicle: Vehicle, settings) -> tuple[float, str]:
    """Usable pack capacity (kWh) for turning a drive's range/SoC delta into
    kWh, plus where it came from.

    Primary method: the factory spec for this exact variant (model/badge/
    wheel from the trim, generation from the VIN year) minus the car's own
    measured degradation (from range-projection history, the same figure the
    Battery Health card shows) — this ties the capacity used for every kWh/%
    calculation to the same degradation signal already displayed elsewhere,
    instead of two unrelated numbers that can silently disagree (e.g. a car
    showing 7% degradation implying, via a noisy charge-derived figure, a
    pack far smaller than spec-minus-7% would say).

    Falls back to the measured charge EMA when degradation data isn't
    available yet (a freshly-linked car with little reading history), then
    spec alone, then a generic default. An explicit config override always
    wins outright.
    """
    if settings.battery_capacity_kwh and settings.battery_capacity_kwh > 0:
        return settings.battery_capacity_kwh, "override"
    spec = battery_analysis.usable_capacity_for(
        vehicle.model, vehicle.trim, vin_mod.decode(vehicle.vin).get("year"))
    if spec:
        degradation = _degradation_pct(session, vehicle, settings)
        if degradation is not None:
            return round(spec * (1 - degradation / 100.0), 1), "spec - degradation"
    if vehicle.battery_capacity_kwh and vehicle.battery_capacity_kwh != 75.0:
        return vehicle.battery_capacity_kwh, "measured"
    return (spec or vehicle.battery_capacity_kwh or 75.0), ("variant spec" if spec else "default")


def _window(
    session: Session, vehicle_id: int, days: int,
    since: datetime | None = None, until: datetime | None = None,
):
    if since is None:
        since = datetime.now() - timedelta(days=days)
    drive_q = select(Drive).where(Drive.vehicle_id == vehicle_id, Drive.start_time >= since)
    charge_q = select(Charge).where(Charge.vehicle_id == vehicle_id, Charge.start_time >= since)
    if until is not None:
        drive_q = drive_q.where(Drive.start_time < until)
        charge_q = charge_q.where(Charge.start_time < until)
    drives = session.scalars(drive_q.order_by(Drive.start_time)).all()
    charges = session.scalars(charge_q.order_by(Charge.start_time)).all()
    return list(drives), list(charges)


def _live_eta(session: Session, snap: dict, live: dict, capacity_kwh: float) -> dict | None:
    """Distance/time/projected-SoC to the nearest named place the car isn't
    already at, estimated from the live drive's own current position and pace.

    Deliberately not a routed ETA (no map/routing service involved) — just a
    straight-line distance at the drive's own average speed so far, which is
    honest about what it is: a rough "will I make it, and with how much
    battery" gut-check, not turn-by-turn navigation. Needs at least one named
    place (see Place/_geofence_name) to have anything to project toward.
    """
    lat, lon = snap.get("lat"), snap.get("lon")
    if lat is None or lon is None:
        return None
    cur_coords = f"{lat}, {lon}"
    best_place, best_km = None, None
    for p in session.query(Place).all():
        dist = haversine_km(cur_coords, f"{p.lat}, {p.lon}")
        if dist is None or dist <= p.radius_km:
            continue  # unknown, or already there
        if best_km is None or dist < best_km:
            best_place, best_km = p, dist
    if best_place is None:
        return None
    from .. import sync as sync_mod

    # A just-started drive has near-zero avg speed (tiny elapsed time) — fall
    # back to a typical city pace rather than projecting a near-infinite ETA.
    pace = live["avg_speed_kmh"] if live.get("avg_speed_kmh", 0) >= 5.0 else sync_mod.CITY_SPEED_KMH
    wh_per_km = live.get("driving_wh_per_km") or live.get("wh_per_km") or 0.0
    projected_soc = None
    if capacity_kwh and wh_per_km:
        used_kwh = best_km * wh_per_km / 1000.0
        projected_soc = round(max(live["soc"] - used_kwh / capacity_kwh * 100.0, 0.0), 1)
    return {
        "place": best_place.name,
        "distance_km": round(best_km, 1),
        "eta_min": round(best_km / pace * 60.0),
        "projected_soc": projected_soc,
    }


_PLACE_CACHE: dict[str, tuple[str, str]] = {}


def _label_from_geocode(data: dict) -> tuple[str, str]:
    """Turn a Nominatim reverse payload into (label, area).

    ``label`` prefers the most *specific* feature actually at the point — the
    named POI (mall, building, amenity) or the street, with house number when
    present — over the broader neighbourhood/suburb. That's what a person
    calls "the place", so it tracks the real position instead of naming an
    adjacent district; falls back down the granularity ladder so there's
    always a name. ``area`` is the coarser district/suburb it sits in, kept
    separately so callers that need a GPS-jitter-stable grouping key (e.g.
    "did I drive this same route before?") aren't stuck matching on the
    specific label, which can legitimately vary between visits to the same
    place (a different POI/building matched a few metres apart).
    """
    addr = data.get("address") or {}
    # The named feature Nominatim matched at this exact point (a POI/building),
    # then the specific address fields, in decreasing precision.
    poi = (
        data.get("name")
        or addr.get("amenity") or addr.get("shop") or addr.get("building")
        or addr.get("office") or addr.get("leisure") or addr.get("tourism")
    )
    road = addr.get("road")
    if road and addr.get("house_number"):
        road = f"{addr['house_number']} {road}"
    name = (
        poi or road or addr.get("neighbourhood") or addr.get("suburb")
        or addr.get("village") or addr.get("town") or addr.get("city") or ""
    )
    # The surrounding district/city, kept coarser than `name` so the two read
    # as "specific spot, general area" rather than repeating the same word.
    area = (
        addr.get("suburb") or addr.get("city_district") or addr.get("city")
        or addr.get("town") or addr.get("county") or ""
    )
    label = f"{name}, {area}" if name and area and name != area else (name or area)
    return label[:120], area[:120]


def _geofence_name(coords: str, session: Session | None) -> str | None:
    """Nearest user-defined Place (e.g. "Home", "Office") whose radius
    contains these coords, if any — checked before any network geocode so a
    user's own name for a place always wins over OSM's, and a well-known
    driveway/office never needs a lookup at all."""
    if not session or not coords or "," not in coords:
        return None
    best_name, best_km = None, None
    for p in session.query(Place).all():
        d = haversine_km(coords, f"{p.lat}, {p.lon}")
        if d is not None and d <= p.radius_km and (best_km is None or d < best_km):
            best_name, best_km = p.name, d
    return best_name


def _place_and_area(coords: str, session: Session | None = None) -> tuple[str, str]:
    """Best-effort reverse geocode of a 'lat, lon' string to (label, area).

    Uses OpenStreetMap's Nominatim (a couple of lookups per completed trip is
    well within its usage policy). Any failure falls back to the raw
    coordinates for both, which stay searchable in a maps app. A coordinate
    inside a user-defined geofence (see _geofence_name) short-circuits this
    entirely and uses the user's own name for both label and area.
    """
    geofenced = _geofence_name(coords, session)
    if geofenced:
        return geofenced, geofenced
    if not coords or "," not in coords:
        return coords, coords
    if coords in _PLACE_CACHE:
        return _PLACE_CACHE[coords]
    try:
        lat, lon = (p.strip() for p in coords.split(",", 1))
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/reverse",
            # zoom 18 = building/address level (16 stopped at neighbourhood, so
            # it named a broad district instead of the actual spot);
            # namedetails surfaces the matched feature's own name.
            params={
                "format": "jsonv2", "lat": lat, "lon": lon,
                "zoom": 18, "addressdetails": 1, "namedetails": 1,
            },
            headers={"User-Agent": "tesla-analyzer/0.1"},
            timeout=4.0,
        )
        resp.raise_for_status()
        label, area = _label_from_geocode(resp.json())
        result = (label or coords, area or coords)
    except Exception:  # noqa: BLE001 — never let naming block trip logging
        result = (coords, coords)
    _PLACE_CACHE[coords] = result
    return result


def _place(coords: str, session: Session | None = None) -> str:
    """Specific place label only — for callers (Charge locations) that don't
    need the coarser route-grouping key."""
    return _place_and_area(coords, session)[0]


def _build_info() -> dict:
    """Deployed version: git SHA (from the host's env) + image build time in MYT."""
    import os
    from pathlib import Path

    from ..sync import MYT

    sha = (os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GITHUB_SHA") or "")
    sha = sha.strip()[:7] or None
    time_str = None
    for p in ("/app/.build_time", ".build_time"):
        try:
            ts = float(Path(p).read_text().strip())
            time_str = datetime.fromtimestamp(ts, MYT).strftime("%d %b %Y %H%M")
            break
        except (OSError, ValueError):
            continue
    return {"sha": sha, "time": time_str}


@router.get("/health")
def health(session: Session = Depends(get_session)):
    source = state.data_source(session)
    mode = "live" if state.is_live(session) else ("imported" if source == "imported" else "demo")
    return {
        "status": "ok",
        "mode": mode,
        "source": source,
        "oauth_available": auth.oauth_configured(),
        "build": _build_info(),
    }


# --- Data source: manual import -------------------------------------------


@router.post("/import")
async def import_data(
    file: UploadFile = File(...), session: Session = Depends(get_session)
):
    """Button 1 — load a Tesla privacy/usage data export (CSV/JSON/ZIP)."""
    content = await file.read()
    try:
        drives, charges = parse_upload(file.filename or "upload", content)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not read file: {exc}") from exc
    return services.replace_with_import(session, drives, charges)


@router.post("/data/clear-drives")
def clear_drives(session: Session = Depends(get_session)):
    """Wipe the trip history for a clean start (charges/battery data kept).

    Sits behind the passcode gate like every other endpoint.
    """
    deleted = services.clear_drives(session)
    return {"deleted_drives": deleted}


@router.post("/data/delete-drives")
def delete_drives(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Delete only the selected trips (by id); charges/battery kept."""
    ids = [int(i) for i in (payload.get("ids") or []) if str(i).lstrip("-").isdigit()]
    deleted = services.delete_drives(session, ids)
    return {"deleted_drives": deleted}


@router.post("/data/tag-drive")
def tag_drive(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Set (or clear) a single trip's work/personal/... category."""
    drive_id = payload.get("id")
    if not isinstance(drive_id, int):
        raise HTTPException(400, "Missing or invalid 'id'.")
    tag = str(payload.get("tag") or "")
    if not services.tag_drive(session, drive_id, tag):
        raise HTTPException(404, "Trip not found.")
    return {"id": drive_id, "tag": tag}


# --- Named places (geofenced Home/Office/... trip labels) -----------------


def _relabel_existing(session: Session, place: Place) -> int:
    """Retroactively apply a new/changed geofence's name to already-logged
    trips (and charges, where the raw coords are still recoverable) whose
    stored coordinates fall inside its radius — so defining "Home" today
    renames the driveway on last month's trips too, not just future ones.

    Charge rows don't keep a separate raw-coords column (only the already-
    resolved ``location`` label), so a charge can only be relabeled here when
    that label still looks like coordinates — i.e. the geocode never
    resolved it to a name. Charges already labeled by Nominatim are left as
    they are; only new charges going through ``_place`` pick up the geofence
    from here on.
    """
    changed = 0
    place_coords = f"{place.lat}, {place.lon}"
    drives = session.scalars(
        select(Drive).where((Drive.start_coords != "") | (Drive.end_coords != ""))
    ).all()
    for d in drives:
        if d.start_coords:
            dist = haversine_km(d.start_coords, place_coords)
            if dist is not None and dist <= place.radius_km:
                d.start_location = place.name
                d.start_area = place.name
                changed += 1
        if d.end_coords:
            dist = haversine_km(d.end_coords, place_coords)
            if dist is not None and dist <= place.radius_km:
                d.end_location = place.name
                d.end_area = place.name
                changed += 1
    for c in session.scalars(select(Charge)).all():
        if c.location and "," in c.location:
            dist = haversine_km(c.location, place_coords)
            if dist is not None and dist <= place.radius_km:
                c.location = place.name
                changed += 1
    return changed


@router.get("/places")
def list_places(session: Session = Depends(get_session)):
    """User-defined geofences that override trip/charge location names."""
    places = session.scalars(select(Place).order_by(Place.name)).all()
    return [
        {"id": p.id, "name": p.name, "lat": p.lat, "lon": p.lon, "radius_km": p.radius_km}
        for p in places
    ]


@router.post("/places")
def create_place(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Add (or, by name, update) a named geofence and relabel matching history."""
    name = str(payload.get("name") or "").strip()[:60]
    if not name:
        raise HTTPException(400, "Missing 'name'.")
    try:
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Missing or invalid 'lat'/'lon'.")
    radius_km = float(payload.get("radius_km") or 0.15)
    radius_km = max(0.02, min(radius_km, 5.0))  # sane bounds: 20 m to 5 km

    place = session.scalars(select(Place).where(Place.name == name)).first()
    if place:
        place.lat, place.lon, place.radius_km = lat, lon, radius_km
    else:
        place = Place(name=name, lat=lat, lon=lon, radius_km=radius_km,
                      created_at=datetime.now())
        session.add(place)
    session.flush()
    relabeled = _relabel_existing(session, place)
    session.commit()
    _PLACE_CACHE.clear()  # a newly-named geofence can relabel already-cached coords
    return {"id": place.id, "name": place.name, "lat": place.lat, "lon": place.lon,
            "radius_km": place.radius_km, "relabeled": relabeled}


@router.delete("/places/{place_id}")
def delete_place(place_id: int, session: Session = Depends(get_session)):
    """Remove a geofence. Already-relabeled trips keep the place's name —
    only new processing stops applying it."""
    place = session.get(Place, place_id)
    if place is None:
        raise HTTPException(404, "Place not found.")
    session.delete(place)
    session.commit()
    return {"deleted": True}


# --- Service & tyre tracker -------------------------------------------------


@router.get("/service")
def list_service(session: Session = Depends(get_session)):
    """Logged maintenance history plus a due/overdue reading for each known
    service type (see app/analysis/service.py)."""
    vehicle = _first_vehicle(session)
    records = session.scalars(
        select(ServiceRecord)
        .where(ServiceRecord.vehicle_id == vehicle.id)
        .order_by(ServiceRecord.date.desc())
    ).all()
    current_odo = session.scalar(
        select(func.max(BatteryReading.odo_km)).where(BatteryReading.vehicle_id == vehicle.id)
    )
    due = service_analysis.due_status(
        [{"type": r.type, "date": r.date, "odo_km": r.odo_km} for r in records],
        current_odo_km=current_odo,
    )
    for row in due:
        row["last_date"] = row["last_date"].isoformat() if row["last_date"] else None
        row["due_date"] = row["due_date"].isoformat() if row["due_date"] else None
    return {
        "current_odo_km": round(current_odo, 1) if current_odo is not None else None,
        "types": list(service_analysis.SERVICE_INTERVALS.keys()),
        "due": due,
        "records": [
            {"id": r.id, "type": r.type, "date": r.date.isoformat(), "odo_km": r.odo_km,
             "cost": r.cost, "notes": r.notes}
            for r in records
        ],
    }


@router.post("/service")
def add_service(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Log a maintenance event. ``type`` can be any of the known tracked
    types (gets a due-date/odometer projection) or free text (logged for the
    record but never shows as due/overdue)."""
    vehicle = _first_vehicle(session)
    type_ = str(payload.get("type") or "").strip()[:40]
    if not type_:
        raise HTTPException(400, "Missing 'type'.")
    try:
        date = datetime.fromisoformat(payload["date"]) if payload.get("date") else datetime.now()
    except (KeyError, ValueError):
        raise HTTPException(400, "Invalid 'date' (expected ISO format).")
    try:
        odo_km = float(payload.get("odo_km") or 0.0)
        cost = float(payload.get("cost") or 0.0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid 'odo_km'/'cost'.")
    notes = str(payload.get("notes") or "")[:200]

    record = ServiceRecord(
        vehicle_id=vehicle.id, type=type_, date=date, odo_km=odo_km, cost=cost, notes=notes,
    )
    session.add(record)
    session.commit()
    return {"id": record.id}


@router.delete("/service/{record_id}")
def delete_service(record_id: int, session: Session = Depends(get_session)):
    record = session.get(ServiceRecord, record_id)
    if record is None:
        raise HTTPException(404, "Record not found.")
    session.delete(record)
    session.commit()
    return {"deleted": True}


# --- Data source: link Tesla account --------------------------------------


@router.post("/link/token")
def link_token(
    payload: dict = Body(...), session: Session = Depends(get_session)
):
    """Button 2 (token flow) — link an account with an access token."""
    token = (payload.get("access_token") or "").strip()
    if not token:
        raise HTTPException(422, "access_token is required.")
    try:
        return services.link_with_token(
            session,
            token,
            refresh_token=(payload.get("refresh_token") or "").strip(),
            base_url=(payload.get("base_url") or "").strip() or None,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(401, f"Tesla rejected the token ({exc.response.status_code}).") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not link account: {exc}") from exc


def _oauth_redirect_uri(request: Request) -> str:
    """Callback URL derived from the live host, so no env config is needed.

    Cloud proxies terminate TLS, so anything that isn't localhost is forced to
    https (Tesla also refuses plain-http redirect URIs).
    """
    host = request.url.hostname or "localhost"
    if host in ("localhost", "127.0.0.1"):
        return str(request.base_url).rstrip("/") + "/api/link/oauth/callback"
    return f"https://{request.url.netloc}/api/link/oauth/callback"


@router.get("/link/oauth/start")
def oauth_start(request: Request, session: Session = Depends(get_session)):
    """Button 2 (OAuth flow) — redirect to Tesla's sign-in page."""
    if not auth.oauth_configured():
        raise HTTPException(
            400,
            "Tesla OAuth is not configured. Set TESLA_CLIENT_ID / TESLA_CLIENT_SECRET, "
            "or use the access-token option instead.",
        )
    # One-time Fleet API requirement: register this domain with Tesla. Tesla
    # fetches the public key the app serves under /.well-known/ during the call.
    if state.get(session, "partner_registered") != "yes":
        domain = request.url.hostname or ""
        try:
            auth.register_partner(domain)
            state.put(session, "partner_registered", "yes")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                400,
                f"Tesla app registration for domain '{domain}' failed: {exc}. "
                "Check TESLA_CLIENT_ID / TESLA_CLIENT_SECRET and that this domain "
                "is listed under Allowed Origins in your Tesla developer app.",
            ) from exc
    url, _state = auth.authorize_url(_oauth_redirect_uri(request))
    return RedirectResponse(url)


@router.get("/link/oauth/callback")
def oauth_callback(
    request: Request,
    code: str | None = None, error: str | None = None,
    session: Session = Depends(get_session),
):
    if error:
        raise HTTPException(400, f"Tesla sign-in failed: {error}")
    if not code:
        raise HTTPException(400, "Missing authorization code.")
    try:
        tokens = auth.exchange_code(code, _oauth_redirect_uri(request))
        result = services.link_with_token(
            session,
            tokens["access_token"],
            refresh_token=tokens.get("refresh_token", ""),
            base_url=get_settings().tesla_oauth_audience,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"OAuth exchange failed: {exc}") from exc
    # Land the user back on the dashboard.
    return RedirectResponse(f"/?linked={result['source']}")


def _process_vehicle(
    session: Session, data: dict, v_summary: dict, settings, migrate_legacy: bool = False
) -> tuple:
    """Log drives/charges for one car from its vehicle_data snapshot.

    Snapshot / open-trip / open-charge state is namespaced by VIN, so each car
    on the account advances its own independent session state machine. Returns
    ``(vehicle, snapshot, n_drives, n_charges, open_trip)``.

    ``migrate_legacy`` (set for the active car) folds in the pre-multi-car global
    ``last_snapshot`` state a one time, so a drive taken around the upgrade to
    per-VIN state isn't dropped.
    """
    import json as _json

    from .. import sync as sync_mod

    vin = data.get("vin") or v_summary.get("vin") or "LINKED-UNKNOWN"
    vehicle = session.query(Vehicle).filter(Vehicle.vin == vin).first()
    if vehicle is None:
        vehicle = Vehicle(vin=vin, name=data.get("display_name") or "My Tesla", model="Tesla")
        session.add(vehicle)
        session.flush()

    # Enrich from the car's own config (real model / trim / colour / wheels).
    cfg = data.get("vehicle_config") or {}
    car_map = {"model3": "Model 3", "modely": "Model Y",
               "models": "Model S", "modelx": "Model X"}
    real_model = (
        car_map.get((cfg.get("car_type") or "").lower().replace(" ", ""))
        or vin_mod.decode(vin).get("model")
    )
    if real_model and vehicle.model in ("Tesla", ""):
        vehicle.model = real_model
    if not vehicle.trim:
        trim_bits = [
            (cfg.get("trim_badging") or "").upper(),
            (cfg.get("exterior_color") or ""),
        ]
        vehicle.trim = " ".join(b for b in trim_bits if b)
    wheel = cfg.get("wheel_type") or ""
    if wheel and wheel.upper() not in vehicle.trim.upper():
        vehicle.trim = f"{vehicle.trim} {wheel}".strip()[:60]
    if data.get("display_name") and vehicle.name in ("My Tesla", ""):
        vehicle.name = data["display_name"]
    # Seed usable capacity from the variant spec so the EMA starts from the
    # right pack size (an LR Model 3 is ~78 kWh, not the generic 75 default)
    # and per-drive kWh is right from the first synced drive. Only when the
    # column is still untouched — a measured value is never overwritten.
    if vehicle.battery_capacity_kwh == 75.0:
        spec_cap = battery_analysis.usable_capacity_for(
            vehicle.model, vehicle.trim, vin_mod.decode(vin).get("year"))
        if spec_cap:
            vehicle.battery_capacity_kwh = spec_cap

    snap = sync_mod.snapshot_from_vehicle_data(data)
    sk = state.scoped(state.SNAPSHOT_KEY, vin)
    tk = state.scoped(state.OPEN_TRIP_KEY, vin)
    ck = state.scoped(state.OPEN_CHARGE_KEY, vin)
    # Usable pack capacity: override > measured charge EMA (seeded from the
    # variant spec) > spec > default. The EMA is a smoothed average over
    # measured charges — robust to a single contaminated charge reading that
    # a "last full charge" figure would swallow whole.
    capacity_kwh, _ = _usable_capacity(session, vehicle, settings)

    # One-time migration from the pre-multi-car global keys (only the active car
    # inherits them), so a drive taken around the upgrade isn't lost.
    recovered: list[dict] = []
    if migrate_legacy:
        legacy_snap = state.get(session, state.SNAPSHOT_KEY)  # bare/global key
        if legacy_snap:
            if not state.get(session, sk):
                # Never synced under the scoped key yet — adopt the legacy state so
                # the normal gap-fallback below logs the missed drive.
                state.put(session, sk, legacy_snap)
                if not state.get(session, tk):
                    state.put(session, tk, state.get(session, state.OPEN_TRIP_KEY) or "")
                if not state.get(session, ck):
                    state.put(session, ck, state.get(session, state.OPEN_CHARGE_KEY) or "")
            else:
                # Already synced under the scoped key once (the transition drive
                # slipped through) — reconstruct it from the legacy → scoped gap.
                try:
                    legacy = _json.loads(legacy_snap)
                    scoped = _json.loads(state.get(session, sk))
                    d = sync_mod._drive_from(legacy, scoped, capacity_kwh)
                    if d:
                        # The car slept between the two snapshots, so the true drive
                        # time is unknown; give it a sensible duration from the
                        # distance (~40 km/h) anchored at the later snapshot, rather
                        # than the whole multi-hour gap.
                        dur = max(round(d["distance_km"] / 40.0 * 60.0), 1)
                        end = sync_mod._dt(scoped["ts"])
                        d["end_time"] = end
                        d["start_time"] = end - timedelta(minutes=dur)
                        d["duration_min"] = float(dur)
                        d["avg_speed_kmh"] = round(d["distance_km"] / (dur / 60.0), 1)
                        d["max_speed_kmh"] = max(d["max_speed_kmh"], d["avg_speed_kmh"])
                        recovered.append(d)
                except (ValueError, KeyError, TypeError):
                    pass
            state.delete(
                session, state.SNAPSHOT_KEY, state.OPEN_TRIP_KEY, state.OPEN_CHARGE_KEY
            )

    prev_raw = state.get(session, sk)
    prev = _json.loads(prev_raw) if prev_raw else None
    open_trip = _json.loads(state.get(session, tk) or "null")
    open_charge = _json.loads(state.get(session, ck) or "null")

    drives, charges, open_trip, open_charge = sync_mod.process_snapshot(
        prev, snap, open_trip, open_charge,
        capacity_kwh, settings.energy_price_per_kwh, settings.drive_min_km,
    )
    drives = recovered + drives  # include a drive recovered from the upgrade gap
    for d in drives:
        # Keep the raw coords (for map links) before geocoding replaces them.
        d["start_coords"], d["end_coords"] = d["start_location"], d["end_location"]
        d["start_location"], d["start_area"] = _place_and_area(d["start_location"], session)
        d["end_location"], d["end_area"] = _place_and_area(d["end_location"], session)
        session.add(Drive(vehicle_id=vehicle.id, **d))
        # Webhook-only (not routed through notify()'s push channel) — a
        # push alert per every single drive would be unwanted noise for
        # anyone who already has charge-complete/low-battery push enabled,
        # but a home-automation webhook consumer (arrive-home triggers,
        # trip logging, ...) very much wants this event.
        notifications.fire_webhook(
            "drive-complete", "Drive completed",
            f"{vehicle.name}: {d['distance_km']:.1f} km, {d['duration_min']:.0f} min, "
            f"{d['start_soc']:.0f}% → {d['end_soc']:.0f}%.",
        )
    for c in charges:
        cap = sync_mod.implied_capacity_kwh(c)
        c.pop("energy_measured", None)  # transient flag, not a DB column
        if cap:
            old = vehicle.battery_capacity_kwh or 75.0
            vehicle.battery_capacity_kwh = round(0.8 * old + 0.2 * cap, 1)
        raw_coords = c.get("location", "")
        c["location"] = _place(raw_coords, session)
        # Re-price at the session's own start time and actual charger type —
        # auto-matched Home/Office location (or the saved default source)
        # takes priority; Public falls back to flat/ToU pricing.
        source, rate = pricing_prefs.resolve_source_and_rate(
            session, settings, raw_coords, c["charge_type"] == "DC", c["start_time"])
        c["cost"] = round(c["energy_added_kwh"] * rate, 2)
        c["price_source"] = source
        session.add(Charge(vehicle_id=vehicle.id, **c))
        notifications.notify(
            session, "Charging complete",
            f"{vehicle.name}: {c['energy_added_kwh']:.1f} kWh added, now at {c['end_soc']:.0f}%.",
            tag="charge-complete",
        )
    if snap["soc"] > 0 and snap.get("range_km", 0) > 0:
        last_reading = session.scalars(
            select(BatteryReading)
            .where(BatteryReading.vehicle_id == vehicle.id)
            .order_by(BatteryReading.ts.desc())
        ).first()
        if last_reading is None or abs(last_reading.soc - snap["soc"]) >= 1.0:
            session.add(BatteryReading(
                vehicle_id=vehicle.id,
                ts=datetime.fromtimestamp(snap["ts"], sync_mod.MYT).replace(tzinfo=None),
                soc=snap["soc"],
                range_km=round(snap["range_km"], 1),
                odo_km=round(snap["odo_km"], 1),
            ))
        # Low-battery alert: fires once per low episode (a state.py flag,
        # cleared once SoC recovers past the threshold + a small hysteresis
        # band so it doesn't flicker on/off right at the line), not on every
        # sync tick while it stays low.
        threshold = settings.low_soc_notify_pct
        if threshold > 0:
            notified_key = state.scoped(state.LOW_SOC_NOTIFIED_KEY, vin)
            already_notified = state.get(session, notified_key) == "1"
            if snap["soc"] <= threshold and not already_notified:
                notifications.notify(
                    session, "Battery low",
                    f"{vehicle.name} is at {snap['soc']:.0f}% — time to plug in.",
                    tag="low-soc",
                )
                state.put(session, notified_key, "1")
            elif snap["soc"] > threshold + 5 and already_notified:
                state.put(session, notified_key, "")

    session.commit()
    state.put(session, sk, _json.dumps(snap))
    state.put(session, tk, _json.dumps(open_trip) if open_trip else "")
    state.put(session, ck, _json.dumps(open_charge) if open_charge else "")
    return vehicle, snap, len(drives), len(charges), open_trip


@router.get("/sync")  # GET so external cron/uptime services can trigger it
@router.post("/sync")
def sync_now(wake: bool = Query(False), session: Session = Depends(get_session)):
    """Snapshot the linked car and log what happened since the last snapshot.

    ``wake=1`` (the manual Sync button) nudges a sleeping car online first.
    The cron never wakes the car, so it can't drain the battery overnight.
    """
    import time

    from .. import sync as sync_mod
    from ..tesla_client import TeslaClient
    import json as _json

    settings = get_settings()
    token = state.active_token(session)
    if not token:
        raise HTTPException(400, "No linked Tesla account — link your account first.")
    base = state.active_base_url(session)
    active_target = state.active_vin(session)

    now_ts = datetime.now().timestamp()

    def make_client(tok):
        return TeslaClient(access_token=tok, base_url=base)

    def refresh_or_401() -> str:
        refresh = state.get(session, state.REFRESH_KEY)
        if not refresh or not auth.oauth_configured():
            raise HTTPException(401, "Token expired — sign in with Tesla again.")
        tokens = auth.refresh_tokens(refresh)
        state.put(session, state.TOKEN_KEY, tokens["access_token"])
        if tokens.get("refresh_token"):
            state.put(session, state.REFRESH_KEY, tokens["refresh_token"])
        return tokens["access_token"]

    # List every car on the account (with a single token-refresh retry).
    try:
        vehicles = make_client(token).list_vehicles()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            token = refresh_or_401()
            vehicles = make_client(token).list_vehicles()
        else:
            raise HTTPException(exc.response.status_code, f"Tesla error: {exc}") from exc
    except httpx.RequestError as exc:
        # Network/timeout reaching Tesla — return a clean 503, never a 500.
        raise HTTPException(503, "Couldn't reach Tesla right now — try again in a moment.") from exc
    if not vehicles:
        raise HTTPException(404, "No vehicles on this Tesla account.")

    # The car the dashboard follows (and the only one the manual button wakes) —
    # keep the current pick if it's still on the account, else default to first.
    account_vins = [vv.get("vin") for vv in vehicles]
    if active_target not in account_vins:
        active_target = account_vins[0]
    state.put(session, state.ACTIVE_VIN_KEY, active_target)
    state.put(session, state.LINKED_VIN_KEY, active_target)

    client = make_client(token)
    total = {"drives": 0, "charges": 0}
    purged = False
    active_snap = active_open_trip = active_vehicle = None
    active_cfg: dict = {}
    active_seen_online = False

    for vv in vehicles:
        vvin = vv.get("vin")
        vid = vv.get("id_s") or vv.get("id")
        vstate = vv.get("state")
        if vvin == active_target and vstate == "online":
            active_seen_online = True
        # Tesla's own reported state, captured before any wake_up() of ours below
        # — the pure signal of whether the car woke up on its own (phone-as-key,
        # a scheduled precondition, remote start) between this poll and the last.
        # list_vehicles() is a cached backend read that never touches the car, so
        # tracking it costs nothing extra regardless of polling frequency.
        raw_vstate = vstate
        vstate_key = state.scoped(state.LAST_VSTATE_KEY, vvin)
        prev_vstate = state.get(session, vstate_key)
        if prev_vstate and prev_vstate != "online" and raw_vstate == "online":
            # It came online without our help — possibly about to drive off.
            # Remember this so the caller can poll tightly for a short bounded
            # window instead of waiting up to a full cron tick to notice.
            state.put(session, state.scoped(state.WOKE_AT_KEY, vvin), str(now_ts))
        state.put(session, vstate_key, raw_vstate or "")

        # Only ever wake the active car, so a multi-car account is never woken
        # (and drained) all at once.
        if wake and vvin == active_target and vstate and vstate != "online":
            try:
                client.wake_up(vid)
                for _ in range(6):  # cars typically wake within ~15-30 s
                    time.sleep(5)
                    fresh = [x for x in client.list_vehicles() if x.get("vin") == vvin]
                    if fresh and fresh[0].get("state") == "online":
                        vstate = "online"
                        break
            except Exception:  # noqa: BLE001 — wake is best-effort
                pass
        if vstate and vstate != "online":
            # A car can only reach true "asleep" while parked and idle — never
            # mid-drive — so it's an immediate, definitive "the trip is over"
            # signal. "offline" is murkier: some accounts/cars report a
            # genuinely-sleeping car as "offline" rather than "asleep", but it
            # can *also* mean a momentary signal gap during an active drive (a
            # tunnel, a dead zone). So "offline" only counts once it's been
            # sustained for UNREACHABLE_CLOSE_MIN straight — long enough that a
            # brief blip would have already recovered, short enough that a
            # short trip still closes promptly rather than waiting hours for
            # the car to happen to wake up again.
            unreachable_key = state.scoped(state.UNREACHABLE_SINCE_KEY, vvin)
            unreachable_since = float(state.get(session, unreachable_key) or 0)
            if not unreachable_since:
                unreachable_since = now_ts
                state.put(session, unreachable_key, str(now_ts))
            sustained_offline = (now_ts - unreachable_since) >= UNREACHABLE_CLOSE_MIN * 60

            if vstate == "asleep" or sustained_offline:
                trip_key = state.scoped(state.OPEN_TRIP_KEY, vvin)
                trip_raw = state.get(session, trip_key)
                charge_key = state.scoped(state.OPEN_CHARGE_KEY, vvin)
                charge_raw = state.get(session, charge_key)
                last_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, vvin))
                vehicle_row = (
                    session.query(Vehicle).filter(Vehicle.vin == vvin).first()
                    if (trip_raw or charge_raw) and last_raw else None
                )
                row_capacity_kwh = _usable_capacity(session, vehicle_row, settings)[0] if vehicle_row else 75.0
                if trip_raw and last_raw and vehicle_row:
                    d = sync_mod.close_trip_on_sleep(
                        _json.loads(trip_raw), _json.loads(last_raw),
                        row_capacity_kwh, settings.drive_min_km,
                    )
                    if d:
                        d["start_coords"], d["end_coords"] = d["start_location"], d["end_location"]
                        d["start_location"], d["start_area"] = _place_and_area(d["start_location"], session)
                        d["end_location"], d["end_area"] = _place_and_area(d["end_location"], session)
                        session.add(Drive(vehicle_id=vehicle_row.id, **d))
                        session.commit()
                        total["drives"] += 1
                        notifications.fire_webhook(
                            "drive-complete", "Drive completed",
                            f"{vehicle_row.name}: {d['distance_km']:.1f} km, "
                            f"{d['duration_min']:.0f} min, {d['start_soc']:.0f}% → "
                            f"{d['end_soc']:.0f}% (car went offline/asleep).",
                        )
                if trip_raw:
                    state.put(session, trip_key, "")
                # Charging usually keeps the car awake, so this rarely fires —
                # but connectivity can still drop mid-session, and without
                # this an interrupted charge would sit open indefinitely
                # waiting for a reconnect, never logged to Neon at all.
                if charge_raw and last_raw and vehicle_row:
                    c = sync_mod.close_charge_on_sleep(
                        _json.loads(charge_raw), _json.loads(last_raw),
                        row_capacity_kwh, settings.energy_price_per_kwh, settings.drive_min_km,
                    )
                    if c:
                        cap = sync_mod.implied_capacity_kwh(c)
                        c.pop("energy_measured", None)
                        if cap:
                            old_cap = vehicle_row.battery_capacity_kwh or 75.0
                            vehicle_row.battery_capacity_kwh = round(0.8 * old_cap + 0.2 * cap, 1)
                        raw_coords = c.get("location", "")
                        c["location"] = _place(raw_coords, session)
                        source, rate = pricing_prefs.resolve_source_and_rate(
                            session, settings, raw_coords, c["charge_type"] == "DC", c["start_time"])
                        c["cost"] = round(c["energy_added_kwh"] * rate, 2)
                        c["price_source"] = source
                        session.add(Charge(vehicle_id=vehicle_row.id, **c))
                        session.commit()
                        total["charges"] += 1
                        # Closed via the car going offline/asleep rather than a
                        # clean "stopped charging" reading — could be complete
                        # or genuinely interrupted, so the message stays neutral.
                        notifications.notify(
                            session, "Charging session ended",
                            f"{vehicle_row.name}: {c['energy_added_kwh']:.1f} kWh added, "
                            f"now at {c['end_soc']:.0f}% (car went offline/asleep).",
                            tag="charge-complete",
                        )
                if charge_raw:
                    state.put(session, charge_key, "")
            continue  # asleep/offline — nothing readable right now

        # Back online — this unreachable episode (if any) is over; the next
        # one starts its own fresh clock.
        state.put(session, state.scoped(state.UNREACHABLE_SINCE_KEY, vvin), "")

        # The car is online, but that alone isn't reason enough to read it —
        # it may just not have fallen asleep yet from something unrelated to
        # us. Only actually call vehicle_data() (the read that resets Tesla's
        # own sleep countdown) when there's a concrete reason to: a trip or
        # charge is already open (need to track it live), it woke up
        # unprompted within the escalation window (may be about to drive
        # off), the normal base interval has elapsed anyway, or this is the
        # user's own manual sync.
        poll_key = state.scoped(state.LAST_POLL_KEY, vvin)
        last_poll_ts = float(state.get(session, poll_key) or 0)
        woke_at = float(state.get(session, state.scoped(state.WOKE_AT_KEY, vvin)) or 0)
        recently_woke = bool(woke_at) and (now_ts - woke_at) <= FAST_POLL_WINDOW_MIN * 60
        due = (now_ts - last_poll_ts) >= settings.sync_poll_interval_min * 60
        trip_in_progress = bool(state.get(session, state.scoped(state.OPEN_TRIP_KEY, vvin)))
        charge_in_progress = bool(state.get(session, state.scoped(state.OPEN_CHARGE_KEY, vvin)))
        manual_sync = wake and vvin == active_target
        if not (trip_in_progress or charge_in_progress or recently_woke or due or manual_sync):
            continue  # online but idle, not due yet — let it settle toward sleep
        state.put(session, poll_key, str(now_ts))

        try:
            data = client.vehicle_data(vid)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 401:
                token = refresh_or_401()
                client = make_client(token)
                try:
                    data = client.vehicle_data(vid)
                except (httpx.HTTPStatusError, httpx.RequestError):
                    continue
            elif code == 408:
                continue  # fell asleep between the list and the read
            else:
                raise HTTPException(code, f"Tesla error: {exc}") from exc
        except httpx.RequestError:
            continue  # transient network error for this car — skip it this round

        if not purged:
            services.purge_demo(session)  # retire the seeded sample on first real data
            purged = True
        vehicle, snap, nd, nc, open_trip = _process_vehicle(
            session, data, vv, settings, migrate_legacy=(vvin == active_target)
        )
        total["drives"] += nd
        total["charges"] += nc
        if vvin == active_target:
            active_snap, active_open_trip, active_vehicle = snap, open_trip, vehicle
            active_cfg = data.get("vehicle_config") or {}

    state.put(session, state.SOURCE_KEY, "linked")

    # The dashboard's live status reflects the active car specifically.
    if active_snap is None:
        # Distinguish "genuinely asleep/offline" from "online, but this tick
        # deliberately skipped reading it" (the poll-throttle above) — telling
        # a user their online car is "asleep" would be actively misleading.
        resp = {
            "status": "asleep" if not active_seen_online else "parked",
            "tried_wake": wake,
            "logged": total,
            "poll_fast": False,
            "note": ("Couldn't wake the car — it may be offline. Try again in a minute."
                     if wake else
                     "Car is asleep — try again while charging or right after a drive."
                     if not active_seen_online else
                     "Car is online but idle — skipping this read to let it settle to "
                     "sleep. It'll be read again shortly if that changes."),
        }
        last_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, active_target))
        if last_raw:
            last = _json.loads(last_raw)
            resp["last"] = {"soc": last.get("soc"), "ts": last.get("ts"),
                            "odo_km": round(last.get("odo_km", 0), 1)}
        _save_last_status(
            session, active_target, status=resp["status"], ts=now_ts,
            soc=resp.get("last", {}).get("soc"), odo_km=resp.get("last", {}).get("odo_km"),
            speed_kmh=None, note=resp["note"],
        )
        return resp

    snap, open_trip, vehicle = active_snap, active_open_trip, active_vehicle
    if snap["charging"]:
        activity = "charging"
    elif sync_mod.is_driving(snap):
        activity = "driving"
    elif open_trip:
        activity = "stopped"  # trip still open — parked briefly, driver present
    else:
        activity = "parked"

    # Tell the caller (the sync cron) whether it's worth polling again soon
    # instead of waiting for the next scheduled tick: a trip is actively in
    # progress, or the car just woke up on its own within the last few
    # minutes and may be about to drive off. Bounded so an online-but-idle
    # car isn't kept awake indefinitely — once the window lapses (or it goes
    # back to sleep) this drops to False and the normal cadence takes over.
    woke_at = float(state.get(session, state.scoped(state.WOKE_AT_KEY, active_target)) or 0)
    recently_woke = bool(woke_at) and (now_ts - woke_at) <= FAST_POLL_WINDOW_MIN * 60
    poll_fast = activity == "driving" or recently_woke

    _save_last_status(
        session, active_target, status=activity, ts=now_ts,
        soc=snap["soc"], odo_km=round(snap["odo_km"], 1),
        speed_kmh=round(snap.get("speed_kmh") or 0.0), note=None,
    )
    return {
        "status": activity,
        "soc": snap["soc"],
        "odo_km": round(snap["odo_km"], 1),
        "speed_kmh": round(snap.get("speed_kmh") or 0.0),
        "trip_in_progress": bool(open_trip),
        "poll_fast": poll_fast,
        # Tells the dashboard whether the token really has location access —
        # the 403 fallback makes a missing scope otherwise invisible.
        "location_access": snap.get("lat") is not None,
        # Config the active car reported this sync — makes wheel detection auditable.
        "wheel_type": active_cfg.get("wheel_type") or None,
        "trim": vehicle.trim,
        "logged": total,
    }


@router.post("/link/refresh")
def refresh_link(session: Session = Depends(get_session)):
    """Mint a fresh access token from the stored refresh token (OAuth links)."""
    refresh = state.get(session, state.REFRESH_KEY)
    if not refresh:
        raise HTTPException(400, "No refresh token stored — link the account first.")
    try:
        tokens = auth.refresh_tokens(refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(401, f"Token refresh failed: {exc}") from exc
    state.put(session, state.TOKEN_KEY, tokens["access_token"])
    if tokens.get("refresh_token"):
        state.put(session, state.REFRESH_KEY, tokens["refresh_token"])
    return {"status": "refreshed"}


@router.get("/vehicles", response_model=list[VehicleOut])
def list_vehicles(session: Session = Depends(get_session)):
    return session.scalars(select(Vehicle).order_by(Vehicle.id)).all()


@router.get("/compare")
def compare_vehicles(days: int = Query(30, ge=1, le=730), session: Session = Depends(get_session)):
    """One summary row per real (non-demo/import) car on the account, over
    the same window — a household with more than one Tesla can see at a
    glance which car is driven more, costs more to run, or is degrading
    faster, without switching the active car back and forth."""
    settings = get_settings()
    vehicles = session.scalars(
        select(Vehicle).where(~Vehicle.vin.startswith("DEMO"), ~Vehicle.vin.startswith("IMPORT"))
        .order_by(Vehicle.id)
    ).all()

    rows = []
    for vehicle in vehicles:
        capacity_kwh, _ = _usable_capacity(session, vehicle, settings)
        drives, charges = _window(session, vehicle.id, days)
        driving = driving_analysis.analyze(
            drives, settings.rated_wh_per_km, capacity_kwh, tariff.price_fn_from_settings(settings))
        charging = charging_analysis.analyze(charges, drives)
        readings = session.execute(
            select(BatteryReading.soc, BatteryReading.range_km,
                   BatteryReading.ts, BatteryReading.odo_km)
            .where(BatteryReading.vehicle_id == vehicle.id)
            .order_by(BatteryReading.ts)
            .limit(2000)
        ).all()
        vin_info = vin_mod.decode(vehicle.vin)
        spec_km = settings.battery_new_range_km or battery_analysis.new_range_for(
            vehicle.model, vehicle.trim, year=vin_info.get("year"))
        battery = battery_analysis.analyze(
            [{"soc": soc, "range_km": rng, "ts": ts, "odo_km": odo}
             for soc, rng, ts, odo in readings],
            new_range_km=spec_km,
        )
        rows.append({
            "vin": vehicle.vin,
            "name": vehicle.name,
            "model": vehicle.model,
            "distance_km": driving.get("total_distance_km") if driving.get("available") else 0.0,
            "drives": driving.get("total_drives") if driving.get("available") else 0,
            "avg_wh_per_km": driving.get("avg_efficiency_wh_per_km") if driving.get("available") else None,
            "driving_cost": driving.get("total_cost") if driving.get("available") else None,
            "cost_per_km": driving.get("cost_per_km") if driving.get("available") else None,
            "charging_cost": charging.get("total_cost") if charging.get("available") else None,
            "energy_charged_kwh": charging.get("total_energy_kwh") if charging.get("available") else None,
            "health_pct": battery.get("health_pct") if battery.get("available") else None,
            "vs_fleet_pct": battery.get("vs_fleet_pct") if battery.get("available") else None,
        })
    return {"window_days": days, "currency": settings.currency, "vehicles": rows}


@router.post("/unlink")
def unlink_account(session: Session = Depends(get_session)):
    """Disconnect the linked Tesla account so a different one can be linked
    (keeps the logged history)."""
    return services.unlink(session)


@router.post("/active-vehicle")
def set_active_vehicle(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Pick which linked car the dashboard follows (multi-car accounts)."""
    vin = (payload.get("vin") or "").strip()
    vehicle = session.query(Vehicle).filter(Vehicle.vin == vin).first()
    if vehicle is None:
        raise HTTPException(404, "Unknown vehicle.")
    state.put(session, state.ACTIVE_VIN_KEY, vin)
    state.put(session, state.LINKED_VIN_KEY, vin)
    return {"status": "ok", "active_vin": vin, "name": vehicle.name}


@router.get("/drives", response_model=list[DriveOut])
def list_drives(
    days: int = Query(30, ge=1, le=730),
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    vehicle = _first_vehicle(session)
    drives, _ = _window(session, vehicle.id, days)
    return drives[-limit:]


@router.get("/charges", response_model=list[ChargeOut])
def list_charges(
    days: int = Query(30, ge=1, le=730),
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
):
    vehicle = _first_vehicle(session)
    _, charges = _window(session, vehicle.id, days)
    return charges[-limit:]


@router.post("/charges/manual")
def add_manual_charge(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Log a charging session by hand — for a session the sync loop never
    saw (missed before the account was linked, or dropped by a bug that's
    since been fixed) and has no live snapshot data left to reconstruct
    from. Purely additive: inserts one Charge row for the active vehicle
    and touches nothing else, unlike /api/import (which replaces the
    entire dataset).
    """
    settings = get_settings()
    try:
        start_time = datetime.fromisoformat(payload["start_time"])
        end_time = datetime.fromisoformat(payload["end_time"])
    except (KeyError, ValueError):
        raise HTTPException(400, "Missing or invalid 'start_time'/'end_time' (expected ISO format).")
    if end_time <= start_time:
        raise HTTPException(400, "'end_time' must be after 'start_time'.")
    try:
        energy_added_kwh = float(payload["energy_added_kwh"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Missing or invalid 'energy_added_kwh'.")
    if energy_added_kwh <= 0:
        raise HTTPException(400, "'energy_added_kwh' must be greater than 0.")

    charge_type = str(payload.get("charge_type") or "AC").upper()
    if charge_type not in ("AC", "DC"):
        raise HTTPException(400, "'charge_type' must be 'AC' or 'DC'.")
    try:
        start_soc = float(payload.get("start_soc") or 0.0)
        end_soc = float(payload.get("end_soc") or 0.0)
        max_power_kw = float(payload.get("max_power_kw") or 0.0)
        outside_temp_c = float(payload.get("outside_temp_c") or 20.0)
        cost = payload.get("cost")
        cost = float(cost) if cost not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid numeric field.")
    location = str(payload.get("location") or "")[:120]
    # No telemetry field reliably marks a free session (e.g. a Tesla
    # Destination Charger) apart from a paid AC charger, so this is a
    # manual flag rather than something auto-detected.
    is_free = bool(payload.get("is_free"))

    # A source is only recorded when the rate was auto-resolved — a free
    # session or one given an explicit cost isn't tied to any of the three
    # presets, so the dashboard falls back to guessing from location text.
    price_source = ""
    if is_free:
        cost = 0.0
    elif cost is None:
        price_source, rate = pricing_prefs.resolve_source_and_rate(
            session, settings, location, charge_type == "DC", start_time)
        cost = round(energy_added_kwh * rate, 2)

    vehicle = _first_vehicle(session)
    charge = Charge(
        vehicle_id=vehicle.id, start_time=start_time, end_time=end_time,
        duration_min=round((end_time - start_time).total_seconds() / 60.0, 1),
        start_soc=start_soc, end_soc=end_soc, energy_added_kwh=round(energy_added_kwh, 2),
        charge_type=charge_type, max_power_kw=max_power_kw, location=location,
        cost=cost, outside_temp_c=outside_temp_c, is_free=is_free, price_source=price_source,
    )
    session.add(charge)
    session.commit()
    return {"id": charge.id}


@router.post("/charges/edit-rate")
def edit_charge_rate(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Recalculate one charging session's cost from a per-kWh rate you
    supply — for a session priced at something other than the configured
    AC/DC default (a promo rate, a pricier one-off public charger, ...).
    0 doubles as marking the session free.

    An optional 'source' (from the dashboard's 🌐/🏠/🏢 quick-rate buttons)
    is persisted so the row's selected-icon indicator can show it later —
    without this, a typed custom rate clears any source the charge had, since
    it no longer matches one of the three presets.
    """
    charge_id = payload.get("id")
    if not isinstance(charge_id, int):
        raise HTTPException(400, "Missing or invalid 'id'.")
    try:
        rate = float(payload["price_per_kwh"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Missing or invalid 'price_per_kwh'.")
    if rate < 0:
        raise HTTPException(400, "'price_per_kwh' must be >= 0.")
    source = payload.get("source") or ""
    if source and source not in pricing_prefs.SOURCES:
        raise HTTPException(400, "'source' must be 'public', 'home', or 'office'.")

    charge = session.get(Charge, charge_id)
    if charge is None:
        raise HTTPException(404, "Charge not found.")
    charge.cost = round(charge.energy_added_kwh * rate, 2)
    charge.is_free = rate == 0
    charge.price_source = source
    session.commit()
    return {
        "id": charge.id, "cost": charge.cost, "is_free": charge.is_free,
        "source": charge.price_source or None,
    }


@router.get("/pricing-prefs")
def get_pricing_prefs(session: Session = Depends(get_session)):
    """Current Public/Home/Office AC+DC rates and which source new charges
    default to — the Rates page reads this to populate its form."""
    settings = get_settings()
    return {
        "rates": pricing_prefs.get_rates(session, settings),
        "default_source": pricing_prefs.get_default_source(session),
        "match_radius_km": pricing_prefs.HOME_OFFICE_MATCH_RADIUS_KM,
    }


@router.post("/pricing-prefs")
def save_pricing_prefs(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Save the Rates page. Only affects charges priced from now on — never
    retroactive (use the ✎ edit-rate button on a session to fix one already
    logged)."""
    raw_rates = payload.get("rates") or {}
    if not isinstance(raw_rates, dict):
        raise HTTPException(400, "'rates' must be an object.")
    rates: dict[str, float] = {}
    for key in ("public_ac", "public_dc", "home_ac", "home_dc", "office_ac", "office_dc"):
        if key not in raw_rates or raw_rates[key] in (None, ""):
            continue
        try:
            value = float(raw_rates[key])
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{key}' must be a number.")
        if value < 0:
            raise HTTPException(400, f"'{key}' must be >= 0.")
        rates[key] = value

    default_source = str(payload.get("default_source") or "public")
    if default_source not in pricing_prefs.SOURCES:
        raise HTTPException(400, "'default_source' must be 'public', 'home', or 'office'.")

    pricing_prefs.save(session, rates, default_source)
    settings = get_settings()
    return {
        "rates": pricing_prefs.get_rates(session, settings),
        "default_source": pricing_prefs.get_default_source(session),
    }


@router.get("/export")
def export_data(
    days: int = Query(730, ge=1, le=3650), session: Session = Depends(get_session)
):
    """Export stored drives & charges as JSON (re-importable via /api/import)."""
    vehicle = _first_vehicle(session)
    drives, charges = _window(session, vehicle.id, days)
    return {
        "vehicle": VehicleOut.model_validate(vehicle).model_dump(),
        "drives": [DriveOut.model_validate(d).model_dump() for d in drives],
        "charges": [ChargeOut.model_validate(c).model_dump() for c in charges],
    }


def _build_export_zip(drives: list[Drive], charges: list[Charge]) -> bytes:
    """The drives.csv + charges.csv ZIP bytes, shared by the download
    endpoint and the backup webhook so both produce an identically
    re-importable archive."""
    import csv
    import io
    import zipfile

    def sheet(headers, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(rows)
        return buf.getvalue()

    ts = lambda t: t.isoformat(sep=" ", timespec="minutes")  # noqa: E731
    drives_csv = sheet(
        ["start_time", "end_time", "distance_km", "duration_min", "start_soc",
         "end_soc", "energy_used_kwh", "avg_speed_kmh", "max_speed_kmh",
         "outside_temp_c", "start_location", "end_location"],
        [[ts(d.start_time), ts(d.end_time), d.distance_km, d.duration_min,
          d.start_soc, d.end_soc, d.energy_used_kwh, d.avg_speed_kmh,
          d.max_speed_kmh, d.outside_temp_c, d.start_location, d.end_location]
         for d in drives],
    )
    charges_csv = sheet(
        ["start_time", "end_time", "duration_min", "start_soc", "end_soc",
         "energy_added_kwh", "charge_type", "max_power_kw", "location",
         "cost", "outside_temp_c"],
        [[ts(c.start_time), ts(c.end_time), c.duration_min, c.start_soc,
          c.end_soc, c.energy_added_kwh, c.charge_type, c.max_power_kw,
          c.location, c.cost, c.outside_temp_c] for c in charges],
    )

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("drives.csv", drives_csv)
        z.writestr("charges.csv", charges_csv)
    return zbuf.getvalue()


@router.get("/export/csv")
def export_csv(
    days: int = Query(3650, ge=1, le=3650),
    since_charge: bool = Query(False),
    current_drive: bool = Query(False),
    session: Session = Depends(get_session),
):
    """Download drives & charges as a ZIP of CSVs (re-importable).

    Defaults to everything; pass ``days``, ``since_charge`` or
    ``current_drive`` to export only the currently viewed window.
    """
    import json as _json

    from fastapi.responses import Response

    vehicle = _first_vehicle(session)
    since = None
    label = "all"
    if current_drive:
        open_trip = _json.loads(
            state.get(session, state.scoped(state.OPEN_TRIP_KEY, vehicle.vin)) or "null")
        if open_trip:
            from .. import sync as sync_mod

            since = datetime.fromtimestamp(open_trip["ts"], sync_mod.MYT).replace(tzinfo=None)
        else:
            since = session.scalar(
                select(func.max(Drive.start_time)).where(Drive.vehicle_id == vehicle.id)
            )
        if since is not None:
            label = "current-drive"
    elif since_charge:
        last_end = session.scalar(
            select(func.max(Charge.end_time)).where(Charge.vehicle_id == vehicle.id)
        )
        if last_end is not None:
            since = last_end
            label = "since-charge"
    elif days < 3650:
        label = f"{days}d"
    drives, charges = _window(session, vehicle.id, days, since=since)

    zip_bytes = _build_export_zip(drives, charges)
    name = f"tesla-analyzer-{vehicle.vin[-6:]}-{label}.zip"
    return Response(
        zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/backup")
def backup_now(session: Session = Depends(get_session)):
    """POST the full-history export ZIP to the configured backup webhook.

    Cron-callable the same way /api/sync is (the sync_key query param passes
    the passcode gate) — call this on whatever schedule you want a backup
    (daily/weekly) from the same external cron that already hits /api/sync.
    No internal scheduling: every call sends a fresh full backup, so the
    calling cron's own interval is the backup interval.
    """
    settings = get_settings()
    url = settings.backup_webhook_url.strip()
    if not url:
        raise HTTPException(400, "No BACKUP_WEBHOOK_URL configured.")

    vehicle = _first_vehicle(session)
    drives, charges = _window(session, vehicle.id, days=3650)
    zip_bytes = _build_export_zip(drives, charges)
    name = f"tesla-analyzer-{vehicle.vin[-6:]}-backup.zip"

    try:
        resp = httpx.post(
            url, content=zip_bytes,
            headers={"Content-Type": "application/zip",
                    "Content-Disposition": f'attachment; filename="{name}"'},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Backup webhook delivery failed: {exc}") from exc

    return {
        "sent": True, "bytes": len(zip_bytes),
        "drives": len(drives), "charges": len(charges),
        "webhook_status": resp.status_code,
    }


def _monthly_report_payload(session: Session, vehicle: Vehicle, settings, days: int) -> dict:
    """Driving/charging/efficiency summary for the last ``days`` days, plus a
    plain-text rendering — Slack and Discord incoming webhooks both read a
    top-level "text" field directly, so the same payload works untouched
    there while still carrying the structured figures for anything else."""
    capacity_kwh, _ = _usable_capacity(session, vehicle, settings)
    drives, charges = _window(session, vehicle.id, days)
    price_fn = tariff.price_fn_from_settings(settings)
    driving = driving_analysis.analyze(drives, settings.rated_wh_per_km, capacity_kwh, price_fn)
    charging = charging_analysis.analyze(charges, drives)
    efficiency = efficiency_analysis.analyze(drives, settings.rated_wh_per_km)
    cur = settings.currency

    lines = [f"📊 {vehicle.name} — last {days} days"]
    if driving.get("available"):
        lines.append(
            f"🚗 {driving['total_distance_km']} km over {driving['total_drives']} drives "
            f"({driving['total_duration_h']} h)"
        )
        if driving.get("total_cost") is not None:
            lines.append(f"💵 {cur} {driving['total_cost']} in driving energy cost")
    else:
        lines.append("🚗 No drives logged in this period.")
    if efficiency.get("available") and efficiency.get("avg_efficiency_wh_per_km"):
        vs = efficiency["vs_rated_pct"]
        lines.append(
            f"📈 {efficiency['avg_efficiency_wh_per_km']} Wh/km "
            f"({'+' if vs >= 0 else ''}{vs}% vs rated)"
        )
    if charging.get("available"):
        lines.append(
            f"⚡ {charging['total_energy_kwh']} kWh charged across "
            f"{charging['total_sessions']} sessions — {cur} {charging['total_cost']}"
        )
    else:
        lines.append("⚡ No charging sessions logged in this period.")

    # Data-driven narrative: this period vs the equal-length one immediately
    # before it, so "you drove more/less than usual" is a real comparison
    # instead of a stats table with no context.
    now = datetime.now()
    since = now - timedelta(days=days)
    prev_since = since - timedelta(days=days)
    prev_drives, prev_charges = _window(session, vehicle.id, days, since=prev_since, until=since)
    prev_driving = driving_analysis.analyze(prev_drives, settings.rated_wh_per_km, capacity_kwh, price_fn)
    prev_charging = charging_analysis.analyze(prev_charges, prev_drives)
    prev_efficiency = efficiency_analysis.analyze(prev_drives, settings.rated_wh_per_km)
    narrative_lines = narrative_engine.build(
        {"driving": driving, "charging": charging, "efficiency": efficiency},
        {"driving": prev_driving, "charging": prev_charging, "efficiency": prev_efficiency},
        cur,
    )

    return {
        "text": "\n".join(lines) + "\n\n📝 " + " ".join(narrative_lines),
        "narrative": narrative_lines,
        "vehicle": vehicle.name,
        "period_days": days,
        "driving": driving,
        "charging": charging,
        "efficiency": efficiency,
    }


@router.get("/reports/monthly")
def monthly_report(days: int = Query(30, ge=1, le=365), session: Session = Depends(get_session)):
    """POST a driving/charging/efficiency summary to the configured report
    webhook — cron-callable the same way /api/sync and /api/backup are (the
    sync_key query param passes the passcode gate). No internal scheduling:
    call this on whatever schedule you want the report at (monthly is the
    intended use — see README) from the same external cron that already
    hits /api/sync; ``days`` controls how far back each report looks.
    """
    settings = get_settings()
    url = settings.report_webhook_url.strip()
    if not url:
        raise HTTPException(400, "No REPORT_WEBHOOK_URL configured.")

    vehicle = _first_vehicle(session)
    payload = _monthly_report_payload(session, vehicle, settings, days)

    try:
        resp = httpx.post(url, json=payload, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Report webhook delivery failed: {exc}") from exc

    return {"sent": True, "webhook_status": resp.status_code, "period_days": days}


# --- Web push notifications -------------------------------------------------


@router.get("/push/vapid-public-key")
def push_vapid_public_key():
    """The VAPID application server key the browser needs to call
    PushManager.subscribe(). 404 (not just an empty value) when push isn't
    configured, so the frontend can cleanly hide the "Enable notifications"
    control rather than offer a subscribe button that would fail."""
    key = notifications.public_key_b64()
    if not key:
        raise HTTPException(404, "Push notifications aren't configured on this server.")
    return {"key": key}


@router.post("/push/subscribe")
def push_subscribe(payload: dict = Body(...), session: Session = Depends(get_session)):
    """Register a browser's push subscription. Body matches the browser's
    own PushSubscription.toJSON() shape: {endpoint, keys: {p256dh, auth}}."""
    if not notifications.enabled():
        raise HTTPException(404, "Push notifications aren't configured on this server.")
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "Malformed subscription payload.")
    notifications.subscribe(session, endpoint, keys["p256dh"], keys["auth"])
    return {"subscribed": True}


@router.post("/push/unsubscribe")
def push_unsubscribe(payload: dict = Body(...), session: Session = Depends(get_session)):
    endpoint = payload.get("endpoint")
    if endpoint:
        notifications.unsubscribe(session, endpoint)
    return {"unsubscribed": True}


@router.get("/summary")
def summary(
    days: int = Query(90, ge=1, le=730),
    since_charge: bool = Query(False),
    current_drive: bool = Query(False),
    session: Session = Depends(get_session),
):
    """The single endpoint the dashboard consumes: full analysis + recommendations.

    With ``since_charge`` the window starts when the most recent charging
    session ended. With ``current_drive`` it covers the drive in progress
    (plus a live-trip readout) or, if the car is parked, the last drive.
    """
    import json as _json

    from .. import sync as sync_mod

    settings = get_settings()
    vehicle = _first_vehicle(session)
    # Usable pack capacity (override > measured EMA > variant spec > default),
    # used everywhere below that turns kWh into % or range delta into kWh.
    capacity_kwh, capacity_source = _usable_capacity(session, vehicle, settings)
    # The cron's own last determination of what the car was doing (including
    # "found it asleep") — written every /api/sync tick, read here purely
    # from the database. This is what lets the dashboard show a near-live
    # status on page load without itself ever pinging Tesla: the background
    # cron already did, and left the answer sitting in Neon.
    last_status_raw = state.get(session, state.scoped(state.LAST_STATUS_KEY, vehicle.vin))
    last_status = _json.loads(last_status_raw) if last_status_raw else None
    # Computed fresh on every request from the server's own clock (not the
    # browser's), so a stale/wrong client clock can't mask or fake this.
    if last_status is not None:
        age_min = (datetime.now().timestamp() - (last_status.get("ts") or 0)) / 60.0
        last_status["stale"] = age_min > CRON_STALE_MIN
    since = None
    window_label = None
    live = None
    if current_drive:
        open_trip = _json.loads(
            state.get(session, state.scoped(state.OPEN_TRIP_KEY, vehicle.vin)) or "null")
        snap_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, vehicle.vin))
        snap = _json.loads(snap_raw) if snap_raw else None
        if open_trip and snap:
            live = sync_mod.live_trip(open_trip, snap, capacity_kwh, settings.drive_min_km)
            live["eta"] = _live_eta(session, snap, live, capacity_kwh)
            since = datetime.fromtimestamp(open_trip["ts"], sync_mod.MYT).replace(tzinfo=None)
            window_label = "current drive"
        else:
            last_start = session.scalar(
                select(func.max(Drive.start_time)).where(Drive.vehicle_id == vehicle.id)
            )
            if last_start is not None:
                since = last_start
                window_label = "last drive"
    # Fetched unconditionally (not just for since_charge) so the dashboard can
    # always pin a "last charge" row atop the Recent Charges list — the
    # charge itself is otherwise invisible in the since-charge view (it ended
    # right at the window's start, so it's excluded from every list below by
    # definition), which reads as "my charge went missing" rather than "this
    # window starts after it"; showing it in every window keeps the format
    # (and the at-a-glance context) consistent regardless of which is picked.
    # Same field shape as charging_analysis.recent_charges' rows (id/rate_per_kwh/
    # is_free included) so the frontend can render and dedupe both with one
    # template instead of two.
    last_charge_summary = None
    last_charge = session.scalar(
        select(Charge).where(Charge.vehicle_id == vehicle.id)
        .order_by(Charge.end_time.desc())
    )
    if last_charge is not None:
        # kWh driven since this charge ended — independent of whatever window
        # is currently selected, so "Net Battery" (last charge's kWh added
        # minus this) reads the same regardless of which window the user has
        # picked, same as last_charge_summary itself.
        used_since_last_charge_kwh = session.scalar(
            select(func.sum(Drive.energy_used_kwh))
            .where(Drive.vehicle_id == vehicle.id, Drive.start_time >= last_charge.end_time)
        ) or 0.0
        last_charge_summary = {
            "id": last_charge.id,
            "start_time": last_charge.start_time.isoformat(timespec="minutes"),
            "end_time": last_charge.end_time.isoformat(timespec="minutes"),
            "energy_added_kwh": last_charge.energy_added_kwh,
            "start_soc": last_charge.start_soc,
            "end_soc": last_charge.end_soc,
            "cost": last_charge.cost,
            "charge_type": last_charge.charge_type,
            "location": last_charge.location,
            "rate_per_kwh": (
                round(last_charge.cost / last_charge.energy_added_kwh, 3)
                if last_charge.energy_added_kwh else None
            ),
            "is_free": bool(last_charge.is_free),
            "used_since_kwh": round(used_since_last_charge_kwh, 2),
            "source": last_charge.price_source or None,
        }
        if since_charge:
            since = last_charge.end_time
            window_label = "since last charge"
    drives, charges = _window(session, vehicle.id, days, since=since)

    driving = driving_analysis.analyze(
        drives, settings.rated_wh_per_km, capacity_kwh, tariff.price_fn_from_settings(settings))
    charging = charging_analysis.analyze(charges, drives)
    efficiency = efficiency_analysis.analyze(drives, settings.rated_wh_per_km)

    # This week vs last week (rolling 7-day windows anchored at now), regardless
    # of the display window — a steady, comparable pulse of usage.
    now = datetime.now()
    wk_drives = [d for d in drives if d.start_time >= now - timedelta(days=7)] \
        if since is None and days >= 14 else None
    week_compare = None
    if wk_drives is not None:
        last_wk = [d for d in drives
                   if now - timedelta(days=14) <= d.start_time < now - timedelta(days=7)]
        if wk_drives and last_wk:
            def _wk(ds):
                dist = sum(d.distance_km for d in ds)
                energy = sum(d.energy_used_kwh for d in ds)
                return {
                    "drives": len(ds),
                    "distance_km": round(dist, 1),
                    "energy_kwh": round(energy, 1),
                    "wh_per_km": round(energy * 1000.0 / dist) if dist and energy > 0 else None,
                }
            week_compare = {"this": _wk(wk_drives), "last": _wk(last_wk)}

    # Data-driven narrative: this window vs the equal-length one immediately
    # before it. Same gating as week_compare — only meaningful for a plain
    # days-based window (not "since last charge"/"current drive", which have
    # no natural "period before" to compare against).
    narrative_lines = None
    if since is None and days >= 14:
        cur_since = now - timedelta(days=days)
        prev_since = cur_since - timedelta(days=days)
        prev_drives, prev_charges = _window(session, vehicle.id, days, since=prev_since, until=cur_since)
        prev_driving = driving_analysis.analyze(
            prev_drives, settings.rated_wh_per_km, capacity_kwh, tariff.price_fn_from_settings(settings))
        prev_charging = charging_analysis.analyze(prev_charges, prev_drives)
        prev_efficiency = efficiency_analysis.analyze(prev_drives, settings.rated_wh_per_km)
        narrative_lines = narrative_engine.build(
            {"driving": driving, "charging": charging, "efficiency": efficiency},
            {"driving": prev_driving, "charging": prev_charging, "efficiency": prev_efficiency},
            settings.currency,
        )

    # Battery Used %: gross energy used this window (parking/idle included,
    # matching total_energy_used_kwh) as a share of usable pack capacity — the
    # same figure that turns the window's kWh into %.
    full_charge_kwh = capacity_kwh
    charged_kwh = charging.get("total_energy_kwh") or 0.0
    used_kwh = (driving.get("total_energy_used_kwh") or 0.0) if driving.get("available") else 0.0
    # Battery Balance: how much charge is actually left in the pack right now
    # (the latest logged SoC reading) — the "fuel gauge", not a derived delta.
    current_soc = session.scalar(
        select(BatteryReading.soc)
        .where(BatteryReading.vehicle_id == vehicle.id)
        .order_by(BatteryReading.ts.desc())
        .limit(1)
    )
    battery_balance = {
        "full_charge_kwh": full_charge_kwh,
        "charged_kwh": round(charged_kwh, 1),
        "used_kwh": round(used_kwh, 1),
        "used_pct": round(used_kwh / full_charge_kwh * 100.0, 1) if full_charge_kwh else None,
        "current_soc_pct": round(current_soc, 1) if current_soc is not None else None,
    }

    # Petrol comparator (TCO): what an equivalent petrol car would have cost
    # to run this window's distance, at the configured price/consumption.
    # Both settings default to 0 (disabled) — no assumed "average car" figure
    # is guessed, since a wrong one would misinform rather than just be absent.
    petrol_comparison = None
    if (
        settings.petrol_price_per_liter > 0 and settings.petrol_l_per_100km > 0
        and driving.get("available")
    ):
        distance_km = driving["total_distance_km"]
        petrol_cost = round(
            distance_km / 100.0 * settings.petrol_l_per_100km * settings.petrol_price_per_liter, 2
        )
        ev_cost = driving.get("total_cost")
        petrol_comparison = {
            "distance_km": distance_km,
            "petrol_cost": petrol_cost,
            "ev_cost": ev_cost,
            "savings": round(petrol_cost - ev_cost, 2) if ev_cost is not None else None,
            "petrol_price_per_liter": settings.petrol_price_per_liter,
            "petrol_l_per_100km": settings.petrol_l_per_100km,
        }

    # Battery health uses the full reading history, not the display window.
    # Column-only select: analyze() needs four fields, not 2000 hydrated ORM
    # rows on every dashboard load.
    readings = session.execute(
        select(BatteryReading.soc, BatteryReading.range_km,
               BatteryReading.ts, BatteryReading.odo_km)
        .where(BatteryReading.vehicle_id == vehicle.id)
        .order_by(BatteryReading.ts)
        .limit(2000)
    ).all()
    # 100% reference: explicit override, else the factory figure for this
    # exact variant — model+badge+wheel from the trim, generation from the
    # VIN's model-year letter (74D means 536 km in 2023 but 549 km in 2024).
    vin_info = vin_mod.decode(vehicle.vin)
    spec_km = settings.battery_new_range_km or battery_analysis.new_range_for(
        vehicle.model, vehicle.trim, year=vin_info.get("year")
    )
    battery = battery_analysis.analyze(
        [{"soc": soc, "range_km": rng, "ts": ts, "odo_km": odo}
         for soc, rng, ts, odo in readings],
        new_range_km=spec_km,
    )

    tou = None
    if settings.energy_price_peak_kwh > 0 and settings.energy_price_offpeak_kwh > 0:
        tou = {
            "peak_price": settings.energy_price_peak_kwh,
            "offpeak_price": settings.energy_price_offpeak_kwh,
            "peak_start_hour": settings.tariff_peak_start_hour,
            "peak_end_hour": settings.tariff_peak_end_hour,
        }
    recs = recommendations_engine.build(
        driving,
        charging,
        efficiency,
        battery,
        energy_price=settings.energy_price_per_kwh,
        currency=settings.currency,
        tou=tou,
    )

    vehicle_out = VehicleOut.model_validate(vehicle).model_dump()
    vehicle_out.update({k: v for k, v in vin_info.items() if v})  # year, plant
    # The pack size actually used to turn range/SoC deltas into kWh, and where
    # it came from — surfaced so a wrong figure (which scales every drive's
    # kWh) is visible and diagnosable rather than hidden.
    vehicle_out["usable_capacity_kwh"] = round(capacity_kwh, 1)
    vehicle_out["capacity_source"] = capacity_source
    # Whether time-of-use pricing is active, so it's clear why cost figures
    # vary by time of day instead of using the flat rate.
    vehicle_out["tou_enabled"] = bool(
        settings.energy_price_peak_kwh > 0 and settings.energy_price_offpeak_kwh > 0)
    # The account's cars, so the header can offer a picker when more than one is
    # linked. Only real (account-linked) cars, not demo/imported placeholders.
    garage = []
    if state.data_source(session) == "linked":
        garage = [
            {"vin": v.vin, "name": v.name, "model": v.model}
            for v in session.scalars(select(Vehicle).order_by(Vehicle.id)).all()
            if not v.vin.startswith(("DEMO", "IMPORT"))
        ]
    return {
        "vehicle": vehicle_out,
        "active_vin": vehicle.vin,
        "garage": garage,
        "window_days": days,
        "window_label": window_label,
        "last_charge": last_charge_summary,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "currency": settings.currency,
        "last_status": last_status,
        "live_trip": live,
        "driving": driving,
        "charging": charging,
        "efficiency": efficiency,
        "battery": battery,
        "battery_balance": battery_balance,
        "petrol_comparison": petrol_comparison,
        "week_compare": week_compare,
        "narrative": narrative_lines,
        "recommendations": recs,
    }
