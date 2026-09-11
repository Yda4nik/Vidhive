"""Vidhive agent — backend worker that checks identifiers and downloads files.

Stage 1 is a skeleton: it boots, exposes health, and knows how to reach the
coordinator. Registration, the lease loop and the two-phase check/download
pipeline arrive in stage 3.
"""

import logging

from fastapi import FastAPI

from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Vidhive Agent", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "service": "agent",
            "worker_name": settings.worker_name,
            "coordinator_url": settings.coordinator_url,
        }

    return app


app = create_app()
