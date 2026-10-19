"""Evidence contract: which kinds of evidence this request needs, and which are still missing.

EXP-02C: assembling evidence by role (rule, control, interface, data, process)
beats ranking single documents. Roles are decided by deterministic wording, a
missing role triggers one targeted search, and whatever is still missing is
reported as a gap. Nothing here asks a model to fill a gap.
"""

import re

from sqlalchemy import select

from .db import Link, Source

# role: (label, request wording that makes it required, evidence wording that covers it, search terms)
ROLES = {
    "BUSINESS_RULE": ("業務規則", None, r"規則|規範|作業要點|辦法|準則|條件|限額|政策", "作業規範 規則"),
    "CONTROL": ("控制與法遵", r"個資|個人資料|資安|加密|法遵|稽核|授權|洗錢|風險|遮蔽|密碼|驗證|權限",
                r"個資|個人資料|資安|加密|法遵|稽核|授權|洗錢|遮蔽|驗證|權限|保護", "資安 個資 法遵 控制"),
    "INTERFACE": ("介面／電文", r"API|電文|介面|主機|串接|簡訊閘道|上行|下行",
                  r"API|電文|介面|主機|request|response|欄位對應|端點", "API 電文 介面規格"),
    "DATA": ("資料", r"欄位|資料表|資料庫|保存|保留|紀錄|DB",
             r"資料表|欄位|資料庫|DB|保存期限|保留期限|資料異動|Schema", "資料表 欄位 保存期限"),
    "PROCESS": ("流程", r"流程|步驟|批次|排程|重送|審核|核准",
                r"流程|步驟|狀態|批次|排程|作業程序", "作業流程 步驟"),
}
REQUEST_KINDS = {"REQUIREMENT_TEXT", "REQUIREMENT_DOCUMENT"}
MAX_ROLE_PASSES = 3


def required_roles(request_text):
    text = request_text or ""
    return [role for role, (_, need, _, _) in ROLES.items() if need is None or re.search(need, text, re.I)]


def source_roles(source):
    text = f"{source.title}\n{(source.content or '')[:4000]}"
    roles = {role for role, (_, _, cover, _) in ROLES.items() if re.search(cover, text, re.I)}
    if source.kind == "INTERNAL_POLICY":
        roles.add("BUSINESS_RULE")
    return roles


def role_query(project_name, role):
    return f"{(project_name or '')[:16]} {ROLES[role][3]}".strip()


def assess(db, project, request_text, attempts=None):
    """Coverage of each required role by this project's linked evidence.

    `attempts` maps a role to what its targeted search saw: "CANDIDATES_REJECTED"
    (something matched but was not relevant enough) or "NONE_FOUND".
    """
    attempts = attempts or {}
    evidence = [s for s in db.scalars(select(Source).join(Link, Link.source_id == Source.id)
                                      .where(Link.project_id == project.id, Source.status == "ACTIVE"))
                if s.kind not in REQUEST_KINDS]
    covered = {}
    for source in evidence:
        for role in source_roles(source):
            covered.setdefault(role, []).append(source.title)
    needed = required_roles(request_text)
    rows = []
    for role, (label, _, _, _) in ROLES.items():
        if role not in needed:
            continue
        titles = covered.get(role, [])
        gap = "" if titles else ("有候選但相關性不足" if attempts.get(role) == "CANDIDATES_REJECTED"
                                 else "目前知識庫找不到")
        rows.append({"role": role, "label": label, "status": "COVERED" if titles else "GAP",
                     "covered_by": titles[:3], "gap_reason": gap})
    return {"roles": rows, "gaps": [r["label"] for r in rows if r["status"] == "GAP"],
            "note": "缺少的證據角色只列為待補，不會讓 AI 自行補寫。"}
