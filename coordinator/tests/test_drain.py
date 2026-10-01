"""'Distribute to other servers' never loses a video: the source server only drains,
and is removed once every video it holds has a completed copy somewhere else."""

import asyncio
import pathlib

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services import drain, youtube

DB = pathlib.Path(__file__).parent / "_test.db"


def _run(coro_fn):
    """Run an async service call in its own engine (foreign keys on, like the app)."""
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{DB.as_posix()}")

        @event.listens_for(engine.sync_engine, "connect")
        def _fk(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        try:
            async with AsyncSession(engine) as s:
                result = await coro_fn(s)
                await s.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _finalize():
    return _run(drain.finalize_drained_workers)


def _worker(client, name, agent_url=None):
    body = {"name": name}
    if agent_url:
        body["agent_url"] = agent_url
    return client.post("/api/workers/register", json=body).json()["id"]


def _lease(client, wid):
    r = client.post(f"/api/workers/{wid}/lease", json={"worker_id": wid, "lease_seconds": 120})
    return r.json() if r.status_code == 200 else None


def _download(client, wid, ext, *, status="completed", title="clip", url=None):
    """Make ``wid`` lease the next chunk and report ``ext`` with ``status``."""
    ch = _lease(client, wid)
    assert ch is not None, "no chunk to lease"
    item = {"external_id": ext, "status": status, "title": title,
            "storage_path": f"/data/videos/0/{ext}/video.mp4" if status == "completed" else None}
    if url:
        item["download_url"] = url
    client.post(f"/api/workers/{wid}/progress", json={
        "chunk_id": ch["chunk_id"], "next_id": ext + 1, "items": [item]})
    client.post(f"/api/workers/{wid}/complete", json={"chunk_id": ch["chunk_id"]})
    return ch


def _own_video(client, wid, ext):
    j = client.post("/api/jobs", json={"range_start": ext, "range_end": ext, "chunk_size": 1}).json()["id"]
    client.post(f"/api/jobs/{j}/start")
    _download(client, wid, ext)
    return j


def _state(raw_sql, name):
    r = raw_sql("SELECT state FROM workers WHERE name=?", (name,))
    return r[0][0] if r else None


def _copies(raw_sql, ext):
    return raw_sql(
        "SELECT w.name FROM items i JOIN workers w ON w.id=i.worker_id "
        "WHERE i.external_id=? AND i.status='completed' ORDER BY w.name", (ext,))


def test_server_is_removed_only_after_the_video_has_a_copy_elsewhere(client, raw_sql):
    src, dst = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)

    assert client.request("DELETE", f"/api/workers/{src}?mode=redistribute").status_code == 200
    assert _state(raw_sql, "src") == "draining"
    assert _copies(raw_sql, 9) == [("src",)]                 # nothing deleted yet

    assert _finalize() == 0                                  # no copy yet: it must stay
    assert _state(raw_sql, "src") == "draining"

    _download(client, dst, 9)                                # the re-download completes on dst
    assert _copies(raw_sql, 9) == [("dst",), ("src",)]
    assert _finalize() == 1
    assert _state(raw_sql, "src") is None                    # now it is removed...
    assert _copies(raw_sql, 9) == [("dst",)]                 # ...and the video survives on dst


def test_a_failed_redownload_never_loses_the_video(client, raw_sql):
    src, dst = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)
    client.request("DELETE", f"/api/workers/{src}?mode=redistribute")

    _download(client, dst, 9, status="failed")               # the re-download fails
    assert _finalize() == 0
    assert _state(raw_sql, "src") == "draining"
    assert _copies(raw_sql, 9) == [("src",)]                 # the original is still there


def test_a_draining_server_takes_no_new_work_and_stays_draining(client, raw_sql):
    src, dst = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)
    client.request("DELETE", f"/api/workers/{src}?mode=redistribute")

    assert _lease(client, src) is None                       # the transfer job is for others
    assert client.post(f"/api/workers/{src}/heartbeat", json={}).status_code == 200
    assert _state(raw_sql, "src") == "draining"              # a heartbeat does not revive it
    _worker(client, "src")                                   # nor does re-registering
    assert _state(raw_sql, "src") == "draining"
    assert _lease(client, dst)["range_start"] == 9           # dst picks the work up


def test_nothing_is_redownloaded_when_a_copy_already_exists_elsewhere(client, raw_sql):
    src, dst = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)
    _own_video(client, dst, 9)                               # same video already on dst

    assert client.request("DELETE", f"/api/workers/{src}?mode=redistribute").status_code == 200
    assert _state(raw_sql, "src") is None                    # nothing exclusive: removed at once
    assert raw_sql("SELECT COUNT(*) FROM jobs WHERE name LIKE 'Перенос%'")[0][0] == 0
    assert _copies(raw_sql, 9) == [("dst",)]


def test_redistribute_twice_is_refused(client):
    src, _ = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)
    assert client.request("DELETE", f"/api/workers/{src}?mode=redistribute").status_code == 200
    assert client.request("DELETE", f"/api/workers/{src}?mode=redistribute").status_code == 409


def test_a_draining_server_can_still_be_purged(client, raw_sql):
    src, _ = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)
    client.request("DELETE", f"/api/workers/{src}?mode=redistribute")
    assert client.request("DELETE", f"/api/workers/{src}?mode=purge").status_code == 200
    assert _state(raw_sql, "src") is None


def test_the_workers_api_lists_a_draining_server(client):
    src, _ = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)
    client.request("DELETE", f"/api/workers/{src}?mode=redistribute")
    states = {w["name"]: w["state"] for w in client.get("/api/workers").json()}
    assert states == {"src": "draining", "dst": "online"}


def test_a_youtube_video_is_redownloaded_from_its_url(client, raw_sql, monkeypatch):
    """YouTube videos have no numeric template — the transfer job must carry the URL."""
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    monkeypatch.setattr(youtube, "expand", lambda u: [{"id": "dQw4w9WgXcQ", "url": url}])
    src, _ = _worker(client, "src"), _worker(client, "dst")
    client.post("/add", data={"source": "youtube", "mode": "link",
                              "value": "https://youtu.be/dQw4w9WgXcQ", "name": "yt"})
    ext = youtube.yt_external_id("dQw4w9WgXcQ")
    _download(client, src, ext, url=url)

    assert client.request("DELETE", f"/api/workers/{src}?mode=redistribute").status_code == 200
    job = raw_sql("SELECT id, source FROM jobs WHERE name LIKE 'Перенос%'")[0]
    assert job[1] == "youtube"
    assert raw_sql("SELECT url FROM range_chunks WHERE job_id=?", (job[0],)) == [(url,)]


def test_the_servers_page_shows_how_many_videos_are_left(client):
    src, _ = _worker(client, "src"), _worker(client, "dst")
    _own_video(client, src, 9)
    _own_video(client, src, 11)
    client.request("DELETE", f"/api/workers/{src}?mode=redistribute")
    html = client.get("/servers").text
    assert "переносит · осталось 2" in html
    assert 'data-state="draining"' in html
