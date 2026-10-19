"""Advisory quality hints for a business PRD draft.

Nothing here blocks a baseline. Rules find the problems so the result is
predictable and explainable; the model is only asked for an optional
rewrite or a split proposal that the PM accepts or ignores.
"""

import re

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from .db import Revision

VAGUE = re.compile(r"適當|盡量|儘量|友善|快速|視情況|彈性處理|相關資料|相關單位|盡速|及時|大量|妥善|必要時")
ENUM_ETC = re.compile(r"、[^、。；;，,]{1,12}等(?![級待])")
TIMING = re.compile(r"效能|回應時間|速度|快速|迅速|時效|可用性|容量|延遲|即時")
DIGIT = re.compile(r"\d")
ACTORS = re.compile(r"員工|客戶|使用者|管理者|行員|主管|企業|系統|客服|PM|審查")
REVIEW_TOPICS = re.compile(r"個資|個人資料|資安|加密|法遵|洗錢|風險|稽核|權限|密碼")
# Verbs that mark an independently testable behaviour. Splitting needs two of
# them in different clauses, so qualifiers and conditions never trigger it.
ACTIONS = re.compile(r"寄送|發送|傳送|另送|送出|送達|傳遞|顯示|提供|設定|保留|記錄|加密|通知|驗證|產生|下載|上傳|查詢|停用|啟用|限制|鎖定|改為|轉換|匯出|重送|檢核")
QUALIFIER = re.compile(r"(若|如果|如|當|但|除|例外|否則|失敗|逾時|超過|未|於.{0,8}時)")
CLAUSE_SPLIT = re.compile(r"[；;]|，(?:並|且|同時|另)|並且|同時")
PLACEHOLDER = "待 PM"

# Severity: 提醒 = likely to cause rework; 建議 = worth tidying; 資訊 = context only.
WEIGHT = {"提醒": 6, "建議": 2, "資訊": 0}


def compound_clauses(statement):
    """Clauses that each carry their own action, or [] when the requirement is one thing."""
    clauses = [c.strip(" ，,。") for c in CLAUSE_SPLIT.split(statement or "") if c.strip(" ，,。")]
    # A clause that opens with a condition or exception refines the same behaviour.
    acting = [c for c in clauses if len(c) >= 6 and ACTIONS.search(c) and not QUALIFIER.match(c)]
    verbs = {ACTIONS.search(c).group(0) for c in acting}
    return acting if len(acting) >= 2 and len(verbs) >= 2 else []


def check(content, sources_text=None, request_text=None):
    """Hints for a draft; with `sources_text`, also specifics no source supports;
    with `request_text`, also parts of the request the draft never states."""
    from .faithfulness import unsourced, uncovered
    issues = []

    def add(code, severity, message, suggestion, requirement=None, text=""):
        issues.append({"code": code, "severity": severity, "message": message, "suggestion": suggestion,
                       "requirement_id": requirement["id"] if requirement else None,
                       "topic": requirement["topic"] if requirement else None, "text": text})

    criteria = {}
    for item in content.get("acceptance_criteria", []):
        if isinstance(item, dict) and item.get("requirement_id"):
            criteria.setdefault(item["requirement_id"], []).append(item.get("description", ""))
    for row in content.get("requirements", []):
        statement = row.get("statement", "")
        words = sorted(set(VAGUE.findall(statement)) | ({"…等"} if ENUM_ETC.search(statement) else set()))
        if words:
            add("VAGUE", "提醒", f"用語不夠明確：{'、'.join(words)}",
                "改成可判斷對錯的描述，例如具體條件、數值或清單。", row, statement)
        if TIMING.search(statement) and not DIGIT.search(statement):
            add("UNMEASURABLE", "提醒", "提到效能或時效，但沒有數值",
                "補上可量測的目標，例如「5 分鐘內送達」。", row, statement)
        if row.get("actor", "待確認") == "待確認" and not ACTORS.search(statement):
            add("NO_ACTOR", "建議", "看不出是誰在操作或受影響", "在需求或角色欄位標明對象。", row)
        if row.get("priority", "待確認") == "待確認":
            add("NO_PRIORITY", "資訊", "尚未設定優先級", "在需求列按「編修」設定。", row)
        own = criteria.get(row["id"].split("::")[0], [])
        real = [c for c in own if PLACEHOLDER not in c]
        if not real:
            add("NO_CRITERIA", "提醒", "沒有可測試的驗收條件" if not own else "驗收條件仍是待確認",
                "用「前提／操作／結果」寫出主要成功情境與一個重要例外。", row)
        if not row.get("evidence") and not row.get("split_from"):
            add("NO_SOURCE", "建議", "沒有來源依據", "加入依據，或在決策中註明來源。", row)
        if sources_text is not None:
            missing = unsourced(" ".join([statement] + criteria.get(row["id"].split("::")[0], [])), sources_text)
            if missing:
                add("UNSOURCED", "提醒", f"來源文件中找不到：{'、'.join(missing[:6])}",
                    "確認出處；沒有依據的數值或名稱請改為「待確認」或補上來源。", row)
        parts = compound_clauses(statement)
        if parts and not row.get("split_from"):
            add("COMPOUND", "建議", f"可能包含 {len(parts)} 件可各自驗收的事",
                "若各自獨立驗收、可能分開排期，可拆成多條；只是補充說明就不必拆。", row, statement)
        if REVIEW_TOPICS.search(statement):
            add("REVIEW_UNIT", "資訊", "涉及個資、資安或法遵",
                "建議送審查單位（法遵、風管、資安）確認。", row)
    for item in uncovered(request_text, content) if request_text else []:
        detail = f"（缺：{'、'.join(item['missing'][:3])}）" if item["missing"] else ""
        add("UNCOVERED", "提醒", f"需求來源提到「{item['clause']}」{detail}，PRD 沒有寫到",
            "確認是否遺漏；要做就補成需求或驗收條件，決定不做請列入範圍外。", text=item["clause"])
    from .prd_editor import gaps
    empty = [g["title"] for g in gaps(content) if not g["filled"]]
    if empty:
        add("EMPTY_SECTIONS", "建議", f"{len(empty)} 個章節待補：{'、'.join(empty)}",
            "在「補齊 PRD」回答對應問題。")
    penalty = sum(WEIGHT[i["severity"]] for i in issues)
    counts = {level: sum(i["severity"] == level for i in issues) for level in WEIGHT}
    return {"score": max(0, 100 - penalty), "counts": counts, "issues": issues,
            "note": "品質提示僅供參考，不影響定版。"}


class ImproveInput(BaseModel):
    codes: list[str] = Field(default_factory=list, max_length=8)


class Improved(BaseModel):
    statement: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=3)


class SplitPart(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    statement: str = Field(min_length=6, max_length=600)


class SplitResult(BaseModel):
    split: bool
    reason: str = Field(default="", max_length=300)
    parts: list[SplitPart] = Field(default_factory=list, max_length=4)


def _row(revision, req_id):
    row = next((r for r in revision.content.get("requirements", []) if r["id"] == req_id), None)
    if row is None:
        raise HTTPException(404, "此需求不在這份草稿")
    return row


def register(app, get_db, get):
    from .intelligence import settings
    from .model_runtime import structured

    def sources_for(db, revision):
        from .db import Project
        from .faithfulness import project_sources_text
        return project_sources_text(db, get(db, Project, revision.project_id))

    @app.get("/api/prd-revisions/{rid}/quality")
    def quality(rid: str, db=Depends(get_db)):
        from .db import Project
        from .faithfulness import project_request_text
        revision = get(db, Revision, rid)
        project = get(db, Project, revision.project_id)
        return check(revision.content, sources_for(db, revision), project_request_text(db, project))

    @app.post("/api/prd-revisions/{rid}/requirements/{req_id}/improve")
    def improve(rid: str, req_id: str, body: ImproveInput, db=Depends(get_db)):
        """A rewrite proposal for one requirement; nothing is saved."""
        revision = get(db, Revision, rid)
        row = _row(revision, req_id)
        problems = [i for i in check(revision.content)["issues"] if i["requirement_id"] == req_id
                    and (not body.codes or i["code"] in body.codes)]
        evidence = [f.get("statement", "")[:300] for f in row.get("evidence", [])[:6]]
        config = {**settings(db), "_project_id": revision.project_id}
        fallback = {"statement": row["statement"], "acceptance_criteria": []}
        try:
            result = structured(
                config, "PRD_ASSIST",
                "你是金融業資深 PM。依指出的品質問題改寫這一條需求，讓它具體、可驗證、只描述一件主要的事。"
                "只能使用需求原文與依據中的資訊；依據沒有的數值、系統或期限一律寫「待確認」，不得自行編造。"
                "若需要驗收條件，用「前提：…；操作：…；結果：…」寫 1–3 條：主要成功情境與最重要的例外，不寫空泛條件。",
                {"requirement": row["statement"], "topic": row["topic"],
                 "problems": [p["message"] for p in problems], "evidence": evidence},
                Improved, lambda: fallback, timeout_seconds=120, max_tokens=900)
            proposal = result.model_dump()
        except Exception as exc:
            proposal = {**fallback, "warning": f"模型改寫失敗：{type(exc).__name__}"}
        if config.get("provider") == "mock":
            proposal["warning"] = "目前為離線規則模式，無法產生改寫建議；請手動編修。"
        else:
            from .faithfulness import unsourced
            missing = unsourced(" ".join([proposal["statement"]] + proposal.get("acceptance_criteria", [])),
                                sources_for(db, revision))
            if missing:
                proposal["unsourced"] = missing
                proposal["warning"] = f"建議中有來源找不到的內容：{'、'.join(missing[:6])}；採用前請確認。"
        return {**proposal, "problems": problems}

    @app.post("/api/prd-revisions/{rid}/requirements/{req_id}/split-suggestion")
    def split_suggestion(rid: str, req_id: str, db=Depends(get_db)):
        """Propose a split only when the requirement bundles independent items."""
        revision = get(db, Revision, rid)
        row = _row(revision, req_id)
        clauses = compound_clauses(row["statement"])
        fallback = {"split": bool(clauses), "reason": "依分句與動作判斷" if clauses else "看起來是一件事，不建議拆分",
                    "parts": [{"title": f"{row['topic']}（{i + 1}）", "statement": c} for i, c in enumerate(clauses[:4])]}
        config = {**settings(db), "_project_id": revision.project_id}

        def validate(result):
            if result.split and not 2 <= len(result.parts) <= 4:
                raise ValueError("拆分結果須為 2–4 條")

        try:
            result = structured(
                config, "PRD_ASSIST",
                "你是金融業資深 PM，判斷這條需求是否應拆分。只有當它包含 2–4 件可各自驗收、可能分開開發或排期的事，才拆分；"
                "條件、例外、補充說明、欄位細節都留在同一條，不可拆成瑣碎項目。不需要拆時回傳 split=false 並說明理由。"
                "拆分時每條給簡短標題與一句完整需求，內容只能來自原文，不得新增。",
                {"topic": row["topic"], "requirement": row["statement"]},
                SplitResult, lambda: fallback, validator=validate, timeout_seconds=120, max_tokens=900)
            return result.model_dump()
        except Exception:
            return fallback
