"""Deterministic IT handoff gate; independent from the display template."""

from .technical_delivery import TechnicalModel, AREAS


def assess(revision, reviews=(), db=None):
    content = revision.content or {}
    gaps = []

    def gap(code, message, owner_role, item_id=None):
        gaps.append({"code": code, "message": message, "owner_role": owner_role,
                     "item_id": item_id, "severity": "BLOCKING"})

    requirements = content.get("requirements") or []
    if not requirements:
        gap("NO_REQUIREMENTS", "尚無已確認的功能需求。", "PM")
    try:
        model = TechnicalModel.model_validate(content.get("technical_model") or {})
    except Exception:
        model = TechnicalModel()
        gap("INVALID_TECHNICAL_MODEL", "技術模型格式不正確。", "SA")
    if db is not None and content.get("technical_model"):
        from .technical_delivery import validate_model
        try:
            validate_model(db, revision, model)
        except ValueError as exc:
            gap("INVALID_TECHNICAL_SOURCE", str(exc), "SA")
    acceptance = model.acceptance_cases
    for item in requirements:
        rid = item.get("id")
        title = item.get("topic") or item.get("statement") or rid or "未命名需求"
        if not item.get("evidence"):
            gap("MISSING_EVIDENCE", f"{title} 缺少可定位的來源。", "PM", rid)
        if item.get("decision", {}).get("action") not in {"ACCEPT", "MODIFY"}:
            gap("MISSING_DECISION", f"{title} 尚未完成採納決策。", "PM", rid)
        if not any(rid in case.requirement_refs and case.expected for case in acceptance):
            gap("MISSING_ACCEPTANCE", f"{title} 缺少有預期結果的驗收案例。", "PM / QA", rid)

    impact_by_area = {x.area: x for x in model.technical_impact}
    for key, label, area in (("system_components", "系統責任與架構", "SYSTEM"),
                             ("interface_contracts", "電文／API before/after 與欄位差異", "INTERFACE"),
                             ("data_changes", "資料異動／遷移", "DATA"),
                             ("flow_definitions", "處理順序及例外流程", "FLOW"),
                             ("business_rules", "商業規則", None)):
        if not getattr(model, key) and (area is None or area not in impact_by_area or impact_by_area[area].affected):
            gap("MISSING_" + key.upper(), f"缺少已確認的{label}。若無影響，仍須記錄判斷依據。", "SA")
    for area in sorted(AREAS):
        if not any(x.area == area for x in model.technical_impact):
            gap("MISSING_IMPACT", f"{area} 尚未記錄 YES／NO 及理由。", "SA", area)
    for kind, label in (("BUSINESS_RULE_FLOW", "商業規則圖"),
                        ("SYSTEM_SEQUENCE", "系統循序圖"),
                        ("SYSTEM_ARCHITECTURE", "系統架構圖")):
        if not any(d.get("type") == kind and d.get("status") == "READY"
                   and d.get("generated_from") == "technical_model" and d.get("source_refs")
                   for d in content.get("diagrams", [])):
            gap("MISSING_" + kind, f"{label}未由技術模型生成並驗證。", "SA")
    for role in ("PM", "SA", "QA"):
        if not any(r.get("role") == role and r.get("content_hash") == revision.content_hash for r in reviews):
            gap("MISSING_REVIEW", f"缺少針對本版內容的 {role} 審閱。", role, role)
    return {"purpose": "IT_HANDOFF", "revision_id": revision.id,
            "revision_hash": revision.content_hash, "readiness": "BLOCKED" if gaps else "READY",
            "blocking_gaps": gaps, "blocking_count": len(gaps),
            "message": "IT 交付仍須補足缺口。" if gaps else "IT 交付檢查通過，可建立交付包。"}
