"""Build app/road_network.json.gz: the expressway/trunk network, bundled.

Runs app/roads.py's own tiled Overpass download to completion — retrying
tiles that time out, since the public servers often do — and writes every
tile, gzipped, where the app loads it first. With it in the image a restart
needs no download at all.

Run by the "Build road network" GitHub Action; also runnable by hand from
anywhere that can reach overpass-api.de:  python scripts/build_road_network.py
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import roads  # noqa: E402

ROUNDS = 8           # full passes over the missing tiles before giving up
ROUND_PAUSE_SEC = 60  # between passes, to let a busy server recover
# Stop downloading with this much of the job's 120 minutes used, and write
# what arrived. A run killed at the limit wrote nothing and threw away every
# tile it had; now a slow night leaves a partial bundle, and the next run
# starts from it and fetches only the rest.
TIME_BUDGET_SEC = float(os.environ.get("ROAD_BUILD_BUDGET_SEC", str(100 * 60)))


# Printed after the build: what the map calls the roads through one corridor
# the owner knows (Jelutong -> Bayan Lepas), and which of them the rule keeps
# as expressway — so a rule change can be judged against a known answer from
# the action's log, without anyone reaching Overpass by hand.
CHECK_BBOX = (5.33, 100.29, 5.38, 100.33)


def report_corridor() -> None:
    import httpx
    from collections import Counter

    s, w, n, e = CHECK_BBOX
    q = (f'[out:json][timeout:60];way["highway"~"^({"|".join(roads.ROAD_CLASSES)})$"]'
         f"({s},{w},{n},{e});out tags;")
    for url in roads.OVERPASS_URLS:
        try:
            r = httpx.post(url, data={"data": q}, timeout=90.0,
                           headers={"User-Agent": "ev-drive-analyzer (road type lookup)"})
            r.raise_for_status()
            break
        except Exception as exc:  # noqa: BLE001
            print(f"corridor check: {url} failed: {exc}", flush=True)
    else:
        return
    seen = Counter()
    for el in r.json().get("elements", []):
        t = el.get("tags") or {}
        keep = t.get("highway") == "motorway" or roads.is_expressway(t)
        seen[(t.get("highway"), t.get("ref", "-"), t.get("name", "-"),
              "EXPRESSWAY" if keep else "city")] += 1
    print("corridor check (Jelutong -> Bayan Lepas): ways by highway/ref/name -> verdict",
          flush=True)
    for (hw, ref, name, verdict), n_ways in sorted(seen.items()):
        print(f"  {n_ways:3d} x {hw:8s} ref={ref:10s} {name:40s} -> {verdict}", flush=True)


def _shipped_pending(path: str) -> list:
    """Tiles the shipped bundle still carries from an older rule."""
    import gzip as _gz

    try:
        with _gz.open(path, "rt") as f:
            return json.load(f).get("pending_tiles") or []
    except (OSError, ValueError, EOFError):
        return []


def main() -> int:
    shipped, fetch = roads.BUNDLE_PATH, roads._fetch
    try:
        return _build()
    finally:
        # Put back what _build swapped out, so a second build in the same
        # process (the tests) starts from the bundle this one wrote.
        roads.BUNDLE_PATH, roads._fetch = shipped, fetch


def _build() -> int:
    started = time.monotonic()
    roads.CACHE_PATH = os.path.join(tempfile.mkdtemp(), "roads.json")
    shipped = roads.BUNDLE_PATH
    roads.BUNDLE_PATH = os.path.join(tempfile.mkdtemp(), "none.json.gz")  # start empty
    roads.clear()
    # Resume: tiles already in the shipped bundle under the CURRENT rule are
    # kept, so a run that stopped on its budget is finished by the next one.
    shipped_tiles, rule = roads._read_tiles(shipped, want_rule=True)
    pending = set(_shipped_pending(shipped))
    kept = {k: v for k, v in (shipped_tiles or {}).items() if k not in pending}
    if kept and rule == roads.NETWORK_RULE:
        roads._save_cache_file(kept, roads.DEFAULT_BBOX)
        print(f"resuming from the shipped bundle: {len(kept)} tiles under {rule}", flush=True)
    wanted = roads._tiles_for(roads.DEFAULT_BBOX)
    real_fetch = roads._fetch

    def budgeted_fetch(bbox):
        if time.monotonic() - started > TIME_BUDGET_SEC:
            raise RuntimeError("time budget spent; leaving this tile for the next run")
        return real_fetch(bbox)

    roads._fetch = budgeted_fetch
    for n in range(ROUNDS):
        if time.monotonic() - started > TIME_BUDGET_SEC:
            print("time budget spent; writing what arrived", flush=True)
            break
        roads._last_attempt = 0.0
        roads.ensure_loaded(roads.DEFAULT_BBOX, background=False)
        st = roads.status()
        print(f"round {n + 1}: {st.get('state')} {st.get('tiles')} {st.get('error') or ''}",
              flush=True)
        if st.get("state") == "loaded":
            break
        if time.monotonic() - started + ROUND_PAUSE_SEC > TIME_BUDGET_SEC:
            continue
        time.sleep(ROUND_PAUSE_SEC)
    tiles = roads._tiles or {}
    if not tiles:
        print("no tile answered; nothing to write", file=sys.stderr)
        return 1
    tiles = dict(tiles)
    stale = []
    if len(tiles) < len(wanted):
        # Never ship fewer tiles than before: a tile this run did not reach
        # keeps the shipped bundle's version (built under the older rule), and
        # is listed in pending_tiles so the next run fetches it. Run the
        # workflow again until nothing is pending.
        for k in wanted:
            if k not in tiles and shipped_tiles and k in shipped_tiles:
                tiles[k] = shipped_tiles[k]
                stale.append(k)
        print(f"incomplete: {len(tiles) - len(stale)}/{len(wanted)} tiles fetched, "
              f"{len(stale)} kept from the previous bundle — run again to finish",
              flush=True)
    out = os.environ.get("ROAD_BUNDLE_OUT") or shipped
    body = {"classes": list(roads.ROAD_CLASSES), "rule": roads.NETWORK_RULE,
            "bbox": list(roads.DEFAULT_BBOX),
            "fetched_at": time.strftime("%Y-%m-%d"),
            "pending_tiles": sorted(stale),
            "tiles": {k: tiles[k] for k in sorted(tiles)}}
    # mtime=0 so the same network gzips to the same bytes, and an unchanged
    # rebuild commits nothing.
    with open(out, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(body, separators=(",", ":")).encode())
    print(f"wrote {out}: {roads.status().get('segments')} segments, "
          f"{os.path.getsize(out) / 1e6:.2f} MB", flush=True)
    report_corridor()
    return 0


if __name__ == "__main__":
    sys.exit(main())
