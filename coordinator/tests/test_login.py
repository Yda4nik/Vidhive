def test_login_page_renders(anon_client):
    r = anon_client.get("/login")
    assert r.status_code == 200
    assert "Вход" in r.text


def test_login_success_sets_session(anon_client):
    r = anon_client.post("/login", data={"username": "tester-admin", "password": "adminpass"})
    assert r.status_code == 200            # followed redirect to /
    assert anon_client.cookies.get("session") is not None


def test_login_failure_shows_error(anon_client):
    r = anon_client.post("/login", data={"username": "tester-admin", "password": "nope"})
    assert r.status_code == 200
    assert "неверный" in r.text.lower()


def test_logout_clears_session(client):
    # `client` is already logged in as admin.
    r = client.post("/logout")
    assert r.status_code == 200            # followed redirect to /login
    assert "Вход" in r.text
