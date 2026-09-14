import time

from app.services import deployer


def _seed_ssh_worker(raw_sql, name):
    raw_sql("INSERT INTO workers (name, state) VALUES (?, 'online')", (name,))
    wid = raw_sql("SELECT id FROM workers WHERE name=?", (name,))[0][0]
    raw_sql(
        "INSERT INTO agent_deployments "
        "(worker_name, ssh_host, ssh_port, ssh_user, install_dir, service_name, storage_path) "
        "VALUES (?,?,?,?,?,?,?)",
        (name, "1.2.3.4", 22, "root", "/opt/vidhive", "vidhive-agent", "/var/lib/vidhive/videos"),
    )
    return wid


def test_teardown_removes_worker_and_deployment(client, raw_sql, monkeypatch):
    async def stub(params, on_line):
        on_line("stopping service")
        return 0

    monkeypatch.setattr(deployer, "teardown", stub)
    wid = _seed_ssh_worker(raw_sql, "agent-04")

    r = client.post(f"/servers/{wid}/teardown", data={"ssh_key": "KEY"})
    assert r.status_code == 200  # followed redirect to the log page

    # The background task deletes the rows once teardown succeeds.
    for _ in range(60):
        if raw_sql("SELECT COUNT(*) FROM workers WHERE id=?", (wid,))[0][0] == 0:
            break
        time.sleep(0.05)
    assert raw_sql("SELECT COUNT(*) FROM workers WHERE id=?", (wid,))[0][0] == 0
    assert raw_sql("SELECT COUNT(*) FROM agent_deployments WHERE worker_name='agent-04'")[0][0] == 0


def test_teardown_requires_key(client, raw_sql):
    wid = _seed_ssh_worker(raw_sql, "agent-05")
    r = client.post(f"/servers/{wid}/teardown", data={"ssh_key": "   "}, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_teardown_admin_only(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    r = anon_client.post("/servers/999/teardown", data={"ssh_key": "KEY"})
    assert r.status_code == 403
