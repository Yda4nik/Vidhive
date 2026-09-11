"""Metrics storage via heartbeat, and idempotent progress (recovery)."""


def _register(client, name="agent-01"):
    return client.post(
        "/api/workers/register", json={"name": name, "agent_url": "http://a1:8100"}
    ).json()["id"]


def test_heartbeat_stores_metrics_shown_on_servers_page(client):
    wid = _register(client)
    client.post(
        f"/api/workers/{wid}/heartbeat",
        json={
            "cpu_percent": 42.0,
            "ram_used_mb": 2048,
            "ram_total_mb": 8192,
            "disk_free_gb": 123.4,
            "active_downloads": 2,
        },
    )
    r = client.get("/servers")
    assert r.status_code == 200
    assert "42" in r.text          # cpu percent rendered
    assert "123.4" in r.text       # disk free rendered


def test_progress_is_idempotent(client, raw_sql):
    job = client.post("/api/jobs", json={"range_start": 5, "range_end": 5, "chunk_size": 1}).json()
    client.post(f"/api/jobs/{job['id']}/start")
    wid = _register(client)
    lease = client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).json()

    body = {
        "chunk_id": lease["chunk_id"],
        "next_id": 6,
        "items": [{"external_id": 5, "status": "completed", "title": "clip"}],
    }
    client.post(f"/api/workers/{wid}/progress", json=body)
    client.post(f"/api/workers/{wid}/progress", json=body)  # duplicate delivery

    count = raw_sql("SELECT COUNT(*) FROM items WHERE external_id = 5")[0][0]
    assert count == 1  # no duplicate row despite at-least-once delivery
