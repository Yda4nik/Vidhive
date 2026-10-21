"""Live per-agent download progress, kept in memory only.

Agents report what they are downloading with every heartbeat; the dashboard polls it.
It is display-only and changes every few seconds, so it is not worth a database row —
after a coordinator restart it refills on the next heartbeat.
"""

from __future__ import annotations

import time

from vidhive_common.schemas import DownloadProgress

_STALE_SECONDS = 30.0  # an agent that stopped heartbeating shows nothing

_state: dict[int, tuple[float, list[DownloadProgress]]] = {}


def record(worker_id: int, downloads: list[DownloadProgress]) -> None:
    _state[worker_id] = (time.monotonic(), list(downloads))


def forget(worker_id: int) -> None:
    _state.pop(worker_id, None)


def snapshot() -> dict[int, list[dict]]:
    """worker_id -> [{external_id, percent, title}] for agents heard from recently."""
    now = time.monotonic()
    return {
        wid: [d.model_dump() for d in downloads]
        for wid, (ts, downloads) in _state.items()
        if now - ts <= _STALE_SECONDS and downloads
    }
