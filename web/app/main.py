"""Vidhive web front-end.

Server-rendered with Jinja2; all state comes from the coordinator API. HTMX (a
small helper loaded in the template) will drive live updates in later stages —
the application logic stays entirely in Python.
"""

import logging
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import get_settings

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Vidhive Web", version="0.1.0")
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    async def _coordinator_get(path: str):
        url = f"{settings.coordinator_url}{path}"
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "web"}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        coordinator_ok = True
        jobs: list = []
        workers: list = []
        try:
            jobs = await _coordinator_get("/api/jobs")
            workers = await _coordinator_get("/api/workers")
        except Exception as exc:  # noqa: BLE001 - surface any coordinator problem in the UI
            coordinator_ok = False
            logging.warning("coordinator unreachable: %s", exc)

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "coordinator_ok": coordinator_ok,
                "coordinator_url": settings.coordinator_url,
                "jobs": jobs,
                "workers": workers,
            },
        )

    return app


app = create_app()
