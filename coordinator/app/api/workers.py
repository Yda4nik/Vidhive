"""Worker protocol: registration, lease, heartbeat, progress, complete/fail.

This is the coordinator side of the block protocol. Agents (stage 3) drive it.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sources import template_for
from app.db.models import Job, Worker, WorkerMetric
from app.db.session import get_session
from app.services import drain, scheduler
from app.services.agent_client import validate_agent_url
from app.services.auth import require_agent_token, require_role
from app.services.bus import bus
from app.services.events import log_event
from vidhive_common.enums import ChunkStatus, WorkerState
from vidhive_common.schemas import (
    Ack,
    ChunkComplete,
    ChunkLease,
    Heartbeat,
    LeaseRequest,
    ProgressAck,
    ProgressReport,
    WorkerOut,
    WorkerRegister,
)

log = logging.getLogger("vidhive.workers")

router = APIRouter(prefix="/api/workers", tags=["workers"])


async def _get_worker_or_404(session: AsyncSession, worker_id: int) -> Worker:
    worker = await session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker


@router.post("/register", response_model=WorkerOut,
             dependencies=[Depends(require_agent_token)])
async def register_worker(
    payload: WorkerRegister, session: AsyncSession = Depends(get_session)
) -> Worker:
    """Register a worker, or refresh it if the name already exists (idempotent)."""
    # The coordinator will later call this address (stream/delete files): refuse
    # anything that could point it at internal services (SSRF).
    bad = validate_agent_url(payload.agent_url)
    if bad:
        raise HTTPException(status_code=422, detail=bad)
    result = await session.execute(select(Worker).where(Worker.name == payload.name))
    worker = result.scalar_one_or_none()
    if worker is None:
        worker = Worker(name=payload.name)
        session.add(worker)

    worker.host = payload.host
    worker.agent_url = payload.agent_url
    worker.agent_version = payload.agent_version
    worker.threads = payload.threads
    worker.storage_path = payload.storage_path
    if worker.state != WorkerState.DRAINING.value:  # a draining server stays draining
        worker.state = WorkerState.ONLINE.value
    worker.last_heartbeat_at = datetime.now(timezone.utc)
    await session.flush()

    log_event(
        session,
        component="workers",
        operation="register",
        result="online",
        worker_id=worker.id,
        message=f"{worker.name} at {worker.agent_url or '-'} ({worker.threads} threads)",
    )
    await session.commit()
    await session.refresh(worker)
    return worker


@router.get("", response_model=list[WorkerOut], dependencies=[Depends(require_role("viewer"))])
async def list_workers(session: AsyncSession = Depends(get_session)) -> list[Worker]:
    result = await session.execute(select(Worker).order_by(Worker.name))
    return list(result.scalars().all())


@router.get("/{worker_id}", response_model=WorkerOut,
            dependencies=[Depends(require_role("viewer"))])
async def get_worker(worker_id: int, session: AsyncSession = Depends(get_session)) -> Worker:
    return await _get_worker_or_404(session, worker_id)


@router.delete("/{worker_id}", response_model=Ack,
               dependencies=[Depends(require_role("administrator"))])
async def delete_worker(
    worker_id: int,
    mode: str = Query("purge", pattern="^(purge|redistribute)$"),
    session: AsyncSession = Depends(get_session),
) -> Ack:
    """Remove a server from the network.

    * ``purge``        — delete it now, together with every file it downloaded.
    * ``redistribute`` — mark it *draining*: it takes no new work, a re-download is
      queued for videos that exist only on it, and the coordinator removes it once
      every one of them has a copy elsewhere. Nothing is deleted before that, so a
      failed re-download can never lose a video.
    """
    worker = await _get_worker_or_404(session, worker_id)

    if mode == "redistribute":
        if worker.state == WorkerState.DRAINING.value:
            raise HTTPException(status_code=409, detail="сервер уже переносит свои видео")
        others = (
            await session.execute(
                select(func.count())
                .select_from(Worker)
                .where(Worker.id != worker.id)
                .where(Worker.state == WorkerState.ONLINE.value)
            )
        ).scalar_one()
        if not others:
            raise HTTPException(
                status_code=409,
                detail="нет других серверов в сети для распределения — используйте удаление с данными",
            )
        await drain.start_drain(session, worker)
        # Nothing exclusive to this server (or nothing at all): no need to wait.
        if not await drain.unmigrated_items(session, worker.id):
            await drain.purge_worker(session, worker, reason="redistribute")
    else:
        await drain.purge_worker(session, worker, reason="purge")

    await session.commit()
    return Ack()


@router.post("/{worker_id}/heartbeat", response_model=Ack,
             dependencies=[Depends(require_agent_token)])
async def heartbeat(
    worker_id: int,
    payload: Heartbeat,
    lease_seconds: int = 120,
    session: AsyncSession = Depends(get_session),
) -> Ack:
    worker = await _get_worker_or_404(session, worker_id)
    came_back = worker.state not in (WorkerState.ONLINE.value, WorkerState.DRAINING.value)
    if worker.state != WorkerState.DRAINING.value:
        worker.state = WorkerState.ONLINE.value
    await scheduler.renew_worker_leases(session, worker, lease_seconds, payload.active_chunk_id)

    # Store a metrics sample if the heartbeat carried resource data.
    if any(
        v is not None
        for v in (payload.cpu_percent, payload.ram_used_mb, payload.disk_free_gb)
    ):
        session.add(
            WorkerMetric(
                worker_id=worker.id,
                cpu_percent=payload.cpu_percent,
                ram_used_mb=payload.ram_used_mb,
                ram_total_mb=payload.ram_total_mb,
                disk_free_gb=payload.disk_free_gb,
                active_checks=payload.active_checks,
                active_downloads=payload.active_downloads,
            )
        )
    await session.commit()
    if came_back:
        bus.publish()  # heartbeats are not published by the middleware; a recovery must be
    return Ack()


@router.post("/{worker_id}/lease", response_model=ChunkLease,
             dependencies=[Depends(require_agent_token)])
async def lease(
    worker_id: int,
    payload: LeaseRequest,
    session: AsyncSession = Depends(get_session),
):
    """Lease the next available chunk. Returns 204 when there is no work."""
    worker = await _get_worker_or_404(session, worker_id)
    chunk = await scheduler.lease_chunk(session, worker, payload.lease_seconds)
    if chunk is None:
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    job = await session.get(Job, chunk.job_id)
    await session.commit()
    return ChunkLease(
        chunk_id=chunk.id,
        job_id=chunk.job_id,
        range_start=chunk.range_start,
        range_end=chunk.range_end,
        next_id=chunk.next_id,
        lease_expires_at=chunk.lease_expires_at,
        # A link-based chunk (YouTube) carries the exact URL; otherwise build
        # the URL from the source's numeric template.
        target_template=chunk.url or (template_for(job.source) if job else None),
    )


@router.post("/{worker_id}/progress", response_model=ProgressAck,
             dependencies=[Depends(require_agent_token)])
async def progress(
    worker_id: int,
    report: ProgressReport,
    session: AsyncSession = Depends(get_session),
) -> ProgressAck:
    """Record a batch. 409 = this worker no longer holds the chunk (abandon it);
    ``proceed=false`` = the job was paused/stopped (batch recorded, stop working)."""
    worker = await _get_worker_or_404(session, worker_id)
    try:
        _, proceed = await scheduler.apply_progress(session, worker, report)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except scheduler.StaleLease as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return ProgressAck(proceed=proceed)


@router.post("/{worker_id}/complete", response_model=Ack,
             dependencies=[Depends(require_agent_token)])
async def complete(
    worker_id: int,
    payload: ChunkComplete,
    session: AsyncSession = Depends(get_session),
) -> Ack:
    await _get_worker_or_404(session, worker_id)
    try:
        chunk = await scheduler.complete_chunk(
            session, payload.chunk_id, payload.status, worker_id=worker_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except scheduler.StaleLease as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await scheduler.maybe_complete_job(session, chunk.job_id)
    await session.commit()
    return Ack()


@router.post("/{worker_id}/fail", response_model=Ack,
             dependencies=[Depends(require_agent_token)])
async def fail(
    worker_id: int,
    payload: ChunkComplete,
    session: AsyncSession = Depends(get_session),
) -> Ack:
    """Report a chunk as failed; it returns to the queue for another worker."""
    await _get_worker_or_404(session, worker_id)
    try:
        await scheduler.complete_chunk(
            session, payload.chunk_id, ChunkStatus.FAILED, worker_id=worker_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except scheduler.StaleLease as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return Ack()
