"""Server-rendered web UI, served by the coordinator itself.

The dashboard reads the database directly. Video streaming is proxied: the
coordinator looks up which agent holds a file (item.worker_id -> worker.agent_url)
and streams the bytes from that agent to the browser, forwarding Range requests
so seeking works.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.jobs import (
    create_job,
    pause_job,
    resume_job,
    retry_failed,
    start_job,
    stop_job,
)
from app.db.models import FileRecord, Item, Job, MediaMetadata, Worker, WorkerMetric
from app.db.session import get_session
from vidhive_common.enums import ItemStatus
from vidhive_common.schemas import JobCreate

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _human_size(num: int | None) -> str:
    """Readable size in binary units — a 256 KB file must not read as '0 МБ'."""
    if not num:
        return "—"
    value = float(num)
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    for index, unit in enumerate(units):
        if value < 1024 or index == len(units) - 1:
            return f"{value:.0f} {unit}" if index == 0 else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def _exact_bytes(num: int | None) -> str:
    """Exact byte count with thousands separators, shown next to the rounded size."""
    if not num:
        return ""
    return f"{num:,}".replace(",", " ") + " Б"


templates.env.filters["hsize"] = _human_size
templates.env.filters["bexact"] = _exact_bytes

router = APIRouter(tags=["web"])

_KINESCOPE_ID = re.compile(r"(\d+)")

# Actions the jobs page can trigger, mapped to the reused API handlers.
_JOB_ACTIONS = {
    "start": start_job,
    "pause": pause_job,
    "resume": resume_job,
    "stop": stop_job,
    "retry": retry_failed,
}


def _page(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


_SPEED_WINDOW_SECONDS = 60
_DONE_STATUSES = (ItemStatus.COMPLETED.value,)
_OPEN_STATUSES = (ItemStatus.PENDING.value, ItemStatus.CHECKING.value)


async def _dashboard_context(session: AsyncSession) -> dict:
    """Progress, speed, active downloads and totals (acceptance criterion 10)."""
    jobs = (await session.execute(select(Job).order_by(Job.id.desc()))).scalars().all()
    workers = (await session.execute(select(Worker).order_by(Worker.name))).scalars().all()

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_SPEED_WINDOW_SECONDS)
    rows = []
    for job in jobs:
        processed = (
            await session.execute(
                select(func.count())
                .select_from(Item)
                .where(Item.job_id == job.id)
                .where(Item.status.not_in(_OPEN_STATUSES))
            )
        ).scalar_one()
        downloaded = (
            await session.execute(
                select(func.count())
                .select_from(Item)
                .where(Item.job_id == job.id)
                .where(Item.status.in_(_DONE_STATUSES))
            )
        ).scalar_one()
        recent = (
            await session.execute(
                select(func.count())
                .select_from(Item)
                .where(Item.job_id == job.id)
                .where(Item.checked_at.is_not(None))
                .where(Item.checked_at >= cutoff)
            )
        ).scalar_one()

        total = None
        if job.range_end is not None and job.range_start is not None:
            total = job.range_end - job.range_start + 1
        rows.append(
            {
                "job": job,
                "total": total,
                "processed": processed,
                "downloaded": downloaded,
                "percent": round(processed * 100 / total, 1) if total else None,
                "speed": round(recent / _SPEED_WINDOW_SECONDS, 2),
                # Remaining time only means something for a finite range.
                "eta": (
                    round((total - processed) / (recent / _SPEED_WINDOW_SECONDS))
                    if total and recent and processed < total
                    else None
                ),
            }
        )

    # Latest metric sample per worker, for live load figures.
    metrics: dict[int, WorkerMetric | None] = {}
    for w in workers:
        metrics[w.id] = (
            await session.execute(
                select(WorkerMetric)
                .where(WorkerMetric.worker_id == w.id)
                .order_by(WorkerMetric.captured_at.desc())
                .limit(1)
            )
        ).scalars().first()

    active_downloads = sum((m.active_downloads or 0) for m in metrics.values() if m)
    active_checks = sum((m.active_checks or 0) for m in metrics.values() if m)
    total_bytes = (
        await session.execute(select(func.coalesce(func.sum(FileRecord.size_bytes), 0)))
    ).scalar_one()

    return {
        "rows": rows,
        "workers": list(workers),
        "metrics": metrics,
        "active_downloads": active_downloads,
        "active_checks": active_checks,
        "total_bytes": total_bytes,
        "online": sum(1 for w in workers if w.state == "online"),
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    return _page(request, "index.html", await _dashboard_context(session))


@router.get("/fragments/dashboard", response_class=HTMLResponse)
async def dashboard_fragment(request: Request, session: AsyncSession = Depends(get_session)):
    """Polled by HTMX so live figures refresh without a page reload."""
    return _page(request, "_dashboard.html", await _dashboard_context(session))


@router.get("/servers", response_class=HTMLResponse)
async def servers(request: Request, session: AsyncSession = Depends(get_session)):
    workers = (await session.execute(select(Worker).order_by(Worker.name))).scalars().all()
    metrics: dict[int, WorkerMetric | None] = {}
    for w in workers:
        metrics[w.id] = (
            await session.execute(
                select(WorkerMetric)
                .where(WorkerMetric.worker_id == w.id)
                .order_by(WorkerMetric.captured_at.desc())
                .limit(1)
            )
        ).scalars().first()
    return _page(request, "servers.html", {"workers": list(workers), "metrics": metrics})


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, session: AsyncSession = Depends(get_session)):
    jobs = (await session.execute(select(Job).order_by(Job.id.desc()))).scalars().all()
    return _page(request, "jobs.html", {"jobs": list(jobs)})


@router.post("/jobs/{job_id}/{action}")
async def job_action(job_id: int, action: str, session: AsyncSession = Depends(get_session)):
    handler = _JOB_ACTIONS.get(action)
    if handler is None:
        raise HTTPException(status_code=404, detail="unknown action")
    try:
        await handler(job_id, session)
    except HTTPException:
        raise
    return RedirectResponse(url="/jobs", status_code=303)


@router.get("/add", response_class=HTMLResponse)
async def add_form(request: Request):
    return _page(request, "add.html", {})


@router.post("/add")
async def add_submit(
    mode: str = Form(...),
    value: str = Form(""),
    name: str = Form(""),
    chunk_size: int = Form(5000),
    session: AsyncSession = Depends(get_session),
):
    if mode == "single":
        match = _KINESCOPE_ID.search(value or "")
        if not match:
            raise HTTPException(status_code=422, detail="не найден числовой ID в значении")
        ident = int(match.group(1))
        payload = JobCreate(
            name=name or f"single {ident}", range_start=ident, range_end=ident, chunk_size=1
        )
    else:  # range
        payload = JobCreate(name=name or None, range_spec=value or "", chunk_size=chunk_size)

    job = await create_job(payload, session)
    await start_job(job.id, session)
    return RedirectResponse(url="/jobs", status_code=303)


@router.get("/library", response_class=HTMLResponse)
async def library(request: Request, q: str = "", session: AsyncSession = Depends(get_session)):
    # The library is a catalogue of materials, not of job items: the same video
    # picked up by two overlapping jobs must appear once. Keep the newest record
    # for each external identifier.
    latest = (
        select(func.max(Item.id).label("item_id"))
        .where(Item.status == ItemStatus.COMPLETED.value)
        .group_by(Item.external_id)
        .subquery()
    )
    stmt = (
        select(Item, MediaMetadata, Worker)
        .join(latest, latest.c.item_id == Item.id)
        .join(MediaMetadata, MediaMetadata.item_id == Item.id, isouter=True)
        .join(Worker, Worker.id == Item.worker_id, isouter=True)
        .order_by(Item.updated_at.desc())
    )
    q = q.strip()
    if q:
        filters = [MediaMetadata.title.ilike(f"%{q}%")]
        if q.isdigit():
            filters.append(Item.external_id == int(q))
        stmt = stmt.where(or_(*filters))

    rows = (await session.execute(stmt)).all()
    items = [
        {
            "id": item.id,
            "external_id": item.external_id,
            "title": (meta.title if meta else None) or f"ID {item.external_id}",
            "size_bytes": meta.size_bytes if meta else None,
            "worker": worker.name if worker else None,
        }
        for item, meta, worker in rows
    ]
    return _page(request, "library.html", {"items": items, "q": q})


@router.get("/player/{item_id}", response_class=HTMLResponse)
async def player(item_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """A real player page; the <video> element streams from /watch/{id}."""
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    meta = (
        await session.execute(select(MediaMetadata).where(MediaMetadata.item_id == item.id))
    ).scalars().first()
    worker = await session.get(Worker, item.worker_id) if item.worker_id else None
    return _page(
        request,
        "watch.html",
        {
            "item_id": item.id,
            "external_id": item.external_id,
            "title": (meta.title if meta else None) or f"ID {item.external_id}",
            "size_bytes": meta.size_bytes if meta else None,
            "worker": worker.name if worker else None,
        },
    )


_STREAM_HEADERS = ("content-type", "content-length", "accept-ranges", "content-range")


@router.get("/watch/{item_id}")
async def watch(item_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Proxy the video from the agent that holds it, forwarding Range requests."""
    item = await session.get(Item, item_id)
    if item is None or item.worker_id is None:
        raise HTTPException(status_code=404, detail="item not available")
    worker = await session.get(Worker, item.worker_id)
    if worker is None or not worker.agent_url:
        raise HTTPException(status_code=404, detail="owning agent unknown")

    url = f"{worker.agent_url.rstrip('/')}/files/{item.external_id}"
    fwd = {"Range": request.headers["range"]} if "range" in request.headers else {}

    client = httpx.AsyncClient(timeout=None)
    try:
        resp = await client.send(client.build_request("GET", url, headers=fwd), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"agent unreachable: {exc}") from exc

    async def body():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    headers = {k: resp.headers[k] for k in _STREAM_HEADERS if k in resp.headers}
    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)
