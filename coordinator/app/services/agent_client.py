"""The coordinator -> agent direction: authenticating to an agent and vetting the
address an agent asks us to call.

Agents serve video files over HTTP (``/files/{id}``) and the coordinator streams or
deletes them. Two risks are handled here:

* an agent's file endpoints must not be open to the world (they are reachable on a
  public IP for SSH-deployed agents) — the coordinator sends the shared agent token;
* ``agent_url`` is supplied by the agent at registration, so it must not be usable to
  make the coordinator call arbitrary internal services (SSRF), e.g. the cloud
  metadata address 169.254.169.254.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from app.core.config import get_settings


def agent_headers() -> dict[str, str]:
    """Headers authenticating the coordinator to an agent (empty if no token set)."""
    token = get_settings().agent_token
    return {"X-Agent-Token": token} if token else {}


def file_url(agent_url: str, external_id: int) -> str:
    return f"{agent_url.rstrip('/')}/files/{int(external_id)}"


def validate_agent_url(url: str | None) -> str | None:
    """Return a human-readable error if ``url`` is not an acceptable agent address."""
    if not url:
        return None  # optional
    try:
        p = urlparse(url)
        port = p.port  # raises ValueError when out of range / not a number
    except ValueError:
        return "agent_url: некорректный адрес или порт"
    if p.scheme not in ("http", "https") or not p.hostname:
        return "agent_url: ожидается http(s)://хост[:порт]"
    if p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
        return "agent_url: допустим только адрес хоста (без пути, параметров и логина)"
    if port is not None and not (1 <= port <= 65535):
        return "agent_url: порт вне диапазона"
    try:
        ip = ipaddress.ip_address(p.hostname)
    except ValueError:
        return None  # a DNS name (e.g. a Docker service name) — resolved only when used
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return "agent_url: этот адрес недопустим (link-local/служебный)"
    return None
