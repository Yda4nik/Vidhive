"""Job management endpoints.

Stage 1 covers creation and read; the block scheduler and lifecycle actions
(start/pause/stop) arrive in stage 2.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job
from app.db.session import get_session
from vidhive_common.schemas import JobCreate, JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreate, session: AsyncSession = Depends(get_session)) -> Job:
    if payload.range_end is not None and payload.range_end < payload.range_start:
        raise HTTPException(status_code=422, detail="range_end must be >= range_start")

    job = Job(
        name=payload.name,
        range_start=payload.range_start,
        range_end=payload.range_end,
        chunk_size=payload.chunk_size,
        request_timeout_seconds=payload.request_timeout_seconds,
        max_retries=payload.max_retries,
        global_rate_limit_rps=payload.global_rate_limit_rps,
        stop_on_consecutive_missing=payload.stop_on_consecutive_missing,
        config=payload.model_dump(mode="json"),
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


@router.get("", response_model=list[JobOut])
async def list_jobs(session: AsyncSession = Depends(get_session)) -> list[Job]:
    result = await session.execute(select(Job).order_by(Job.id.desc()))
    return list(result.scalars().all())


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job
