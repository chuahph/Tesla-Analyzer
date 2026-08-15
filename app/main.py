"""FastAPI application entry point."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .collector import seed_demo_if_empty
from .config import get_settings
from .database import init_db

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Tesla Analyzer",
    description="Self-hosted analytics for driving, usage and charging patterns.",
    version="0.1.0",
)


# --- Optional passcode gate --------------------------------------------------
# When APP_PASSCODE is set, everything except the login page requires a signed
# session cookie. Keeps a public cloud URL private without a user system.

AUTH_COOKIE = "ta_auth"


def _auth_token(passcode: str) -> str:
    return hmac.new(passcode.encode(), b"tesla-analyzer-session", hashlib.sha256).hexdigest()


def _is_authed(request: Request, passcode: str) -> bool:
    cookie = request.cookies.get(AUTH_COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, _auth_token(passcode))


# Styled to match the dashboard's design system (style.css): same backdrop
# washes, panel palette, elevation shadows and glassy top-edge highlight, so
# the sign-in screen reads as page one of the app rather than a bare gate.
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tesla Analyzer — Sign in</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;min-height:100dvh;display:flex;align-items:center;justify-content:center;
   padding:24px env(safe-area-inset-right) calc(24px + env(safe-area-inset-bottom)) env(safe-area-inset-left);
   background:
     radial-gradient(1200px 700px at 6% -12%, rgba(232,33,39,.13), transparent 56%),
     radial-gradient(1100px 640px at 100% -6%, rgba(59,130,246,.10), transparent 54%),
     radial-gradient(1000px 900px at 50% 118%, rgba(45,212,191,.07), transparent 60%),
     linear-gradient(180deg,#0d1015 0%,#0a0c11 55%,#0c0f14 100%);
   color:#e6e9ef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
   -webkit-font-smoothing:antialiased}
 @keyframes riseIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
 @media (prefers-reduced-motion:reduce){*{animation-duration:.001ms!important}}
 form{position:relative;background:linear-gradient(180deg,#1b212b,#171b22);
   border:1px solid #262b34;border-radius:18px;padding:34px 28px 28px;
   width:min(92vw,360px);display:flex;flex-direction:column;gap:15px;text-align:center;
   box-shadow:0 2px 6px rgba(0,0,0,.4),0 14px 34px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.055);
   animation:riseIn .5s cubic-bezier(.22,.61,.36,1) both}
 .logo{width:64px;height:64px;margin:0 auto;display:flex;align-items:center;justify-content:center;
   font-size:32px;border-radius:50%;background:radial-gradient(circle at 50% 35%,#2a1215,#171b22 72%);
   border:1px solid rgba(232,33,39,.35);
   box-shadow:0 0 26px rgba(232,33,39,.28),inset 0 1px 0 rgba(255,255,255,.06)}
 h1{margin:2px 0 0;font-size:19px;letter-spacing:.2px}
 .tagline{margin:0;color:#9aa4b2;font-size:12px;letter-spacing:.4px}
 p.hint{margin:2px 0 0;color:#9aa4b2;font-size:13px;line-height:1.5}
 input{background:#1f242d;border:1px solid #262b34;border-radius:10px;color:#e6e9ef;
   padding:13px;font-size:16px;text-align:center;letter-spacing:.25em;outline:none;
   transition:border-color .15s ease,box-shadow .15s ease}
 input:focus{border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,.18)}
 button{background:linear-gradient(180deg,#f03238,#d31c22);border:none;border-radius:10px;
   color:#fff;padding:13px;font-size:14px;font-weight:700;cursor:pointer;min-height:46px;
   letter-spacing:.3px;box-shadow:0 6px 18px -6px rgba(232,33,39,.55),inset 0 1px 0 rgba(255,255,255,.15);
   transition:transform .15s ease,box-shadow .15s ease,filter .15s ease}
 button:hover{filter:brightness(1.06);transform:translateY(-1px);
   box-shadow:0 9px 24px -6px rgba(232,33,39,.6),inset 0 1px 0 rgba(255,255,255,.15)}
 button:active{transform:translateY(0)}
 .err{color:#f59e0b;font-size:12.5px}
</style></head><body>
<form method="post" action="/login">
  <div class="logo">⚡</div>
  <h1>Tesla Analyzer</h1>
  <p class="tagline">Driving · Charging · Efficiency Analytics</p>
  <p class="hint">Enter the passcode to open your dashboard.</p>
  <input type="password" name="passcode" placeholder="Passcode" autofocus autocomplete="current-password">
  {err}
  <button type="submit">Unlock</button>
</form></body></html>"""


# Paths that stay open even when a passcode is set: the login page itself, the
# health endpoint (cloud hosts probe it to decide the deploy succeeded), and
# Tesla's partner public key (Tesla fetches it to verify the registered domain).
TESLA_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"
_OPEN_PATHS = {"/login", "/api/health", TESLA_KEY_PATH}


@app.middleware("http")
async def _passcode_gate(request: Request, call_next):
    settings = get_settings()
    passcode = settings.app_passcode.strip()
    path = request.url.path
    if not passcode or path in _OPEN_PATHS or _is_authed(request, passcode):
        return await call_next(request)
    # External cron services trigger /api/sync (hands-off background logging),
    # /api/backup (scheduled webhook backups), /api/reports/monthly (scheduled
    # webhook reports) and /api/alerts/check (proactive alerts) with the secret
    # key instead of the passcode cookie.
    #
    # /api/repair-arrivals joins them, and it is the only one here that edits
    # trips. The reason is that the correction it applies cannot be made in the
    # sync path safely: the fold-in that should catch a short close runs once
    # and clears its marker, and the guards around it exist because widening
    # them once double-counted 2 km under two names. Running the tested,
    # idempotent repair on a schedule reaches the same place without touching
    # that logic.
    #
    # It is safe to expose because of what it can do rather than who calls it:
    # it only ever moves a boundary to an odometer reading taken while the car
    # sat parked, it refuses anything past ARRIVAL_EST_MAX_KM, and a second run
    # changes nothing. There is no input to forge — the key holder cannot
    # supply a figure, only ask that measurements already on record be applied.
    sync_key = settings.sync_key.strip()
    if (
        sync_key
        and path in ("/api/sync", "/api/backup", "/api/reports/monthly",
                     "/api/alerts/check", "/api/repair-arrivals")
        and hmac.compare_digest(request.query_params.get("key", ""), sync_key)
    ):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Passcode required."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML.replace("{err}", ""))


@app.post("/login")
def login_submit(passcode: str = Form("")):
    expected = get_settings().app_passcode.strip()
    if expected and secrets.compare_digest(passcode.strip(), expected):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            AUTH_COOKIE, _auth_token(expected),
            max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax",
        )
        return resp
    return HTMLResponse(
        LOGIN_HTML.replace("{err}", '<div class="err">Wrong passcode — try again.</div>'),
        status_code=401,
    )


@app.on_event("startup")
def _startup() -> None:
    init_db()
    settings = get_settings()
    if settings.demo_mode:
        # Seed sample data so the dashboard is usable out of the box.
        seed_demo_if_empty()


app.include_router(api_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# Served from the root so the PWA scope covers the whole app (a service worker
# can only control paths at or below its own URL).
@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get(TESLA_KEY_PATH)
def tesla_public_key() -> FileResponse:
    """Partner public key Tesla checks when registering this domain (Fleet API)."""
    return FileResponse(
        STATIC_DIR / "well-known" / "com.tesla.3p.public-key.pem",
        media_type="application/x-pem-file",
    )
