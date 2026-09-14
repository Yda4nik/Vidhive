def test_admin_creates_invite_and_link_appears(client, raw_sql):
    r = client.post("/users/invite")
    assert r.status_code == 200            # redirected to /users
    assert "register?token=" in r.text
    assert raw_sql("SELECT COUNT(*) FROM invites")[0][0] == 1


def test_register_via_invite_creates_viewer(client, raw_sql):
    client.post("/users/invite")
    token = raw_sql("SELECT token FROM invites")[0][0]
    # Registering logs the newcomer in on this client (replacing the admin session).
    r = client.post(
        "/register",
        data={"token": token, "username": "newbie", "password": "pw", "confirm": "pw"},
    )
    assert r.status_code == 200            # redirected to /
    # The newcomer is a viewer: can read, cannot create jobs.
    assert client.get("/api/jobs").status_code == 200
    assert client.post("/api/jobs", json={"range_start": 0, "range_end": 1}).status_code == 403
    role = raw_sql(
        "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id "
        "JOIN users u ON u.id=ur.user_id WHERE u.username='newbie'"
    )[0][0]
    assert role == "viewer"


def test_invalid_token_rejected(anon_client):
    assert "недействительна" in anon_client.get("/register?token=nope").text.lower()
    r = anon_client.post(
        "/register", data={"token": "nope", "username": "x", "password": "p", "confirm": "p"}
    )
    assert "недействительна" in r.text.lower()


def test_expired_invite_rejected(client, raw_sql):
    client.post("/users/invite")
    token = raw_sql("SELECT token FROM invites")[0][0]
    raw_sql("UPDATE invites SET expires_at = datetime('now','-1 hour') WHERE token = ?", (token,))
    assert "недействительна" in client.get(f"/register?token={token}").text.lower()


def test_delete_invite(client, raw_sql):
    client.post("/users/invite")
    iid = raw_sql("SELECT id FROM invites")[0][0]
    client.post(f"/users/invite/{iid}/delete")
    assert raw_sql("SELECT COUNT(*) FROM invites")[0][0] == 0
