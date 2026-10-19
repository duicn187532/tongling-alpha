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
