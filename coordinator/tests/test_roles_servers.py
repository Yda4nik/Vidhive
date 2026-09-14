def test_operator_cannot_open_servers_page(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    r = anon_client.get("/servers", follow_redirects=False)
    assert r.status_code == 403


def test_operator_cannot_delete_worker(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    # register is still open (no agent token configured in tests)
    wid = anon_client.post("/api/workers/register", json={"name": "agent-01"}).json()["id"]
    assert anon_client.request("DELETE", f"/api/workers/{wid}").status_code == 403


def test_admin_sees_gear_and_can_open_servers(client):
    assert "⚙ Управление" in client.get("/").text
    assert client.get("/servers").status_code == 200


def test_operator_ui_hides_gear(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    assert "⚙ Управление" not in anon_client.get("/").text
