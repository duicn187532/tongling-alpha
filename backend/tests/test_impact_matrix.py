"""The PRD impact section maps each requirement to SA-sourced systems, interfaces and data."""

from app.prd_templates import builtins, canonical_v2, project
from app.technical_delivery import impact_matrix


def content(model=None):
    data = {"title": "撥薪通知", "background": "", "goals": "", "requirements": [
        {"id": "r1", "topic": "通知加密", "statement": "通知改為加密 PDF 附件", "evidence": [], "decision": {}},
        {"id": "r2", "topic": "密碼另送", "statement": "密碼以簡訊另送", "evidence": [], "decision": {}}]}
    if model is not None:
        data["technical_model"] = model
    return data


def item(iid, name, refs, **extra):
    return {"id": iid, "name": name, "description": name, "source_refs": ["sa-doc"], "requirement_refs": refs, **extra}


def test_matrix_lists_systems_interfaces_and_data_per_requirement():
    rows = impact_matrix(content({
        "system_components": [item("s1", "通知平台", ["r1"])],
        "interface_contracts": [item("i1", "簡訊發送", ["r2"], from_system="通知平台", to_system="簡訊閘道")],
        "data_changes": [item("d1", "寄送紀錄表", ["r1"]), item("d2", "批次暫存", [])]}))
    assert rows[0]["description"] == "系統：通知平台；資料：寄送紀錄表"
    assert rows[1]["description"] == "介面：通知平台→簡訊閘道（簡訊發送）"
    assert rows[0]["source_refs"] == ["sa-doc"]
    assert rows[-1]["title"] == "未對應到需求的技術項目" and "批次暫存" in rows[-1]["description"]


def test_without_technical_model_the_section_points_to_it_delivery_instead_of_guessing():
    assert impact_matrix(content()) is None
    data = canonical_v2(content())
    systems = next(s for s in project(data, builtins()[0])["sections"] if s["key"] == "systems")
    assert systems["paragraphs"][0].startswith("尚無 SA 技術模型")
    assert not any("可能受影響" in p for p in systems["paragraphs"])
