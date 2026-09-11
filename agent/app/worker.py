"""Worker runner — the agent's main loop.

Registers with the coordinator, then repeatedly leases a chunk and processes its
identifiers in small batches, reporting progress (a ``next_id`` checkpoint) after
each batch so a crash never loses more than one batch of work.
"""

from __future__ import annotations

import asyncio
import logging

from app.checker import Checker
from app.coordinator_client import CoordinatorClient
from app.core.config import Settings
from vidhive_common.enums import ItemStatus
from vidhive_common.schemas import (
    ChunkLease,
    Heartbeat,
    ItemResult,
    ProgressReport,
    WorkerRegister,
)

log = logging.getLogger("vidhive.agent")


class WorkerRunner:
    def __init__(self, settings: Settings, client: CoordinatorClient, checker: Checker) -> None:
        self.settings = settings
        self.client = client
        self.checker = checker
        self.worker_id: int | None = None
        self._sem = asyncio.Semaphore(settings.threads)
        self._stop = asyncio.Event()
        self._active_checks = 0
        self._active_downloads = 0

    # -- lifecycle ---------------------------------------------------------- #
    async def register(self) -> int:
        payload = WorkerRegister(
            name=self.settings.worker_name,
            agent_url=self.settings.advertised_url(),
            agent_version="0.3.0",
            threads=self.settings.threads,
            storage_path=self.settings.storage_path,
        )
        self.worker_id = await self.client.register(payload)
        log.info("registered as worker_id=%s (%s)", self.worker_id, self.settings.worker_name)
        return self.worker_id

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.register()
        hb_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._lease_loop()
        finally:
            hb_task.cancel()

    # -- loops -------------------------------------------------------------- #
    async def _lease_loop(self) -> None:
        assert self.worker_id is not None
        while not self._stop.is_set():
            try:
                chunk = await self.client.lease(self.worker_id, self.settings.lease_seconds)
            except Exception as exc:  # noqa: BLE001 - keep the agent alive on transient errors
                log.warning("lease failed: %s", exc)
                await self._sleep(self.settings.poll_interval)
                continue

            if chunk is None:
                await self._sleep(self.settings.poll_interval)
                continue

            await self.process_chunk(chunk)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.settings.heartbeat_interval)
            if self.worker_id is None:
                continue
            try:
                await self.client.heartbeat(
                    self.worker_id,
                    Heartbeat(
                        active_checks=self._active_checks,
                        active_downloads=self._active_downloads,
                    ),
                    self.settings.lease_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("heartbeat failed: %s", exc)

    # -- chunk processing --------------------------------------------------- #
    async def process_chunk(self, chunk: ChunkLease) -> None:
        assert self.worker_id is not None
        start = chunk.next_id
        end = chunk.range_end
        batch = self.settings.progress_batch
        log.info("chunk %s: processing %s..%s", chunk.chunk_id, start, end)

        cur = start
        while cur <= end and not self._stop.is_set():
            batch_end = min(cur + batch, end + 1)  # exclusive
            ids = range(cur, batch_end)
            results = await asyncio.gather(*(self._handle_id(i) for i in ids))
            try:
                await self.client.progress(
                    self.worker_id,
                    ProgressReport(chunk_id=chunk.chunk_id, next_id=batch_end, items=results),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("progress report failed: %s", exc)
                return  # lease will expire and the chunk returns to the queue
            cur = batch_end

        if not self._stop.is_set():
            try:
                await self.client.complete(self.worker_id, chunk.chunk_id)
                log.info("chunk %s: complete", chunk.chunk_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("complete failed: %s", exc)

    async def _handle_id(self, external_id: int) -> ItemResult:
        async with self._sem:
            self._active_checks += 1
            try:
                result = await self.checker.check(external_id)
            finally:
                self._active_checks -= 1

            if result.status == ItemStatus.FOUND and self.settings.enable_download:
                self._active_downloads += 1
                try:
                    file = await self.checker.download(external_id, result)
                    result.status = ItemStatus.COMPLETED
                    result.size_bytes = file.size_bytes or result.size_bytes
                    result.mime_type = file.mime_type or result.mime_type
                except Exception as exc:  # noqa: BLE001 - a failed download is not fatal
                    result.status = ItemStatus.FAILED
                    result.error = str(exc)[:200]
                finally:
                    self._active_downloads -= 1
            return result

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
