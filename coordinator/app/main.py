"""Coordinator FastAPI application — control centre and web UI of Vidhive.

Serves both the REST API (/api/*, /health) and the server-rendered dashboard (/).
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from starlette.requests import Request

from app.api import health, jobs, library, workers
from app.core.config import get_settings
from app.services.bus import bus
from app.web.router import router as web_router

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Vidhive Coordinator", version="0.2.0")

    @app.middleware("http")
    async def _broadcast_changes(request: Request, call_next):
        """Push a real-time 'update' after any successful state change."""
        response = await call_next(request)
        if request.method in _MUTATING and response.status_code < 400:
            bus.publish()
        return response

    # API
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(workers.router)
    app.include_router(library.router)

    # Web UI
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(web_router)

    return app


app = create_app()
