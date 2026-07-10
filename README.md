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
- **Battery health vs fleet** — once an odometer reading exists, the health
  card compares this pack's own degradation against a typical Tesla pack at
  the same mileage (a rough yardstick from widely-cited aggregate studies,
  not per-VIN precision), so "6% degradation" reads as *ahead of* or
  *behind* what's normal at that distance rather than in isolation.
- **vs Petrol (TCO)** — optional: set `PETROL_PRICE_PER_LITER` and
  `PETROL_L_PER_100KM` for a "vs Petrol" KPI card showing what this window's
  distance would have cost in an equivalent petrol car, vs. what it actually
  cost to charge. Hidden until both are configured.

**Recommendations engine**
Turns the analysis into actionable advice with estimated savings, e.g.:
- "37% of charges go to 100%" → set the daily limit to 80–90%.
- "High speed is costing significant range" → with the Wh/km penalty per km/h.
- "Cold weather is hurting efficiency" → pre-condition while plugged in.
- **Smart charging advisor** (advisory only) — with a time-of-use tariff
  configured, sizes a real currency figure from the account's own peak-hour
  charging energy (e.g. "146.6 kWh charged at peak → RM 109.97/window could
  be saved by scheduling charging after 22:00"), rather than a generic hint.
  Purely a suggestion in the recommendation feed — it never sets a schedule
  or sends any command to the car.

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

   Optional: time-of-use pricing instead of the flat rate above — driving and
   charging cost then price each session by its own start time. Both rates
   must be set (> 0) to enable it; leaving either at 0 keeps the flat rate.

   ```env
   ENERGY_PRICE_PEAK_KWH=1.20      # RM/kWh during peak hours
   ENERGY_PRICE_OFFPEAK_KWH=0.45   # RM/kWh outside them
   TARIFF_PEAK_START_HOUR=8        # 24h, default 8
   TARIFF_PEAK_END_HOUR=22         # 24h, default 22
   TARIFF_WEEKEND_OFFPEAK=true     # whole weekend at the off-peak rate (default)
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

## Keeping it in sync (cron)

For the hosted deployment (Render), something needs to hit `/api/sync` on a
timer so drives/charges get logged without you opening the dashboard. The repo
ships `.github/workflows/sync-car.yml` for this, but **its schedule trigger is
disabled by default** — use an external cron service instead (below). It stays
in the workflow only as a manual `workflow_dispatch` button (Actions tab → Sync
car from Tesla → Run workflow) for on-demand testing.

**Why not GitHub Actions' own schedule?** GitHub
[documents](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule)
its `schedule` trigger as best-effort — runs can be delayed by several minutes,
worse at the top of every hour, and can be skipped outright under load. A
5-minute nominal interval can easily become 10-15 minutes in practice. That's
long enough to miss a short trip's entire "driving" window, so the sync never
sees the car in gear at all — it only catches up once the car is parked again,
and if it fell straight asleep after locking, that catch-up read can be an hour
or more late. The result: a real trip logged with the right distance but the
wrong clock time. A dedicated external cron service is simply more reliable
than GitHub's for a job that needs to fire close to on-time, every time.

### Set up an external cron (recommended)

Any service that can hit a URL on a schedule works. [cron-job.org](https://cron-job.org)
is free and reliable enough for this:

1. Create a free account.
2. **Create cronjob** → URL:
   ```
   https://<your-app>.onrender.com/api/sync?key=<SYNC_KEY>
   ```
   (`SYNC_KEY` is whatever you set in Render's environment variables — see the
   Render blueprint section above.)
3. Set the schedule to **every 1-2 minutes** — see the battery-safety note below
   for why this doesn't drain the car.
4. Save. That's it; no repository secrets or GitHub Actions involved.

Any similar service works the same way — UptimeRobot (as an "HTTP(s)" monitor,
which incidentally also gets you uptime alerts for free), EasyCron, or your
own always-on machine's system `cron` calling `curl`.

**Battery safety is unaffected by how often you call this.** The endpoint
decides for itself whether to actually read the car, separately from how
often you hit it: it never reads a car that's asleep, and beyond that it
only reads an online-but-idle car about once every `BASE_POLL_INTERVAL_MIN`
(5 min by default) — escalating tighter only while a trip is genuinely in
progress or the car just woke up on its own. Calling `/api/sync` every
minute drives that decision more often, it doesn't force a read every
minute. See `BASE_POLL_INTERVAL_MIN` / `FAST_POLL_WINDOW_MIN` in
`app/api/routes.py` for the exact logic — a `vehicle_data` read is itself an
activity signal that resets Tesla's own sleep countdown, which is exactly
what this throttle protects against.

### Re-enabling the GitHub Actions schedule instead

If you'd rather not sign up for another service, uncomment the `schedule:`
block at the top of `.github/workflows/sync-car.yml` and add the `RENDER_URL`
/ `SYNC_KEY` repository secrets (Settings → Secrets and variables → Actions).
It will work, just with the delay/reliability caveat above.

---

## Automated backups

`GET /api/backup` builds a full-history export (the same ZIP as **⬇ Export
CSV**) and POSTs it to a webhook you configure — a safety net for the free-tier
Neon database, without setting up an SMTP server.

1. Set `BACKUP_WEBHOOK_URL` in Render's environment variables to wherever you
   want the ZIP delivered: your own small receiving endpoint, a cloud-storage
   presigned upload URL (S3, Cloudflare R2, Backblaze B2), or a relay service
   (e.g. [Pipedream](https://pipedream.com) or [Make](https://www.make.com))
   that forwards it to email.
2. Add a **second** cron job pointed at:
   ```
   https://<your-app>.onrender.com/api/backup?key=<SYNC_KEY>
   ```
   on whatever schedule you want a backup — daily or weekly is plenty; unlike
   `/api/sync`, there's no reason to call this often. It reuses the same
   `SYNC_KEY` as the sync cron job.
3. Without `BACKUP_WEBHOOK_URL` set, the endpoint returns a 400 explaining
   that — it never silently no-ops.

The posted body is `Content-Type: application/zip`, raw bytes (not
multipart) — a generic HTTP endpoint or presigned PUT URL can consume it
directly. Slack/Discord's own incoming webhooks expect multipart file
uploads, so those need a small relay in between rather than the URL directly.

---

## Scheduled reports

`GET /api/reports/monthly` POSTs a driving/charging/efficiency summary — as
JSON — to a webhook, on whatever schedule you point a cron job at it.

1. Set `REPORT_WEBHOOK_URL` in Render's environment variables.
2. Add a **third** cron job pointed at:
   ```
   https://<your-app>.onrender.com/api/reports/monthly?key=<SYNC_KEY>
   ```
   monthly (or whatever cadence you'd like a report at — the endpoint itself
   doesn't track a period, it just summarises the last `?days=N` days,
   default 30). Reuses the same `SYNC_KEY` as the sync and backup cron jobs.
3. Without `REPORT_WEBHOOK_URL` set, the endpoint returns a 400 explaining
   that — it never silently no-ops.

The JSON body includes a top-level `"text"` field — a plain-English summary
line — which **Slack and Discord incoming webhooks read directly**, so
pointing either straight at this endpoint's URL works with no relay needed
(unlike the raw-ZIP backup above). The same payload also carries the full
structured figures (`driving`, `charging`, `efficiency`) for anything that
wants to parse it instead.

---

## Generic event webhook

Set `EVENT_WEBHOOK_URL` to have Tesla Analyzer POST a small JSON payload —
`{"event", "title", "body", "timestamp"}` — whenever a **charge completes**,
the **battery goes low**, or a **drive finishes**, for home automation (Home
Assistant, IFTTT, Zapier, n8n, a webhook-triggered Shortcut, ...) to react to.
Independent of [push notifications](#push-notifications) below — it fires
even without VAPID keys configured, and vice versa, so you can use either,
both, or neither. Drive-complete only fires here, not as a push notification
(a push alert per every single trip would be unwanted noise for anyone who
already has charge/low-battery push enabled). A delivery failure is silently
ignored — a flaky third-party endpoint never blocks the sync loop that
triggered it.

---

## Push notifications

Get an alert on your phone/desktop the moment a charge finishes or the
battery drops low — no app-store app, this uses the standard [Web Push
API](https://web.dev/articles/push-notifications-web-push-protocol), the same
mechanism sites like Twitter/Gmail use for browser notifications, delivered
through the PWA you already installed.

1. Generate a keypair:
   ```bash
   python run.py push-keys
   ```
   Paste the three printed lines (`VAPID_PRIVATE_KEY_PEM`,
   `VAPID_PUBLIC_KEY_PEM`, `VAPID_SUBJECT_EMAIL`) into Render's environment
   variables (or `.env` for self-hosting). The subject email is a contact
   address a push service could use if a subscription misbehaves — it's
   never emailed to you and doesn't need to be a real inbox you check.
2. Optionally set `LOW_SOC_NOTIFY_PCT` (e.g. `20`) to also get a one-time
   "Battery low" alert when SoC drops to/below that level — it re-arms once
   the battery recovers above the threshold (+5% hysteresis), so plugging in
   and charging resets it rather than it firing once ever.
3. Redeploy, open the dashboard, and tap **🔔 Enable notifications** at the
   bottom of the page (only appears once VAPID keys are configured). Your
   browser will ask for notification permission once.

That's it — every device that taps the button gets notified independently
(subscriptions aren't tied to a single browser/phone). Tap the button again
on a device to turn its notifications off. Currently wired up: **charging
complete** and **battery low**; the delivery mechanism (`app/notifications.py`
— a single `notify(session, title, body)` call) is meant to be easy to hook
up to more events later (car left unlocked, sync gone stale, ...).

> Uses the `webpush` package rather than the more commonly-referenced
> `pywebpush` — the latter depends on `http-ece`, which fails to build with
> current Python/setuptools and would break the Docker build. `webpush` is a
> pure `cryptography` + `pydantic` + `PyJWT` implementation of the same
> RFC 8291/8292 protocol, with no C-extension dependency.

---

## Named places (Home/Office)

Trip locations are reverse-geocoded automatically, but a street address isn't
as useful as "Home". Tap **📍 Places** at the bottom of the dashboard (or the
small 📍 next to a trip's own start/end location) to name a spot — any trip
starting or ending within its radius (default 150 m) then shows that name
instead, **including trips already logged**. Delete a place any time; it only
stops applying to new trips going forward.

Two ways to add one:
- **📍 next to a trip** — uses that trip's own coordinates, so you only type
  the name.
- **📡 Use my location** in the Places panel — uses the device's GPS (asks for
  browser location permission once).

Self-hosted only (needs the backend database to persist against), so the
button stays hidden in the static/demo build.

Once at least one place is defined, an in-progress drive also gets a live
**ETA** KPI card: straight-line distance and time to the nearest place you
haven't reached yet, plus the SoC it projects on arrival at the drive's own
current pace and efficiency. It's a gut-check, not turn-by-turn navigation —
no map/routing service is involved.

---

## Compare Cars

With more than one real car linked to the account, **⚖ Compare Cars** on the
garage page shows every car's driving distance, efficiency, cost, charging
and battery-health figures for the same window, side by side — useful for a
household with more than one Tesla to see at a glance which car is driven
more, costs more to run, or is degrading faster, without switching the
active car back and forth. Hidden with a single car (nothing to compare).

---

## Service & tyre tracker

Tap **🔧 Service & Tyres** to log maintenance (tyre rotation, cabin air
filter, brake fluid, ...) and see when each is next due, from Tesla's own
published general intervals — e.g. tyre rotation every 10,000 km or 12
months, whichever comes first. A type never logged shows **Not logged**
rather than a false "overdue" (it may well have been done before you started
tracking); logging it once starts the countdown. Purely what you enter here
— the car doesn't report service history over the API. Self-hosted only.

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
