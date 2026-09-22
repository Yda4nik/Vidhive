def test_create_and_list_note(client):
    r = client.post(
        "/api/notes",
        json={"external_id": 5, "t_seconds": 12.5, "label": "intro", "color": "#e05a5a"},
    )
    assert r.status_code == 200
    lst = client.get("/api/notes?external_id=5").json()
    assert len(lst) == 1
    assert lst[0]["label"] == "intro"
    assert lst[0]["color"] == "#e05a5a"
    assert lst[0]["t_seconds"] == 12.5


def test_notes_are_personal(anon_client, make_user):
    make_user("u1", "pw", "viewer")
    make_user("u2", "pw", "viewer")
    anon_client.post("/login", data={"username": "u1", "password": "pw"})
    anon_client.post("/api/notes", json={"external_id": 7, "t_seconds": 3, "label": "mine"})
    assert len(anon_client.get("/api/notes?external_id=7").json()) == 1
    # A different user sees nothing.
    anon_client.post("/login", data={"username": "u2", "password": "pw"})
    assert anon_client.get("/api/notes?external_id=7").json() == []


def test_update_and_delete_note(client):
    nid = client.post(
        "/api/notes", json={"external_id": 5, "t_seconds": 1, "label": "a"}
    ).json()["id"]
    client.patch(f"/api/notes/{nid}", json={"label": "b", "color": "#3fb27f"})
    row = client.get("/api/notes?external_id=5").json()[0]
    assert row["label"] == "b" and row["color"] == "#3fb27f"
    assert client.request("DELETE", f"/api/notes/{nid}").status_code == 200
    assert client.get("/api/notes?external_id=5").json() == []


def test_cannot_touch_others_note(anon_client, make_user):
    make_user("a", "pw", "viewer")
    make_user("b", "pw", "viewer")
    anon_client.post("/login", data={"username": "a", "password": "pw"})
    nid = anon_client.post(
        "/api/notes", json={"external_id": 9, "t_seconds": 1, "label": "x"}
    ).json()["id"]
    anon_client.post("/login", data={"username": "b", "password": "pw"})
    assert anon_client.patch(f"/api/notes/{nid}", json={"label": "hack"}).status_code == 404
    assert anon_client.request("DELETE", f"/api/notes/{nid}").status_code == 404


def test_notes_require_login(anon_client):
    assert anon_client.post(
        "/api/notes", json={"external_id": 1, "t_seconds": 0}
    ).status_code == 401


def test_notes_all_lists_video(client):
    client.post("/api/notes", json={"external_id": 42, "t_seconds": 1, "label": "n"})
    alln = client.get("/api/notes/all").json()
    hit = [x for x in alln if x["external_id"] == 42]
    assert len(hit) == 1
    assert hit[0]["item_id"] is None          # no downloaded item for this id
    assert hit[0]["title"] == "ID 42"
