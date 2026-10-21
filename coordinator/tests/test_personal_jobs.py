"""A job can be given to one specific agent: nobody else may take its work."""


def _worker(client, name):
    return client.post("/api/workers/register",
                       json={"name": name, "agent_url": f"http://{name}:8100"}).json()["id"]


def _lease(client, wid):
    return client.post(f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 60})


def test_personal_job_goes_only_to_its_agent(client):
    a, b = _worker(client, "agent-a"), _worker(client, "agent-b")
    job = client.post("/api/jobs", json={"range_start": 1, "range_end": 5, "chunk_size": 5,
                                         "worker_id": b}).json()
    assert job["assigned_worker_id"] == b
    client.post(f"/api/jobs/{job['id']}/start")
    assert _lease(client, a).status_code == 204        # not for agent-a
    got = _lease(client, b)
    assert got.status_code == 200 and got.json()["job_id"] == job["id"]


def test_common_job_still_goes_to_anyone(client):
    a = _worker(client, "agent-c")
    job = client.post("/api/jobs", json={"range_start": 1, "range_end": 5, "chunk_size": 5}).json()
    client.post(f"/api/jobs/{job['id']}/start")
    assert _lease(client, a).status_code == 200


def test_unknown_agent_is_rejected(client):
    r = client.post("/api/jobs", json={"range_start": 1, "range_end": 5, "worker_id": 9999})
    assert r.status_code == 422


def test_web_form_assigns_the_agent_and_shows_it(client, raw_sql):
    wid = _worker(client, "agent-d")
    client.post("/add", data={"source": "mock", "mode": "id", "value": "5", "worker_id": str(wid)})
    assert raw_sql("SELECT assigned_worker_id FROM jobs ORDER BY id DESC LIMIT 1")[0][0] == wid
    assert "только agent-d" in client.get("/jobs").text
