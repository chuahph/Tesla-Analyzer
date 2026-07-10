#!/usr/bin/env python3
"""Command-line entry point for Tesla Analyzer.

Usage:
    python run.py serve              Start the web dashboard + API (default)
    python run.py seed [--days N]    Seed the database with demo data
    python run.py collect            Run the live Tesla API collector
    python run.py report [--days N]  Print a text analysis report to stdout
    python run.py reset              Drop and recreate the database schema
    python run.py push-keys          Generate a VAPID keypair for web push
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Cloud hosts (Render/Railway/Fly/…) inject the port to bind via $PORT.
DEFAULT_PORT = int(os.environ.get("PORT", "8000"))


def cmd_serve(args):
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


def cmd_seed(args):
    from app.database import SessionLocal, init_db
    from app.models import Vehicle
    from app.sample_data import generate
    from sqlalchemy import func, select

    init_db()
    with SessionLocal() as session:
        if session.scalar(select(func.count()).select_from(Vehicle)) and not args.force:
            print("Database already has data. Use --force to add another vehicle.")
            return
        v = generate(session, days=args.days)
        print(f"Seeded {args.days} days of demo data for vehicle '{v.name}'.")


def cmd_collect(args):
    from app.collector import run_live

    run_live()


def cmd_report(args):
    from app.api.routes import summary
    from app.database import SessionLocal

    with SessionLocal() as session:
        data = summary(days=args.days, session=session)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return
    _print_report(data)


def cmd_reset(args):
    from app.database import Base, engine, init_db

    Base.metadata.drop_all(bind=engine)
    init_db()
    print("Database schema reset.")


def cmd_push_keys(args):
    from webpush.vapid import VAPID

    # Stored as a single-line env var with literal "\n" sequences (the same
    # trick used for multi-line PEM/JSON secrets on most hosts); config.py
    # converts them back to real newlines before handing the PEM to the
    # crypto library.
    private_pem, public_pem, _ = VAPID.generate_keys()
    private_line = private_pem.decode().strip().replace("\n", "\\n")
    public_line = public_pem.decode().strip().replace("\n", "\\n")
    print("Generated a new VAPID keypair for web push notifications.")
    print("Set these in your environment (Render → Environment, or .env):\n")
    print(f"VAPID_PRIVATE_KEY_PEM={private_line}")
    print(f"VAPID_PUBLIC_KEY_PEM={public_line}")
    print("VAPID_SUBJECT_EMAIL=you@example.com   # a contact address; never emailed to you")
    print("\nKeep the private key secret — anyone with it can send push "
          "messages impersonating this app to your subscribed devices.")


def _print_report(d):
    v = d["vehicle"]
    print(f"\n=== Tesla Analyzer report — {v['name']} ({v['model']} {v['trim']}) ===")
    print(f"Window: last {d['window_days']} days · generated {d['generated_at']}\n")

    drv, chg, eff = d["driving"], d["charging"], d["efficiency"]
    if drv.get("available"):
        print("DRIVING")
        print(f"  Distance .......... {drv['total_distance_km']:.0f} km over {drv['total_drives']} drives")
        print(f"  Driving time ...... {drv['total_duration_h']:.1f} h")
        print(f"  Avg / p95 speed ... {drv['avg_speed_kmh']:.0f} / {drv['p95_speed_kmh']:.0f} km/h")
    if eff.get("available"):
        print("\nEFFICIENCY")
        print(f"  Average ........... {eff['avg_efficiency_wh_per_km']:.0f} Wh/km "
              f"({eff['vs_rated_pct']:+.0f}% vs {eff['rated_wh_per_km']:.0f} rated)")
        print(f"  Best / worst ...... {eff['best_efficiency_wh_per_km']:.0f} / "
              f"{eff['worst_efficiency_wh_per_km']:.0f} Wh/km")
    if chg.get("available"):
        print("\nCHARGING")
        print(f"  Energy ............ {chg['total_energy_kwh']:.0f} kWh "
              f"({d['currency']} {chg['total_cost']:.0f})")
        print(f"  AC / DC sessions .. {chg['ac_sessions']} / {chg['dc_sessions']} "
              f"(DC = {chg['dc_energy_share_pct']:.0f}% of energy)")
        print(f"  Charges to 100% ... {chg['full_charge_share_pct']:.0f}%")

    print("\nRECOMMENDATIONS")
    for r in d["recommendations"]:
        print(f"  [{r['priority'].upper():6}] {r['title']}")
        print(f"           {r['detail']}")
        if r["estimated_saving"]:
            print(f"           → {r['estimated_saving']}")
    print()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tesla Analyzer")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="Run the web dashboard + API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("seed", help="Seed demo data")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("collect", help="Run the live Tesla API collector")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("report", help="Print a text analysis report")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("reset", help="Reset the database schema")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("push-keys", help="Generate a VAPID keypair for web push notifications")
    p.set_defaults(func=cmd_push_keys)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args.command = "serve"
        args.func = cmd_serve
        args.host, args.port, args.reload = "0.0.0.0", DEFAULT_PORT, False
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
