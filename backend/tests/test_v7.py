import copy
from sqlalchemy import select, func
from app.db import (
    Session,
    Project,
    Source,
    Chunk,
    Link,
    Revision,
    GraphEdge,
    KnowledgeJob,
    KnowledgeItem,
    now,
)
from app import prd_templates, enrichment, peer_research
from app.evidence import ingest
from app.intelligence import settings
from app.graph_store import persist_chunk, graph_search
from app.retrieval import retrieve, qualify


def test_template_projection_never_filters_canonical():
    content = {
        "title": "金融需求",
        "background": "背景",
        "goals": "背景",
        "requirements": [],
        "meetings": [
            {
                "meeting_date": "2026-10-17",
                "title": "決策",
                "events": [{"statement": "保留"}],
            }
        ],
        "diagrams": [],
    }
    before = copy.deepcopy(content)
    template = {
        "name": "自訂",
        "sections": [
            {"key": "meetings", "title": "附錄", "enabled": False},
            {"key": "scope", "title": "範圍先看", "enabled": True},
            {"key": "overview", "title": "目標", "enabled": True},
        ],
    }
    projected = prd_templates.project(content, template)
    assert content == before and [s["key"] for s in projected["sections"]] == [
        "scope",
        "overview",
    ]
    assert len(projected["warnings"]) == 3
    assert [
        s["key"] for s in prd_templates.builtins()[0]["sections"]
    ] == prd_templates.STANDARD


def test_trash_restore_purge_preserves_shared_and_baselines(client, project):
    pid = project["id"]
    with Session() as db:
        p = db.get(Project, pid)
        shared = ingest(
            db, "共用政策", [{"text": "驗證政策：使用 OTP"}], kind="INTERNAL_POLICY"
        )
        sid = shared.id
        db.add(Link(project_id=pid, source_id=sid))
        owned = list(
            db.scalars(
                select(Link.source_id).where(
                    Link.project_id == pid, Link.source_id != sid
                )
            )
        )
        revision = Revision(
            project_id=pid,
            version="v1.0",
            status="BASELINED",
            content={"requirements": []},
            content_hash="test",
        )
        db.add(revision)
        db.flush()
        p.current_revision_id = revision.id
        rid = revision.id
        db.commit()
    assert client.delete("/api/projects/" + pid).status_code == 200
    assert client.get("/api/projects/" + pid).status_code == 404
    assert pid not in [p["id"] for p in client.get("/api/projects").json()]
    assert any(p["id"] == pid for p in client.get("/api/projects/trash").json())
    assert client.post("/api/projects/" + pid + "/restore").status_code == 200
    with Session() as db:
        assert db.get(Revision, rid).status == "BASELINED"
    client.delete("/api/projects/" + pid)
    assert (
        client.request(
            "DELETE", f"/api/projects/{pid}/permanent", json={"confirmation": "wrong"}
        ).status_code
        == 422
    )
    result = client.request(
        "DELETE",
        f"/api/projects/{pid}/permanent",
        json={"confirmation": project["name"]},
    )
    assert result.status_code == 200, result.text
    with Session() as db:
        assert db.get(Source, sid) and not db.get(Revision, rid)
        assert not db.get(Project, pid) and all(
            db.get(Source, s) is None for s in owned
        )


def test_graph_qualification_scope_temporal_and_external(client, project):
    with Session() as db:
        config = {
            **settings(db),
            "graph_retrieval_enabled": True,
            "graph_document_top_k": 3,
            "reranker_enabled": False,
        }
        sources = []
        for index in range(5):
            s = ingest(
                db,
                f"OTP 權威 {index}",
                [{"text": "OTP 驗證 必須綁定裝置"}],
                kind="INTERNAL_POLICY",
            )
            sources.append(s)
            c = db.scalar(select(Chunk).where(Chunk.source_id == s.id))
            persist_chunk(
                db,
                s,
                c,
                {"tags": [{"tag": "OTP", "tag_type": "FUNCTION", "confidence": 1}]},
                "test",
                "mock",
            )
        other = Project(workspace_id=project["workspace_id"], name="其他專案", state={})
        db.add(other)
        db.flush()
        private = ingest(
            db, "OTP 不可跨專案", [{"text": "OTP 私有機密"}], project=other
        )
        c = db.scalar(select(Chunk).where(Chunk.source_id == private.id))
        persist_chunk(
            db,
            private,
            c,
            {"tags": [{"tag": "OTP", "tag_type": "FUNCTION", "confidence": 1}]},
            "test",
            "mock",
        )
        sources[0].status = "SUPERSEDED"
        db.commit()
        hits = graph_search(db, "OTP", project["id"], 3, 2)
        assert len(hits) == 3 and all(
            h["source_id"] not in {private.id, sources[0].id} for h in hits
        )
        result = retrieve(db, "OTP 驗證", project["id"], config)
        assert any("GRAPH" in r["retrieval_channels"] for r in result)
        assert all(r["source_id"] != private.id for r in result)
        ext = Source(kind="EXTERNAL_RESEARCH", status="ACTIVE", meta={})
        assert (
            qualify(ext, 1, "test", True, config)["evidence_role"]
            == "EXTERNAL_BENCHMARK"
        )
        assert (
            qualify(sources[0], 1, "test", True, config)["evidence_role"] == "REJECTED"
        )
        assert qualify(private, 1, "test", False, config)["evidence_role"] == "REJECTED"


def test_105_documents_checkpoint_retry_idempotency_and_cancel(
    client, project, monkeypatch
):
    enrichment.shutdown()
    original = enrichment.structured

    def failing(config, task, instruction, payload, schema, fallback):
        if payload.get("title") == "fixture-failure" and task == "ENTITY_EXTRACTION":
            raise ValueError("injected OOM")
        return original(config, task, instruction, payload, schema, fallback)

    monkeypatch.setattr(enrichment, "structured", failing)
    try:
        with Session() as db:
            p = db.get(Project, project["id"])
            # Keep fixture strictly project-scoped; exact total excludes global library fixtures.
            for i in range(105):
                ingest(
                    db,
                    "fixture-failure" if i == 104 else f"GraphDoc-{i}",
                    [{"text": f"AlphaAPI 訂單 {i} 綁定驗證"}],
                    project=p,
                )
            job = enrichment.create(db, "GRAPH_BUILD", p.id)
            jid = job.id
            db.commit()
        percentages = []
        for _ in range(400):
            lease = enrichment.claim()
            if lease:
                enrichment.process(*lease)
            with Session() as db:
                for i in db.scalars(
                    select(KnowledgeItem).where(
                        KnowledgeItem.job_id == jid,
                        KnowledgeItem.status == "WAITING_RETRY",
                    )
                ):
                    i.available_at = now()
                j = db.get(KnowledgeJob, jid)
                percentages.append(j.progress)
                state = j.status
                db.commit()
            if state in {"COMPLETED", "PARTIAL"}:
                break
        assert state == "PARTIAL"
        assert percentages == sorted(percentages)
        with Session() as db:
            job = db.get(KnowledgeJob, jid)
            report = enrichment.status(db, job)
            assert report["completed_items"] >= 105 and report["failed_items"] == 1
            failed = db.scalar(
                select(KnowledgeItem).where(
                    KnowledgeItem.job_id == jid, KnowledgeItem.status == "FAILED"
                )
            )
            assert failed.result["tags"] and failed.stage == "ENTITY_EXTRACTION"
            # Simulate death after a saved stage: recovery must keep that checkpoint.
            failed.status = "RUNNING"
            failed.lease_token = "dead-process"
            failed.lease_until = now()
            db.commit()
        enrichment.recover()
        with Session() as db:
            failed = db.get(KnowledgeItem, failed.id)
            assert failed.result["tags"] and failed.stage == "ENTITY_EXTRACTION"
            failed.status = "FAILED"
            enrichment.control(db, db.get(KnowledgeJob, jid), "retry")
            db.commit()
        monkeypatch.setattr(enrichment, "structured", original)
        lease = enrichment.claim()
        assert lease
        enrichment.process(*lease)
        with Session() as db:
            assert db.get(KnowledgeJob, jid).status == "COMPLETED"
            before = db.scalar(select(func.count()).select_from(GraphEdge))
            item = db.get(KnowledgeItem, lease[0])
            source = db.get(Source, item.source_id)
            chunk = db.get(Chunk, item.chunk_id)
            persist_chunk(db, source, chunk, item.result, jid, "mock")
            db.commit()
            assert db.scalar(select(func.count()).select_from(GraphEdge)) == before
            extra = enrichment.create(db, "TAG_GENERATION", project["id"])
            enrichment.control(db, extra, "cancel")
            db.commit()
            assert extra.status == "CANCELLED"
        assert enrichment.claim() is None
    finally:
        enrichment.start()


def test_peer_provider_failure_and_canonical_dedup(client, project, monkeypatch):
    assert peer_research.canonical_url(
        "https://example.org/a?utm_source=x&b=2#top"
    ) == peer_research.canonical_url("https://example.org/a?b=2")
    assert (
        client.patch(
            "/api/settings/organization",
            json={"industry": "銀行", "peer_companies": ["範例銀行"]},
        ).status_code
        == 200
    )

    def unavailable(*args, **kwargs):
        raise ValueError("provider unavailable")

    monkeypatch.setattr(peer_research.SearchProvider, "search", unavailable)
    row = client.post(
        f"/api/projects/{project['id']}/peer-research", json={"topic": "行動銀行"}
    ).json()
    import time

    for _ in range(100):
        r = client.get(
            f"/api/projects/{project['id']}/peer-research/{row['id']}"
        ).json()
        if r["status"] == "DEGRADED":
            break
        time.sleep(0.03)
    assert (
        r["status"] == "DEGRADED" and not r["data"]["sources"] and r["data"]["errors"]
    )


def test_new_model_routing_and_oom_releases_capacity(monkeypatch):
    import httpx
    from app import model_runtime as runtime
    from app.graph_store import Tags
    from app.intelligence import DEFAULT_SETTINGS

    seen = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kw):
            seen.append((url, kw["json"]["model"]))
            return httpx.Response(
                500, text="out of memory", request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)
    monkeypatch.setattr(runtime, "resource_limit", 4)
    cfg = {
        **DEFAULT_SETTINGS,
        "model_roles": {
            "worker": {
                "provider": "ollama",
                "endpoint": "http://localhost:11434",
                "model": "configured-worker",
            }
        },
    }
    import pytest

    with pytest.raises(RuntimeError, match="OOM"):
        runtime.structured(cfg, "TAG_EXTRACTION", "test", {}, Tags, lambda: {})
    assert seen == [("http://localhost:11434/api/chat", "configured-worker")]
    assert runtime.active == 0 and runtime.resource_limit == 1


def test_revision_derived_fields_refresh_only_linked_rows():
    content = {
        "requirements": [
            {
                "id": "one",
                "topic": "費用",
                "statement": "費用 10 元",
                "evidence": [],
                "decision": {},
            }
        ]
    }
    prd_templates.canonical_v2(content)
    content["business_rules"].append("獨立政策保留")
    content["requirements"][0]["statement"] = "費用 20 元"
    prd_templates.canonical_v2(content)
    assert content["business_rules"] == [
        "獨立政策保留",
        {"requirement_id": "one", "description": "費用 20 元", "source_refs": []},
    ]


def test_rejected_alias_and_incomplete_graph_still_uses_hybrid(client, project):
    from app.db import GraphNode

    with Session() as db:
        s = ingest(
            db,
            "ClearanceAPI",
            [{"text": "ClearanceAPI 清算作業驗證"}],
            kind="TECH_SPEC",
        )
        c = db.scalar(select(Chunk).where(Chunk.source_id == s.id))
        persist_chunk(
            db,
            s,
            c,
            {"tags": [{"tag": "UniqueAlias", "tag_type": "SYSTEM", "confidence": 1}]},
            "fixture",
            "mock",
        )
        n = db.scalar(select(GraphNode).where(GraphNode.label == "UniqueAlias"))
        n.aliases = ["ClearanceAPI"]
        db.commit()
        assert any(
            h["source_id"] == s.id
            for h in graph_search(db, "UniqueAlias", project["id"])
        )
        n.meta = {**n.meta, "status": "REJECTED"}
        db.commit()
        assert not any(
            h["source_id"] == s.id
            for h in graph_search(db, "UniqueAlias", project["id"])
        )
        hits = retrieve(
            db,
            "ClearanceAPI",
            project["id"],
            {**settings(db), "graph_retrieval_enabled": True},
        )
        assert any(h["source_id"] == s.id for h in hits)


def test_peer_changed_article_creates_source_version(client, project, monkeypatch):
    from app.db import ResearchRun
    from app import activity

    body = ["範例銀行 PaymentAPI 上線"]
    monkeypatch.setattr(
        peer_research.SearchProvider,
        "search",
        lambda *a, **k: [
            {
                "url": "https://example.org/versioned",
                "title": "範例銀行 PaymentAPI",
                "content": body[0],
            }
        ],
    )
    monkeypatch.setattr(
        peer_research.SearchProvider,
        "fetch",
        lambda *a, **k: {"content": "", "coverage": "SEARCH_SNIPPET"},
    )
    source_ids = []
    for text in [
        "範例銀行 PaymentAPI 上線",
        "範例銀行 PaymentAPI 更新並新增驗證",
        "範例銀行 PaymentAPI 更新並新增驗證",
    ]:
        body[0] = text
        with Session() as db:
            config = {
                **settings(db),
                "provider": "mock",
                "model_roles": {},
                "organization": {"industry": "銀行", "peer_companies": ["範例銀行"]},
            }
            r = ResearchRun(
                project_id=project["id"],
                data={"topic": "PaymentAPI", "organization": config["organization"]},
            )
            db.add(r)
            db.flush()
            rid = r.id
            activity.create(db, "版本測試", config, project["id"], rid)
            db.commit()
        peer_research.execute(rid, config)
        with Session() as db:
            r = db.get(ResearchRun, rid)
            assert r.status == "COMPLETED"
            source_ids.append(r.data["sources"][0]["source_id"])
            assert db.get(Source, source_ids[-1]).content == text
    assert source_ids[0] != source_ids[1] == source_ids[2]
    with Session() as db:
        assert db.get(Source, source_ids[0]).status == "SUPERSEDED"


def test_template_docx_projection_order_without_mutating_baseline(client, project):
    from app.lifecycle import render_docx, content_hash
    from app.db import PRDTemplate, Workspace
    from docx import Document

    content = {
        "title": "測試金融 PRD",
        "background": "背景",
        "goals": "目標",
        "requirements": [],
        "meetings": [],
        "diagrams": [],
    }
    with Session() as db:
        row = Revision(
            project_id=project["id"],
            version="v1.0",
            status="BASELINED",
            content=content,
            content_hash=content_hash(content),
            presentation=prd_templates.builtins()[0],
        )
        db.add(row)
        db.flush()
        rid = row.id
        t = PRDTemplate(
            name="測試順序",
            data={
                "sections": [
                    {"key": "scope", "title": "範圍優先", "enabled": True},
                    {"key": "overview", "title": "目標其次", "enabled": True},
                ]
            },
        )
        db.add(t)
        db.flush()
        w = db.get(Workspace, project["workspace_id"])
        old = w.settings
        w.settings = {**old, "prd_template": t.id}
        db.commit()
    try:
        response = client.get(f"/api/prd-revisions/{rid}/export-template")
        assert response.status_code == 200
        import io

        texts = [p.text for p in Document(io.BytesIO(response.content)).paragraphs]
        assert next(i for i, t in enumerate(texts) if "範圍優先" in t) < next(
            i for i, t in enumerate(texts) if "目標其次" in t
        )
        with Session() as db:
            r = db.get(Revision, rid)
            assert r.content == content and r.presentation["id"] == "FINANCIAL_STANDARD"
            exported = render_docx(r)
        from pathlib import Path
        downloaded = client.get(f"/api/prd-revisions/{rid}/download")
        assert downloaded.status_code == 200 and downloaded.content == Path(exported).read_bytes()
    finally:
        with Session() as db:
            db.get(Workspace, project["workspace_id"]).settings = old
            db.commit()
