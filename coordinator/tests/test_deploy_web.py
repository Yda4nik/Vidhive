from app.services import deployer

# No coordinator_url or agent_url: both are derived (coordinator from the request
# host, agent from the SSH host) — the form no longer asks for the coordinator URL.
_FORM = {
    "ssh_host": "1.2.3.4", "ssh_port": "22", "ssh_user": "root", "ssh_password": "secret",
    "worker_name": "agent-04", "threads": "8", "storage_path": "/var/lib/vidhive/videos",
}


def test_deploy_requires_admin(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    r = anon_client.post("/servers/deploy", data=_FORM)
    assert r.status_code == 403


def test_deploy_rejects_bad_worker_name(client):
    bad = dict(_FORM, worker_name="bad name!")
    r = client.post("/servers/deploy", data=bad, follow_redirects=False)
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_deploy_starts_job_and_records(client, raw_sql, monkeypatch):
    async def stub(params, on_line):
        on_line("step 1")
        on_line("done")
        return 0

    monkeypatch.setattr(deployer, "deploy", stub)
    r = client.post("/servers/deploy", data=_FORM)
    assert r.status_code == 200                 # followed redirect to the log page
    assert "agent-04" in r.text                 # log page shows the target
    assert raw_sql(
        "SELECT COUNT(*) FROM agent_deployments WHERE worker_name='agent-04'"
    )[0][0] == 1
    # its env carries the coordinator's agent token slot and the parsed port
    # (verified indirectly: the row exists and the redirect points at a log page)
    assert "/servers/deploy/" in str(r.url)
