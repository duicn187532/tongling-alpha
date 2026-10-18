import hashlib, html, ipaddress, os, re, socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol
import xml.etree.ElementTree as ET
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from .db import Session, ResearchRun, Source, Project, Link, now
from .evidence import ingest
from .intelligence import settings
from .model_runtime import structured
from . import activity

workers = ThreadPoolExecutor(max_workers=2)


def canonical_url(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("報導 URL 無效")
    query = [
        (k, v)
        for k, v in parse_qsl(p.query)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"gclid", "fbclid", "oc", "hl", "gl", "ceid"}
    ]
    return urlunsplit(
        (
            p.scheme,
            p.netloc.lower(),
            p.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def public_url(url):
    url = canonical_url(url)
    host = urlsplit(url).hostname
    for result in socket.getaddrinfo(host, None):
        if not ipaddress.ip_address(result[4][0]).is_global:
            raise ValueError("外部報導不得存取本機或私人網路")
    return url


class ExternalResearchProvider(Protocol):
    def search(self, query, limit=5): ...
    def fetch(self, url): ...
    def health(self): ...


class SearchProvider:
    def __init__(self, name="google_news"):
        self.name = name

    def health(self):
        env = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_SEARCH_API_KEY"}.get(
            self.name
        )
        return {
            "provider": self.name,
            "ok": not env or bool(os.getenv(env)),
            "capabilities": ["search", "article_fetch"],
            "message": "免金鑰公開新聞索引；可能限流或僅提供摘要"
            if not env
            else "金鑰已設定"
            if os.getenv(env)
            else f"尚未設定 {env}",
        }

    def search(self, query, limit=5):
        health = self.health()
        if not health["ok"]:
            raise ValueError(health["message"])
        with httpx.Client(
            timeout=20, follow_redirects=False, trust_env=False
        ) as client:
            if self.name == "google_news":
                r = client.get(
                    "https://news.google.com/rss/search",
                    params={
                        "q": query,
                        "hl": "zh-TW",
                        "gl": "TW",
                        "ceid": "TW:zh-Hant",
                    },
                )
                r.raise_for_status()
                root = ET.fromstring(r.content[:2_000_000])
                hits = []
                for item in root.findall("./channel/item")[:limit]:
                    hits.append(
                        {
                            "title": item.findtext("title", ""),
                            "url": item.findtext("link", ""),
                            "content": html.unescape(
                                re.sub("<[^>]+>", " ", item.findtext("description", ""))
                            ),
                            "publisher": item.findtext("source", ""),
                            "published_at": item.findtext("pubDate", ""),
                            "coverage": "SEARCH_SNIPPET",
                        }
                    )
                return hits
            if self.name == "tavily":
                r = client.post(
                    "https://api.tavily.com/search",
                    headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"]},
                    json={
                        "query": query,
                        "max_results": limit,
                        "topic": "news",
                        "include_raw_content": False,
                    },
                )
                r.raise_for_status()
                return [
                    {
                        **h,
                        "published_at": h.get("published_date", ""),
                        "coverage": "SEARCH_SNIPPET",
                    }
                    for h in r.json().get("results", [])
                ]
            r = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"]},
                params={"q": query, "count": limit},
            )
            r.raise_for_status()
            return [
                {
                    "title": h["title"],
                    "url": h["url"],
                    "content": h.get("description", ""),
                    "published_at": h.get("page_age", ""),
                    "coverage": "SEARCH_SNIPPET",
                }
                for h in r.json().get("web", {}).get("results", [])
            ]

    def fetch(self, url):
        if urlsplit(url).hostname == "news.google.com":
            return {
                "content": "",
                "coverage": "SEARCH_SNIPPET",
                "error": "新聞索引連結，請開啟原文查核；未聲稱讀取全文",
            }
        with httpx.Client(
            timeout=15, follow_redirects=False, trust_env=False
        ) as client:
            for _ in range(4):
                url = public_url(url)
                with client.stream(
                    "GET", url, headers={"User-Agent": "TongLing/0.7 personal research"}
                ) as r:
                    if r.is_redirect:
                        from urllib.parse import urljoin

                        url = urljoin(url, r.headers["location"])
                        continue
                    r.raise_for_status()
                    if "text/html" not in r.headers.get("content-type", ""):
                        raise ValueError("報導格式不是 HTML")
                    data = b""
                    for part in r.iter_bytes():
                        data += part
                        if len(data) > 1_000_000:
                            raise ValueError("報導超過擷取上限")
                text = data.decode("utf-8", errors="replace")
                text = re.sub(
                    r"<(script|style|nav|footer)\b[^>]*>.*?</\1>",
                    "",
                    text,
                    flags=re.S | re.I,
                )
                paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", text, re.S | re.I)
                text = "\n".join(
                    html.unescape(re.sub("<[^>]+>", "", p)).strip() for p in paragraphs
                )
                return {
                    "content": text[:16000],
                    "coverage": "ARTICLE_EXCERPT",
                    "url": url,
                }
        raise ValueError("報導重新導向過多")


class QueryPlan(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=5)


class Finding(BaseModel):
    peer: str = "待確認"
    capability_or_practice: str
    quote: str
    case_relevance_score: float = Field(ge=0, le=1)
    applicability: str = "供需求研討參考，需內部證據確認"
    risk_or_constraint: str = "不可將外部作法直接視為內部要求"
    maturity: str = "未確認"


class Findings(BaseModel):
    findings: list[Finding] = Field(default_factory=list, max_length=5)


class Observation(BaseModel):
    section: str
    observation: str
    quote: str
    source_refs: list[str] = Field(min_length=1, max_length=5)


class PeerAnalysis(BaseModel):
    observations: list[Observation] = Field(default_factory=list, max_length=8)


def start(db, p, topic):
    config = {**settings(db), "_project_id": p.id}
    org = config.get("organization", {})
    if not org.get("industry", "").strip():
        raise ValueError("請先在設定填寫行業；系統不會自行猜測")
    if p.status != "ACTIVE":
        raise ValueError("專案已刪除")
    existing = db.scalar(
        select(ResearchRun).where(
            ResearchRun.project_id == p.id,
            ResearchRun.status.in_(["QUEUED", "RUNNING"]),
        )
    )
    if existing:
        return existing
    row = ResearchRun(
        project_id=p.id,
        data={
            "topic": topic,
            "organization": org,
            "sources": [],
            "findings": [],
            "errors": [],
        },
    )
    db.add(row)
    db.flush()
    activity.create(db, "同業報導搜尋與分析", config, p.id, row.id)
    db.commit()
    workers.submit(execute, row.id, config)
    return row


def execute(rid, config):
    try:
        with activity.tracking(rid):
            with Session() as db:
                run = db.get(ResearchRun, rid)
                run.status = "RUNNING"
                data = dict(run.data)
                pid = run.project_id
                db.commit()
            org = data["organization"]
            topic = data["topic"]
            industry = org["industry"]
            provider = SearchProvider(config.get("research_provider", "google_news"))
            activity.stage(
                "產生公開搜尋關鍵字", "僅使用設定行業及公開主題，不傳送內部文件全文"
            )
            fallback = [f"{industry} {topic} 新聞", f"{industry} {topic} 上線 案例"] + [
                f"{peer} {topic}" for peer in org.get("peer_companies", [])[:2]
            ]
            plan = structured(
                config,
                "PEER_QUERY_GENERATION",
                "產生搜尋查詢，僅使用提供的公開行業、主題及同業名稱；每句不超過 120 字。",
                {
                    "industry": industry,
                    "topic": topic,
                    "region": org.get("region", ""),
                    "peers": org.get("peer_companies", []),
                },
                QueryPlan,
                lambda: {"queries": fallback[:5]},
            )
            data["queries"] = plan.queries
            seen = set()
            sources = []
            findings = []
            errors = []
            source_texts = {}
            for query in plan.queries[:3]:
                activity.stage("搜尋同業報導", query)
                try:
                    hits = provider.search(query, limit=config.get("research_limit", 5))
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                for hit in hits:
                    url = canonical_url(hit["url"])
                    if url in seen:
                        continue
                    seen.add(url)
                    if len(sources) >= config.get("research_limit", 5):
                        break
                    activity.stage("擷取與分析報導", hit["title"])
                    coverage = "SEARCH_SNIPPET"
                    body = hit.get("content", "") or hit["title"]
                    fetch_error = ""
                    try:
                        fetched = provider.fetch(url)
                        if fetched.get("content"):
                            body = fetched["content"]
                            coverage = fetched["coverage"]
                        fetch_error = fetched.get("error", "")
                    except Exception as exc:
                        fetch_error = str(exc)
                    try:
                        result = structured(
                            config,
                            "PEER_ARTICLE_EXTRACTION",
                            "只從報導節錄擷取同業做法。quote 必須逐字出現在 text，資料不足回空陣列；相關性依本案公開主題評分。",
                            {
                                "topic": topic,
                                "industry": industry,
                                "title": hit["title"],
                                "text": body[:6000],
                            },
                            Findings,
                            lambda: {
                                "findings": [
                                    {
                                        "peer": next(
                                            (
                                                p
                                                for p in org.get("peer_companies", [])
                                                if p in body
                                            ),
                                            "待確認",
                                        ),
                                        "capability_or_practice": hit["title"],
                                        "quote": body[:160],
                                        "case_relevance_score": 0.5,
                                    }
                                ]
                            },
                        )
                    except Exception as exc:
                        errors.append(
                            f"{hit['title']}：模型分析失敗，已保留來源待重試：{exc}"
                        )
                        result = Findings()
                    valid = [
                        f.model_dump()
                        for f in result.findings
                        if f.quote and f.quote in body
                    ]
                    score = max([f["case_relevance_score"] for f in valid] + [0])
                    published = hit.get("published_at", "")
                    freshness = None
                    try:
                        date = (
                            parsedate_to_datetime(published)
                            if "," in published
                            else datetime.fromisoformat(
                                published.replace("Z", "+00:00")
                            )
                        )
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=timezone.utc)
                        freshness = max(
                            0,
                            min(1, 1 - (datetime.now(timezone.utc) - date).days / 365),
                        )
                    except (ValueError, TypeError):
                        pass
                    with Session() as db:
                        run = db.get(ResearchRun, rid)
                        p = db.get(Project, pid)
                        if (
                            not run
                            or run.status == "CANCELLED"
                            or not p
                            or p.status != "ACTIVE"
                        ):
                            return
                        existing = next(
                            (
                                s
                                for s in db.scalars(
                                    select(Source).where(
                                        Source.kind == "EXTERNAL_RESEARCH",
                                        Source.status == "ACTIVE",
                                    )
                                )
                                if s.meta.get("canonical_url") == url
                                and s.meta.get("project_id") == pid
                            ),
                            None,
                        )
                        meta = {
                            "library_scope": "PROJECT",
                            "project_id": pid,
                            "evidence_type": "EXTERNAL_RESEARCH",
                            "authority_class": "EXTERNAL_BENCHMARK",
                            "canonical_url": url,
                            "source_url": url,
                            "publisher": hit.get("publisher") or urlsplit(url).hostname,
                            "title": hit["title"],
                            "published_at": published,
                            "fetched_at": now(),
                            "author": hit.get("author", "未知"),
                            "language": org.get("default_research_language", "zh-TW"),
                            "content_hash": hashlib.sha256(body.encode()).hexdigest(),
                            "query_used": query,
                            "industry": industry,
                            "peer_entity": [f["peer"] for f in valid],
                            "source_quality_score": 0.45
                            if coverage == "SEARCH_SNIPPET"
                            else 0.65,
                            "case_relevance_score": score,
                            "freshness_score": freshness,
                            "analysis_summary": [
                                f["capability_or_practice"] for f in valid
                            ],
                            "coverage": coverage,
                            "fetch_error": fetch_error,
                        }
                        if existing and existing.sha256 != meta["content_hash"]:
                            existing.status = "SUPERSEDED"
                            meta["previous_source_id"] = existing.id
                            existing = None
                        source = existing or ingest(
                            db,
                            hit["title"],
                            [{"text": body}],
                            provider="WEB_SEARCH",
                            kind="EXTERNAL_RESEARCH",
                            meta=meta,
                        )
                        if not db.get(Link, (pid, source.id)):
                            db.add(
                                Link(
                                    project_id=pid,
                                    source_id=source.id,
                                    role="REFERENCE",
                                )
                            )
                        source.meta = meta
                        meta["source_id"] = source.id
                        source_texts[source.id] = body
                        sources.append(meta)
                        findings.extend(
                            [
                                {
                                    **f,
                                    "source_refs": [source.id],
                                    "source_url": url,
                                    "observed_date": published,
                                    "recommendation_type": "WORKSHOP_REFERENCE",
                                    "evidence_role": "EXTERNAL_BENCHMARK",
                                }
                                for f in valid
                            ]
                        )
                        run.data = {
                            **data,
                            "sources": sources,
                            "findings": findings,
                            "errors": errors,
                        }
                        db.commit()
            analysis = []
            if findings:
                activity.stage(
                    "綜合同業分析",
                    "使用推理角色整理帶引用的觀察；外部來源不升格為內部要求",
                )
                try:
                    result = structured(
                        config,
                        "PEER_ANALYSIS",
                        "僅依提供的發現整理觀察。每項 quote 必須逐字出自來源發現的 quote，source_refs 必須使用提供的來源 ID。不可推論法規義務或自動建議納為需求。沒有跨來源證據時不可聲稱業界共識。",
                        {"topic": topic, "findings": findings},
                        PeerAnalysis,
                        lambda: {"observations": []},
                    )
                    analysis = [
                        a.model_dump()
                        for a in result.observations
                        if a.quote
                        and all(
                            s in source_texts and a.quote in source_texts[s]
                            for s in a.source_refs
                        )
                    ]
                except Exception as exc:
                    errors.append(f"綜合分析未完成；逐篇發現與來源已保留：{exc}")
            with Session() as db:
                run = db.get(ResearchRun, rid)
                p = db.get(Project, pid)
                if (
                    not run
                    or run.status == "CANCELLED"
                    or not p
                    or p.status != "ACTIVE"
                ):
                    return
                run.status = "COMPLETED" if sources else "DEGRADED"
                run.data = {
                    **data,
                    "sources": sources,
                    "findings": findings,
                    "errors": errors,
                    "analysis": analysis,
                    "report": {
                        "本案外部趨勢摘要": f"取得 {len(sources)} 篇可追溯來源，其中 {sum(s['coverage'] == 'SEARCH_SNIPPET' for s in sources)} 篇僅為搜尋摘要。",
                        "同業作法": [f["capability_or_practice"] for f in findings],
                        "共通模式": "需跨來源人工核對；不將單一報導推論為業界共識。",
                        "差異化機會": "請依內部策略與來源進行需求研討。",
                        "風險與法規訊號": [f["risk_or_constraint"] for f in findings],
                        "帶回研討的問題": [
                            "此作法是否適用本案客群、法域與內部政策？",
                            "有哪些內部權威證據支持採用？",
                        ],
                    },
                }
                db.commit()
            activity.finish(
                rid,
                "SUCCEEDED" if sources else "FAILED",
                f"搜尋完成：{len(sources)} 篇來源、{len(findings)} 項發現；全部僅為外部參考。"
                if sources
                else "搜尋服務不可用或無結果：" + "；".join(errors),
            )
    except Exception as exc:
        with Session() as db:
            run = db.get(ResearchRun, rid)
            if run and run.status != "CANCELLED":
                run.status = "DEGRADED"
                run.data = {
                    **run.data,
                    "errors": run.data.get("errors", []) + [str(exc)],
                }
                db.commit()
