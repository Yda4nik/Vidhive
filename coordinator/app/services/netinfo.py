"""Where a request really came from: directly (home LAN, Tailscale) or from the
public internet through a tunnel / reverse proxy.

Why this matters: behind a public tunnel every request reaches the coordinator from
the tunnel process (127.0.0.1 or a Docker bridge), so ``request.client.host`` is the
same for every visitor. The brute-force throttle would then treat the whole internet
as one client (one attacker could lock everybody out), and nothing could tell a
visitor on the home network from one on the internet.

A request is **public** when it carries proxy headers (set by Cloudflare Tunnel,
Tailscale Funnel/serve, nginx, ...) or when its TCP peer is a globally routable
address (e.g. someone port-forwarded the port on the router). A browser on the home
network or on the tailnet sends neither.

The forwarded headers can be forged by someone who already sits on the LAN. That is
acceptable: it can only make a request look *more* public (more restricted), or let
a LAN host pick its own throttle bucket — and a LAN host is already inside the wall.
A visitor from the internet can only arrive through the tunnel, which overwrites them.
"""

from __future__ import annotations

import ipaddress
import json

from starlette.requests import Request

# Headers a tunnel/proxy adds. A browser on the home network never sends them.
PROXY_HEADERS = (
    "cf-connecting-ip",
    "cf-ray",
    "x-forwarded-for",
    "forwarded",
    "x-real-ip",
    "tailscale-funnel-request",
)


def peer(request: Request) -> str:
    return request.client.host if request.client else ""


def peer_is_global(request: Request) -> bool:
    """True when the TCP peer is a public internet address (not LAN/CGNAT/loopback)."""
    try:
        return ipaddress.ip_address(peer(request)).is_global
    except ValueError:  # "testclient", unix socket, empty
        return False


def is_proxied(request: Request) -> bool:
    return any(h in request.headers for h in PROXY_HEADERS)


def is_public(request: Request) -> bool:
    return is_proxied(request) or peer_is_global(request)


def _valid_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """The best-known address of the visitor, for rate limiting and logs.

    Order: ``CF-Connecting-IP`` (Cloudflare sets/overwrites it), then the *last*
    ``X-Forwarded-For`` entry (the one the single trusted proxy appended — earlier
    entries are client-supplied and forgeable), then ``X-Real-IP``, then the TCP peer.
    Anything that isn't a valid IP is ignored, so the throttle can't be fed junk keys.
    """
    h = request.headers
    candidates = [h.get("cf-connecting-ip", "")]
    xff = [p for p in h.get("x-forwarded-for", "").split(",") if p.strip()]
    if xff:
        candidates.append(xff[-1])
    candidates.append(h.get("x-real-ip", ""))
    for c in candidates:
        ip = _valid_ip(c)
        if ip:
            return ip
    return peer(request) or "unknown"


def is_https(request: Request) -> bool:
    """Did the visitor use https? (the tunnel terminates TLS and talks http to us)"""
    if request.scope.get("scheme") == "https":
        return True
    h = request.headers
    if h.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https":
        return True
    if "tailscale-funnel-request" in h:       # Funnel terminates TLS and talks http to us
        return True
    visitor = h.get("cf-visitor", "")
    if visitor:
        try:
            return json.loads(visitor).get("scheme") == "https"
        except (ValueError, AttributeError):
            return False
    return False
