import re
from app.db import Session, Event
from app.main import initial_event_cursor
from sqlalchemy import select


def test_large_project_is_compressed_and_assets_are_cacheable(client):
    created = client.post("/api/projects", json={
        "name": "傳輸測試",
        "description": "測試說明。" * 1200,
    })
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    response = client.get(f"/api/projects/{pid}", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["cache-control"] == "private, no-store"

    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    asset = re.search(r'/assets/[^" ]+\.js', page.text)
    assert asset
    cached = client.get(asset.group(0), headers={"Accept-Encoding": "gzip"})
    assert cached.status_code == 200
    assert cached.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert cached.headers["content-encoding"] == "gzip"


def test_new_event_connection_skips_old_history(client):
    created = client.post("/api/projects", json={"name": "事件測試"})
    pid = created.json()["id"]
    with Session() as db:
        latest = db.scalar(select(Event.id).where(Event.project_id == pid)
                           .order_by(Event.id.desc()).limit(1))
        assert latest
        assert initial_event_cursor(db, pid, 0, 0) == latest
        assert initial_event_cursor(db, pid, latest - 1, 0) == latest - 1
        assert initial_event_cursor(db, pid, 0, latest - 1) == latest - 1
