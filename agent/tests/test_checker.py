"""Unit tests for the two-phase checker's classification (offline)."""

import asyncio

import httpx

from app.checker import Checker
from app.core.config import Settings
from vidhive_common.enums import ItemStatus


def _checker(status: int, probe=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="body")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Checker(Settings(), client, probe=probe or (lambda url: {"title": "t", "webpage_url": url}))


def _run(coro):
    return asyncio.run(coro)


def test_forbidden():
    assert _run(_checker(403).check(5)).status == ItemStatus.FORBIDDEN


def test_rate_limited():
    assert _run(_checker(429).check(5)).status == ItemStatus.RATE_LIMITED


def test_not_found():
    assert _run(_checker(404).check(5)).status == ItemStatus.NOT_FOUND


def test_server_error_retries():
    assert _run(_checker(503).check(5)).status == ItemStatus.RETRY_WAIT


def test_found_with_metadata():
    c = _checker(200, probe=lambda url: {"title": "clip", "webpage_url": url, "ext": "mp4", "filesize": 123})
    r = _run(c.check(200673499))
    assert r.status == ItemStatus.FOUND
    assert r.title == "clip"
    assert r.size_bytes == 123


def test_page_exists_but_no_media_is_not_found():
    assert _run(_checker(200, probe=lambda url: None).check(1)).status == ItemStatus.NOT_FOUND


def test_probe_error_is_not_found():
    def boom(url):
        raise RuntimeError("no media")

    assert _run(_checker(200, probe=boom).check(1)).status == ItemStatus.NOT_FOUND
