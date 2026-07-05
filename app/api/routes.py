"""REST API endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth, services, state, vin as vin_mod
from ..analysis import battery as battery_analysis
from ..analysis import charging as charging_analysis
from ..analysis import driving as driving_analysis
from ..analysis import efficiency as efficiency_analysis
from ..analysis import recommendations as recommendations_engine
from ..config import get_settings
from ..database import get_session
from ..importer import ImportError_, parse_upload
from ..models import BatteryReading, Charge, Drive, Vehicle
from ..schemas import ChargeOut, DriveOut, VehicleOut

router = APIRouter(prefix="/api", tags=["analytics"])


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


def _window(session: Session, vehicle_id: int, days: int, since: datetime | None = None):
    if since is None:
        since = datetime.now() - timedelta(days=days)
    drives = session.scalars(
        select(Drive)
        .where(Drive.vehicle_id == vehicle_id, Drive.start_time >= since)
        .order_by(Drive.start_time)
    ).all()
    charges = session.scalars(
        select(Charge)
        .where(Charge.vehicle_id == vehicle_id, Charge.start_time >= since)
        .order_by(Charge.start_time)
    ).all()
    return list(drives), list(charges)


_PLACE_CACHE: dict[str, str] = {}


def _place(coords: str) -> str:
    """Best-effort reverse geocode of a 'lat, lon' string to a short place name.

    Uses OpenStreetMap's Nominatim (a couple of lookups per completed trip is
    well within its usage policy). Any failure falls back to the raw
    coordinates, which stay searchable in a maps app.
    """
    if not coords or "," not in coords:
        return coords
    if coords in _PLACE_CACHE:
        return _PLACE_CACHE[coords]
    try:
        lat, lon = (p.strip() for p in coords.split(",", 1))
        resp = httpx.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 16},
            headers={"User-Agent": "tesla-analyzer/0.1"},
            timeout=4.0,
        )
        resp.raise_for_status()
        addr = resp.json().get("address") or {}
        name = (
            addr.get("neighbourhood") or addr.get("suburb") or addr.get("road")
            or addr.get("village") or addr.get("town") or addr.get("city") or ""
        )
        area = addr.get("city") or addr.get("town") or addr.get("county") or ""
        label = f"{name}, {area}" if name and area and name != area else (name or area)
        result = (label or coords)[:120]
    except Exception:  # noqa: BLE001 — never let naming block trip logging
        result = coords
    _PLACE_CACHE[coords] = result
    return result


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


def _process_vehicle(session: Session, data: dict, v_summary: dict, settings) -> tuple:
    """Log drives/charges for one car from its vehicle_data snapshot.

    Snapshot / open-trip / open-charge state is namespaced by VIN, so each car
    on the account advances its own independent session state machine. Returns
    ``(vehicle, snapshot, n_drives, n_charges, open_trip)``.
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

    snap = sync_mod.snapshot_from_vehicle_data(data)
    sk = state.scoped(state.SNAPSHOT_KEY, vin)
    tk = state.scoped(state.OPEN_TRIP_KEY, vin)
    ck = state.scoped(state.OPEN_CHARGE_KEY, vin)
    prev_raw = state.get(session, sk)
    prev = _json.loads(prev_raw) if prev_raw else None
    open_trip = _json.loads(state.get(session, tk) or "null")
    open_charge = _json.loads(state.get(session, ck) or "null")

    drives, charges, open_trip, open_charge = sync_mod.process_snapshot(
        prev, snap, open_trip, open_charge,
        vehicle.battery_capacity_kwh, settings.energy_price_per_kwh,
    )
    for d in drives:
        d["start_location"] = _place(d["start_location"])
        d["end_location"] = _place(d["end_location"])
        session.add(Drive(vehicle_id=vehicle.id, **d))
    for c in charges:
        cap = sync_mod.implied_capacity_kwh(c)
        c.pop("energy_measured", None)  # transient flag, not a DB column
        if cap:
            old = vehicle.battery_capacity_kwh or 75.0
            vehicle.battery_capacity_kwh = round(0.8 * old + 0.2 * cap, 1)
        c["location"] = _place(c.get("location", ""))
        session.add(Charge(vehicle_id=vehicle.id, **c))
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

    session.commit()
    state.put(session, sk, _json.dumps(snap))
    state.put(session, tk, _json.dumps(open_trip) if open_trip else "")
    state.put(session, ck, _json.dumps(open_charge) if open_charge else "")
    return vehicle, snap, len(drives), len(charges), open_trip


def _update_idle_window(session: Session, any_active: bool, wake: bool, now_ts: float) -> None:
    """Schedule/clear the car-sleep quiet window from activity across all cars."""
    IDLE_AFTER_MIN, SUSPEND_MIN = 15, 30
    last_active = float(state.get(session, state.LAST_ACTIVE_KEY) or 0)
    if any_active or not last_active:
        state.put(session, state.LAST_ACTIVE_KEY, str(now_ts))
        if any_active:
            state.put(session, state.SUSPEND_KEY, "")
    elif not wake and now_ts - last_active >= IDLE_AFTER_MIN * 60:
        state.put(session, state.SUSPEND_KEY, str(now_ts + SUSPEND_MIN * 60))


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

    # Sleep window: polling an awake-but-idle car resets its sleep timer and
    # slowly drains the battery. After IDLE_AFTER_MIN of no activity the cron
    # goes quiet for SUSPEND_MIN so the car can nap; the manual Sync button
    # (wake=1) always bypasses this and clears the window.
    now_ts = datetime.now().timestamp()
    if wake:
        state.put(session, state.SUSPEND_KEY, "")
    else:
        suspend_until = float(state.get(session, state.SUSPEND_KEY) or 0)
        if now_ts < suspend_until:
            resp = {
                "status": "sleep-window",
                "logged": {"drives": 0, "charges": 0},
                "note": ("Letting the car sleep — polling resumes in "
                         f"~{int((suspend_until - now_ts) / 60) + 1} min."),
            }
            last_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, active_target))
            if last_raw:
                last = _json.loads(last_raw)
                resp["last"] = {"soc": last.get("soc"), "ts": last.get("ts"),
                                "odo_km": round(last.get("odo_km", 0), 1)}
            return resp

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
    any_active = False
    purged = False
    active_snap = active_open_trip = active_vehicle = None
    active_cfg: dict = {}

    for vv in vehicles:
        vvin = vv.get("vin")
        vid = vv.get("id_s") or vv.get("id")
        vstate = vv.get("state")
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
            continue  # asleep/offline — nothing readable right now

        try:
            data = client.vehicle_data(vid)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 401:
                token = refresh_or_401()
                client = make_client(token)
                try:
                    data = client.vehicle_data(vid)
                except httpx.HTTPStatusError:
                    continue
            elif code == 408:
                continue  # fell asleep between the list and the read
            else:
                raise HTTPException(code, f"Tesla error: {exc}") from exc

        if not purged:
            services.purge_demo(session)  # retire the seeded sample on first real data
            purged = True
        vehicle, snap, nd, nc, open_trip = _process_vehicle(session, data, vv, settings)
        total["drives"] += nd
        total["charges"] += nc
        if snap["charging"] or sync_mod.is_driving(snap) or snap.get("user_present"):
            any_active = True
        if vvin == active_target:
            active_snap, active_open_trip, active_vehicle = snap, open_trip, vehicle
            active_cfg = data.get("vehicle_config") or {}

    state.put(session, state.SOURCE_KEY, "linked")
    _update_idle_window(session, any_active, wake, now_ts)

    # The dashboard's live status reflects the active car specifically.
    if active_snap is None:
        resp = {
            "status": "asleep",
            "tried_wake": wake,
            "logged": total,
            "note": ("Couldn't wake the car — it may be offline. Try again in a minute."
                     if wake else
                     "Car is asleep — try again while charging or right after a drive."),
        }
        last_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, active_target))
        if last_raw:
            last = _json.loads(last_raw)
            resp["last"] = {"soc": last.get("soc"), "ts": last.get("ts"),
                            "odo_km": round(last.get("odo_km", 0), 1)}
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
    return {
        "status": activity,
        "soc": snap["soc"],
        "odo_km": round(snap["odo_km"], 1),
        "speed_kmh": round(snap.get("speed_kmh") or 0.0),
        "trip_in_progress": bool(open_trip),
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
    import csv
    import io
    import json as _json
    import zipfile

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
    name = f"tesla-analyzer-{vehicle.vin[-6:]}-{label}.zip"
    return Response(
        zbuf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


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
    since = None
    window_label = None
    live = None
    if current_drive:
        open_trip = _json.loads(
            state.get(session, state.scoped(state.OPEN_TRIP_KEY, vehicle.vin)) or "null")
        snap_raw = state.get(session, state.scoped(state.SNAPSHOT_KEY, vehicle.vin))
        snap = _json.loads(snap_raw) if snap_raw else None
        if open_trip and snap:
            live = sync_mod.live_trip(open_trip, snap, vehicle.battery_capacity_kwh)
            since = datetime.fromtimestamp(open_trip["ts"], sync_mod.MYT).replace(tzinfo=None)
            window_label = "current drive"
        else:
            last_start = session.scalar(
                select(func.max(Drive.start_time)).where(Drive.vehicle_id == vehicle.id)
            )
            if last_start is not None:
                since = last_start
                window_label = "last drive"
    elif since_charge:
        last_end = session.scalar(
            select(func.max(Charge.end_time)).where(Charge.vehicle_id == vehicle.id)
        )
        if last_end is not None:
            since = last_end
            window_label = "since last charge"
    drives, charges = _window(session, vehicle.id, days, since=since)

    driving = driving_analysis.analyze(
        drives, settings.rated_wh_per_km, vehicle.battery_capacity_kwh)
    charging = charging_analysis.analyze(charges, drives)
    efficiency = efficiency_analysis.analyze(drives, settings.rated_wh_per_km)

    # Battery health uses the full reading history, not the display window.
    readings = session.scalars(
        select(BatteryReading)
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
        [{"soc": r.soc, "range_km": r.range_km} for r in readings],
        new_range_km=spec_km,
    )

    recs = recommendations_engine.build(
        driving,
        charging,
        efficiency,
        battery,
        energy_price=settings.energy_price_per_kwh,
        currency=settings.currency,
    )

    vehicle_out = VehicleOut.model_validate(vehicle).model_dump()
    vehicle_out.update({k: v for k, v in vin_info.items() if v})  # year, plant
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
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "currency": settings.currency,
        "live_trip": live,
        "driving": driving,
        "charging": charging,
        "efficiency": efficiency,
        "battery": battery,
        "recommendations": recs,
    }
