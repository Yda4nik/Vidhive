"""User-controlled text must never reach JavaScript code.

Jinja HTML-escapes ``'`` to ``&#39;``, but a browser decodes entities in an
attribute *before* running the handler, so ``onclick="f('{{ name }}')"`` is
injectable. The fix is to pass text via data-* attributes. These tests render the
pages with hostile values and parse the HTML to prove nothing leaks into code.
"""

from html.parser import HTMLParser

MARK = "PWN7"
HOSTILE = "x');" + MARK + "();//"


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers = []      # (tag, attr, value) for on* attributes
        self.data_attrs = []    # values of data-* attributes
        self.scripts = []       # inline <script> bodies
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.startswith("on"):
                self.handlers.append((tag, name, value or ""))
            elif name.startswith("data-"):
                self.data_attrs.append(value or "")
        if tag == "script" and not any(n == "src" for n, _ in attrs):
            self._in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.scripts.append(data)


def _scan(html):
    s = _Scan()
    s.feed(html)
    return s


def _assert_no_user_data_in_code(html, page):
    s = _scan(html)
    for tag, name, value in s.handlers:
        assert MARK not in value, f"{page}: <{tag} {name}> carries user data: {value[:120]}"
    for body in s.scripts:
        assert MARK not in body, f"{page}: inline <script> carries user data"
    return s


def _seed(client, raw_sql):
    """Hostile values in every user-controlled field that reaches a template."""
    client.post("/api/groups", json={"name": HOSTILE})                       # library tabs
    client.post("/users/invite")                                              # a hostile username
    token = raw_sql("SELECT token FROM invites")[0][0]
    client.post("/register", data={"token": token, "username": HOSTILE, "password": "p", "confirm": "p"})
    client.post("/login", data={"username": "tester-admin", "password": "adminpass"})
    wid = client.post("/api/workers/register", json={"name": HOSTILE}).json()["id"]
    raw_sql(                                                                  # SSH-deployed server row
        "INSERT INTO agent_deployments (worker_name, ssh_host, ssh_port, ssh_user, install_dir,"
        " service_name, storage_path) VALUES (?,?,22,?,'/opt/vidhive','vidhive-agent','/var/lib/vidhive/v')",
        (HOSTILE, HOSTILE, HOSTILE),
    )
    # a video whose (externally sourced) title is hostile
    job = client.post("/api/jobs", json={"range_start": 5, "range_end": 5, "chunk_size": 1}).json()
    client.post(f"/api/jobs/{job['id']}/start")
    lease = client.post(f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}).json()
    client.post(f"/api/workers/{wid}/progress", json={
        "chunk_id": lease["chunk_id"], "next_id": 6,
        "items": [{"external_id": 5, "status": "completed", "title": HOSTILE,
                   "storage_path": "/data/videos/0/5/video.mp4"}]})
    return raw_sql("SELECT id FROM items WHERE external_id=5")[0][0]


def test_hostile_values_never_reach_inline_javascript(client, raw_sql):
    item_id = _seed(client, raw_sql)
    seen_in_data = {}
    for page in ("/library", "/users", "/servers", "/jobs", "/", f"/player/{item_id}"):
        r = client.get(page)
        assert r.status_code == 200, page
        s = _assert_no_user_data_in_code(r.text, page)
        seen_in_data[page] = any(MARK in v for v in s.data_attrs)

    # Not vacuous: the values really are rendered — as inert data-* attributes.
    assert seen_in_data["/library"] and seen_in_data["/users"] and seen_in_data["/servers"]


def test_library_group_filter_query_param_is_json_encoded(client):
    html = client.get('/library?group=x"</script><script>PWN7()</script>').text
    # No live injected tag; the value is present only as an inert, escaped JS string.
    assert "<script>PWN7()" not in html
    assert "</script><script>PWN7" not in html
    assert "\\u003c/script\\u003e" in html
    # ...and the page's own scripts are intact (injected text did not split a block).
    assert html.count("<script") == html.count("</script>")


# --------------------------------------------------------------------------- #
# Security headers
# --------------------------------------------------------------------------- #
def test_security_headers_present(client):
    r = client.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "object-src 'none'" in r.headers["Content-Security-Policy"]


def test_swagger_docs_are_exempt_from_csp(client):
    r = client.get("/docs")
    assert r.status_code == 200
    assert "Content-Security-Policy" not in r.headers
    assert r.headers["X-Frame-Options"] == "DENY"
