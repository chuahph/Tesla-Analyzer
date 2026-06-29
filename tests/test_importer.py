"""Tests for the Tesla data-export importer and the import service."""
import io
import json
import zipfile

from sqlalchemy import select

from app.importer import parse_upload, ImportError_
from app.models import Charge, Drive
from app.services import replace_with_import


DRIVE_CSV = (
    "Start Time,End Time,Distance (miles),Start SOC,End SOC,Energy Used,Avg Speed,Outside Temp,Origin,Destination\n"
    "2026-01-02 08:00,2026-01-02 08:30,12.4,80,72,4.1,55,3,Home,Office\n"
    "2026-01-02 17:30,2026-01-02 18:10,12.4,70,61,4.4,48,5,Office,Home\n"
)

CHARGE_CSV = (
    "start_time,end_time,energy_added,charge_type,max_power,start_soc,end_soc,location,cost\n"
    "2026-01-02 22:00,2026-01-03 02:00,38.0,AC,11,61,90,Home,11.40\n"
    "2026-01-05 14:00,2026-01-05 14:25,32.0,DC,150,20,70,Supercharger,14.40\n"
)


def test_parse_drive_csv_miles_converted():
    drives, charges = parse_upload("drives.csv", DRIVE_CSV.encode())
    assert len(drives) == 2
    assert charges == []
    # 12.4 miles -> ~19.96 km
    assert 19.5 < drives[0]["distance_km"] < 20.5
    assert drives[0]["start_location"] == "Home"
    assert drives[0]["avg_speed_kmh"] > 0


def test_parse_charge_csv_types():
    drives, charges = parse_upload("charges.csv", CHARGE_CSV.encode())
    assert drives == []
    assert [c["charge_type"] for c in charges] == ["AC", "DC"]
    assert charges[0]["energy_added_kwh"] == 38.0
    assert charges[0]["cost"] == 11.40


def test_parse_json_own_format():
    payload = {
        "drives": [{"start_time": "2026-02-01T09:00", "distance_km": 30,
                    "duration_min": 30, "start_soc": 80, "end_soc": 72,
                    "energy_used_kwh": 5.0}],
        "charges": [{"start_time": "2026-02-01T22:00", "energy_added_kwh": 20,
                     "charge_type": "AC", "max_power_kw": 11, "end_soc": 90}],
    }
    drives, charges = parse_upload("export.json", json.dumps(payload).encode())
    assert len(drives) == 1 and len(charges) == 1
    assert drives[0]["distance_km"] == 30


def test_parse_zip_bundle():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("drives.csv", DRIVE_CSV)
        zf.writestr("charges.csv", CHARGE_CSV)
    drives, charges = parse_upload("export.zip", buf.getvalue())
    assert len(drives) == 2
    assert len(charges) == 2


def test_parse_garbage_raises():
    try:
        parse_upload("notes.txt", b"hello world, nothing useful here")
        assert False, "expected ImportError_"
    except ImportError_:
        pass


def test_replace_with_import_persists(session):
    drives, charges = parse_upload("drives.csv", DRIVE_CSV.encode())
    _, ch = parse_upload("charges.csv", CHARGE_CSV.encode())
    result = replace_with_import(session, drives, ch, name="My Export")
    assert result["imported_drives"] == 2
    assert result["imported_charges"] == 2
    assert session.scalar(select(Drive).limit(1)) is not None
    # Cost was present, so it should be preserved (not re-estimated).
    stored = session.scalars(select(Charge)).all()
    assert any(abs(c.cost - 11.40) < 0.01 for c in stored)


def test_replace_estimates_missing_cost(session):
    drives, charges = parse_upload(
        "c.csv",
        b"start_time,energy_added,charge_type,max_power,end_soc\n"
        b"2026-03-01 22:00,40,AC,11,90\n",
    )
    result = replace_with_import(session, drives, charges)
    assert result["imported_charges"] == 1
    c = session.scalars(select(Charge)).first()
    assert c.cost > 0  # estimated from energy * price
