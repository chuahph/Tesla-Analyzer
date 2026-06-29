"""REST API endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analysis import charging as charging_analysis
from ..analysis import driving as driving_analysis
from ..analysis import efficiency as efficiency_analysis
from ..analysis import recommendations as recommendations_engine
from ..config import get_settings
from ..database import get_session
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
def health():
    settings = get_settings()
    return {"status": "ok", "mode": "demo" if settings.demo_mode else "live"}


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
