"""v7.1 knowledge policy and remote provider boundary.

Remote results are references. Nothing in this module stores remote document text.
"""

import hashlib
import os
import re
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from .db import KnowledgeServer, Project, RemoteQueryAudit, Source
from .graph_store import visible_sources
from .retrieval import qualify

SCOPES = {"PROJECT_LOCAL", "LOCAL_PRIVATE", "SERVER_SHARED"}
MODES = {
    "LOCAL_ONLY": {"PROJECT_LOCAL", "LOCAL_PRIVATE"},
    "SERVER_ONLY": {"PROJECT_LOCAL", "SERVER_SHARED"},
    "HYBRID": set(SCOPES),
}
CAPABILITIES = {"SEARCH", "VECTOR", "KEYWORD", "HYBRID", "GRAPH", "FETCH_FULL", "FETCH_SNIPPET", "METADATA", "TAGS", "VERSION", "AUTHORITY", "CHANGE_SCAN"}
CACHE_POLICIES = {"DISABLED", "METADATA_ONLY", "SNIPPET_ONLY", "FULL_CONTENT"}


def default_policy():
    return {"mode": "LOCAL_ONLY", "scopes": sorted(MODES["LOCAL_ONLY"]),
            "allow_remote_query": False, "allow_project_name_in_query": False,
            "allow_requirement_text_in_query": False,
            "allow_local_document_excerpt_in_query": False,
            "external_research": False, "server_enabled": True}


def validate_policy(data):
    mode = data.get("mode", "LOCAL_ONLY")
    if mode not in (*MODES, "CUSTOM"):
        raise ValueError("未知的知識模式")
    scopes = set(data.get("scopes", [])) if mode == "CUSTOM" else MODES[mode]
    if not scopes <= SCOPES:
        raise ValueError("未知的知識範圍")
    base = default_policy()
    return {**base, **{k: bool(data[k]) for k in base if isinstance(base[k], bool) and k in data},
            "mode": mode, "scopes": sorted(scopes)}


def policy_for(db, project_id, config):
    data = config.get("knowledge_policy") or {}
    project = db.get(Project, project_id) if project_id else None
    override = (project.state or {}).get("knowledge_policy") if project else None
    return validate_policy(override or data)


def source_scope(source, project_id, linked):
    if source.kind in {"COMPETITOR", "EXTERNAL_RESEARCH"}:
        return "EXTERNAL_RESEARCH"
    if source.meta.get("library_scope") == "GLOBAL":
        return "LOCAL_PRIVATE"
    return "PROJECT_LOCAL" if source.id in linked else "LOCAL_PRIVATE"


def local_source_ids(db, project_id, policy):
    from .db import Link
    linked = set(db.scalars(select(Link.source_id).where(Link.project_id == project_id))) if project_id else set()
    allowed = set(policy["scopes"])
    if policy["external_research"]:
        allowed.add("EXTERNAL_RESEARCH")
    return {s.id for s in visible_sources(db, project_id) if source_scope(s, project_id, linked) in allowed}


def validate_server_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("伺服器網址須為不含帳密、查詢參數的 HTTP(S) 網址")
    return value.rstrip("/")


def public_server(server):
    return {"id": server.id, "name": server.name, "base_url": server.base_url,
            "enabled": bool(server.enabled), "auth_env_var": server.auth_env_var,
            "timeout_seconds": server.timeout_seconds, "tls_verify": bool(server.tls_verify),
            "cache_policy": server.cache_policy, "capabilities": server.capabilities or [],
            "credential_available": bool(server.auth_env_var and os.getenv(server.auth_env_var))}


class HttpKnowledgeServerProvider:
    def __init__(self, server):
        self.server = server

    def _request(self, method, path, payload=None):
        headers = {}
        if self.server.auth_env_var:
            token = os.getenv(self.server.auth_env_var)
            if not token:
                raise ValueError("伺服器金鑰環境變數尚未設定")
            headers["Authorization"] = f"Bearer {token}"
        with httpx.Client(timeout=httpx.Timeout(self.server.timeout_seconds, connect=min(5, self.server.timeout_seconds)),
                          verify=bool(self.server.tls_verify), follow_redirects=False, trust_env=False) as client:
            response = client.request(method, self.server.base_url + path, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    def health(self):
        return self._request("GET", "/health")

    def capabilities(self):
        raw = self._request("GET", "/capabilities")
        values = raw.get("capabilities", []) if isinstance(raw, dict) else raw
        return sorted(set(values) & CAPABILITIES)

    def search(self, query, limit, project_id=None):
        # Query is the only case data sent. No project name, requirements or excerpts.
        raw = self._request("POST", "/search", {"query": query, "limit": limit})
        return raw.get("results", []) if isinstance(raw, dict) else raw

    def graph_search(self, query, limit, project_id=None):
        raw = self._request("POST", "/graph/search", {"query": query, "limit": limit})
        return raw.get("results", []) if isinstance(raw, dict) else raw

    def fetch_evidence(self, evidence_id):
        from urllib.parse import quote
        return self._request("GET", "/evidence/" + quote(evidence_id, safe=""))

    def get_metadata(self, evidence_id):
        from urllib.parse import quote
        return self._request("GET", "/evidence/" + quote(evidence_id, safe="") + "/metadata")

    def list_documents(self):
        return self._request("GET", "/documents")

    def rescan(self):
        return self._request("POST", "/rescan")


def _local_hit(hit, scope):
    return {**hit, "source_scope": scope, "source_location": "LOCAL",
            "provider_id": "local", "remote_evidence_id": None, "remote_uri": None,
            "cache_policy": None, "provenance_variants": []}


def _remote_hit(item, server, rank, channel, config):
    if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
        return None
    meta = item.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    score = max(0.0, min(1.0, float(item.get("case_relevance_score", item.get("score", 0.5)))))
    authority_class = str(meta.get("authority_class", "UNVERIFIED_INTERNAL"))
    # Server location never grants internal authority. Only explicit server metadata does.
    kind = "INTERNAL_POLICY" if authority_class == "INTERNAL_AUTHORITY" and meta.get("verified_authority") is True else "USER_UPLOAD"
    source = type("RemoteSource", (), {"kind": kind, "status": str(meta.get("status", "ACTIVE")), "meta": meta})()
    relevance = qualify(source, score, "伺服器候選來源；相關分數由伺服器回傳，須人工確認關鍵證據。", score >= config.get("unknown_threshold", 0.2), config)
    # No remote evidence may become KEY on unverified provider scores alone.
    if relevance["evidence_role"] == "KEY":
        relevance["evidence_role"] = "SUPPORTING"
    snippet = str(item.get("snippet", ""))[:1000] if server.cache_policy in {"SNIPPET_ONLY", "FULL_CONTENT"} else ""
    remote_id = str(item["id"])
    return {"source_id": f"remote:{server.id}:{remote_id}", "chunk_id": f"remote:{server.id}:{remote_id}",
            "title": str(item["title"])[:300], "text": snippet, "kind": kind,
            "meta": {k: meta.get(k) for k in ("version", "effective_at", "status", "canonical_uri", "content_hash") if k in meta},
            "score": score, "retrieval_channels": [channel], "source_scope": "SERVER_SHARED",
            "source_location": "REMOTE", "provider_id": server.id, "remote_evidence_id": remote_id,
            "remote_uri": str(item.get("uri", ""))[:1000], "cache_policy": server.cache_policy,
            "provenance_variants": [], "reason": relevance["case_relevance_reason"],
            "qualification_method": "遠端分數，待人工確認", **relevance, "_rank": rank}


def _dedupe_key(hit):
    meta = hit.get("meta") or {}
    return meta.get("content_hash") or meta.get("canonical_uri") or hit.get("remote_uri") or hit["source_id"]


def merge(local_hits, remote_hits, limit):
    # Reciprocal rank fusion compares ordinal positions, never unrelated raw scores.
    combined = []
    for group in (local_hits, remote_hits):
        for rank, hit in enumerate(group, 1):
            combined.append((1 / (60 + rank), hit))
    unique = {}
    for contribution, hit in combined:
        key = _dedupe_key(hit)
        if key not in unique:
            unique[key] = {**hit, "fusion_score": contribution, "provenance_variants": list(hit.get("provenance_variants", []))}
        else:
            current = unique[key]
            current["fusion_score"] += contribution
            variant = {k: hit.get(k) for k in ("source_id", "source_scope", "source_location", "provider_id", "remote_evidence_id", "remote_uri", "cache_policy")}
            current["provenance_variants"].append(variant)
            if current["source_location"] == "REMOTE" and hit["source_location"] == "LOCAL":
                current.update({k: v for k, v in hit.items() if k not in {"fusion_score", "provenance_variants"}})
    role_order = {"KEY": 0, "SUPPORTING": 1, "CONTEXT": 2,
                  "EXTERNAL_BENCHMARK": 3, "REJECTED": 4}
    authority_order = {"INTERNAL_AUTHORITY": 0, "UNVERIFIED_INTERNAL": 1,
                       "EXTERNAL_BENCHMARK": 2}
    return sorted(unique.values(), key=lambda h: (
        role_order.get(h.get("evidence_role"), 3),
        h.get("temporal_status", "CURRENT") != "CURRENT",
        authority_order.get(h.get("authority_class"), 1),
        -h["fusion_score"],
    ))[:limit]


def retrieve(db, query, project_id, config, local_search, strategy=None, source_ids=None, provider_factory=None, remote_query=None):
    provider_factory = provider_factory or HttpKnowledgeServerProvider
    policy = policy_for(db, project_id, config)
    allowed = local_source_ids(db, project_id, policy)
    if source_ids is not None:
        allowed &= set(source_ids)
    all_visible = {s.id for s in visible_sources(db, project_id)}
    local_filter = None if source_ids is None and allowed == all_visible else allowed
    local_limit = config["top_k"] if local_filter is None else max(config["top_k"], 20)
    local = local_search(db, query, project_id, {**config, "top_k": local_limit}, strategy, local_filter) if allowed else []
    sources = {s.id: s for s in db.scalars(select(Source).where(Source.id.in_([h["source_id"] for h in local])))} if local else {}
    linked = set()
    if project_id:
        from .db import Link
        linked = set(db.scalars(select(Link.source_id).where(Link.project_id == project_id)))
    local = [_local_hit(h, source_scope(sources[h["source_id"]], project_id, linked)) for h in local if h["source_id"] in allowed]
    if "SERVER_SHARED" not in policy["scopes"] or not policy["server_enabled"] or not policy["allow_remote_query"] or source_ids is not None:
        return merge(local, [], config["top_k"])
    remote = []
    for server in db.scalars(select(KnowledgeServer).where(KnowledgeServer.enabled == 1)):
        sent = remote_query or query
        audit = RemoteQueryAudit(project_id=project_id, server_id=server.id,
                                 query_hash=hashlib.sha256(sent.encode()).hexdigest(), status="RUNNING")
        db.add(audit)
        try:
            provider = provider_factory(server)
            capabilities = set(server.capabilities or provider.capabilities())
            if "SEARCH" not in capabilities:
                raise ValueError("伺服器不支援 SEARCH")
            for rank, item in enumerate(provider.search(sent, config["top_k"], project_id), 1):
                hit = _remote_hit(item, server, rank, "SERVER_SEARCH", config)
                if hit:
                    remote.append(hit)
            if "GRAPH" in capabilities and config.get("graph_retrieval_enabled"):
                for rank, item in enumerate(provider.graph_search(sent, config.get("graph_document_top_k", 3), project_id), 1):
                    hit = _remote_hit(item, server, rank, "SERVER_GRAPH", config)
                    if hit:
                        remote.append(hit)
            audit.status, audit.result_count = "SUCCEEDED", len(remote)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            audit.status, audit.detail = "DEGRADED", str(exc)[:200]
    return merge(local, remote, config["top_k"])


# Function words that separate topics in a requirement sentence. Single
# characters such as 需/可/時 are left alone because they start real words.
_SPLIT = re.compile(r"必須|需要|應該|可以|以及|並且|透過|進行|之後|之前|如果|的|與|及|和|或|並|且|若|則|等|給|讓")
_PUNCT = re.compile(r"[\s，。、；;：:,.!?！？（）()「」『』【】\[\]/\\|\"'“”‘’-]+")


def focus_queries(name, text, limit=3):
    """Short topic phrases for Agent retrieval.

    Remote servers score by matched-term ratio, so one long requirement text
    scores near zero. These phrases are search terms, not the requirement text.
    """
    segments = []
    for clause in _PUNCT.split(text or ""):
        for part in _SPLIT.split(clause):
            part = part.strip()
            if 2 <= len(part) <= 16 and part not in segments:
                segments.append(part)
    queries = []
    name = (name or "").strip()
    if len(name) >= 2:
        queries.append(name[:24])
    for start in range(0, len(segments), 2):
        if len(queries) >= limit:
            break
        phrase = " ".join(segments[start:start + 2])[:24]
        if phrase not in queries:
            queries.append(phrase)
    return queries


def agent_search(db, project, text, config, local_search, provider_factory=None, contract=None):
    """The Agent's own library search: one local pass with the full requirement
    plus focused passes that also reach enabled company file servers.

    With `contract` (a dict), evidence roles the request needs but the hits do
    not cover get one targeted pass each; what each pass saw is recorded there.
    """
    queries = focus_queries(project.name, text)
    passes = [(text or project.name, queries[0] if queries else project.name)]
    passes += [(query, query) for query in queries[1:]]
    order = {"KEY": 0, "SUPPORTING": 1, "CONTEXT": 2, "EXTERNAL_BENCHMARK": 3, "REJECTED": 4}
    best = {}

    def run(local_query, remote_query, role=None):
        found = retrieve(db, local_query, project.id, config, local_search,
                         provider_factory=provider_factory, remote_query=remote_query)
        # Release SQLite's write lock (audit rows) before the next slow
        # embedding or HTTP pass; other workers' heartbeats wait on it.
        db.commit()
        for hit in found:
            current = best.get(hit["source_id"])
            if current is None or (order.get(hit["evidence_role"], 3), -hit.get("case_relevance_score", 0)) < (
                    order.get(current["evidence_role"], 3), -current.get("case_relevance_score", 0)):
                best[hit["source_id"]] = {**hit, "matched_query": remote_query,
                                          **({"contract_role": role} if role else {})}
        return found

    for local_query, remote_query in passes:
        run(local_query, remote_query)
    hits = sorted(best.values(), key=lambda h: (order.get(h["evidence_role"], 3), -h.get("case_relevance_score", 0)))
    limit = max(config.get("top_k", 8), 8)
    if contract is not None:
        from .evidence_contract import ROLES, MAX_ROLE_PASSES, assess, role_query
        admitted = [h for h in hits[:limit] if h["evidence_role"] in {"KEY", "SUPPORTING"}]
        linked_gaps = {r["role"] for r in assess(db, project, text)["roles"] if r["status"] == "GAP"}
        missing = [role for role in ROLES if role in linked_gaps and not any(
            re.search(ROLES[role][2], f"{h.get('title', '')} {h.get('text', '')}", re.I) for h in admitted)]
        attempts = {}
        for role in missing[:MAX_ROLE_PASSES]:
            query = role_query(project.name, role)
            found = run(query, query, role)
            matching = [h for h in found if re.search(ROLES[role][2], f"{h.get('title', '')} {h.get('text', '')}", re.I)]
            attempts[role] = ("ADMITTED" if any(h["evidence_role"] in {"KEY", "SUPPORTING"} for h in matching)
                              else "CANDIDATES_REJECTED" if matching else "NONE_FOUND")
        contract["attempts"] = attempts
        contract["queries"] = [role_query(project.name, role) for role in attempts]
        hits = sorted(best.values(), key=lambda h: (order.get(h["evidence_role"], 3), -h.get("case_relevance_score", 0)))
        # Role passes may add evidence beyond the usual budget, one document per role.
        extra = [h for h in hits[limit:] if h.get("contract_role")]
        return hits[:limit] + extra[:MAX_ROLE_PASSES], queries
    return hits[:limit], queries


def _excerpt(content, query, size=1200):
    terms = sorted({t for t in re.findall(r"[㐀-鿿]{2}|[a-z0-9]{3,}", (query or "").lower())}, key=len, reverse=True)
    lower = content.lower()
    offset = min((lower.find(t) for t in terms if lower.find(t) >= 0), default=0)
    start = max(0, offset - 200)
    return content[start:start + size]


def import_remote_evidence(db, project, server_id, evidence_id, config, query="", provider_factory=None):
    """Copy one company-server hit into this project as a reviewable source.

    The server's cache policy decides how much text may be kept on this host.
    Returns (source, created). The caller decides how to mark the project stale.
    """
    from .db import Link
    from .evidence import ingest
    policy = policy_for(db, project.id, config)
    if "SERVER_SHARED" not in policy["scopes"] or not policy["allow_remote_query"] or not policy["server_enabled"]:
        raise ValueError("此專案的知識政策未允許使用公司檔案服務器")
    server = db.get(KnowledgeServer, server_id)
    if not server or not server.enabled:
        raise ValueError("公司檔案服務器未啟用或已移除")
    if server.cache_policy not in {"SNIPPET_ONLY", "FULL_CONTENT"}:
        raise ValueError("此公司檔案服務器設定為不在本機保留內容；請主機管理者將「本機保留範圍」改為片段或全文後再加入")
    remote_key = f"{server.id}:{evidence_id}"
    linked = set(db.scalars(select(Link.source_id).where(Link.project_id == project.id)))
    for source in db.scalars(select(Source).where(Source.provider == "KNOWLEDGE_SERVER", Source.status == "ACTIVE")):
        if source.id in linked and (source.meta or {}).get("remote_key") == remote_key:
            return source, False
    raw = (provider_factory or HttpKnowledgeServerProvider)(server).fetch_evidence(evidence_id)
    if not isinstance(raw, dict) or not str(raw.get("content", "")).strip():
        raise ValueError("公司檔案服務器沒有回傳可保存的文字內容")
    meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    content = str(raw["content"])
    if server.cache_policy == "SNIPPET_ONLY":
        content = _excerpt(content, query)
    verified = meta.get("authority_class") == "INTERNAL_AUTHORITY" and meta.get("verified_authority") is True
    source = ingest(
        db, str(raw.get("title") or evidence_id)[:300], [{"text": content}], "KNOWLEDGE_SERVER",
        "INTERNAL_POLICY" if verified else "BUSINESS_KNOWLEDGE", None,
        {"library_scope": "PROJECT", "remote_key": remote_key, "provider_id": server.id,
         "provider_name": server.name, "remote_evidence_id": str(evidence_id),
         "remote_uri": str(raw.get("uri", ""))[:1000], "version": meta.get("version"),
         "content_hash": meta.get("content_hash"), "cache_policy": server.cache_policy,
         "excerpt_only": server.cache_policy == "SNIPPET_ONLY"},
    )
    db.add(Link(project_id=project.id, source_id=source.id))
    db.flush()
    return source, True
