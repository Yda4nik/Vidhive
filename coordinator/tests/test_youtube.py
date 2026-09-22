from app.services import youtube


def _one_video(url):
    return [{"id": "dQw4w9WgXcQ", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "Rick"}]


def test_add_youtube_video_creates_url_chunk(client, raw_sql, monkeypatch):
    monkeypatch.setattr(youtube, "expand", _one_video)
    r = client.post(
        "/add",
        data={"source": "youtube", "mode": "link",
              "value": "https://youtu.be/dQw4w9WgXcQ", "name": "yt"},
    )
    assert r.status_code == 200
    jid = raw_sql("SELECT id FROM jobs WHERE name='yt'")[0][0]
    assert raw_sql("SELECT source FROM jobs WHERE id=?", (jid,))[0][0] == "youtube"
    tgt = raw_sql("SELECT url FROM job_targets WHERE job_id=?", (jid,))
    assert tgt and "watch?v=dQw4w9WgXcQ" in tgt[0][0]
    ch = raw_sql("SELECT url, range_start, range_end FROM range_chunks WHERE job_id=?", (jid,))
    assert ch and ch[0][0] and ch[0][1] == ch[0][2]  # single-id chunk carrying the URL


def test_add_youtube_playlist_expands(client, raw_sql, monkeypatch):
    def playlist(url):
        return [
            {"id": "a" * 11, "url": "https://www.youtube.com/watch?v=" + "a" * 11},
            {"id": "b" * 11, "url": "https://www.youtube.com/watch?v=" + "b" * 11},
            {"id": "c" * 11, "url": "https://www.youtube.com/watch?v=" + "c" * 11},
        ]

    monkeypatch.setattr(youtube, "expand", playlist)
    client.post(
        "/add",
        data={"source": "youtube", "mode": "link",
              "value": "https://www.youtube.com/playlist?list=PL", "name": "pl"},
    )
    jid = raw_sql("SELECT id FROM jobs WHERE name='pl'")[0][0]
    n = raw_sql(
        "SELECT COUNT(*) FROM range_chunks WHERE job_id=? AND url IS NOT NULL", (jid,)
    )[0][0]
    assert n == 3


def test_auto_mode_detects_youtube(client, raw_sql, monkeypatch):
    monkeypatch.setattr(youtube, "expand", _one_video)
    client.post(
        "/add",
        data={"source": "auto", "mode": "link",
              "value": "https://youtu.be/dQw4w9WgXcQ", "name": "auto-yt"},
    )
    assert raw_sql("SELECT source FROM jobs WHERE name='auto-yt'")[0][0] == "youtube"


def test_lease_returns_youtube_url(client, raw_sql, monkeypatch):
    monkeypatch.setattr(youtube, "expand", _one_video)
    client.post(
        "/add",
        data={"source": "youtube", "mode": "link",
              "value": "https://youtu.be/dQw4w9WgXcQ", "name": "yt2"},
    )
    wid = client.post("/api/workers/register", json={"name": "agent-01"}).json()["id"]
    lease = client.post(
        f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120}
    ).json()
    assert "watch?v=dQw4w9WgXcQ" in lease["target_template"]


def test_yt_external_id_is_stable():
    a = youtube.yt_external_id("dQw4w9WgXcQ")
    b = youtube.yt_external_id("dQw4w9WgXcQ")
    assert a == b and 0 < a < (1 << 56)
