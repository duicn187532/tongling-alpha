"""Alpha T01/T02 and U01/U02/U08 acceptance checks."""

import json
import zipfile

import pytest
from sqlalchemy import select

from app.db import DATA, Session, Project, Requirement, Revision, KnowledgeItem
from app.evidence import ingest
from app.enrichment import create as create_job
from app.intelligence import apply_analysis, resolve_temporal, contradiction
from app.lifecycle import content_hash


def _item(identifier, name, sid, rid):
    return {"id": identifier, "name": name, "description": name + "：已由測試來源確認",
            "source_refs": [sid], "requirement_refs": [rid], "status": "CONFIRMED"}


def test_technical_model_review_gate_and_immutable_package(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        source = ingest(db, "SA 技術規格", [{"text": "前台傳送交易電文至核心；新增驗證欄位"}], project=p)
        db.flush()
        sid = source.id
        rid = "REQ-ALPHA"
        content = {"title": "交易驗證", "background": "測試", "goals": "驗證",
                   "requirements": [{"id": rid, "topic": "交易驗證", "statement": "交易應驗證",
                                     "evidence": [{"source_id": sid, "chunk_ids": []}],
                                     "decision": {"action": "ACCEPT"}}], "diagrams": []}
        revision = Revision(project_id=p.id, version="v0.1", status="IN_REVIEW",
                            content=content, content_hash=content_hash(content))
        db.add(revision)
        db.flush()
        revision_id, initial_hash = revision.id, revision.content_hash
    assert client.get(f"/api/prd-revisions/{revision_id}/delivery-readiness").json()["readiness"] == "BLOCKED"
    model = {
        "system_components": [_item("SYS-A", "前台", sid, rid), _item("SYS-B", "核心", sid, rid)],
        "interface_contracts": [{**_item("API-1", "交易電文", sid, rid), "from_system": "前台",
                                 "to_system": "核心", "before": "無驗證碼", "after": "增加驗證碼",
                                 "field_diffs": [{"field": "驗證碼", "before": "無", "after": "必填", "reason": "風險控制"}]}],
        "data_changes": [{**_item("DATA-1", "驗證紀錄", sid, rid), "before": "無紀錄", "after": "新增紀錄",
                          "migration": "既有資料不回補", "rollback": "停用新欄位並保留歷史"}],
        "flow_definitions": [{**_item("FLOW-1", "交易流程", sid, rid), "steps": ["送出", "驗證"], "exception_steps": ["拒絕"]}],
        "business_rules": [{**_item("RULE-1", "驗證失敗不得送出", sid, rid),
                            "condition": "驗證通過", "on_true": "送出交易", "on_false": "拒絕交易"}],
        "acceptance_cases": [{**_item("CASE-1", "驗證失敗", sid, rid), "scenario": "FAILURE", "expected": "交易拒絕"}],
        "technical_impact": [{"area": area, "affected": True, "rationale": "已評估", "source_refs": [sid]}
                             for area in ("SYSTEM", "INTERFACE", "DATA", "FLOW", "SECURITY", "OPERATIONS")],
    }
    bad = client.put(f"/api/prd-revisions/{revision_id}/technical-model",
                     json={"expected_hash": "0" * 64, "model": model})
    assert bad.status_code == 409
    saved = client.put(f"/api/prd-revisions/{revision_id}/technical-model",
                       json={"expected_hash": initial_hash, "model": model})
    assert saved.status_code == 200, saved.text
    new_hash = saved.json()["content_hash"]
    generated = client.post(f"/api/prd-revisions/{revision_id}/technical-diagrams")
    assert generated.status_code == 200, generated.text
    diagram_hash = generated.json()["content_hash"]
    assert diagram_hash != new_hash
    for role in ("PM", "SA", "QA"):
        response = client.post(f"/api/prd-revisions/{revision_id}/technical-reviews",
                               json={"role": role, "reviewer": "測試審閱者", "note": "已核對來源與欄位",
                                     "expected_hash": diagram_hash})
        assert response.status_code == 200, response.text
    assert response.json()["readiness"] == "READY"
    with Session.begin() as db:
        revision = db.get(Revision, revision_id)
        revision.status = "BASELINED"
        p = db.get(Project, project["id"])
        p.current_revision_id = revision_id
        p.state = {**p.state, "draft_id": None}
        folder = DATA / "exports" / revision_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "PRD.docx").write_bytes(b"test baseline")
    packaged = client.post(f"/api/prd-revisions/{revision_id}/delivery-package")
    assert packaged.status_code == 200, packaged.text
    manifest = packaged.json()["manifest"]
    assert manifest["content_hash"] == diagram_hash
    with zipfile.ZipFile(DATA / "exports" / revision_id / "IT_HANDOFF.zip") as archive:
        assert {"PRD.docx", "technical_model.json", "manifest.json",
                "technical/BUSINESS_RULE_FLOW.png", "technical/SYSTEM_SEQUENCE.png",
                "technical/SYSTEM_ARCHITECTURE.png"}.issubset(archive.namelist())
        assert json.loads(archive.read("technical_model.json"))["interface_contracts"][0]["field_diffs"][0]["field"] == "驗證碼"
    assert client.post(f"/api/prd-revisions/{revision_id}/delivery-package").json()["id"] == packaged.json()["id"]
    amendment = client.post(f"/api/prd-revisions/{revision_id}/technical-amendment")
    assert amendment.status_code == 200, amendment.text
    assert amendment.json()["parent_revision_id"] == revision_id
    assert amendment.json()["content"]["requirements"] == content["requirements"]
    assert client.post(f"/api/prd-revisions/{amendment.json()['id']}/baseline").status_code == 200
    assert client.delete(f"/api/projects/{project['id']}").status_code == 200
    purged = client.request("DELETE", f"/api/projects/{project['id']}/permanent",
                            json={"confirmation": project["name"]})
    assert purged.status_code == 200, purged.text


def test_complementary_conditions_and_explicit_meeting_supersession(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        a = ingest(db, "規則一", [{"text": "當狀態正常時，應通知"}], project=p)
        b = ingest(db, "規則二", [{"text": "當狀態異常時，不得通知"}], project=p)
        c = ingest(db, "新會議", [{"text": "決議改為新規則"}], project=p)
        db.flush()
        topic = "通知規則"
        base = {"topic": topic, "kind": "DECISION", "chunk_ids": [], "confidence": 1}
        apply_analysis(db, p, {a.id: [{**base, "statement": "當狀態正常時，應通知", "condition": "正常"}],
                               b.id: [{**base, "statement": "當狀態異常時，不得通知", "condition": "異常"}]})
        row = db.scalar(select(Requirement).where(Requirement.project_id == p.id, Requirement.topic == topic))
        assert row.status == "CONFIRMED_CANDIDATE"
        apply_analysis(db, p, {c.id: [{**base, "statement": "當狀態正常時，決議改為不得通知",
                                        "condition": "正常", "meeting_id": "meeting-new",
                                        "meeting_date": "2026-10-18", "index": 0}]})
        db.flush()
        assert row.status == "CONFIRMED_CANDIDATE"
        assert any(x.get("validity") == "SUPERSEDED" and x["source_id"] == a.id for x in row.evidence)
        assert any(x["source_id"] == b.id and x.get("validity") != "SUPERSEDED" for x in row.evidence)


def test_selected_graph_job_only_enqueues_selected_document(client, project):
    with Session.begin() as db:
        p = db.get(Project, project["id"])
        first = ingest(db, "圖譜選取一", [{"text": "本案需要 OTP 驗證"}], project=p)
        second = ingest(db, "圖譜選取二", [{"text": "本案需要通知"}], project=p)
        db.flush()
        job = create_job(db, "GRAPH_BUILD", p.id, [first.id], force=True)
        db.flush()
        items = list(db.scalars(select(KnowledgeItem).where(KnowledgeItem.job_id == job.id)))
        assert items and {i.source_id for i in items} == {first.id}
        assert job.checkpoint["selected_source_ids"] == [first.id]
        with pytest.raises(ValueError):
            create_job(db, "GRAPH_BUILD", p.id, [second.id], force=True)


def test_same_condition_conflicts_but_distinct_condition_does_not():
    base = {"topic": "每日限額", "subject": "轉帳限額", "effective_at": "2026-11-01"}
    a = {**base, "condition": "一般戶", "statement": "每日限額為五萬", "value": "50000"}
    b = {**base, "condition": "一般戶", "statement": "每日限額為十萬", "value": "100000"}
    c = {**base, "condition": "VIP", "statement": "每日限額為十萬", "value": "100000"}
    assert contradiction(a, b)
    assert not contradiction(a, c)
    later = {**b, "kind": "DECISION", "statement": "決議改為十萬", "meeting_date": "2026-10-18"}
    current, reopened, history = resolve_temporal([{**a, "kind": "DECISION", "meeting_date": "2026-10-01"}, later])
    assert not reopened and len(current) == 1 and current[0]["statement"] == "決議改為十萬"
    assert history[0]["validity"] == "SUPERSEDED"


def test_confirmed_meeting_needs_and_exclusions_stay_in_force():
    def fact(day, kind, statement):
        return {"meeting_date": day, "kind": kind, "statement": statement}

    current, reopened, _ = resolve_temporal([fact("2026-10-14", "REQUIREMENT", "客服需能查詢重送紀錄"),
                                             fact("2026-10-14", "EXCLUDED", "行銷推播不納入本次範圍")])
    assert {x["statement"] for x in current} == {"客服需能查詢重送紀錄", "行銷推播不納入本次範圍"}
    assert not reopened
    # A follow-up someone still owes keeps the topic open.
    _, reopened, history = resolve_temporal([fact("2026-10-14", "ACTION_ITEM", "法遵下週三前回覆可否代填手機")])
    assert reopened and history[0]["validity"] == "OPEN"
