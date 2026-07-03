"""REST API endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import auth, services, state
from ..analysis import charging as charging_analysis
from ..analysis import driving as driving_analysis
from ..analysis import efficiency as efficiency_analysis
from ..analysis import recommendations as recommendations_engine
from ..config import get_settings
from ..database import get_session
from ..importer import ImportError_, parse_upload
from ..models import Charge, Drive, Vehicle
from ..schemas import ChargeOut, DriveOut, VehicleOut

router = APIRouter(prefix="/api", tags=["analytics"])


def _first_vehicle(session: Session) -> Vehicle:
    # An account-linked vehicle takes precedence over demo/imported rows.
    linked_vin = state.get(session, state.LINKED_VIN_KEY)
    if linked_vin:
        vehicle = session.scalars(
            select(Vehicle).where(Vehicle.vin == linked_vin)
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


@router.get("/sync")  # GET so external cron/uptime services can trigger it
@router.post("/sync")
def sync_now(session: Session = Depends(get_session)):
    """Snapshot the linked car and log what happened since the last snapshot."""
    import json as _json

    from .. import sync as sync_mod
    from ..tesla_client import TeslaClient

    settings = get_settings()
    token = state.active_token(session)
    if not token:
        raise HTTPException(400, "No linked Tesla account — link your account first.")
    base = state.active_base_url(session)

    def fetch(tok: str):
        client = TeslaClient(access_token=tok, base_url=base)
        vehicles = client.list_vehicles()
        if not vehicles:
            raise HTTPException(404, "No vehicles on this Tesla account.")
        v = vehicles[0]
        if v.get("state") and v["state"] != "online":
            return v, None  # asleep/offline — nothing readable right now
        return v, client.vehicle_data(v.get("id_s") or v.get("id"))

    try:
        v, data = fetch(token)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 401:
            refresh = state.get(session, state.REFRESH_KEY)
            if not refresh or not auth.oauth_configured():
                raise HTTPException(401, "Token expired — sign in with Tesla again.") from exc
            tokens = auth.refresh_tokens(refresh)
            state.put(session, state.TOKEN_KEY, tokens["access_token"])
            if tokens.get("refresh_token"):
                state.put(session, state.REFRESH_KEY, tokens["refresh_token"])
            v, data = fetch(tokens["access_token"])
        elif code == 408:
            v, data = None, None  # vehicle asleep
        else:
            raise HTTPException(code, f"Tesla error: {exc}") from exc

    if data is None:
        return {
            "status": "asleep",
            "logged": {"drives": 0, "charges": 0},
            "note": "Car is asleep — try again while charging or right after a drive.",
        }

    vin = data.get("vin") or v.get("vin") or "LINKED-UNKNOWN"
    # Retire the seeded demo data and pin the dashboard to the real car.
    services.purge_demo(session)
    state.put(session, state.LINKED_VIN_KEY, vin)
    vehicle = session.query(Vehicle).filter(Vehicle.vin == vin).first()
    if vehicle is None:
        vehicle = Vehicle(vin=vin, name=data.get("display_name") or "My Tesla", model="Tesla")
        session.add(vehicle)
        session.flush()

    snap = sync_mod.snapshot_from_vehicle_data(data)
    prev_raw = state.get(session, state.SNAPSHOT_KEY)
    prev = _json.loads(prev_raw) if prev_raw else None
    open_trip = _json.loads(state.get(session, state.OPEN_TRIP_KEY) or "null")
    open_charge = _json.loads(state.get(session, state.OPEN_CHARGE_KEY) or "null")

    drives, charges, open_trip, open_charge = sync_mod.process_snapshot(
        prev, snap, open_trip, open_charge,
        vehicle.battery_capacity_kwh, settings.energy_price_per_kwh,
    )
    for d in drives:
        session.add(Drive(vehicle_id=vehicle.id, **d))
    for c in charges:
        session.add(Charge(vehicle_id=vehicle.id, **c))
    session.commit()
    state.put(session, state.SNAPSHOT_KEY, _json.dumps(snap))
    state.put(session, state.OPEN_TRIP_KEY, _json.dumps(open_trip) if open_trip else "")
    state.put(session, state.OPEN_CHARGE_KEY, _json.dumps(open_charge) if open_charge else "")
    state.put(session, state.SOURCE_KEY, "linked")

    activity = "charging" if snap["charging"] else (
        "driving" if snap["shift"] not in ("P", None, "") else "parked"
    )
    return {
        "status": activity,
        "soc": snap["soc"],
        "odo_km": round(snap["odo_km"], 1),
        "trip_in_progress": bool(open_trip),
        "logged": {"drives": len(drives), "charges": len(charges)},
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


@router.get("/summary")
def summary(
    days: int = Query(90, ge=1, le=730),
    since_charge: bool = Query(False),
    session: Session = Depends(get_session),
):
    """The single endpoint the dashboard consumes: full analysis + recommendations.

    With ``since_charge`` the window starts when the most recent charging
    session ended — i.e. the car's "since last charge" view.
    """
    settings = get_settings()
    vehicle = _first_vehicle(session)
    since = None
    window_label = None
    if since_charge:
        last_end = session.scalar(
            select(func.max(Charge.end_time)).where(Charge.vehicle_id == vehicle.id)
        )
        if last_end is not None:
            since = last_end
            window_label = "since last charge"
    drives, charges = _window(session, vehicle.id, days, since=since)

    driving = driving_analysis.analyze(drives)
    charging = charging_analysis.analyze(charges)
    efficiency = efficiency_analysis.analyze(drives, settings.rated_wh_per_km)
    recs = recommendations_engine.build(
        driving,
        charging,
        efficiency,
        energy_price=settings.energy_price_per_kwh,
        currency=settings.currency,
    )

    return {
        "vehicle": VehicleOut.model_validate(vehicle).model_dump(),
        "window_days": days,
        "window_label": window_label,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "currency": settings.currency,
        "driving": driving,
        "charging": charging,
        "efficiency": efficiency,
        "recommendations": recs,
    }
