def test_viewer_cannot_create_job_via_api(anon_client, make_user):
    make_user("val", "pw", "viewer")
    anon_client.post("/login", data={"username": "val", "password": "pw"})
    r = anon_client.post("/api/jobs", json={"range_start": 0, "range_end": 9})
    assert r.status_code == 403


def test_viewer_can_read_jobs(anon_client, make_user):
    make_user("val", "pw", "viewer")
    anon_client.post("/login", data={"username": "val", "password": "pw"})
    assert anon_client.get("/api/jobs").status_code == 200
    assert anon_client.get("/").status_code == 200


def test_operator_can_create_job(anon_client, make_user):
    make_user("op", "pw", "operator")
    anon_client.post("/login", data={"username": "op", "password": "pw"})
    r = anon_client.post("/api/jobs", json={"range_start": 0, "range_end": 9})
    assert r.status_code == 201


def test_anonymous_api_is_unauthorized(anon_client):
    assert anon_client.get("/api/jobs").status_code == 401


def test_anonymous_page_redirects_to_login(anon_client):
    r = anon_client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_viewer_ui_hides_add_form(anon_client, make_user):
    make_user("val", "pw", "viewer")
    anon_client.post("/login", data={"username": "val", "password": "pw"})
    assert 'action="/add"' not in anon_client.get("/jobs").text
