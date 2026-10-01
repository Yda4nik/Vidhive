"""The chunk processing loop: batched checkpoints, downloads, completion."""

import asyncio
from datetime import datetime, timezone

from app.checker import DownloadedFile
from app.core.config import Settings
from app.worker import WorkerRunner
from vidhive_common.enums import ItemStatus
from vidhive_common.schemas import ChunkLease, ItemResult


class FakeChecker:
    def __init__(self):
        self.downloaded = []

    async def check(self, external_id, template=None):
        found = external_id % 2 == 0  # even ids "have" a video
        return ItemResult(
            external_id=external_id,
            status=ItemStatus.FOUND if found else ItemStatus.NOT_FOUND,
            title="clip" if found else None,
            download_url="u",
        )

    async def download(self, external_id, found):
        self.downloaded.append(external_id)
        return DownloadedFile(path=f"/x/{external_id}", size_bytes=10, mime_type="mp4")


class FakeClient:
    def __init__(self, proceed=True, fail_with=None):
        self.progress_calls = []
        self.completed = []
        self._proceed = proceed          # what the "coordinator" answers to every progress report
        self._fail_with = fail_with      # raise this on progress (e.g. a 409: lease lost)

    async def progress(self, worker_id, report):
        self.progress_calls.append(report)
        if self._fail_with is not None:
            raise self._fail_with
        return self._proceed

    async def complete(self, worker_id, chunk_id):
        self.completed.append(chunk_id)


def test_process_chunk_checkpoints_and_downloads():
    settings = Settings(progress_batch=3, threads=4, enable_download=True)
    client, checker = FakeClient(), FakeChecker()
    runner = WorkerRunner(settings, client, checker)
    runner.worker_id = 1

    lease = ChunkLease(
        chunk_id=7, job_id=1, range_start=0, range_end=5, next_id=0,
        lease_expires_at=datetime.now(timezone.utc),
    )
    asyncio.run(runner.process_chunk(lease))

    # ids 0..5 in batches of 3 => checkpoints at next_id 3 and 6.
    assert [r.next_id for r in client.progress_calls] == [3, 6]
    assert client.completed == [7]

    # Even ids were downloaded and reported completed; odd ids not_found.
    assert sorted(checker.downloaded) == [0, 2, 4]
    items = [i for r in client.progress_calls for i in r.items]
    completed = sorted(i.external_id for i in items if i.status == ItemStatus.COMPLETED)
    not_found = sorted(i.external_id for i in items if i.status == ItemStatus.NOT_FOUND)
    assert completed == [0, 2, 4]
    assert not_found == [1, 3, 5]


def test_resume_from_checkpoint():
    """A chunk handed back with next_id=4 only processes 4..5."""
    settings = Settings(progress_batch=10, threads=2, enable_download=False)
    client, checker = FakeClient(), FakeChecker()
    runner = WorkerRunner(settings, client, checker)
    runner.worker_id = 1

    lease = ChunkLease(
        chunk_id=9, job_id=1, range_start=0, range_end=5, next_id=4,
        lease_expires_at=datetime.now(timezone.utc),
    )
    asyncio.run(runner.process_chunk(lease))

    items = sorted(i.external_id for r in client.progress_calls for i in r.items)
    assert items == [4, 5]
    assert client.progress_calls[-1].next_id == 6


def _lease(chunk_id=11):
    return ChunkLease(
        chunk_id=chunk_id, job_id=1, range_start=0, range_end=8, next_id=0,
        lease_expires_at=datetime.now(timezone.utc),
    )


def test_stops_after_the_batch_when_the_job_is_paused_or_stopped():
    """proceed=False: the batch is reported, then no more batches and NO complete."""
    settings = Settings(progress_batch=3, threads=4, enable_download=False)
    client = FakeClient(proceed=False)
    runner = WorkerRunner(settings, client, FakeChecker())
    runner.worker_id = 1
    asyncio.run(runner.process_chunk(_lease()))

    assert len(client.progress_calls) == 1          # 9 ids / batches of 3, but stopped after the first
    assert client.completed == []                   # the coordinator already released the chunk
    assert runner._active_chunk_id is None          # lease no longer renewed by heartbeats


def test_abandons_the_chunk_when_the_lease_is_lost():
    """A 409 from the coordinator (another agent owns the chunk) must end processing."""
    import httpx

    err = httpx.HTTPStatusError(
        "409", request=httpx.Request("POST", "http://c/x"), response=httpx.Response(409)
    )
    settings = Settings(progress_batch=3, threads=4, enable_download=False)
    client = FakeClient(fail_with=err)
    runner = WorkerRunner(settings, client, FakeChecker())
    runner.worker_id = 1
    asyncio.run(runner.process_chunk(_lease()))

    assert len(client.progress_calls) == 1 and client.completed == []


def test_an_old_coordinator_without_the_field_keeps_going():
    """A reply with no 'proceed' (older coordinator) must behave as before."""
    settings = Settings(progress_batch=3, threads=4, enable_download=False)
    client = FakeClient(proceed=None)               # progress() returns None like the old API
    runner = WorkerRunner(settings, client, FakeChecker())
    runner.worker_id = 1
    asyncio.run(runner.process_chunk(_lease()))
    assert [r.next_id for r in client.progress_calls] == [3, 6, 9] and client.completed == [11]
