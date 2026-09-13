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
    r = client.post(
        "/add",
        data={"source": "mock", "mode": "range", "range_from": "0", "range_to": "50", "name": "scan"},
    )
    assert r.status_code == 200          # followed redirect to /jobs
    assert "scan" in r.text
    assert "running" in r.text           # job was started


def test_add_link_creates_job(client):
    r = client.post(
        "/add",
        data={"source": "kinescope", "mode": "link", "value": "http://kinescope.io/200673499"},
    )
    assert r.status_code == 200
    assert "running" in r.text


def test_add_ids_spread_into_separate_blocks(client, raw_sql):
    # Three explicit ids -> three single-id blocks (so they can spread over agents).
    r = client.post(
        "/add", data={"source": "mock", "mode": "id", "value": ["10", "20", "30"], "name": "three"}
    )
    assert r.status_code == 200
    chunks = raw_sql(
        "SELECT c.range_start, c.range_end FROM range_chunks c JOIN jobs j ON j.id=c.job_id "
        "WHERE j.name='three' ORDER BY c.range_start"
    )
    assert chunks == [(10, 10), (20, 20), (30, 30)]


def test_add_incompatible_combo_shows_error(client):
    r = client.post("/add", data={"source": "mock", "mode": "link", "value": "http://x/1"})
    assert r.status_code == 200
    assert "не поддерживает" in r.text


def test_job_action_pause(client):
    client.post("/add", data={"source": "mock", "mode": "id", "value": "5", "name": "scan"})
    r = client.post("/jobs/1/pause")
    assert r.status_code == 200
    assert "paused" in r.text


def test_delete_job(client, raw_sql):
    client.post("/add", data={"source": "mock", "mode": "id", "value": "7", "name": "gone"})
    jid = raw_sql("SELECT id FROM jobs WHERE name='gone'")[0][0]
    assert client.request("DELETE", f"/api/jobs/{jid}").status_code == 200
    assert raw_sql("SELECT COUNT(*) FROM jobs WHERE id=?", (jid,))[0][0] == 0


def test_retry_all_endpoint(client):
    r = client.post("/api/jobs/retry-all")
    assert r.status_code == 200
    assert "requeued" in r.json()


def test_job_creates_into_target_group(client, raw_sql):
    gid = client.post("/api/groups", json={"name": "Курс"}).json()["id"]
    client.post(
        "/add", data={"source": "mock", "mode": "id", "value": "5", "name": "g", "group_id": str(gid)},
    )
    jid = raw_sql("SELECT id FROM jobs WHERE name='g'")[0][0]
    assert raw_sql("SELECT target_group_id FROM jobs WHERE id=?", (jid,))[0][0] == gid


def test_dashboard_shows_progress_speed_and_active_downloads(client):
    """Acceptance criterion 10: progress, speed, active downloads, metrics."""
    job = client.post(
        "/api/jobs", json={"name": "scan", "range_start": 0, "range_end": 9, "chunk_size": 10}
    ).json()
    client.post(f"/api/jobs/{job['id']}/start")
    wid = client.post("/api/workers/register", json={"name": "agent-01"}).json()["id"]
    client.post(
        f"/api/workers/{wid}/heartbeat",
        json={"cpu_percent": 12.5, "disk_free_gb": 500.0, "active_downloads": 2, "active_checks": 7},
    )
    lease = client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).json()
    client.post(
        f"/api/workers/{wid}/progress",
        json={
            "chunk_id": lease["chunk_id"],
            "next_id": 4,
            "items": [
                {"external_id": 0, "status": "completed", "title": "a", "size_bytes": 1024},
                {"external_id": 1, "status": "not_found"},
                {"external_id": 2, "status": "not_found"},
                {"external_id": 3, "status": "forbidden"},
            ],
        },
    )

    body = client.get("/").text
    assert "4 / 10" in body          # progress against the finite range
    assert "40.0%" in body
    assert "ID/с" in body            # processing speed
    assert ">2<" in body or "2</td>" in body  # active downloads from the heartbeat

    # The fragment that HTMX polls every 5 seconds must render on its own.
    frag = client.get("/fragments/dashboard")
    assert frag.status_code == 200
    assert "4 / 10" in frag.text


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
    # The exact weight must be visible too, not just the rounded figure.
    assert "262 144 Б" in body


def test_library_lists_a_video_once_across_overlapping_jobs(client):
    """Two jobs covering the same identifier must not double the catalogue."""
    wid = client.post("/api/workers/register", json={"name": "agent-01"}).json()["id"]

    for _ in range(2):
        job = client.post(
            "/api/jobs", json={"range_start": 7, "range_end": 7, "chunk_size": 1}
        ).json()
        client.post(f"/api/jobs/{job['id']}/start")
        lease = client.post(
            f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
        ).json()
        client.post(
            f"/api/workers/{wid}/progress",
            json={
                "chunk_id": lease["chunk_id"],
                "next_id": 8,
                "items": [{"external_id": 7, "status": "completed", "title": "clip7"}],
            },
        )
        client.post(f"/api/workers/{wid}/complete", json={"chunk_id": lease["chunk_id"]})

    body = client.get("/library").text
    assert body.count("clip7") == 1


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


def test_jobs_fragment_renders(client):
    # The fragment is just the live table now; an active job shows up in it.
    client.post("/add", data={"source": "mock", "mode": "id", "value": "5", "name": "livejob"})
    r = client.get("/fragments/jobs")
    assert r.status_code == 200
    assert "livejob" in r.text


def test_servers_page_removed(client):
    # The Servers page was merged into the dashboard; the route is gone.
    assert client.get("/servers").status_code == 404


def test_jobs_page_hosts_the_add_form(client):
    # Add-download was merged into the Jobs page.
    body = client.get("/jobs").text
    assert 'action="/add"' in body
    assert "Добавить загрузку" in body


def test_library_page_renders(client):
    lib = client.get("/library")
    assert lib.status_code == 200
    assert "Пока ничего не загружено" in lib.text
