"""Strategies adopted from EXP-01 to EXP-03B and the 2026-10-17 UAT, kept from regressing."""

from sqlalchemy import select

from app.db import Chunk, Project, Requirement, Session
from app.evidence import ingest
from app.intelligence import apply_analysis


def claim(topic, statement, kind="REQUIREMENT"):
    return {"topic": topic, "statement": statement, "kind": kind, "chunk_ids": ["fixture-chunk"],
            "confidence": 1, "subject": topic, "condition": "", "value": ""}


def test_golden_cases_meet_expectations_offline(client):
    from evaluation.golden import judge, load_cases, run_case
    for case in load_cases():
        verdict = judge(case, run_case(client, case, timeout=60))
        assert verdict["key_point_coverage"].split("/")[0] == verdict["key_point_coverage"].split("/")[1], case["id"]
        assert verdict.get("critical_in_range", True), case["id"]


def test_second_opinion_passes_run_only_when_something_is_at_risk(client, project):
    from app.workflow import needs_ai_review
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        goal = ingest(db, "本次需求", [{"text": "員工可自行設定密碼規則"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {goal.id: [claim("密碼規則", "員工可自行設定密碼規則")]})
        p.state = {**p.state, "change_analysis": {"proposals": [], "critical_questions": []}}
        for row in db.scalars(select(Requirement).where(Requirement.project_id == p.id)):
            row.status = "CONFIRMED_CANDIDATE"
        assert not needs_ai_review(db, p)
        row = db.scalar(select(Requirement).where(Requirement.project_id == p.id))
        row.status = "OPEN"
        assert needs_ai_review(db, p)
        row.status = "CONFIRMED_CANDIDATE"
        p.state = {**p.state, "change_analysis": {"proposals": [], "critical_questions": [
            {"requirement_id": row.id, "question": "是否放寬控制？"}]}}
        assert needs_ai_review(db, p)
        p.state = {**p.state, "change_analysis": {"proposals": [], "critical_questions": []}}


def test_shared_tags_do_not_bridge_unrelated_documents(client, project):
    from app.graph_store import graph_search, persist_chunk
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        near = ingest(db, "OTP 規範", [{"text": "OTP 驗證規則"}], project=p)
        far = ingest(db, "行銷活動辦法", [{"text": "行銷活動通則"}], project=p)
        db.flush()
        persist_chunk(db, near, db.scalar(select(Chunk).where(Chunk.source_id == near.id)),
                      {"tags": [{"tag": "OTP", "tag_type": "FUNCTION", "confidence": 1},
                                {"tag": "通用", "tag_type": "DOMAIN", "confidence": 1}]}, "t", "mock")
        persist_chunk(db, far, db.scalar(select(Chunk).where(Chunk.source_id == far.id)),
                      {"tags": [{"tag": "通用", "tag_type": "DOMAIN", "confidence": 1}]}, "t", "mock")
        found = {h["source_id"] for h in graph_search(db, "OTP", project["id"], 5, 3)}
        assert near.id in found and far.id not in found


def test_request_parts_missing_from_the_prd_are_hinted():
    from app.faithfulness import uncovered
    content = {"requirements": [{"statement": "薪資通知以加密 PDF 附件寄送，密碼以簡訊另送"}],
               "background": "通知改為加密附件，附件密碼不可與通知同封寄送，上線日 2027-03-01"}
    found = uncovered("本次需求：通知改為加密附件，附件密碼不可與通知同封寄送。上線日 2027-03-01。", content)
    assert [f["clause"] for f in found] == ["附件密碼不可與通知同封寄送", "上線日 2027-03-01"]
    assert found[1]["missing"] == ["2027-03-01"]


def test_quality_reports_uncovered_request_parts(client):
    project = client.post("/api/projects", json={"name": "撥薪通知",
                                                  "description": "通知改為加密附件。上線日 2027-03-01。"}).json()
    from test_prd_editor import draft
    revision = draft(client, project)
    issues = client.get(f"/api/prd-revisions/{revision['id']}/quality").json()["issues"]
    assert any(i["code"] == "UNCOVERED" and "2027-03-01" in i["message"] for i in issues)


def test_evidence_contract_reports_roles_and_targets_missing_ones(client, project):
    from app import knowledge_federation as federation
    from app.evidence_contract import assess, required_roles
    assert required_roles("通知改為加密附件，保留寄送紀錄") == ["BUSINESS_RULE", "CONTROL", "DATA"]
    seen = []
    with Session() as db:  # agent_search commits between passes
        p = db.get(Project, project["id"])
        policy = ingest(db, "個資保護政策", [{"text": "含個資附件應加密，稽核紀錄保存 5 年"}], kind="INTERNAL_POLICY")
        db.commit()

        def local_search(db_, query, project_id, config, strategy=None, source_ids=None):
            seen.append(query)
            hit = {"source_id": policy.id, "chunk_id": "c", "title": policy.title, "text": policy.content,
                   "kind": policy.kind, "meta": {}, "score": 0.9, "retrieval_channels": ["BM25"],
                   "case_relevance_score": 0.9, "evidence_role": "SUPPORTING"}
            return [hit] if "資安" in query else []

        contract = {}
        hits, queries = federation.agent_search(db, p, "通知改為加密附件", {"top_k": 8}, local_search,
                                                contract=contract)
        assert any("資安" in q for q in seen) and contract["attempts"]["CONTROL"] == "ADMITTED"
        assert any(h["source_id"] == policy.id for h in hits)
        result = assess(db, p, "通知改為加密附件", contract["attempts"])
        assert {r["role"]: r["status"] for r in result["roles"]} == {"BUSINESS_RULE": "GAP", "CONTROL": "GAP"}
        assert "不會讓 AI 自行補寫" in result["note"]


def test_chinese_numerals_count_as_covered():
    from app.faithfulness import uncovered
    content = {"requirements": [{"statement": "OTP 輸入錯誤三次即取消交易，不得扣款"}]}
    assert uncovered("連續輸入錯誤 3 次即取消交易", content) == []
