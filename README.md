# ⚡ Tesla Analyzer

A self-hosted analytics app for your Tesla with its own clean dashboard design.
It logs **drives** and **charging sessions**, analyses your **driving, usage and
charging patterns**, and produces **concrete, prioritised recommendations** to
improve efficiency, cut charging cost and protect long-term battery health.

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
and the recommendation feed. Installs on iOS/Android as a **PWA** (home-screen
app, full-screen, offline). Plus a CLI text/JSON report.

**Two ways to load your own data** (buttons in the dashboard header)
1. **📁 Load Tesla Data** — upload a Tesla *Download Your Data* export
   (CSV / JSON / ZIP). The importer matches columns loosely, converts miles→km,
   and replaces the demo data with yours.
2. **🔗 Link Tesla Account** — either *Sign in with Tesla* (OAuth, needs a Tesla
   developer app) or paste an access token. The token is stored only on your
   own server; run the collector to log new sessions over time.

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

## Loading your own data

The dashboard header has two buttons. **📁 Load Tesla Data** works everywhere,
including the static Pages app / installed PWA (it parses and analyses in the
browser). **🔗 Link Tesla Account** needs the self-hosted backend.

### 📁 Load Tesla Data (manual import)

Request your data from Tesla (**tesla.com → Privacy → Download Your Data**),
then upload the export. The importer accepts **CSV**, **JSON**, or the whole
**ZIP** export and replaces the current data set. The ZIP reader walks nested
folders, recurses into nested zips, treats `.csv`/`.tsv`/`.txt` as delimited
data, and skips macOS `__MACOSX`/`.DS_Store` junk. Columns are matched loosely
(case/spacing/punctuation insensitive) and miles are converted to km
automatically.

| Drives | Charges |
|--------|---------|
| `start_time`, `end_time` | `start_time`, `end_time` |
| `distance` (km or miles) | `energy_added` (kWh) |
| `duration`, `start_soc`, `end_soc` | `charge_type` (AC/DC), `max_power` |
| `energy_used`, `avg_speed`, `max_speed` | `start_soc`, `end_soc` |
| `outside_temp`, `start_location`, `end_location` | `location`, `cost`, `outside_temp` |

Drive vs charge files are detected automatically from their columns. You can
also re-import this app's own `GET /api/export` JSON. Equivalent API call:

```bash
curl -F "file=@my_drives.csv" http://localhost:8000/api/import
```

### 🔗 Link Tesla Account

- **Sign in with Tesla (OAuth):** set `TESLA_CLIENT_ID` / `TESLA_CLIENT_SECRET`
  (from a [Tesla developer app](https://developer.tesla.com)) in `.env`, then
  click the button to complete the OAuth flow.
- **Access token:** paste a token (from
  [tesla_auth](https://github.com/adriankumpf/tesla_auth)) and pick the API base
  URL. The token is validated against Tesla and stored only on your server.

```bash
curl -X POST http://localhost:8000/api/link/token \
  -H 'Content-Type: application/json' \
  -d '{"access_token":"<token>","base_url":"https://owner-api.teslamotors.com"}'
```

Once linked, run `python run.py collect` to log new drives/charges over time.

---

## Connecting your real Tesla (via .env)

1. Obtain an access token for the **Owner API** or **Fleet API** (e.g. via
   [Tesla Auth](https://github.com/adriankumpf/tesla_auth)).
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

## Install on iPhone / iPad (PWA) — no computer needed

Tesla Analyzer is an installable **Progressive Web App** — it adds to your home
screen, launches full-screen with its own icon, and works offline.

**It runs entirely on the phone.** In the installed app / on the Pages site, the
import and the full analysis run **in the browser** (`analysis.js` + `importer.js`,
with JSZip for `.zip`) — no backend, no host PC. Upload your Tesla export on the
iPhone and it parses and analyses on-device; your data is kept only in the
browser's local storage (use **Use demo data** in the import dialog to clear it).

> The **🔗 Link Tesla Account** button still needs the self-hosted backend (Tesla's
> API can't be called directly from a browser). For on-device use, export your
> data from Tesla and use **📁 Load Tesla Data**.

1. Open the dashboard in **Safari** on your iPhone/iPad:
   - the live demo — **https://chuahph.github.io/Tesla-Analyzer/**, or
   - your self-hosted app — `http://<your-computer-ip>:8000` (phone on the same
     Wi-Fi; start it with `python run.py serve`).
2. Tap the **Share** button (the square with an ↑ arrow).
3. Choose **Add to Home Screen**, then **Add**.
4. Launch it from the new **⚡ Tesla Analyzer** icon — it opens like a native app.

> iOS only allows installing PWAs from **Safari** (not Chrome/Firefox on iOS).
> The static demo works fully offline once opened; the self-hosted app caches the
> last view so it still opens offline and refreshes when back online.

Under the hood this is a web app manifest + Apple meta tags + a service worker
(`app/static/sw.js`, served at `/sw.js`) with runtime caching. App icons are
generated by `python scripts/make_icons.py`.

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
| `GET /api/export` | Export stored data as re-importable JSON |
| `POST /api/import` | Import a Tesla data export (CSV/JSON/ZIP) |
| `POST /api/link/token` | Link an account with an access token |
| `GET /api/link/oauth/start` · `…/callback` | Tesla OAuth sign-in flow |

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
