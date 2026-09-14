"""Worker protocol: registration, lease, heartbeat, progress, complete/fail.

This is the coordinator side of the block protocol. Agents (stage 3) drive it.
"""

import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sources import template_for
from app.db.models import Item, Job, JobTarget, RangeChunk, Worker, WorkerMetric
from app.db.session import get_session
from app.services import scheduler
from app.services.auth import require_agent_token, require_role
from app.services.events import log_event
from vidhive_common.enums import ChunkStatus, ItemStatus, JobState, WorkerState
from vidhive_common.schemas import (
    Ack,
    ChunkComplete,
    ChunkLease,
    Heartbeat,
    LeaseRequest,
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


async def _redistribute_worker_videos(session: AsyncSession, worker: Worker, items: list[Item]) -> int:
    """Re-queue the identifiers a departing worker holds so another server fetches them again.

    Files can't be moved between machines, but we still have the source ids: a fresh
    re-download job (one single-id target per video, grouped by source) lets a surviving
    online worker pull the same videos onto itself. The old copy is removed afterwards.
    """
    ids_by_source: dict[str, set[int]] = {}
    for it in items:
        job = await session.get(Job, it.job_id)
        source = job.source if job else "kinescope"
        ids_by_source.setdefault(source, set()).add(it.external_id)

    total = 0
    for source, ids in ids_by_source.items():
        new_job = Job(name=f"Перенос с «{worker.name}»", source=source)
        session.add(new_job)
        await session.flush()
        for ext in sorted(ids):
            session.add(JobTarget(job_id=new_job.id, range_start=ext, range_end=ext, url=None))
        await session.flush()
        await scheduler.initialize_job_chunks(session, new_job)
        new_job.state = JobState.RUNNING.value
        total += len(ids)
    return total


@router.delete("/{worker_id}", response_model=Ack,
               dependencies=[Depends(require_role("administrator"))])
async def delete_worker(
    worker_id: int,
    mode: str = Query("purge", pattern="^(purge|redistribute)$"),
    session: AsyncSession = Depends(get_session),
) -> Ack:
    """Remove a server from the network.

    * ``purge``        — delete the server together with every file it downloaded.
    * ``redistribute`` — first re-queue its videos so a surviving online server
      re-downloads them, then remove the server and its files.
    """
    worker = await _get_worker_or_404(session, worker_id)

    completed = (
        await session.execute(
            select(Item)
            .where(Item.worker_id == worker.id)
            .where(Item.status == ItemStatus.COMPLETED.value)
        )
    ).scalars().all()

    requeued = 0
    if mode == "redistribute":
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
        requeued = await _redistribute_worker_videos(session, worker, completed)

    # Delete the physical files on the agent (best-effort — it may already be offline).
    if worker.agent_url:
        base = worker.agent_url.rstrip("/")
        async with httpx.AsyncClient(timeout=15.0) as client:
            for it in completed:
                try:
                    await client.delete(f"{base}/files/{it.external_id}")
                except httpx.HTTPError as exc:
                    log.warning("agent file delete failed for %s: %s", it.external_id, exc)

    # Return any chunk this worker was holding to the queue so others pick it up now.
    await session.execute(
        update(RangeChunk)
        .where(RangeChunk.leased_by == worker.id)
        .where(RangeChunk.status == ChunkStatus.LEASED.value)
        .values(status=ChunkStatus.PENDING.value, leased_by=None, lease_expires_at=None)
    )

    # Drop this worker's completed items (and their metadata/downloads/files/library rows).
    for it in completed:
        await session.delete(it)

    log_event(
        session,
        component="workers",
        operation="delete",
        result=mode,
        worker_id=worker.id,
        message=(
            f"{worker.name} removed ({mode}); {len(completed)} file(s) purged"
            + (f", {requeued} re-queued" if mode == "redistribute" else "")
        ),
    )
    await session.delete(worker)  # cascades metrics; SET NULL on any remaining references
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
        target_template=template_for(job.source) if job else None,
    )


@router.post("/{worker_id}/progress", response_model=Ack,
             dependencies=[Depends(require_agent_token)])
async def progress(
    worker_id: int,
    report: ProgressReport,
    session: AsyncSession = Depends(get_session),
) -> Ack:
    worker = await _get_worker_or_404(session, worker_id)
    try:
        await scheduler.apply_progress(session, worker, report)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return Ack()


@router.post("/{worker_id}/complete", response_model=Ack,
             dependencies=[Depends(require_agent_token)])
async def complete(
    worker_id: int,
    payload: ChunkComplete,
    session: AsyncSession = Depends(get_session),
) -> Ack:
    await _get_worker_or_404(session, worker_id)
    try:
        chunk = await scheduler.complete_chunk(session, payload.chunk_id, payload.status)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
        await scheduler.complete_chunk(session, payload.chunk_id, ChunkStatus.FAILED)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return Ack()
