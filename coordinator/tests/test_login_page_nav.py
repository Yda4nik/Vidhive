"""Before signing in, the page shows no navigation — only the logo and the theme switch."""

NAV_LINKS = ('href="/jobs"', 'href="/library"', 'href="/docs"')


def test_login_page_has_no_navigation(anon_client):
    html = anon_client.get("/login").text
    assert "Вход" in html and "Vidhive" in html
    for link in NAV_LINKS:
        assert link not in html, link


def test_register_page_has_no_navigation(anon_client):
    html = anon_client.get("/register").text
    for link in NAV_LINKS:
        assert link not in html, link


def test_navigation_is_shown_after_signing_in(client):
    html = client.get("/").text
    for link in NAV_LINKS:
        assert link in html, link


def test_register_page_has_no_menu_even_for_someone_already_signed_in(client, raw_sql):
    client.post("/users/invite")
    token = raw_sql("SELECT token FROM invites ORDER BY id DESC LIMIT 1")[0][0]
    html = client.get(f"/register?token={token}").text            # `client` is signed in as the administrator
    assert 'name="username"' in html
    for link in NAV_LINKS + ('href="/settings"', 'action="/logout"'):
        assert link not in html, link
