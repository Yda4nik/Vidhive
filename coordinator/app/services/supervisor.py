"""Client for the supervisor service (public link and VPN switches).

The coordinator never touches Docker itself; it asks the supervisor, which only knows a
handful of whitelisted actions and demands the shared agent token.
"""

from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.services.agent_client import agent_headers

_OFF = {"available": False}


async def _call(method: str, path: str, payload: dict | None = None) -> dict:
    base = get_settings().supervisor_url.rstrip("/")
    if not base:
        return {**_OFF, "error": "Сторож не настроен (VIDHIVE_SUPERVISOR_URL пуст)"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=5.0)) as client:
            resp = await client.request(method, base + path, json=payload, headers=agent_headers())
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {**_OFF, "error": f"Сторож недоступен: {exc.__class__.__name__}"}
    if resp.status_code >= 400:
        return {**_OFF, "error": data.get("error", f"ошибка {resp.status_code}")}
    return data


async def status() -> dict:
    return await _call("GET", "/status")


async def set_tunnel(enabled: bool) -> dict:
    return await _call("POST", "/tunnel", {"enabled": enabled})


async def set_vpn(targets: list[str], enabled: bool) -> dict:
    return await _call("POST", "/vpn", {"targets": targets, "enabled": enabled})
