"""Coordinator <-> agent hardening: the token is sent on file calls, and an agent
can't register an address that would turn the coordinator into an SSRF proxy."""

import httpx
import pytest

from app.core import config
from app.services.agent_client import agent_headers, file_url, validate_agent_url


@pytest.fixture
def agent_token(monkeypatch):
    """Turn the shared agent token on (and back off) for one test."""

    def _set(value):
        monkeypatch.setenv("VIDHIVE_AGENT_TOKEN", value)
        config.get_settings.cache_clear()

    yield _set
    monkeypatch.setenv("VIDHIVE_AGENT_TOKEN", "")
    config.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# agent_url validation (SSRF)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    ["http://agent-01:8100", "http://10.0.0.5:8100", "https://vps.example.com:8100",
     "http://192.168.1.50:8100/", "http://localhost:8100", None, ""],
)
def test_valid_agent_urls_are_accepted(url):
    assert validate_agent_url(url) is None


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254", "http://169.254.169.254/latest/meta-data",     # cloud metadata
     "http://0.0.0.0:8100", "http://[fe80::1]:8100", "http://224.0.0.1:8100",
     "file:///etc/passwd", "gopher://x", "ftp://x", "http://",
     "http://user:pw@host:8100", "http://host:8100/x/y", "http://host:8100?x=1",
     "http://host:8100#f", "http://host:99999", "http://host:abc"],
)
def test_dangerous_agent_urls_are_rejected(url):
    assert validate_agent_url(url) is not None


def test_register_rejects_an_ssrf_address(client):
    r = client.post("/api/workers/register",
                    json={"name": "evil", "agent_url": "http://169.254.169.254"})
    assert r.status_code == 422
    assert client.get("/api/workers").json() == []


def test_register_accepts_a_normal_address(client):
    r = client.post("/api/workers/register",
                    json={"name": "ok", "agent_url": "http://agent-01:8100"})
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# The token is attached to calls the coordinator makes to agents
# --------------------------------------------------------------------------- #
def test_helpers(agent_token):
    assert agent_headers() == {}
    agent_token("tok")
    assert agent_headers() == {"X-Agent-Token": "tok"}
    assert file_url("http://a:8100/", 5) == "http://a:8100/files/5"


def _completed_item(client):
    wid = client.post("/api/workers/register",
                      json={"name": "agent-01", "agent_url": "http://agent-01:8100"}).json()["id"]
    job = client.post("/api/jobs", json={"range_start": 5, "range_end": 5, "chunk_size": 1}).json()
    client.post(f"/api/jobs/{job['id']}/start")
    lease = client.post(f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}).json()
    client.post(f"/api/workers/{wid}/progress", json={
        "chunk_id": lease["chunk_id"], "next_id": 6,
        "items": [{"external_id": 5, "status": "completed", "title": "clip",
                   "storage_path": "/data/videos/0/5/video.mp4"}]})
    return client.get(f"/api/jobs/{job['id']}/items").json()[0]["id"]


def _capture_agent_calls(monkeypatch):
    """Route every httpx.AsyncClient the app creates to an in-memory fake agent."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("x-agent-token")))
        if request.method == "GET":
            return httpx.Response(200, content=b"abc", headers={"content-type": "video/mp4"})
        return httpx.Response(200, json={"deleted": True})

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def test_stream_and_download_and_delete_send_the_token(client, agent_token, monkeypatch):
    item_id = _completed_item(client)          # seeded while the token is still off
    agent_token("tok")
    seen = _capture_agent_calls(monkeypatch)

    assert client.get(f"/watch/{item_id}").status_code == 200
    assert client.get(f"/download/{item_id}").status_code == 200
    assert client.post("/api/library/delete", json={"item_ids": [item_id]}).status_code == 200

    calls = {(m, u.split("/files/")[0]): t for m, u, t in seen}
    assert len(seen) == 3
    assert all(url == "http://agent-01:8100/files/5" for _, url, _ in seen)
    assert all(token == "tok" for _, _, token in seen)


def test_without_a_token_no_header_is_sent(client, monkeypatch):
    item_id = _completed_item(client)
    seen = _capture_agent_calls(monkeypatch)
    client.get(f"/watch/{item_id}")
    assert seen and seen[0][2] is None
