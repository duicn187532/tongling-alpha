"""Change-centric analysis acceptance cases; documents here are test-only fixtures."""

from sqlalchemy import select

from app.db import Session, Project, Requirement
from app.evidence import ingest
from app.intelligence import apply_analysis


def claim(topic, statement, *, value="", condition="", kind="REQUIREMENT"):
    return {"topic": topic, "statement": statement, "kind": kind,
            "chunk_ids": ["fixture-chunk"], "confidence": 1,
            "subject": topic, "condition": condition, "value": value}


def test_requested_change_is_delta_and_technical_unknown(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        old = ingest(db, "已核准驗證規範", [{"text": "現行採實體憑證"}], kind="INTERNAL_POLICY", project=p)
        goal = ingest(db, "本次需求", [{"text": "改用推播驗證"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {old.id: [claim("登入驗證", "現行採實體憑證")],
                               goal.id: [claim("登入驗證", "改用推播驗證", kind="CHANGE")]})
        row = db.scalar(select(Requirement).where(Requirement.project_id == p.id, Requirement.topic == "登入驗證"))
        report = p.state["change_analysis"]
        proposal = report["proposals"][0]
        assert row.status != "CONFLICT"
        assert proposal["status"] == "MODIFY"
        assert proposal["current_state"] == "現行採實體憑證"
        assert proposal["target_state"] == "改用推播驗證"
        assert proposal["impact_analysis"]["technical"][0]["status"] == "SA_CONFIRMATION_REQUIRED"
        assert report["critical_questions"][0]["reason"] == "HIGH_RISK_CHANGE"
        fingerprint = report["fingerprint"]
        rid = row.id
    assert client.post(f"/api/projects/{project['id']}/change-analysis/review",
                       json={"action": "APPROVE", "fingerprint": fingerprint}).status_code == 422
    decision = client.put(f"/api/requirements/{rid}/decision", json={
        "action": "MODIFY", "statement": "登入驗證改採推播方式", "rationale": "PM 同意納入草稿，技術仍待 SA 確認"})
    assert decision.status_code == 200, decision.text
    reviewed = client.post(f"/api/projects/{project['id']}/change-analysis/review",
                           json={"action": "APPROVE", "fingerprint": fingerprint})
    assert reviewed.status_code == 200, reviewed.text
    draft = client.post(f"/api/projects/{project['id']}/prd/revisions")
    assert draft.status_code == 200, draft.text
    content = draft.json()["content"]
    assert content["change_scope"][0]["action"] == "MODIFY"
    assert content["it_impact"][0]["status"] == "OPEN"
    assert content["change_analysis"]["proposals"][0]["status"] == "MODIFY"


def test_true_conflict_requires_choice_and_conditions_do_not(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        a = ingest(db, "限額規範甲", [{"text": "一般戶限額五萬"}], kind="INTERNAL_POLICY", project=p)
        b = ingest(db, "限額規範乙", [{"text": "一般戶限額十萬"}], kind="INTERNAL_POLICY", project=p)
        c = ingest(db, "VIP 規範", [{"text": "VIP 限額二十萬"}], kind="INTERNAL_POLICY", project=p)
        goal = ingest(db, "本次目標", [{"text": "一般戶限額提高"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {a.id: [claim("每日限額", "一般戶限額五萬", value="50000", condition="一般戶")],
                               b.id: [claim("每日限額", "一般戶限額十萬", value="100000", condition="一般戶")],
                               c.id: [claim("每日限額", "VIP 限額二十萬", value="200000", condition="VIP")],
                               goal.id: [claim("每日限額", "一般戶限額提高", condition="一般戶")]})
        row = db.scalar(select(Requirement).where(Requirement.project_id == p.id, Requirement.topic == "每日限額"))
        assert row.status == "CONFLICT"
        assert p.state["change_analysis"]["proposals"][0]["status"] == "TRUE_CONFLICT"
        assert len(p.state["change_analysis"]["critical_questions"]) == 1
        fingerprint = p.state["change_analysis"]["fingerprint"]
    assert client.post(f"/api/projects/{project['id']}/change-analysis/review",
                       json={"action": "APPROVE", "fingerprint": fingerprint}).status_code == 422


def test_approved_supersession_and_supporting_evidence_preserve_decision(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        old = ingest(db, "舊版正式規範", [{"text": "舊規則"}], kind="INTERNAL_POLICY", project=p)
        new = ingest(db, "新版正式規範", [{"text": "新規則"}], kind="INTERNAL_POLICY", project=p)
        new.meta = {**(new.meta or {}), "approval_status": "APPROVED", "supersedes_source_ids": [old.id]}
        goal = ingest(db, "需求", [{"text": "採用新規則"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {old.id: [claim("流程規則", "舊規則")],
                               new.id: [claim("流程規則", "新規則", kind="CHANGE")],
                               goal.id: [claim("流程規則", "採用新規則")]})
        row = db.scalar(select(Requirement).where(Requirement.project_id == p.id, Requirement.topic == "流程規則"))
        assert row.status != "CONFLICT"
        assert any(f.get("validity") == "SUPERSEDED" and f["source_id"] == old.id for f in row.evidence)
        row.decision = {"action": "ACCEPT", "statement": row.statement, "rationale": "PM 已確認"}
        fingerprint = p.state["change_analysis"]["fingerprint"]
        p.state = {**p.state, "change_review": {"status": "APPROVED", "fingerprint": fingerprint}}
        support = ingest(db, "補充佐證", [{"text": "採用新規則"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {support.id: [claim("流程規則", "採用新規則")]})
        assert row.decision["action"] == "ACCEPT"
        assert p.state["change_analysis_unchanged"] is True
        assert p.state["change_review"]["fingerprint"] == fingerprint


def test_changed_evidence_reopens_only_affected_requirement(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        first = ingest(db, "需求甲", [{"text": "通知採簡訊"}], kind="REQUIREMENT_TEXT", project=p)
        second = ingest(db, "需求乙", [{"text": "保留紀錄"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {first.id: [claim("通知", "通知採簡訊")],
                               second.id: [claim("紀錄", "保留紀錄")]})
        rows = {r.topic: r for r in db.scalars(select(Requirement).where(Requirement.project_id == p.id))}
        for r in rows.values():
            r.decision = {"action": "ACCEPT", "statement": r.statement}
        old_fingerprint = p.state["change_analysis"]["fingerprint"]
        p.state = {**p.state, "change_review": {"status": "APPROVED", "fingerprint": old_fingerprint}}
        apply_analysis(db, p, {first.id: [claim("通知", "通知改為推播", kind="CHANGE")]})
        assert rows["通知"].decision == {}
        assert rows["紀錄"].decision["action"] == "ACCEPT"
        assert "change_review" not in p.state


def test_explicit_technical_non_support_is_not_invented_as_existing_api(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        spec = ingest(db, "SA 系統規格", [{"text": "目前不支援查詢 API"}], kind="TECH_SPEC", project=p)
        goal = ingest(db, "新需求", [{"text": "新增查詢 API"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {spec.id: [claim("查詢介面", "目前不支援查詢 API", kind="SYSTEM_CAPABILITY")],
                               goal.id: [claim("查詢介面", "新增查詢 API")]})
        item = p.state["change_analysis"]["proposals"][0]
        assert item["status"] == "UNSUPPORTED"
        assert item["impact_analysis"]["technical"][0]["source_id"] == spec.id
        assert p.state["change_analysis"]["critical_questions"][0]["reason"] == "TECHNICAL_UNSUPPORTED"
