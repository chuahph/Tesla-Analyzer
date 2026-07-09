---
name: verify
description: Launch Tesla Analyzer against a scratch DB with demo data and drive the dashboard end-to-end in a headless browser.
---

# Verify Tesla Analyzer

## Launch (demo mode seeds sample drives/charges on startup)

```bash
DATABASE_URL="sqlite:////tmp/verify.db" APP_PASSCODE="" \
  python -m uvicorn app.main:app --port 8777 --host 127.0.0.1 &
curl -s http://127.0.0.1:8777/api/health   # {"mode":"demo",...}
```

## Drive the API

`curl "http://127.0.0.1:8777/api/summary?days=365"` — the single payload the
dashboard consumes. Also worth: `?days=7`, `?since_charge=1`, `?current_drive=1`.

## Drive the UI (Python Playwright; node playwright is NOT installed)

```python
from playwright.sync_api import sync_playwright
# executable_path="/opt/pw-browsers/chromium"
```

- The garage/home view shows first: click into `#home-cars` to reach `#dashboard`.
- Change window via `page.select_option("#range", "365")`.
- KPI cards: `#kpis .kpi`; trips: `#recentTrips li`; strip: `#week-compare`.
- Report button: stub `window.print` first, then check `#print-report` innerHTML.

## Gotchas

- Demo data seeds NO BatteryReading rows: the Battery Health card, trend chart
  and Battery Balance current-SoC are hidden/null in plain demo mode. Insert
  rows into `battery_readings` directly to exercise them (no restart needed).
- Wide windows (90/365d) exercise the ">100% of pack used" display paths.
- Collect console errors via `page.on("console"/"pageerror", ...)` — the app
  should produce none.
