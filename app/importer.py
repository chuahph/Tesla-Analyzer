"""Import Tesla privacy/usage data exports into the analyzer.

Tesla's "Download Your Data" export and various community tools produce data in
slightly different shapes, so this importer is deliberately tolerant. It accepts:

  * JSON   — either this app's own export ({"drives": [...], "charges": [...]})
             or a bare list of records.
  * CSV    — one file of drives or one file of charges; columns are matched by a
             set of common aliases (case/space/_ insensitive).
  * ZIP    — a bundle containing any number of the above (e.g. the raw export).

Records are normalised to the Drive/Charge model fields. Unknown columns are
ignored; missing optional fields fall back to sensible defaults.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime
from typing import Any

from dateutil import parser as dateparser

MILES_TO_KM = 1.60934

# Column aliases (normalised to lower-case, no spaces/underscores).
DRIVE_ALIASES = {
    "start_time": ["starttime", "startdate", "begin", "starteddate", "date", "departuretime"],
    "end_time": ["endtime", "enddate", "finish", "endeddate", "arrivaltime"],
    "distance_km": ["distancekm", "distance", "km", "kilometers"],
    "distance_miles": ["distancemiles", "distancemi", "miles", "mi"],
    "duration_min": ["durationmin", "duration", "durationminutes", "minutes"],
    "duration_sec": ["durations", "durationsec", "durationseconds", "drivedurations"],
    "start_soc": ["startsoc", "startbatterylevel", "socstart", "beginsoc"],
    "end_soc": ["endsoc", "endbatterylevel", "socend"],
    "energy_used_kwh": ["energyusedkwh", "energyused", "energy", "kwhused", "consumedkwh"],
    "avg_speed_kmh": ["avgspeedkmh", "avgspeed", "averagespeed", "speed"],
    "max_speed_kmh": ["maxspeedkmh", "maxspeed", "topspeed"],
    "outside_temp_c": ["outsidetempc", "outsidetemp", "temperature", "temp"],
    "start_location": ["startlocation", "origin", "from", "startaddress"],
    "end_location": ["endlocation", "destination", "to", "endaddress"],
}

CHARGE_ALIASES = {
    "start_time": ["starttime", "startdate", "begin", "date", "chargestarttime"],
    "end_time": ["endtime", "enddate", "finish", "chargeendtime"],
    "duration_min": ["durationmin", "duration", "minutes"],
    "duration_sec": ["durations", "durationsec", "durationseconds", "chargedurations"],
    "start_soc": ["startsoc", "startbatterylevel", "socstart"],
    "end_soc": ["endsoc", "endbatterylevel", "socend"],
    "energy_added_kwh": ["energyaddedkwh", "energyadded", "kwhadded", "energy", "addedkwh"],
    "charge_type": ["chargetype", "chargertype", "type", "current", "chargercurrenttype"],
    "max_power_kw": ["maxpowerkw", "maxpower", "power", "chargerpower"],
    "location": ["location", "site", "address", "sitename"],
    "cost": ["cost", "price", "amount", "totalcost"],
    "outside_temp_c": ["outsidetempc", "outsidetemp", "temperature", "temp"],
}


class ImportError_(ValueError):
    """Raised when an upload can't be parsed into any usable records."""


def _norm(name: str) -> str:
    """Lower-case and strip every non-alphanumeric character for loose matching."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _build_index(headers: list[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    """Map our field name -> the actual header present in the file.

    Also registers a copy of each header with a trailing "utc" timezone marker
    stripped, so Tesla's real export columns like "Charge Start Time (UTC)"
    ("chargestarttimeutc") match the "chargestarttime" alias.
    """
    normalised: dict[str, str] = {}
    for h in headers:
        key = _norm(h)
        normalised.setdefault(key, h)
        if key.endswith("utc"):
            normalised.setdefault(key[:-3], h)
    index: dict[str, str] = {}
    for field, names in aliases.items():
        for candidate in [field, *names]:
            if _norm(candidate) in normalised:
                index[field] = normalised[_norm(candidate)]
                break
    return index


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return default


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return dateparser.parse(str(value))
    except (ValueError, OverflowError):
        return None


def _looks_like_charges(headers: list[str]) -> bool:
    idx = _build_index(headers, CHARGE_ALIASES)
    return "energy_added_kwh" in idx or "charge_type" in idx or "max_power_kw" in idx


def _normalise_drive(row: dict[str, Any], index: dict[str, str]) -> dict[str, Any] | None:
    def g(field, default=None):
        col = index.get(field)
        return row.get(col) if col else default

    start = _dt(g("start_time"))
    if start is None:
        return None
    duration = _num(g("duration_min")) or _num(g("duration_sec")) / 60.0
    end = _dt(g("end_time"))
    if end is None and duration:
        from datetime import timedelta

        end = start + timedelta(minutes=duration)
    end = end or start

    distance = _num(g("distance_km"))
    if not distance and index.get("distance_miles"):
        distance = _num(g("distance_miles")) * MILES_TO_KM
    if not duration and end and start:
        duration = max((end - start).total_seconds() / 60.0, 0.0)

    return {
        "start_time": start,
        "end_time": end,
        "distance_km": round(distance, 2),
        "duration_min": round(duration, 1),
        "start_soc": _num(g("start_soc")),
        "end_soc": _num(g("end_soc")),
        "energy_used_kwh": round(_num(g("energy_used_kwh")), 3),
        "avg_speed_kmh": _num(g("avg_speed_kmh")) or (
            round(distance / (duration / 60.0), 1) if duration else 0.0
        ),
        "max_speed_kmh": _num(g("max_speed_kmh")),
        "outside_temp_c": _num(g("outside_temp_c"), 20.0),
        "start_location": str(g("start_location", "") or ""),
        "end_location": str(g("end_location", "") or ""),
    }


def _normalise_charge(row: dict[str, Any], index: dict[str, str]) -> dict[str, Any] | None:
    def g(field, default=None):
        col = index.get(field)
        return row.get(col) if col else default

    start = _dt(g("start_time"))
    if start is None:
        return None
    duration = _num(g("duration_min")) or _num(g("duration_sec")) / 60.0
    end = _dt(g("end_time"))
    if end is None and duration:
        from datetime import timedelta

        end = start + timedelta(minutes=duration)
    end = end or start
    if not duration and end and start:
        duration = max((end - start).total_seconds() / 60.0, 0.0)

    ctype_raw = str(g("charge_type", "") or "").upper()
    power = _num(g("max_power_kw"))
    if "DC" in ctype_raw or "SUPERCHARG" in ctype_raw or "FAST" in ctype_raw:
        ctype = "DC"
    elif "AC" in ctype_raw or power and power <= 22:
        ctype = "AC"
    else:
        ctype = "DC" if power > 22 else "AC"

    return {
        "start_time": start,
        "end_time": end,
        "duration_min": round(duration, 1),
        "start_soc": _num(g("start_soc")),
        "end_soc": _num(g("end_soc")),
        "energy_added_kwh": round(_num(g("energy_added_kwh")), 3),
        "charge_type": ctype,
        "max_power_kw": power,
        "location": str(g("location", "") or ""),
        "cost": _num(g("cost")),
        "outside_temp_c": _num(g("outside_temp_c"), 20.0),
    }


def _parse_csv(text: str) -> tuple[list[dict], list[dict]]:
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    rows = list(reader)
    if not headers:
        return [], []
    if _looks_like_charges(headers):
        index = _build_index(headers, CHARGE_ALIASES)
        charges = [c for r in rows if (c := _normalise_charge(r, index))]
        return [], charges
    index = _build_index(headers, DRIVE_ALIASES)
    drives = [d for r in rows if (d := _normalise_drive(r, index))]
    return drives, []


def _parse_json(text: str) -> tuple[list[dict], list[dict]]:
    data = json.loads(text)
    drives_raw: list[dict] = []
    charges_raw: list[dict] = []
    if isinstance(data, dict):
        drives_raw = data.get("drives", []) or []
        charges_raw = data.get("charges", []) or []
        if not drives_raw and not charges_raw and "records" in data:
            data = data["records"]
    if isinstance(data, list):
        # Bare list — classify each record by its keys.
        for rec in data:
            if not isinstance(rec, dict):
                continue
            if _looks_like_charges(list(rec.keys())):
                charges_raw.append(rec)
            else:
                drives_raw.append(rec)

    drives = []
    for r in drives_raw:
        idx = _build_index(list(r.keys()), DRIVE_ALIASES)
        d = _normalise_drive(r, idx)
        if d:
            drives.append(d)
    charges = []
    for r in charges_raw:
        idx = _build_index(list(r.keys()), CHARGE_ALIASES)
        c = _normalise_charge(r, idx)
        if c:
            charges.append(c)
    return drives, charges


def _is_junk(path: str) -> bool:
    """Skip macOS bundle metadata, AppleDouble files and hidden entries."""
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    return (
        path.startswith("__MACOSX")
        or "__MACOSX" in parts
        or base.startswith("._")
        or base.startswith(".")
        or base in {"Thumbs.db", "desktop.ini"}
    )


def _parse_zip(content: bytes, _depth: int = 0) -> tuple[list[dict], list[dict], list[str]]:
    """Parse every recognised data file inside a ZIP (recursing into nested zips).

    Returns (drives, charges, data_filenames_seen). Real Tesla exports nest files
    in folders and ship macOS junk, so this is deliberately forgiving: it tries
    CSV for .csv/.tsv/.txt and JSON for .json, and a failure in one inner file
    never aborts the whole import.
    """
    drives: list[dict] = []
    charges: list[dict] = []
    seen: list[str] = []

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir() or _is_junk(info.filename):
                continue
            inner = info.filename.lower()
            try:
                raw = zf.read(info)
            except Exception:  # noqa: BLE001 - skip unreadable entries
                continue

            if inner.endswith(".zip") and _depth < 3:
                d, c, s = _parse_zip(raw, _depth + 1)
                drives += d
                charges += c
                seen += s
                continue

            try:
                if inner.endswith((".csv", ".tsv", ".txt")):
                    d, c = _parse_csv(raw.decode("utf-8-sig", errors="replace"))
                elif inner.endswith(".json"):
                    d, c = _parse_json(raw.decode("utf-8-sig", errors="replace"))
                else:
                    continue
            except Exception:  # noqa: BLE001 - ignore an unparsable inner file
                continue

            if d or c:
                seen.append(info.filename)
            drives += d
            charges += c

    return drives, charges, seen


def parse_upload(filename: str, content: bytes) -> tuple[list[dict], list[dict]]:
    """Parse an uploaded export into (drives, charges) normalised dicts.

    Accepts a single CSV/JSON file or a ZIP bundle (e.g. Tesla's *Download Your
    Data* export), including nested folders and zips.
    """
    name = (filename or "").lower()
    drives: list[dict] = []
    charges: list[dict] = []
    zip_seen: list[str] = []

    if name.endswith(".zip") or content[:2] == b"PK":
        drives, charges, zip_seen = _parse_zip(content)
    elif name.endswith(".json") or content[:1] in (b"{", b"["):
        drives, charges = _parse_json(content.decode("utf-8-sig", errors="replace"))
    else:  # assume CSV
        drives, charges = _parse_csv(content.decode("utf-8-sig", errors="replace"))

    if not drives and not charges:
        if name.endswith(".zip") or content[:2] == b"PK":
            raise ImportError_(
                "No drive or charge records were found inside the ZIP. Make sure it "
                "contains CSV/JSON files with columns like start_time, distance, "
                "energy and soc (drives) or energy_added, charge_type and power "
                "(charges)."
            )
        raise ImportError_(
            "No drive or charge records found. Expected CSV/JSON/ZIP with columns "
            "like start_time, distance, energy, soc (drives) or energy_added, "
            "charge_type, power (charges)."
        )
    return drives, charges
