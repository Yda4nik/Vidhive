"""One removal flow: choose what happens to the videos, and (for SSH-deployed servers)
whether the agent is also wiped from the machine."""

import time

from app.services import deployer, drain
from test_teardown_web import _seed_ssh_worker


def _wait_gone(raw_sql, wid):
    for _ in range(80):
        if raw_sql("SELECT COUNT(*) FROM workers WHERE id=?", (wid,))[0][0] == 0:
            return True
        time.sleep(0.05)
    return False


def test_remove_without_wipe_deletes_with_data(client, raw_sql):
    raw_sql("INSERT INTO workers (name, state) VALUES ('docker-agent', 'online')")
    wid = raw_sql("SELECT id FROM workers WHERE name='docker-agent'")[0][0]
    r = client.post(f"/servers/{wid}/remove", data={"videos": "delete"}, follow_redirects=False)
    assert r.status_code == 303
    assert raw_sql("SELECT COUNT(*) FROM workers WHERE id=?", (wid,))[0][0] == 0


def test_remove_with_wipe_runs_teardown_and_purges(client, raw_sql, monkeypatch):
    calls = []

    async def stub(params, on_line):
        calls.append(params.ssh_host)
        on_line("wiped")
        return 0

    monkeypatch.setattr(deployer, "teardown", stub)
    wid = _seed_ssh_worker(raw_sql, "agent-wipe")
    r = client.post(
        f"/servers/{wid}/remove",
        data={"videos": "delete", "wipe": "1", "ssh_key": "KEY"},
    )
    assert r.status_code == 200  # redirected to the log page
    assert _wait_gone(raw_sql, wid)
    assert calls == ["1.2.3.4"]
    assert raw_sql("SELECT COUNT(*) FROM agent_deployments WHERE worker_name='agent-wipe'")[0][0] == 0


def test_wipe_requires_credentials(client, raw_sql):
    wid = _seed_ssh_worker(raw_sql, "agent-nocreds")
    r = client.post(f"/servers/{wid}/remove", data={"videos": "delete", "wipe": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert raw_sql("SELECT COUNT(*) FROM workers WHERE id=?", (wid,))[0][0] == 1


def test_transfer_and_wipe_needs_another_online_server(client, raw_sql):
    wid = _seed_ssh_worker(raw_sql, "agent-alone")
    r = client.post(f"/servers/{wid}/remove",
                    data={"videos": "transfer", "wipe": "1", "ssh_key": "KEY"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert raw_sql("SELECT state FROM workers WHERE id=?", (wid,))[0][0] == "online"


def test_transfer_and_wipe_waits_for_copies_then_wipes(client, raw_sql, monkeypatch):
    monkeypatch.setattr("app.web.router._COPY_POLL_SECONDS", 0.05)
    order = []

    async def stub(params, on_line):
        order.append("wipe")
        return 0

    monkeypatch.setattr(deployer, "teardown", stub)
    raw_sql("INSERT INTO workers (name, state) VALUES ('other', 'online')")
    wid = _seed_ssh_worker(raw_sql, "agent-move")
    r = client.post(f"/servers/{wid}/remove",
                    data={"videos": "transfer", "wipe": "1", "ssh_key": "KEY"})
    assert r.status_code == 200
    # Nothing exclusive on it, so the copy condition holds at once and the wipe runs.
    assert _wait_gone(raw_sql, wid)
    assert order == ["wipe"]
    assert wid not in drain._HELD  # released when the task ends
