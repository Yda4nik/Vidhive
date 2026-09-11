"""Items/events endpoints, download cataloguing, and the free-space guard."""


def _register(client, name="agent-01"):
    return client.post(
        "/api/workers/register", json={"name": name, "agent_url": "http://a1:8100"}
    ).json()["id"]


def _running_job(client, start=0, end=4, chunk=10):
    job = client.post(
        "/api/jobs", json={"range_start": start, "range_end": end, "chunk_size": chunk}
    ).json()
    client.post(f"/api/jobs/{job['id']}/start")
    return job["id"]


def _lease(client, wid):
    return client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).json()


def test_items_endpoint_lists_and_filters(client):
    jid = _running_job(client)
    wid = _register(client)
    lease = _lease(client, wid)
    client.post(
        f"/api/workers/{wid}/progress",
        json={
            "chunk_id": lease["chunk_id"],
            "next_id": 5,
            "items": [
                {"external_id": 0, "status": "completed", "title": "clip", "size_bytes": 10},
                {"external_id": 1, "status": "not_found"},
            ],
        },
    )

    all_items = client.get(f"/api/jobs/{jid}/items").json()
    assert {i["external_id"] for i in all_items} == {0, 1}
    assert next(i for i in all_items if i["external_id"] == 0)["title"] == "clip"
    assert next(i for i in all_items if i["external_id"] == 0)["worker"] == "agent-01"

    only_done = client.get(f"/api/jobs/{jid}/items", params={"status": "completed"}).json()
    assert [i["external_id"] for i in only_done] == [0]


def test_events_endpoint_records_lifecycle(client):
    jid = _running_job(client)
    wid = _register(client)
    _lease(client, wid)

    events = client.get(f"/api/jobs/{jid}/events").json()
    operations = {e["operation"] for e in events}
    assert {"create", "start", "lease"} <= operations
    assert all(e["job_id"] == jid for e in events)


def test_completed_download_is_catalogued(client, raw_sql):
    _running_job(client)
    wid = _register(client)
    lease = _lease(client, wid)
    body = {
        "chunk_id": lease["chunk_id"],
        "next_id": 5,
        "items": [
            {
                "external_id": 0,
                "status": "completed",
                "title": "clip",
                "size_bytes": 1024,
                "storage_path": "/data/videos/0/0/video.mp4",
            }
        ],
    }
    client.post(f"/api/workers/{wid}/progress", json=body)
    client.post(f"/api/workers/{wid}/progress", json=body)  # duplicate delivery

    assert raw_sql("SELECT COUNT(*) FROM downloads")[0][0] == 1
    files = raw_sql("SELECT storage_path, worker_name FROM files")
    assert files == [("/data/videos/0/0/video.mp4", "agent-01")]


def test_lease_skipped_when_worker_is_low_on_disk(client):
    wid = _register(client)
    # Report less free space than the coordinator's threshold (5 GB default).
    client.post(f"/api/workers/{wid}/heartbeat", json={"disk_free_gb": 1.0})
    _running_job(client, start=0, end=100, chunk=10)

    assert _lease_status(client, wid) == 204


def _lease_status(client, wid):
    return client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).status_code
