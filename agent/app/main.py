"""Vidhive agent — backend worker that checks identifiers and downloads files.

On startup it registers with the coordinator and runs the lease loop in the
background. It also serves downloaded files from local storage so the
coordinator/web can stream them.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.checker import Checker, target_dir
from app.coordinator_client import CoordinatorClient
from app.core.config import get_settings
from app.ratelimit import AsyncRateLimiter
from app.worker import WorkerRunner


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    http = httpx.AsyncClient(timeout=settings.check_timeout)
    client = CoordinatorClient(settings.coordinator_url)
    limiter = AsyncRateLimiter(settings.rate_limit_rps)
    runner = WorkerRunner(settings, client, Checker(settings, http, limiter=limiter))
    app.state.runner = runner
    task = asyncio.create_task(runner.run())
    try:
        yield
    finally:
        runner.stop()
        task.cancel()
        await http.aclose()
        await client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Vidhive Agent", version="0.3.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        runner: WorkerRunner | None = getattr(app.state, "runner", None)
        return {
            "status": "ok",
            "service": "agent",
            "worker_name": settings.worker_name,
            "worker_id": getattr(runner, "worker_id", None),
            "coordinator_url": settings.coordinator_url,
        }

    @app.get("/files/{external_id}")
    async def get_file(external_id: int):
        """Serve the downloaded video for streaming by the coordinator/web."""
        directory = target_dir(settings.storage_path, external_id)
        for candidate in sorted(directory.glob("video.*")):
            if candidate.suffix != ".part":
                return FileResponse(candidate)
        raise HTTPException(status_code=404, detail="file not found on this agent")

    return app


app = create_app()
