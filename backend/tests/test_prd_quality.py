"""Advisory PRD quality hints, conservative splitting and the golden harness."""

from app.db import Project, Session
from app.prd_quality import check, compound_clauses
from test_prd_editor import draft  # sibling module; works from repo root and backend/


def row(rid, statement, **extra):
    return {"id": rid, "topic": rid, "statement": statement, "evidence": [{"source_id": "s"}], **extra}


def test_rules_flag_vague_unmeasurable_and_missing_criteria_but_never_block():
    content = {"requirements": [
        row("a", "系統應盡量快速寄送通知"),
        row("b", "員工於通知寄出後 5 分鐘內收到簡訊密碼", priority="高", actor="員工"),
    ], "acceptance_criteria": [
        {"requirement_id": "b", "description": "前提：通知已寄出；操作：查看手機；結果：5 分鐘內收到密碼簡訊"}]}
    result = check(content)
    codes = {(i["requirement_id"], i["code"]) for i in result["issues"]}
    assert ("a", "VAGUE") in codes and ("a", "UNMEASURABLE") in codes and ("a", "NO_CRITERIA") in codes
    assert not any(rid == "b" and code in {"VAGUE", "UNMEASURABLE", "NO_CRITERIA"} for rid, code in codes)
    assert result["note"] == "品質提示僅供參考，不影響定版。" and 0 <= result["score"] < 100


def test_split_is_suggested_only_for_independent_actions():
    assert compound_clauses("通知改為加密 PDF 附件；附件密碼以簡訊另送；員工可設定密碼規則") == [
        "通知改為加密 PDF 附件", "附件密碼以簡訊另送", "員工可設定密碼規則"]
    # Details and conditions of one behaviour stay together.
    assert compound_clauses("通知改為加密 PDF 附件，內容含姓名、金額與末四碼帳號") == []
    # An exception path belongs to the behaviour it qualifies.
    assert compound_clauses("撥薪入帳後寄送通知；失敗時最多重送 3 次") == []
    assert compound_clauses("寄送通知；寄送通知給主管") == []  # same action twice is not two requirements


def test_confirmed_split_persists_edits_and_can_be_undone(client, project):
    revision = draft(client, project)
    rid, req_id = revision["id"], revision["content"]["requirements"][0]["id"]
    suggestion = client.post(f"/api/prd-revisions/{rid}/requirements/{req_id}/split-suggestion").json()
    assert suggestion["split"] is False  # "通知改為加密附件" is one thing
    assert client.put(f"/api/prd-revisions/{rid}/requirements/{req_id}/split",
                      json={"parts": [{"title": "只有一條", "statement": "通知加密"}]}).status_code == 422
    parts = [{"title": "附件加密", "statement": "薪資通知改為加密 PDF 附件"},
             {"title": "密碼另送", "statement": "附件密碼以簡訊另送"}]
    split = client.put(f"/api/prd-revisions/{rid}/requirements/{req_id}/split", json={"parts": parts})
    assert split.status_code == 200, split.text
    rows = split.json()["content"]["requirements"]
    assert [r["id"] for r in rows] == [f"{req_id}::1", f"{req_id}::2"]
    edited = client.put(f"/api/prd-revisions/{rid}/requirements/{req_id}::2", json={"priority": "高"})
    assert edited.status_code == 200 and edited.json()["content"]["requirements"][1]["priority"] == "高"
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        p.state = {**p.state, "draft_id": None}
    again = client.post(f"/api/projects/{project['id']}/prd/revisions").json()
    assert [r["topic"] for r in again["content"]["requirements"]] == ["附件加密", "密碼另送"]
    assert again["content"]["requirements"][1]["priority"] == "高"
    undone = client.put(f"/api/prd-revisions/{again['id']}/requirements/{req_id}/split", json={"parts": []})
    assert [r["id"] for r in undone.json()["content"]["requirements"]] == [req_id]
    quality = client.get(f"/api/prd-revisions/{again['id']}/quality").json()
    assert any(i["code"] == "EMPTY_SECTIONS" for i in quality["issues"])


def test_improve_in_offline_mode_says_so_instead_of_inventing(client, project):
    revision = draft(client, project)
    req_id = revision["content"]["requirements"][0]["id"]
    result = client.post(f"/api/prd-revisions/{revision['id']}/requirements/{req_id}/improve", json={}).json()
    assert "離線規則模式" in result["warning"]
    assert result["statement"] == revision["content"]["requirements"][0]["statement"]


def test_golden_case_runs_end_to_end_and_repeats_identically_offline(client):
    from evaluation.golden import judge, load_cases, run_case, stability
    case = next(c for c in load_cases() if c["id"] == "payroll-notification")
    runs = [run_case(client, case, timeout=60) for _ in range(2)]
    assert runs[0]["model_status"] == "OFFLINE_RULES"
    assert runs[0]["prd_requirements"] >= 1 and runs[0]["quality_score"] is not None
    assert stability(runs)["topic_agreement"] == 1.0
    assert set(judge(case, runs[0])) == {"review_items_in_range", "fragmented", "key_point_coverage"}


def test_adopted_criteria_attach_to_their_requirement(client, project):
    revision = draft(client, project)
    req_id = revision["content"]["requirements"][0]["id"]
    applied = client.post(f"/api/prd-revisions/{revision['id']}/advice/apply", json={
        "kind": "acceptance_criteria", "requirement_id": req_id,
        "text": "前提：撥薪已入帳；操作：員工開啟通知；結果：附件需密碼才能開啟"}).json()
    assert any(c.get("requirement_id") == req_id and c["description"].startswith("前提")
               for c in applied["content"]["acceptance_criteria"])
    issues = client.get(f"/api/prd-revisions/{revision['id']}/quality").json()["issues"]
    assert not any(i["requirement_id"] == req_id and i["code"] == "NO_CRITERIA" for i in issues)
