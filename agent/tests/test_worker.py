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

    async def check(self, external_id):
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
    def __init__(self):
        self.progress_calls = []
        self.completed = []

    async def progress(self, worker_id, report):
        self.progress_calls.append(report)

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
