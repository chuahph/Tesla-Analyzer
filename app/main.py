"""FastAPI application entry point."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
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
