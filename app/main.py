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


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tesla Analyzer — Sign in</title>
<style>
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
   background:#0e1116;color:#e6e9ef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
 form{background:#171b22;border:1px solid #262b34;border-radius:14px;padding:30px 26px;
   width:min(92vw,340px);display:flex;flex-direction:column;gap:14px;text-align:center}
 .logo{font-size:34px}
 h1{margin:0;font-size:18px}
 p{margin:0;color:#9aa4b2;font-size:13px}
 input{background:#1f242d;border:1px solid #262b34;border-radius:9px;color:#e6e9ef;
   padding:12px;font-size:16px;text-align:center;letter-spacing:.2em}
 button{background:#e82127;border:none;border-radius:9px;color:#fff;padding:13px;
   font-size:14px;font-weight:700;cursor:pointer;min-height:44px}
 .err{color:#f59e0b;font-size:12.5px}
</style></head><body>
<form method="post" action="/login">
  <div class="logo">⚡</div>
  <h1>Tesla Analyzer</h1>
  <p>Enter the passcode to open your dashboard.</p>
  <input type="password" name="passcode" placeholder="Passcode" autofocus autocomplete="current-password">
  {err}
  <button type="submit">Unlock</button>
</form></body></html>"""


@app.middleware("http")
async def _passcode_gate(request: Request, call_next):
    passcode = get_settings().app_passcode.strip()
    path = request.url.path
    if not passcode or path == "/login" or _is_authed(request, passcode):
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
