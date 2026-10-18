"""PM edits and gap filling on an in-review PRD draft.

Edits are written into the draft and remembered on the project, so a later
draft built from new decisions keeps what the PM already wrote.
"""

import re

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import Project, Requirement, Revision, log, now

# field -> (PRD section title, the one question that fills it)
GAP_FIELDS = {
    "success_metrics": ("成功指標", "上線後用什麼指標判斷成功？例如寄送成功率、客訴件數、處理時間。"),
    "actors": ("使用者、角色與權限", "哪些角色會使用或受影響？各自可以做什麼、不能做什麼？"),
    "business_rules": ("商業規則", "有哪些必須遵守的業務規則？例如條件、限制、例外處理。"),
    "risk_compliance_security": ("風險、法遵與資安", "有哪些法遵、資安或個資要求？"),
    "data_impact": ("資料影響", "會新增、保存或異動哪些資料？需要保存多久？"),
    "nonfunctional_requirements": ("非功能需求", "有哪些效能、時效、可用性或容量要求？"),
    "dependencies": ("相依與假設", "有哪些前提假設或外部相依？例如其他系統、單位或時程。"),
    "out_of_scope": ("非範圍", "本次明確不做哪些事？"),
}
TEXT_FIELDS = {"background": "背景", "goals": "目標"}
PRIORITIES = {"高", "中", "低", "待確認"}


class SectionInput(BaseModel):
    text: str = Field(default="", max_length=6000)
    items: list[str] = Field(default_factory=list, max_length=40)


class AnswerInput(BaseModel):
    answer: str = Field(min_length=1, max_length=3000)


class RequirementEditInput(BaseModel):
    statement: str | None = Field(default=None, max_length=2000)
    priority: str | None = None
    actor: str | None = Field(default=None, max_length=120)


class AdviceInput(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=2000)


class SectionItems(BaseModel):
    items: list[str] = Field(min_length=1, max_length=12)


STRING_FIELDS = {"actors", "out_of_scope"}  # plain string lists in PRDModel


def _pm_items(items, field=""):
    clean = [item.strip() for item in items if item and item.strip()]
    if field in STRING_FIELDS:
        return clean
    return [{"description": item, "origin": "PM"} for item in clean]


def _has_content(content, field):
    value = content.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    return any((x.get("description") or x.get("statement")) if isinstance(x, dict) else str(x).strip()
               for x in value or [])


def gaps(content):
    return [{"field": field, "title": title, "question": question,
             "filled": _has_content(content, field),
             "items": [x.get("description") or x.get("statement") if isinstance(x, dict) else str(x)
                       for x in content.get(field) or []]}
            for field, (title, question) in GAP_FIELDS.items()]


def apply_edits(content, edits):
    """Replay the PM's saved section edits onto freshly built draft content."""
    for field, value in (edits or {}).items():
        if field == "acceptance_extra":
            have = {x.get("description") for x in content.get("acceptance_criteria", []) if isinstance(x, dict)}
            content["acceptance_criteria"] = content.get("acceptance_criteria", []) + [
                {"description": text, "status": "TO_CONFIRM", "origin": "PM"} for text in value if text not in have]
        elif field in TEXT_FIELDS:
            content[field] = value
        elif field in GAP_FIELDS:
            content[field] = _pm_items(value, field)
    return content


def _draft(db, get, rid):
    revision = get(db, Revision, rid)
    if revision.status != "IN_REVIEW":
        raise HTTPException(409, "只有待審閱的草稿可以編修；已定版版本請走變更流程")
    project = get(db, Project, revision.project_id)
    if project.state.get("draft_id") != revision.id:
        raise HTTPException(409, "此草稿已被較新的版本取代，請開啟最新草稿")
    return revision, project


def _save(db, revision, project, content, field, value, message):
    from .lifecycle import PRDModel, content_hash
    content = PRDModel.model_validate(content).model_dump()
    revision.content = content
    revision.content_hash = content_hash(content)
    edits = dict(project.state.get("prd_edits") or {})
    if field is not None:
        edits[field] = value
    project.state = {**project.state, "prd_edits": edits}
    log(db, project.id, "PRD_EDIT", message, {"revision_id": revision.id, "field": field})


def organize_answer(config, title, question, answer, project_id):
    from .model_runtime import structured
    fallback = {"items": [part.strip(" 。") for part in re.split(r"[\n；;]+", answer) if part.strip(" 。")][:12]
                or [answer.strip()]}
    try:
        result = structured(
            {**config, "_project_id": project_id}, "PRD_ASSIST",
            f"你是金融業資深 PM。把 PM 對「{title}」的回答整理成 PRD 條列，每條一句、具體可驗證、繁體中文。"
            "只能使用回答中的資訊，不得新增系統、數值、核准或法規；回答沒說的就不要寫。只回傳 items。",
            {"section": title, "question": question, "pm_answer": answer},
            SectionItems, lambda: fallback, timeout_seconds=90, max_tokens=800)
        return [item.strip() for item in result.items if item.strip()] or fallback["items"]
    except Exception:
        return fallback["items"]


def register(app, get_db, get):
    from .intelligence import settings

    def projected(revision, db):
        from .main import public_revision
        return public_revision(revision)

    @app.get("/api/prd-revisions/{rid}/gaps")
    def read_gaps(rid: str, db=Depends(get_db)):
        revision = get(db, Revision, rid)
        return {"gaps": gaps(revision.content),
                "requirements": [{"id": r["id"], "topic": r["topic"], "priority": r.get("priority", "待確認"),
                                  "actor": r.get("actor", "待確認")}
                                 for r in revision.content.get("requirements", [])]}

    @app.put("/api/prd-revisions/{rid}/sections/{field}")
    def save_section(rid: str, field: str, body: SectionInput, db=Depends(get_db)):
        if field not in GAP_FIELDS and field not in TEXT_FIELDS:
            raise HTTPException(404, "此章節不開放編修")
        revision, project = _draft(db, get, rid)
        content = dict(revision.content)
        if field in TEXT_FIELDS:
            value = body.text.strip()
            content[field] = value
        else:
            value = [item.strip() for item in body.items if item.strip()]
            content[field] = _pm_items(value, field)
        title = TEXT_FIELDS.get(field) or GAP_FIELDS[field][0]
        _save(db, revision, project, content, field, value, f"PM 編修 PRD：{title}")
        return projected(revision, db)

    @app.post("/api/prd-revisions/{rid}/gaps/{field}/answer")
    def answer_gap(rid: str, field: str, body: AnswerInput, db=Depends(get_db)):
        if field not in GAP_FIELDS:
            raise HTTPException(404, "此章節不開放補齊")
        title, question = GAP_FIELDS[field]
        with_project = get(db, Revision, rid)
        config = settings(db)
        # The model call runs before any write so no SQLite lock is held.
        items = organize_answer(config, title, question, body.answer, with_project.project_id)
        revision, project = _draft(db, get, rid)
        content = dict(revision.content)
        existing = list((project.state.get("prd_edits") or {}).get(field) or [])
        value = existing + [item for item in items if item not in existing]
        content[field] = _pm_items(value, field)
        _save(db, revision, project, content, field, value, f"PM 補齊 PRD：{title}")
        return {"items": items, "revision": projected(revision, db)}

    @app.post("/api/prd-revisions/{rid}/advice/apply")
    def apply_advice(rid: str, body: AdviceInput, db=Depends(get_db)):
        """Adopt one AI review suggestion into the matching PRD section."""
        if body.kind != "acceptance_criteria" and body.kind not in GAP_FIELDS:
            raise HTTPException(422, "這項建議沒有對應的 PRD 章節，請手動編修")
        revision, project = _draft(db, get, rid)
        content = dict(revision.content)
        text = body.text.strip()
        edits = project.state.get("prd_edits") or {}
        if body.kind == "acceptance_criteria":
            field = "acceptance_extra"
            value = list(edits.get(field) or [])
            if text not in value:
                value.append(text)
            content = apply_edits(content, {field: [text]})
            title = "驗收條件"
        else:
            field = body.kind
            current = [x.get("description") or x.get("statement") if isinstance(x, dict) else str(x)
                       for x in content.get(field) or []]
            value = list(edits.get(field) or current)
            if text not in value:
                value.append(text)
            content[field] = _pm_items(value, field)
            title = GAP_FIELDS[field][0]
        _save(db, revision, project, content, field, value, f"採用 AI 建議：{title}")
        return projected(revision, db)

    @app.put("/api/prd-revisions/{rid}/requirements/{req_id}")
    def edit_requirement(rid: str, req_id: str, body: RequirementEditInput, db=Depends(get_db)):
        revision, project = _draft(db, get, rid)
        if body.priority is not None and body.priority not in PRIORITIES:
            raise HTTPException(422, "優先級須為 高、中、低 或 待確認")
        requirement = get(db, Requirement, req_id)
        if requirement.project_id != project.id:
            raise HTTPException(404, "找不到需求")
        content = dict(revision.content)
        rows = [dict(r) for r in content.get("requirements", [])]
        row = next((r for r in rows if r["id"] == req_id), None)
        if row is None:
            raise HTTPException(404, "此需求不在這份草稿")
        decision = dict(requirement.decision or {})
        if body.statement is not None and body.statement.strip() and body.statement.strip() != row["statement"]:
            row["statement"] = body.statement.strip()
            decision.update({"action": "MODIFY", "statement": row["statement"], "decided_at": now(),
                             "rationale": decision.get("rationale") or "PM 於 PRD 預覽中修改文字"})
        for key in ("priority", "actor"):
            value = getattr(body, key)
            if value is not None:
                row[key] = value.strip() or "待確認"
                decision[key] = row[key]
        requirement.decision = decision
        row["decision"] = decision
        content["requirements"] = rows
        # Requirement wording lives in the decision; no section edit to replay.
        _save(db, revision, project, content, None, None, f"PM 編修需求：{row['topic']}")
        return projected(revision, db)
