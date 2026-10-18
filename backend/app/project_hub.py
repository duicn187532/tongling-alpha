"""Compact PM portfolio and personal to-dos; no external service is required."""

from collections import defaultdict
from datetime import date
from uuid import uuid4

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from . import client_gateway
from .db import Activity, Change, Interruption, Link, Meeting, Project, Requirement, Revision, Source, now


class TodoInput(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    due_date: date | None = None


class TodoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    due_date: date | None = None
    done: bool | None = None


def _by_project(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.project_id].append(row)
    return grouped


def register(app, get_db, get):
    @app.get("/api/dashboard")
    def dashboard(db=Depends(get_db)):
        projects = [p for p in db.scalars(select(Project).where(Project.status == "ACTIVE")
                    .order_by(Project.created_at.desc()))
                    if not client_gateway.CURRENT_CLIENT.get() or client_gateway.project_is_owned(p)]
        ids = [p.id for p in projects]
        if not ids:
            return {"projects": [], "totals": {"projects": 0, "running": 0, "attention": 0,
                    "baselined": 0, "open_todos": 0}}
        requirements = _by_project(db.scalars(select(Requirement).where(Requirement.project_id.in_(ids))))
        meetings = _by_project(db.scalars(select(Meeting).where(Meeting.project_id.in_(ids))))
        revisions = _by_project(db.scalars(select(Revision).where(Revision.project_id.in_(ids))))
        changes = _by_project(db.scalars(select(Change).where(Change.project_id.in_(ids))))
        interruptions = _by_project(db.scalars(select(Interruption).where(Interruption.project_id.in_(ids))))
        activities = _by_project(db.scalars(select(Activity).where(Activity.project_id.in_(ids))))
        links = _by_project(db.scalars(select(Link).where(Link.project_id.in_(ids))))
        source_ids = {link.source_id for project_links in links.values() for link in project_links}
        source_kinds = {source.id: source.kind for source in db.scalars(select(Source).where(Source.id.in_(source_ids)))} if source_ids else {}
        cards = []
        for p in projects:
            req = requirements[p.id]
            rev = revisions[p.id]
            report = (p.state or {}).get("change_analysis") or {}
            if report:
                from .change_analysis import unresolved
                pending_critical = len(unresolved(report, req))
                reviewed = (p.state or {}).get("change_review") or {}
                pending_decisions = pending_critical + int(bool(req) and
                    not (reviewed.get("status") == "APPROVED" and reviewed.get("fingerprint") == report.get("fingerprint")))
            else:
                pending_decisions = sum(not r.decision or r.decision.get("action") not in {"ACCEPT", "MODIFY", "REJECT"} for r in req)
            pending_meetings = sum(m.status != "CONFIRMED" for m in meetings[p.id])
            open_changes = sum(c.status in {"OPEN", "REVIEWED"} for c in changes[p.id])
            open_interruptions = [i for i in interruptions[p.id] if i.status == "OPEN"]
            active_jobs = [a for a in activities[p.id] if a.status in {"QUEUED", "RUNNING", "WAITING_RETRY"}]
            baselined = any(r.id == p.current_revision_id and r.status == "BASELINED" for r in rev)
            draft = bool(rev)
            has_evidence = any(source_kinds.get(link.source_id) not in {None, "REQUIREMENT_TEXT"} for link in links[p.id])
            milestones = [bool(p.description.strip()), has_evidence,
                          bool(req) and not pending_decisions, draft, baselined]
            suggestions = []
            if open_interruptions:
                suggestions.append({"title": open_interruptions[0].title, "step": 6, "kind": "HUMAN"})
            if pending_meetings:
                suggestions.append({"title": f"確認 {pending_meetings} 份會議紀錄", "step": 4, "kind": "MEETING"})
            if open_changes:
                suggestions.append({"title": f"處理 {open_changes} 項需求變更", "step": 9, "kind": "CHANGE"})
            if (p.state or {}).get("analysis_stale") and links[p.id]:
                suggestions.append({"title": "更新 AI 需求分析", "step": 5, "kind": "ANALYSIS"})
            if pending_decisions:
                suggestions.append({"title": f"審閱變更提案與 {pending_critical} 項關鍵問題" if report else f"確認 {pending_decisions} 項需求決策", "step": 6, "kind": "DECISION"})
            if not p.description.strip():
                suggestions.append({"title": "補上需求起點", "step": 1, "kind": "BRIEF"})
            elif not has_evidence:
                suggestions.append({"title": "檢索可用來源；若找不到，安排會議或列待補證據", "step": 2, "kind": "SOURCE"})
            elif not req and not (p.state or {}).get("analysis_stale"):
                suggestions.append({"title": "整理需求與待確認問題", "step": 5, "kind": "REQUIREMENT"})
            if req and not draft and not pending_decisions:
                suggestions.append({"title": "產生並審閱 PRD 草稿", "step": 8, "kind": "DRAFT"})
            elif draft and not baselined:
                suggestions.append({"title": "審閱 PRD 並決定是否建立業務基準", "step": 8, "kind": "BASELINE"})
            todos = sorted((p.state or {}).get("pm_todos") or [], key=lambda t: (t.get("done", False), t.get("due_date") or "9999-12-31", t.get("created_at", "")))
            if active_jobs or p.runtime == "RUNNING":
                status = "RUNNING"
            elif open_interruptions or pending_decisions or pending_meetings:
                status = "ATTENTION"
            elif baselined:
                status = "BASELINED"
            elif draft:
                status = "REVIEW"
            else:
                status = "PLANNING"
            cards.append({"id": p.id, "name": p.name, "description": p.description[:240],
                          "mode": p.mode, "runtime": p.runtime, "status": status,
                          "progress": sum(milestones) * 20, "milestones": milestones,
                          "revision": next((r.version for r in rev if r.id == p.current_revision_id),
                                           max(rev, key=lambda r: r.created_at).version if rev else None),
                          "counts": {"sources": len(links[p.id]),
                                     "evidence_sources": sum(source_kinds.get(link.source_id) not in {None, "REQUIREMENT_TEXT"} for link in links[p.id]),
                                     "requirements": len(req),
                                     "pending_decisions": pending_decisions, "pending_meetings": pending_meetings,
                                     "open_changes": open_changes},
                          "suggestions": suggestions[:3], "todos": todos,
                          "active_work": active_jobs[0].title if active_jobs else None,
                          "updated_at": max([p.created_at] + [r.created_at for r in rev]
                                            + [a.updated_at for a in activities[p.id]]
                                            + [m.created_at for m in meetings[p.id]])})
        return {"projects": cards, "totals": {
            "projects": len(cards), "running": sum(c["status"] == "RUNNING" for c in cards),
            "attention": sum(c["status"] == "ATTENTION" for c in cards),
            "baselined": sum(c["status"] == "BASELINED" for c in cards),
            "open_todos": sum(sum(not t.get("done") for t in c["todos"]) for c in cards)}}

    @app.post("/api/projects/{pid}/todos")
    def add_todo(pid: str, body: TodoInput, db=Depends(get_db)):
        db.execute(text("BEGIN IMMEDIATE"))
        project = get(db, Project, pid)
        todos = list((project.state or {}).get("pm_todos") or [])
        if len(todos) >= 200:
            raise ValueError("每個專案最多保留 200 項代辦")
        todo = {"id": str(uuid4()), "title": body.title.strip(), "due_date": str(body.due_date) if body.due_date else None,
                "done": False, "created_at": now()}
        if not todo["title"]:
            raise ValueError("請填寫代辦事項")
        project.state = {**(project.state or {}), "pm_todos": [*todos, todo]}
        return todo

    @app.patch("/api/projects/{pid}/todos/{tid}")
    def update_todo(pid: str, tid: str, body: TodoUpdate, db=Depends(get_db)):
        db.execute(text("BEGIN IMMEDIATE"))
        project = get(db, Project, pid)
        todos = list((project.state or {}).get("pm_todos") or [])
        todo = next((t for t in todos if t.get("id") == tid), None)
        if not todo:
            raise HTTPException(404, "找不到代辦")
        updated = {**todo}
        if body.title is not None:
            updated["title"] = body.title.strip()
            if not updated["title"]:
                raise ValueError("請填寫代辦事項")
        if "due_date" in body.model_fields_set:
            updated["due_date"] = str(body.due_date) if body.due_date else None
        if body.done is not None:
            updated["done"] = body.done
        project.state = {**(project.state or {}), "pm_todos": [updated if t.get("id") == tid else t for t in todos]}
        return updated

    @app.delete("/api/projects/{pid}/todos/{tid}")
    def delete_todo(pid: str, tid: str, db=Depends(get_db)):
        db.execute(text("BEGIN IMMEDIATE"))
        project = get(db, Project, pid)
        todos = list((project.state or {}).get("pm_todos") or [])
        if not any(t.get("id") == tid for t in todos):
            raise HTTPException(404, "找不到代辦")
        project.state = {**(project.state or {}), "pm_todos": [t for t in todos if t.get("id") != tid]}
        return {"ok": True}
