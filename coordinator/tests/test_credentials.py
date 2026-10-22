"""New accounts need a sane login and password; an invitation works exactly once."""

STRONG = "correct-horse-9"


def _invite(client, raw_sql):
    client.post("/users/invite")
    return raw_sql("SELECT token FROM invites ORDER BY id DESC LIMIT 1")[0][0]


def test_invitation_is_single_use(client, anon_client, raw_sql):
    token = _invite(client, raw_sql)
    first = anon_client.post("/register", data={"token": token, "username": "first", "password": STRONG, "confirm": STRONG},
                             follow_redirects=False)
    assert first.status_code == 303
    anon_client.post("/logout")
    second = anon_client.post("/register", data={"token": token, "username": "second", "password": STRONG, "confirm": STRONG})
    assert second.status_code == 200 and "second" not in str(raw_sql("SELECT username FROM users"))
    assert raw_sql("SELECT COUNT(*) FROM invites WHERE token=?", (token,))[0][0] == 0


def test_failed_registration_does_not_burn_the_invitation(client, anon_client, raw_sql):
    token = _invite(client, raw_sql)
    bad = anon_client.post("/register", data={"token": token, "username": "ok-name", "password": "short", "confirm": "short"})
    assert "не короче" in bad.text
    mismatch = anon_client.post("/register", data={"token": token, "username": "ok-name", "password": STRONG, "confirm": STRONG + "x"})
    assert "не совпадают" in mismatch.text
    assert raw_sql("SELECT COUNT(*) FROM invites WHERE token=?", (token,))[0][0] == 1       # still usable


def test_short_login_and_password_are_rejected_when_an_admin_creates_a_user(client, raw_sql):
    for data in ({"username": "", "password": STRONG}, {"username": "ab", "password": STRONG},
                 {"username": "a" * 33, "password": STRONG}, {"username": "validname", "password": "1234567"},
                 {"username": "validname", "password": "        "}):
        r = client.post("/users", data={**data, "role": "viewer"}, follow_redirects=False)
        assert r.status_code == 303 and "error=" in r.headers["location"], data
    assert raw_sql("SELECT COUNT(*) FROM users WHERE username IN ('', 'ab', 'validname')")[0][0] == 0
    ok = client.post("/users", data={"username": "  validname  ", "password": STRONG, "role": "viewer"}, follow_redirects=False)
    assert ok.status_code == 303 and "error=" not in ok.headers["location"]
    assert raw_sql("SELECT COUNT(*) FROM users WHERE username='validname'")[0][0] == 1    # trimmed


def test_password_change_enforces_the_minimum(client, raw_sql):
    uid = raw_sql("SELECT id FROM users LIMIT 1")[0][0]
    r = client.post(f"/users/{uid}/password", data={"password": "short"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
