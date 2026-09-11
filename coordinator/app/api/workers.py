"""Worker registration and listing.

Stage 1 covers register/list/get; lease, heartbeat, progress and complete
arrive in stage 2 with the block protocol.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Worker
from app.db.session import get_session
from vidhive_common.enums import WorkerState
from vidhive_common.schemas import WorkerOut, WorkerRegister

router = APIRouter(prefix="/api/workers", tags=["workers"])


@router.post("/register", response_model=WorkerOut)
async def register_worker(
    payload: WorkerRegister, session: AsyncSession = Depends(get_session)
) -> Worker:
    """Register a worker, or refresh it if a worker with that name already exists."""
    result = await session.execute(select(Worker).where(Worker.name == payload.name))
    worker = result.scalar_one_or_none()
    if worker is None:
        worker = Worker(name=payload.name)
        session.add(worker)

    worker.host = payload.host
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
    worker = await session.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="worker not found")
    return worker
