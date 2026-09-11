"""Coordinator FastAPI application — the control centre of Vidhive."""

import logging

from fastapi import FastAPI

from app.api import health, jobs, workers
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Vidhive Coordinator", version="0.1.0")
    app.include_router(health.router)
    app.include_router(jobs.router)
    app.include_router(workers.router)
    return app


app = create_app()
