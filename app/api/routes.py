"""REST API endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
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
    vehicle = session.scalars(select(Vehicle).order_by(Vehicle.id)).first()
    if vehicle is None:
        raise HTTPException(404, "No vehicle data. Run the collector or seed demo data.")
    return vehicle


def _window(session: Session, vehicle_id: int, days: int):
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


@router.get("/health")
def health(session: Session = Depends(get_session)):
    source = state.data_source(session)
    mode = "live" if state.is_live(session) else ("imported" if source == "imported" else "demo")
    return {
        "status": "ok",
        "mode": mode,
        "source": source,
        "oauth_available": auth.oauth_configured(),
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


@router.get("/link/oauth/start")
def oauth_start():
    """Button 2 (OAuth flow) — redirect to Tesla's sign-in page."""
    if not auth.oauth_configured():
        raise HTTPException(
            400,
            "Tesla OAuth is not configured. Set TESLA_CLIENT_ID / TESLA_CLIENT_SECRET, "
            "or use the access-token option instead.",
        )
    url, _state = auth.authorize_url()
    return RedirectResponse(url)


@router.get("/link/oauth/callback")
def oauth_callback(
    code: str | None = None, error: str | None = None,
    session: Session = Depends(get_session),
):
    if error:
        raise HTTPException(400, f"Tesla sign-in failed: {error}")
    if not code:
        raise HTTPException(400, "Missing authorization code.")
    try:
        tokens = auth.exchange_code(code)
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
def summary(days: int = Query(90, ge=1, le=730), session: Session = Depends(get_session)):
    """The single endpoint the dashboard consumes: full analysis + recommendations."""
    settings = get_settings()
    vehicle = _first_vehicle(session)
    drives, charges = _window(session, vehicle.id, days)

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
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "currency": settings.currency,
        "driving": driving,
        "charging": charging,
        "efficiency": efficiency,
        "recommendations": recs,
    }
