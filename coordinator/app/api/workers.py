"""Worker protocol: registration, lease, heartbeat, progress, complete/fail.

This is the coordinator side of the block protocol. Agents (stage 3) drive it.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Worker, WorkerMetric
from app.db.session import get_session
from app.services import scheduler
from vidhive_common.enums import ChunkStatus, WorkerState
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

router = APIRouter(prefix="/api/workers", tags=["workers"])


async def _get_worker_or_404(session: AsyncSession, worker_id: int) -> Worker:
    worker = await session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker


@router.post("/register", response_model=WorkerOut)
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

    await session.commit()
    await session.refresh(worker)
    return worker


@router.get("", response_model=list[WorkerOut])
async def list_workers(session: AsyncSession = Depends(get_session)) -> list[Worker]:
    result = await session.execute(select(Worker).order_by(Worker.name))
    return list(result.scalars().all())


@router.get("/{worker_id}", response_model=WorkerOut)
async def get_worker(worker_id: int, session: AsyncSession = Depends(get_session)) -> Worker:
    return await _get_worker_or_404(session, worker_id)


@router.post("/{worker_id}/heartbeat", response_model=Ack)
async def heartbeat(
    worker_id: int,
    payload: Heartbeat,
    lease_seconds: int = 120,
    session: AsyncSession = Depends(get_session),
) -> Ack:
    worker = await _get_worker_or_404(session, worker_id)
    worker.state = WorkerState.ONLINE.value
    await scheduler.renew_worker_leases(session, worker, lease_seconds)

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


@router.post("/{worker_id}/lease", response_model=ChunkLease)
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
    await session.commit()
    return ChunkLease(
        chunk_id=chunk.id,
        job_id=chunk.job_id,
        range_start=chunk.range_start,
        range_end=chunk.range_end,
        next_id=chunk.next_id,
        lease_expires_at=chunk.lease_expires_at,
    )


@router.post("/{worker_id}/progress", response_model=Ack)
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


@router.post("/{worker_id}/complete", response_model=Ack)
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


@router.post("/{worker_id}/fail", response_model=Ack)
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
