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


def main() -> int:
    roads.CACHE_PATH = os.path.join(tempfile.mkdtemp(), "roads.json")
    roads.BUNDLE_PATH = os.path.join(tempfile.mkdtemp(), "none.json.gz")  # start empty
    roads.clear()
    wanted = roads._tiles_for(roads.DEFAULT_BBOX)
    for n in range(ROUNDS):
        roads._last_attempt = 0.0
        roads.ensure_loaded(roads.DEFAULT_BBOX, background=False)
        st = roads.status()
        print(f"round {n + 1}: {st.get('state')} {st.get('tiles')} {st.get('error') or ''}",
              flush=True)
        if st.get("state") == "loaded":
            break
        time.sleep(ROUND_PAUSE_SEC)
    tiles = roads._tiles or {}
    if not tiles:
        print("no tile answered; nothing to write", file=sys.stderr)
        return 1
    if len(tiles) < len(wanted):
        # Written anyway: the app loads what the bundle has and downloads
        # only the tiles it lacks, so a partial bundle still spares every
        # restart most of the download. Run the workflow again to fill it.
        print(f"incomplete: {len(tiles)}/{len(wanted)} tiles — writing what there is",
              flush=True)
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "app", "road_network.json.gz")
    body = {"classes": list(roads.ROAD_CLASSES), "bbox": list(roads.DEFAULT_BBOX),
            "fetched_at": time.strftime("%Y-%m-%d"),
            "tiles": {k: tiles[k] for k in sorted(tiles)}}
    # mtime=0 so the same network gzips to the same bytes, and an unchanged
    # rebuild commits nothing.
    with open(out, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(body, separators=(",", ":")).encode())
    print(f"wrote {out}: {roads.status().get('segments')} segments, "
          f"{os.path.getsize(out) / 1e6:.2f} MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
