"""Turn a one-sentence business need into a reviewable Agent plan."""

from pydantic import BaseModel, Field

from .model_runtime import structured


class Guidance(BaseModel):
    summary: str = Field(max_length=1000)
    suggested_project_name: str = Field(max_length=100)
    refined_brief: str = Field(max_length=4000)
    questions: list[str] = Field(default_factory=list, max_length=5)
    next_steps: list[str] = Field(default_factory=list, max_length=6)
    unknowns: list[str] = Field(default_factory=list, max_length=5)


def suggest(brief, config):
    original = brief.strip()
    fallback = lambda: {
        "summary": original,
        "suggested_project_name": original[:36],
        "refined_brief": original,
        "questions": ["主要使用者是誰？", "成功或失敗要如何驗收？", "有哪些系統、法遵或資料限制？"],
        "next_steps": ["保存需求起點", "搜尋並核對來源證據", "分析待確認項目與衝突", "形成 PRD 草稿並審閱"],
        "unknowns": ["尚未提供可驗證的驗收條件與系統影響"],
    }
    return structured(
        config,
        "REQUIREMENT_GUIDANCE",
        "你是金融產品需求分析助理。只重述使用者已提供的事實，不增造規則、電文、系統或業務決議。"
        "把一句或一段需求整理成可開始工作的需求起點；不明確之處列為問題與未知事項。"
        "提出後續取證、訪談、驗收、PRD 草稿步驟。簡短、繁體中文。",
        {"user_brief": original},
        Guidance,
        fallback,
    ).model_dump()
