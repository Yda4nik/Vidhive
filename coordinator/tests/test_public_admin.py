"""Who may open administrator pages through a public link (VIDHIVE_PUBLIC_ADMIN)."""

import pytest

from app.core import config

PUBLIC = {"X-Forwarded-For": "203.0.113.7"}


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value):
        monkeypatch.setenv("VIDHIVE_PUBLIC_ADMIN", value)
        config.get_settings.cache_clear()
    yield set_mode
    monkeypatch.delenv("VIDHIVE_PUBLIC_ADMIN", raising=False)
    config.get_settings.cache_clear()


def _login(c, user, pw):
    assert c.post("/login", data={"username": user, "password": pw}, follow_redirects=False).status_code == 303


def test_default_is_nobody(client):
    assert client.get("/servers", headers=PUBLIC).status_code == 403


def test_owner_mode_lets_only_the_main_admin_in(client, anon_client, make_user, mode):
    mode("owner")
    # the main administrator (first account) is the owner of the test app
    assert client.get("/servers", headers=PUBLIC).status_code == 200
    assert client.get("/users", headers=PUBLIC).status_code == 200
    # another administrator is refused, with the reason
    make_user("admin2", "pw-admin2", "administrator")
    _login(anon_client, "admin2", "pw-admin2")
    r = anon_client.get("/servers", headers=PUBLIC)
    assert r.status_code == 403 and "главный администратор" in r.text
    # ...but works from the home network
    assert anon_client.get("/servers").status_code == 200


def test_owner_mode_sends_a_stranger_to_sign_in(anon_client, mode):
    mode("owner")
    r = anon_client.get("/servers", headers=PUBLIC, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_owner_mode_does_not_open_admin_to_operators(anon_client, make_user, mode):
    mode("owner")
    make_user("op", "pw-op", "operator")
    _login(anon_client, "op", "pw-op")
    assert anon_client.get("/servers", headers=PUBLIC).status_code == 403
    # operator functions still work publicly
    assert anon_client.get("/jobs", headers=PUBLIC).status_code == 200


def test_any_mode_lets_every_administrator_in(anon_client, make_user, mode):
    mode("any")
    make_user("admin3", "pw-admin3", "administrator")
    _login(anon_client, "admin3", "pw-admin3")
    assert anon_client.get("/servers", headers=PUBLIC).status_code == 200


def test_owner_mode_does_not_reopen_setup_or_docs(client, mode):
    mode("owner")
    assert client.get("/docs", headers=PUBLIC).status_code == 404
