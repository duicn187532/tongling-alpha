from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from .db import (
    Project,
    Workspace,
    PRDTemplate,
    GraphNode,
    GraphEdge,
    KnowledgeJob,
    KnowledgeItem,
    ResearchRun,
    ProjectMemory,
    Revision,
    serialize,
    now,
)
from . import prd_templates, enrichment, peer_research, project_lifecycle
from .intelligence import settings, validate_endpoint
from .graph_store import SQLiteGraphStore, visible_sources, graph_search, key
from .model_runtime import TASK_ROLES


class TemplateInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sections: list[dict]


class JobInput(BaseModel):
    project_id: str | None = None
    source_ids: list[str] | None = None
    force: bool = False


class GraphQuery(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    project_id: str | None = None


class ResearchInput(BaseModel):
    topic: str = Field(min_length=1, max_length=180)


class PurgeInput(BaseModel):
    confirmation: str


class TagEdit(BaseModel):
    label: str = Field(default="", max_length=100)
    merge_into: str = ""
    reject: bool = False


def register(app, get_db, get):
    @app.get("/api/prd/templates")
    def templates(db=Depends(get_db)):
        return {
            "templates": prd_templates.builtins()
            + [
                {"id": r.id, "name": r.name, "built_in": False, **r.data}
                for r in db.scalars(select(PRDTemplate))
            ],
            "sections": [
                {
                    "key": k,
                    "title": v,
                    "internal_required": k in {"requirements", "rules", "traceability"},
                    "user_hide_allowed": True,
                }
                for k, v in prd_templates.SECTIONS.items()
            ],
            "selected": settings(db).get("prd_template", "FINANCIAL_STANDARD"),
        }

    @app.post("/api/prd/templates")
    def create_template(body: TemplateInput, db=Depends(get_db)):
        row = PRDTemplate(
            name=body.name, data=prd_templates.validate_template(body.model_dump())
        )
        db.add(row)
        db.flush()
        return {"id": row.id, "name": row.name, **row.data}

    @app.put("/api/prd/templates/{tid}")
    def update_template(tid: str, body: TemplateInput, db=Depends(get_db)):
        row = get(db, PRDTemplate, tid)
        row.name = body.name
        row.data = prd_templates.validate_template(body.model_dump())
        return {"id": row.id, "name": row.name, **row.data}

    @app.get("/api/prd-revisions/{rid}/projection")
    def projection(rid: str, db=Depends(get_db)):
        row = get(db, Revision, rid)
        return prd_templates.project(row.content, prd_templates.chosen(db), row.version)

    @app.put("/api/prd-revisions/{rid}/presentation")
    def draft_template(rid: str, db=Depends(get_db)):
        row = get(db, Revision, rid)
        if row.status not in {"DRAFT", "IN_REVIEW"}:
            raise ValueError("已定版模板不可覆寫，請使用另存投影")
        row.presentation = prd_templates.chosen(db)
        return prd_templates.project(row.content, row.presentation, row.version)

    @app.get("/api/prd-revisions/{rid}/export-template")
    def export_template(rid: str, db=Depends(get_db)):
        from types import SimpleNamespace
        from fastapi.responses import FileResponse
        from .lifecycle import render_docx, content_hash

        row = get(db, Revision, rid)
        template = prd_templates.chosen(db)
        exported = SimpleNamespace(
            id=row.id + "-template-" + content_hash(template)[:12],
            content=row.content,
            presentation=template,
            version=row.version,
            content_hash=row.content_hash,
            created_at=row.created_at,
            project_id=row.project_id,
        )
        return FileResponse(render_docx(exported), filename="PRD-template.docx")

    @app.get("/api/projects/trash")
    def trash_list(db=Depends(get_db)):
        from . import client_gateway
        return [
            {**serialize(p), "state": {k: v for k, v in (p.state or {}).items() if k != "client_owner_id"}} if client_gateway.CURRENT_CLIENT.get() else serialize(p)
            for p in db.scalars(
                select(Project)
                .where(Project.status == "TRASHED")
                .order_by(Project.deleted_at.desc())
            )
            if not client_gateway.CURRENT_CLIENT.get() or client_gateway.project_is_owned(p)
        ]

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str, db=Depends(get_db)):
        p = get(db, Project, pid)
        project_lifecycle.trash(db, p)
        return {"ok": True, "status": p.status}

    @app.post("/api/projects/{pid}/restore")
    def restore_project(pid: str, db=Depends(get_db)):
        from . import client_gateway
        p = db.get(Project, pid)
        if not p or (client_gateway.CURRENT_CLIENT.get() and not client_gateway.project_is_owned(p)):
            raise HTTPException(404, "專案不存在")
        project_lifecycle.restore(db, p)
        result = serialize(p)
        if client_gateway.CURRENT_CLIENT.get():
            result["state"].pop("client_owner_id", None)
        return result

    @app.delete("/api/projects/{pid}/permanent")
    def permanent(pid: str, body: PurgeInput, db=Depends(get_db)):
        from . import client_gateway
        if client_gateway.CURRENT_CLIENT.get():
            raise HTTPException(403, "永久刪除只供主機管理")
        p = db.get(Project, pid)
        if not p:
            raise HTTPException(404, "專案不存在")
        if body.confirmation != p.name:
            raise ValueError("請完整輸入專案名稱以確認永久刪除")
        return project_lifecycle.purge(db, p)

    @app.get("/api/settings/organization")
    def org(db=Depends(get_db)):
        return settings(db).get("organization", {})

    @app.patch("/api/settings/organization")
    def save_org(body: dict, db=Depends(get_db)):
        allowed = {
            "name",
            "industry",
            "sub_industry",
            "region",
            "peer_companies",
            "peer_keywords",
            "regulatory_keywords",
            "default_research_language",
        }
        if set(body) - allowed:
            raise ValueError("未知組織欄位")
        row = db.scalar(select(Workspace))
        organization = {
            **row.settings.get("organization", {}),
            **body,
            "updated_at": now(),
        }
        row.settings = {**row.settings, "organization": organization}
        return organization

    @app.get("/api/settings/models")
    def model_discovery(db=Depends(get_db)):
        import httpx
        from concurrent.futures import ThreadPoolExecutor

        config = settings(db)
        validate_endpoint(config)
        if config["provider"] == "mock":
            return {"models": [], "model_details": [], "text_models": [],
                    "embedding_models": [], "decision_models": [],
                    "tasks": TASK_ROLES, "message": "離線規則模式"}
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            if config["provider"] == "ollama":
                r = client.get(config["endpoint"].rstrip("/") + "/api/tags")
                r.raise_for_status()
                installed = r.json().get("models", [])
                models = [m["name"] for m in installed if m.get("name")]
                digests = {m["name"]: m.get("digest", "") for m in installed if m.get("name")}

                def describe(name):
                    try:
                        response = client.post(config["endpoint"].rstrip("/") + "/api/show",
                                               json={"model": name})
                        response.raise_for_status()
                        data = response.json()
                        capabilities = data.get("capabilities") or []
                        thinking = data.get("thinking") or {}
                        category = ("文字生成" if "completion" in capabilities else
                                    "向量嵌入" if "embedding" in capabilities else
                                    "Decision 判斷" if "decision" in capabilities else "其他／無法判定")
                        return {"name": name, "digest": digests.get(name, ""),
                                "capabilities": capabilities, "thinking": thinking.get("values", [])
                                if isinstance(thinking, dict) else [], "category": category,
                                "status": "READY" if category != "其他／無法判定" else "UNSUPPORTED",
                                "reason": "" if category != "其他／無法判定" else "未宣告文字、向量或 Decision 能力"}
                    except Exception as exc:
                        return {"name": name, "digest": digests.get(name, ""),
                                "capabilities": [], "thinking": [], "category": "無法查驗",
                                "status": "UNAVAILABLE", "reason": str(exc)[:160]}

                with ThreadPoolExecutor(max_workers=6) as pool:
                    details = list(pool.map(describe, models))
            else:
                import os

                k = os.getenv("OPENAI_API_KEY", "")
                r = client.get(
                    config["endpoint"].rstrip("/") + "/models",
                    headers={"Authorization": "Bearer " + k} if k else {},
                )
                r.raise_for_status()
                models = [m["id"] for m in r.json()["data"]]
                details = [{"name": name, "digest": "", "capabilities": ["completion"],
                            "thinking": [], "category": "文字生成", "status": "READY", "reason": ""}
                           for name in models]
        return {"models": models, "model_details": details,
                "text_models": [m["name"] for m in details if "completion" in m["capabilities"]],
                "embedding_models": [m["name"] for m in details if "embedding" in m["capabilities"]],
                "decision_models": [m["name"] for m in details if "decision" in m["capabilities"]],
                "tasks": TASK_ROLES}

    @app.get("/api/research/providers")
    def providers():
        return [
            peer_research.SearchProvider(p).health()
            for p in ["google_news", "tavily", "brave"]
        ]

    @app.post("/api/projects/{pid}/peer-research")
    def research(pid: str, body: ResearchInput, db=Depends(get_db)):
        return serialize(peer_research.start(db, get(db, Project, pid), body.topic))

    @app.get("/api/projects/{pid}/peer-research")
    def research_list(pid: str, db=Depends(get_db)):
        get(db, Project, pid)
        return [
            serialize(r)
            for r in db.scalars(
                select(ResearchRun)
                .where(ResearchRun.project_id == pid)
                .order_by(ResearchRun.created_at.desc())
            )
        ]

    @app.get("/api/projects/{pid}/peer-research/{rid}")
    def research_run(pid: str, rid: str, db=Depends(get_db)):
        get(db, Project, pid)
        r = get(db, ResearchRun, rid)
        if r.project_id != pid:
            raise HTTPException(404, "搜尋工作不存在")
        return serialize(r)

    @app.post("/api/knowledge/tag-jobs")
    def tag_job(body: JobInput, db=Depends(get_db)):
        return enrichment.status(
            db, enrichment.create(db, "TAG_GENERATION", body.project_id, body.source_ids, body.force)
        )

    @app.post("/api/knowledge/graph-jobs")
    def graph_job(body: JobInput, db=Depends(get_db)):
        return enrichment.status(
            db, enrichment.create(db, "GRAPH_BUILD", body.project_id, body.source_ids, body.force)
        )

    @app.get("/api/knowledge/build-sources")
    def build_sources(project_id: str | None = None, db=Depends(get_db)):
        if project_id:
            get(db, Project, project_id)
        return [{"id": s.id, "title": s.title, "kind": s.kind,
                 "sha256": s.sha256} for s in visible_sources(db, project_id)]

    @app.get("/api/knowledge/jobs")
    def jobs(db=Depends(get_db)):
        return [
            enrichment.status(db, j)
            for j in db.scalars(
                select(KnowledgeJob).order_by(KnowledgeJob.created_at.desc()).limit(15)
            )
        ]

    @app.get("/api/knowledge/jobs/{jid}")
    def job(jid: str, db=Depends(get_db)):
        return enrichment.status(db, get(db, KnowledgeJob, jid))

    @app.post("/api/knowledge/jobs/{jid}/{action}")
    def control_job(jid: str, action: str, db=Depends(get_db)):
        if action not in {"pause", "resume", "cancel", "retry"}:
            raise HTTPException(404, "未知操作")
        j = get(db, KnowledgeJob, jid)
        enrichment.control(db, j, action)
        return enrichment.status(db, j)

    @app.post("/api/knowledge/items/{iid}/retry")
    def retry_item(iid: str, db=Depends(get_db)):
        i = get(db, KnowledgeItem, iid)
        j = get(db, KnowledgeJob, i.job_id)
        if i.status != "FAILED" or j.status == "CANCELLED":
            raise ValueError("此項不可重試")
        i.status = "QUEUED"
        i.attempts = 0
        i.available_at = now()
        j.status = "QUEUED"
        return {"ok": True}

    @app.get("/api/knowledge/graph/status")
    def graph_status(project_id: str | None = None, db=Depends(get_db)):
        from . import client_gateway
        if client_gateway.CURRENT_CLIENT.get() and not project_id:
            raise HTTPException(403, "請指定自己的專案")
        if project_id:
            get(db, Project, project_id)
        ids = {s.id for s in visible_sources(db, project_id)}
        edges = list(db.scalars(select(GraphEdge).where(GraphEdge.source_id.in_(ids))))
        connected = {n for e in edges for n in [e.origin, e.target]}
        nodes = [n for n in db.scalars(select(GraphNode)) if n.id in connected]
        return {
            "nodes": len(nodes),
            "edges": len(edges),
            "documents_covered": len({e.source_id for e in edges}),
            "total_documents": len(ids),
            "versions": sorted({e.meta.get("graph_build_version", "") for e in edges}),
            "graph_nodes": [serialize(n) for n in nodes[:150]],
            "graph_edges": [serialize(e) for e in edges[:250]],
        }

    @app.post("/api/retrieval/graph/search")
    def graph_query(body: GraphQuery, db=Depends(get_db)):
        if body.project_id:
            get(db, Project, body.project_id)
        c = settings(db)
        return graph_search(
            db,
            body.query,
            body.project_id,
            c.get("graph_document_top_k", 3),
            c.get("graph_hop_limit", 2),
        )

    @app.patch("/api/knowledge/tags/{nid}")
    def edit_tag(nid: str, body: TagEdit, db=Depends(get_db)):
        n = get(db, GraphNode, nid)
        if n.kind != "TAG":
            raise ValueError("只可編輯標籤")
        if body.merge_into:
            target = get(db, GraphNode, body.merge_into)
            if target.kind != "TAG" or target.id == n.id:
                raise ValueError("合併目標無效")
            target.aliases = list(set(target.aliases + [n.label] + n.aliases))
            for e in db.scalars(
                select(GraphEdge).where(
                    (GraphEdge.origin == nid) | (GraphEdge.target == nid)
                )
            ).all():
                values = serialize(e)
                db.delete(e)
                db.flush()
                values["origin"] = (
                    target.id if values["origin"] == nid else values["origin"]
                )
                values["target"] = (
                    target.id if values["target"] == nid else values["target"]
                )
                values["id"] = key(
                    values["origin"],
                    values["relation"],
                    values["target"],
                    values["source_id"],
                    values["chunk_id"],
                )
                SQLiteGraphStore(db).upsert_edges([values])
            n.meta = {**n.meta, "status": "MERGED", "merged_into": target.id}
        elif body.reject:
            n.meta = {**n.meta, "status": "REJECTED"}
        elif body.label:
            n.aliases = list(set(n.aliases + [n.label]))
            n.label = body.label
            n.meta = {**n.meta, "source": "USER"}
        return serialize(n)

    @app.get("/api/projects/{pid}/memory")
    def memories(pid: str, db=Depends(get_db)):
        get(db, Project, pid)
        return [
            serialize(m)
            for m in db.scalars(
                select(ProjectMemory).where(ProjectMemory.project_id == pid)
            )
        ]
