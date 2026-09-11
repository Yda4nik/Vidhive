"""Job management: creation, read, and lifecycle transitions."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job
from app.db.session import get_session
from vidhive_common.enums import JobState
from vidhive_common.ranges import RangeParseError, parse_range
from vidhive_common.schemas import Ack, JobCreate, JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Which lifecycle transitions are allowed.
_ALLOWED: dict[str, set[JobState]] = {
    "start": {JobState.CREATED, JobState.PAUSED, JobState.STOPPED},
    "pause": {JobState.RUNNING},
    "resume": {JobState.PAUSED},
    "stop": {JobState.RUNNING, JobState.PAUSED, JobState.PAUSING},
}


async def _get_job_or_404(session: AsyncSession, job_id: int) -> Job:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _guard(job: Job, action: str) -> None:
    if JobState(job.state) not in _ALLOWED[action]:
        raise HTTPException(
            status_code=409, detail=f"cannot {action} a job in state '{job.state}'"
        )


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreate, session: AsyncSession = Depends(get_session)) -> Job:
    range_start, range_end = payload.range_start, payload.range_end
    if payload.range_spec is not None:
        try:
            range_start, range_end = parse_range(payload.range_spec)
        except RangeParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if range_end is not None and range_end < range_start:
        raise HTTPException(status_code=422, detail="range_end must be >= range_start")

    job = Job(
        name=payload.name,
        range_start=range_start,
        range_end=range_end,
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
    return await _get_job_or_404(session, job_id)


@router.post("/{job_id}/start", response_model=JobOut)
async def start_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    job = await _get_job_or_404(session, job_id)
    _guard(job, "start")
    if job.next_chunk_start is None:
        job.next_chunk_start = job.range_start if job.range_start is not None else 0
    job.state = JobState.RUNNING.value
    await session.commit()
    await session.refresh(job)
    return job


@router.post("/{job_id}/pause", response_model=JobOut)
async def pause_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    job = await _get_job_or_404(session, job_id)
    _guard(job, "pause")
    job.state = JobState.PAUSED.value
    await session.commit()
    await session.refresh(job)
    return job


@router.post("/{job_id}/resume", response_model=JobOut)
async def resume_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    job = await _get_job_or_404(session, job_id)
    _guard(job, "resume")
    job.state = JobState.RUNNING.value
    await session.commit()
    await session.refresh(job)
    return job


@router.post("/{job_id}/stop", response_model=JobOut)
async def stop_job(job_id: int, session: AsyncSession = Depends(get_session)) -> Job:
    job = await _get_job_or_404(session, job_id)
    _guard(job, "stop")
    job.state = JobState.STOPPED.value
    await session.commit()
    await session.refresh(job)
    return job
