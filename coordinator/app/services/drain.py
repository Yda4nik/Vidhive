"""Draining a server: move its videos elsewhere *before* anything is deleted.

"Distribute to other servers" used to queue a re-download and then immediately wipe
the source copies. If the re-download then failed (source gone, network blocked) the
video was lost. Now the server is only marked ``draining``: it takes no new work but
keeps serving its files, a re-download job is queued for videos that exist *only* on
it, and the coordinator removes the server once every video it holds has a completed
copy on another server (see :func:`finalize_drained_workers`).
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Item, Job, JobTarget, MediaMetadata, RangeChunk, Worker
from app.services import scheduler
from app.services.agent_client import agent_headers, file_url
from app.services.events import log_event
from vidhive_common.enums import ChunkStatus, ItemStatus, JobState, WorkerState

log = logging.getLogger("vidhive.drain")


async def unmigrated_items(session: AsyncSession, worker_id: int) -> list[Item]:
    """Completed items on ``worker_id`` whose video has NO completed copy elsewhere."""
    other = aliased(Item)
    stmt = (
        select(Item)
        .where(Item.worker_id == worker_id)
        .where(Item.status == ItemStatus.COMPLETED.value)
        .where(
            ~exists().where(
                other.external_id == Item.external_id,
                other.status == ItemStatus.COMPLETED.value,
                other.worker_id.is_not(None),
                other.worker_id != worker_id,
            )
        )
        .order_by(Item.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def purge_worker(session: AsyncSession, worker: Worker, *, reason: str) -> int:
    """Delete a worker with everything it downloaded. Returns how many videos it held.

    File deletion on the agent is best-effort (it may be offline); the database rows
    are removed regardless.
    """
    completed = list(
        (
            await session.execute(
                select(Item)
                .where(Item.worker_id == worker.id)
                .where(Item.status == ItemStatus.COMPLETED.value)
            )
        ).scalars().all()
    )

    if worker.agent_url:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for it in completed:
                try:
                    await client.delete(
                        file_url(worker.agent_url, it.external_id), headers=agent_headers()
                    )
                except httpx.HTTPError as exc:
                    log.warning("agent file delete failed for %s: %s", it.external_id, exc)

    # Return any chunk this worker was holding so others pick it up now.
    await session.execute(
        update(RangeChunk)
        .where(RangeChunk.leased_by == worker.id)
        .where(RangeChunk.status == ChunkStatus.LEASED.value)
        .values(status=ChunkStatus.PENDING.value, leased_by=None, lease_expires_at=None)
    )

    # Drop its completed items (and their metadata/downloads/files/library rows).
    for it in completed:
        await session.delete(it)

    log_event(
        session,
        component="workers",
        operation="delete",
        result=reason,
        worker_id=worker.id,
        message=f"{worker.name} removed ({reason}); {len(completed)} file(s) purged",
    )
    await session.delete(worker)  # cascades metrics; SET NULL on remaining references
    return len(completed)


async def start_drain(session: AsyncSession, worker: Worker) -> int:
    """Queue re-downloads for videos that exist only on ``worker`` and mark it draining.

    Returns the number of distinct videos queued. A video that already has a copy on
    another server is not downloaded again.
    """
    pending = await unmigrated_items(session, worker.id)

    # One (external_id -> source / url) entry per distinct video.
    videos: dict[int, tuple[str, str | None]] = {}
    for it in pending:
        if it.external_id in videos:
            continue
        job = await session.get(Job, it.job_id)
        source = job.source if job else "kinescope"
        url = None
        if source == "youtube":
            # A YouTube video is fetched by URL, which we kept in its metadata.
            meta = (
                await session.execute(
                    select(MediaMetadata).where(MediaMetadata.item_id == it.id)
                )
            ).scalars().first()
            url = meta.download_url if meta else None
            if not url:
                continue  # can't re-download without the URL; stays "unmigrated"
        videos[it.external_id] = (source, url)

    by_source: dict[str, list[tuple[int, str | None]]] = {}
    for ext, (source, url) in videos.items():
        by_source.setdefault(source, []).append((ext, url))

    for source, entries in by_source.items():
        new_job = Job(name=f"Перенос с «{worker.name}»", source=source)
        session.add(new_job)
        await session.flush()
        for ext, url in sorted(entries):
            session.add(JobTarget(job_id=new_job.id, range_start=ext, range_end=ext, url=url))
        await session.flush()
        await scheduler.initialize_job_chunks(session, new_job)
        new_job.state = JobState.RUNNING.value

    # No new work for this server; hand back whatever it was holding.
    worker.state = WorkerState.DRAINING.value
    await session.execute(
        update(RangeChunk)
        .where(RangeChunk.leased_by == worker.id)
        .where(RangeChunk.status == ChunkStatus.LEASED.value)
        .values(status=ChunkStatus.PENDING.value, leased_by=None, lease_expires_at=None)
    )
    log_event(
        session,
        component="workers",
        operation="drain",
        result="started",
        worker_id=worker.id,
        message=f"{worker.name} is draining: {len(videos)} video(s) queued to move",
    )
    return len(videos)


async def finalize_drained_workers(session: AsyncSession) -> int:
    """Remove every draining server whose videos all have a copy elsewhere."""
    draining = (
        await session.execute(select(Worker).where(Worker.state == WorkerState.DRAINING.value))
    ).scalars().all()
    done = 0
    for worker in draining:
        if await unmigrated_items(session, worker.id):
            continue  # still waiting for some re-download to finish
        await purge_worker(session, worker, reason="drained")
        done += 1
    return done
