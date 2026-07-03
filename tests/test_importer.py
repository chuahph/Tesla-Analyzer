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


def test_parse_zip_nested_with_macos_junk():
    """Real exports nest files in folders and include __MACOSX / AppleDouble junk."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("TeslaData/2026/drives.csv", DRIVE_CSV)
        zf.writestr("TeslaData/2026/charges.txt", CHARGE_CSV)  # .txt treated as CSV
        zf.writestr("__MACOSX/TeslaData/._drives.csv", b"\x00\x01garbage")
        zf.writestr(".DS_Store", b"\x00\x00")
    drives, charges = parse_upload("MyTeslaData.zip", buf.getvalue())
    assert len(drives) == 2
    assert len(charges) == 2


def test_parse_zip_detects_by_magic_without_extension():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("drives.csv", DRIVE_CSV)
    # No .zip extension, but PK magic bytes should still be recognised.
    drives, charges = parse_upload("export", buf.getvalue())
    assert len(drives) == 2


def test_parse_nested_zip():
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("charges.csv", CHARGE_CSV)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("drives.csv", DRIVE_CSV)
        zf.writestr("inner.zip", inner.getvalue())
    drives, charges = parse_upload("bundle.zip", outer.getvalue())
    assert len(drives) == 2
    assert len(charges) == 2


def test_empty_zip_raises_helpful_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no data here")
    try:
        parse_upload("empty.zip", buf.getvalue())
        assert False, "expected ImportError_"
    except ImportError_ as exc:
        assert "ZIP" in str(exc)


OFFICIAL_TESLA_CHARGING = (
    "VIN,Charge Start Time (UTC),Charge End Time (UTC),Charge Duration (s),"
    "Energy Added (kWh),Charger Type,Location\n"
    "LRW3F7EK3RC309372,2026-06-11 06:59:28,2026-06-11 09:14:39,8111,25.02,General - AC power,Home\n"
    "LRW3F7EK3RC309372,2026-06-14 23:43:47,2026-06-15 07:51:39,29272,52.78,General - AC power,Work\n"
)


def test_official_tesla_charging_export():
    """Tesla's real export uses '(UTC)' suffixes, 'Charger Type', and seconds."""
    drives, charges = parse_upload("Charging_Data.csv", OFFICIAL_TESLA_CHARGING.encode())
    assert drives == []
    assert len(charges) == 2
    assert [c["charge_type"] for c in charges] == ["AC", "AC"]
    assert abs(charges[0]["energy_added_kwh"] - 25.02) < 1e-6
    # 8111 seconds -> ~135.2 minutes
    assert 134 < charges[0]["duration_min"] < 136
    assert charges[0]["location"] == "Home"


def test_manual_log_estimates_energy_from_soc():
    """A hand-kept log has battery % but no kWh — energy comes from the SoC drop."""
    csv = (
        "start_time,end_time,distance_km,start_soc,end_soc,origin,destination\n"
        "2026-07-01 08:00,2026-07-01 08:35,25,80,72,Home,Office\n"
    )
    drives, _ = parse_upload("My_Drives.csv", csv.encode())
    assert len(drives) == 1
    # 8% of a 60 kWh pack = 4.8 kWh -> 192 Wh/km over 25 km
    assert abs(drives[0]["energy_used_kwh"] - 4.8) < 1e-6


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
