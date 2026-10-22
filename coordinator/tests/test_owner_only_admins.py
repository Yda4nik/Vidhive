"""Only the owner (the first account) manages administrators: an administrator can neither
promote someone to administrator nor demote, delete or reset another administrator."""

PW = "correct-horse-9"


def _login(c, user, pw=PW):
    assert c.post("/login", data={"username": user, "password": pw}, follow_redirects=False).status_code == 303


def _uid(raw_sql, name):
    return raw_sql("SELECT id FROM users WHERE username=?", (name,))[0][0]


def _role(raw_sql, name):
    return raw_sql("SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id JOIN users u ON u.id=ur.user_id WHERE u.username=?", (name,))[0][0]


def test_an_administrator_cannot_touch_another_administrator(anon_client, make_user, raw_sql):
    make_user("admin-b", PW, "administrator")
    make_user("admin-c", PW, "administrator")
    _login(anon_client, "admin-b")
    cid = _uid(raw_sql, "admin-c")
    for r in (anon_client.post(f"/users/{cid}/role", data={"role": "viewer"}, follow_redirects=False),
              anon_client.post(f"/users/{cid}/delete", follow_redirects=False),
              anon_client.post(f"/users/{cid}/password", data={"password": "another-pass-1"}, follow_redirects=False)):
        assert r.status_code == 303 and "error=" in r.headers["location"]
    assert _role(raw_sql, "admin-c") == "administrator"
    assert raw_sql("SELECT COUNT(*) FROM users WHERE username='admin-c'")[0][0] == 1


def test_an_administrator_cannot_create_or_promote_administrators(anon_client, make_user, raw_sql):
    make_user("admin-b", PW, "administrator")
    make_user("op1", PW, "operator")
    _login(anon_client, "admin-b")
    r = anon_client.post("/users", data={"username": "new-admin", "password": PW, "role": "administrator"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and raw_sql("SELECT COUNT(*) FROM users WHERE username='new-admin'")[0][0] == 0
    r = anon_client.post(f"/users/{_uid(raw_sql, 'op1')}/role", data={"role": "administrator"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and _role(raw_sql, "op1") == "operator"


def test_an_administrator_still_manages_ordinary_users(anon_client, make_user, raw_sql):
    make_user("admin-b", PW, "administrator")
    make_user("viewer1", PW, "viewer")
    _login(anon_client, "admin-b")
    vid = _uid(raw_sql, "viewer1")
    anon_client.post(f"/users/{vid}/role", data={"role": "operator"})
    assert _role(raw_sql, "viewer1") == "operator"
    anon_client.post(f"/users/{vid}/delete")
    assert raw_sql("SELECT COUNT(*) FROM users WHERE username='viewer1'")[0][0] == 0


def test_the_owner_manages_administrators(client, make_user, raw_sql):
    make_user("admin-b", PW, "administrator")
    bid = _uid(raw_sql, "admin-b")
    client.post(f"/users/{bid}/role", data={"role": "operator"})
    assert _role(raw_sql, "admin-b") == "operator"
    client.post(f"/users/{bid}/role", data={"role": "administrator"})
    assert _role(raw_sql, "admin-b") == "administrator"
    client.post(f"/users/{bid}/delete")
    assert raw_sql("SELECT COUNT(*) FROM users WHERE username='admin-b'")[0][0] == 0


def test_the_users_page_hides_controls_for_other_administrators(anon_client, make_user, raw_sql):
    make_user("admin-b", PW, "administrator")
    make_user("admin-c", PW, "administrator")
    _login(anon_client, "admin-b")
    html = anon_client.get("/users").text
    cid = _uid(raw_sql, "admin-c")
    assert f"/users/{cid}/delete" not in html and f"/users/{cid}/password" not in html
    assert '<option value="administrator"' not in html          # cannot hand out the role
