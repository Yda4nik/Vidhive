"""Chunk ownership (fencing) and what pause / stop / retry actually do."""


def _worker(client, name):
    return client.post("/api/workers/register", json={"name": name}).json()["id"]


def _lease(client, wid):
    r = client.post(f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120})
    return r.json() if r.status_code == 200 else None


def _job(client, start, end, chunk=None):
    j = client.post(
        "/api/jobs", json={"range_start": start, "range_end": end, "chunk_size": chunk or (end - start + 1)}
    ).json()["id"]
    client.post(f"/api/jobs/{j}/start")
    return j


def _progress(client, wid, chunk_id, next_id, items=()):
    return client.post(
        f"/api/workers/{wid}/progress",
        json={"chunk_id": chunk_id, "next_id": next_id, "items": list(items)},
    )


def _state(client, job_id):
    return client.get(f"/api/jobs/{job_id}").json()["state"]


# --------------------------------------------------------------------------- #
# Fencing: only the current lease holder may touch a chunk
# --------------------------------------------------------------------------- #
def _stale_setup(client, raw_sql):
    _job(client, 1, 5)
    a, b = _worker(client, "A"), _worker(client, "B")
    first = _lease(client, a)
    raw_sql("UPDATE range_chunks SET lease_expires_at = datetime('now','-1 hour') WHERE id=?",
            (first["chunk_id"],))
    taken = _lease(client, b)                      # B legitimately takes over after expiry
    assert taken["chunk_id"] == first["chunk_id"]
    return a, b, taken["chunk_id"]


def test_stale_agent_cannot_fail_anothers_chunk(client, raw_sql):
    a, b, chunk = _stale_setup(client, raw_sql)
    assert client.post(f"/api/workers/{a}/fail", json={"chunk_id": chunk}).status_code == 409
    row = raw_sql("SELECT status, leased_by FROM range_chunks WHERE id=?", (chunk,))[0]
    assert row == ("leased", b)                    # B still owns it


def test_stale_agent_cannot_complete_anothers_chunk(client, raw_sql):
    a, b, chunk = _stale_setup(client, raw_sql)
    assert client.post(f"/api/workers/{a}/complete", json={"chunk_id": chunk}).status_code == 409
    assert raw_sql("SELECT status FROM range_chunks WHERE id=?", (chunk,))[0][0] == "leased"


def test_stale_agent_progress_is_rejected_and_records_nothing(client, raw_sql):
    a, b, chunk = _stale_setup(client, raw_sql)
    r = _progress(client, a, chunk, 3, [{"external_id": 1, "status": "completed", "title": "x",
                                          "storage_path": "/d/1/video.mp4"}])
    assert r.status_code == 409
    assert raw_sql("SELECT COUNT(*) FROM items")[0][0] == 0
    assert raw_sql("SELECT next_id FROM range_chunks WHERE id=?", (chunk,))[0][0] == 1


def test_progress_on_an_unowned_chunk_is_rejected(client, raw_sql):
    _job(client, 1, 5)
    a = _worker(client, "A")
    chunk = _lease(client, a)["chunk_id"]
    raw_sql("UPDATE range_chunks SET status='pending', leased_by=NULL WHERE id=?", (chunk,))
    assert _progress(client, a, chunk, 3).status_code == 409


def test_the_owner_still_works_and_complete_is_idempotent(client):
    _job(client, 1, 5)
    a = _worker(client, "A")
    chunk = _lease(client, a)["chunk_id"]
    r = _progress(client, a, chunk, 6, [{"external_id": 1, "status": "not_found"}])
    assert r.status_code == 200 and r.json()["proceed"] is True
    assert client.post(f"/api/workers/{a}/complete", json={"chunk_id": chunk}).status_code == 200
    # A retried 'complete' (e.g. the first reply was lost) is harmless.
    assert client.post(f"/api/workers/{a}/complete", json={"chunk_id": chunk}).status_code == 200


# --------------------------------------------------------------------------- #
# Pause / stop: in-flight work stops after the current batch, resume continues
# --------------------------------------------------------------------------- #
def _batch():
    return [{"external_id": i, "status": "not_found"} for i in (1, 2, 3)]


def test_pause_stops_the_agent_records_the_batch_and_resume_continues(client, raw_sql):
    j = _job(client, 1, 6)
    w = _worker(client, "A")
    chunk = _lease(client, w)["chunk_id"]
    client.post(f"/api/jobs/{j}/pause")

    r = _progress(client, w, chunk, 4, _batch())            # the batch that was in flight
    assert r.status_code == 200 and r.json()["proceed"] is False
    # The batch is not lost, and the chunk is back in the queue with its checkpoint.
    assert raw_sql("SELECT COUNT(*) FROM items WHERE job_id=?", (j,))[0][0] == 3
    assert raw_sql("SELECT status, leased_by, next_id FROM range_chunks WHERE id=?", (chunk,))[0] == (
        "pending", None, 4)

    assert _lease(client, w) is None                         # paused: nothing is handed out
    client.post(f"/api/jobs/{j}/resume")
    assert _lease(client, w)["next_id"] == 4                 # continues where it stopped


def test_stop_stops_the_agent_and_a_restart_continues(client, raw_sql):
    j = _job(client, 1, 6)
    w = _worker(client, "A")
    chunk = _lease(client, w)["chunk_id"]
    client.post(f"/api/jobs/{j}/stop")

    r = _progress(client, w, chunk, 4, _batch())
    assert r.json()["proceed"] is False
    assert _state(client, j) == "stopped"
    assert raw_sql("SELECT status FROM range_chunks WHERE id=?", (chunk,))[0][0] == "pending"
    assert _lease(client, w) is None

    client.post(f"/api/jobs/{j}/start")
    assert _lease(client, w)["next_id"] == 4


def test_a_stale_report_after_pause_cannot_resurrect_work(client):
    """After the pause release, the same agent's next report is fenced out."""
    j = _job(client, 1, 6)
    w = _worker(client, "A")
    chunk = _lease(client, w)["chunk_id"]
    client.post(f"/api/jobs/{j}/pause")
    assert _progress(client, w, chunk, 4, _batch()).json()["proceed"] is False
    assert _progress(client, w, chunk, 7, _batch()).status_code == 409


# --------------------------------------------------------------------------- #
# Retry must not change a paused / stopped job's state
# --------------------------------------------------------------------------- #
def _job_with_a_failed_item(client):
    j = _job(client, 5, 9)
    w = _worker(client, "A")
    chunk = _lease(client, w)["chunk_id"]
    _progress(client, w, chunk, 6, [{"external_id": 5, "status": "failed", "error": "x"}])
    return j, w


def test_retry_does_not_unpause_a_paused_job(client):
    j, w = _job_with_a_failed_item(client)
    client.post(f"/api/jobs/{j}/pause")
    assert client.post(f"/api/jobs/{j}/retry").json()["requeued"] == 1
    assert _state(client, j) == "paused"                     # was "running" before the fix
    assert _lease(client, w) is None                         # the retried work waits...
    client.post(f"/api/jobs/{j}/resume")
    assert _lease(client, w)["range_start"] == 5             # ...and runs once resumed


def test_retry_does_not_restart_a_stopped_job(client):
    j, _ = _job_with_a_failed_item(client)
    client.post(f"/api/jobs/{j}/stop")
    client.post(f"/api/jobs/{j}/retry")
    assert _state(client, j) == "stopped"


def test_retry_still_reopens_a_finished_job(client):
    j = _job(client, 5, 5)
    w = _worker(client, "A")
    chunk = _lease(client, w)["chunk_id"]
    _progress(client, w, chunk, 6, [{"external_id": 5, "status": "failed", "error": "x"}])
    client.post(f"/api/workers/{w}/complete", json={"chunk_id": chunk})
    assert _state(client, j) == "completed"
    client.post(f"/api/jobs/{j}/retry")
    assert _state(client, j) == "running"
