from pathlib import Path
import shutil
from sqlalchemy import select, delete
from .db import (
    Link,
    Source,
    Chunk,
    Assertion,
    Requirement,
    Meeting,
    Revision,
    TechnicalReview,
    DeliveryPackage,
    Change,
    Run,
    Activity,
    Interruption,
    Event,
    Connector,
    KnowledgeJob,
    KnowledgeItem,
    GraphEdge,
    GraphNode,
    ProjectMemory,
    ResearchRun,
    DATA,
    now,
)
from .activity import cancel_project
from . import enrichment


def trash(db, p, reason=""):
    if p.status != "ACTIVE":
        raise ValueError("專案已在垃圾桶")
    cancel_project(db, p.id)
    p.status = "TRASHED"
    p.deleted_at = now()
    p.deleted_by = "LOCAL_USER"
    p.delete_reason = reason
    p.runtime = "PAUSED"
    p.epoch += 1
    for j in db.scalars(select(KnowledgeJob).where(KnowledgeJob.project_id == p.id)):
        enrichment.control(db, j, "cancel")
    for a in db.scalars(
        select(Activity).where(
            Activity.project_id == p.id, Activity.status.in_(["QUEUED", "RUNNING"])
        )
    ):
        a.status = "CANCELLED"
        a.finished_at = now()
    for r in db.scalars(
        select(ResearchRun).where(
            ResearchRun.project_id == p.id,
            ResearchRun.status.in_(["QUEUED", "RUNNING"]),
        )
    ):
        r.status = "CANCELLED"


def restore(db, p):
    if p.status != "TRASHED":
        raise ValueError("專案不在垃圾桶")
    p.status = "ACTIVE"
    p.deleted_at = None
    p.deleted_by = ""
    p.delete_reason = ""
    p.runtime = "PAUSED"


def purge(db, p):
    if p.status != "TRASHED":
        raise ValueError("必須先移至垃圾桶")
    p.status = "PURGING"
    db.flush()
    ids = list(db.scalars(select(Link.source_id).where(Link.project_id == p.id)))
    owned = [
        s
        for sid in ids
        if (s := db.get(Source, sid))
        and s.meta.get("library_scope") != "GLOBAL"
        and not db.scalar(
            select(Link).where(Link.source_id == sid, Link.project_id != p.id)
        )
    ]
    exports = list(db.scalars(select(Revision.id).where(Revision.project_id == p.id)))
    if exports:
        db.execute(delete(TechnicalReview).where(TechnicalReview.revision_id.in_(exports)))
        db.execute(delete(DeliveryPackage).where(DeliveryPackage.revision_id.in_(exports)))
    for job in db.scalars(
        select(KnowledgeJob).where(KnowledgeJob.project_id == p.id)
    ).all():
        db.execute(delete(KnowledgeItem).where(KnowledgeItem.job_id == job.id))
        db.delete(job)
    for model in [
        ResearchRun,
        ProjectMemory,
        Activity,
        Run,
        Event,
        Interruption,
        Change,
        Requirement,
        Meeting,
        Assertion,
        Connector,
        Link,
    ]:
        db.execute(delete(model).where(model.project_id == p.id))
    db.execute(delete(Revision).where(Revision.project_id == p.id))
    files = []
    for s in owned:
        db.execute(delete(GraphEdge).where(GraphEdge.source_id == s.id))
        db.execute(delete(KnowledgeItem).where(KnowledgeItem.source_id == s.id))
        for c in db.scalars(select(Chunk).where(Chunk.source_id == s.id)):
            db.delete(c)
        if s.meta.get("object_id"):
            files.append(DATA / "uploads" / s.meta["object_id"])
        db.delete(s)
    db.flush()
    used = set(db.scalars(select(GraphEdge.origin))) | set(
        db.scalars(select(GraphEdge.target))
    )
    for node in db.scalars(select(GraphNode)):
        if node.id not in used:
            db.delete(node)
    db.delete(p)
    db.commit()
    failures = []
    export_paths = [
        path
        for path in (DATA / "exports").glob("*")
        if any(
            path.name == rid or path.name.startswith(rid + "-template-")
            for rid in exports
        )
    ]
    for candidate in export_paths + files:
        path = Path(candidate).resolve()
        if not path.is_relative_to(DATA.resolve()):
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            failures.append({"path": str(path), "error": str(exc)})
    return {"ok": True, "file_cleanup_errors": failures}


def memory_for_decision(db, requirement):
    for item in db.scalars(
        select(ProjectMemory).where(
            ProjectMemory.project_id == requirement.project_id,
            ProjectMemory.canonical_ref == requirement.id,
            ProjectMemory.status == "ACTIVE",
        )
    ):
        item.status = "SUPERSEDED"
    db.add(
        ProjectMemory(
            project_id=requirement.project_id,
            topic_key=requirement.topic,
            canonical_ref=requirement.id,
            data={
                "memory_type": "DECISION_INDEX",
                "summary": requirement.decision.get("statement", requirement.statement),
                "source_refs": sorted({f["source_id"] for f in requirement.evidence}),
                "authority": "HUMAN_DECISION",
                "effective_at": now(),
                "action": requirement.decision.get("action"),
            },
        )
    )
