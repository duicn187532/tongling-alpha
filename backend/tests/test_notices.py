"""Finished work notices can be cleared by their owner only."""

import time

from fastapi.testclient import TestClient

from app import client_gateway
from app.main import app


def finished_job(client, pid):
    job = client.post("/api/jobs", json={"kind": "RAG", "project_id": pid, "query": "通知"}).json()
    for _ in range(100):
        row = client.get(f"/api/jobs/{job['job_id']}").json()
        if row["status"] not in {"QUEUED", "RUNNING"}:
            return row
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_finished_job_and_activity_can_be_dismissed(client, project):
    job = finished_job(client, project["id"])
    dismissed = client.post(f"/api/jobs/{job['job_id']}/dismiss")
    assert dismissed.status_code == 200 and dismissed.json()["dismissed_at"]
    listed = {row["job_id"]: row for row in client.get("/api/jobs").json()}
    assert listed[job["job_id"]]["dismissed_at"]
    task = client.post(f"/api/projects/{project['id']}/knowledge/search", json={"query": "通知", "project_id": project["id"]})
    assert task.status_code == 200
    activity = client.get(f"/api/projects/{project['id']}").json()["activities"][0]
    assert activity["status"] == "SUCCEEDED"
    assert client.post(f"/api/activities/{activity['id']}/dismiss").json()["dismissed_at"]


def test_other_client_cannot_dismiss_foreign_notices(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as alice, TestClient(app, base_url="https://testserver") as bob:
        code = client_gateway.pairing_details()["code"]
        assert alice.post("/api/pair", json={"code": code}).status_code == 200
        assert bob.post("/api/pair", json={"code": code}).status_code == 200
        pid = alice.post("/api/projects", json={"name": "通知隔離"}).json()["id"]
        job = finished_job(alice, pid)
        assert bob.post(f"/api/jobs/{job['job_id']}/dismiss").status_code == 404
        assert alice.post(f"/api/jobs/{job['job_id']}/dismiss").status_code == 200
