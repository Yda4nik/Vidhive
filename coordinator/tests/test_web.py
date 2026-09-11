"""The dashboard is served by the coordinator and reads the DB directly."""


def test_dashboard_renders_jobs_and_workers(client):
    client.post("/api/jobs", json={"name": "demo-job", "range_start": 0, "range_end": 99})
    client.post("/api/workers/register", json={"name": "agent-01", "threads": 8})

    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Vidhive" in body
    assert "demo-job" in body      # job rendered from the database
    assert "agent-01" in body      # worker rendered from the database


def test_dashboard_empty_state(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Заданий пока нет." in r.text


def test_add_range_creates_and_starts_job(client):
    r = client.post("/add", data={"mode": "range", "value": "[0,50]", "name": "scan", "chunk_size": "10"})
    assert r.status_code == 200          # followed redirect to /jobs
    assert "scan" in r.text
    assert "running" in r.text           # job was started


def test_add_single_id_from_url(client):
    r = client.post(
        "/add", data={"mode": "single", "value": "http://kinescope.io/200673499", "name": ""}
    )
    assert r.status_code == 200
    assert "200673499" in r.text


def test_job_action_pause(client):
    client.post("/add", data={"mode": "range", "value": "[0,50]", "name": "scan", "chunk_size": "10"})
    r = client.post("/jobs/1/pause")
    assert r.status_code == 200
    assert "paused" in r.text


def test_servers_and_library_pages(client):
    assert client.get("/servers").status_code == 200
    lib = client.get("/library")
    assert lib.status_code == 200
    assert "Пока ничего не загружено" in lib.text
