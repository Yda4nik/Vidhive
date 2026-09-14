def _wipe_users(raw_sql):
    raw_sql("DELETE FROM user_roles")
    raw_sql("DELETE FROM users")


def test_login_bounces_to_setup_when_no_users(anon_client, raw_sql):
    _wipe_users(raw_sql)
    r = anon_client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_setup_wizard_creates_first_admin(anon_client, raw_sql):
    _wipe_users(raw_sql)
    assert "Первоначальная настройка" in anon_client.get("/setup").text
    r = anon_client.post(
        "/setup", data={"username": "root", "password": "rootpw", "confirm": "rootpw"}
    )
    assert r.status_code == 200            # created, logged in, redirected to /
    # The new admin has full access.
    assert anon_client.get("/servers").status_code == 200
    # Setup is closed once a user exists.
    r2 = anon_client.get("/setup", follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/login"


def test_setup_rejects_mismatched_passwords(anon_client, raw_sql):
    _wipe_users(raw_sql)
    r = anon_client.post(
        "/setup", data={"username": "root", "password": "a", "confirm": "b"}
    )
    assert "не совпад" in r.text.lower()
