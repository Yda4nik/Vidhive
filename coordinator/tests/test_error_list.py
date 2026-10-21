"""The retry list on the Jobs page: entries can be dismissed so they are not retried."""


def _seed(client, raw_sql, n=2):
    jid = client.post("/api/jobs", json={"range_start": 100, "range_end": 199, "chunk_size": 100}).json()["id"]
    for i in range(n):
        raw_sql(
            "INSERT INTO items (job_id, external_id, status, attempts, last_error) "
            "VALUES (?,?,?,?,?)",
            (jid, 100 + i, "failed", 1, "boom"),
        )
    return jid


def test_errors_fragment_lists_failed_items(client, raw_sql):
    _seed(client, raw_sql)
    r = client.get("/fragments/errors")
    assert r.status_code == 200 and "boom" in r.text and "100" in r.text


def test_dismiss_one_removes_it_from_the_retry_list(client, raw_sql):
    jid = _seed(client, raw_sql)
    item_id = raw_sql("SELECT id FROM items WHERE external_id=100")[0][0]
    assert client.post(f"/api/jobs/errors/{item_id}/dismiss").status_code == 200
    assert raw_sql("SELECT status FROM items WHERE id=?", (item_id,))[0][0] == "cancelled"
    # the other one is still listed, and a retry does not re-queue the dismissed one
    assert client.post(f"/api/jobs/{jid}/retry").json()["requeued"] == 1


def test_dismiss_all(client, raw_sql):
    _seed(client, raw_sql, 3)
    assert client.post("/api/jobs/errors/dismiss-all").json()["dismissed"] == 3
    assert "Ошибок нет" in client.get("/fragments/errors").text


def test_dismiss_unknown_is_404(client):
    assert client.post("/api/jobs/errors/99999/dismiss").status_code == 404
