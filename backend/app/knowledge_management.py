"""Host-only management surface for the local shared knowledge library."""

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import client_gateway
from .db import Chunk, GraphEdge, Link, Project, Source
from .evidence import mark_stale, tokens
from .intelligence import settings
from .retrieval import retrieve


class DocumentState(BaseModel):
    active: bool


class PreviewQuery(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)
    use_model_rerank: bool = False


def _host_only():
    if client_gateway.IS_GATEWAY:
        raise HTTPException(403, "知識庫管理只供彤靈主機使用")


def _global_document(db, sid):
    row = db.get(Source, sid)
    if not row or row.meta.get("library_scope") != "GLOBAL":
        raise HTTPException(404, "找不到共用文件")
    return row


def _document_summary(db, row):
    return {
        "id": row.id,
        "title": row.title,
        "kind": row.kind,
        "provider": row.provider,
        "status": row.status,
        "searchable": row.status == "ACTIVE" and row.kind != "MEETING_TRANSCRIPT",
        "created_at": row.created_at,
        "characters": len(row.content or ""),
        "chunks": db.scalar(select(func.count(Chunk.id)).where(Chunk.source_id == row.id)) or 0,
        "graph_edges": db.scalar(select(func.count(GraphEdge.id)).where(GraphEdge.source_id == row.id)) or 0,
        "linked_projects": db.scalar(select(func.count(Link.project_id)).where(Link.source_id == row.id)) or 0,
    }


def register(app, get_db):
    @app.get("/api/knowledge/documents")
    def documents(db=Depends(get_db)):
        _host_only()
        rows = db.scalars(select(Source).order_by(Source.created_at.desc())).all()
        return [_document_summary(db, row) for row in rows if row.meta.get("library_scope") == "GLOBAL"]

    @app.get("/api/knowledge/documents/{sid}")
    def document(sid: str, db=Depends(get_db)):
        _host_only()
        row = _global_document(db, sid)
        return {**_document_summary(db, row), "content_preview": (row.content or "")[:12000]}

    @app.patch("/api/knowledge/documents/{sid}")
    def set_document_state(sid: str, body: DocumentState, db=Depends(get_db)):
        _host_only()
        row = _global_document(db, sid)
        if body.active and row.status != "ARCHIVED":
            raise ValueError("只能恢復由知識庫管理停用的文件")
        if not body.active and row.status != "ACTIVE":
            raise ValueError("只能停用目前有效的共用文件")
        row.status = "ACTIVE" if body.active else "ARCHIVED"
        if not body.active:
            for link in db.scalars(select(Link).where(Link.source_id == sid)).all():
                project = db.get(Project, link.project_id)
                if project and project.status == "ACTIVE":
                    mark_stale(db, project, sid, f"共用知識已停用：{row.title}")
                    project.state = {**project.state, "retrieval_done": False}
                db.delete(link)
        return _document_summary(db, row)

    @app.post("/api/knowledge/search-preview")
    def search_preview(body: PreviewQuery, db=Depends(get_db)):
        _host_only()
        if not body.use_model_rerank:
            query_tokens = set(tokens(body.query))
            candidates = []
            for source in db.scalars(select(Source).where(Source.status == "ACTIVE")):
                if source.meta.get("library_scope") != "GLOBAL" or source.kind == "MEETING_TRANSCRIPT":
                    continue
                title_tokens = set(tokens(source.title))
                content_tokens = set(tokens(source.content or ""))
                overlap = query_tokens & (title_tokens | content_tokens)
                if not overlap:
                    continue
                coverage = len(overlap) / max(1, len(query_tokens))
                title_bonus = len(query_tokens & title_tokens) / max(1, len(query_tokens))
                candidates.append({"source_id": source.id, "title": source.title,
                                   "kind": source.kind, "text": (source.content or "")[:360],
                                   "keyword_overlap": round(coverage, 3),
                                   "preview_score": round(min(1.0, coverage * 0.8 + title_bonus * 0.2), 3),
                                   "retrieval_channels": ["KEYWORD_PREVIEW"],
                                   "evidence_role": "PREVIEW_ONLY",
                                   "case_relevance_reason": "快速詞彙預覽；並非完整檢索或證據判定"})
            candidates.sort(key=lambda item: (-item["preview_score"], item["title"]))
            return {"results": candidates[:body.top_k], "query": body.query,
                    "total": min(len(candidates), body.top_k), "top_k": body.top_k,
                    "mode": "FAST_LEXICAL"}
        config = {**settings(db), "top_k": body.top_k}
        strategy = ("hybrid_graph" if config.get("graph_retrieval_enabled") else "hybrid") + ("_reranker" if config.get("reranker_enabled") else "")
        results = retrieve(db, body.query, None, config, strategy=strategy)
        return {"results": results, "query": body.query,
                "total": len(results), "top_k": body.top_k,
                "mode": "FULL"}
