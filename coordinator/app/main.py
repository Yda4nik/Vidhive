"""Coordinator FastAPI application — control centre and web UI of Vidhive.

Serves both the REST API (/api/*, /health) and the server-rendered dashboard (/).
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app.api import health, jobs, library, workers
from app.core.config import get_settings
from app.db.models import Job
from app.db.session import get_sessionmaker
from app.services import scheduler
from app.services.auth import AccessError, load_current_user
from app.services.bus import bus
from app.web.router import router as web_router

log = logging.getLogger("vidhive.coordinator")
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# High-frequency agent polling that must NOT trigger a real-time refresh — the
# meaningful change (progress/complete) publishes on its own.
_NO_PUBLISH = ("/lease", "/heartbeat")
_SWEEP_INTERVAL = 15  # seconds
_METRICS_RETENTION_MINUTES = 30


async def _completion_sweeper() -> None:
    """Safety net: finish any running job whose work is fully done.

    Chunk-completion events normally trigger completion, but a rare race (the
    last chunks finishing at once) can leave a job stuck at 100%. This sweep
    catches it.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from app.db.models import WorkerMetric

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

                # Bound the metrics table so its "latest per worker" query stays fast.
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=_METRICS_RETENTION_MINUTES)
                await session.execute(
                    delete(WorkerMetric).where(WorkerMetric.captured_at < cutoff)
                )

                await session.commit()
                if changed:
                    bus.publish()
        except Exception as exc:  # noqa: BLE001 - a sweep failure must not kill the loop
            log.debug("maintenance sweep failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    async with get_sessionmaker()() as session:
        from app.services import auth

        await auth.seed_roles(session)
        await auth.ensure_bootstrap_admin(session, settings.admin_user, settings.admin_password)
        await session.commit()
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
        if (
            request.method in _MUTATING
            and response.status_code < 400
            and not request.url.path.endswith(_NO_PUBLISH)
        ):
            bus.publish()
        return response

    # Attach the current user to request.state from the session cookie. Added
    # before SessionMiddleware so that (add_middleware stacks last-added
    # outermost) SessionMiddleware wraps it and request.session is available.
    async def _attach_user(request: Request, call_next):
        await load_current_user(request)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=_attach_user)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")

    @app.exception_handler(AccessError)
    async def _access_error(request: Request, exc: AccessError):
        if request.url.path.startswith("/api"):
            detail = "forbidden" if exc.status_code == 403 else "unauthorized"
            return JSONResponse({"detail": detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
        return HTMLResponse("<h1>403 — недостаточно прав</h1>", status_code=403)

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
