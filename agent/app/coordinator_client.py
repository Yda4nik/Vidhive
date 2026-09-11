"""Thin async client for the coordinator's worker API."""

from __future__ import annotations

import httpx

from vidhive_common.schemas import (
    ChunkComplete,
    ChunkLease,
    Heartbeat,
    LeaseRequest,
    ProgressReport,
    WorkerRegister,
)


class CoordinatorClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def register(self, payload: WorkerRegister) -> int:
        resp = await self._client.post("/api/workers/register", json=payload.model_dump())
        resp.raise_for_status()
        return resp.json()["id"]

    async def lease(self, worker_id: int, lease_seconds: int) -> ChunkLease | None:
        resp = await self._client.post(
            f"/api/workers/{worker_id}/lease",
            json=LeaseRequest(worker_id=worker_id, lease_seconds=lease_seconds).model_dump(),
        )
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return ChunkLease.model_validate(resp.json())

    async def heartbeat(self, worker_id: int, hb: Heartbeat, lease_seconds: int) -> None:
        resp = await self._client.post(
            f"/api/workers/{worker_id}/heartbeat",
            params={"lease_seconds": lease_seconds},
            json=hb.model_dump(),
        )
        resp.raise_for_status()

    async def progress(self, worker_id: int, report: ProgressReport) -> None:
        resp = await self._client.post(
            f"/api/workers/{worker_id}/progress", json=report.model_dump(mode="json")
        )
        resp.raise_for_status()

    async def complete(self, worker_id: int, chunk_id: int) -> None:
        resp = await self._client.post(
            f"/api/workers/{worker_id}/complete", json=ChunkComplete(chunk_id=chunk_id).model_dump()
        )
        resp.raise_for_status()

    async def fail(self, worker_id: int, chunk_id: int) -> None:
        resp = await self._client.post(
            f"/api/workers/{worker_id}/fail", json=ChunkComplete(chunk_id=chunk_id).model_dump()
        )
        resp.raise_for_status()
