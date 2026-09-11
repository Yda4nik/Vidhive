"""Server-rendered web UI, served by the coordinator itself.

The dashboard reads the database directly. Video streaming is proxied: the
coordinator looks up which agent holds a file (item.worker_id -> worker.agent_url)
and streams the bytes from that agent to the browser, forwarding Range requests
so seeking works.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.jobs import create_job, pause_job, resume_job, start_job, stop_job
from app.db.models import Item, Job, MediaMetadata, Worker
from app.db.session import get_session
from vidhive_common.enums import ItemStatus
from vidhive_common.schemas import JobCreate

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

router = APIRouter(tags=["web"])

_KINESCOPE_ID = re.compile(r"(\d+)")

# Actions the jobs page can trigger, mapped to the reused API handlers.
_JOB_ACTIONS = {
    "start": start_job,
    "pause": pause_job,
    "resume": resume_job,
    "stop": stop_job,
}


def _page(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    jobs = (await session.execute(select(Job).order_by(Job.id.desc()))).scalars().all()
    workers = (await session.execute(select(Worker).order_by(Worker.name))).scalars().all()
    return _page(request, "index.html", {"jobs": list(jobs), "workers": list(workers)})


@router.get("/servers", response_class=HTMLResponse)
async def servers(request: Request, session: AsyncSession = Depends(get_session)):
    workers = (await session.execute(select(Worker).order_by(Worker.name))).scalars().all()
    return _page(request, "servers.html", {"workers": list(workers)})


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
    stmt = (
        select(Item, MediaMetadata, Worker)
        .join(MediaMetadata, MediaMetadata.item_id == Item.id, isouter=True)
        .join(Worker, Worker.id == Item.worker_id, isouter=True)
        .where(Item.status == ItemStatus.COMPLETED.value)
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
