"""Library groups, favorites, filtering and full deletion."""


def _completed_item(client, external_id, title="clip", source_url="https://kinescope.io/x",
                    duration=120, worker="agent-01"):
    """Create a completed library item and return its item id."""
    wid = client.post("/api/workers/register", json={"name": worker}).json()["id"]
    job = client.post(
        "/api/jobs", json={"range_start": external_id, "range_end": external_id, "chunk_size": 1}
    ).json()
    client.post(f"/api/jobs/{job['id']}/start")
    lease = client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).json()
    client.post(
        f"/api/workers/{wid}/progress",
        json={
            "chunk_id": lease["chunk_id"],
            "next_id": external_id + 1,
            "items": [{
                "external_id": external_id, "status": "completed", "title": title,
                "download_url": source_url, "duration_seconds": duration, "size_bytes": 1024,
                "storage_path": f"/data/videos/0/{external_id}/video.mp4",
            }],
        },
    )
    client.post(f"/api/workers/{wid}/complete", json={"chunk_id": lease["chunk_id"]})
    items = client.get(f"/api/jobs/{job['id']}/items").json()
    return next(i["id"] for i in items if i["external_id"] == external_id)


def test_groups_crud(client):
    # Favorites appears automatically; a user group can be created and renamed.
    groups = client.get("/api/groups").json()
    assert any(g["kind"] == "favorites" for g in groups)

    gid = client.post("/api/groups", json={"name": "Лекции"}).json()["id"]
    names = {g["name"] for g in client.get("/api/groups").json()}
    assert "Лекции" in names

    client.patch(f"/api/groups/{gid}", json={"name": "Вебинары"})
    names = {g["name"] for g in client.get("/api/groups").json()}
    assert "Вебинары" in names and "Лекции" not in names

    assert client.request("DELETE", f"/api/groups/{gid}").status_code == 200


def test_cannot_delete_system_favorites_group(client):
    fav = next(g for g in client.get("/api/groups").json() if g["kind"] == "favorites")
    assert client.request("DELETE", f"/api/groups/{fav['id']}").status_code == 409


def _row(item_id):
    return f'data-id="{item_id}"'


def test_favorite_and_filter(client):
    item_id = _completed_item(client, 5)
    client.post("/api/library/favorite", json={"item_ids": [item_id], "on": True})

    assert _row(item_id) in client.get("/library", params={"group": "fav"}).text

    # Un-favorite removes it from the favourites view.
    client.post("/api/library/favorite", json={"item_ids": [item_id], "on": False})
    assert _row(item_id) not in client.get("/library", params={"group": "fav"}).text


def test_assign_to_group_and_filter(client):
    item_id = _completed_item(client, 7, title="lecture")
    gid = client.post("/api/groups", json={"name": "Курс"}).json()["id"]
    client.post("/api/library/assign", json={"item_ids": [item_id], "group_id": gid})

    assert _row(item_id) in client.get("/library", params={"group": str(gid)}).text
    # A different (empty) filter does not show it.
    assert _row(item_id) not in client.get("/library", params={"group": "fav"}).text


def test_source_filter_and_duration_shown(client):
    item_id = _completed_item(client, 10, source_url="https://kinescope.io/a", duration=3661)
    page = client.get("/library", params={"source": "kinescope"}).text
    assert _row(item_id) in page
    assert "1:01:01" in page   # duration formatted h:mm:ss
    assert _row(item_id) not in client.get("/library", params={"source": "youtube"}).text


def test_delete_video_removes_it(client, raw_sql):
    # Worker without agent_url: deletion skips the agent call and clears the DB.
    item_id = _completed_item(client, 9)
    assert _row(item_id) in client.get("/library").text

    r = client.post("/api/library/delete", json={"item_ids": [item_id]})
    assert r.status_code == 200
    assert _row(item_id) not in client.get("/library").text
    assert raw_sql("SELECT COUNT(*) FROM items WHERE external_id = 9")[0][0] == 0
    assert raw_sql("SELECT COUNT(*) FROM media_metadata")[0][0] == 0
