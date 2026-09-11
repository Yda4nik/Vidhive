"""Block scheduler — the heart of the coordinator.

Responsibilities:

* cut the (possibly open-ended) job range into small chunks on demand;
* hand a chunk to a worker atomically, so one chunk is never leased twice
  (``SELECT ... FOR UPDATE SKIP LOCKED`` on PostgreSQL; serialized on SQLite);
* reclaim chunks whose lease expired because a worker went silent;
* record progress idempotently via a ``next_id`` checkpoint inside the chunk.

The functions mutate the passed session; the caller owns the transaction
(commit/rollback), which keeps the lease + generate steps in one atomic unit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Item, Job, MediaMetadata, RangeChunk, Worker
from vidhive_common.enums import ChunkStatus, ItemStatus, JobState
from vidhive_common.schemas import ProgressReport


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def reclaim_expired(session: AsyncSession) -> int:
    """Return chunks whose lease expired back to the queue. Returns the count."""
    result = await session.execute(
        update(RangeChunk)
        .where(RangeChunk.status == ChunkStatus.LEASED.value)
        .where(RangeChunk.lease_expires_at.is_not(None))
        .where(RangeChunk.lease_expires_at < _now())
        .values(
            status=ChunkStatus.PENDING.value,
            leased_by=None,
            lease_expires_at=None,
            attempt=RangeChunk.attempt + 1,
        )
    )
    return result.rowcount or 0


async def _take_pending_chunk(session: AsyncSession) -> RangeChunk | None:
    """Atomically claim one pending chunk of a running job (skip locked rows)."""
    stmt = (
        select(RangeChunk)
        .join(RangeChunk.job)
        .where(RangeChunk.status == ChunkStatus.PENDING.value)
        .where(Job.state == JobState.RUNNING.value)
        .order_by(RangeChunk.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=RangeChunk)
    )
    return (await session.execute(stmt)).scalars().first()


async def _generate_chunk(session: AsyncSession) -> RangeChunk | None:
    """Cut a fresh chunk from a running job that still has range left."""
    stmt = (
        select(Job)
        .where(Job.state == JobState.RUNNING.value)
        .where(Job.next_chunk_start.is_not(None))
        .where(or_(Job.range_end.is_(None), Job.next_chunk_start <= Job.range_end))
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(stmt)).scalars().first()
    if job is None:
        return None

    start = job.next_chunk_start
    last = start + job.chunk_size - 1
    if job.range_end is not None:
        last = min(last, job.range_end)

    chunk = RangeChunk(
        job_id=job.id,
        range_start=start,
        range_end=last,
        next_id=start,
        status=ChunkStatus.PENDING.value,
    )
    session.add(chunk)
    job.next_chunk_start = last + 1
    await session.flush()
    return chunk


async def lease_chunk(
    session: AsyncSession, worker: Worker, lease_seconds: int
) -> RangeChunk | None:
    """Lease the next available chunk to ``worker``, generating one if needed."""
    await reclaim_expired(session)

    chunk = await _take_pending_chunk(session)
    if chunk is None:
        chunk = await _generate_chunk(session)
    if chunk is None:
        return None

    chunk.status = ChunkStatus.LEASED.value
    chunk.leased_by = worker.id
    chunk.lease_expires_at = _now() + timedelta(seconds=lease_seconds)
    if chunk.next_id is None:
        chunk.next_id = chunk.range_start
    await session.flush()
    return chunk


async def renew_worker_leases(session: AsyncSession, worker: Worker, lease_seconds: int) -> None:
    """Heartbeat: extend every lease held by this worker and stamp liveness."""
    await session.execute(
        update(RangeChunk)
        .where(RangeChunk.leased_by == worker.id)
        .where(RangeChunk.status == ChunkStatus.LEASED.value)
        .values(lease_expires_at=_now() + timedelta(seconds=lease_seconds))
    )
    worker.last_heartbeat_at = _now()


async def _upsert_item(session: AsyncSession, job_id: int, worker_id: int, external_id: int,
                       status: str, error: str | None) -> Item:
    existing = (
        await session.execute(
            select(Item).where(Item.job_id == job_id).where(Item.external_id == external_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = Item(job_id=job_id, external_id=external_id, attempts=0)
        session.add(existing)
    existing.status = status
    existing.worker_id = worker_id
    existing.attempts = (existing.attempts or 0) + 1
    existing.last_error = error
    existing.checked_at = _now()
    await session.flush()
    return existing


async def apply_progress(session: AsyncSession, worker: Worker, report: ProgressReport) -> RangeChunk:
    """Advance a chunk's checkpoint and record item results idempotently."""
    chunk = await session.get(RangeChunk, report.chunk_id)
    if chunk is None:
        raise LookupError(f"chunk {report.chunk_id} not found")

    # Move the checkpoint forward only (never backwards).
    if chunk.next_id is None or report.next_id > chunk.next_id:
        chunk.next_id = report.next_id
    # Progress counts as a heartbeat: renew the lease on a still-leased chunk.
    if chunk.status == ChunkStatus.LEASED.value:
        chunk.lease_expires_at = _now() + timedelta(seconds=get_settings().default_lease_seconds)
    worker.last_heartbeat_at = _now()

    for item in report.items:
        row = await _upsert_item(
            session, chunk.job_id, worker.id, item.external_id, item.status.value, item.error
        )
        if item.status in (ItemStatus.FOUND, ItemStatus.COMPLETED) and (
            item.title or item.download_url
        ):
            meta = (
                await session.execute(
                    select(MediaMetadata).where(MediaMetadata.item_id == row.id)
                )
            ).scalar_one_or_none()
            if meta is None:
                meta = MediaMetadata(item_id=row.id)
                session.add(meta)
            meta.title = item.title
            meta.download_url = item.download_url
            meta.mime_type = item.mime_type
            meta.size_bytes = item.size_bytes
    await session.flush()
    return chunk


async def complete_chunk(
    session: AsyncSession, chunk_id: int, status: ChunkStatus
) -> RangeChunk:
    """Mark a chunk done, or return it to the queue on failure for a retry."""
    chunk = await session.get(RangeChunk, chunk_id)
    if chunk is None:
        raise LookupError(f"chunk {chunk_id} not found")

    if status == ChunkStatus.COMPLETED:
        chunk.status = ChunkStatus.COMPLETED.value
        chunk.leased_by = None
        chunk.lease_expires_at = None
    else:
        # Failure: back to the queue so another worker can retry.
        chunk.status = ChunkStatus.PENDING.value
        chunk.leased_by = None
        chunk.lease_expires_at = None
        chunk.attempt += 1
    await session.flush()
    return chunk


async def maybe_complete_job(session: AsyncSession, job_id: int) -> bool:
    """Complete a finite job once its range is exhausted and no chunks remain open."""
    job = await session.get(Job, job_id)
    if job is None or job.state != JobState.RUNNING.value or job.range_end is None:
        return False
    if job.next_chunk_start is not None and job.next_chunk_start <= job.range_end:
        return False  # range not fully cut yet

    open_chunks = (
        await session.execute(
            select(RangeChunk.id)
            .where(RangeChunk.job_id == job_id)
            .where(RangeChunk.status.in_([ChunkStatus.PENDING.value, ChunkStatus.LEASED.value]))
            .limit(1)
        )
    ).first()
    if open_chunks is not None:
        return False

    job.state = JobState.COMPLETED.value
    await session.flush()
    return True
