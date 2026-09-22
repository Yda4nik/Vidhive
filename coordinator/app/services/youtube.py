"""YouTube link handling for the coordinator.

A YouTube video has an 11-character id, not a numeric one, and is downloaded by
URL. We give each video a stable synthetic numeric ``external_id`` (a hash of its
id) so it fits the existing pipeline (dedup, library, notes), and carry the real
URL on the chunk. A playlist URL is expanded here into its individual videos so
they distribute across agents like any other work.
"""

from __future__ import annotations

import hashlib
import re

_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def yt_external_id(video_id: str) -> int:
    """Stable positive BigInt from a YouTube video id (same video -> same id)."""
    return int.from_bytes(hashlib.sha1(video_id.encode()).digest()[:7], "big")


def video_id_from_url(url: str) -> str | None:
    m = _YT_ID_RE.search(url or "")
    return m.group(1) if m else None


def _extract(url: str) -> list[dict]:
    """List the video(s) for a URL (single video or playlist) via yt-dlp (flat).

    Returns dicts with ``id``, ``url`` and ``title``. Imported lazily so tests can
    monkeypatch this function without yt-dlp doing any network I/O.
    """
    from yt_dlp import YoutubeDL

    # socket_timeout keeps a blocked/slow YouTube from hanging the /add request.
    opts = {"quiet": True, "skip_download": True, "extract_flat": "in_playlist",
            "socket_timeout": 15, "retries": 1}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries")
    if entries is None:  # a single video
        vid = info.get("id")
        return [{"id": vid, "url": info.get("webpage_url") or url, "title": info.get("title")}]

    videos: list[dict] = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        vurl = e.get("url") or e.get("webpage_url")
        if vid and (not vurl or "watch?v=" not in (vurl or "")):
            vurl = "https://www.youtube.com/watch?v=" + vid
        videos.append({"id": vid, "url": vurl, "title": e.get("title")})
    return videos


def expand(url: str) -> list[dict]:
    """Public entry point (indirection kept so tests can patch ``_extract``)."""
    return _extract(url)
