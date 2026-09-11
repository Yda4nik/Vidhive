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
    # Tiny retry delays so transient-status tests don't actually wait for backoff.
    settings = Settings(retry_base_delay=0.001, retry_max_delay=0.01, max_retries=2)
    return Checker(settings, client, probe=probe or (lambda url: {"title": "t", "webpage_url": url}))


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


def _fast_settings():
    return Settings(retry_base_delay=0.001, retry_max_delay=0.01, max_retries=4)


def test_check_retries_then_found():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = Checker(_fast_settings(), client, probe=lambda url: {"title": "t", "webpage_url": url})
    r = _run(c.check(1))
    assert r.status == ItemStatus.FOUND
    assert calls["n"] == 3  # retried twice, succeeded on the third


def test_check_gives_up_after_max_retries():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "0"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = Settings(retry_base_delay=0.001, retry_max_delay=0.01, max_retries=2)
    r = _run(Checker(s, client).check(1))
    assert r.status == ItemStatus.RATE_LIMITED
