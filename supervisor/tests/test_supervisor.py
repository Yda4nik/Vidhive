import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import app as sup


class FakeDocker:
    """In-memory stand-in for the few container facts the supervisor reads and changes."""

    def __init__(self):
        self.routes = {"agent-01": "172.16.1.1", "agent-02": "172.16.1.9", "agent-03": "172.16.1.1"}
        self.running = {"vpn": True, "tunnel": True, **{a: True for a in self.routes}}
        self.vpn_address = "172.16.1.9"
        self.tunnel_log = ""
        self.tunnel_restarts = 0

    def install(self, monkeypatch):
        def find(service):
            return {"Id": service, "State": "running" if self.running.get(service) else "exited"}

        monkeypatch.setattr(sup, "find", find)
        monkeypatch.setattr(sup, "vpn_ip", lambda: self.vpn_address if self.running["vpn"] else None)
        monkeypatch.setattr(sup, "bridge_gateway", lambda: "172.16.1.1")
        monkeypatch.setattr(sup, "current_gateway", lambda cid: self.routes.get(cid))
        monkeypatch.setattr(sup, "exec_in", self._exec)
        monkeypatch.setattr(sup, "ensure_vpn_running", lambda: True)
        monkeypatch.setattr(sup, "tunnel_info", self._tunnel_info)
        monkeypatch.setattr(sup, "start_tunnel", self._start_tunnel)
        monkeypatch.setattr(sup, "stop_tunnel", lambda: self.running.__setitem__("tunnel", False))
        monkeypatch.setattr(sup, "save_state", lambda: None)

    def _exec(self, cid, cmd):
        if cmd[:4] == ["ip", "route", "replace", "default"]:
            self.routes[cid] = cmd[-1]
        return ""

    def _tunnel_info(self, c):
        reg = "Registered tunnel connection" in self.tunnel_log
        dead = "Tunnel not found" in self.tunnel_log.rsplit("Registered tunnel connection", 1)[-1]
        return {"running": bool(c) and c["State"] == "running", "registered": reg, "dead": dead,
                "url": "https://x-y.trycloudflare.com" if reg else None, "age": 5}

    def _start_tunnel(self):
        self.tunnel_restarts += 1
        self.running["tunnel"] = True
        self.tunnel_log = "Registered tunnel connection"
        sup._runtime["last_restart"] = time.time()
        return True, ""


@pytest.fixture
def fake(monkeypatch):
    f = FakeDocker()
    f.install(monkeypatch)
    sup._state.update({"tunnel": False, "agents": {}})
    sup._runtime.update({"tunnel_restarts": 0, "last_restart": 0.0, "tunnel_note": ""})
    return f


def test_vpn_switch_sets_the_route_and_is_remembered(fake):
    res = sup.set_vpn(["agent-01"], True)
    assert res["ok"] and fake.routes["agent-01"] == "172.16.1.9"
    res = sup.set_vpn(["agent-02"], False)
    assert res["ok"] and fake.routes["agent-02"] == "172.16.1.1"
    assert sup._state["agents"] == {"agent-01": True, "agent-02": False}


def test_reconcile_repairs_the_route_after_the_vpn_address_changed(fake):
    sup.set_vpn(["agent-01", "agent-02", "agent-03"], True)
    fake.vpn_address = "172.16.1.8"                        # the VPN container was recreated
    sup.reconcile()
    assert set(fake.routes.values()) == {"172.16.1.8"}


def test_unknown_targets_are_refused(fake):
    res = sup.set_vpn(["coordinator", "postgres"], True)
    assert res["ok"] is False and "неизвестная цель" in res["error"]


def test_a_deleted_quick_tunnel_is_renewed(fake):
    sup._state["tunnel"] = True
    fake.tunnel_log = "Registered tunnel connection ... ERR Unauthorized: Tunnel not found"
    sup.reconcile()
    assert fake.tunnel_restarts == 1 and sup._runtime["tunnel_restarts"] == 1


def test_a_healthy_tunnel_is_left_alone_and_restarts_are_rate_limited(fake):
    sup._state["tunnel"] = True
    fake.tunnel_log = "Registered tunnel connection"
    sup.reconcile()
    assert fake.tunnel_restarts == 0
    fake.tunnel_log = "Registered tunnel connection Tunnel not found"
    sup.reconcile()
    sup.reconcile()
    assert fake.tunnel_restarts == 1                       # the second pass is inside the 60 s guard


def test_http_requires_the_token(fake, monkeypatch):
    monkeypatch.setattr(sup, "TOKEN", "secret-token-123")
    server = ThreadingHTTPServer(("127.0.0.1", 0), sup.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/status")
        assert e.value.code == 401
        req = urllib.request.Request(
            base + "/vpn", data=json.dumps({"targets": ["agent-01"], "enabled": True}).encode(),
            headers={"X-Agent-Token": "secret-token-123", "Content-Type": "application/json"})
        assert json.load(urllib.request.urlopen(req))["ok"] is True
        monkeypatch.setattr(sup, "TOKEN", "")              # no token configured -> everything refused
        bad = urllib.request.Request(base + "/health", headers={"X-Agent-Token": ""})
        with pytest.raises(urllib.error.HTTPError) as e2:
            urllib.request.urlopen(bad)
        assert e2.value.code == 401
    finally:
        server.shutdown()
