"""Regression tests for the 2026-10-01 audit "today" block:
offline detection, open-redirect fix, and the storage-path safety rails."""

import asyncio
import os
import pathlib
import shutil
import subprocess

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import deployer, scheduler

DB = pathlib.Path(__file__).parent / "_test.db"
REPO = pathlib.Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Worker offline detection
# --------------------------------------------------------------------------- #
def _mark_stale(offline_after: int) -> int:
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{DB.as_posix()}")
        try:
            async with AsyncSession(engine) as s:
                n = await scheduler.mark_stale_workers(s, offline_after)
                await s.commit()
                return n
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _state(raw_sql, name):
    return raw_sql("SELECT state FROM workers WHERE name=?", (name,))[0][0]


def test_silent_worker_goes_offline_and_fresh_one_stays(client, raw_sql):
    client.post("/api/workers/register", json={"name": "dead"})
    client.post("/api/workers/register", json={"name": "alive"})
    raw_sql("UPDATE workers SET last_heartbeat_at = datetime('now','-10 minutes') WHERE name='dead'")

    assert _mark_stale(30) == 1
    assert _state(raw_sql, "dead") == "offline"
    assert _state(raw_sql, "alive") == "online"
    # The change is journaled.
    assert raw_sql("SELECT COUNT(*) FROM events WHERE operation='offline'")[0][0] == 1


def test_heartbeat_brings_an_offline_worker_back_online(client, raw_sql):
    wid = client.post("/api/workers/register", json={"name": "flaky"}).json()["id"]
    raw_sql("UPDATE workers SET last_heartbeat_at = datetime('now','-10 minutes') WHERE name='flaky'")
    _mark_stale(30)
    assert _state(raw_sql, "flaky") == "offline"

    assert client.post(f"/api/workers/{wid}/heartbeat", json={}).status_code == 200
    assert _state(raw_sql, "flaky") == "online"


def test_dead_worker_is_not_a_redistribution_target(client, raw_sql):
    """A dead 'other' server must not count as capacity for 'distribute'."""
    a = client.post("/api/workers/register", json={"name": "src"}).json()["id"]
    client.post("/api/workers/register", json={"name": "dead"})
    raw_sql("UPDATE workers SET last_heartbeat_at = datetime('now','-10 minutes') WHERE name='dead'")
    _mark_stale(30)
    r = client.request("DELETE", f"/api/workers/{a}?mode=redistribute")
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Open redirect
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "target",
    ["https://evil.example/phish", "//evil.example", "/\\evil.example", "javascript:alert(1)",
     "http://evil.example", "/ok\r\nSet-Cookie: x=y"],
)
def test_login_rejects_offsite_redirect_targets(anon_client, target):
    r = anon_client.post(
        "/login",
        data={"username": "tester-admin", "password": "adminpass", "next": target},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_keeps_a_legitimate_local_target(anon_client):
    r = anon_client.post(
        "/login",
        data={"username": "tester-admin", "password": "adminpass", "next": "/library?group=fav"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/library?group=fav"


def test_login_page_does_not_echo_an_offsite_target(anon_client):
    html = anon_client.get("/login?next=https://evil.example").text
    assert "evil.example" not in html


# --------------------------------------------------------------------------- #
# Storage path safety
# --------------------------------------------------------------------------- #
_GOOD = {"ssh_host": "1.2.3.4", "ssh_user": "root", "ssh_password": "x", "threads": "8"}


@pytest.mark.parametrize(
    "path",
    ["/", "/home", "/var", "/var/lib", "/etc/vidhive/../..", "/var/lib/other/videos",
     "relative/vidhive", "/var/lib/vidhive/a b", "/var/lib/vidhive/x\nVIDHIVE_AGENT_TOKEN=pwn",
     "/var//lib/vidhive", "/var/lib/vidhive/$(rm -rf x)"],
)
def test_deploy_rejects_dangerous_storage_paths(client, raw_sql, monkeypatch, path):
    called = []

    async def stub(params, on_line):
        called.append(1)
        return 0

    monkeypatch.setattr(deployer, "deploy", stub)
    r = client.post(
        "/servers/deploy",
        data={**_GOOD, "worker_name": "w1", "storage_path": path},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert raw_sql("SELECT COUNT(*) FROM agent_deployments")[0][0] == 0
    assert not called


@pytest.mark.parametrize("path", ["/var/lib/vidhive/videos", "/mnt/disk1/vidhive", "/srv/vidhive-data/v"])
def test_deploy_accepts_vidhive_storage_paths(client, raw_sql, monkeypatch, path):
    async def stub(params, on_line):
        return 0

    monkeypatch.setattr(deployer, "deploy", stub)
    r = client.post(
        "/servers/deploy",
        data={**_GOOD, "worker_name": "w2", "storage_path": path},
        follow_redirects=False,
    )
    assert r.headers["location"].startswith("/servers/deploy/")
    assert raw_sql("SELECT storage_path FROM agent_deployments")[0][0] == path


def test_deploy_rejects_a_bad_agent_url(client, monkeypatch):
    r = client.post(
        "/servers/deploy",
        data={**_GOOD, "worker_name": "w3", "storage_path": "/var/lib/vidhive/v",
              "agent_url": "http://x:8100\nVIDHIVE_AGENT_TOKEN=pwn"},
        follow_redirects=False,
    )
    assert "error=" in r.headers["location"]


def test_teardown_refuses_a_stored_dangerous_path(client, raw_sql, monkeypatch):
    """Records created before the check existed must not be able to rm -rf /."""
    called = []

    async def stub(params, on_line):
        called.append(1)
        return 0

    monkeypatch.setattr(deployer, "teardown", stub)
    raw_sql("INSERT INTO workers (name, state) VALUES ('legacy', 'online')")
    wid = raw_sql("SELECT id FROM workers WHERE name='legacy'")[0][0]
    raw_sql(
        "INSERT INTO agent_deployments (worker_name, ssh_host, ssh_port, ssh_user, install_dir,"
        " service_name, storage_path) VALUES ('legacy','1.2.3.4',22,'root','/opt/vidhive','vidhive-agent','/')"
    )
    r = client.post(f"/servers/{wid}/teardown", data={"ssh_password": "x"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert not called
    assert raw_sql("SELECT COUNT(*) FROM workers WHERE id=?", (wid,))[0][0] == 1


# --------------------------------------------------------------------------- #
# The shell scripts themselves (defence in depth). `rm`, `id`, `systemctl` and
# `pkill` are stubbed, so these tests can never delete anything.
# --------------------------------------------------------------------------- #
def _run_script(tmp_path, script_name, env_extra):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    bodies = {
        "id": "#!/bin/sh\necho 0\n",
        "rm": '#!/bin/sh\necho "RM-CALLED $*"\n',
        "pkill": "#!/bin/sh\nexit 0\n",
        "systemctl": '#!/bin/sh\necho "SYSTEMCTL $*"\n',
    }
    for name, body in bodies.items():
        f = stubs / name
        f.write_text(body)
        f.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(stubs) + os.pathsep + env.get("PATH", "")
    env.update(env_extra)
    return subprocess.run(
        [bash, str(REPO / "deploy" / script_name)], env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize("storage", ["/", "/home", "/var/lib", "/var/lib/other", "/x/../vidhive"])
def test_teardown_script_refuses_dangerous_paths(tmp_path, storage):
    r = _run_script(tmp_path, "agent-teardown.sh",
                    {"VIDHIVE_STORAGE_PATH": storage, "INSTALL_DIR": "/opt/vidhive"})
    assert r.returncode != 0
    assert "REFUSING" in r.stderr
    assert "RM-CALLED" not in r.stdout          # nothing was deleted


def test_teardown_script_refuses_a_dangerous_install_dir(tmp_path):
    r = _run_script(tmp_path, "agent-teardown.sh",
                    {"VIDHIVE_STORAGE_PATH": "/var/lib/vidhive/videos", "INSTALL_DIR": "/opt"})
    assert r.returncode != 0 and "RM-CALLED" not in r.stdout


def test_teardown_script_still_works_for_the_default_layout(tmp_path):
    r = _run_script(tmp_path, "agent-teardown.sh",
                    {"VIDHIVE_STORAGE_PATH": "/var/lib/vidhive/videos", "INSTALL_DIR": "/opt/vidhive"})
    assert r.returncode == 0, r.stderr
    assert "RM-CALLED" in r.stdout and "/var/lib/vidhive/videos" in r.stdout


def test_bootstrap_script_refuses_env_injection(tmp_path):
    r = _run_script(tmp_path, "agent-bootstrap.sh", {
        "VIDHIVE_COORDINATOR_URL": "http://c:8000", "VIDHIVE_WORKER_NAME": "a",
        "VIDHIVE_AGENT_URL": "http://h:8100",
        "VIDHIVE_STORAGE_PATH": "/var/lib/vidhive/v\nVIDHIVE_AGENT_TOKEN=pwn",
    })
    assert r.returncode != 0 and "REFUSING" in r.stderr


def test_bootstrap_script_refuses_a_non_vidhive_storage_path(tmp_path):
    r = _run_script(tmp_path, "agent-bootstrap.sh", {
        "VIDHIVE_COORDINATOR_URL": "http://c:8000", "VIDHIVE_WORKER_NAME": "a",
        "VIDHIVE_AGENT_URL": "http://h:8100", "VIDHIVE_STORAGE_PATH": "/home",
    })
    assert r.returncode != 0 and "REFUSING" in r.stderr
