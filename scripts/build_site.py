#!/usr/bin/env python3
"""Build a static, GitHub Pages-ready version of the dashboard.

GitHub Pages serves static files only, so this script seeds an in-memory demo
database, pre-computes the analysis for each time window into JSON snapshots,
and assembles a self-contained ``site/`` directory that reuses the same CSS,
Chart.js bundle and ``app.js`` as the live dashboard.

Usage:
    python scripts/build_site.py [--out site]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def build(out_dir: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from app.api.routes import export_data
    from app.database import Base
    from app.sample_data import generate

    # --- Seed an in-memory demo database -------------------------------------
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = out_dir / "data"
    data_dir.mkdir(exist_ok=True)

    # Export the raw demo dataset; the in-browser engine computes the analysis
    # for any window client-side (the same code path used for imported data).
    with Session() as session:
        generate(session, days=365, seed=42)
        payload = export_data(days=400, session=session)
        (data_dir / "demo.json").write_text(json.dumps(payload, default=str))
        print(f"  wrote data/demo.json "
              f"({len(payload['drives'])} drives, {len(payload['charges'])} charges)")

    # --- Copy shared assets --------------------------------------------------
    shutil.copy(STATIC / "style.css", out_dir / "style.css")
    shutil.copy(STATIC / "app.js", out_dir / "app.js")
    shutil.copy(STATIC / "sw.js", out_dir / "sw.js")
    shutil.copy(STATIC / "analysis.js", out_dir / "analysis.js")
    shutil.copy(STATIC / "importer.js", out_dir / "importer.js")
    (out_dir / "vendor").mkdir(exist_ok=True)
    shutil.copy(STATIC / "vendor" / "chart.umd.js", out_dir / "vendor" / "chart.umd.js")
    shutil.copy(STATIC / "vendor" / "jszip.min.js", out_dir / "vendor" / "jszip.min.js")

    # PWA icons (copied as-is) and a manifest tailored to the Pages subpath.
    shutil.copytree(STATIC / "icons", out_dir / "icons", dirs_exist_ok=True)
    manifest = json.loads((STATIC / "manifest.webmanifest").read_text())
    manifest["start_url"] = "./"
    manifest["scope"] = "./"
    for icon in manifest["icons"]:
        icon["src"] = icon["src"].replace("/static/icons/", "icons/")
    (out_dir / "manifest.webmanifest").write_text(json.dumps(manifest, indent=2))

    # --- Static index.html (relative asset paths + static data source) -------
    index = (STATIC / "index.html").read_text()
    index = index.replace('href="/static/style.css"', 'href="style.css"')
    index = index.replace('src="/static/vendor/chart.umd.js"', 'src="vendor/chart.umd.js"')
    index = index.replace('src="/static/vendor/jszip.min.js"', 'src="vendor/jszip.min.js"')
    index = index.replace('src="/static/analysis.js"', 'src="analysis.js"')
    index = index.replace('src="/static/importer.js"', 'src="importer.js"')
    index = index.replace('src="/static/app.js"', 'src="app.js"')
    # PWA assets: rewrite root-absolute paths to relative for the Pages subpath.
    index = index.replace('href="/manifest.webmanifest"', 'href="manifest.webmanifest"')
    index = index.replace('href="/static/icons/', 'href="icons/')
    # Enable in-browser (no-backend) mode, point at the raw demo dataset, and
    # stamp the build (Actions run #/SHA + MYT build time) for the header.
    import subprocess
    from datetime import datetime, timedelta, timezone

    sha = (os.environ.get("GITHUB_SHA") or "")[:7]
    if not sha:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
            ).strip()
        except Exception:  # noqa: BLE001
            sha = ""
    run = os.environ.get("GITHUB_RUN_NUMBER") or ""
    btime = datetime.now(timezone(timedelta(hours=8))).strftime("%d %b %Y %H%M")
    index = index.replace(
        '<script src="analysis.js"></script>',
        "<script>window.TA_STATIC = true; window.DEMO_URL = \"data/demo.json\"; "
        f'window.BUILD_INFO = {{run:"{run}",sha:"{sha}",time:"{btime}"}};</script>\n'
        '  <script src="analysis.js"></script>',
    )
    (out_dir / "index.html").write_text(index)

    # Disable Jekyll processing on Pages so files are served verbatim.
    (out_dir / ".nojekyll").write_text("")
    print(f"Static site built at: {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static dashboard site")
    parser.add_argument("--out", default="site", help="Output directory (default: site)")
    args = parser.parse_args()
    build((ROOT / args.out).resolve())


if __name__ == "__main__":
    main()
