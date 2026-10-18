import copy
import hashlib
import json
import subprocess
import shutil
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from . import prd_templates
from .db import (
    Requirement,
    Revision,
    Change,
    Meeting,
    Interruption,
    DATA,
    ROOT,
    now,
    log,
)


class PRDModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    title: str
    background: str
    goals: str
    requirements: list[dict]
    open_issues: list[dict] = Field(default_factory=list)
    meetings: list[dict] = Field(default_factory=list)
    traceability: list[dict] = Field(default_factory=list)
    diagrams: list[dict] = Field(default_factory=list)
    it_impact: list[dict] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    acceptance_criteria: list[dict] = Field(default_factory=list)
    risks: list[dict] = Field(default_factory=list)


def content_hash(content):
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def human_gate(db, project):
    if project.state.get("analysis_stale", True):
        raise ValueError("證據已變動，請先重新分析")
    rows = list(
        db.scalars(select(Requirement).where(Requirement.project_id == project.id))
    )
    if not rows:
        raise ValueError("尚無需求可形成 PRD")
    pending = [
        r
        for r in rows
        if r.decision.get("action") not in {"ACCEPT", "MODIFY", "REJECT"}
    ]
    if pending:
        raise ValueError(f"仍有 {len(pending)} 項需求待人工決策")
    for row in rows:
        if row.decision["action"] != "REJECT" and not row.evidence:
            raise ValueError("無證據的需求不可形成正式基準")
    if not any(
        r.decision["action"] != "REJECT"
        and r.status != "EXCLUDED"
        and r.decision.get("category", "FUNCTIONAL") != "SCOPE_EXCLUSION"
        for r in rows
    ):
        raise ValueError("沒有採用的需求")
    return rows


def interruption(db, project, kind, title, summary):
    existing = db.scalar(
        select(Interruption).where(
            Interruption.project_id == project.id,
            Interruption.kind == kind,
            Interruption.status != "RESOLVED",
        )
    )
    if existing:
        existing.status = "OPEN"
        return existing
    row = Interruption(project_id=project.id, kind=kind, title=title, summary=summary)
    db.add(row)
    return row


def resolve_interruptions(db, project, kinds):
    for row in db.scalars(
        select(Interruption).where(
            Interruption.project_id == project.id, Interruption.kind.in_(kinds)
        )
    ):
        row.status = "RESOLVED"


def diff_requirements(before, after):
    old = {r["id"]: r for r in before}
    new = {r["id"]: r for r in after}
    result = []
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            result.append(
                {
                    "requirement_id": key,
                    "section": "requirements",
                    "before": old.get(key),
                    "after": new.get(key),
                    "source_refs": sorted(
                        {f["source_id"] for f in new.get(key, {}).get("evidence", [])}
                    ),
                    "reason": "人工需求決策與來源證據更新",
                }
            )
    return result


def diagram_sources(requirements):
    def safe(text):
        return (
            text.replace('"', "＂")
            .replace("<", "＜")
            .replace(">", "＞")
            .replace("\n", " ")[:90]
        )

    nodes = [f'R{i}["{safe(r["topic"])}"]' for i, r in enumerate(requirements)]
    functional = (
        "flowchart TD\n"
        + "\n".join(nodes)
        + '\nG["已確認需求範圍（順序待設計）"]\n'
        + "\n".join(f"G --> R{i}" for i in range(len(nodes)))
    )
    # No fabricated system topology: visualize evidence relationships until technical design is supplied.
    system = (
        'flowchart LR\nS["來源證據"] --> R["已確認需求"] --> I["系統互動待 SA 確認"]'
    )
    architecture = 'flowchart TD\nA["需求範圍"] --> B["系統邊界待確認"]\nB --> C["介面與資料架構待技術來源補齊"]'
    return [
        {
            "type": "FUNCTIONAL",
            "title": "功能範圍圖",
            "mermaid": functional,
            "warning": "此圖不表示已確認的執行先後順序",
        },
        {
            "type": "SYSTEM",
            "title": "系統互動圖",
            "mermaid": system,
            "warning": "架構待 SA 補充，不推測系統名稱",
        },
        {
            "type": "ARCHITECTURE",
            "title": "架構邊界圖",
            "mermaid": architecture,
            "warning": "架構待確認",
        },
    ]


def create_draft(db, project):
    rows = human_gate(db, project)
    existing = (
        db.get(Revision, project.state.get("draft_id"))
        if project.state.get("draft_id")
        else None
    )
    if existing and existing.status in {"DRAFT", "IN_REVIEW"}:
        return existing
    changes = list(
        db.scalars(
            select(Change).where(
                Change.project_id == project.id, Change.status.in_(["OPEN", "REVIEWED"])
            )
        )
    )
    if any(c.status == "OPEN" for c in changes):
        raise ValueError("請先審查變更影響")
    parent = (
        db.get(Revision, project.current_revision_id)
        if project.current_revision_id
        else None
    )
    accepted = [
        r for r in rows if r.decision.get("action") in {"ACCEPT", "MODIFY"}
    ]
    requirements = [
        {
            "id": r.id,
            "topic": r.topic,
            "statement": r.decision.get("statement", r.statement),
            "status": r.status,
            "decision": r.decision,
            "evidence": r.evidence,
        }
        for r in accepted
        if r.status != "EXCLUDED" and r.decision.get("category", "FUNCTIONAL") == "FUNCTIONAL"
    ]
    if parent and not changes:
        raise ValueError("目前沒有已核准的變更請求")
    content = (
        copy.deepcopy(parent.content)
        if parent
        else PRDModel(
            title=project.name,
            background=project.description,
            goals=project.description,
            requirements=[],
        ).model_dump()
    )
    # Preserve every unrelated section exactly; update requirement rows and traceability only.
    content["requirements"] = requirements
    content["scope"] = [
        r["statement"] for r in requirements if r["status"] != "EXCLUDED"
    ]
    content["out_of_scope"] = [
        r.decision.get("statement", r.statement)
        for r in rows
        if (r.status == "EXCLUDED" or r.decision.get("category") == "SCOPE_EXCLUSION")
        and r.decision.get("action") in {"ACCEPT", "MODIFY"}
    ]
    content["open_issues"] = [
        {
            "id": r["id"],
            "statement": r["statement"],
            "rationale": r["decision"].get("rationale", ""),
        }
        for r in requirements
        if r["status"] == "OPEN"
    ] + [
        {
            "id": r.id,
            "statement": r.decision.get("statement", r.statement),
            "rationale": r.decision.get("rationale", ""),
            "source_refs": sorted({f["source_id"] for f in r.evidence}),
        }
        for r in accepted
        if r.decision.get("category") == "OPEN_ISSUE"
    ]
    content["risks"] = [
        {
            "requirement_id": r["id"],
            "description": "原始來源存在分歧，請維持決策追溯",
            "decision": r["decision"],
        }
        for r in requirements
        if r["status"] == "CONFLICT"
    ]
    content["traceability"] = [
        {
            "requirement_id": r["id"],
            "source_id": f["source_id"],
            "chunk_ids": f["chunk_ids"],
        }
        for r in requirements
        for f in r["evidence"]
    ]
    content["meetings"] = [
        {
            "id": m.id,
            "title": m.title,
            "meeting_date": m.meeting_date,
            "events": m.events,
        }
        for m in db.scalars(
            select(Meeting)
            .where(Meeting.project_id == project.id, Meeting.status == "CONFIRMED")
            .order_by(Meeting.meeting_date, Meeting.started_at)
        )
    ]
    if not parent:
        content["diagrams"] = diagram_sources(requirements)
        technical = [
            {
                "status": "SOURCE_BACKED",
                "description": f["statement"],
                "source_id": f["source_id"],
            }
            for r in requirements
            for f in r["evidence"]
            if f["kind"] == "SYSTEM_CAPABILITY"
        ]
        content["it_impact"] = technical or [
            {
                "status": "OPEN",
                "description": "請依技術來源確認系統、介面與資料影響；本系統不自動推測。",
            }
        ]
    else:
        affected_ids = {
            rid for c in changes for rid in c.impact.get("affected_requirement_ids", [])
        }
        if any(
            r["id"] in affected_ids
            and any(f["kind"] == "SYSTEM_CAPABILITY" for f in r["evidence"])
            for r in requirements
        ):
            content["it_impact"] = [
                {
                    "status": "SOURCE_BACKED",
                    "description": f["statement"],
                    "source_id": f["source_id"],
                }
                for r in requirements
                for f in r["evidence"]
                if f["kind"] == "SYSTEM_CAPABILITY"
            ]
        if {r["topic"] for r in requirements} != {
            r["topic"] for r in parent.content["requirements"]
        }:
            content["diagrams"][0] = diagram_sources(requirements)[0]
    content = prd_templates.canonical_v2(content)
    content["business_rules"] += [
        {
            "requirement_id": r.id,
            "description": r.decision.get("statement", r.statement),
            "source_refs": sorted({f["source_id"] for f in r.evidence}),
            "decision": r.decision,
        }
        for r in accepted
        if r.decision.get("category") == "BUSINESS_RULE"
    ]
    content["decisions"] += [
        {
            "requirement_id": r.id,
            "description": r.decision.get("statement", r.statement),
            "source_refs": sorted({f["source_id"] for f in r.evidence}),
            "action": r.decision.get("action"),
            "rationale": r.decision.get("rationale", ""),
        }
        for r in accepted
        if r.decision.get("category") == "DECISION_CONTEXT"
    ]
    from .db import ResearchRun

    research = db.scalar(
        select(ResearchRun)
        .where(ResearchRun.project_id == project.id, ResearchRun.status == "COMPLETED")
        .order_by(ResearchRun.created_at.desc())
    )
    if research:
        content["peer_industry_analysis"] = [
            {
                "description": f["capability_or_practice"],
                "source_refs": f["source_refs"],
                "role": "EXTERNAL_BENCHMARK",
                "source_url": f["source_url"],
            }
            for f in research.data.get("findings", [])
        ]
    differences = diff_requirements(
        parent.content["requirements"] if parent else [], requirements
    )
    all_revisions = list(
        db.scalars(select(Revision).where(Revision.project_id == project.id))
    )
    if parent:
        version = "v1." + str(
            max(
                [
                    int(r.version.split(".")[1])
                    for r in all_revisions
                    if r.version.startswith("v1.")
                ]
                + [0]
            )
            + 1
        )
    else:
        version = "v0." + str(len(all_revisions) + 1)
    row = Revision(
        presentation=prd_templates.chosen(db),
        project_id=project.id,
        parent_revision_id=parent.id if parent else None,
        version=version,
        content=PRDModel.model_validate(content).model_dump(),
        content_hash=content_hash(PRDModel.model_validate(content).model_dump()),
        diff=differences,
        changes=[c.id for c in changes],
        status="IN_REVIEW",
    )
    db.add(row)
    db.flush()
    project.state = {
        **project.state,
        "draft_id": row.id,
        "progress": max(90, project.state.get("progress", 0)),
        "step": 8,
    }
    project.runtime = "WAITING_HUMAN"
    resolve_interruptions(db, project, ["REQUIREMENT_REVIEW", "CHANGE_REVIEW"])
    interruption(
        db,
        project,
        "FINAL_PRD_REVIEW_REQUIRED",
        "PRD 草稿已備妥，請完成最後審閱",
        "確認需求、來源與圖表後才能建立不可變更的正式版本。你可以先預覽或稍後處理。",
    )
    log(
        db,
        project.id,
        "PRD_DRAFT",
        f"產生 {version} 草稿",
        {"revision_id": row.id, "changed_requirements": len(differences)},
    )
    return row


def create_technical_amendment(db, project, parent):
    """Create a new version for source-backed SA additions without changing business scope."""
    if parent.id != project.current_revision_id or parent.status != "BASELINED":
        raise ValueError("只能對目前定版版本建立技術補件")
    existing = db.get(Revision, project.state.get("draft_id")) if project.state.get("draft_id") else None
    if existing and existing.status in {"DRAFT", "IN_REVIEW"}:
        return existing
    all_revisions = list(db.scalars(select(Revision).where(Revision.project_id == project.id)))
    minor = max([int(r.version.split(".")[1]) for r in all_revisions
                 if r.version.startswith("v1.") and r.version.split(".")[1].isdigit()] + [0]) + 1
    content = copy.deepcopy(parent.content)
    content["technical_amendment"] = True
    row = Revision(project_id=project.id, parent_revision_id=parent.id,
                   version=f"v1.{minor}", status="IN_REVIEW", content=content,
                   content_hash=content_hash(content), diff=[], changes=[],
                   presentation=copy.deepcopy(parent.presentation or {}))
    db.add(row)
    db.flush()
    project.state = {**project.state, "draft_id": row.id, "step": 8, "progress": 90}
    project.runtime = "WAITING_HUMAN"
    log(db, project.id, "TECHNICAL_AMENDMENT", "建立技術補件草稿；原業務基準保持不變", {"revision_id": row.id})
    return row


def render_docx(revision, content=None, version=None):
    from docx import Document
    from docx.shared import Inches, Pt

    content = content or revision.content
    folder = DATA / "exports" / revision.id
    folder.mkdir(parents=True, exist_ok=True)
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Microsoft JhengHei", Pt(10)
    doc.add_heading(content["title"], 0)
    doc.add_paragraph(f"產品需求文件 · {version or revision.version}")
    doc.add_paragraph("用途：業務需求基準。技術架構、電文／API、資料異動與驗收仍須通過 IT 交付檢查；本文件不可單獨視為已核准的開發設計。")
    doc.add_paragraph(
        f"文件快照：{revision.content_hash}\n建立時間：{revision.created_at}"
    )
    projection = prd_templates.project(
        content,
        revision.presentation or prd_templates.builtins()[0],
        version or revision.version,
    )
    diagram_index = 0
    for warning in projection["warnings"]:
        doc.add_paragraph(warning)
    for number, section in enumerate(projection["sections"], 1):
        doc.add_heading(f"{number}. {section['title']}", 1)
        for paragraph in section["paragraphs"]:
            doc.add_paragraph(paragraph)
        if section["rows"]:
            table = doc.add_table(rows=1, cols=3)
            table.style = "Light Shading Accent 1"
            for cell, title in zip(table.rows[0].cells, ["編號", "項目", "內容與追溯"]):
                cell.text = title
            for row in section["rows"]:
                cells = table.add_row().cells
                cells[0].text = row.get("id", "")
                cells[1].text = row.get("title", "")
                cells[2].text = row.get("description", "") + (
                    "\n" + row["detail"] if row.get("detail") else ""
                )
                if row.get("source_refs"):
                    cells[2].text += "\n來源：" + ", ".join(row["source_refs"])
        for diagram in section["diagrams"]:
            source = diagram["mermaid"]
            if len(source) > 20000 or re_unsafe(source):
                raise ValueError("圖表包含不允許的指令")
            cli = ROOT / "frontend/node_modules/@mermaid-js/mermaid-cli/src/cli.js"
            node = shutil.which("node")
            if not cli.exists() or not node:
                raise ValueError("缺少 Mermaid CLI 或 Node.js")
            mmd, png = (
                folder / f"diagram-{diagram_index}.mmd",
                folder / f"diagram-{diagram_index}.png",
            )
            diagram_index += 1
            mmd.write_text(source, encoding="utf-8")
            proc = subprocess.run(
                [
                    node,
                    str(cli),
                    "-i",
                    str(mmd),
                    "-o",
                    str(png),
                    "-b",
                    "white",
                    "-s",
                    "2",
                ],
                capture_output=True,
                timeout=90,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode or not png.exists():
                raise ValueError("Mermaid 圖表渲染失敗：" + proc.stderr[-1200:])
            doc.add_heading(diagram["title"], 2)
            if diagram.get("warning"):
                doc.add_paragraph(diagram["warning"])
            doc.add_picture(str(png), width=Inches(6))
    technical = content.get("technical_model")
    if technical:
        doc.add_heading("IT 技術設計附件", 1)
        doc.add_paragraph("此附件記錄來源支持的技術模型。正式 IT 交付仍以獨立交付檢查、同版審閱與交付包為準。")
        for key, heading in (("system_components", "系統與責任"),
                             ("interface_contracts", "電文／API before/after 與欄位差異"),
                             ("data_changes", "資料異動"),
                             ("flow_definitions", "處理與例外流程"),
                             ("business_rules", "商業規則"),
                             ("acceptance_cases", "驗收案例")):
            doc.add_heading(heading, 2)
            for item in technical.get(key, []):
                detail = [item.get("description", "")]
                for field, label in (("from_system", "來源系統"), ("to_system", "目標系統"),
                                     ("before", "異動前"), ("after", "異動後"),
                                     ("migration", "遷移方式"), ("rollback", "回復方式"),
                                     ("condition", "判斷條件"), ("on_true", "成立時"),
                                     ("on_false", "不成立時"),
                                     ("expected", "預期結果")):
                    if item.get(field):
                        detail.append(f"{label}：{item[field]}")
                for diff in item.get("field_diffs", []):
                    detail.append("欄位 {field}：{before} → {after}；原因：{reason}".format(**diff))
                for field, label in (("steps", "正常流程"), ("exception_steps", "例外流程")):
                    if item.get(field):
                        detail.append(f"{label}：" + "；".join(item[field]))
                detail.append("需求：" + ", ".join(item.get("requirement_refs", [])))
                detail.append("來源：" + ", ".join(item.get("source_refs", [])))
                doc.add_paragraph(f"{item.get('id', '')} {item.get('name', '')}\n" + "\n".join(detail))
        doc.add_heading("技術影響判斷", 2)
        for item in technical.get("technical_impact", []):
            doc.add_paragraph(f"{item['area']}：{'YES' if item['affected'] else 'NO'}；{item['rationale']}；來源：{', '.join(item['source_refs'])}")
        for spec in content.get("diagrams", []):
            if spec.get("generated_from") != "technical_model":
                continue
            png = folder / "technical" / (spec["type"] + ".png")
            if png.is_file():
                doc.add_heading(spec["title"], 2)
                doc.add_picture(str(png), width=Inches(6))
    output = folder / "PRD.docx"
    temp = folder / "PRD.tmp.docx"
    doc.save(temp)
    temp.replace(output)
    return str(output)


def re_unsafe(source):
    import re

    return (
        re.search(r"<|javascript:|%%\{|\bclick\b|https?://", source, re.I) is not None
    )


def baseline(db, project, revision):
    if revision.status in {"BASELINED", "SUPERSEDED"}:
        return revision
    if revision.content.get("technical_amendment"):
        parent = db.get(Revision, revision.parent_revision_id)
        if not parent:
            raise ValueError("技術補件缺少父版本")
        excluded = {"technical_model", "technical_amendment"}
        before = {k: v for k, v in parent.content.items() if k not in excluded and k != "diagrams"}
        after = {k: v for k, v in revision.content.items() if k not in excluded and k != "diagrams"}
        if before != after:
            raise ValueError("技術補件不得改變已定版業務內容，請走正常變更流程")
        old_business_diagrams = [d for d in parent.content.get("diagrams", []) if d.get("generated_from") != "technical_model"]
        new_business_diagrams = [d for d in revision.content.get("diagrams", []) if d.get("generated_from") != "technical_model"]
        if old_business_diagrams != new_business_diagrams:
            raise ValueError("技術補件不得改變已定版業務圖表")
    else:
        human_gate(db, project)
    if project.state.get("draft_id") != revision.id:
        raise ValueError("此草稿已過時，請產生最新版本")
    if revision.parent_revision_id != project.current_revision_id:
        raise ValueError("草稿的父版本已過時")
    if revision.version.startswith("v0."):
        revision.version = "v1.0"
    revision.docx_path = render_docx(revision)
    if project.current_revision_id:
        db.get(Revision, project.current_revision_id).status = "SUPERSEDED"
    revision.status, revision.baselined_at = "BASELINED", now()
    project.current_revision_id, project.runtime = revision.id, "BASELINED"
    project.state = {**project.state, "draft_id": None, "progress": 100, "step": 9}
    for change_id in revision.changes:
        change = db.get(Change, change_id)
        change.status = "APPLIED"
        change.resolution = {**change.resolution, "revision_id": revision.id}
    resolve_interruptions(db, project, ["FINAL_PRD_REVIEW_REQUIRED"])
    log(
        db,
        project.id,
        "BASELINE",
        f"已建立不可變更版本 {revision.version}",
        {"revision_id": revision.id},
    )
    return revision
