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


def test_library_shows_readable_size_for_sub_megabyte_files(client):
    """A 256 KB file must not render as '0 МБ' (integer-MB truncation bug)."""
    job = client.post("/api/jobs", json={"range_start": 5, "range_end": 5, "chunk_size": 1}).json()
    client.post(f"/api/jobs/{job['id']}/start")
    wid = client.post("/api/workers/register", json={"name": "agent-01"}).json()["id"]
    lease = client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).json()
    client.post(
        f"/api/workers/{wid}/progress",
        json={
            "chunk_id": lease["chunk_id"],
            "next_id": 6,
            "items": [
                {
                    "external_id": 5,
                    "status": "completed",
                    "title": "clip",
                    "size_bytes": 262144,
                    "storage_path": "/data/videos/0/5/video.mp4",
                }
            ],
        },
    )

    body = client.get("/library").text
    assert "256.0 КБ" in body
    assert "0 МБ" not in body


def test_player_page_embeds_the_stream(client):
    job = client.post("/api/jobs", json={"range_start": 5, "range_end": 5, "chunk_size": 1}).json()
    client.post(f"/api/jobs/{job['id']}/start")
    wid = client.post("/api/workers/register", json={"name": "agent-01"}).json()["id"]
    lease = client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).json()
    client.post(
        f"/api/workers/{wid}/progress",
        json={
            "chunk_id": lease["chunk_id"],
            "next_id": 6,
            "items": [{"external_id": 5, "status": "completed", "title": "clip"}],
        },
    )
    items = client.get(f"/api/jobs/{job['id']}/items").json()
    item_id = items[0]["id"]

    r = client.get(f"/player/{item_id}")
    assert r.status_code == 200
    assert f'src="/watch/{item_id}"' in r.text
    assert "<video" in r.text


def test_servers_and_library_pages(client):
    assert client.get("/servers").status_code == 200
    lib = client.get("/library")
    assert lib.status_code == 200
    assert "Пока ничего не загружено" in lib.text
