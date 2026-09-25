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
# The network built ahead of time (scripts/build_road_network.py, run by the
# "Build road network" GitHub Action) and shipped inside the image, so a
# restart — which on Render's free plan wipes CACHE_PATH — never has to
# download it again. Gzipped JSON in the same shape as the cache file.
BUNDLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "road_network.json.gz")
CACHE_PATH = os.environ.get(
    "ROAD_CACHE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "roads_cache.json"))
# How long to wait after a download that left tiles missing before trying the
# missing ones again. Short, because every trip driven meanwhile falls back to
# the speed rule; the sync tick is what calls back in.
_RETRY_SEC = 30 * 60.0
# The network is fetched in tiles of this many degrees (~110 km), one Overpass
# request each: a single query for the whole peninsula is exactly the kind
# the public servers time out on when busy, and one timeout then cost the
# whole download. Tiles that answered are kept; only the rest are retried.
TILE_DEG = 1.0
# A trunk road within this distance of a traffic light is a city arterial
# there, not an expressway. Wide enough to cover the approach queue and the
# short stretches between closely spaced junctions; an expressway's rare
# signalled junction costs it only this much either side.
SIGNAL_CLEAR_M = float(os.environ.get("ROAD_SIGNAL_CLEAR_M", "400"))
# Bumped whenever what counts as a highway changes, so a network saved under
# the old rule is replaced rather than trusted (see _read_tiles).
NETWORK_RULE = f"signal-clear-{SIGNAL_CLEAR_M:g}m"
# Pause between tile requests — the Overpass servers are a shared, free
# service and ask for sequential, unhurried clients.
_TILE_PAUSE_SEC = 1.0

_lock = threading.Lock()
_grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] | None = None
_meta: dict[str, Any] = {"state": "not loaded"}
_last_attempt = 0.0
# Tiles fetched so far, "lat,lon" of their south-west corner -> their lines.
# None when the network was installed whole (set_network): every point is
# then covered.
_tiles: dict[str, list] | None = None
_cache_tried = False


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
    """Install a whole road network: a list of polylines of [lat, lon] points.
    Every point counts as covered."""
    global _grid, _tiles
    grid = _build(lines)
    with _lock:
        _grid, _tiles = grid, None
        _meta.update(state="loaded", source=source, lines=len(lines),
                     segments=sum(max(len(line) - 1, 0) for line in lines),
                     classes=list(ROAD_CLASSES))
        _meta.pop("error", None)


def _install_tiles(tiles: dict[str, list], total: int, source: str) -> None:
    global _grid, _tiles
    lines = [line for tile_lines in tiles.values() for line in tile_lines]
    grid = _build(lines)
    with _lock:
        _grid, _tiles = grid, dict(tiles)
        _meta.update(state="loaded" if len(tiles) >= total else "partial",
                     source=source, tiles=f"{len(tiles)}/{total}", lines=len(lines),
                     segments=sum(max(len(line) - 1, 0) for line in lines),
                     classes=list(ROAD_CLASSES))


def clear() -> None:
    global _grid, _tiles, _cache_tried
    with _lock:
        _grid, _tiles, _cache_tried = None, None, False
        _meta.clear()
        _meta["state"] = "not loaded"


def _tile_of(lat: float, lon: float) -> str:
    return f"{math.floor(lat / TILE_DEG) * TILE_DEG:g},{math.floor(lon / TILE_DEG) * TILE_DEG:g}"


def _tiles_for(bbox: tuple[float, float, float, float]) -> dict[str, tuple]:
    """{tile key: its bbox, clipped to ``bbox``} for every tile ``bbox`` touches."""
    s, w, n, e = bbox
    out = {}
    lat = math.floor(s / TILE_DEG) * TILE_DEG
    while lat < n:
        lon = math.floor(w / TILE_DEG) * TILE_DEG
        while lon < e:
            out[_tile_of(lat, lon)] = (max(lat, s), max(lon, w),
                                       min(lat + TILE_DEG, n), min(lon + TILE_DEG, e))
            lon += TILE_DEG
        lat += TILE_DEG
    return out


# Stretches the owner has marked as city whatever the map says — a trunk road
# through town whose traffic lights OpenStreetMap never mapped reads exactly
# like an expressway, and no rule applied to the map can tell them apart.
# Checked before the network: a position within CITY_OVERRIDE_M of one is
# city. Held here, loaded from the database by the app (routes
# _load_city_overrides), so this module stays DB-free.
CITY_OVERRIDE_M = 35.0
# Shipped starting set: the Jelutong–Bayan Lepas arterials (Jalan Sultan Azlan
# Shah and the parallel road past Home) that trips 3126 and 3127 crawled along
# and the map called expressway. Seeded into the database once; after that the
# stored list is the owner's to edit.
SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "city_overrides_seed.json")
_city_grid: dict[tuple[int, int], list] | None = None
_city_count = 0


def set_city_overrides(items: list[dict]) -> None:
    """Install the owner's city stretches: ``[{"lines": [[[lat, lon], ...]]}]``."""
    global _city_grid, _city_count
    lines = [line for it in items for line in (it.get("lines") or [])
             if isinstance(line, list) and len(line) >= 2]
    grid = _build(lines) if lines else None
    with _lock:
        _city_grid, _city_count = grid, len(items)


def seed_city_lines() -> list:
    try:
        with open(SEED_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def network_lines_near(lat: float, lon: float, radius_m: float) -> list:
    """The loaded expressway segments within ``radius_m`` of a point, as
    two-point lines — what "mark the road here as city" captures."""
    grid = _grid
    if grid is None:
        return []
    reach = int(radius_m / 1100.0) + 2
    ci, cj = int(math.floor(lat / _CELL)), int(math.floor(lon / _CELL))
    seen, out = set(), []
    for i in range(ci - reach, ci + reach + 1):
        for j in range(cj - reach, cj + reach + 1):
            for seg in grid.get((i, j), ()):
                if seg in seen:
                    continue
                seen.add(seg)
                if _seg_dist_m(lat, lon, seg) <= radius_m:
                    out.append([[seg[0], seg[1]], [seg[2], seg[3]]])
    return out


def status() -> dict[str, Any]:
    return {**_meta, "city_overrides": _city_count}


def road_class(lat: Any, lon: Any) -> str | None:
    """"highway" or "city" for this position; None when unknown — no network
    loaded yet, or no position — so the caller falls back to the speed rule."""
    grid, tiles = _grid, _tiles
    if grid is None or lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    if tiles is not None and _tile_of(lat, lon) not in tiles:
        # This tile has not downloaded yet: unknown, never "city" — a road
        # the map has not got must not read as the absence of one.
        return None
    cell = (int(math.floor(lat / _CELL)), int(math.floor(lon / _CELL)))
    city = _city_grid
    if city is not None:
        for seg in city.get(cell, ()):
            if _seg_dist_m(lat, lon, seg) <= CITY_OVERRIDE_M:
                return "city"
    for seg in grid.get(cell, ()):
        if _seg_dist_m(lat, lon, seg) <= MATCH_M:
            return "highway"
    return "city"


def _query(bbox: tuple[float, float, float, float]) -> str:
    """The roads, their node ids (to find which carry traffic lights), and
    the traffic lights on them."""
    classes = "|".join(ROAD_CLASSES)
    s, w, n, e = bbox
    return (f'[out:json][timeout:90];way["highway"~"^({classes})$"]'
            f"({s},{w},{n},{e})->.r;"
            'node(w.r)["highway"="traffic_signals"]->.s;'
            ".r out body geom;.s out skel qt;")


def _clear_of_signals(pts: list[tuple[float, float]], signals: list[tuple[float, float]],
                      radius_m: float) -> list[list[tuple[float, float]]]:
    """The runs of ``pts`` farther than ``radius_m`` from every signal — a
    trunk road split around its traffic lights."""
    if not signals:
        return [pts]
    runs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    for lat, lon in pts:
        near = any(math.hypot(*_metres(lat, lon, s_lat, s_lon)) <= radius_m
                   for s_lat, s_lon in signals
                   if abs(s_lat - lat) < 0.01 and abs(s_lon - lon) < 0.01)
        if near:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
        else:
            cur.append((lat, lon))
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def _fetch(bbox: tuple[float, float, float, float]) -> list[list[list[float]]]:
    import httpx

    last: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            r = httpx.post(url, data={"data": _query(bbox)}, timeout=120.0,
                           headers={"User-Agent": "ev-drive-analyzer (road type lookup)"})
            r.raise_for_status()
            elements = r.json().get("elements", [])
            signals = [(el["lat"], el["lon"]) for el in elements
                       if el.get("type") == "node" and "lat" in el and "lon" in el]
            lines = []
            for el in elements:
                if el.get("type") != "way":
                    continue
                pts = [(round(p["lat"], 5), round(p["lon"], 5))
                       for p in el.get("geometry") or [] if "lat" in p and "lon" in p]
                # Trunk is not only expressways. OSM also tags city arterials
                # with traffic lights trunk — Jalan Sultan Azlan Shah, which
                # made a signal-to-signal crawl through Bayan Lepas read as a
                # jammed expressway. Expressways do not have traffic lights,
                # so a trunk road counts only where it is clear of them.
                # Motorways are taken whole.
                runs = ([pts] if (el.get("tags") or {}).get("highway") != "trunk"
                        else _clear_of_signals(pts, signals, SIGNAL_CLEAR_M))
                for run in runs:
                    run = _simplify(run, _SIMPLIFY_M)
                    if len(run) >= 2:
                        lines.append([list(p) for p in run])
            return lines
        except Exception as exc:  # noqa: BLE001 — try the next mirror
            last = exc
    raise RuntimeError(f"no Overpass mirror answered: {last}")


def _read_tiles(path: str, want_rule: bool = False):
    """Tiles from a saved network (plain or gzipped JSON), or None — with the
    rule it was built under when ``want_rule`` (None for a network saved
    before rules were recorded)."""
    import gzip

    try:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt") as f:
            data = json.load(f)
    except (OSError, ValueError, EOFError):
        return (None, None) if want_rule else None
    if data.get("classes") != list(ROAD_CLASSES) or not isinstance(data.get("tiles"), dict):
        return (None, None) if want_rule else None
    return (data["tiles"], data.get("rule")) if want_rule else data["tiles"]


def _load_cache_file() -> dict[str, list] | None:
    """Tiles from the bundled network and the cache file together, or None.
    The older whole-network cache form (one "lines" list) reads as a single
    tile covering everything.

    A network built under an older NETWORK_RULE is still used — a slightly
    wrong map beats none, and rebuilding the bundle takes a GitHub Action run
    — but never mixed with one built under the current rule: tiles saved
    under the old rule are dropped once the bundle carries the new one."""
    bundle, bundle_rule = _read_tiles(BUNDLE_PATH, want_rule=True)
    cache, cache_rule = _read_tiles(CACHE_PATH, want_rule=True)
    tiles = dict(bundle or {})
    if cache and (cache_rule == bundle_rule or bundle_rule != NETWORK_RULE):
        tiles.update(cache)
    _meta["rule"] = (NETWORK_RULE if (bundle_rule if bundle else cache_rule) == NETWORK_RULE
                     else "older rule — rebuild the bundle")
    if tiles:
        return tiles
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("classes") != list(ROAD_CLASSES):
        return None
    if isinstance(data.get("tiles"), dict):
        return data["tiles"]
    if isinstance(data.get("lines"), list):
        set_network(data["lines"], source=f"cache {data.get('fetched_at', '?')}")
        return None
    return None


def _save_cache_file(tiles: dict[str, list], bbox: tuple) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"classes": list(ROAD_CLASSES), "rule": NETWORK_RULE,
                       "bbox": list(bbox),
                       "fetched_at": time.strftime("%Y-%m-%d"), "tiles": tiles},
                      f, separators=(",", ":"))
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # memory-only this run; the next start fetches again


def ensure_loaded(bbox: tuple[float, float, float, float] = DEFAULT_BBOX,
                  background: bool = True) -> None:
    """Make sure every tile of ``bbox`` is loaded: from the cache file first,
    then by downloading only the tiles still missing — in a background thread
    by default, so the request that asked never waits on Overpass. A download
    that leaves tiles missing is retried after _RETRY_SEC."""
    global _last_attempt, _cache_tried
    wanted = _tiles_for(bbox)
    if _grid is not None and _tiles is None:
        return  # installed whole (set_network, or the older cache form)
    if not _cache_tried:
        _cache_tried = True
        cached = _load_cache_file()
        if _grid is not None and _tiles is None:
            return
        if cached:
            _install_tiles({k: v for k, v in cached.items() if k in wanted},
                           len(wanted), source="bundled" if os.path.exists(BUNDLE_PATH)
                           else "cache")
    have = dict(_tiles or {})
    missing = [k for k in wanted if k not in have]
    if not missing:
        return
    now = time.time()
    with _lock:
        if _meta.get("state") == "downloading" or now - _last_attempt < _RETRY_SEC:
            return
        _last_attempt = now
        _meta.update(state="downloading", started=now)

    def run() -> None:
        tiles = dict(have)
        error = None
        for i, key in enumerate(missing):
            if i:
                time.sleep(_TILE_PAUSE_SEC)
            try:
                tiles[key] = _fetch(wanted[key])
            except Exception as exc:  # noqa: BLE001 — keep the rest, retry this later
                error = f"{type(exc).__name__}: {exc}"[:300]
                continue
            # Installed and saved after every tile, so a restart halfway
            # through keeps what already arrived.
            _install_tiles(tiles, len(wanted), source="overpass")
            _save_cache_file(tiles, bbox)
        with _lock:
            if len(tiles) < len(wanted):
                _meta.update(state="partial" if tiles else "failed",
                             tiles=f"{len(tiles)}/{len(wanted)}",
                             error=error, retry_after_min=round(_RETRY_SEC / 60))
            else:
                _meta.pop("error", None)
                _meta.pop("retry_after_min", None)

    if background:
        threading.Thread(target=run, name="road-network", daemon=True).start()
    else:
        run()


def load_cached() -> bool:
    """Whether a usable network came from the cache file (tests, tooling)."""
    tiles = _load_cache_file()
    if _grid is not None and _tiles is None:
        return True
    if tiles:
        _install_tiles(tiles, len(tiles), source="cache")
        return True
    return False
