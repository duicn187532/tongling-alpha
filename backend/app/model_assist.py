"""Review-only AI candidates. No candidate can change a requirement or technical baseline."""

import hashlib
import json

from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import Link, Requirement, Source
from .evidence import search
from .model_runtime import enabled, structured
from .technical_delivery import TechnicalModel, AREAS


class Candidate(BaseModel):
    kind: str = Field(max_length=40)
    title: str = Field(max_length=160)
    suggestion: str = Field(max_length=2000)
    source_refs: list[str] = Field(min_length=1)
    requirement_refs: list[str] = Field(default_factory=list)
    uncertainty: str = Field(default="", max_length=500)


class Suggestions(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list, max_length=12)
    missing_information: list[str] = Field(default_factory=list, max_length=12)


class TechnicalDraft(BaseModel):
    technical_model: TechnicalModel = Field(default_factory=TechnicalModel)
    missing_information: list[str] = Field(default_factory=list, max_length=20)


def technical_candidate_issues(model):
    """Explain fields still needed before a candidate can pass the IT model gate."""
    issues = {}
    system_names = {item.name for item in model.system_components}
    for item in model.system_components:
        if "資料表" in item.description or item.name.endswith("表"):
            issues[f"system_components:{item.id}"] = ["請確認此項是系統元件，而非資料表"]
    for item in model.interface_contracts:
        gaps = []
        if item.from_system not in system_names or item.to_system not in system_names:
            gaps.append("來源與目標系統須先列入系統清單")
        if not item.before or not item.after:
            gaps.append("缺少電文／API 異動前後")
        if not item.field_diffs or any(not all(k in diff for k in ("field", "before", "after", "reason"))
                                       for diff in item.field_diffs):
            gaps.append("欄位差異需有 field、before、after、reason")
        if gaps:
            issues[f"interface_contracts:{item.id}"] = gaps
    for item in model.data_changes:
        gaps = [label for value, label in ((item.before, "異動前"), (item.after, "異動後"),
                                          (item.migration, "遷移方式"), (item.rollback, "回復方式")) if not value]
        if gaps:
            issues[f"data_changes:{item.id}"] = ["待補：" + "、".join(gaps)]
    for item in model.flow_definitions:
        if not item.steps:
            issues[f"flow_definitions:{item.id}"] = ["缺少正常處理步驟"]
    for item in model.business_rules:
        if not all((item.condition, item.on_true, item.on_false)):
            issues[f"business_rules:{item.id}"] = ["缺少判斷條件或是／否結果"]
    for item in model.acceptance_cases:
        if not item.expected:
            issues[f"acceptance_cases:{item.id}"] = ["缺少預期結果"]
    return issues


def linked_source_signature(db, project_id):
    rows = db.execute(select(Source.id, Source.sha256, Source.status).join(
        Link, Link.source_id == Source.id).where(Link.project_id == project_id)).all()
    return hashlib.sha256(json.dumps(sorted(tuple(row) for row in rows), ensure_ascii=False).encode()).hexdigest()[:16]


class Judgement(BaseModel):
    requirement_id: str
    recommendation: str
    explanation: str = Field(max_length=1000)
    source_refs: list[str] = Field(default_factory=list)


class Judgements(BaseModel):
    items: list[Judgement] = Field(default_factory=list)


class GraphExplanation(BaseModel):
    source_id: str
    explanation: str = Field(max_length=1000)
    cited_edge_ids: list[str] = Field(default_factory=list)
    uncertainty: str = Field(default="", max_length=500)


class GraphExplanations(BaseModel):
    items: list[GraphExplanation] = Field(default_factory=list)


def evidence_package(db, project_id):
    """References and provenance only: never copy source text into a second store."""
    if project_id:
        source_ids = set(db.scalars(select(Link.source_id).where(Link.project_id == project_id)))
    else:
        source_ids = set()
    package = []
    for row in db.scalars(select(Requirement).where(Requirement.project_id == project_id)):
        for fact in row.evidence or []:
            sid = fact.get("source_id")
            source = db.get(Source, sid) if sid in source_ids else None
            if not source or source.status != "ACTIVE":
                continue
            package.append({
                "requirement_id": row.id, "source_id": sid,
                "chunk_ids": fact.get("chunk_ids") or [],
                "source_kind": source.kind, "source_title": source.title,
                "source_status": source.status,
                "approved": source.meta.get("approved", "unknown"),
                "effective_at": source.meta.get("effective_at", "unknown"),
                "version": source.meta.get("version", "unknown"),
                "statement": fact.get("statement", "")[:1200],
            })
    return package


def judge_requirements(db, project, config):
    if not enabled(config, "agent_split"):
        return {"status": "OFF", "items": []}
    evidence = evidence_package(db, project.id)
    ids = {row.id for row in db.scalars(select(Requirement).where(Requirement.project_id == project.id))}
    allowed = {item["source_id"] for item in evidence}

    def verify(output):
        for item in output.items:
            if item.requirement_id not in ids or item.recommendation not in {"REVIEW", "NEEDS_EVIDENCE", "POSSIBLE_CONFLICT"}:
                raise ValueError("Judge 回傳不合法需求或建議")
            if set(item.source_refs) - allowed:
                raise ValueError("Judge 回傳不存在的來源")

    output = structured(config, "AGENT_EVIDENCE_JUDGEMENT",
        "你是金融需求證據審閱者，只能建議人工覆核、補充來源或標記可能衝突。不可批准、拒絕或改寫需求。來源狀態 unknown 不可視為已核准。",
        {"project_brief": project.description[:2000], "evidence": evidence[:40]},
        Judgements, lambda: {"items": []}, validator=verify, project_id=project.id)
    return {"status": "READY", "items": [x.model_dump() for x in output.items]}


def explain_graph(config, query, hits, project_id=None):
    if not enabled(config, "graph_reasoning") or not hits:
        return {}
    allowed = {h["source_id"]: {p.get("edge_id") for group in h["paths"] for p in group.get("paths", [])}
               for h in hits}

    def verify(output):
        for item in output.items:
            if item.source_id not in allowed or set(item.cited_edge_ids) - allowed[item.source_id]:
                raise ValueError("圖譜解釋引用未檢索到的來源或邊")

    output = structured(config, "GRAPH_REASONING",
        "依圖譜路徑解釋文件與查詢的可能關係及不確定性。只引用提供的 edge_id；不能建立或宣稱新的權威關係。",
        {"query": query, "graph_hits": hits[:5]}, GraphExplanations,
        lambda: {"items": []}, validator=verify, project_id=project_id)
    return {x.source_id: x.model_dump() for x in output.items}


def suggest_revision(db, revision, config, kind, selected_source_ids=None):
    flag = "prd_assist" if kind == "prd" else "architecture_routing"
    if not enabled(config, flag):
        raise ValueError("請先在主機設定頁啟用此項 AI 建議")
    source_ids = set(db.scalars(select(Link.source_id).where(Link.project_id == revision.project_id)))
    requirements = revision.content.get("requirements", [])
    requirement_ids = {r.get("id") for r in requirements}
    decided_ids = {r.get("id") for r in requirements
                   if (r.get("decision") or {}).get("action") in {"ACCEPT", "MODIFY", "REJECT"}}
    cited_source_ids = {fact.get("source_id") for row in requirements
                        for fact in row.get("evidence", []) if fact.get("source_id")}
    allowed_refs = source_ids & cited_source_ids if kind == "prd" else source_ids
    material = {
        "requirements": [{k: r.get(k) for k in ("id", "topic", "statement", "decision", "evidence")}
                         for r in requirements][:30],
        "decisions": revision.content.get("decisions") or [],
        "current_decisions": [{"requirement_id": r.get("id"), "topic": r.get("topic"),
                               "decision": r.get("decision")}
                              for r in requirements if r.get("id") in decided_ids][:30],
        "technical_model": revision.content.get("technical_model") or {},
        "current_gaps": [gap for gap in revision.content.get("open_issues") or []
                         if gap.get("id") not in decided_ids],
        "source_catalog": [{"source_id": sid, "title": source.title, "kind": source.kind,
                            "approved": source.meta.get("approved", "unknown")}
                           for sid in sorted(allowed_refs) if (source := db.get(Source, sid))][:30],
    }

    def verify(output):
        for item in output.candidates:
            if set(item.source_refs) - allowed_refs or set(item.requirement_refs) - requirement_ids:
                raise ValueError("建議引用未連結來源或不存在的需求")

    if kind == "architecture":
        if selected_source_ids is None:
            technical_ids = {sid for sid in source_ids if (source := db.get(Source, sid))
                             and source.status == "ACTIVE" and source.kind == "TECH_SPEC"}
        else:
            technical_ids = set(selected_source_ids)
            if technical_ids - source_ids or any(not (source := db.get(Source, sid)) or source.status != "ACTIVE"
                                                  for sid in technical_ids):
                raise ValueError("技術分析來源必須是本專案已連結的有效文件")
        if not technical_ids:
            return {"candidates": [], "technical_model": TechnicalModel().model_dump(),
                    "retrieved_evidence": [], "selected_source_ids": [],
                    "missing_information": ["尚未選取技術來源。請先檢索並連結 SA 文件，或上傳／輸入技術資料。"]}
        query = " ".join(["系統架構 電文 API 欄位 資料異動 流程 商業規則 驗收",
                          *[r.get("statement", "")[:180] for r in requirements[:8]]])[:1800]
        hits = search(db, query, limit=12, eligible_ids=technical_ids)
        if not hits:
            return {"candidates": [], "technical_model": TechnicalModel().model_dump(),
                    "retrieved_evidence": [], "selected_source_ids": sorted(technical_ids),
                    "missing_information": ["選取的技術來源沒有可檢索的內容，請檢查文件解析結果或補充文字。"]}
        allowed_chunks = {hit["chunk_id"]: hit["source_id"] for hit in hits}
        allowed_technical_sources = {hit["source_id"] for hit in hits}
        source_chunks = {sid: [cid for cid, owner in allowed_chunks.items() if owner == sid]
                         for sid in allowed_technical_sources}
        material["source_catalog"] = [item for item in material["source_catalog"]
                                      if item["source_id"] in allowed_technical_sources]
        material["retrieved_evidence"] = [
            {"source_id": hit["source_id"], "chunk_id": hit["chunk_id"],
             "title": hit["title"], "text": hit["text"][:1100]}
            for hit in hits]

        def verify_technical(output):
            item_ids = set()
            model = output.technical_model
            removed = 0
            def valid_refs(item):
                if not item.chunk_refs and len(item.source_refs) == 1:
                    chunks = source_chunks.get(item.source_refs[0], [])
                    if len(chunks) == 1:
                        item.chunk_refs = chunks
                return (bool(item.chunk_refs) and set(item.source_refs) <= allowed_technical_sources
                        and all(allowed_chunks.get(cid) in item.source_refs for cid in item.chunk_refs))

            for key in ("system_components", "interface_contracts", "data_changes",
                        "flow_definitions", "business_rules", "acceptance_cases"):
                kept = []
                for item in getattr(model, key):
                    if item.id in item_ids or not set(item.requirement_refs) <= requirement_ids or not valid_refs(item):
                        removed += 1
                        continue
                    item_ids.add(item.id)
                    kept.append(item)
                setattr(model, key, kept)
            kept_impacts = []
            seen_areas = set()
            for impact in model.technical_impact:
                if impact.area not in AREAS or impact.area in seen_areas or not valid_refs(impact):
                    removed += 1
                    continue
                seen_areas.add(impact.area)
                kept_impacts.append(impact)
            model.technical_impact = kept_impacts
            if removed:
                output.missing_information = (output.missing_information + [
                    f"已排除 {removed} 項缺少有效來源／原文段落或引用重複的 AI 候選；請補充 SA 證據。"])[-20:]

        output = structured(config, "ARCHITECTURE_IMPACT",
            "你是金融系統分析師。只根據 retrieved_evidence 中的原文建立可編輯的技術資料候選；每項填真實 source_id 和至少一個對應 chunk_id，必要時填 requirement_id。"
            "沒有明示的系統名稱、電文欄位、異動前後、資料表、例外流程不可推測；留空並列入 missing_information。"
            "不可把業務需求直接當作 SA 技術事實；已作成的 UAT 決策也不是正式銀行政策。"
            "只有原文足以填齊介面異動前後與欄位差異時才建立 interface_contracts；資料異動同理。"
            "各項 status 填 CANDIDATE，絕不宣稱已確認。",
            material, TechnicalDraft,
            lambda: {"technical_model": TechnicalModel().model_dump(),
                     "missing_information": ["規則模式無法產生 AI 技術資料"]},
            validator=verify_technical, project_id=revision.project_id,
            timeout_seconds=180, max_tokens=3500)
        result = output.model_dump()
        for key in ("system_components", "interface_contracts", "data_changes",
                    "flow_definitions", "business_rules", "acceptance_cases"):
            for item in result["technical_model"][key]:
                item["status"] = "CANDIDATE"
        result.update({"candidates": [], "selected_source_ids": sorted(technical_ids),
                       "validation_issues": technical_candidate_issues(output.technical_model),
                       "retrieved_evidence": [{"source_id": hit["source_id"],
                                               "chunk_id": hit["chunk_id"], "title": hit["title"],
                                               "text": hit["text"][:700]}
                                              for hit in hits]})
        return result

    instruction = ("提出驗收條件、需求章節、品質缺漏的候選建議；每項必須引用提供的真實 source_id。"
                   "已接受或人工修改的 decision 是本專案目前決策；不要把來源中的舊 OPEN_ITEM 誤判為仍未決。"
                   "若人工決策明示僅供 UAT，仍須保留其測試限定，不宣稱為正式銀行政策。"
                   "沒有來源的想法放在 missing_information，不列為候選。不可改寫已確認需求或定版內容。"
                   )
    output = structured(config, "PRD_ASSIST",
        instruction, material, Suggestions,
        lambda: {"candidates": [], "missing_information": ["規則模式無法產生 AI 建議"]},
        validator=verify, project_id=revision.project_id, timeout_seconds=180, max_tokens=3000)
    return output.model_dump()
