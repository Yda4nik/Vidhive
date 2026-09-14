"""Server-rendered web UI, served by the coordinator itself.

The dashboard reads the database directly. Video streaming is proxied: the
coordinator looks up which agent holds a file (item.worker_id -> worker.agent_url)
and streams the bytes from that agent to the browser, forwarding Range requests
so seeking works.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.bus import bus

from app.api.jobs import (
    create_job,
    pause_job,
    resume_job,
    retry_failed,
    start_job,
    stop_job,
)
from app.api.library import get_or_create_favorites
from app.api.workers import delete_worker
from app.db.models import RoleModel, User, UserRole
from app.services.auth import ROLE_RANK, require_role, role_of, seed_roles
from app.services.security import hash_password, verify_password
from app.core.sources import NUMERIC_SOURCES, SUPPORTED_MODES, source_from_url
from app.db.models import (
    FileRecord,
    Group,
    Item,
    ItemGroup,
    Job,
    JobTarget,
    MediaMetadata,
    Worker,
    WorkerMetric,
)
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


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_role("viewer"))])
async def dashboard(request: Request, session: AsyncSession = Depends(get_session)):
    return _page(request, "index.html", await _dashboard_context(session))


@router.get("/fragments/dashboard", response_class=HTMLResponse,
            dependencies=[Depends(require_role("viewer"))])
async def dashboard_fragment(request: Request, session: AsyncSession = Depends(get_session)):
    """The live dashboard body, re-fetched on a real-time event."""
    return _page(request, "_dashboard.html", await _dashboard_context(session))


async def _servers_context(session: AsyncSession) -> dict:
    """Full per-server detail for the management page."""
    workers = (await session.execute(select(Worker).order_by(Worker.name))).scalars().all()
    metrics: dict[int, WorkerMetric | None] = {}
    videos: dict[int, int] = {}
    used_bytes: dict[int, int] = {}
    for w in workers:
        metrics[w.id] = (
            await session.execute(
                select(WorkerMetric)
                .where(WorkerMetric.worker_id == w.id)
                .order_by(WorkerMetric.captured_at.desc())
                .limit(1)
            )
        ).scalars().first()
        videos[w.id] = (
            await session.execute(
                select(func.count())
                .select_from(Item)
                .where(Item.worker_id == w.id)
                .where(Item.status == ItemStatus.COMPLETED.value)
            )
        ).scalar_one()
        used_bytes[w.id] = (
            await session.execute(
                select(func.coalesce(func.sum(FileRecord.size_bytes), 0)).where(
                    FileRecord.worker_name == w.name
                )
            )
        ).scalar_one()
    online = sum(1 for w in workers if w.state == "online")
    return {
        "workers": list(workers),
        "metrics": metrics,
        "videos": videos,
        "used_bytes": used_bytes,
        # Redistribution needs at least one other online server to receive the work.
        "can_redistribute": online > 1,
    }


@router.get("/servers", response_class=HTMLResponse,
            dependencies=[Depends(require_role("administrator"))])
async def servers_page(
    request: Request, error: str = "", session: AsyncSession = Depends(get_session)
):
    ctx = await _servers_context(session)
    ctx["error"] = error
    return _page(request, "servers.html", ctx)


@router.get("/fragments/servers", response_class=HTMLResponse,
            dependencies=[Depends(require_role("administrator"))])
async def servers_fragment(request: Request, session: AsyncSession = Depends(get_session)):
    return _page(request, "_servers_table.html", await _servers_context(session))


@router.post("/servers/{worker_id}/delete",
             dependencies=[Depends(require_role("administrator"))])
async def server_delete(
    worker_id: int,
    mode: str = Form("purge"),
    session: AsyncSession = Depends(get_session),
):
    try:
        await delete_worker(worker_id, mode, session)
    except HTTPException as exc:
        return RedirectResponse(url=f"/servers?error={quote(str(exc.detail))}", status_code=303)
    return RedirectResponse(url="/servers", status_code=303)


async def _user_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request, session: AsyncSession = Depends(get_session)):
    # First-run wizard: only available until the first user exists.
    if await _user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    return _page(request, "setup.html", {"error": ""})


@router.post("/setup")
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if await _user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    if not username.strip() or not password:
        return _page(request, "setup.html", {"error": "Заполните логин и пароль"})
    if password != confirm:
        return _page(request, "setup.html", {"error": "Пароли не совпадают"})
    await seed_roles(session)
    user = User(username=username.strip(), password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    rid = (
        await session.execute(select(RoleModel.id).where(RoleModel.name == "administrator"))
    ).scalar_one()
    session.add(UserRole(user_id=user.id, role_id=rid))
    await session.commit()
    request.session["user_id"] = user.id  # log the new admin straight in
    return RedirectResponse("/", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request, next: str = "/", error: str = "", session: AsyncSession = Depends(get_session)
):
    if await _user_count(session) == 0:
        return RedirectResponse("/setup", status_code=303)
    return _page(request, "login.html", {"next": next, "error": error})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: AsyncSession = Depends(get_session),
):
    user = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return _page(request, "login.html", {"next": next, "error": "Неверный логин или пароль"})
    request.session["user_id"] = user.id
    return RedirectResponse(url=next or "/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


async def _users_context(session: AsyncSession) -> dict:
    users = (await session.execute(select(User).order_by(User.username))).scalars().all()
    rows = [{"id": u.id, "username": u.username, "role": await role_of(session, u.id)} for u in users]
    return {"users": rows, "roles": list(ROLE_RANK.keys())}


async def _admin_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(UserRole)
            .join(RoleModel, RoleModel.id == UserRole.role_id)
            .where(RoleModel.name == "administrator")
        )
    ).scalar_one()


async def _set_role(session: AsyncSession, user_id: int, role: str) -> None:
    await session.execute(UserRole.__table__.delete().where(UserRole.user_id == user_id))
    rid = (await session.execute(select(RoleModel.id).where(RoleModel.name == role))).scalar_one()
    session.add(UserRole(user_id=user_id, role_id=rid))


@router.get("/users", response_class=HTMLResponse,
            dependencies=[Depends(require_role("administrator"))])
async def users_page(request: Request, error: str = "", session: AsyncSession = Depends(get_session)):
    ctx = await _users_context(session)
    ctx["error"] = error
    return _page(request, "users.html", ctx)


@router.post("/users", dependencies=[Depends(require_role("administrator"))])
async def users_create(
    username: str = Form(...), password: str = Form(...), role: str = Form("viewer"),
    session: AsyncSession = Depends(get_session),
):
    if role not in ROLE_RANK:
        return RedirectResponse("/users?error=неизвестная+роль", status_code=303)
    exists = (
        await session.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if exists is not None:
        return RedirectResponse("/users?error=логин+занят", status_code=303)
    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    await _set_role(session, user.id, role)
    await session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role", dependencies=[Depends(require_role("administrator"))])
async def users_set_role(
    user_id: int, role: str = Form(...), session: AsyncSession = Depends(get_session)
):
    if role not in ROLE_RANK:
        return RedirectResponse("/users?error=неизвестная+роль", status_code=303)
    current = await role_of(session, user_id)
    if current == "administrator" and role != "administrator" and await _admin_count(session) <= 1:
        return RedirectResponse("/users?error=нельзя+снять+последнего+админа", status_code=303)
    await _set_role(session, user_id, role)
    await session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/password", dependencies=[Depends(require_role("administrator"))])
async def users_set_password(
    user_id: int, password: str = Form(...), session: AsyncSession = Depends(get_session)
):
    user = await session.get(User, user_id)
    if user is not None:
        user.password_hash = hash_password(password)
        await session.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete", dependencies=[Depends(require_role("administrator"))])
async def users_delete(
    request: Request, user_id: int, session: AsyncSession = Depends(get_session)
):
    me = request.state.user
    if me is not None and me.id == user_id:
        return RedirectResponse("/users?error=нельзя+удалить+себя", status_code=303)
    if await role_of(session, user_id) == "administrator" and await _admin_count(session) <= 1:
        return RedirectResponse("/users?error=нельзя+удалить+последнего+админа", status_code=303)
    user = await session.get(User, user_id)
    if user is not None:
        await session.delete(user)
        await session.commit()
    return RedirectResponse("/users", status_code=303)


@router.get("/events/stream", dependencies=[Depends(require_role("viewer"))])
async def events_stream(request: Request):
    """Server-Sent Events: pushes an 'update' the instant state changes."""

    async def gen():
        q = bus.register()
        idle = 0
        try:
            yield "retry: 3000\n\n"          # client reconnect backoff
            while True:
                # Poll for a client disconnect every second so a navigated-away
                # connection is released fast (it holds one of the browser's ~6
                # per-host slots — lingering ones stall the next page load).
                if await request.is_disconnected():
                    break
                try:
                    await asyncio.wait_for(q.get(), timeout=1)
                    yield "data: update\n\n"
                    idle = 0
                except asyncio.TimeoutError:
                    idle += 1
                    if idle >= 15:           # keepalive roughly every 15s
                        yield ": keepalive\n\n"
                        idle = 0
        finally:
            bus.unregister(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


_ACTIVE_JOB_STATES = ("validating", "running", "pausing", "paused", "stopping")


async def _active_job_rows(session: AsyncSession) -> list[dict]:
    """Active jobs with per-job progress (processed / total, %, speed, downloaded)."""
    jobs = (
        await session.execute(
            select(Job).where(Job.state.in_(_ACTIVE_JOB_STATES)).order_by(Job.id.desc())
        )
    ).scalars().all()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_SPEED_WINDOW_SECONDS)
    rows = []
    for job in jobs:
        targets = (
            await session.execute(select(JobTarget).where(JobTarget.job_id == job.id))
        ).scalars().all()
        if targets:
            spans = [t for t in targets if t.range_start is not None and t.range_end is not None]
            open_ended = any(t.range_end is None for t in targets)
            total = None if open_ended else sum(t.range_end - t.range_start + 1 for t in spans) or None
        elif job.range_end is not None and job.range_start is not None:
            total = job.range_end - job.range_start + 1
        else:
            total = None
        processed = (
            await session.execute(
                select(func.count()).select_from(Item)
                .where(Item.job_id == job.id).where(Item.status.not_in(_OPEN_STATUSES))
            )
        ).scalar_one()
        downloaded = (
            await session.execute(
                select(func.count()).select_from(Item)
                .where(Item.job_id == job.id).where(Item.status == ItemStatus.COMPLETED.value)
            )
        ).scalar_one()
        recent = (
            await session.execute(
                select(func.count()).select_from(Item)
                .where(Item.job_id == job.id)
                .where(Item.checked_at.is_not(None)).where(Item.checked_at >= cutoff)
            )
        ).scalar_one()
        rows.append({
            "job": job,
            "total": total,
            "processed": processed,
            "downloaded": downloaded,
            "percent": round(processed * 100 / total, 1) if total else None,
            "speed": round(recent / _SPEED_WINDOW_SECONDS, 2),
        })
    return rows


async def _failed_item_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Item).where(
                Item.status.in_(("failed", "rate_limited", "retry_wait"))
            )
        )
    ).scalar_one()


@router.get("/fragments/jobs", response_class=HTMLResponse,
            dependencies=[Depends(require_role("viewer"))])
async def jobs_fragment(request: Request, session: AsyncSession = Depends(get_session)):
    return _page(request, "_jobs_table.html", {"rows": await _active_job_rows(session)})


async def _all_jobs_brief(session: AsyncSession) -> list[dict]:
    jobs = (await session.execute(select(Job).order_by(Job.id.desc()))).scalars().all()
    return [{"id": j.id, "name": j.name} for j in jobs]


@router.get("/jobs", response_class=HTMLResponse, dependencies=[Depends(require_role("viewer"))])
async def jobs_page(request: Request, error: str = "", session: AsyncSession = Depends(get_session)):
    groups = (
        await session.execute(select(Group).where(Group.kind == "user").order_by(Group.id))
    ).scalars().all()
    return _page(request, "jobs.html", {
        "rows": await _active_job_rows(session),
        "failed_count": await _failed_item_count(session),
        "all_jobs": await _all_jobs_brief(session),
        "groups": [{"id": g.id, "name": g.name} for g in groups],
        "error": error,
    })


@router.post("/jobs/{job_id}/{action}", dependencies=[Depends(require_role("operator"))])
async def job_action(job_id: int, action: str, session: AsyncSession = Depends(get_session)):
    handler = _JOB_ACTIONS.get(action)
    if handler is None:
        raise HTTPException(status_code=404, detail="unknown action")
    try:
        await handler(job_id, session)
    except HTTPException:
        raise
    return RedirectResponse(url="/jobs", status_code=303)


def _build_targets(source, mode, values, range_from, range_to):
    """Return (resolved_source, [(range_start, range_end, url), ...]) or raise ValueError."""
    values = [v.strip() for v in values if v.strip()]
    targets: list[tuple] = []

    if source == "youtube":
        raise ValueError("YouTube пока не поддерживается")
    if source in SUPPORTED_MODES and mode not in SUPPORTED_MODES[source]:
        raise ValueError(f"Источник «{source}» не поддерживает режим «{mode}»")

    if mode == "id":
        if source not in NUMERIC_SOURCES:
            raise ValueError("Для режима «id» выберите источник kinescope или mock")
        for v in values:
            if not v.isdigit():
                raise ValueError(f"«{v}» — это не число")
            targets.append((int(v), int(v), None))
        resolved = source

    elif mode == "range":
        if source not in NUMERIC_SOURCES:
            raise ValueError("Диапазон работает с kinescope или mock")
        pairs = list(zip(range_from, range_to))
        for f, t in pairs:
            f, t = f.strip(), t.strip()
            if not f and not t:
                continue
            a = int(f) if f.isdigit() else 0
            b = int(t) if t.isdigit() else None
            if b is not None and b < a:
                raise ValueError(f"В диапазоне {a}–{t} конец меньше начала")
            targets.append((a, b, None))
        resolved = source

    elif mode == "link":
        resolved = None
        for url in values:
            src = source if source != "auto" else (source_from_url(url) or "")
            if src == "youtube":
                raise ValueError("YouTube пока не поддерживается")
            if src != "kinescope":
                raise ValueError(f"Не удалось определить источник для «{url}»")
            match = _KINESCOPE_ID.search(url)
            if not match:
                raise ValueError(f"В ссылке «{url}» нет числового ID")
            ident = int(match.group(1))
            targets.append((ident, ident, None))
            resolved = "kinescope"
    else:
        raise ValueError(f"Неизвестный режим «{mode}»")

    if not targets:
        raise ValueError("Заполните хотя бы одно поле ввода")
    open_ended = [t for t in targets if t[1] is None]
    if open_ended and len(targets) > 1:
        raise ValueError("Бесконечный диапазон должен быть единственной целью")
    return resolved, targets


@router.post("/add", dependencies=[Depends(require_role("operator"))])
async def add_submit(
    source: str = Form("auto"),
    mode: str = Form("link"),
    value: list[str] = Form(default=[]),
    range_from: list[str] = Form(default=[]),
    range_to: list[str] = Form(default=[]),
    name: str = Form(""),
    group_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    try:
        resolved_source, targets = _build_targets(source, mode, value, range_from, range_to)
    except ValueError as exc:
        return RedirectResponse(url=f"/jobs?error={quote(str(exc))}", status_code=303)

    job = Job(
        name=name.strip() or None,
        source=resolved_source,
        target_group_id=int(group_id) if group_id.isdigit() else None,
    )
    session.add(job)
    await session.flush()
    for a, b, url in targets:
        session.add(JobTarget(job_id=job.id, range_start=a, range_end=b, url=url))
    await session.commit()
    await start_job(job.id, session)
    return RedirectResponse(url="/jobs", status_code=303)


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_SORT_COLUMNS = {
    "date": Item.updated_at,
    "name": MediaMetadata.title,
    "duration": MediaMetadata.duration_seconds,
    "size": MediaMetadata.size_bytes,
    "server": Worker.name,
}


@router.get("/library", response_class=HTMLResponse, dependencies=[Depends(require_role("viewer"))])
async def library(
    request: Request,
    q: str = "",
    group: str = "all",
    source: str = "",
    server: str = "",
    sort: str = "date",
    order: str = "desc",
    session: AsyncSession = Depends(get_session),
):
    fav = await get_or_create_favorites(session)
    await session.commit()

    # Newest completed item per external identifier (a video appears once).
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
    )

    q = q.strip()
    if q:
        filters = [MediaMetadata.title.ilike(f"%{q}%")]
        if q.isdigit():
            filters.append(Item.external_id == int(q))
        stmt = stmt.where(or_(*filters))
    if source:
        stmt = stmt.where(MediaMetadata.source == source)
    if server:
        stmt = stmt.where(Worker.name == server)

    # Group filter: "all" = everything, "fav" = favourites, or a numeric group id.
    filter_group_id = None
    if group == "fav":
        filter_group_id = fav.id
    elif group.isdigit():
        filter_group_id = int(group)
    if filter_group_id is not None:
        member = select(ItemGroup.item_id).where(ItemGroup.group_id == filter_group_id).subquery()
        stmt = stmt.join(member, member.c.item_id == Item.id)

    column = _SORT_COLUMNS.get(sort, Item.updated_at)
    stmt = stmt.order_by(column.asc() if order == "asc" else column.desc())

    rows = (await session.execute(stmt)).all()

    fav_ids = set(
        (
            await session.execute(select(ItemGroup.item_id).where(ItemGroup.group_id == fav.id))
        ).scalars().all()
    )
    items = [
        {
            "id": item.id,
            "external_id": item.external_id,
            "title": (meta.title if meta else None) or f"ID {item.external_id}",
            "size_bytes": meta.size_bytes if meta else None,
            "duration": _fmt_duration(meta.duration_seconds if meta else None),
            "source": (meta.source if meta else None) or "—",
            "worker": worker.name if worker else None,
            "fav": item.id in fav_ids,
        }
        for item, meta, worker in rows
    ]

    # Tabs: groups with counts; distinct sources for the source dropdown.
    all_groups = (await session.execute(select(Group).order_by(Group.id))).scalars().all()
    groups = []
    for g in all_groups:
        cnt = (
            await session.execute(
                select(func.count()).select_from(ItemGroup).where(ItemGroup.group_id == g.id)
            )
        ).scalar_one()
        groups.append({"id": g.id, "name": g.name, "kind": g.kind, "count": cnt})
    sources = [
        s for s in (
            await session.execute(
                select(MediaMetadata.source).where(MediaMetadata.source.is_not(None)).distinct()
            )
        ).scalars().all()
    ]
    servers = (
        await session.execute(
            select(Worker.name)
            .join(Item, Item.worker_id == Worker.id)
            .where(Item.status == ItemStatus.COMPLETED.value)
            .distinct()
            .order_by(Worker.name)
        )
    ).scalars().all()
    total = (
        await session.execute(
            select(func.count(func.distinct(Item.external_id))).where(
                Item.status == ItemStatus.COMPLETED.value
            )
        )
    ).scalar_one()

    return _page(
        request,
        "library.html",
        {
            "items": items,
            "q": q,
            "group": group,
            "source": source,
            "server": server,
            "sort": sort,
            "order": order,
            "groups": groups,
            "sources": sources,
            "servers": list(servers),
            "fav_id": fav.id,
            "total_count": total,
        },
    )


@router.get("/player/{item_id}", response_class=HTMLResponse,
            dependencies=[Depends(require_role("viewer"))])
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


@router.get("/watch/{item_id}", dependencies=[Depends(require_role("viewer"))])
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
