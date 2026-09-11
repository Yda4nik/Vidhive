"""Integration tests for the block/lease protocol through the HTTP API."""


def _lease(client, wid, seconds=120):
    return client.post(f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": seconds})


def _register(client, name):
    return client.post("/api/workers/register", json={"name": name}).json()["id"]


def test_block_protocol_happy_path(client):
    # Range 0..9 with chunk_size 5 => two chunks: [0-4] and [5-9].
    job = client.post(
        "/api/jobs", json={"name": "t", "range_start": 0, "range_end": 9, "chunk_size": 5}
    ).json()
    jid = job["id"]
    assert client.post(f"/api/jobs/{jid}/start").json()["state"] == "running"

    w1 = _register(client, "agent-01")
    w2 = _register(client, "agent-02")

    c1 = _lease(client, w1).json()
    assert (c1["range_start"], c1["range_end"], c1["next_id"]) == (0, 4, 0)

    # A different worker gets the OTHER chunk — never the same one.
    c2 = _lease(client, w2).json()
    assert (c2["range_start"], c2["range_end"]) == (5, 9)
    assert c2["chunk_id"] != c1["chunk_id"]

    # Range exhausted -> no work left.
    assert _lease(client, w1).status_code == 204

    # Progress advances the checkpoint and records an item.
    client.post(
        f"/api/workers/{w1}/progress",
        json={
            "chunk_id": c1["chunk_id"],
            "next_id": 3,
            "items": [{"external_id": 1, "status": "found", "title": "clip"}],
        },
    )
    assert client.post(
        f"/api/workers/{w1}/complete", json={"chunk_id": c1["chunk_id"]}
    ).status_code == 200
    client.post(f"/api/workers/{w2}/complete", json={"chunk_id": c2["chunk_id"]})

    # Both chunks done and range exhausted -> job completes.
    assert client.get(f"/api/jobs/{jid}").json()["state"] == "completed"


def test_lease_reclaims_expired(client, raw_sql):
    job = client.post(
        "/api/jobs", json={"range_start": 0, "range_end": 4, "chunk_size": 10}
    ).json()
    jid = job["id"]
    client.post(f"/api/jobs/{jid}/start")
    w1 = _register(client, "agent-01")

    c1 = _lease(client, w1).json()
    # While leased, there is no other work.
    assert _lease(client, w1).status_code == 204

    # Force the lease into the past, then the next lease should reclaim it.
    raw_sql(
        "UPDATE range_chunks SET lease_expires_at = '2000-01-01 00:00:00.000000' WHERE id = ?",
        (c1["chunk_id"],),
    )
    c2 = _lease(client, w1).json()
    assert c2["chunk_id"] == c1["chunk_id"]


def test_paused_job_hands_out_no_work(client):
    job = client.post(
        "/api/jobs", json={"range_start": 0, "range_end": 100, "chunk_size": 10}
    ).json()
    jid = job["id"]
    client.post(f"/api/jobs/{jid}/start")
    w1 = _register(client, "agent-01")
    assert _lease(client, w1).status_code == 200  # running: gets work

    client.post(f"/api/jobs/{jid}/pause")
    # Complete the in-flight chunk, then paused job must not hand out new blocks.
    # (a fresh worker asking while paused gets nothing)
    w2 = _register(client, "agent-02")
    assert _lease(client, w2).status_code == 204
