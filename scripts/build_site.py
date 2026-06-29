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
import shutil
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
WINDOWS = [30, 60, 90, 180, 365]


def build(out_dir: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from app.api.routes import summary
    from app.database import Base
    from app.sample_data import generate

    # --- Seed an in-memory demo database -------------------------------------
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = out_dir / "data"
    data_dir.mkdir(exist_ok=True)

    with Session() as session:
        generate(session, days=365, seed=42)
        for days in WINDOWS:
            payload = summary(days=days, session=session)
            (data_dir / f"summary-{days}.json").write_text(
                json.dumps(payload, default=str, indent=2)
            )
            print(f"  wrote data/summary-{days}.json")

    # --- Copy shared assets --------------------------------------------------
    shutil.copy(STATIC / "style.css", out_dir / "style.css")
    shutil.copy(STATIC / "app.js", out_dir / "app.js")
    (out_dir / "vendor").mkdir(exist_ok=True)
    shutil.copy(STATIC / "vendor" / "chart.umd.js", out_dir / "vendor" / "chart.umd.js")

    # --- Static index.html (relative asset paths + static data source) -------
    index = (STATIC / "index.html").read_text()
    index = index.replace('href="/static/style.css"', 'href="style.css"')
    index = index.replace('src="/static/vendor/chart.umd.js"', 'src="vendor/chart.umd.js"')
    index = index.replace('src="/static/app.js"', 'src="app.js"')
    # Tell app.js to read the pre-built JSON snapshots, and add a banner.
    index = index.replace(
        "<script src=\"app.js\"></script>",
        '<script>window.SUMMARY_URL = (days) => `data/summary-${days}.json`;</script>\n'
        '  <script src="app.js"></script>',
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
