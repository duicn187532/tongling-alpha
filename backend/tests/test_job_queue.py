"""Durable jobs, isolation, overload and resource limits."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app import client_gateway, job_queue
from app.db import Activity, AppJob, ClientSession, Project, ResourceLease, Run, Session, Workspace
from app.resource_limits import acquire_llm, release_llm


def wait_for(client, jid, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        response = client.get(f"/api/jobs/{jid}")
        assert response.status_code == 200, response.text
        if response.json()["status"] in job_queue.TERMINAL:
            return response.json()
        time.sleep(0.1)
    raise AssertionError("工作沒有在時限內完成")


def test_guidance_job_is_durable_and_idempotent(client):
    headers = {"Idempotency-Key": "guidance-idempotent-001"}
    body = {"kind": "GUIDANCE", "brief": "客戶想收到帳單提醒"}
    first = client.post("/api/jobs", json=body, headers=headers)
    second = client.post("/api/jobs", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    changed = client.post("/api/jobs", json={**body, "brief": "不同需求"}, headers=headers)
    assert changed.status_code == 409
    finished = wait_for(client, first.json()["job_id"])
    assert finished["status"] == "COMPLETED"
    assert "帳單" in finished["result"]["summary"]
    assert "session_id" not in finished
    assert "payload" not in finished
    listed = client.get("/api/jobs").json()
    assert first.json()["job_id"] in {row["job_id"] for row in listed}
    assert "result" not in listed[0]


def test_queue_capacity_and_per_user_limit_are_rejected(client):
    with Session() as db:
        workspace = db.scalar(select(Workspace))
        workspace.settings = {**workspace.settings, "demo_queue": {**job_queue.DEFAULTS,
                              "queue_capacity": 1, "per_user_jobs": 10}}
        db.flush()
        job_queue.submit(db, "GUIDANCE", payload={"brief": "第一份"})
        with pytest.raises(HTTPException) as full:
            job_queue.submit(db, "GUIDANCE", payload={"brief": "第二份"})
        assert full.value.status_code == 503
        db.rollback()
    with Session() as db:
        workspace = db.scalar(select(Workspace))
        workspace.settings = {**workspace.settings, "demo_queue": {**job_queue.DEFAULTS,
                              "queue_capacity": 20, "per_user_jobs": 1}}
        db.flush()
        job_queue.submit(db, "GUIDANCE", payload={"brief": "第一份"})
        with pytest.raises(HTTPException) as busy:
            job_queue.submit(db, "GUIDANCE", payload={"brief": "第二份"})
        assert busy.value.status_code == 429
        db.rollback()


def test_demo_without_pairing_still_uses_distinct_sessions(client, monkeypatch):
    assert client.put("/api/admin/client-access/mode", json={"mode": "demo"}).status_code == 200
    client_gateway.DEMO_ISSUES.clear()
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    try:
        _demo_queue_isolation()
    finally:
        monkeypatch.setattr(client_gateway, "IS_GATEWAY", False)
        client.put("/api/admin/client-access/mode", json={"mode": "pairing"})


def _demo_queue_isolation():
    with TestClient(app, base_url="https://testserver") as alice, TestClient(app, base_url="https://testserver") as bob:
        assert alice.post("/api/session/demo").status_code == 200
        assert bob.post("/api/session/demo").status_code == 200
        assert alice.get("/api/session/context").json()["authenticated"] is True
        assert bob.get("/api/session/context").json()["authenticated"] is True
        a = alice.post("/api/projects", json={"name": "Alice 展示", "description": "帳單提醒"}).json()
        b = bob.post("/api/projects", json={"name": "Bob 展示", "description": "付款通知"}).json()
        assert a["id"] != b["id"]
        a_job = alice.post("/api/jobs", json={"kind": "RAG", "project_id": a["id"], "query": "帳單"})
        b_job = bob.post("/api/jobs", json={"kind": "RAG", "project_id": b["id"], "query": "付款"})
        assert a_job.status_code == b_job.status_code == 202
        assert a_job.json()["job_id"] != b_job.json()["job_id"]
        assert bob.get(f'/api/jobs/{a_job.json()["job_id"]}').status_code == 404
        assert bob.post(f'/api/jobs/{a_job.json()["job_id"]}/cancel').status_code == 404
        assert bob.post("/api/jobs", json={"kind": "RAG", "project_id": a["id"], "query": "帳單"}).status_code == 404
        assert a_job.json()["job_id"] in {row["job_id"] for row in alice.get("/api/jobs").json()}
        assert a_job.json()["job_id"] not in {row["job_id"] for row in bob.get("/api/jobs").json()}
        assert wait_for(alice, a_job.json()["job_id"])["status"] == "COMPLETED"
        assert wait_for(bob, b_job.json()["job_id"])["status"] == "COMPLETED"


def test_llm_lease_is_shared_and_has_deadline(client):
    with Session.begin() as db:
        lease = ResourceLease(resource="LLM", owner_id="another-server",
                              expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat())
        db.add(lease)
        db.flush()
        lease_id = lease.id
    try:
        with pytest.raises(TimeoutError):
            acquire_llm(1, 0.05)
    finally:
        with Session.begin() as db:
            db.delete(db.get(ResourceLease, lease_id))
    acquire_llm(1, 1)
    release_llm()
    acquire_llm(1, 1, heavy=True)
    try:
        with pytest.raises(TimeoutError):
            acquire_llm(2, 0.05)
    finally:
        release_llm()


def test_host_monitor_is_not_available_to_gateway(client, monkeypatch):
    assert client.get("/api/admin/queue/status").status_code == 200
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as visitor:
        assert visitor.get("/api/admin/queue/status").status_code == 403


def test_queued_cancel_and_resume_keep_original_record(client):
    with Session.begin() as db:
        job = AppJob(session_id="host", kind="GUIDANCE", queue_class="AGENT",
                     idempotency_key="queued-cancel-fixture", payload={"brief": "付款提醒"},
                     available_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
        db.add(job)
        db.flush()
        original_id = job.id
    assert original_id in {row["job_id"] for row in client.get("/api/admin/queue/status").json()["active_jobs"]}
    cancelled = client.post(f"/api/jobs/{original_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    resumed = client.post(f"/api/jobs/{original_id}/resume")
    assert resumed.status_code == 202
    assert resumed.json()["job_id"] != original_id
    assert wait_for(client, resumed.json()["job_id"])["status"] == "COMPLETED"
    assert client.get(f"/api/jobs/{original_id}").json()["status"] == "CANCELLED"


def test_gateway_restart_does_not_interrupt_host_agent(client, project, monkeypatch):
    with Session.begin() as db:
        row = db.get(Project, project["id"])
        row.runtime = "RUNNING"
        row.epoch = 1
        run = Run(project_id=row.id, epoch=1, reason="TEST", status="RUNNING")
        db.add(run)
        db.flush()
        db.add(Activity(id=run.id, project_id=row.id, title="Test", status="RUNNING"))
        run_id = run.id
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as visitor:
        assert visitor.get("/api/health").status_code == 200
    with Session.begin() as db:
        assert db.get(Project, project["id"]).runtime == "RUNNING"
        assert db.get(Run, run_id).status == "RUNNING"
        assert db.get(Activity, run_id).status == "RUNNING"
        db.get(Project, project["id"]).runtime = "PAUSED"
        db.get(Run, run_id).status = "CANCELLED"
        db.get(Activity, run_id).status = "CANCELLED"


def test_running_job_timeout_requests_stop(client):
    with Session.begin() as db:
        job = AppJob(session_id="host", kind="GUIDANCE", queue_class="AGENT",
                     idempotency_key="running-timeout-fixture", payload={"brief": "逾時"},
                     status="RUNNING", attempts=1,
                     started_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
        db.add(job)
        db.flush()
        jid = job.id
    job_queue._claim()
    with Session.begin() as db:
        row = db.get(AppJob, jid)
        assert row.cancel_requested == 1
        assert "逾時" in row.error
        row.status = "FAILED"
        row.finished_at = datetime.now(timezone.utc).isoformat()


def test_monitor_counts_recent_sessions_only(client):
    before = client.get("/api/admin/queue/status").json()["active_sessions"]
    token = client_gateway.new_session("activity-monitor-fixture")
    with Session.begin() as db:
        row = db.scalar(select(ClientSession).where(ClientSession.client_id == "activity-monitor-fixture"))
        row.last_seen_at = int(time.time()) - 1000
    assert client.get("/api/admin/queue/status").json()["active_sessions"] == before
    assert client_gateway.session_client(token) == "activity-monitor-fixture"
    assert client.get("/api/admin/queue/status").json()["active_sessions"] == before + 1
    client_gateway.revoke_session(token)
