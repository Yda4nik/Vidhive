"""Vidhive supervisor — the few Docker actions the web UI needs, behind a token.

The coordinator must not hold the Docker socket (that would make any bug in the public-facing
web app equal to root on the host). This small separate service does, and exposes ONLY:

  * public link  : start / stop the Cloudflare tunnel container, report its address, and
                   restart it when it dies (a quick tunnel is deleted by Cloudflare after
                   repeated disconnects and keeps failing with "Tunnel not found");
  * VPN for agents: send an agent's internet traffic through the VPN container or straight
                   out, and keep that route correct when the VPN container's address changes.

It works on named compose services of one project only, needs the shared agent token for every
request and is reachable only inside the Docker network (not published to the host).
"""

from __future__ import annotations

import hmac
import http.client
import json
import logging
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

log = logging.getLogger("vidhive.supervisor")

SOCK = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
PROJECT = os.environ.get("COMPOSE_PROJECT", "vidhive")
TOKEN = os.environ.get("VIDHIVE_AGENT_TOKEN", "")
STATE_FILE = os.environ.get("STATE_FILE", "/state/state.json")
PORT = int(os.environ.get("SUPERVISOR_PORT", "8200"))
RECONCILE_SECONDS = int(os.environ.get("RECONCILE_SECONDS", "15"))
AGENTS = ("agent-01", "agent-02", "agent-03")
TS_CONTAINER = os.environ.get("TAILSCALE_CONTAINER", "ix-tailscale-tailscale-1")
FUNNEL_TARGET = os.environ.get("FUNNEL_TARGET_PORT", "8000")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

_lock = threading.RLock()
_state: dict = {"tunnel": None, "agents": {}}
_runtime: dict = {"tunnel_restarts": 0, "last_restart": 0.0, "tunnel_note": ""}


# --------------------------------------------------------------------------- #
# Docker Engine API over the unix socket (no third-party packages)
# --------------------------------------------------------------------------- #
class _Conn(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("docker", timeout=30)

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(SOCK)
        self.sock = s


def docker(method: str, path: str, body=None, raw: bool = False):
    conn = _Conn()
    try:
        headers, data = {}, None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        out = resp.read()
        if raw:
            return resp.status, out
        ctype = resp.getheader("Content-Type", "")
        return resp.status, (json.loads(out) if out and ctype.startswith("application/json") else out)
    finally:
        conn.close()


def demux(data: bytes) -> str:
    """Docker multiplexes stdout/stderr: 8-byte header (stream, 0,0,0, size) + payload."""
    out, i = [], 0
    while i + 8 <= len(data):
        size = int.from_bytes(data[i + 4:i + 8], "big")
        out.append(data[i + 8:i + 8 + size])
        i += 8 + size
    return b"".join(out).decode("utf-8", "replace") if out else data.decode("utf-8", "replace")


def find(service: str) -> dict | None:
    flt = json.dumps({"label": [f"com.docker.compose.project={PROJECT}",
                                f"com.docker.compose.service={service}"]})
    status, lst = docker("GET", "/containers/json?all=1&filters=" + quote(flt))
    return lst[0] if status == 200 and lst else None


def inspect(cid: str) -> dict:
    return docker("GET", f"/containers/{cid}/json")[1]


def is_running(c: dict | None) -> bool:
    return bool(c) and c.get("State") == "running"


def exec_in(cid: str, cmd: list[str]) -> str:
    status, e = docker("POST", f"/containers/{cid}/exec",
                       {"Cmd": cmd, "AttachStdout": True, "AttachStderr": True, "User": "root"})
    if status != 201:
        return ""
    _, out = docker("POST", f"/exec/{e['Id']}/start", {"Detach": False, "Tty": False}, raw=True)
    return demux(out)


# --------------------------------------------------------------------------- #
# State (survives restarts of the supervisor / the NAS)
# --------------------------------------------------------------------------- #
def load_state() -> None:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            _state.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_state() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except OSError as exc:
        log.warning("cannot save state: %s", exc)


# --------------------------------------------------------------------------- #
# VPN routing for agents
# --------------------------------------------------------------------------- #
def vpn_ip() -> str | None:
    vpn = find("vpn")
    if not is_running(vpn):
        return None
    nets = inspect(vpn["Id"]).get("NetworkSettings", {}).get("Networks", {})
    net = nets.get(f"{PROJECT}_vidhive") or next(iter(nets.values()), {})
    return net.get("IPAddress") or None


def bridge_gateway() -> str | None:
    status, net = docker("GET", f"/networks/{PROJECT}_vidhive")
    if status != 200:
        return None
    cfg = (net.get("IPAM", {}).get("Config") or [{}])
    return next((c.get("Gateway") for c in cfg if c.get("Gateway") and "." in c["Gateway"]), None)


def current_gateway(cid: str) -> str | None:
    m = re.search(r"default via (\S+)", exec_in(cid, ["ip", "route", "show", "default"]))
    return m.group(1) if m else None


def agent_vpn_enabled(c: dict, via: str | None) -> bool:
    return bool(via) and current_gateway(c["Id"]) == via


def set_agent_vpn(service: str, enabled: bool) -> tuple[bool, str]:
    c = find(service)
    if not is_running(c):
        return False, f"{service} не запущен"
    if enabled:
        gw = vpn_ip()
        if not gw:
            return False, "контейнер VPN не запущен"
    else:
        gw = bridge_gateway()
        if not gw:
            return False, "не найден шлюз сети Docker"
    exec_in(c["Id"], ["ip", "route", "replace", "default", "via", gw])
    return current_gateway(c["Id"]) == gw, ""


# --------------------------------------------------------------------------- #
# Public link (Cloudflare quick tunnel inside the VPN network namespace)
# --------------------------------------------------------------------------- #
def tunnel_info(c: dict | None) -> dict:
    info = {"running": is_running(c), "url": None, "registered": False}
    if not c or not info["running"]:
        return info
    started = inspect(c["Id"]).get("State", {}).get("StartedAt", "")
    since = 0
    try:
        from datetime import datetime
        since = int(datetime.fromisoformat(started.replace("Z", "+00:00")[:26] + "+00:00").timestamp()) - 1
    except Exception:  # noqa: BLE001
        pass
    _, raw = docker("GET", f"/containers/{c['Id']}/logs?stdout=1&stderr=1&since={since}&tail=1500", raw=True)
    text = demux(raw)
    urls = URL_RE.findall(text)
    info["url"] = urls[-1] if urls else None
    info["registered"] = "Registered tunnel connection" in text
    info["dead"] = "Tunnel not found" in text.rsplit("Registered tunnel connection", 1)[-1] \
        if "Registered tunnel connection" in text else ("Tunnel not found" in text)
    info["age"] = time.time() - since if since else 0
    return info


def ensure_vpn_running() -> bool:
    vpn = find("vpn")
    if vpn is None:
        return False
    if not is_running(vpn):
        docker("POST", f"/containers/{vpn['Id']}/start")
        time.sleep(8)
    return True


def start_tunnel() -> tuple[bool, str]:
    if not ensure_vpn_running():
        return False, "контейнер VPN не найден (запустите профиль vpn)"
    c = find("tunnel")
    if c is None:
        return False, "контейнер туннеля не найден (запустите профиль tunnel)"
    docker("POST", f"/containers/{c['Id']}/{'restart' if is_running(c) else 'start'}")
    _runtime["last_restart"] = time.time()
    return True, ""


def stop_tunnel() -> None:
    c = find("tunnel")
    if is_running(c):
        docker("POST", f"/containers/{c['Id']}/stop?t=5")


# --------------------------------------------------------------------------- #
# Permanent address through Tailscale Funnel (preferred when the tailnet allows it)
# --------------------------------------------------------------------------- #
def ts_container() -> dict | None:
    flt = json.dumps({"name": [TS_CONTAINER]})
    status, lst = docker("GET", "/containers/json?filters=" + quote(flt))
    return next((c for c in lst if is_running(c)), None) if status == 200 and isinstance(lst, list) else None


def ts_exec(cmd: list[str]) -> str:
    c = ts_container()
    return exec_in(c["Id"], cmd) if c else ""


def funnel_info() -> dict:
    """Is Tailscale Funnel usable here, and is it switched on for the app?"""
    if ts_container() is None:
        return {"available": False, "enabled": False, "url": None, "reason": "контейнер Tailscale не найден"}
    try:
        d = json.loads(ts_exec(["tailscale", "status", "--json"]) or "{}")
    except ValueError:
        return {"available": False, "enabled": False, "url": None, "reason": "Tailscale не отвечает"}
    me = d.get("Self") or {}
    dns = (me.get("DNSName") or "").rstrip(".")
    caps = set(me.get("CapMap") or {}) | set(me.get("Capabilities") or [])
    has_funnel = any("funnel" in str(k).lower() for k in caps)
    https_ok = bool(d.get("CertDomains"))
    if not (has_funnel and https_ok):
        why = "в Tailscale не включён HTTPS" if not https_ok else "в Tailscale не включён Funnel"
        return {"available": False, "enabled": False, "url": None, "reason": why, "dns": dns}
    enabled = "Funnel on" in ts_exec(["tailscale", "funnel", "status"])
    return {"available": True, "enabled": enabled, "url": f"https://{dns}" if dns and enabled else None,
            "reason": "", "dns": dns}


def set_funnel(enabled: bool) -> tuple[bool, str]:
    cmd = ["tailscale", "funnel", "--bg", FUNNEL_TARGET] if enabled else ["tailscale", "funnel", "--https=443", "off"]
    out = ts_exec(cmd)
    ok = funnel_info()["enabled"] == enabled
    return ok, "" if ok else ("Tailscale ответил: " + out.strip()[:160])


# --------------------------------------------------------------------------- #
# Reconcile loop: make reality match the desired state
# --------------------------------------------------------------------------- #
def reconcile() -> None:
    with _lock:
        # --- agents: route matches the wish, even after the VPN container changed address
        for svc in AGENTS:
            want = _state["agents"].get(svc)
            c = find(svc)
            if want is None or not is_running(c):
                continue
            target = vpn_ip() if want else bridge_gateway()
            if target and current_gateway(c["Id"]) != target:
                log.info("fixing route of %s -> %s", svc, target)
                exec_in(c["Id"], ["ip", "route", "replace", "default", "via", target])

        # --- permanent Tailscale address, when available, replaces the Cloudflare tunnel
        fn = funnel_info()
        if fn["available"]:
            if _state.get("tunnel") and not fn["enabled"]:
                log.info("re-enabling Tailscale Funnel")
                set_funnel(True)
            stop_tunnel()   # one public address is enough: the Cloudflare tunnel is not needed
            return

        # --- tunnel: keep it alive, renew it when Cloudflare deleted it
        if _state.get("tunnel"):
            if not ensure_vpn_running():
                _runtime["tunnel_note"] = "контейнер VPN не найден"
                return
            c = find("tunnel")
            if c is None:
                _runtime["tunnel_note"] = "контейнер туннеля не найден"
                return
            now = time.time()
            if not is_running(c):
                if now - _runtime["last_restart"] > 20:
                    log.info("tunnel container is down - starting")
                    start_tunnel()
                    _runtime["tunnel_restarts"] += 1
                return
            info = tunnel_info(c)
            stuck = (not info["registered"]) and info.get("age", 0) > 90
            if (info.get("dead") or stuck) and now - _runtime["last_restart"] > 60:
                log.info("tunnel is %s - restarting for a new address", "deleted" if info.get("dead") else "not registering")
                _runtime["tunnel_note"] = "туннель перезапущен: " + ("Cloudflare удалил старый" if info.get("dead") else "не подключался")
                start_tunnel()
                _runtime["tunnel_restarts"] += 1


def _loop() -> None:
    while True:
        try:
            reconcile()
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            log.warning("reconcile failed: %s", exc)
        time.sleep(RECONCILE_SECONDS)


def initialise() -> None:
    """First start: adopt what is running now as the desired state."""
    load_state()
    with _lock:
        if _state.get("tunnel") is None:
            _state["tunnel"] = is_running(find("tunnel"))
        gw = vpn_ip()
        for svc in AGENTS:
            if svc not in _state["agents"]:
                c = find(svc)
                _state["agents"][svc] = bool(is_running(c) and gw and current_gateway(c["Id"]) == gw)
        save_state()


# --------------------------------------------------------------------------- #
# Status / actions used by the HTTP handler
# --------------------------------------------------------------------------- #
def status() -> dict:
    vpn = find("vpn")
    gw = vpn_ip()
    agents = {}
    for svc in AGENTS:
        c = find(svc)
        running = is_running(c)
        agents[svc] = {
            "running": running,
            "enabled": bool(_state["agents"].get(svc)),
            "active": bool(running and gw and current_gateway(c["Id"]) == gw),
        }
    fn = funnel_info()
    if fn["available"]:
        tun = {"provider": "tailscale", "enabled": bool(_state.get("tunnel")), "running": fn["enabled"],
               "registered": fn["enabled"], "url": fn["url"], "restarts": 0, "note": "постоянный адрес Tailscale"}
    else:
        tinfo = tunnel_info(find("tunnel"))
        tun = {"provider": "cloudflare", "enabled": bool(_state.get("tunnel")), "running": tinfo["running"],
               "registered": tinfo["registered"], "url": tinfo["url"] if tinfo["registered"] else None,
               "restarts": _runtime["tunnel_restarts"], "note": _runtime["tunnel_note"]}
    return {
        "available": True,
        "vpn": {"running": is_running(vpn), "address": gw},
        "tunnel": tun,
        "funnel": {"available": fn["available"], "reason": fn.get("reason", ""), "dns": fn.get("dns", "")},
        "agents": agents,
    }


def set_tunnel(enabled: bool) -> dict:
    with _lock:
        _state["tunnel"] = bool(enabled)
        save_state()
        _runtime["tunnel_note"] = ""
        if funnel_info()["available"]:
            ok, msg = set_funnel(bool(enabled))
            stop_tunnel()
        elif enabled:
            ok, msg = start_tunnel()
        else:
            stop_tunnel()
            ok, msg = True, ""
    return {"ok": ok, "error": msg}


def set_vpn(targets: list[str], enabled: bool) -> dict:
    errors = []
    with _lock:
        if enabled:
            ensure_vpn_running()
        for svc in targets:
            if svc not in AGENTS:
                errors.append(f"{svc}: неизвестная цель")
                continue
            _state["agents"][svc] = bool(enabled)
            ok, msg = set_agent_vpn(svc, bool(enabled))
            if not ok and msg:
                errors.append(f"{svc}: {msg}")
        save_state()
    return {"ok": not errors, "error": "; ".join(errors)}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "vidhive-supervisor"

    def log_message(self, fmt, *args) -> None:  # noqa: D401 - quiet
        log.debug(fmt, *args)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not TOKEN:
            return False  # never run open: without a token nothing is allowed
        given = self.headers.get("X-Agent-Token", "")
        return hmac.compare_digest(given.encode(), TOKEN.encode())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorised():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/status":
            try:
                return self._send(200, status())
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": str(exc)})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorised():
            return self._send(401, {"error": "unauthorized"})
        data = self._body()
        try:
            if self.path == "/tunnel":
                return self._send(200, set_tunnel(bool(data.get("enabled"))))
            if self.path == "/vpn":
                targets = data.get("targets") or list(AGENTS)
                return self._send(200, set_vpn([str(t) for t in targets], bool(data.get("enabled"))))
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"ok": False, "error": str(exc)})
        self._send(404, {"error": "not found"})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        log.warning("VIDHIVE_AGENT_TOKEN is empty: every request will be refused")
    initialise()
    threading.Thread(target=_loop, daemon=True).start()
    log.info("supervisor listening on :%s (project %s)", PORT, PROJECT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
