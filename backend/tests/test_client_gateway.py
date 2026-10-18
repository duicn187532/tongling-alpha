"""The ngrok process is a restricted client gateway, not a second admin UI."""

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app import client_gateway
from app.db import Session, Project, Requirement, Link
from sqlalchemy import select
from app.workflow import auto_decide_safe
from app.evidence import ingest
from app.knowledge_federation import local_source_ids, policy_for
from app.intelligence import settings


@pytest.fixture(autouse=True)
def clear_pairing_attempts():
    client_gateway.ATTEMPTS.clear()


def test_gateway_pairing_and_project_isolation(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as alice, TestClient(app, base_url="https://testserver") as bob:
        assert alice.get("/").status_code == 200
        assert alice.get("/openapi.json").status_code == 404
        assert alice.get("/api/session/context").json() == {"role": "CLIENT", "authenticated": False}
        assert alice.get("/api/projects").status_code == 401
        assert alice.get("/api/settings").status_code == 403
        code = client_gateway.pairing_details()["code"]
        assert alice.post("/api/pair", json={"code": code}).status_code == 200
        assert bob.post("/api/pair", json={"code": code}).status_code == 200
        assert alice.get("/api/session/context").json()["authenticated"] is True
        first = alice.post("/api/projects", json={"name": "Alice", "description": "我要查詢帳單"})
        assert first.status_code == 200, first.text
        pid = first.json()["id"]
        assert "client_owner_id" not in first.json()["state"]
        assert [p["id"] for p in alice.get("/api/projects").json()] == [pid]
        assert bob.get("/api/projects").json() == []
        assert bob.get(f"/api/projects/{pid}").status_code == 404
        assert bob.get(f"/api/projects/{pid}/pulse").status_code == 404
        assert bob.post(f"/api/projects/{pid}/agent/continue").status_code == 404
        assert alice.get(f"/api/projects/{pid}").status_code == 200
        pulse = alice.get(f"/api/projects/{pid}/pulse")
        assert pulse.status_code == 200
        assert pulse.json()["id"] == pid
        assert "sources" not in pulse.json() and "revisions" not in pulse.json()
        assert set(pulse.json()["state"]) == {"step", "progress", "analysis_stale", "draft_id"}
        assert alice.get("/api/knowledge/servers").status_code == 403
        assert alice.get("/api/knowledge/documents").status_code == 403
        assert alice.get("/api/client/workspace-config").status_code == 200
        assert "endpoint" not in alice.get("/api/client/workspace-config").json()
        second = bob.post("/api/projects", json={"name": "Bob", "description": "Bob 的私人需求"}).json()
        with Session() as db:
            shared = ingest(db, "主機共用政策", [{"text": "共用知識範例"}], kind="INTERNAL_POLICY")
            db.flush()
            alice_policy = policy_for(db, pid, settings(db))
            visible = local_source_ids(db, pid, alice_policy)
            assert shared.id in visible
            bob_sources = set(db.scalars(select(Link.source_id).where(Link.project_id == second["id"])))
            assert not visible.intersection(bob_sources)
            assert second["id"] not in [p["id"] for p in alice.get("/api/projects").json()]
            assert db.get(Project, second["id"]).state["client_owner_id"] != db.get(Project, pid).state["client_owner_id"]
            db.rollback()


def test_pairing_survives_new_server_instance_and_logout_revokes(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as first:
        code = client_gateway.pairing_details()["code"]
        assert first.post("/api/pair", json={"code": code}).status_code == 200
        cookies = dict(first.cookies)
    # A fresh request client represents a restarted gateway: authentication is
    # resolved from SQLite, not a process-local session dictionary.
    with TestClient(app, base_url="https://testserver", cookies=cookies) as second:
        assert second.get("/api/session/context").json()["authenticated"] is True
        assert second.get("/api/projects").status_code == 200
        assert second.post("/api/logout").status_code == 200
    with TestClient(app, base_url="https://testserver", cookies=cookies) as third:
        assert third.get("/api/session/context").json()["authenticated"] is False
        assert third.get("/api/projects").status_code == 401


def test_gateway_manual_workflow_and_owned_trash(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as alice, TestClient(app, base_url="https://testserver") as bob:
        code = client_gateway.pairing_details()["code"]
        assert alice.post("/api/pair", json={"code": code}).status_code == 200
        assert bob.post("/api/pair", json={"code": code}).status_code == 200
        created = alice.post("/api/projects", json={"name": "手動帳單專案", "description": "客戶需要電子帳單"}).json()
        pid = created["id"]
        assert alice.post(f"/api/projects/{pid}/workflow/mode", json={"mode": "MANUAL"}).status_code == 200
        assert alice.get(f"/api/projects/{pid}").json()["mode"] == "MANUAL"
        meeting = alice.post(f"/api/projects/{pid}/meetings", json={"title": "需求會議", "meeting_date": "2026-10-18", "transcript": "決議：客戶同意後送出"})
        assert meeting.status_code == 200, meeting.text
        assert bob.post(f"/api/projects/{pid}/workflow/mode", json={"mode": "AGENT"}).status_code == 404
        assert alice.get("/api/knowledge/graph/status").status_code == 403
        assert alice.get(f"/api/knowledge/graph/status?project_id={pid}").status_code == 200
        assert alice.delete(f"/api/projects/{pid}").status_code == 200
        assert pid in [row["id"] for row in alice.get("/api/projects/trash").json()]
        assert pid not in [row["id"] for row in bob.get("/api/projects/trash").json()]
        assert bob.post(f"/api/projects/{pid}/restore").status_code == 404
        assert alice.post(f"/api/projects/{pid}/restore").status_code == 200
        assert alice.get(f"/api/projects/{pid}").status_code == 200


def test_gateway_can_attach_shared_knowledge_without_cross_client_leak(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as alice, TestClient(app, base_url="https://testserver") as bob:
        code = client_gateway.pairing_details()["code"]
        assert alice.post("/api/pair", json={"code": code}).status_code == 200
        assert bob.post("/api/pair", json={"code": code}).status_code == 200
        aid = alice.post("/api/projects", json={"name": "共用知識測試"}).json()["id"]
        bid = bob.post("/api/projects", json={"name": "私人知識測試"}).json()["id"]
        with Session.begin() as db:
            shared = ingest(db, "共用規範", [{"text": "主機共用知識"}], kind="INTERNAL_POLICY")
            private = ingest(db, "Bob 私人來源", [{"text": "私人資料"}], kind="USER_UPLOAD", project=db.get(Project, bid))
            db.flush()
            shared_id, private_id = shared.id, private.id
        assert alice.get(f"/api/sources/{shared_id}?project_id={aid}").status_code == 200
        assert alice.post(f"/api/projects/{aid}/knowledge/attach", json={"source_id": shared_id}).status_code == 200
        assert alice.get(f"/api/sources/{private_id}?project_id={aid}").status_code == 404
        assert alice.post(f"/api/projects/{aid}/knowledge/attach", json={"source_id": private_id}).status_code == 404


def test_gateway_guidance_and_delegation(client, monkeypatch):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as browser:
        code = client_gateway.pairing_details()["code"]
        assert browser.post("/api/pair", json={"code": code}).status_code == 200
        guidance = browser.post("/api/agent/guidance", json={"brief": "客戶要在線上申請電子帳單"})
        assert guidance.status_code == 200, guidance.text
        assert "客戶要在線上申請電子帳單" in guidance.json()["refined_brief"]
        project = browser.post("/api/projects", json={"name": "電子帳單", "description": "客戶要在線上申請電子帳單"}).json()
        response = browser.post(f"/api/projects/{project['id']}/agent/delegation", json={"mode": "AUTO_DECIDE_SAFE"})
        assert response.status_code == 200, response.text
        detail = browser.get(f"/api/projects/{project['id']}").json()
        assert detail["state"]["delegation_mode"] == "AUTO_DECIDE_SAFE"
        assert detail["state"]["knowledge_policy"]["scopes"] == ["PROJECT_LOCAL", "LOCAL_PRIVATE"]


def test_delegated_agent_accepts_clear_requirement_but_not_open_question(client, project):
    with Session() as db:
        row = db.get(Project, project["id"])
        row.state = {**row.state, "delegation_mode": "AUTO_DECIDE_SAFE"}
        source = ingest(db, "需求", [{"text": "必須顯示成功訊息"}], kind="REQUIREMENT_TEXT", project=row)
        db.flush()
        clear = Requirement(project_id=row.id, topic="成功訊息", statement="必須顯示成功訊息",
                            status="CONFIRMED_CANDIDATE", evidence=[{"source_id": source.id, "kind": "REQUIREMENT", "statement": "必須顯示成功訊息"}])
        open_item = Requirement(project_id=row.id, topic="生效時間", statement="生效時間待確認",
                                status="OPEN", evidence=[{"source_id": source.id, "kind": "OPEN_ITEM", "statement": "生效時間待確認"}])
        db.add_all([clear, open_item]); db.flush()
        accepted = auto_decide_safe(db, row)
        assert accepted == [clear.id]
        assert clear.decision["decided_by"] == "AGENT_DELEGATED"
        assert open_item.decision == {}
        db.rollback()


def test_delegated_agent_reaches_reviewable_draft(client):
    import time
    created = client.post("/api/projects", json={"name": "電子帳單申請", "description": "必須顯示電子帳單申請成功訊息。"}).json()
    pid = created["id"]
    assert client.post(f"/api/projects/{pid}/agent/delegation", json={"mode": "AUTO_DECIDE_SAFE"}).status_code == 200
    job = client.post(f"/api/projects/{pid}/agent/continue").json()
    for _ in range(150):
        result = client.get(f"/api/jobs/{job['id']}").json()
        if result["status"] not in {"RUNNING", "QUEUED"}:
            break
        time.sleep(.05)
    assert result["status"] == "SUCCEEDED", result
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["state"]["draft_id"]
    assert all(r["decision"].get("decided_by") == "AGENT_DELEGATED" for r in detail["requirements"])
    assert detail["runtime"] == "WAITING_HUMAN"  # Final adoption stays human.
