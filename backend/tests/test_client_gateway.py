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
        assert alice.get("/").status_code == 404  # HTML is served by the frontend process.
        assert alice.get("/openapi.json").status_code == 404
        assert alice.get("/api/session/context").json()["authenticated"] is False
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
        assert [p["id"] for p in alice.get("/api/dashboard").json()["projects"]] == [pid]
        assert bob.get("/api/dashboard").json()["projects"] == []
        assert bob.post(f"/api/projects/{pid}/todos", json={"title": "不應存取"}).status_code == 404
        assert alice.post(f"/api/projects/{pid}/todos", json={"title": "客戶端代辦"}).status_code == 200
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


def _set_mode(monkeypatch, client, mode, network="LAN"):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", False)
    response = client.put("/api/admin/client-access/mode", json={"mode": mode, "demo_network": network})
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def restore_pairing(client, monkeypatch):
    yield
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", False)
    client.put("/api/admin/client-access/mode", json={"mode": "pairing"})
    client_gateway.DEMO_ISSUES.clear()


def test_access_mode_defaults_to_pairing_and_only_host_can_change(client, monkeypatch, restore_pairing):
    assert client.get("/api/admin/client-access").json()["mode"] == "pairing"
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as visitor:
        context = visitor.get("/api/session/context").json()
        assert context["mode"] == "pairing" and context["demo_available"] is False
        assert visitor.post("/api/session/demo").status_code == 403
        # The gateway process refuses the admin route even for a paired visitor.
        code = client_gateway.pairing_details()["code"]
        assert visitor.post("/api/pair", json={"code": code}).status_code == 200
        assert visitor.put("/api/admin/client-access/mode", json={"mode": "demo"}).status_code == 403
        assert visitor.get("/api/admin/client-access").status_code == 403
        assert visitor.put("/api/settings", json={}).status_code == 403
    assert client_gateway.access_config()["mode"] == "pairing"
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", False)
    # A non-loopback caller on the host process is refused as well.
    # No context manager: entering one would restart the host workers' lifespan.
    lan = TestClient(app, base_url="http://testserver", client=("192.168.1.30", 5000))
    assert lan.put("/api/admin/client-access/mode", json={"mode": "demo"}).status_code == 403


def test_demo_mode_sessions_are_isolated_and_die_with_demo_mode(client, monkeypatch, restore_pairing):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    with TestClient(app, base_url="https://testserver") as paired:
        code = client_gateway.pairing_details()["code"]
        assert paired.post("/api/pair", json={"code": code}).status_code == 200
        paired_pid = paired.post("/api/projects", json={"name": "正式配對"}).json()["id"]
        _set_mode(monkeypatch, client, "demo")
        # Persisted in the workspace row: a fresh read (restart) sees the same mode.
        client_gateway._access_cache = (0.0, client_gateway.ACCESS_DEFAULTS)
        with Session() as db:
            assert client_gateway.access_config(db)["mode"] == "demo"
        with TestClient(app, base_url="https://testserver") as alice, TestClient(app, base_url="https://testserver") as bob:
            assert alice.get("/api/projects").status_code == 401
            assert alice.get("/api/session/context").json()["demo_available"] is True
            assert alice.post("/api/session/demo").json()["reused"] is False
            assert alice.post("/api/session/demo").json()["reused"] is True  # refresh reuses
            assert bob.post("/api/session/demo").status_code == 200
            assert alice.get("/api/session/context").json()["session_kind"] == "DEMO"
            a = alice.post("/api/projects", json={"name": "Alice 展示"}).json()["id"]
            b = bob.post("/api/projects", json={"name": "Bob 展示"}).json()["id"]
            assert [p["id"] for p in alice.get("/api/projects").json()] == [a]
            assert [p["id"] for p in bob.get("/api/projects").json()] == [b]
            assert bob.get(f"/api/projects/{a}").status_code == 404
            assert bob.post(f"/api/projects/{a}/agent/continue").status_code == 404
            job = alice.post("/api/jobs", json={"kind": "RAG", "project_id": a, "query": "撥薪"})
            assert job.status_code == 202, job.text
            assert bob.get(f'/api/jobs/{job.json()["job_id"]}').status_code == 404
            assert bob.post(f'/api/jobs/{job.json()["job_id"]}/cancel').status_code == 404
            assert alice.get("/api/settings").status_code == 403
            assert alice.get("/api/admin/client-sessions").status_code == 403
            assert alice.get("/api/knowledge/servers").status_code == 403
            assert alice.put("/api/admin/client-access/mode", json={"mode": "demo"}).status_code == 403
            result = _set_mode(monkeypatch, client, "pairing")
            assert result["revoked_demo_sessions"] >= 2
            assert alice.get("/api/projects").status_code == 401
            assert bob.get(f"/api/projects/{b}").status_code == 401
            assert alice.get("/api/session/context").json()["authenticated"] is False
            assert alice.post("/api/session/demo").status_code == 403
        assert paired.get(f"/api/projects/{paired_pid}").status_code == 200


def test_demo_mode_is_lan_only_unless_host_opens_it(client, monkeypatch, restore_pairing):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    _set_mode(monkeypatch, client, "demo")
    assert client_gateway.demo_block_reason("192.168.1.20", "192.168.1.5") == ""
    assert "區域網路" in client_gateway.demo_block_reason("8.8.8.8", "testserver")
    monkeypatch.setattr(client_gateway, "tunnel_url", lambda: "https://demo.ngrok.example")
    # Tunnel traffic is public even if a forwarded address looks private.
    assert "區域網路" in client_gateway.demo_block_reason("127.0.0.1", "demo.ngrok.example")
    _set_mode(monkeypatch, client, "demo", "PUBLIC")
    assert client_gateway.demo_block_reason("8.8.8.8", "demo.ngrok.example") == ""


def test_demo_session_issue_is_rate_limited(client, monkeypatch, restore_pairing):
    monkeypatch.setattr(client_gateway, "IS_GATEWAY", True)
    _set_mode(monkeypatch, client, "demo")
    client_gateway.DEMO_ISSUES.clear()
    codes = []
    for _ in range(client_gateway.DEMO_ISSUE_LIMIT[0] + 1):
        with TestClient(app, base_url="https://testserver") as browser:
            codes.append(browser.post("/api/session/demo").status_code)
    assert codes[:-1] == [200] * client_gateway.DEMO_ISSUE_LIMIT[0]
    assert codes[-1] == 429
