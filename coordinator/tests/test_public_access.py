"""Preparing the app to be reachable through a public tunnel (Cloudflare Tunnel,
Tailscale Funnel ...): who is "public", the real visitor address, admin pages stay
home-only, /docs hidden, Secure cookie, first-run setup closed to strangers."""

import pytest
from starlette.requests import Request

from app.core import config
from app.services import netinfo

PUBLIC = {"X-Forwarded-For": "203.0.113.7"}          # what a tunnel adds in front of a visitor


def _req(headers=None, client=("192.168.1.68", 5555), scheme="http"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": client, "scheme": scheme,
                    "method": "GET", "path": "/", "query_string": b""})


@pytest.fixture
def restrict_public_off(monkeypatch):
    monkeypatch.setenv("VIDHIVE_RESTRICT_PUBLIC", "false")
    config.get_settings.cache_clear()
    yield
    monkeypatch.delenv("VIDHIVE_RESTRICT_PUBLIC", raising=False)
    config.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# netinfo: public vs home, and the real visitor address
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("client", [("192.168.1.68", 1), ("10.0.0.5", 1), ("172.16.1.1", 1),
                                    ("127.0.0.1", 1), ("100.65.157.40", 1)])   # LAN, docker, tailnet
def test_home_and_tailnet_peers_are_not_public(client):
    assert netinfo.is_public(_req(client=client)) is False


@pytest.mark.parametrize("header", ["cf-connecting-ip", "x-forwarded-for", "forwarded",
                                    "x-real-ip", "cf-ray", "tailscale-funnel-request"])
def test_any_tunnel_header_makes_a_request_public(header):
    assert netinfo.is_public(_req({header: "1.2.3.4"})) is True


def test_a_globally_routable_peer_is_public_even_without_headers():
    """Someone port-forwarded the port on the router: no proxy headers, but a public peer."""
    assert netinfo.is_public(_req(client=("8.8.8.8", 1))) is True


def test_visitor_address_prefers_cloudflare_then_the_proxy_appended_entry():
    assert netinfo.client_ip(_req({"CF-Connecting-IP": "9.9.9.9", "X-Forwarded-For": "1.1.1.1"})) == "9.9.9.9"
    # A client-supplied leftmost entry is forgeable; the proxy appends the real one last.
    assert netinfo.client_ip(_req({"X-Forwarded-For": "6.6.6.6, 5.5.5.5"})) == "5.5.5.5"
    assert netinfo.client_ip(_req({})) == "192.168.1.68"                       # no proxy -> the peer
    assert netinfo.client_ip(_req({"X-Forwarded-For": "not-an-ip"})) == "192.168.1.68"
    assert netinfo.client_ip(_req({"X-Forwarded-For": "2a00:1370::1"})) == "2a00:1370::1"


def test_https_detection():
    assert netinfo.is_https(_req(scheme="https"))
    assert netinfo.is_https(_req({"X-Forwarded-Proto": "https"}))
    assert netinfo.is_https(_req({"CF-Visitor": '{"scheme":"https"}'}))
    assert not netinfo.is_https(_req({"X-Forwarded-Proto": "http"}))
    assert not netinfo.is_https(_req({"CF-Visitor": "garbage"}))
    assert not netinfo.is_https(_req())


# --------------------------------------------------------------------------- #
# Administrator pages & API stay home-only
# --------------------------------------------------------------------------- #
ADMIN_GET = ["/servers", "/fragments/servers", "/users"]


@pytest.mark.parametrize("path", ADMIN_GET)
def test_admin_pages_are_refused_to_public_visitors_even_for_an_admin(client, path):
    assert client.get(path, headers=PUBLIC).status_code == 403
    assert client.get(path).status_code == 200                                  # home: unchanged


def test_admin_actions_are_refused_to_public_visitors(client):
    assert client.post("/users/invite", headers=PUBLIC).status_code == 403
    assert client.post("/users", headers=PUBLIC,
                       data={"username": "x", "password": "y", "role": "viewer"}).status_code == 403
    assert client.post("/servers/deploy", headers=PUBLIC,
                       data={"ssh_host": "1.2.3.4", "worker_name": "w", "ssh_password": "p"}
                       ).status_code == 403
    assert client.request("DELETE", "/api/workers/1", headers=PUBLIC).status_code == 403
    # ...and the API says why in JSON.
    r = client.request("DELETE", "/api/workers/1", headers=PUBLIC)
    assert "домашней сети" in r.json()["detail"]


def test_public_denial_does_not_even_prompt_for_login(anon_client):
    r = anon_client.get("/servers", headers=PUBLIC, follow_redirects=False)
    assert r.status_code == 403                                                 # not a redirect to /login


def test_ordinary_use_still_works_through_the_public_link(client):
    """Viewer / operator functions are meant to be usable publicly."""
    for path in ("/", "/library", "/jobs", "/api/jobs", "/api/notes/all"):
        assert client.get(path, headers=PUBLIC).status_code == 200, path
    r = client.post("/api/jobs", headers=PUBLIC, json={"range_start": 0, "range_end": 3})
    assert r.status_code == 201


def test_the_switch_can_be_turned_off(client, restrict_public_off):
    assert client.get("/servers", headers=PUBLIC).status_code == 200


# --------------------------------------------------------------------------- #
# /docs hidden, first-run setup closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
def test_api_docs_are_hidden_from_public_visitors(client, path):
    assert client.get(path, headers=PUBLIC).status_code == 404
    assert client.get(path).status_code == 200


def test_first_run_setup_cannot_be_completed_from_a_public_link(anon_client, raw_sql):
    raw_sql("DELETE FROM user_roles")
    raw_sql("DELETE FROM users")
    assert anon_client.get("/setup", headers=PUBLIC).status_code == 403
    r = anon_client.post("/setup", headers=PUBLIC,
                         data={"username": "evil", "password": "p", "confirm": "p"})
    assert r.status_code == 403
    assert raw_sql("SELECT COUNT(*) FROM users")[0][0] == 0                      # nobody was created
    # From the home network the wizard still works.
    assert anon_client.get("/setup").status_code == 200


# --------------------------------------------------------------------------- #
# Login throttle keyed on the real visitor
# --------------------------------------------------------------------------- #
def _bad_login(client, ip, user="tester-admin"):
    return client.post("/login", data={"username": user, "password": "nope"},
                       headers={"CF-Connecting-IP": ip})


def test_one_visitor_cannot_lock_out_another_behind_the_same_tunnel(anon_client):
    for _ in range(5):
        _bad_login(anon_client, "198.51.100.1")
    assert _bad_login(anon_client, "198.51.100.1").status_code == 429            # the attacker
    # A different visitor (same tunnel, same account) is not affected.
    ok = anon_client.post("/login", data={"username": "tester-admin", "password": "adminpass"},
                          headers={"CF-Connecting-IP": "198.51.100.2"}, follow_redirects=False)
    assert ok.status_code == 303


def test_a_forged_leftmost_forwarded_entry_does_not_dodge_the_throttle(anon_client):
    def attempt(first):
        return anon_client.post("/login", data={"username": "tester-admin", "password": "nope"},
                                headers={"X-Forwarded-For": f"{first}, 198.51.100.9"})
    for i in range(5):
        assert attempt(f"10.0.0.{i}").status_code == 200
    assert attempt("10.0.0.99").status_code == 429                               # same real address


# --------------------------------------------------------------------------- #
# Secure session cookie only when the visitor used https
# --------------------------------------------------------------------------- #
def _login_cookie(client, headers):
    r = client.post("/login", data={"username": "tester-admin", "password": "adminpass"},
                    headers=headers, follow_redirects=False)
    cookies = [c for c in r.headers.get_list("set-cookie") if c.startswith("session=")]
    assert len(cookies) == 1
    return cookies[0]


def test_session_cookie_is_secure_over_https_only(anon_client):
    over_https = _login_cookie(anon_client, {"X-Forwarded-Proto": "https"}).lower()
    attrs = [a.strip() for a in over_https.split(";")[1:]]                       # Starlette writes them lowercase
    assert "secure" in attrs and "httponly" in attrs and "samesite=lax" in attrs
    assert attrs.count("secure") == 1
    plain = _login_cookie(anon_client, {}).lower()
    plain_attrs = [a.strip() for a in plain.split(";")[1:]]
    assert "secure" not in plain_attrs                                           # LAN login over http keeps working
    assert "httponly" in plain_attrs
