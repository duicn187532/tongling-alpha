"""Find specifics in generated text that no source document contains.

Models fill gaps with plausible numbers, algorithms and system names. These
are the claims that hurt most when wrong, and they are easy to check
mechanically: a number, acronym or quoted term either appears in a source
or it does not. Wording is not judged here.
"""

import re
import unicodedata

from sqlalchemy import select

NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|筆|次|天|日|年|個月|月|週|分鐘|秒|小時|萬|億|元|位數|位|碼|人|件|字元)?")
ACRONYM = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*(?![A-Za-z0-9])")
QUOTED = re.compile(r"「([^」]{2,24})」")
# Generic document vocabulary; mentioning these invents nothing.
COMMON = {"PDF", "PM", "SA", "QA", "IT", "UI", "UX", "ID", "API", "EMAIL", "OK", "PRD", "AI", "UAT", "SIT"}


def _norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).casefold()


DATE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}(?:\s*[-/月]\s*\d{1,2}\s*日?)?")


def specifics(text):
    text = text or ""
    found = []
    for token in [t.strip() for t in DATE.findall(text)] + ACRONYM.findall(text) + QUOTED.findall(text):
        if token.upper() not in COMMON and token not in found:
            found.append(token)
    # Numbers inside dates and names (AES-256) are already covered above.
    rest = ACRONYM.sub(" ", DATE.sub(" ", text))
    for match in NUMBER.finditer(rest):
        token = match.group(0).strip()
        if token and token not in found:
            found.append(token)
    return found


def unsourced(text, sources_text):
    """Specifics in `text` that never appear in the sources (order kept)."""
    haystack = _norm(sources_text)
    # The same date written 2027-03-01 or 2027年3月1日 is one date.
    dates = {tuple(int(x) for x in re.findall(r"\d+", d)) for d in DATE.findall(sources_text or "")}
    missing = []
    for token in specifics(text):
        if DATE.fullmatch(token.strip()):
            if tuple(int(x) for x in re.findall(r"\d+", token)) in dates:
                continue
            missing.append(token)
            continue
        key = _norm(token)
        digits = re.sub(r"\D", "", key)
        # "5 年" is supported by "保存 5 年" and by "5年"; a bare number must appear as a number.
        if key in haystack or (digits and digits == key and re.search(rf"(?<!\d){digits}(?!\d)", haystack)):
            continue
        missing.append(token)
    return missing


def project_sources_text(db, project):
    """Everything the PM supplied for this project: brief plus linked sources."""
    from .db import Link, Source
    texts = [project.description or "", project.name or ""]
    for source in db.scalars(select(Source).join(Link, Link.source_id == Source.id)
                             .where(Link.project_id == project.id)):
        texts.append(source.content or "")
    return "\n".join(texts)


REQUEST_KINDS = {"REQUIREMENT_TEXT", "REQUIREMENT_DOCUMENT", "MEETING_RECORD"}
CONSTRAINT = re.compile(r"不可|不得|禁止|不能|必須|須|需要|需|應|要求")
CURRENT_STATE = re.compile(r"^(?:現行|目前|原本|過去|現況)")
LABEL = re.compile(r"^[^：:，,]{1,8}[：:]\s*")
# PRD fields that state what will be built; background and evidence quotes are excluded
# because they repeat the request verbatim and would hide every omission.
COVERAGE_FIELDS = ("requirements", "acceptance_criteria", "scope", "out_of_scope", "open_issues",
                   "business_rules", "nonfunctional_requirements", "exceptions", "success_metrics",
                   "risk_compliance_security", "actors", "permissions", "dependencies", "data_impact",
                   "api_message_impact", "batch_integration_impact", "deployment_impact", "assumptions")
TEXT_KEYS = ("statement", "description", "text", "rule", "item", "title")


def project_request_text(db, project):
    """What the PM asked for: the brief plus request documents and confirmed meetings."""
    from .db import Link, Source
    texts = [project.description or ""]
    for source in db.scalars(select(Source).join(Link, Link.source_id == Source.id)
                             .where(Link.project_id == project.id, Source.status == "ACTIVE",
                                    Source.kind.in_(REQUEST_KINDS))):
        if (source.content or "").strip() not in texts[0]:
            texts.append(source.content or "")
    return "\n".join(t for t in texts if t)


def _flatten(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for item in value for t in _flatten(item)]
    if isinstance(value, dict):
        return [t for key in TEXT_KEYS if key in value for t in _flatten(value[key])]
    return []


def prd_text(content):
    return "\n".join(t for field in COVERAGE_FIELDS for t in _flatten(content.get(field)))


def _bigrams(text):
    chars = re.sub(r"[^\u4e00-\u9fff]", "", _norm(text))
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def uncovered(request_text, content, limit=5):
    """Request clauses the PRD never states: a missing specific, or a constraint
    whose wording is mostly absent. Advisory; deciding not to build it is fine."""
    # "三次" in the PRD states the same thing as "3 次" in the request.
    haystack = re.sub(r"([一二兩三四五六七八九十])(?=\s*(?:次|天|日|碼|位|個|年|秒|分鐘|小時|筆|人|件))",
                      lambda m: str("一二三四五六七八九十".find(m.group(1)) + 1 if m.group(1) != "兩" else 2),
                      prd_text(content))
    grams = _bigrams(haystack)
    found = []
    for sentence in re.split(r"[。；;\n]", request_text or ""):
        for clause in (c.strip(" ，,：:") for c in re.split(r"[，,]", sentence)):
            body = LABEL.sub("", clause)  # "本次需求：…" — the label is not part of the ask
            if len(body) < 6 or CURRENT_STATE.match(body):
                continue
            missing = unsourced(body, haystack)
            if missing:
                found.append({"clause": clause, "missing": missing})
            elif not specifics(body) and CONSTRAINT.search(body.replace("需求", "")):
                own = _bigrams(body)
                if own and len(own & grams) / len(own) < 0.5:
                    found.append({"clause": clause, "missing": []})
            if len(found) >= limit:
                return found
    return found
