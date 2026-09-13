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

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import math

from app.core.config import get_settings
from app.db.models import (
    Download,
    FileRecord,
    Item,
    Job,
    JobTarget,
    MediaMetadata,
    RangeChunk,
    Worker,
    WorkerMetric,
)
from app.services.events import log_event
from vidhive_common.enums import ChunkStatus, DownloadStatus, ItemStatus, JobState
from vidhive_common.schemas import ProgressReport


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _source_of(url: str | None) -> str | None:
    """Short source name from a URL host, e.g. 'kinescope' or 'youtube'."""
    if not url:
        return None
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    base = host.split(".")
    # youtu.be / youtube.com -> youtube ; kinescope.io -> kinescope
    if "youtube" in host or host == "youtu.be":
        return "youtube"
    return base[-2] if len(base) >= 2 else host


_MAX_AUTO_CHUNK = 10000
_CHUNKS_PER_WORKER = 4


def _auto_chunk_size(span: int, workers: int) -> int:
    """Pick a block size so all workers stay busy: aim for ~4 blocks each."""
    target_blocks = max(1, workers * _CHUNKS_PER_WORKER)
    return max(1, min(_MAX_AUTO_CHUNK, math.ceil(span / target_blocks)))


async def _online_worker_count(session: AsyncSession) -> int:
    n = (
        await session.execute(
            select(func.count()).select_from(Worker).where(Worker.state == "online")
        )
    ).scalar_one()
    return n or 3


async def initialize_job_chunks(session: AsyncSession, job: Job) -> None:
    """Prepare a job's work when it first starts.

    * No explicit targets (API/legacy path) -> keep lazy generation from the
      job's own range using its chunk_size (unchanged behaviour).
    * A single open-ended target -> lazy generation from its start.
    * Finite targets -> pre-generate auto-sized chunks so each target (and each
      single id) spreads across workers instead of piling onto one.
    """
    targets = (
        await session.execute(select(JobTarget).where(JobTarget.job_id == job.id))
    ).scalars().all()

    if not targets:
        if job.next_chunk_start is None:
            job.next_chunk_start = job.range_start if job.range_start is not None else 0
        return

    numeric = [t for t in targets if t.url is None and t.range_start is not None]
    open_ended = [t for t in numeric if t.range_end is None]
    if open_ended:
        t = open_ended[0]
        job.range_start = t.range_start
        job.range_end = None
        job.next_chunk_start = t.range_start
        return

    workers = await _online_worker_count(session)
    job.next_chunk_start = None  # finite: nothing generated lazily
    for t in numeric:
        a, b = t.range_start, t.range_end
        if a > b:
            a, b = b, a
        size = _auto_chunk_size(b - a + 1, workers)
        start = a
        while start <= b:
            end = min(start + size - 1, b)
            session.add(
                RangeChunk(
                    job_id=job.id, range_start=start, range_end=end,
                    next_id=start, status=ChunkStatus.PENDING.value,
                )
            )
            start = end + 1
    await session.flush()


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
    count = result.rowcount or 0
    if count:
        log_event(
            session,
            component="scheduler",
            operation="reclaim",
            level="warning",
            result="requeued",
            message=f"{count} chunk(s) returned to the queue after lease expiry",
        )
    return count


async def _worker_has_space(session: AsyncSession, worker: Worker) -> bool:
    """Stop issuing work to a worker that is running out of disk (spec 6.3)."""
    metric = (
        await session.execute(
            select(WorkerMetric)
            .where(WorkerMetric.worker_id == worker.id)
            .order_by(WorkerMetric.captured_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if metric is None or metric.disk_free_gb is None:
        return True  # no telemetry yet — don't block work on missing data
    return metric.disk_free_gb >= get_settings().min_free_space_gb


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

    if not await _worker_has_space(session, worker):
        log_event(
            session,
            component="scheduler",
            operation="lease",
            level="warning",
            result="skipped",
            message="worker is below the free-space threshold",
            worker_id=worker.id,
        )
        return None

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
    log_event(
        session,
        component="scheduler",
        operation="lease",
        result="granted",
        message=f"chunk [{chunk.range_start},{chunk.range_end}] from {chunk.next_id}",
        job_id=chunk.job_id,
        worker_id=worker.id,
        chunk_id=chunk.id,
    )
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

    job = await session.get(Job, chunk.job_id)
    target_group_id = job.target_group_id if job else None

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
            meta.duration_seconds = item.duration_seconds
            meta.source = _source_of(item.download_url)

        if item.status == ItemStatus.COMPLETED and item.storage_path:
            await _record_download(session, row, worker, item)
            if target_group_id is not None:
                await _assign_group(session, row.id, target_group_id)
    await session.flush()
    return chunk


async def _assign_group(session: AsyncSession, item_id: int, group_id: int) -> None:
    from app.db.models import ItemGroup

    exists = (
        await session.execute(
            select(ItemGroup.item_id)
            .where(ItemGroup.item_id == item_id)
            .where(ItemGroup.group_id == group_id)
        )
    ).first()
    if exists is None:
        session.add(ItemGroup(item_id=item_id, group_id=group_id))


async def _record_download(
    session: AsyncSession, row: Item, worker: Worker, item
) -> None:
    """Record where a finished file physically lives, idempotently.

    The database is the catalogue: bytes stay on the agent, but this row is what
    lets the web find and stream the file later.
    """
    download = (
        await session.execute(select(Download).where(Download.item_id == row.id))
    ).scalars().first()
    if download is None:
        download = Download(item_id=row.id)
        session.add(download)
    download.worker_id = worker.id
    download.status = DownloadStatus.COMPLETED.value
    download.progress = 100.0
    download.bytes_downloaded = item.size_bytes or 0
    download.finished_at = _now()
    await session.flush()

    existing = (
        await session.execute(
            select(FileRecord)
            .where(FileRecord.download_id == download.id)
            .where(FileRecord.storage_path == item.storage_path)
        )
    ).scalars().first()
    if existing is None:
        session.add(
            FileRecord(
                download_id=download.id,
                storage_path=item.storage_path,
                checksum=item.checksum,
                size_bytes=item.size_bytes,
                worker_name=worker.name,
            )
        )


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


_RETRYABLE = (
    ItemStatus.FAILED.value,
    ItemStatus.RETRY_WAIT.value,
    ItemStatus.RATE_LIMITED.value,
)


async def requeue_failed_items(session: AsyncSession, job_id: int, limit: int = 1000) -> int:
    """Re-queue identifiers that ended in a transient failure (spec 11.3).

    Each such identifier gets its own single-id chunk, so a retry re-checks only
    what actually failed and never re-downloads what already succeeded.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise LookupError(f"job {job_id} not found")

    items = (
        await session.execute(
            select(Item)
            .where(Item.job_id == job_id)
            .where(Item.status.in_(_RETRYABLE))
            .order_by(Item.external_id)
            .limit(limit)
        )
    ).scalars().all()
    if not items:
        return 0

    # Single-id chunks that already exist from an earlier retry.
    existing = {
        c.range_start: c
        for c in (
            await session.execute(
                select(RangeChunk)
                .where(RangeChunk.job_id == job_id)
                .where(RangeChunk.range_start == RangeChunk.range_end)
            )
        ).scalars().all()
    }

    for item in items:
        chunk = existing.get(item.external_id)
        if chunk is None:
            session.add(
                RangeChunk(
                    job_id=job_id,
                    range_start=item.external_id,
                    range_end=item.external_id,
                    next_id=item.external_id,
                    status=ChunkStatus.PENDING.value,
                )
            )
        else:
            chunk.status = ChunkStatus.PENDING.value
            chunk.leased_by = None
            chunk.lease_expires_at = None
            chunk.next_id = item.external_id
            chunk.attempt += 1
        item.status = ItemStatus.PENDING.value

    # A finished job must go back to running, or nothing would be handed out.
    if job.state != JobState.RUNNING.value:
        job.state = JobState.RUNNING.value

    log_event(
        session,
        component="scheduler",
        operation="retry",
        result="requeued",
        job_id=job_id,
        message=f"{len(items)} failed identifier(s) re-queued",
    )
    await session.flush()
    return len(items)


async def maybe_complete_job(session: AsyncSession, job_id: int) -> bool:
    """Complete a job once no work remains.

    An open-ended job (lazy cursor, no upper bound) never auto-completes. For a
    range-cursor job the range must be fully cut; for a pre-generated (finite,
    multi-target) job it is enough that it had chunks and none remain open.
    """
    job = await session.get(Job, job_id)
    if job is None or job.state != JobState.RUNNING.value:
        return False

    if job.next_chunk_start is not None:
        # Lazy-cursor job: open-ended never completes; bounded needs the range cut.
        if job.range_end is None:
            return False
        if job.next_chunk_start <= job.range_end:
            return False
    else:
        # Pre-generated job: it must actually have had chunks.
        any_chunk = (
            await session.execute(
                select(RangeChunk.id).where(RangeChunk.job_id == job_id).limit(1)
            )
        ).first()
        if any_chunk is None:
            return False

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
