"""PMs complete and correct the PRD draft inside the app."""

from sqlalchemy import select

from app.db import Project, Requirement, Session
from app.evidence import ingest
from app.intelligence import apply_analysis


def claim(topic, statement, kind="REQUIREMENT"):
    return {"topic": topic, "statement": statement, "kind": kind, "chunk_ids": ["c"], "confidence": 1,
            "subject": topic, "condition": "", "value": ""}


def draft(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        goal = ingest(db, "撥薪通知需求", [{"text": "通知改為加密附件"}], kind="REQUIREMENT_TEXT", project=p)
        db.flush()
        apply_analysis(db, p, {goal.id: [claim("通知格式", "通知改為加密附件", "CHANGE")]})
        fingerprint = p.state["change_analysis"]["fingerprint"]
    reviewed = client.post(f"/api/projects/{project['id']}/change-analysis/review",
                           json={"action": "APPROVE", "fingerprint": fingerprint})
    assert reviewed.status_code == 200, reviewed.text
    created = client.post(f"/api/projects/{project['id']}/prd/revisions")
    assert created.status_code == 200, created.text
    return created.json()


def test_gaps_are_listed_and_answers_fill_the_draft(client, project):
    revision = draft(client, project)
    rid = revision["id"]
    gaps = {g["field"]: g for g in client.get(f"/api/prd-revisions/{rid}/gaps").json()["gaps"]}
    # Controls are no longer copied from requirement text by keyword.
    assert gaps["risk_compliance_security"]["filled"] is False
    assert gaps["success_metrics"]["question"]
    answered = client.post(f"/api/prd-revisions/{rid}/gaps/success_metrics/answer",
                           json={"answer": "通知寄送成功率 99.5%；客訴件數下降"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["items"] == ["通知寄送成功率 99.5%", "客訴件數下降"]
    overview = next(s for s in answered.json()["revision"]["projection"]["sections"] if s["key"] == "overview")
    assert "成功指標：通知寄送成功率 99.5%" in overview["paragraphs"]
    edited = client.put(f"/api/prd-revisions/{rid}/sections/goals", json={"text": "降低薪資資訊外洩風險"})
    assert edited.status_code == 200 and edited.json()["content"]["goals"] == "降低薪資資訊外洩風險"
    assert client.put(f"/api/prd-revisions/{rid}/sections/requirements", json={}).status_code == 404


def test_requirement_edits_and_carry_over_to_new_draft(client, project):
    revision = draft(client, project)
    rid = revision["id"]
    req_id = revision["content"]["requirements"][0]["id"]
    edited = client.put(f"/api/prd-revisions/{rid}/requirements/{req_id}",
                        json={"statement": "撥薪通知改為 AES-256 加密 PDF 附件", "priority": "高", "actor": "員工"})
    assert edited.status_code == 200, edited.text
    row = next(s for s in edited.json()["projection"]["sections"] if s["key"] == "requirements")["rows"][0]
    assert row["description"] == "撥薪通知改為 AES-256 加密 PDF 附件" and row["priority"] == "高"
    assert client.put(f"/api/prd-revisions/{rid}/requirements/{req_id}", json={"priority": "急"}).status_code == 422
    client.post(f"/api/prd-revisions/{rid}/gaps/out_of_scope/answer", json={"answer": "不含紙本薪資單"})
    with Session.begin() as db:
        requirement = db.get(Requirement, req_id)
        assert requirement.decision["action"] == "MODIFY" and requirement.decision["actor"] == "員工"
        p = db.get(Project, project["id"])
        p.state = {**p.state, "draft_id": None}
    again = client.post(f"/api/projects/{project['id']}/prd/revisions").json()
    assert again["id"] != rid
    assert again["content"]["requirements"][0]["priority"] == "高"
    assert again["content"]["out_of_scope"] == ["不含紙本薪資單"]
    # The earlier draft is superseded and can no longer be edited.
    assert client.put(f"/api/prd-revisions/{rid}/sections/goals", json={"text": "x"}).status_code == 409


def test_baselined_revision_is_read_only(client, project, monkeypatch, tmp_path):
    import app.lifecycle as lifecycle
    output = tmp_path / "prd.docx"
    output.write_bytes(b"docx")
    monkeypatch.setattr(lifecycle, "render_docx", lambda *_: str(output))
    revision = draft(client, project)
    assert client.post(f"/api/prd-revisions/{revision['id']}/baseline").status_code == 200
    assert client.put(f"/api/prd-revisions/{revision['id']}/sections/goals", json={"text": "x"}).status_code == 409
    with Session() as db:
        assert db.scalar(select(Project.current_revision_id).where(Project.id == project["id"])) == revision["id"]


def test_ai_advice_can_be_adopted_into_sections(client, project):
    revision = draft(client, project)
    rid = revision["id"]
    applied = client.post(f"/api/prd-revisions/{rid}/advice/apply",
                          json={"kind": "acceptance_criteria", "text": "附件密碼錯誤三次後鎖定"})
    assert applied.status_code == 200, applied.text
    assert any(x["description"] == "附件密碼錯誤三次後鎖定" for x in applied.json()["content"]["acceptance_criteria"])
    rules = client.post(f"/api/prd-revisions/{rid}/advice/apply", json={"kind": "business_rules", "text": "密碼不得與附件同封"})
    assert [x["description"] for x in rules.json()["content"]["business_rules"]] == ["密碼不得與附件同封"]
    assert client.post(f"/api/prd-revisions/{rid}/advice/apply", json={"kind": "隨意", "text": "x"}).status_code == 422
