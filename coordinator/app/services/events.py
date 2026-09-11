"""Structured event journal (spec section 15).

Every entry carries time, level, component and the identifiers of whatever it
concerns, so the log can be searched by job, worker, chunk, item or status.
The caller owns the transaction — this only stages the row.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event


def log_event(
    session: AsyncSession,
    *,
    component: str,
    operation: str,
    level: str = "info",
    result: str | None = None,
    message: str | None = None,
    job_id: int | None = None,
    worker_id: int | None = None,
    chunk_id: int | None = None,
    item_id: int | None = None,
    error_code: str | None = None,
) -> None:
    session.add(
        Event(
            level=level,
            component=component,
            operation=operation,
            result=result,
            message=message,
            job_id=job_id,
            worker_id=worker_id,
            chunk_id=chunk_id,
            item_id=item_id,
            error_code=error_code,
        )
    )
