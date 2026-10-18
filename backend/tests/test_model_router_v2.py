import httpx
import pytest
from pydantic import BaseModel

from app import model_runtime as runtime


class Answer(BaseModel):
    value: str


def config(model="small", **router):
    return {
        "provider": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "old",
        "local_only": True, "model_concurrency": 1,
        "model_router": {
            "schema_version": 2, "mode": "active",
            "inference_policy": {"enabled": True},
            "role_to_model": {"worker": {"model": model}},
            **router,
        },
    }


def fake_client(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(runtime.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    runtime._registry.clear()


def test_active_router_obeys_capabilities_and_thinking(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "thinking"],
                                               "thinking": {"values": [False, "low"]}})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "small", "digest": "sha256:test"}]})
        return httpx.Response(200, json={"message": {"content": '{"value":"ok"}'}, "eval_count": 3})

    fake_client(monkeypatch, handler)
    cfg = config(policy_by_task={"TAG_EXTRACTION": {"think": "low", "num_predict": 256}})
    result = runtime.structured(cfg, "TAG_EXTRACTION", "test", {}, Answer, lambda: {})
    assert result.value == "ok"
    chat = [r for r in requests if r.url.path == "/api/chat"][0].read()
    assert b'"think":"low"' in chat and b'"num_predict":256' in chat
    assert runtime.model_info(runtime.route(cfg, "TAG_EXTRACTION"))["digest"] == "sha256:test"


def test_embedding_model_cannot_be_used_for_text(monkeypatch):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["embedding"]})
        return httpx.Response(200, json={"models": []})

    fake_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="CAPABILITY_MISMATCH"):
        runtime.structured(config(), "TAG_EXTRACTION", "test", {}, Answer, lambda: {})


def test_output_exhaustion_retries_once_without_thinking(monkeypatch):
    chats = []

    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "thinking"],
                                               "thinking": {"values": [False, True]}})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        chats.append(request)
        if len(chats) == 1:
            return httpx.Response(200, json={"message": {"content": "", "thinking": "hidden"},
                                               "done_reason": "length"})
        return httpx.Response(200, json={"message": {"content": '{"value":"recovered"}'}})

    fake_client(monkeypatch, handler)
    cfg = config(policy_by_task={"TAG_EXTRACTION": {"think": True, "num_predict": 128}})
    assert runtime.structured(cfg, "TAG_EXTRACTION", "test", {}, Answer, lambda: {}).value == "recovered"
    assert len(chats) == 2
    assert b'"think":false' in chats[1].read()


def test_gated_decision_requires_calibration():
    with pytest.raises(ValueError, match="校準"):
        runtime.validate_router(config(decision={"mode": "gated"}))


def test_model_resource_wait_has_a_deadline(monkeypatch):
    monkeypatch.setattr(runtime, "active", 1)
    monkeypatch.setattr(runtime, "resource_limit", 1)
    with pytest.raises(TimeoutError, match="等待逾時"):
        runtime.acquire({"model_concurrency": 1}, timeout_seconds=0.01)
    assert runtime.active == 1


def test_completed_agent_card_is_not_cancelled_by_next_decision(project):
    from app import activity
    from app.db import Session, Run, Activity

    with Session.begin() as db:
        run = Run(project_id=project["id"], epoch=1, reason="UAT", status="SUCCEEDED")
        db.add(run)
        db.flush()
        db.add(Activity(id=run.id, project_id=project["id"], title="Agent 工作流程",
                        status="RUNNING"))
        run_id = run.id
    with Session.begin() as db:
        activity.cancel_project(db, project["id"])
    activity.finish(run_id, "SUCCEEDED", "分析完成")
    with Session() as db:
        card = db.get(Activity, run_id)
        assert card.status == "SUCCEEDED"
        assert card.phase == "已完成"


def test_bounded_fallback_uses_compatible_backup_once(monkeypatch):
    calls = []

    def handler(request):
        if request.url.path == "/api/show":
            model = request.read().decode()
            if "missing" in model:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "backup", "digest": "sha256:backup"}]})
        calls.append(request)
        return httpx.Response(200, json={"message": {"content": '{"value":"backup"}'}})

    fake_client(monkeypatch, handler)
    cfg = config("missing", escalation={"enabled": True},
                 fallback_chain={"worker": "backup"})
    assert runtime.structured(cfg, "TAG_EXTRACTION", "test", {}, Answer, lambda: {}).value == "backup"
    assert len(calls) == 1 and b'"model":"backup"' in calls[0].read()


def test_decision_adapter_advises_without_changing_requirement(monkeypatch):
    from types import SimpleNamespace
    from app.decision_provider import advise_project

    requirement = SimpleNamespace(id="req-1", statement="申請何時生效仍未決議",
                                  status="OPEN", decision={}, evidence=[])
    project = SimpleNamespace(id="project-1")

    class FakeDB:
        def scalars(self, _):
            return [requirement]

    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["decision"]})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "d1:latest", "digest": "sha256:d1"}]})
        assert request.url.path == "/v1/systemone"
        return httpx.Response(200, json={"answers": {"human_review": {"type": "noul", "noul": 0.9}}})

    fake_client(monkeypatch, handler)
    cfg = config(decision={"mode": "advisory", "model": "d1:latest"})
    result = advise_project(FakeDB(), project, cfg)
    assert result["items"][0]["human_review_score"] == 0.9
    assert requirement.decision == {}


def test_assist_does_not_reopen_uat_decision(monkeypatch):
    from types import SimpleNamespace
    from app import model_assist

    captured = {}
    def fake_structured(_config, _task, instruction, material, *_args, **_kwargs):
        captured.update({"instruction": instruction, "material": material})
        return model_assist.Suggestions()

    monkeypatch.setattr(model_assist, "structured", fake_structured)
    revision = SimpleNamespace(project_id="p", content={
        "requirements": [{"id": "r1", "topic": "生效時間", "statement": "下一帳單週期生效",
                          "decision": {"action": "MODIFY", "statement": "下一帳單週期生效",
                                       "rationale": "僅供 UAT，不是正式銀行政策"}, "evidence": []}],
        "open_issues": [{"id": "r1", "statement": "下一帳單週期生效"}],
    })
    db = SimpleNamespace(scalars=lambda _query: [])
    result = model_assist.suggest_revision(
        db, revision, config(prd_assist={"enabled": True}), "prd")
    assert result["candidates"] == []
    assert captured["material"]["current_gaps"] == []
    assert captured["material"]["current_decisions"][0]["decision"]["action"] == "MODIFY"
    assert "UAT" in captured["instruction"]


def test_technical_assist_uses_retrieved_project_source_only(monkeypatch):
    from types import SimpleNamespace
    from app import model_assist
    from app.technical_delivery import TechnicalModel, Item

    source = SimpleNamespace(id="sa-1", kind="TECH_SPEC", status="ACTIVE", title="SA 設計",
                             meta={"approved": "unknown"})
    class FakeDB:
        def scalars(self, _query):
            return ["sa-1"]
        def get(self, _model, id):
            return source if id == "sa-1" else None

    captured = {}
    monkeypatch.setattr(model_assist, "search", lambda _db, query, **kw: [
        {"source_id": "sa-1", "chunk_id": "chunk-1", "title": "SA 設計",
         "text": "網銀前端呼叫申請 API，後端驗證同意註記。"}])
    def fake_structured(_config, _task, instruction, material, _schema, _fallback, **kwargs):
        captured.update({"instruction": instruction, "material": material})
        result = model_assist.TechnicalDraft(technical_model=TechnicalModel(
            system_components=[Item(id="SYS-1", name="網銀前端", description="接收客戶申請",
                                    source_refs=["sa-1"], requirement_refs=["r1"]),
                               Item(id="SYS-2", name="虛構系統", description="不應採用",
                                    source_refs=["not-linked"], requirement_refs=["r1"])]))
        kwargs["validator"](result)
        return result
    monkeypatch.setattr(model_assist, "structured", fake_structured)
    revision = SimpleNamespace(project_id="p", content={
        "requirements": [{"id": "r1", "topic": "申請", "statement": "客戶申請電子帳單",
                          "decision": {"action": "ACCEPT"}, "evidence": []}], "open_issues": []})
    result = model_assist.suggest_revision(FakeDB(), revision,
        config(architecture_routing={"enabled": True}), "architecture", ["sa-1"])
    assert result["technical_model"]["system_components"][0]["status"] == "CANDIDATE"
    assert len(result["technical_model"]["system_components"]) == 1
    assert result["technical_model"]["system_components"][0]["chunk_refs"] == ["chunk-1"]
    assert "已排除 1 項" in result["missing_information"][-1]
    assert result["retrieved_evidence"][0]["chunk_id"] == "chunk-1"
    assert captured["material"]["retrieved_evidence"][0]["source_id"] == "sa-1"
    assert captured["material"]["current_decisions"][0]["decision"]["action"] == "ACCEPT"
    with pytest.raises(ValueError, match="本專案"):
        model_assist.suggest_revision(FakeDB(), revision,
            config(architecture_routing={"enabled": True}), "architecture", ["other-project-source"])


def test_technical_candidate_flags_incomplete_interface_and_data():
    from app.model_assist import technical_candidate_issues
    from app.technical_delivery import TechnicalModel, Item, Interface, DataChange

    model = TechnicalModel(
        system_components=[Item(id="SYS-1", name="網銀", description="前端系統", source_refs=["s"])],
        interface_contracts=[Interface(id="IF-1", name="申請電文", description="發送申請",
                                       source_refs=["s"], from_system="網銀", to_system="帳務主機",
                                       before="無", after="新增", field_diffs=[{"field": "id"}])],
        data_changes=[DataChange(id="DATA-1", name="申請表", description="新增資料表",
                                 source_refs=["s"], before="無", after="新增")],
    )
    issues = technical_candidate_issues(model)
    assert "來源與目標系統" in issues["interface_contracts:IF-1"][0]
    assert any("欄位差異" in gap for gap in issues["interface_contracts:IF-1"])
    assert "遷移方式" in issues["data_changes:DATA-1"][0]
