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
