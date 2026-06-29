# ⚡ Tesla Analyzer

A self-hosted, [TeslaMate](https://github.com/teslamate-org/teslamate)-style
analytics app for your Tesla. It logs **drives** and **charging sessions**, then
analyses your **driving, usage and charging patterns** and produces
**concrete, prioritised recommendations** to improve efficiency, cut charging
cost and protect long-term battery health.

It ships with a **demo mode** that generates realistic sample data, so you can
explore the full dashboard immediately — no Tesla account or token required.

### 🔗 Live demo dashboard

**▶ https://chuahph.github.io/Tesla-Analyzer/**

A static build of the dashboard (with baked-in demo data) is published to
GitHub Pages automatically on every push to `main` by the
[Deploy dashboard to GitHub Pages](.github/workflows/pages.yml) workflow, so the
analytics can be viewed straight from the repo with no setup.

> First-time setup: in the repository, go to **Settings → Pages** and set the
> **Source** to **GitHub Actions**. The workflow then builds and deploys on the
> next push to `main`. You can also run the workflow manually from the Actions
> tab (*Run workflow*).

To build the same static site locally:

```bash
python scripts/build_site.py --out site   # → ./site (open site/index.html)
```

---

## Features

**Data logging**
- Lightweight collector that detects drive/charge sessions from Tesla API
  snapshots and stores them in SQLite (any SQLAlchemy database URL works).
- Demo mode generates ~4 months of realistic data with built-in seasonal and
  speed effects.

**Analytics**
- **Driving** — distance, trips, time, average/peak speed, distance by speed
  band, trips by hour & weekday, most frequent routes.
- **Efficiency** — Wh/km vs the rated figure, efficiency by outside
  temperature, weekly trend, best/worst drives, sensitivity of consumption to
  speed and temperature (least-squares fitted).
- **Charging** — total energy & cost, AC vs DC split, average cost per kWh,
  charge-target (end-SoC) distribution, share of 100% charges, charging by
  hour and location.

**Recommendations engine**
Turns the analysis into actionable advice with estimated savings, e.g.:
- "37% of charges go to 100%" → set the daily limit to 80–90%.
- "High speed is costing significant range" → with the Wh/km penalty per km/h.
- "A lot of charging happens during peak hours" → shift to off-peak.
- "Cold weather is hurting efficiency" → pre-condition while plugged in.

**Dashboard**
A dark, responsive single-page dashboard (Chart.js) with KPI cards, six charts
and the recommendation feed. Plus a CLI text/JSON report.

---

## Quick start (demo mode)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run.py serve            # → http://localhost:8000
```

The first run seeds demo data automatically. Open the dashboard, or:

```bash
python run.py report           # text analysis report in the terminal
python run.py report --json    # full analysis as JSON
```

### With Docker

```bash
docker compose up --build      # → http://localhost:8000
```

---

## Connecting your real Tesla

1. Obtain an access token for the **Owner API** or **Fleet API** (e.g. via
   [Tesla Auth](https://github.com/adriankumpf/tesla_auth), the same tool the
   TeslaMate community uses).
2. Copy `.env.example` to `.env` and set:

   ```env
   TESLA_ACCESS_TOKEN=your-token
   TESLA_API_BASE_URL=https://owner-api.teslamotors.com
   RATED_WH_PER_KM=150        # your model's rated consumption
   ENERGY_PRICE_PER_KWH=0.30
   ```

3. Run the collector to start logging sessions, and serve the dashboard:

   ```bash
   python run.py collect       # polls the API; leave running (e.g. in tmux/systemd)
   python run.py serve         # in another shell
   ```

When a token is present the app switches out of demo mode automatically and the
dashboard badge shows **live**.

> Note: the live collector reads `vehicle_data` snapshots and reconstructs
> sessions from state transitions (park ↔ drive, plug ↔ charge). It is
> intentionally compact rather than a full-fidelity GPS logger.

---

## CLI

```
python run.py serve [--port 8000] [--reload]   Web dashboard + REST API
python run.py seed  [--days 120] [--force]     Seed demo data
python run.py collect                          Live Tesla API collector
python run.py report [--days 90] [--json]      Print analysis report
python run.py reset                            Drop & recreate schema
```

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Status and mode (demo/live) |
| `GET /api/vehicles` | Registered vehicles |
| `GET /api/drives?days=30` | Drive records |
| `GET /api/charges?days=30` | Charging records |
| `GET /api/summary?days=90` | **Full analysis + recommendations** (powers the dashboard) |

---

## Architecture

```
app/
  config.py         Settings (.env)            tesla_client.py  Tesla API client
  database.py       Engine/session            collector.py     Logger + demo seeder
  models.py         Vehicle / Drive / Charge  sample_data.py   Realistic data generator
  schemas.py        API response models       main.py          FastAPI app
  api/routes.py     REST endpoints            static/          Dashboard (HTML/CSS/JS)
  analysis/
    driving.py  charging.py  efficiency.py  recommendations.py  __init__.py (stats)
tests/              pytest suite for the analytics engine
```

## Tests

```bash
pytest -q
```

---

## Disclaimer

Not affiliated with or endorsed by Tesla, Inc. "Tesla" is a trademark of its
owner. Use of the Tesla API is subject to Tesla's terms. Recommendations are
heuristic guidance, not engineering advice.
