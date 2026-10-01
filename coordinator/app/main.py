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

from app.api import health, jobs, library, notes, workers
from app.core.config import get_settings, resolve_secret_key
from app.db.models import Job
from app.db.session import get_sessionmaker
from app.services import drain, scheduler
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

# Pages here use inline <script>/handlers, so script-src keeps 'unsafe-inline';
# this CSP therefore does not stop injected inline script by itself — the XSS fix is
# that user data never reaches JS code (see tests/test_xss.py). What it does buy:
# no framing (clickjacking), no plugins, no <base> hijack, forms/XHR/media/images
# only to this origin. Swagger UI (/docs) loads assets from a CDN, so it is exempt.
_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
_CSP_EXEMPT = ("/docs", "/redoc", "/openapi.json")


def _add_security_headers(request: Request, response) -> None:
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith(_CSP_EXEMPT):
        h.setdefault("Content-Security-Policy", _CSP)


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

                # Agents that stopped heartbeating are no longer "online".
                if await scheduler.mark_stale_workers(
                    session, get_settings().worker_offline_seconds
                ):
                    changed = True

                # A draining server is removed once every video it holds has a copy elsewhere.
                if await drain.finalize_drained_workers(session):
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
        _add_security_headers(request, response)
        return response

    # Attach the current user to request.state from the session cookie. Added
    # before SessionMiddleware so that (add_middleware stacks last-added
    # outermost) SessionMiddleware wraps it and request.session is available.
    async def _attach_user(request: Request, call_next):
        await load_current_user(request)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=_attach_user)
    secret_key, ephemeral = resolve_secret_key(settings.secret_key)
    if ephemeral:
        log.warning(
            "VIDHIVE_SECRET_KEY is unset or a known placeholder: using a random key for this "
            "process, so everyone is signed out on every restart. Set a long random value."
        )
    app.add_middleware(SessionMiddleware, secret_key=secret_key, same_site="lax")

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
    app.include_router(notes.router)

    # Web UI
    static_dir = Path(__file__).resolve().parent / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(web_router)

    return app


app = create_app()
