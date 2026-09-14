def _set_token(token: str, monkeypatch):
    monkeypatch.setenv("VIDHIVE_AGENT_TOKEN", token)
    from app.core import config

    config.get_settings.cache_clear()


def test_register_requires_token_when_configured(anon_client, monkeypatch):
    _set_token("secret-tok", monkeypatch)
    try:
        # no header -> 401
        r = anon_client.post("/api/workers/register", json={"name": "a1"})
        assert r.status_code == 401
        # correct header -> ok
        r = anon_client.post(
            "/api/workers/register", json={"name": "a1"},
            headers={"X-Agent-Token": "secret-tok"},
        )
        assert r.status_code == 200
    finally:
        _set_token("", monkeypatch)


def test_register_open_when_token_unset(anon_client):
    # Default env has no token -> machine endpoints stay open.
    r = anon_client.post("/api/workers/register", json={"name": "a2"})
    assert r.status_code == 200
