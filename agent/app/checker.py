"""Two-phase identifier processing: cheap availability check, then download.

Network work (the HTTP probe and yt-dlp) is injectable so the classification
logic can be tested offline.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from app.core.config import Settings
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


def _default_download(url: str, out_template: str) -> tuple[str, dict]:
    from yt_dlp import YoutubeDL

    opts = {
        "format": "best",       # best available quality
        "outtmpl": out_template,
        "continuedl": True,     # resume a partial .part download
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
    ) -> None:
        self.settings = settings
        self.http = http_client
        self._probe = probe or _default_probe
        self._download = download_fn or _default_download

    def _url(self, external_id: int) -> str:
        return self.settings.target_template.format(id=external_id)

    async def check(self, external_id: int) -> ItemResult:
        """Phase 1 (status) + phase 2 (metadata). Never downloads."""
        url = self._url(external_id)
        try:
            resp = await self.http.get(url, follow_redirects=True)
        except httpx.TimeoutException:
            return ItemResult(external_id=external_id, status=ItemStatus.RETRY_WAIT, error="timeout")
        except httpx.HTTPError as exc:
            return ItemResult(external_id=external_id, status=ItemStatus.RETRY_WAIT, error=str(exc))

        st = resp.status_code
        if st in (401, 403):
            return ItemResult(external_id=external_id, status=ItemStatus.FORBIDDEN)
        if st == 429:
            return ItemResult(external_id=external_id, status=ItemStatus.RATE_LIMITED)
        if st in (404, 410):
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)
        if st >= 500:
            return ItemResult(external_id=external_id, status=ItemStatus.RETRY_WAIT, error=f"http {st}")
        if st != 200:
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)

        # Page exists — confirm a real downloadable stream via metadata.
        try:
            info = await asyncio.to_thread(self._probe, url)
        except Exception as exc:  # noqa: BLE001 - a probe failure means "no media here"
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND, error=str(exc)[:200])

        if not info:
            return ItemResult(external_id=external_id, status=ItemStatus.NOT_FOUND)

        return ItemResult(
            external_id=external_id,
            status=ItemStatus.FOUND,
            title=info.get("title"),
            download_url=info.get("webpage_url") or url,
            size_bytes=info.get("filesize") or info.get("filesize_approx"),
            mime_type=info.get("ext"),
        )

    async def download(self, external_id: int, found: ItemResult) -> DownloadedFile:
        """Phase 3: download best quality to `{storage}/{prefix}/{id}/`."""
        dest = target_dir(self.settings.storage_path, external_id)
        dest.mkdir(parents=True, exist_ok=True)
        out_template = str(dest / "video.%(ext)s")
        url = found.download_url or self._url(external_id)

        path, info = await asyncio.to_thread(self._download, url, out_template)

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
