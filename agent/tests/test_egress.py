"""When the VPN route cannot do the job the agent uses the direct route until the chunk is done."""

import asyncio
import os
from datetime import datetime, timezone

from app.checker import DownloadedFile
from app.core.config import Settings
from app.egress import EgressManager
from app.worker import WorkerRunner
from vidhive_common.enums import ItemStatus
from vidhive_common.schemas import ChunkLease, ItemResult

VPN, DIRECT = "172.16.1.9", "172.16.1.1"


class FakeIp:
    """`ip route show default` / `ip route replace default via X` on a fake routing table."""

    def __init__(self, gw=VPN):
        self.gw = gw

    def __call__(self, args):
        if args[:2] == ["route", "show"]:
            return f"default via {self.gw} dev eth0\n"
        if args[:3] == ["route", "replace", "default"]:
            self.gw = args[-1]
        return ""


def make(tmp_path, gw=VPN):
    (tmp_path / "direct_gw").write_text(DIRECT)
    ip = FakeIp(gw)
    return EgressManager(run=ip, state_dir=str(tmp_path)), ip


def test_fall_back_switches_route_sets_flag_and_restore_goes_back(tmp_path):
    async def go():
        eg, ip = make(tmp_path)
        assert await eg.fall_back(eg.generation) is True
        assert ip.gw == DIRECT and (tmp_path / "direct").exists() and eg.fallen_back
        await eg.restore()
        assert ip.gw == VPN and not (tmp_path / "direct").exists() and not eg.fallen_back

    asyncio.run(go())


def test_no_fallback_when_the_agent_is_not_on_a_vpn_route(tmp_path):
    async def go():
        eg, ip = make(tmp_path, gw=DIRECT)
        assert await eg.fall_back(eg.generation) is False
        assert ip.gw == DIRECT and not (tmp_path / "direct").exists()

    asyncio.run(go())


def test_a_second_failure_after_the_switch_retries_without_switching_again(tmp_path):
    async def go():
        eg, ip = make(tmp_path)
        seen = eg.generation
        assert await eg.fall_back(seen) is True               # first failing download switches
        gen_after = eg.generation
        assert await eg.fall_back(seen) is True               # a sibling that started earlier just retries
        assert eg.generation == gen_after                     # ...without another switch

    asyncio.run(go())


def test_restore_without_fallback_does_nothing(tmp_path):
    async def go():
        eg, ip = make(tmp_path)
        await eg.restore()
        assert ip.gw == VPN

    asyncio.run(go())


# ---- the worker uses it ---------------------------------------------------- #
class FakeClient:
    def __init__(self):
        self.progress_calls, self.completed = [], []

    async def progress(self, worker_id, report):
        self.progress_calls.append(report)
        return True

    async def complete(self, worker_id, chunk_id):
        self.completed.append(chunk_id)


class FlakyChecker:
    """Finds every id, but a download only works once the agent is off the VPN route."""

    def __init__(self, ip):
        self.ip, self.attempts = ip, 0

    async def check(self, external_id, template=None):
        return ItemResult(external_id=external_id, status=ItemStatus.FOUND, title="t", download_url="http://x")

    async def download(self, external_id, found, on_progress=None):
        self.attempts += 1
        if self.ip.gw != DIRECT:
            raise RuntimeError("VPN path timed out")
        return DownloadedFile(path=f"/data/{external_id}.mp4", size_bytes=1, mime_type="mp4")


def _lease(chunk_id, a, b):
    return ChunkLease(chunk_id=chunk_id, job_id=1, range_start=a, range_end=b, next_id=a,
                      lease_expires_at=datetime.now(timezone.utc))


def test_worker_retries_a_failed_download_on_the_direct_route_and_returns_to_vpn(tmp_path):
    eg, ip = make(tmp_path)
    checker = FlakyChecker(ip)
    runner = WorkerRunner(Settings(progress_batch=5, threads=2, enable_download=True), FakeClient(), checker)
    runner.egress, runner.worker_id = eg, 1
    asyncio.run(runner.process_chunk(_lease(3, 10, 11)))
    items = [i for r in runner.client.progress_calls for i in r.items]
    assert len(items) == 2 and all(i.status == ItemStatus.COMPLETED for i in items)
    assert ip.gw == VPN                                        # the chunk is done: back on the VPN
    assert not os.path.exists(tmp_path / "direct")


def test_worker_does_not_fall_back_when_the_download_works_on_the_vpn(tmp_path):
    eg, ip = make(tmp_path)

    class Fine(FlakyChecker):
        async def download(self, external_id, found, on_progress=None):
            self.attempts += 1
            return DownloadedFile(path="/d", size_bytes=1, mime_type="mp4")

    checker = Fine(ip)
    runner = WorkerRunner(Settings(progress_batch=5, threads=2, enable_download=True), FakeClient(), checker)
    runner.egress, runner.worker_id = eg, 1
    asyncio.run(runner.process_chunk(_lease(4, 1, 1)))
    assert eg.generation == 0 and checker.attempts == 1


def test_only_youtube_needs_the_vpn():
    from app.worker import needs_vpn
    assert needs_vpn("https://www.youtube.com/watch?v=CCPsDy-LH0U") and needs_vpn("https://youtu.be/x")
    assert needs_vpn(None)                                   # unknown: keep the current route
    assert not needs_vpn("https://kinescope.io/{id}") and not needs_vpn("http://mock:8090/{id}")


def test_a_kinescope_chunk_runs_direct_and_returns_to_the_vpn(tmp_path):
    eg, ip = make(tmp_path)
    seen = []

    class Probe(FlakyChecker):
        async def check(self, external_id, template=None):
            seen.append(ip.gw)                               # which route is used for the work
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)

    runner = WorkerRunner(Settings(progress_batch=5, threads=2, enable_download=True), FakeClient(), Probe(ip))
    runner.egress, runner.worker_id = eg, 1
    lease = _lease(5, 1, 2)
    lease.target_template = "https://kinescope.io/{id}"
    asyncio.run(runner.process_chunk(lease))
    assert seen == [DIRECT, DIRECT] and ip.gw == VPN         # direct while working, VPN afterwards


def test_a_youtube_chunk_keeps_the_vpn(tmp_path):
    eg, ip = make(tmp_path)
    seen = []

    class Probe(FlakyChecker):
        async def check(self, external_id, template=None):
            seen.append(ip.gw)
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)

    runner = WorkerRunner(Settings(progress_batch=5, threads=2, enable_download=True), FakeClient(), Probe(ip))
    runner.egress, runner.worker_id = eg, 1
    lease = _lease(6, 1, 1)
    lease.target_template = "https://www.youtube.com/watch?v=CCPsDy-LH0U"
    asyncio.run(runner.process_chunk(lease))
    assert seen == [VPN] and eg.generation == 0
