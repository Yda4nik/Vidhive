"""Agents report live download progress with each heartbeat; the dashboard polls it."""

from app.services import activity


def test_heartbeat_progress_is_served_by_activity_endpoint(client, raw_sql):
    raw_sql("INSERT INTO workers (name, state) VALUES ('a-live', 'online')")
    wid = raw_sql("SELECT id FROM workers WHERE name='a-live'")[0][0]
    r = client.post(f"/api/workers/{wid}/heartbeat", json={
        "downloads": [{"external_id": 7, "percent": 42.5, "title": "Lecture"}],
    })
    assert r.status_code == 200
    data = client.get("/api/workers/activity").json()
    assert data[str(wid)] == [{"external_id": 7, "percent": 42.5, "title": "Lecture"}]
    # an empty report clears it
    client.post(f"/api/workers/{wid}/heartbeat", json={"downloads": []})
    assert str(wid) not in client.get("/api/workers/activity").json()
    activity.forget(wid)
