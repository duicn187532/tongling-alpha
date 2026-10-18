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
        old = ingest(db, "已核准驗證規範", [{"text": "現行採實體憑證並加密傳輸"}], kind="INTERNAL_POLICY", project=p)
        goal = ingest(db, "本次需求", [{"text": "改用推播驗證"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {old.id: [claim("登入驗證", "現行採實體憑證並加密傳輸")],
                               goal.id: [claim("登入驗證", "改用推播驗證", kind="CHANGE")]})
        row = db.scalar(select(Requirement).where(Requirement.project_id == p.id, Requirement.topic == "登入驗證"))
        report = p.state["change_analysis"]
        proposal = next(item for item in report["proposals"] if item["name"] == "登入驗證")
        assert row.status != "CONFLICT"
        assert proposal["status"] == "MODIFY"
        # Dropping an existing control (加密) is the risk that needs a decision.
        assert proposal["priority"] == "HIGH"
        assert proposal["current_state"] == "現行採實體憑證並加密傳輸"
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
    assert any(item["status"] == "MODIFY" for item in content["change_analysis"]["proposals"])


def test_report_converges_to_items_that_need_a_decision(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        policy = ingest(db, "通知規範", [{"text": "通知須遮蔽帳號；保存一年"}], kind="INTERNAL_POLICY", project=p)
        goal = ingest(db, "撥薪通知需求", [{"text": "通知改為加密附件"}], kind="REQUIREMENT_TEXT", project=p)
        dup = ingest(db, "會議補充", [{"text": "通知改為加密附件。"}], kind="MEETING_RECORD", project=p)
        db.flush()
        apply_analysis(db, p, {
            policy.id: [claim("帳號遮蔽", "通知須遮蔽帳號"), claim("保存期限", "通知紀錄保存一年"),
                        claim("通知格式", "現行以明文 email 通知")],
            goal.id: [claim("通知格式", "通知改為加密附件", kind="CHANGE")],
            dup.id: [claim("附件加密", "通知改為加密附件。", kind="CHANGE")]})
        report = p.state["change_analysis"]
        by_name = {item["name"]: item for item in report["proposals"]}
        # Current rules nobody asked to change are background, not proposals.
        assert by_name["帳號遮蔽"]["status"] == "CONTEXT" and not by_name["帳號遮蔽"]["needs_review"]
        assert by_name["保存期限"]["status"] == "CONTEXT"
        # Adding encryption strengthens a control: a normal proposal, not a forced question.
        assert by_name["通知格式"]["status"] == "MODIFY" and by_name["通知格式"]["priority"] == "NORMAL"
        assert by_name["附件加密"]["duplicate_of"] == by_name["通知格式"]["requirement_id"]
        assert report["critical_questions"] == []
        assert "1 項需要審閱" in report["summary"]
        fingerprint = report["fingerprint"]
        target = by_name["通知格式"]["requirement_id"]
    # Adopt or reject without a reason; modifying asks AI to word the note.
    assert client.put(f"/api/requirements/{target}/decision", json={"action": "REJECT"}).status_code == 200
    assert client.put(f"/api/requirements/{target}/decision", json={"action": "ACCEPT"}).status_code == 200
    rewritten = client.post(f"/api/requirements/{target}/rewrite", json={"note": "附件密碼以簡訊另送"})
    assert rewritten.status_code == 200 and "簡訊" in rewritten.json()["statement"]
    assert client.put(f"/api/requirements/{target}/decision", json={"action": "MODIFY"}).status_code == 422
    reviewed = client.post(f"/api/projects/{project['id']}/change-analysis/review",
                           json={"action": "APPROVE", "fingerprint": fingerprint})
    assert reviewed.status_code == 200, reviewed.text
    decisions = {r["topic"]: r["decision"] for r in reviewed.json()["requirements"]}
    assert decisions["帳號遮蔽"]["category"] == "DECISION_CONTEXT"
    assert decisions["附件加密"]["action"] == "REJECT"


def test_modified_requirement_regenerates_acceptance_criteria(client, project):
    stale = "系統觸發簡訊發送功能，通知員工薪資入帳"
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        policy = ingest(db, "撥薪通知規範", [{"text": "撥薪通知以 email 明文寄送"}], kind="INTERNAL_POLICY", project=p)
        goal = ingest(db, "撥薪轉通知加密", [{"text": "通知改為加密 PDF 附件並以簡訊通知"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {policy.id: [claim("撥薪通知", "撥薪通知以 email 明文寄送")],
                               goal.id: [claim("撥薪通知", "通知改為加密 PDF 附件並以簡訊通知", kind="CHANGE")]})
        # Stand in for the model's enrichment, written before the PM's clarification.
        report = dict(p.state["change_analysis"])
        proposal = next(item for item in report["proposals"] if item["name"] == "撥薪通知")
        proposal["proposed_requirement"] = {"statement": "撥薪通知改以加密 PDF 附件寄送，並以簡訊通知員工",
                                            "acceptance_criteria": ["通知附件為加密 PDF", stale]}
        report["model_status"] = "COMPLETE"
        p.state = {**p.state, "change_analysis": report}
        fingerprint, rid = report["fingerprint"], proposal["requirement_id"]
    modified = "撥薪通知改以加密 PDF 附件寄送；簡訊僅傳送 PDF 開啟密碼"
    saved = client.put(f"/api/requirements/{rid}/decision", json={"action": "MODIFY", "statement": modified})
    assert saved.status_code == 200, saved.text
    with Session() as db:
        report = db.get(Project, project["id"]).state["change_analysis"]
    proposal = next(item for item in report["proposals"] if item["requirement_id"] == rid)
    criteria = proposal["proposed_requirement"]["acceptance_criteria"]
    assert stale not in criteria and any("簡訊僅傳送 PDF 開啟密碼" in c for c in criteria)
    assert saved.json()["decision"]["acceptance_criteria"] == criteria
    # Criteria are outside the fingerprint, so the review approval still applies.
    assert report["fingerprint"] == fingerprint
    reviewed = client.post(f"/api/projects/{project['id']}/change-analysis/review",
                           json={"action": "APPROVE", "fingerprint": fingerprint})
    assert reviewed.status_code == 200, reviewed.text
    draft = client.post(f"/api/projects/{project['id']}/prd/revisions")
    assert draft.status_code == 200, draft.text
    drafted = [c["description"] for c in draft.json()["content"]["acceptance_criteria"] if c["requirement_id"] == rid]
    assert drafted == criteria
    # Adopting the AI wording afterwards brings back the AI's own criteria.
    assert client.put(f"/api/requirements/{rid}/decision", json={"action": "ACCEPT"}).status_code == 200
    with Session() as db:
        report = db.get(Project, project["id"]).state["change_analysis"]
    restored = next(item for item in report["proposals"] if item["requirement_id"] == rid)["proposed_requirement"]
    assert restored["acceptance_criteria"] == ["通知附件為加密 PDF", stale]
    assert "suggested_acceptance_criteria" not in restored


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


def test_change_filed_under_another_topic_is_paired_with_its_current_rule(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        rule = ingest(db, "撥薪作業規範", [{"text": "現行通知"}], kind="BUSINESS_KNOWLEDGE", project=p)
        goal = ingest(db, "需求", [{"text": "改加密"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {
            rule.id: [claim("撥薪通知", "撥薪入帳完成後，系統以 email 寄送薪資入帳通知給員工，通知內容為明文"),
                      claim("撥薪轉帳", "撥薪檔需經放行主管核准方可入帳")],
            goal.id: [claim("薪資通知方式", "撥薪入帳後，現行以明文 email 寄送薪資入帳通知，需改為加密 PDF 附件", kind="CHANGE")]})
        by_name = {item["name"]: item for item in p.state["change_analysis"]["proposals"]}
        assert by_name["薪資通知方式"]["status"] == "MODIFY"
        assert "明文" in by_name["薪資通知方式"]["current_state"]
        assert by_name["撥薪通知"]["paired_current_state"] is True and not by_name["撥薪通知"]["needs_review"]
        assert by_name["撥薪轉帳"]["status"] == "CONTEXT" and not by_name["撥薪轉帳"].get("paired_current_state")
