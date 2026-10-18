"""Workspace and project knowledge policies, and configurable server connectors."""

import os
import re

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import KnowledgeServer, Project, RemoteQueryAudit, Workspace
from . import client_gateway
from .intelligence import settings
from .knowledge_federation import (
    CACHE_POLICIES, HttpKnowledgeServerProvider, policy_for,
    public_server, validate_policy, validate_server_url,
)


class KnowledgePolicyInput(BaseModel):
    mode: str = "LOCAL_ONLY"
    scopes: list[str] = Field(default_factory=list)
    allow_remote_query: bool = False
    allow_project_name_in_query: bool = False
    allow_requirement_text_in_query: bool = False
    allow_local_document_excerpt_in_query: bool = False
    external_research: bool = False
    server_enabled: bool = True


class ServerInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(min_length=8, max_length=500)
    enabled: bool = False
    auth_env_var: str = Field(default="", max_length=100)
    timeout_seconds: int = Field(default=10, ge=2, le=120)
    tls_verify: bool = True
    cache_policy: str = "METADATA_ONLY"


class SearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    project_id: str


class ServerSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)


def search_project(db, query, project_id):
    """Same retrieval path for the existing API and durable background jobs."""
    from .retrieval import retrieve
    project = db.get(Project, project_id)
    if not project or project.status != "ACTIVE":
        raise HTTPException(404, "找不到專案")
    policy = policy_for(db, project_id, settings(db))
    before = set(db.scalars(select(RemoteQueryAudit.id).where(RemoteQueryAudit.project_id == project_id)))
    results = retrieve(db, query, project_id, settings(db))
    events = list(db.scalars(select(RemoteQueryAudit).where(RemoteQueryAudit.project_id == project_id).order_by(RemoteQueryAudit.created_at.desc()).limit(10)))
    warnings = [e.detail for e in events if e.id not in before and e.status == "DEGRADED" and e.detail]
    if "SERVER_SHARED" in policy["scopes"] and not policy["allow_remote_query"]:
        warnings.append("遠端查詢尚未允許；只使用本機知識。")
    elif "SERVER_SHARED" in policy["scopes"] and not policy["server_enabled"]:
        warnings.append("此專案已停用知識伺服器；只使用本機可用來源。")
    elif "SERVER_SHARED" in policy["scopes"] and policy["allow_remote_query"] and policy["server_enabled"]:
        if not db.scalar(select(KnowledgeServer.id).where(KnowledgeServer.enabled == 1)):
            warnings.append("尚未啟用知識伺服器；目前只使用本機可用來源。")
    return {"results": results, "knowledge_mode": policy["mode"],
            "local_count": sum(r["source_location"] == "LOCAL" for r in results),
            "remote_count": sum(r["source_location"] == "REMOTE" for r in results),
            "warnings": warnings}


def register(app, get_db, get):
    @app.get("/api/settings/knowledge")
    def read_policy(db=Depends(get_db)):
        return validate_policy(settings(db).get("knowledge_policy") or {})

    @app.patch("/api/settings/knowledge")
    def save_policy(body: KnowledgePolicyInput, db=Depends(get_db)):
        row = db.scalar(select(Workspace))
        policy = validate_policy(body.model_dump())
        row.settings = {**row.settings, "knowledge_policy": policy}
        return policy

    @app.get("/api/projects/{pid}/knowledge-policy")
    def project_policy(pid: str, db=Depends(get_db)):
        project = get(db, Project, pid)
        override = (project.state or {}).get("knowledge_policy")
        return {"use_workspace_default": override is None,
                "effective": policy_for(db, pid, settings(db)), "override": override}

    @app.patch("/api/projects/{pid}/knowledge-policy")
    def save_project_policy(pid: str, body: KnowledgePolicyInput | None = None, db=Depends(get_db)):
        project = get(db, Project, pid)
        state = dict(project.state or {})
        if body is None:
            state.pop("knowledge_policy", None)
        else:
            state["knowledge_policy"] = validate_policy(body.model_dump())
        project.state = state
        return {"use_workspace_default": body is None,
                "effective": policy_for(db, pid, settings(db)),
                "override": state.get("knowledge_policy")}

    @app.get("/api/knowledge/servers")
    def servers(db=Depends(get_db)):
        return [public_server(s) for s in db.scalars(select(KnowledgeServer).order_by(KnowledgeServer.created_at))]

    def _assign(server, body):
        server.name = body.name.strip()
        server.base_url = validate_server_url(body.base_url)
        if body.auth_env_var and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", body.auth_env_var):
            raise ValueError("金鑰環境變數名稱格式不正確")
        if body.cache_policy not in CACHE_POLICIES:
            raise ValueError("未知的快取政策")
        server.enabled = int(body.enabled)
        server.auth_env_var = body.auth_env_var
        server.timeout_seconds = body.timeout_seconds
        server.tls_verify = int(body.tls_verify)
        server.cache_policy = body.cache_policy

    @app.post("/api/knowledge/servers")
    def create_server(body: ServerInput, db=Depends(get_db)):
        server = KnowledgeServer()
        _assign(server, body)
        db.add(server)
        db.flush()
        return public_server(server)

    @app.patch("/api/knowledge/servers/{sid}")
    def update_server(sid: str, body: ServerInput, db=Depends(get_db)):
        server = get(db, KnowledgeServer, sid)
        _assign(server, body)
        return public_server(server)

    @app.delete("/api/knowledge/servers/{sid}")
    def delete_server(sid: str, db=Depends(get_db)):
        server = get(db, KnowledgeServer, sid)
        server.enabled = 0
        return {"ok": True, "enabled": False}

    @app.post("/api/knowledge/servers/{sid}/test")
    def test_server(sid: str, db=Depends(get_db)):
        server = get(db, KnowledgeServer, sid)
        try:
            provider = HttpKnowledgeServerProvider(server)
            health = provider.health()
            capabilities = provider.capabilities()
            server.capabilities = capabilities
            return {"status": "CONNECTED", "health": health, "capabilities": capabilities}
        except Exception as exc:
            return {"status": "UNAVAILABLE", "detail": str(exc)[:200], "capabilities": []}

    @app.get("/api/knowledge/servers/{sid}/documents")
    def server_documents(sid: str, db=Depends(get_db)):
        if client_gateway.IS_GATEWAY:
            raise HTTPException(403, "只供主機管理")
        server = get(db, KnowledgeServer, sid)
        return HttpKnowledgeServerProvider(server).list_documents()

    @app.post("/api/knowledge/servers/{sid}/preview")
    def server_preview(sid: str, body: ServerSearchInput, db=Depends(get_db)):
        if client_gateway.IS_GATEWAY:
            raise HTTPException(403, "只供主機管理")
        server = get(db, KnowledgeServer, sid)
        return {"results": HttpKnowledgeServerProvider(server).search(body.query, body.top_k)}

    @app.post("/api/knowledge/servers/{sid}/rescan")
    def server_rescan(sid: str, db=Depends(get_db)):
        if client_gateway.IS_GATEWAY:
            raise HTTPException(403, "只供主機管理")
        server = get(db, KnowledgeServer, sid)
        return HttpKnowledgeServerProvider(server).rescan()

    @app.get("/api/knowledge/remote-audit")
    def audit(project_id: str | None = None, db=Depends(get_db)):
        query = select(RemoteQueryAudit).order_by(RemoteQueryAudit.created_at.desc()).limit(50)
        if project_id:
            get(db, Project, project_id)
            query = query.where(RemoteQueryAudit.project_id == project_id)
        return [{"created_at": row.created_at, "server_id": row.server_id,
                 "query_hash": row.query_hash, "status": row.status,
                 "result_count": row.result_count, "detail": row.detail}
                for row in db.scalars(query)]

    @app.post("/api/retrieval/search")
    def search(body: SearchInput, db=Depends(get_db)):
        get(db, Project, body.project_id)
        return search_project(db, body.query, body.project_id)
