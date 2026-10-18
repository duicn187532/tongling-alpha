"""Deterministic, shared preview/export projection. Canonical data is never filtered."""

from sqlalchemy import select
from .db import PRDTemplate, Workspace

SECTIONS = {
    "control": "文件資訊與版本",
    "overview": "背景、目標與成功指標",
    "scope": "範圍、假設與依賴",
    "actors": "使用者、角色與權限",
    "requirements": "功能需求",
    "rules": "商業規則",
    "flows": "流程與例外處理",
    "controls": "風險、法遵與資安",
    "systems": "系統與介接影響",
    "data": "資料影響",
    "nfr": "非功能需求",
    "acceptance": "驗收條件",
    "decisions": "決策、未決事項與風險",
    "traceability": "需求追溯",
    "meetings": "附錄：會議時間軸",
    "peers": "附錄：同業分析",
    "glossary": "附錄：名詞表",
    "interfaces": "API／主機電文與介面",
    "batch": "批次與排程",
    "deployment": "部署與營運影響",
}
STANDARD = list(SECTIONS)[:14]
PRESETS = {
    "FINANCIAL_STANDARD": ("金融標準", STANDARD),
    "FINANCIAL_LIGHT": (
        "金融精簡",
        [
            "overview",
            "scope",
            "requirements",
            "rules",
            "flows",
            "systems",
            "acceptance",
            "decisions",
            "traceability",
        ],
    ),
    "IT_CHANGE": (
        "IT 變更",
        [
            "control",
            "overview",
            "scope",
            "requirements",
            "systems",
            "interfaces",
            "data",
            "batch",
            "flows",
            "nfr",
            "deployment",
            "acceptance",
            "traceability",
        ],
    ),
    "BUSINESS_ONLY": (
        "業務需求",
        [
            "overview",
            "scope",
            "actors",
            "requirements",
            "rules",
            "flows",
            "acceptance",
            "decisions",
            "traceability",
        ],
    ),
}


def builtins():
    return [
        {
            "id": key,
            "name": name,
            "built_in": True,
            "sections": [
                {"key": s, "title": SECTIONS[s], "enabled": True} for s in keys
            ],
        }
        for key, (name, keys) in PRESETS.items()
    ]


def validate_template(data):
    sections = data.get("sections", [])
    if not sections or len(sections) > len(SECTIONS):
        raise ValueError("請選擇至少一個章節")
    seen = set()
    for s in sections:
        if s.get("key") not in SECTIONS or s["key"] in seen:
            raise ValueError("章節代號無效或重複")
        seen.add(s["key"])
        if not str(s.get("title", "")).strip() or len(s["title"]) > 100:
            raise ValueError("章節標題需為 1–100 字")
    if not any(s.get("enabled", True) for s in sections):
        raise ValueError("至少顯示一個章節")
    return {
        "sections": [
            {
                "key": s["key"],
                "title": s["title"].strip(),
                "enabled": bool(s.get("enabled", True)),
            }
            for s in sections
        ]
    }


def chosen(db):
    config = db.scalar(select(Workspace)).settings
    key = config.get("prd_template", "FINANCIAL_STANDARD")
    for template in builtins():
        if template["id"] == key:
            return template
    row = db.get(PRDTemplate, key)
    return (
        {"id": row.id, "name": row.name, "built_in": False, **row.data}
        if row
        else builtins()[0]
    )


def canonical_v2(content):
    """Populate sourced fields conservatively; absence remains explicit, never inferred compliance."""
    import re

    reqs = content["requirements"]
    for r in reqs:
        r.setdefault("priority", "待確認")
        r.setdefault("actor", "待確認")
        r.setdefault("preconditions", [])
        r.setdefault("main_flow", [])
        r.setdefault("exceptions", [])
        r["evidence_refs"] = sorted({f["source_id"] for f in r["evidence"]})
        r.setdefault("business_rule_refs", [])
        r.setdefault("acceptance_refs", [])

    def matches(pattern, field):
        return [
            {
                "requirement_id": r["id"],
                "description": ("可能受影響，具體介面／電文／資料異動待 SA 依技術來源確認：" + r["statement"])
                               if field in {"api_message_impact", "data_impact", "batch_integration_impact"}
                               and not any(f.get("source_kind") == "TECH_SPEC" for f in r.get("evidence", []))
                               else r["statement"],
                **({"status": "SA_CONFIRMATION_REQUIRED" if not any(f.get("source_kind") == "TECH_SPEC" for f in r.get("evidence", []))
                              else "SOURCE_BACKED"}
                   if field in {"api_message_impact", "data_impact", "batch_integration_impact"} else {}),
                "source_refs": r["evidence_refs"],
            }
            for r in reqs
            if re.search(pattern, r["topic"] + " " + r["statement"], re.I)
        ]

    for field, pattern in {
        "business_rules": "限額|費用|費率|資格|條件|規則|rule|limit",
        "risk_compliance_security": "資安|風險|法遵|驗證|稽核|加密|個資|security|risk",
        "data_impact": "資料|保存|欄位|data|database",
        "nonfunctional_requirements": "效能|可用|延遲|秒|容量|復原|performance",
        "api_message_impact": "API|電文|介面|interface",
        "batch_integration_impact": "批次|排程|整合|batch",
    }.items():
        # Refresh requirement-derived rows when a new revision changes a decision.
        independent = [
            x
            for x in content.get(field, [])
            if not isinstance(x, dict) or not x.get("requirement_id")
        ]
        content[field] = independent + matches(pattern, field)
    content["decisions"] = [
        {
            "requirement_id": r["id"],
            "topic": r.get("topic", ""),
            "action": r["decision"].get("action"),
            "rationale": r["decision"].get("rationale", ""),
        }
        for r in reqs
    ]
    for field in [
        "success_metrics",
        "assumptions",
        "permissions",
        "exceptions",
        "dependencies",
        "deployment_impact",
        "glossary",
        "peer_industry_analysis",
    ]:
        content.setdefault(field, [])
    content["model_version"] = 2
    return content


def project(content, template, version="草稿"):
    sections = []
    fields = {
        "scope": ["out_of_scope", "assumptions", "dependencies"],
        "actors": ["actors", "permissions"],
        "rules": ["business_rules"],
        "controls": ["risk_compliance_security"],
        "systems": ["it_impact", "api_message_impact", "batch_integration_impact"],
        "data": ["data_impact"],
        "nfr": ["nonfunctional_requirements"],
        "acceptance": ["acceptance_criteria"],
        "decisions": ["decisions", "open_issues", "risks"],
        "interfaces": ["api_message_impact"],
        "batch": ["batch_integration_impact"],
        "deployment": ["deployment_impact"],
        "glossary": ["glossary"],
        "peers": ["peer_industry_analysis"],
    }

    def concise(item):
        if isinstance(item, str):
            return item
        return (
            item.get("description")
            or item.get("statement")
            or item.get("summary")
            or item.get("capability_or_practice")
            or " · ".join(filter(None, [
                item.get("topic") or str(item.get("requirement_id", "")),
                {"ACCEPT": "採納", "MODIFY": "修改後採納", "REJECT": "不納入",
                 "PENDING": "待確認"}.get(item.get("action"), str(item.get("action", ""))),
                item.get("rationale", ""),
            ]))
        )

    for s in template.get("sections", []):
        if not s.get("enabled", True):
            continue
        key = s["key"]
        block = {
            "key": key,
            "title": s["title"],
            "paragraphs": [],
            "rows": [],
            "diagrams": [],
        }
        if key == "control":
            block["paragraphs"] = [
                content["title"],
                f"版本：{version} · 結構：PRD v{content.get('model_version', 1)}",
            ]
        elif key == "overview":
            block["paragraphs"] = list(
                dict.fromkeys(
                    filter(None, [content.get("background"), content.get("goals")])
                )
            )
            block["paragraphs"] += [
                "成功指標：" + concise(x) for x in content.get("success_metrics", [])
            ]
            if content.get("change_analysis"):
                block["paragraphs"].append("本次變更分析：" + content["change_analysis"].get("summary", ""))
                block["paragraphs"] += ["現況：" + concise(x) for x in content.get("current_state", [])]
        elif key == "requirements":
            block["rows"] = [
                {
                    "id": "REQ-" + r["id"][:8],
                    "title": r["topic"],
                    "description": r["statement"],
                    "detail": f"優先級：{r.get('priority', '待確認')} · 角色：{r.get('actor', '待確認')} · 證據 {len(r.get('evidence', []))} 筆",
                    "source_refs": sorted(
                        {f["source_id"] for f in r.get("evidence", [])}
                    ),
                }
                for r in content["requirements"]
            ]
        elif key == "traceability":
            block["rows"] = [
                {
                    "id": "REQ-" + r["id"][:8],
                    "title": r["topic"],
                    "description": f"證據 {len(r.get('evidence', []))} 筆 · 人工決策：{r['decision'].get('action', '')}",
                    "source_refs": sorted(
                        {f["source_id"] for f in r.get("evidence", [])}
                    ),
                }
                for r in content["requirements"]
            ]
        elif key == "meetings":
            block["rows"] = [
                {
                    "id": m["meeting_date"],
                    "title": m["title"],
                    "description": "；".join(e["statement"] for e in m["events"]),
                }
                for m in content.get("meetings", [])
            ]
        elif key == "flows":
            block["diagrams"] = [
                d for d in content.get("diagrams", []) if d["type"] == "FUNCTIONAL"
            ]
            block["paragraphs"] = [concise(x) for x in content.get("exceptions", [])]
        if key in fields:
            block["paragraphs"] += [
                concise(x) for field in fields[key] for x in content.get(field, [])
            ]
        if key == "scope":
            block["paragraphs"].insert(
                0,
                f"本次納入 {len(content['requirements'])} 項已確認需求，詳見功能需求表；非範圍、假設與依賴列於下方。",
            )
            block["paragraphs"] += [
                f"{ {'ADD': '新增', 'MODIFY': '修改', 'REMOVE': '移除'}.get(item.get('action'), item.get('action', '待確認'))} · {item.get('before', '現況待確認')} → {item.get('after', '目標待確認')}"
                for item in content.get("change_scope", [])
            ]
        if key == "systems":
            block["diagrams"] = [
                d for d in content.get("diagrams", []) if d["type"] != "FUNCTIONAL"
            ]
        if not block["paragraphs"] and not block["rows"] and not block["diagrams"]:
            block["paragraphs"] = ["待補充並由負責人確認；目前來源沒有足夠資訊。"]
        sections.append(block)
    visible = {s["key"] for s in sections}
    warnings = [
        f"已隱藏治理章節：{SECTIONS[key]}；內部資料與人工關卡仍保留。"
        for key in ["traceability", "controls", "acceptance"]
        if key not in visible
    ]
    return {
        "sections": sections,
        "warnings": warnings,
        "template_name": template.get("name", "自訂"),
    }
