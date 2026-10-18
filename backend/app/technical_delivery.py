"""Source-backed technical design, deterministic diagrams and IT handoff gate."""

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .db import DATA, ROOT, Link, Source, Chunk, TechnicalReview, DeliveryPackage, now
from .lifecycle import content_hash, re_unsafe


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=3000)
    source_refs: list[str] = Field(min_length=1)
    chunk_refs: list[str] = Field(default_factory=list)
    requirement_refs: list[str] = Field(default_factory=list)
    status: str = "CONFIRMED"


class Interface(Item):
    from_system: str = ""
    to_system: str = ""
    before: str = ""
    after: str = ""
    field_diffs: list[dict] = Field(default_factory=list)


class Flow(Item):
    steps: list[str] = Field(default_factory=list)
    exception_steps: list[str] = Field(default_factory=list)


class Acceptance(Item):
    scenario: str = "NORMAL"
    expected: str = ""


class DataChange(Item):
    before: str = ""
    after: str = ""
    migration: str = ""
    rollback: str = ""


class BusinessRule(Item):
    condition: str = ""
    on_true: str = ""
    on_false: str = ""


class Impact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area: str
    affected: bool
    rationale: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    chunk_refs: list[str] = Field(default_factory=list)


class TechnicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_components: list[Item] = Field(default_factory=list)
    interface_contracts: list[Interface] = Field(default_factory=list)
    data_changes: list[DataChange] = Field(default_factory=list)
    flow_definitions: list[Flow] = Field(default_factory=list)
    business_rules: list[BusinessRule] = Field(default_factory=list)
    acceptance_cases: list[Acceptance] = Field(default_factory=list)
    technical_impact: list[Impact] = Field(default_factory=list)


AREAS = {"SYSTEM", "INTERFACE", "DATA", "FLOW", "SECURITY", "OPERATIONS"}
DIAGRAM_TYPES = ("BUSINESS_RULE_FLOW", "SYSTEM_SEQUENCE", "SYSTEM_ARCHITECTURE")


def _safe(value):
    return re.sub(r'["<>\r\n]', " ", str(value))[:90]


def diagrams(model):
    rules = model.business_rules
    flow = ['flowchart TD', 'Start["開始"]']
    for index, rule in enumerate(rules):
        flow += [f'R{index}{{"{_safe(rule.condition)}"}}',
                 f'Y{index}["{_safe(rule.on_true)}"]', f'N{index}["{_safe(rule.on_false)}"]',
                 f'Start --> R{index}', f'R{index} -->|是| Y{index}', f'R{index} -->|否| N{index}']
    sequence = ["sequenceDiagram"]
    if not model.interface_contracts:
        sequence += ["participant NoInterface as 無電文異動", "Note over NoInterface: 已確認無介面異動"]
    for contract in model.interface_contracts:
        a, b = _safe(contract.from_system), _safe(contract.to_system)
        if a and b:
            sequence.append(f"participant {re.sub('[^A-Za-z0-9_]', '_', contract.id)}A as {a}")
            sequence.append(f"participant {re.sub('[^A-Za-z0-9_]', '_', contract.id)}B as {b}")
            stem = re.sub('[^A-Za-z0-9_]', '_', contract.id)
            sequence.append(f"{stem}A->>{stem}B: {_safe(contract.name)}")
    architecture = ["flowchart LR"]
    by_name = {c.name: f"C{i}" for i, c in enumerate(model.system_components)}
    for name, node in by_name.items():
        architecture.append(f'{node}["{_safe(name)}"]')
    for c in model.interface_contracts:
        if c.from_system in by_name and c.to_system in by_name:
            architecture.append(f'{by_name[c.from_system]} -->|"{_safe(c.name)}"| {by_name[c.to_system]}')
    return [
        {"type": kind, "title": title, "mermaid": "\n".join(lines),
         "status": "READY", "source_refs": sorted({s for items in refs for s in items.source_refs} |
                                                   {s for impact in model.technical_impact for s in impact.source_refs}),
         "generated_from": "technical_model"}
        for kind, title, lines, refs in (
            (DIAGRAM_TYPES[0], "商業規則流程圖", flow, rules),
            (DIAGRAM_TYPES[1], "系統互動循序圖", sequence, model.interface_contracts),
            (DIAGRAM_TYPES[2], "系統架構與邊界圖", architecture, model.system_components + model.interface_contracts),
        )
    ]


def render_diagrams(revision_id, specs):
    cli = ROOT / "frontend/node_modules/@mermaid-js/mermaid-cli/src/cli.js"
    node = shutil.which("node")
    if not cli.exists() or not node:
        raise ValueError("缺少 Mermaid CLI 或 Node.js，無法驗證技術圖表")
    folder = DATA / "exports" / revision_id / "technical"
    folder.mkdir(parents=True, exist_ok=True)
    files = []
    for diagram in specs:
        source = diagram["mermaid"]
        if re_unsafe(source) or len(source) > 20000:
            raise ValueError("技術圖表含有不允許的指令")
        path = folder / (diagram["type"] + ".mmd")
        png = folder / (diagram["type"] + ".png")
        path.write_text(source, encoding="utf-8")
        proc = subprocess.run([node, str(cli), "-i", str(path), "-o", str(png), "-b", "white"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        if proc.returncode or not png.is_file():
            raise ValueError(f"{diagram['title']}渲染失敗：{proc.stderr[-600:]}")
        files.extend((path, png))
    return files


def validate_model(db, revision, model):
    ids = set()
    requirements = {r.get("id") for r in revision.content.get("requirements", [])}
    project_id = revision.project_id
    for group in (model.system_components, model.interface_contracts, model.data_changes,
                  model.flow_definitions, model.business_rules, model.acceptance_cases):
        for item in group:
            if item.id in ids:
                raise ValueError(f"技術項目 ID 重複：{item.id}")
            ids.add(item.id)
            if item.status != "CONFIRMED":
                raise ValueError(f"{item.id} 尚未確認")
            if set(item.requirement_refs) - requirements:
                raise ValueError(f"{item.id} 引用不存在的需求")
            for sid in item.source_refs:
                source = db.get(Source, sid)
                if not source or source.status != "ACTIVE" or not db.get(Link, (project_id, sid)):
                    raise ValueError(f"{item.id} 引用未連結的來源：{sid}")
            for cid in item.chunk_refs:
                chunk = db.get(Chunk, cid)
                if not chunk or chunk.source_id not in item.source_refs:
                    raise ValueError(f"{item.id} 引用不屬於來源的段落：{cid}")
    for impact in model.technical_impact:
        if impact.area not in AREAS:
            raise ValueError(f"未知的技術影響類別：{impact.area}")
        for sid in impact.source_refs:
            source = db.get(Source, sid)
            if not source or source.status != "ACTIVE" or not db.get(Link, (project_id, sid)):
                raise ValueError(f"技術影響引用未連結的來源：{sid}")
        for cid in impact.chunk_refs:
            chunk = db.get(Chunk, cid)
            if not chunk or chunk.source_id not in impact.source_refs:
                raise ValueError(f"技術影響引用不屬於來源的段落：{cid}")
    for c in model.interface_contracts:
        names = {x.name for x in model.system_components}
        if c.from_system not in names or c.to_system not in names:
            raise ValueError(f"{c.id} 的系統端點不在系統清單中")
        if not c.before or not c.after or not c.field_diffs:
            raise ValueError(f"{c.id} 須填寫 before、after 與欄位差異")
        for diff in c.field_diffs:
            if not all(k in diff for k in ("field", "before", "after", "reason")):
                raise ValueError(f"{c.id} 欄位差異須含 field、before、after、reason")
    for d in model.data_changes:
        if not all((d.before, d.after, d.migration, d.rollback)):
            raise ValueError(f"{d.id} 須填寫異動前後、遷移與回復方式")
    for rule in model.business_rules:
        if not all((rule.condition, rule.on_true, rule.on_false)):
            raise ValueError(f"{rule.id} 須填寫判斷條件及是／否結果")
    if len({x.area for x in model.technical_impact}) != len(model.technical_impact):
        raise ValueError("技術影響類別重複")


def reviews(db, revision):
    return [{"role": row.role, "reviewer": row.reviewer, "note": row.note,
             "reviewed_at": row.created_at, "content_hash": row.content_hash}
            for row in db.scalars(select(TechnicalReview).where(
                TechnicalReview.revision_id == revision.id,
                TechnicalReview.content_hash == revision.content_hash))]


def readiness(db, revision):
    from .delivery_validation import assess
    return assess(revision, reviews(db, revision), db)


def package(db, revision):
    status = readiness(db, revision)
    if status["readiness"] != "READY":
        raise ValueError(f"IT 交付仍有 {status['blocking_count']} 項阻擋缺口")
    if revision.status != "BASELINED":
        raise ValueError("請先建立業務 Baseline，再建立 IT 交付包")
    existing = db.scalar(select(DeliveryPackage).where(DeliveryPackage.revision_id == revision.id))
    if existing:
        return existing
    model = TechnicalModel.model_validate(revision.content.get("technical_model"))
    specs = [d for d in revision.content.get("diagrams", []) if d.get("type") in DIAGRAM_TYPES]
    files = render_diagrams(revision.id, specs)
    business = DATA / "exports" / revision.id / "PRD.docx"
    if not business.is_file():
        raise ValueError("業務 Baseline 檔案不存在")
    folder = DATA / "exports" / revision.id
    model_path = folder / "technical_model.json"
    model_path.write_text(json.dumps(model.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    package_files = {"PRD.docx": business, "technical_model.json": model_path}
    package_files.update({"technical/" + f.name: f for f in files})
    manifest = {"revision_id": revision.id, "version": revision.version,
                "content_hash": revision.content_hash, "created_at": now(),
                "reviews": reviews(db, revision),
                "files": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for name, path in package_files.items()}}
    output = folder / "IT_HANDOFF.zip"
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in package_files.items():
            archive.write(path, name)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest["sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    row = DeliveryPackage(revision_id=revision.id, content_hash=revision.content_hash,
                          manifest=manifest, path=str(output))
    db.add(row)
    return row
