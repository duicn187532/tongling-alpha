"""The Agent searches the shared library itself; users can add and remove evidence."""

import time

import pytest
from sqlalchemy import select

from app import knowledge_federation as federation
from app.db import KnowledgeServer, Link, Session
from app.evidence import ingest

HYBRID = {"mode": "HYBRID", "allow_remote_query": True, "server_enabled": True}


class FakeServer:
    searched = []

    def __init__(self, server):
        self.server = server

    def capabilities(self):
        return ["SEARCH", "FETCH_SNIPPET"]

    def search(self, query, limit, project_id=None):
        FakeServer.searched.append(query)
        if "撥薪" not in query:
            return []
        return [{"id": "PAYROLL-ENC", "title": "虛構_撥薪通知加密規範", "score": 0.8,
                 "snippet": "撥薪通知須加密", "uri": "company-file://payroll.md",
                 "metadata": {"version": "1.0", "authority_class": "UNVERIFIED_INTERNAL"}}]

    def fetch_evidence(self, evidence_id):
        assert evidence_id == "PAYROLL-ENC"
        return {"title": "虛構_撥薪通知加密規範", "uri": "company-file://payroll.md",
                "content": "前言。" * 200 + "撥薪轉帳通知必須以 AES-256 加密附件寄送，密碼另以簡訊提供。",
                "metadata": {"version": "1.0"}}


@pytest.fixture
def company_server(monkeypatch):
    FakeServer.searched = []
    monkeypatch.setattr(federation, "HttpKnowledgeServerProvider", FakeServer)
    with Session.begin() as db:
        for existing in db.scalars(select(KnowledgeServer)):
            existing.enabled = 0
        server = KnowledgeServer(name="公司檔案服務器", base_url="http://127.0.0.1:8765", enabled=1,
                                 capabilities=["SEARCH", "FETCH_SNIPPET"], cache_policy="SNIPPET_ONLY")
        db.add(server)
        db.flush()
        server_id = server.id
    yield server_id
    with Session.begin() as db:
        db.get(KnowledgeServer, server_id).enabled = 0


def run_agent(client, pid):
    job = client.post(f"/api/projects/{pid}/agent/continue")
    assert job.status_code == 200, job.text
    for _ in range(200):
        status = client.get("/api/jobs/" + job.json()["id"]).json()
        if status["status"] not in {"RUNNING", "QUEUED"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"Agent run did not finish: {status}")


def test_focus_queries_are_short_topic_phrases():
    queries = federation.focus_queries("企業網銀撥薪轉通知加密",
                                       "企業網銀完成撥薪轉帳後，系統必須寄送加密的薪資通知給員工，並保留發送紀錄。")
    assert queries[0] == "企業網銀撥薪轉通知加密"
    assert len(queries) <= 3 and all(len(q) <= 24 for q in queries)
    assert not any("必須" in q for q in queries)


def test_agent_searches_shared_library_and_imports_company_evidence(client, company_server):
    created = client.post("/api/projects", json={
        "name": "企業網銀撥薪轉通知加密",
        "description": "企業網銀完成撥薪轉帳後，必須寄送加密的薪資通知給員工。"}).json()
    pid = created["id"]
    assert client.patch(f"/api/projects/{pid}/knowledge-policy", json=HYBRID).status_code == 200
    with Session.begin() as db:
        shared = ingest(db, "撥薪通知作業規範", [{"text": "撥薪轉帳通知加密寄送給員工，通知須遮蔽帳號。"}],
                        kind="INTERNAL_POLICY")
        shared_id = shared.id
    status = run_agent(client, pid)
    assert status["status"] in {"SUCCEEDED", "WAITING"}, status
    # Remote receives short topic phrases, never the full requirement sentence.
    assert FakeServer.searched and all(len(q) <= 24 for q in FakeServer.searched)
    detail = client.get(f"/api/projects/{pid}").json()
    source_ids = {s["id"] for s in detail["sources"]}
    assert shared_id in source_ids
    imported = [s for s in detail["sources"] if s.get("provider") == "KNOWLEDGE_SERVER"]
    assert len(imported) == 1 and imported[0]["meta"]["excerpt_only"] is True
    assert "AES-256" in client.get(f"/api/sources/{imported[0]['id']}").json()["content"]
    assert detail["state"]["retrieval_queries"][0] == "企業網銀撥薪轉通知加密"


def test_manual_remote_attach_and_remove_evidence(client, company_server):
    pid = client.post("/api/projects", json={"name": "撥薪通知", "description": "撥薪通知必須加密。"}).json()["id"]
    assert client.patch(f"/api/projects/{pid}/knowledge-policy", json=HYBRID).status_code == 200
    first = client.post(f"/api/projects/{pid}/knowledge/attach-remote", json={
        "provider_id": company_server, "remote_evidence_id": "PAYROLL-ENC", "query": "撥薪 加密"})
    assert first.status_code == 200, first.text
    assert first.json()["added"] is True
    again = client.post(f"/api/projects/{pid}/knowledge/attach-remote", json={
        "provider_id": company_server, "remote_evidence_id": "PAYROLL-ENC"})
    assert again.json() == {**again.json(), "added": False, "source_id": first.json()["source_id"]}
    sid = first.json()["source_id"]
    removed = client.delete(f"/api/projects/{pid}/sources/{sid}")
    assert removed.status_code == 200, removed.text
    with Session() as db:
        assert db.get(Link, (pid, sid)) is None
    assert sid not in {s["id"] for s in client.get(f"/api/projects/{pid}").json()["sources"]}
    assert client.delete(f"/api/projects/{pid}/sources/{sid}").status_code == 404


def test_remote_attach_respects_project_policy_and_cache_policy(client, company_server):
    pid = client.post("/api/projects", json={"name": "本機專案"}).json()["id"]
    refused = client.post(f"/api/projects/{pid}/knowledge/attach-remote", json={
        "provider_id": company_server, "remote_evidence_id": "PAYROLL-ENC"})
    assert refused.status_code == 422 and "知識政策" in refused.json()["detail"]
    assert client.patch(f"/api/projects/{pid}/knowledge-policy", json=HYBRID).status_code == 200
    with Session.begin() as db:
        db.get(KnowledgeServer, company_server).cache_policy = "METADATA_ONLY"
    refused = client.post(f"/api/projects/{pid}/knowledge/attach-remote", json={
        "provider_id": company_server, "remote_evidence_id": "PAYROLL-ENC"})
    assert refused.status_code == 422 and "本機保留範圍" in refused.json()["detail"]


def test_removing_requirement_evidence_refreshes_requirements(client):
    pid = client.post("/api/projects", json={"name": "移除依據", "description": "登入需求：使用者必須輸入驗證碼。"}).json()["id"]
    status = run_agent(client, pid)
    assert status["status"] in {"SUCCEEDED", "WAITING"}, status
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["requirements"]
    sid = detail["sources"][0]["id"]
    assert client.delete(f"/api/projects/{pid}/sources/{sid}").status_code == 200
    after = client.get(f"/api/projects/{pid}").json()
    assert all(not any(e.get("source_id") == sid for e in r["evidence"]) for r in after["requirements"])
