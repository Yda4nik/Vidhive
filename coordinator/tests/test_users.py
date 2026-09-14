def test_admin_can_create_user_and_it_can_login(client):
    r = client.post("/users", data={"username": "newop", "password": "pw", "role": "operator"})
    assert r.status_code == 200            # followed redirect to /users
    assert "newop" in r.text
    # Re-login as the new user on the same client; it can act as operator.
    client.post("/login", data={"username": "newop", "password": "pw"})
    assert client.post("/api/jobs", json={"range_start": 0, "range_end": 1}).status_code == 201


def test_non_admin_cannot_open_users(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    assert anon_client.get("/users", follow_redirects=False).status_code == 403


def test_cannot_delete_last_administrator(client, raw_sql):
    uid = raw_sql("SELECT id FROM users WHERE username='tester-admin'")[0][0]
    client.post(f"/users/{uid}/delete")
    # the admin still exists
    assert raw_sql("SELECT COUNT(*) FROM users WHERE id=?", (uid,))[0][0] == 1


def test_admin_cannot_demote_self(client, make_user, raw_sql):
    # A second admin exists, so this is blocked by the self-rule, not the last-admin rule.
    make_user("boss", "pw", "administrator")
    uid = raw_sql("SELECT id FROM users WHERE username='tester-admin'")[0][0]
    r = client.post(f"/users/{uid}/role", data={"role": "viewer"})
    assert r.status_code == 200
    role = raw_sql(
        "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?",
        (uid,),
    )[0][0]
    assert role == "administrator"  # unchanged — you can't strip your own admin


def test_admin_can_change_role(client, make_user, raw_sql):
    make_user("mover", "pw", "viewer")
    uid = raw_sql("SELECT id FROM users WHERE username='mover'")[0][0]
    r = client.post(f"/users/{uid}/role", data={"role": "operator"})
    assert r.status_code == 200
    role = raw_sql(
        "SELECT r.name FROM roles r JOIN user_roles ur ON ur.role_id=r.id WHERE ur.user_id=?",
        (uid,),
    )[0][0]
    assert role == "operator"
