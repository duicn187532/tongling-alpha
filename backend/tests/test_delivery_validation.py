from types import SimpleNamespace

from app.delivery_validation import assess
from app.db import Session, Project, Requirement
from app.evidence import ingest


def test_it_handoff_check_does_not_treat_v2_placeholders_as_design():
    revision = SimpleNamespace(id="revision-1", content_hash="hash-1", content={
        "requirements": [{"id": "REQ-1", "topic": "電子帳單同意", "statement": "同意後才能送出",
                          "decision": {"action": "ACCEPT"}, "evidence": [{"source_id": "source-1"}]}],
        "business_rules": [{"requirement_id": "REQ-1", "description": "同意後才能送出"}],
        "api_message_impact": [{"requirement_id": "REQ-1", "description": "API 變更"}],
        "diagrams": [{"type": "SYSTEM", "mermaid": "flowchart LR\nA-->B", "warning": "待 SA 確認"}],
    })
    result = assess(revision)
    codes = {gap["code"] for gap in result["blocking_gaps"]}
    assert result["readiness"] == "BLOCKED"
    assert "MISSING_ACCEPTANCE" in codes
    assert "MISSING_INTERFACE_CONTRACTS" in codes
    assert "MISSING_SYSTEM_SEQUENCE" in codes
    assert "MISSING_REVIEW" in codes
    assert result["revision_hash"] == "hash-1"


def test_explicit_exclusion_is_not_a_function_and_rejection_is_not_exclusion(client):
    pid = client.post("/api/projects", json={"name": "範圍測試"}).json()["id"]
    with Session.begin() as db:
        project = db.get(Project, pid)
        source = ingest(db, "需求來源", [{"text": "顯示帳單；本期不含紙本寄送"}], kind="REQUIREMENT_TEXT", project=project)
        db.flush()
        facts = [
            ("帳單查詢", "應顯示帳單", "CONFIRMED_CANDIDATE", "ACCEPT", "FUNCTIONAL"),
            ("收費規則", "本期不得收費", "CONFIRMED_CANDIDATE", "ACCEPT", "BUSINESS_RULE"),
            ("跨單位確認時程", "上線前召開確認會", "CONFIRMED_CANDIDATE", "ACCEPT", "DECISION_CONTEXT"),
            ("待確認事項", "通知模板尚待決定", "OPEN", "ACCEPT", "OPEN_ISSUE"),
            ("人工排除", "本期不修改核心帳務", "CONFIRMED_CANDIDATE", "ACCEPT", "SCOPE_EXCLUSION"),
            ("Out of Scope", "本期不含紙本寄送", "EXCLUDED", "ACCEPT", "SCOPE_EXCLUSION"),
            ("被拒候選", "舊來源的建議", "CONFIRMED_CANDIDATE", "REJECT", "FUNCTIONAL"),
        ]
        for topic, statement, status, action, category in facts:
            db.add(Requirement(project_id=pid, topic=topic, statement=statement,
                               status=status, evidence=[{"source_id": source.id, "chunk_ids": [],
                                                         "kind": "EXCLUDED" if status == "EXCLUDED" else "REQUIREMENT",
                                                         "statement": statement}],
                               decision={"action": action, "statement": statement, "rationale": "人工確認",
                                         "category": category}))
        project.state = {**project.state, "analysis_stale": False}
    response = client.post(f"/api/projects/{pid}/prd/revisions")
    assert response.status_code == 200, response.text
    content = response.json()["content"]
    assert [r["topic"] for r in content["requirements"]] == ["帳單查詢"]
    assert set(content["out_of_scope"]) == {"本期不含紙本寄送", "本期不修改核心帳務"}
    assert any(r["description"] == "本期不得收費" for r in content["business_rules"])
    assert any(r.get("description") == "上線前召開確認會" for r in content["decisions"])
    assert any(r["statement"] == "通知模板尚待決定" for r in content["open_issues"])
