"""The agent's file endpoints must not be open when a token is configured."""

import pytest
from fastapi.testclient import TestClient

from app.core import config


def _app(monkeypatch, tmp_path, token):
    monkeypatch.setenv("VIDHIVE_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("VIDHIVE_AGENT_TOKEN", token)
    config.get_settings.cache_clear()
    from app.main import create_app

    # No `with`: the lifespan (which registers with a coordinator) is not started.
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _reset_settings():
    yield
    config.get_settings.cache_clear()


def _video(tmp_path, external_id=7):
    d = tmp_path / str(external_id // 1_000_000) / str(external_id)
    d.mkdir(parents=True)
    f = d / "video.mp4"
    f.write_bytes(b"0123456789")
    return d, f


def test_files_require_the_token_when_configured(monkeypatch, tmp_path):
    _video(tmp_path)
    c = _app(monkeypatch, tmp_path, "s3cret")
    assert c.get("/files/7").status_code == 401                                   # no header
    assert c.get("/files/7", headers={"X-Agent-Token": "wrong"}).status_code == 401
    ok = c.get("/files/7", headers={"X-Agent-Token": "s3cret"})
    assert ok.status_code == 200 and ok.content == b"0123456789"


def test_unauthenticated_delete_does_not_delete(monkeypatch, tmp_path):
    d, f = _video(tmp_path)
    c = _app(monkeypatch, tmp_path, "s3cret")
    assert c.delete("/files/7").status_code == 401
    assert f.exists()                                                              # still there
    assert c.delete("/files/7", headers={"X-Agent-Token": "s3cret"}).status_code == 200
    assert not d.exists()


def test_without_a_configured_token_nothing_changes(monkeypatch, tmp_path):
    _video(tmp_path)
    c = _app(monkeypatch, tmp_path, "")
    assert c.get("/files/7").status_code == 200


def test_health_stays_open(monkeypatch, tmp_path):
    c = _app(monkeypatch, tmp_path, "s3cret")
    assert c.get("/health").status_code == 200
