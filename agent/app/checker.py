"""Two-phase identifier processing: cheap availability check, then download.

Network work (the HTTP probe and yt-dlp) is injectable so the classification
logic can be tested offline.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from app.core.config import Settings
from app.ratelimit import AsyncRateLimiter
from vidhive_common.enums import ItemStatus
from vidhive_common.schemas import ItemResult


@dataclass
class DownloadedFile:
    path: str
    size_bytes: int | None
    mime_type: str | None


def target_dir(storage_path: str, external_id: int) -> Path:
    """`{storage}/{id-prefix}/{id}/` — id is the directory name, prefix buckets it."""
    prefix = external_id // 1_000_000
    return Path(storage_path) / str(prefix) / str(external_id)


def _default_probe(url: str) -> dict | None:
    from yt_dlp import YoutubeDL  # imported lazily; not needed for tests

    opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _default_download(
    url: str, out_template: str, fmt: str, concurrency: int
) -> tuple[str, dict]:
    from yt_dlp import YoutubeDL

    opts = {
        # Merge the best separate video+audio streams (kinescope serves adaptive
        # HLS, so a single progressive "best" file usually does not exist).
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": out_template,
        "continuedl": True,                       # resume a partial .part download
        "concurrent_fragment_downloads": concurrency,  # HLS has many small fragments
        "retries": 3,
        "fragment_retries": 3,
        "noplaylist": True,
        "quiet": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info


class Checker:
    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient,
        probe: Callable[[str], dict | None] | None = None,
        download_fn: Callable[[str, str], tuple[str, dict]] | None = None,
        limiter: AsyncRateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.http = http_client
        self._probe = probe or _default_probe
        self._download = download_fn or _default_download
        self.limiter = limiter

    def _url(self, external_id: int, template: str | None = None) -> str:
        return (template or self.settings.target_template).format(id=external_id)

    def _backoff(self, attempt: int) -> float:
        base, cap = self.settings.retry_base_delay, self.settings.retry_max_delay
        return min(cap, base * (2 ** attempt)) + random.uniform(0, base)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("retry-after")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    async def check(self, external_id: int, template: str | None = None) -> ItemResult:
        """Phase 1 (status) + phase 2 (metadata) with retries. Never downloads.

        ``template`` (from the lease) selects the source; falls back to the
        agent's configured target. Transient conditions (429, 5xx, timeout) are
        retried with exponential backoff + jitter, honouring Retry-After.
        """
        url = self._url(external_id, template)
        attempt = 0
        while True:
            if self.limiter is not None:
                await self.limiter.acquire()
            try:
                resp = await self.http.get(url, follow_redirects=True)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.settings.max_retries:
                    return ItemResult(external_id=external_id, status=ItemStatus.RETRY_WAIT, error=str(exc)[:200] or "timeout")
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue

            st = resp.status_code
            if st == 429:
                if attempt >= self.settings.max_retries:
                    return ItemResult(external_id=external_id, status=ItemStatus.RATE_LIMITED)
                await asyncio.sleep(self._retry_after(resp) or self._backoff(attempt))
                attempt += 1
                continue
            if st >= 500:
                if attempt >= self.settings.max_retries:
                    return ItemResult(external_id=external_id, status=ItemStatus.RETRY_WAIT, error=f"http {st}")
                await asyncio.sleep(self._backoff(attempt))
                attempt += 1
                continue
            break  # terminal status reached

        if st in (401, 403):
            return ItemResult(external_id=external_id, status=ItemStatus.FORBIDDEN)
        if st in (404, 410):
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)
        if st != 200:
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)

        # Page exists — confirm a real downloadable stream via metadata.
        try:
            info = await asyncio.to_thread(self._probe, url)
        except Exception as exc:  # noqa: BLE001 - a probe failure means "no media here"
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND, error=str(exc)[:200])

        if not info:
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)

        duration = info.get("duration")
        return ItemResult(
            external_id=external_id,
            status=ItemStatus.FOUND,
            title=info.get("title"),
            download_url=info.get("webpage_url") or url,
            size_bytes=info.get("filesize") or info.get("filesize_approx"),
            duration_seconds=int(duration) if duration else None,
            mime_type=info.get("ext"),
        )

    async def download(self, external_id: int, found: ItemResult) -> DownloadedFile:
        """Phase 3: download best quality to `{storage}/{prefix}/{id}/`."""
        dest = target_dir(self.settings.storage_path, external_id)
        dest.mkdir(parents=True, exist_ok=True)
        out_template = str(dest / "video.%(ext)s")
        url = found.download_url or self._url(external_id)

        path, info = await asyncio.to_thread(
            self._download,
            url,
            out_template,
            self.settings.download_format,
            self.settings.fragment_concurrency,
        )

        # After a merge the final file may differ from prepare_filename's guess;
        # trust the finished file on disk (the non-.part video.*).
        finished = [p for p in dest.glob("video.*") if p.suffix != ".part"]
        if finished:
            path = str(max(finished, key=lambda p: p.stat().st_size))

        # Persist metadata alongside the file (title kept here, not in the filename).
        try:
            (dest / "metadata.json").write_text(
                json.dumps(
                    {
                        "external_id": external_id,
                        "title": found.title or info.get("title"),
                        "source_url": url,
                        "ext": info.get("ext"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

        size = None
        try:
            size = Path(path).stat().st_size
        except OSError:
            pass
        return DownloadedFile(path=path, size_bytes=size, mime_type=info.get("ext"))
