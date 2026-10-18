"""v7.1 policy, privacy, provenance and degraded-mode acceptance checks."""

import hashlib
from sqlalchemy import select

from app.db import Session, KnowledgeServer, RemoteQueryAudit, Project
from app.intelligence import settings
from app import knowledge_federation as federation
from app.evidence import ingest


def policy(mode, remote=False):
    return {"mode": mode, "allow_remote_query": remote}


def test_local_only_makes_zero_remote_requests(client, project, monkeypatch):
    calls = []
    class Provider:
        def __init__(self, server):
            calls.append(server.id)
    monkeypatch.setattr(federation, "HttpKnowledgeServerProvider", Provider)
    with Session() as db:
        for existing in db.scalars(select(KnowledgeServer)):
            existing.enabled = 0
        db.add(KnowledgeServer(name="server", base_url="https://example.com", enabled=1, capabilities=["SEARCH"]))
        db.commit()
        result = federation.retrieve(db, "OTP", project["id"], {**settings(db), "top_k": 3, "knowledge_policy": policy("LOCAL_ONLY", True)}, lambda *args: [])
        assert result == [] and calls == []


def test_server_only_provenance_and_privacy_audit(client, project, monkeypatch):
    sent = []
    class Provider:
        def __init__(self, server): self.server = server
        def search(self, query, limit, project_id):
            sent.append((query, limit))
            return [{"id": "KB-1", "title": "OTP 規範", "snippet": "秘密全文", "score": 0.9,
                     "uri": "https://example.com/KB-1", "metadata": {"authority_class": "INTERNAL_AUTHORITY", "verified_authority": True}}]
    monkeypatch.setattr(federation, "HttpKnowledgeServerProvider", Provider)
    with Session() as db:
        for existing in db.scalars(select(KnowledgeServer)):
            existing.enabled = 0
        db.add(KnowledgeServer(name="server", base_url="https://example.com", enabled=1, capabilities=["SEARCH"], cache_policy="METADATA_ONLY"))
        db.commit()
        hits = federation.retrieve(db, "OTP", project["id"], {**settings(db), "top_k": 3, "knowledge_policy": policy("SERVER_ONLY", True)}, lambda *args: [])
        db.commit()
        assert sent == [("OTP", 3)]
        assert hits[0]["source_scope"] == "SERVER_SHARED"
        assert hits[0]["text"] == "" and hits[0]["evidence_role"] == "SUPPORTING"
        row = db.scalar(select(RemoteQueryAudit).order_by(RemoteQueryAudit.created_at.desc()))
        assert row.query_hash == hashlib.sha256(b"OTP").hexdigest()
        assert "OTP" not in row.detail


def test_hybrid_degrades_to_local_and_records_failure(client, project, monkeypatch):
    class Provider:
        def __init__(self, server): self.server = server
        def search(self, query, limit, project_id): raise ValueError("server offline")
    monkeypatch.setattr(federation, "HttpKnowledgeServerProvider", Provider)
    with Session() as db:
        for existing in db.scalars(select(KnowledgeServer)):
            existing.enabled = 0
        db.add(KnowledgeServer(name="server", base_url="https://example.com", enabled=1, capabilities=["SEARCH"]))
        db.commit()
        source = ingest(db, "OTP 本機", [{"text": "OTP 登入驗證"}], project=db.get(Project, project["id"]))
        db.flush()
        local_hit = {"source_id": source.id, "title": source.title, "text": "OTP 登入驗證",
                     "evidence_role": "SUPPORTING", "retrieval_channels": ["BM25"]}
        hits = federation.retrieve(db, "OTP", project["id"], {**settings(db), "top_k": 3, "knowledge_policy": policy("HYBRID", True)}, lambda *args: [local_hit])
        db.commit()
        assert len(hits) == 1 and hits[0]["source_location"] == "LOCAL"
        assert db.scalar(select(RemoteQueryAudit).order_by(RemoteQueryAudit.created_at.desc())).status == "DEGRADED"


def test_server_settings_and_project_override(client, project):
    response = client.post("/api/knowledge/servers", json={"name": "Corp", "base_url": "https://example.com", "auth_env_var": "TONGLING_KB_TOKEN"})
    assert response.status_code == 200
    assert "credential_available" in response.json() and "token" not in response.json()
    response = client.patch(f"/api/projects/{project['id']}/knowledge-policy", json=policy("SERVER_ONLY", True))
    assert response.status_code == 200, response.text
    assert response.json()["effective"]["mode"] == "SERVER_ONLY"
    response = client.patch(f"/api/projects/{project['id']}/knowledge-policy")
    assert response.json()["use_workspace_default"] is True


def test_project_can_disable_server_without_changing_workspace(client, project, monkeypatch):
    calls = []
    class Provider:
        def __init__(self, server): calls.append(server.id)
    monkeypatch.setattr(federation, "HttpKnowledgeServerProvider", Provider)
    response = client.patch(f"/api/projects/{project['id']}/knowledge-policy", json={
        "mode": "HYBRID", "allow_remote_query": True, "server_enabled": False,
    })
    assert response.status_code == 200
    with Session() as db:
        federation.retrieve(db, "OTP", project["id"], {**settings(db), "top_k": 3}, lambda *args: [])
    assert calls == []


def test_rank_fusion_and_dedup_preserve_variants():
    local = {"source_id": "L", "source_location": "LOCAL", "source_scope": "LOCAL_PRIVATE",
             "meta": {"content_hash": "same"}, "evidence_role": "SUPPORTING", "score": 0.2}
    remote = {"source_id": "R", "source_location": "REMOTE", "source_scope": "SERVER_SHARED",
              "meta": {"content_hash": "same"}, "evidence_role": "SUPPORTING", "score": 0.99}
    result = federation.merge([local], [remote], 3)
    assert len(result) == 1 and result[0]["source_id"] == "L"
    assert result[0]["provenance_variants"][0]["source_id"] == "R"


def test_formal_current_evidence_precedes_high_raw_score_draft():
    draft = {"source_id": "draft", "source_location": "LOCAL", "evidence_role": "SUPPORTING",
             "authority_class": "UNVERIFIED_INTERNAL", "temporal_status": "CURRENT", "score": 0.99}
    formal = {"source_id": "formal", "source_location": "REMOTE", "evidence_role": "SUPPORTING",
              "authority_class": "INTERNAL_AUTHORITY", "temporal_status": "CURRENT", "score": 0.51}
    assert federation.merge([draft], [formal], 2)[0]["source_id"] == "formal"
