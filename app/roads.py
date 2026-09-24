"""Which kind of road a coordinate is on: expressway/trunk ("highway") or not.

Speed alone decides highway vs city everywhere else in this app, which gets two
real cases wrong: a jammed expressway (the Penang Bridge at peak hour) reads as
city, and a fast trunk road reads as highway. This answers the question from the
map instead, so speed and steadiness are left to describe the TRAFFIC.

The road network comes from OpenStreetMap, fetched by the server itself from the
Overpass API — once, into a local cache file — and never from the database: it
is a few MB of geometry that the Supabase egress budget has no business paying
for on every restart. Until it has loaded, road_class() answers None and every
caller falls back to the speed rule, so a failed or slow download costs accuracy
and nothing else.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any

# OSM highway=* values counted as "highway". Malaysia tags most tolled
# expressways motorway and many of the rest (Lim Chong Eu, Jelutong, federal
# expressways) trunk; trunk also covers some federal roads with traffic lights,
# which the traffic axis — not this one — is there to describe.
ROAD_CLASSES = tuple(
    c.strip() for c in os.environ.get("ROAD_CLASSES", "motorway,trunk").split(",")
    if c.strip())
# How close a position has to be to one of those roads to be ON it. GPS is good
# to 5-10 m on an open road; the carriageway itself is 10-20 m wide either side
# of the centreline OSM draws. A service road running alongside can fall inside
# this — which is why a trip is judged by the share of its kilometres, not by
# any single sample.
MATCH_M = 25.0
# Grid cell for the spatial index, in degrees (~1.1 km at this latitude).
_CELL = 0.01
# Peninsular Malaysia plus Singapore. South, west, north, east.
DEFAULT_BBOX = (1.15, 99.6, 6.75, 104.65)
# Douglas-Peucker tolerance when simplifying fetched geometry, in metres: well
# inside MATCH_M, and it cuts the node count several-fold.
_SIMPLIFY_M = 8.0
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
CACHE_PATH = os.environ.get(
    "ROAD_CACHE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "roads_cache.json"))
# How long to wait after a failed download before trying again.
_RETRY_SEC = 6 * 3600.0

_lock = threading.Lock()
_grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] | None = None
_meta: dict[str, Any] = {"state": "not loaded"}
_last_attempt = 0.0


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Local flat-earth offset of point 2 from point 1, in metres (x east, y north)."""
    k = 111_320.0
    return (lon2 - lon1) * k * math.cos(math.radians((lat1 + lat2) / 2.0)), (lat2 - lat1) * k


def _seg_dist_m(lat: float, lon: float, seg: tuple[float, float, float, float]) -> float:
    a_lat, a_lon, b_lat, b_lon = seg
    bx, by = _metres(a_lat, a_lon, b_lat, b_lon)
    px, py = _metres(a_lat, a_lon, lat, lon)
    length2 = bx * bx + by * by
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / length2))
    dx, dy = px - t * bx, py - t * by
    return math.hypot(dx, dy)


def _simplify(points: list[tuple[float, float]], tol_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker, iterative so a long way cannot hit the recursion limit."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        seg = (*points[i], *points[j])
        worst, idx = 0.0, -1
        for k in range(i + 1, j):
            d = _seg_dist_m(points[k][0], points[k][1], seg)
            if d > worst:
                worst, idx = d, k
        if worst > tol_m and idx > 0:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(points, keep) if k]


def _build(lines: list[list[list[float]]]) -> dict:
    """Grid index: each segment filed under every cell its padded bbox touches,
    so a lookup only ever reads the one cell its point is in."""
    pad = MATCH_M / 111_320.0 * 1.5
    grid: dict[tuple[int, int], list] = {}
    for line in lines:
        for (a_lat, a_lon), (b_lat, b_lon) in zip(line, line[1:]):
            seg = (a_lat, a_lon, b_lat, b_lon)
            lo_lat, hi_lat = min(a_lat, b_lat) - pad, max(a_lat, b_lat) + pad
            lo_lon, hi_lon = min(a_lon, b_lon) - pad, max(a_lon, b_lon) + pad
            for i in range(int(math.floor(lo_lat / _CELL)), int(math.floor(hi_lat / _CELL)) + 1):
                for j in range(int(math.floor(lo_lon / _CELL)), int(math.floor(hi_lon / _CELL)) + 1):
                    grid.setdefault((i, j), []).append(seg)
    return grid


def set_network(lines: list[list[list[float]]], source: str = "given") -> None:
    """Install a road network: a list of polylines of [lat, lon] points."""
    global _grid
    grid = _build(lines)
    with _lock:
        _grid = grid
        _meta.update(state="loaded", source=source, lines=len(lines),
                     segments=sum(max(len(line) - 1, 0) for line in lines),
                     classes=list(ROAD_CLASSES))


def clear() -> None:
    global _grid
    with _lock:
        _grid = None
        _meta.clear()
        _meta["state"] = "not loaded"


def status() -> dict[str, Any]:
    return dict(_meta)


def road_class(lat: Any, lon: Any) -> str | None:
    """"highway" or "city" for this position; None when unknown — no network
    loaded yet, or no position — so the caller falls back to the speed rule."""
    grid = _grid
    if grid is None or lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    for seg in grid.get((int(math.floor(lat / _CELL)), int(math.floor(lon / _CELL))), ()):
        if _seg_dist_m(lat, lon, seg) <= MATCH_M:
            return "highway"
    return "city"


def _query(bbox: tuple[float, float, float, float]) -> str:
    classes = "|".join(ROAD_CLASSES)
    s, w, n, e = bbox
    return (f'[out:json][timeout:240];way["highway"~"^({classes})$"]'
            f"({s},{w},{n},{e});out geom;")


def _fetch(bbox: tuple[float, float, float, float]) -> list[list[list[float]]]:
    import httpx

    last: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            r = httpx.post(url, data={"data": _query(bbox)}, timeout=300.0,
                           headers={"User-Agent": "ev-drive-analyzer (road type lookup)"})
            r.raise_for_status()
            lines = []
            for el in r.json().get("elements", []):
                pts = [(round(p["lat"], 5), round(p["lon"], 5))
                       for p in el.get("geometry") or [] if "lat" in p and "lon" in p]
                pts = _simplify(pts, _SIMPLIFY_M)
                if len(pts) >= 2:
                    lines.append([list(p) for p in pts])
            return lines
        except Exception as exc:  # noqa: BLE001 — try the next mirror
            last = exc
    raise RuntimeError(f"no Overpass mirror answered: {last}")


def load_cached() -> bool:
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        if data.get("classes") != list(ROAD_CLASSES):
            return False
        set_network(data["lines"], source=f"cache {data.get('fetched_at', '?')}")
        return True
    except (OSError, ValueError, KeyError):
        return False


def ensure_loaded(bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
                  background: bool = True) -> None:
    """Load the network if it is not already: from the cache file, else by
    downloading it — in a background thread by default, so the request that
    asked does not wait minutes for Overpass."""
    global _last_attempt
    if _grid is not None or load_cached():
        return
    now = time.time()
    with _lock:
        if _meta.get("state") == "downloading" or now - _last_attempt < _RETRY_SEC:
            return
        _last_attempt = now
        _meta.update(state="downloading", started=now)

    def run() -> None:
        try:
            lines = _fetch(bbox)
            try:
                os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
                with open(CACHE_PATH, "w") as f:
                    json.dump({"classes": list(ROAD_CLASSES), "bbox": list(bbox),
                               "fetched_at": time.strftime("%Y-%m-%d"),
                               "lines": lines}, f, separators=(",", ":"))
            except OSError:
                pass  # memory-only this run; the next start fetches again
            set_network(lines, source="overpass")
        except Exception as exc:  # noqa: BLE001 — speed rule stays in force
            with _lock:
                _meta.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:300])

    if background:
        threading.Thread(target=run, name="road-network", daemon=True).start()
    else:
        run()
