"""Host library controls preserve project evidence and exclude archived documents."""

from sqlalchemy import select

from app.db import Link, Project, Session, Source
from app.evidence import ingest
from app.knowledge_federation import local_source_ids, policy_for
from app.intelligence import settings


def test_host_can_review_archive_and_restore_shared_document(client, project):
    with Session() as db:
        row = ingest(db, "共用付款規範", [{"text": "付款完成後應顯示確認訊息。"}], kind="INTERNAL_POLICY")
        sid = row.id
        db.add(Link(project_id=project["id"], source_id=sid))
        db.commit()

    docs = client.get("/api/knowledge/documents")
    assert docs.status_code == 200
    assert any(d["id"] == sid and d["linked_projects"] == 1 for d in docs.json())
    detail = client.get(f"/api/knowledge/documents/{sid}").json()
    assert "付款完成" in detail["content_preview"]
    assert "content" not in docs.json()[0]

    disabled = client.patch(f"/api/knowledge/documents/{sid}", json={"active": False})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "ARCHIVED"
    with Session() as db:
        assert db.get(Project, project["id"]).state["analysis_stale"] is True
        assert not db.scalar(select(Link).where(Link.project_id == project["id"], Link.source_id == sid))
        assert sid not in local_source_ids(db, project["id"], policy_for(db, project["id"], settings(db)))

    restored = client.patch(f"/api/knowledge/documents/{sid}", json={"active": True})
    assert restored.status_code == 200
    assert restored.json()["status"] == "ACTIVE"
    with Session() as db:
        assert sid in local_source_ids(db, project["id"], policy_for(db, project["id"], settings(db)))


def test_host_preview_search_and_project_files_stay_separate(client, project):
    with Session() as db:
        shared = ingest(db, "共享測試規則", [{"text": "共享測試規則：帳單查詢需要身份驗證。"}], kind="INTERNAL_POLICY")
        own = ingest(db, "專案私有文件", [{"text": "專案私有文件：客戶代碼。"}], project=db.get(Project, project["id"]))
        db.commit()
    response = client.post("/api/knowledge/search-preview", json={"query": "帳單查詢身份驗證", "top_k": 5})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "FAST_LEXICAL"
    assert any(hit["source_id"] == shared.id and hit["evidence_role"] == "PREVIEW_ONLY" for hit in response.json()["results"])
    assert all(hit["source_id"] != own.id for hit in response.json()["results"])
    assert client.get(f"/api/knowledge/documents/{own.id}").status_code == 404
    assert any(d["id"] == shared.id for d in client.get("/api/knowledge/documents").json())
