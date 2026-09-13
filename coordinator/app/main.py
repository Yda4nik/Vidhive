"""Coordinator FastAPI application — control centre and web UI of Vidhive.

Serves both the REST API (/api/*, /health) and the server-rendered dashboard (/).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.requests import Request

from app.api import health, jobs, library, workers
from app.core.config import get_settings
from app.db.models import Job
from app.db.session import get_sessionmaker
from app.services import scheduler
from app.services.bus import bus
from app.web.router import router as web_router

log = logging.getLogger("vidhive.coordinator")
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_SWEEP_INTERVAL = 15  # seconds


async def _completion_sweeper() -> None:
    """Safety net: finish any running job whose work is fully done.

    Chunk-completion events normally trigger completion, but a rare race (the
    last chunks finishing at once) can leave a job stuck at 100%. This sweep
    catches it.
    """
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL)
        try:
            async with get_sessionmaker()() as session:
                running = (
                    await session.execute(
                        select(Job.id).where(Job.state == "running")
                    )
                ).scalars().all()
                changed = False
                for jid in running:
                    if await scheduler.maybe_complete_job(session, jid):
                        changed = True
                if changed:
                    await session.commit()
                    bus.publish()
                else:
                    await session.rollback()
        except Exception as exc:  # noqa: BLE001 - a sweep failure must not kill the loop
            log.debug("completion sweep failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_completion_sweeper())
    try:
        yield
    finally:
        task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(title="Vidhive Coordinator", version="0.2.0", lifespan=lifespan)

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
