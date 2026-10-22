"""Administrator settings: public link and VPN switches (the Docker work is in the supervisor)."""

import pytest

from app.core import config
from app.services import supervisor

PUBLIC = {"X-Forwarded-For": "203.0.113.7"}
FAKE = {
    "available": True,
    "vpn": {"running": True, "address": "172.16.1.9"},
    "tunnel": {"enabled": True, "running": True, "registered": True,
               "url": "https://demo-words.trycloudflare.com", "restarts": 2, "note": ""},
    "agents": {n: {"running": True, "enabled": True, "active": True} for n in ("agent-01", "agent-02", "agent-03")},
}


@pytest.fixture
def fake_supervisor(monkeypatch):
    calls = []

    async def status():
        return FAKE

    async def set_tunnel(enabled):
        calls.append(("tunnel", enabled))
        return {"ok": True, "error": ""}

    async def set_vpn(targets, enabled):
        calls.append(("vpn", tuple(targets), enabled))
        return {"ok": True, "error": ""}

    monkeypatch.setattr(supervisor, "status", status)
    monkeypatch.setattr(supervisor, "set_tunnel", set_tunnel)
    monkeypatch.setattr(supervisor, "set_vpn", set_vpn)
    return calls


def test_settings_page_shows_link_and_switches(client, fake_supervisor):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Публичная ссылка" in r.text and "VPN для всех трёх агентов" in r.text
    assert "demo-words.trycloudflare.com" in r.text                     # the current address is shown
    assert 'href="/settings"' in client.get("/").text                    # the button next to «Выйти»


def test_toggles_reach_the_supervisor(client, fake_supervisor):
    assert client.post("/settings/tunnel", json={"enabled": False}).json()["ok"] is True
    assert client.post("/settings/vpn", json={"targets": ["agent-01"], "enabled": False}).json()["ok"] is True
    assert fake_supervisor == [("tunnel", False), ("vpn", ("agent-01",), False)]
    assert client.get("/settings/status").json()["tunnel"]["url"].endswith("trycloudflare.com")


def test_only_administrators_see_settings(anon_client, make_user, fake_supervisor):
    make_user("op", "password-op1", "operator")
    anon_client.post("/login", data={"username": "op", "password": "password-op1"})
    assert anon_client.get("/settings").status_code == 403
    assert anon_client.post("/settings/tunnel", json={"enabled": True}).status_code == 403
    assert 'href="/settings"' not in anon_client.get("/").text            # no button for operators


def test_settings_follow_the_public_admin_mode(client, fake_supervisor, monkeypatch):
    assert client.get("/settings", headers=PUBLIC).status_code == 403     # default: nobody publicly
    monkeypatch.setenv("VIDHIVE_PUBLIC_ADMIN", "owner")
    config.get_settings.cache_clear()
    try:
        assert client.get("/settings", headers=PUBLIC).status_code == 200  # the owner may
    finally:
        monkeypatch.delenv("VIDHIVE_PUBLIC_ADMIN", raising=False)
        config.get_settings.cache_clear()


def test_missing_supervisor_is_reported_not_crashed(client, monkeypatch):
    monkeypatch.delenv("VIDHIVE_SUPERVISOR_URL", raising=False)
    config.get_settings.cache_clear()
    data = client.get("/settings/status").json()
    assert data["available"] is False and "Сторож" in data["error"]
    assert client.get("/settings").status_code == 200


def test_invitation_links_use_the_public_address_while_it_is_on(client, fake_supervisor, raw_sql):
    client.post("/users/invite")
    token = raw_sql("SELECT token FROM invites ORDER BY id DESC LIMIT 1")[0][0]
    html = client.get("/users").text
    assert f"https://demo-words.trycloudflare.com/register?token={token}" in html


def test_invitation_links_fall_back_to_the_page_address(client, monkeypatch, raw_sql):
    async def down():
        return {"available": False, "error": "нет связи"}

    monkeypatch.setattr(supervisor, "status", down)
    client.post("/users/invite")
    token = raw_sql("SELECT token FROM invites ORDER BY id DESC LIMIT 1")[0][0]
    assert f"/register?token={token}" in client.get("/users").text
    assert "trycloudflare" not in client.get("/settings").text          # no leftover Cloudflare wording


def test_invitation_link_is_https_behind_the_funnel_even_without_the_supervisor(client, monkeypatch, raw_sql):
    async def down():
        return {"available": False, "error": "нет связи"}

    monkeypatch.setattr(supervisor, "status", down)
    monkeypatch.setenv("VIDHIVE_PUBLIC_ADMIN", "owner")          # the owner may open /users publicly
    config.get_settings.cache_clear()
    try:
        client.post("/users/invite")
        token = raw_sql("SELECT token FROM invites ORDER BY id DESC LIMIT 1")[0][0]
        html = client.get("/users", headers={"Host": "truenas-scale.example.ts.net",
                                             "Tailscale-Funnel-Request": "?1",
                                             "X-Forwarded-For": "203.0.113.7"}).text
    finally:
        monkeypatch.delenv("VIDHIVE_PUBLIC_ADMIN", raising=False)
        config.get_settings.cache_clear()
    assert f"https://truenas-scale.example.ts.net/register?token={token}" in html
    assert "http://truenas-scale.example.ts.net/register" not in html
