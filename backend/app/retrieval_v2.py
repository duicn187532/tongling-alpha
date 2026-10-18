"""Measured opt-in retrieval pipeline shared by Agent, UI and benchmark."""

import json
import math
import re
import time
from datetime import date
from pathlib import Path
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, Field
from typing import Literal
from rank_bm25 import BM25Okapi
from sqlalchemy import select
from .db import Chunk, Project, Source, Link, DATA
from .evidence import tokens, vectors
from .retrieval_providers import (
    LocalCrossEncoderReranker,
    OllamaEmbedding,
    OllamaReranker,
    fingerprint,
)
from .retrieval_structure import logical_document, structural_chunks, sidecar_path


class RetrievalConfig(BaseModel):
    lexical_chunks: int = Field(default=50, ge=1, le=2000)
    semantic_chunks: int = Field(default=50, ge=1, le=2000)
    candidate_documents: int = Field(default=40, ge=1, le=200)
    final_k: int = Field(default=10, ge=1, le=100)
    aggregation: Literal["max", "top_n_mean", "weighted_top_n"] = "max"
    fusion: Literal["rrf", "minmax", "rank", "zscore"] = "minmax"
    lexical_weight: float = Field(default=0.4, ge=0, le=1)
    semantic: bool = False
    legacy_hash: bool = False
    embedding: dict = Field(default_factory=dict)
    reranker: dict = Field(default_factory=dict)
    rerank: bool = False
    authority: bool = False
    temporal: bool = False
    structured: bool = False
    dynamic_weights: bool = False
    allow_local_fallback: bool = True
    trace: bool = True
    filters: dict = Field(default_factory=dict)
    minimum_score: float = Field(default=0.45, ge=0, le=1)


def analyze_query(query):
    lower = query.lower()
    kinds = []
    if re.search(r"\b[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+\b|http\s*\d{3}", lower):
        kinds += ["technical", "exact"]
    if re.search("目前|現在|現行|current|baseline", lower):
        kinds.append("current")
    if re.search(r"歷史|以前|舊版|上個版本|histor|\b20\d{2}\b", lower):
        kinds.append("historical")
    if re.search("衝突|差異|修改原因|為什麼修改|比較|conflict|compar", lower):
        kinds.append("conflict")
    if re.search("權威|核准|決議|authority|approv", lower):
        kinds.append("authority")
    if re.search("哪些|各自|整合|跨|multiple|all ", lower):
        kinds.append("multi_document")
    if not kinds:
        kinds = ["semantic"]
    dates = re.findall(r"\b20\d{2}(?:-\d{2}(?:-\d{2})?)?\b", query)
    return {"labels": kinds, "requested_date": dates[0] if dates else None}


def normalize(values, method):
    values = np.asarray(values, dtype=float)
    if not len(values):
        return values
    if not np.isfinite(values).all():
        raise ValueError("Non-finite score")
    if np.ptp(values) < 1e-12:
        return np.zeros_like(values)
    if method == "minmax":
        return (values - values.min()) / np.ptp(values)
    if method == "rank":
        unique = sorted(set(values))
        ranks = {v: i / max(1, len(unique) - 1) for i, v in enumerate(unique)}
        return np.array([ranks[v] for v in values])
    if method == "zscore":
        z = np.clip((values - values.mean()) / max(values.std(), 1e-12), -20, 20)
        return 1 / (1 + np.exp(-z))
    if method == "rrf":
        order = np.argsort(-values, kind="stable")
        result = np.zeros(len(values))
        for rank, index in enumerate(order):
            result[index] = 1 / (61 + rank)
        return result
    raise ValueError("Unknown normalization")


def aggregate(scores, method):
    best = sorted(scores, reverse=True)[:3]
    if method == "max":
        return best[0]
    if method == "top_n_mean":
        return sum(best) / len(best)
    weights = [0.6, 0.3, 0.1][: len(best)]
    return sum(v * w for v, w in zip(best, weights)) / sum(weights)


def rerank_passage(document, mode):
    evidence = document["evidence_chunks"]
    if mode == "chunk":
        return evidence[0]["text"] if evidence else document["text"]
    if mode == "context":
        heading = document["meta"].get("heading_path") or document["meta"].get("heading", "")
        return (
            f"Document: {document['title']}\nSection: {heading}\nContent: "
            + document.get("_parent_context", document["text"])
        )[:1800]
    if mode == "evidence":
        return "\n".join(item["text"] for item in evidence)[:1800]
    raise ValueError("Unknown reranker input mode")


def missing_exact_identifier(query, document):
    """Require a cited passage for a single explicit technical identifier.

    Multi-identifier comparison queries may legitimately need separate sources,
    so this guard applies only when one identifier is named.
    """
    candidates = re.findall(r"\b[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+\b", query)
    identifiers = {
        token for token in candidates
        if "_" in token or re.fullmatch(r"[A-Z]{2,}[A-Z0-9]*-\d+", token)
    }
    if len(identifiers) != 1:
        return False
    cited = document["title"] + "\n" + "\n".join(
        item["text"] for item in document["evidence_chunks"]
    )
    return next(iter(identifiers)).casefold() not in cited.casefold()


def metadata_adjustment(meta, intent, relevance, authority, temporal, today=None):
    # Bounded corrections only after a relevance floor; never promote irrelevant authority.
    if relevance < 0.25:
        return 0.0, 0.0
    auth = (
        {"high": 0.04, "medium": 0.02, "low": 0}.get(
            str(meta.get("authority_level", meta.get("authority", "unknown"))).lower(),
            0.0,
        )
        if authority and relevance >= 0.45
        else 0.0
    )
    adjustment = 0.0
    labels = intent["labels"]
    status = str(
        meta.get("document_status", meta.get("lifecycle_status", "UNKNOWN"))
    ).upper()
    valid_from = str(meta.get("valid_from", meta.get("effective_date", "")) or "")
    valid_to = str(meta.get("valid_to", "") or "")
    if temporal and ("current" in labels or "historical" in labels or "conflict" in labels):
        if "conflict" in labels:
            return auth, 0.0
        requested = intent.get("requested_date")
        if "historical" in labels:
            if requested:
                if (
                    valid_from
                    and valid_from[: len(requested)] <= requested
                    and (not valid_to or requested < valid_to[: len(requested)])
                ):
                    adjustment = 0.03
                elif valid_from and valid_from[: len(requested)] > requested:
                    adjustment = -0.05
            elif status in {"SUPERSEDED", "EXPIRED"}:
                adjustment = 0.03
        elif "current" in labels:
            current = today or date.today().isoformat()
            if (valid_from and valid_from > current) or (
                valid_to and valid_to <= current
            ):
                adjustment = -0.05
            elif status in {"SUPERSEDED", "EXPIRED"}:
                adjustment = -0.05
            elif status == "CURRENT":
                adjustment = 0.03
    return auth, adjustment


def matches_filters(source, filters):
    aliases = {
        "version": "document_version",
        "status": "lifecycle_status",
        "effective_date": "effective_date",
    }
    for key, value in filters.items():
        if key not in {
            "project_id",
            "document_type",
            "system",
            "version",
            "status",
            "effective_date",
            "authority_type",
        }:
            raise ValueError("Unsupported metadata filter: " + key)
        actual = source.meta.get(key, source.meta.get(aliases.get(key, key)))
        if isinstance(value, list):
            if actual not in value:
                return False
        elif actual != value:
            return False
    return True


class RetrievalPipelineV2:
    def __init__(self, root=None, embedding_provider=None, reranker_provider=None):
        self.root = Path(root) if root else DATA / "retrieval_v2"
        self.embedding_provider = embedding_provider
        self.reranker_provider = reranker_provider

    def search(self, db, query, project_id, settings, strategy=None, source_ids=None):
        from .retrieval import qualify

        started = time.perf_counter()
        cfg = RetrievalConfig(**settings.get("retrieval_v2", {}))
        cfg.final_k = settings.get("top_k", cfg.final_k)
        intent = analyze_query(query)
        trace = {
            "trace_id": str(uuid4()),
            "query": query,
            "query_type": intent,
            "project_scope": project_id,
            "config": cfg.model_dump(),
            "timings": {},
            "fallbacks": [],
            "errors": [],
            "lexical_enabled": strategy != "vector",
            "semantic_enabled": False,
            "rerank_applied": False,
        }
        if project_id:
            project = db.get(Project, project_id)
            if not project or project.status != "ACTIVE":
                raise ValueError("專案不存在或已移至垃圾桶")
        linked = (
            set(db.scalars(select(Link.source_id).where(Link.project_id == project_id)))
            if project_id
            else set()
        )
        visible = [
            s
            for s in db.scalars(
                select(Source).where(Source.status.in_(["ACTIVE", "SUPERSEDED"]))
            )
            if s.kind != "MEETING_TRANSCRIPT"
            and (s.meta.get("library_scope") == "GLOBAL" or s.id in linked)
        ]
        filters = dict(cfg.filters)
        requested_project = filters.pop("project_id", None)
        if requested_project is not None and requested_project != project_id:
            raise ValueError("Metadata project filter must match request scope")
        sources = {
            s.id: s
            for s in visible
            if (
                requested_project is None
                or s.id in linked
                or s.meta.get("project_id") == requested_project
            )
            and matches_filters(s, filters)
            and (source_ids is None or s.id in source_ids)
        }
        chunks = []
        stored = {}
        for row in db.scalars(select(Chunk).where(Chunk.source_id.in_(list(sources)))):
            stored.setdefault(row.source_id, []).append(row)
        for sid, source in sources.items():
            docid = logical_document(
                source,
                project_id if source.meta.get("library_scope") == "PROJECT" else None,
            )
            rows = stored.get(sid, [])
            if cfg.structured:
                path = sidecar_path(self.root, source)
                if path.exists():
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload["source_hash"] != source.sha256:
                        raise ValueError("Stale structural index")
                    values = payload["chunks"]
                else:
                    values = structural_chunks(
                        [{"text": c.text, **c.meta} for c in rows], source.title, docid
                    )
                for c in values:
                    # Source references retain actual persisted chunk IDs where known.
                    matching = next(
                        (
                            r.id
                            for r in rows
                            if r.text in c["text"] or c["text"] in r.text
                        ),
                        None,
                    )
                    chunks.append(
                        {
                            "source": source,
                            "text": c["text"],
                            "chunk_id": matching,
                            "logical_chunk_id": c["logical_chunk_id"],
                            "meta": c["meta"],
                            "document_id": docid,
                        }
                    )
            else:
                occurrences = {}
                for c in rows:
                    key = fingerprint(
                        {"heading": c.meta.get("heading", ""), "text": c.text}
                    )
                    n = occurrences.get(key, 0)
                    occurrences[key] = n + 1
                    chunks.append(
                        {
                            "source": source,
                            "text": c.text,
                            "chunk_id": c.id,
                            "logical_chunk_id": f"{docid}:{key}:{n}",
                            "meta": c.meta,
                            "document_id": docid,
                        }
                    )
        trace["eligible_documents"] = len(sources)
        if not chunks or not query.strip():
            return self.finish([], trace, started, cfg)
        texts = [
            c["source"].title
            + "\nSection: "
            + str(c["meta"].get("heading_path", c["meta"].get("heading", "")))
            + "\n"
            + c["text"]
            for c in chunks
        ]
        t = time.perf_counter()
        qt = tokens(query)
        bm = BM25Okapi([tokens(x) for x in texts])
        # Common terms in small project corpora can receive negative Okapi IDF.
        # Keep matched common terms retrievable rather than dropping the entire corpus.
        bm.idf = {term: max(0.01, value) for term, value in bm.idf.items()}
        lexical = bm.get_scores(qt)
        trace["timings"]["lexical_ms"] = (time.perf_counter() - t) * 1000
        semantic = None
        dense = cfg.semantic and strategy != "bm25"
        if dense or (cfg.legacy_hash and strategy != "bm25"):
            t = time.perf_counter()
            try:
                if dense:
                    provider = self.embedding_provider or OllamaEmbedding(
                        cfg.embedding, self.root / "embedding_cache.db"
                    )
                    matrix = provider.embed_documents(texts)
                    q = provider.embed_query(query)
                    trace["embedding_model"] = provider.metadata()
                else:
                    matrix = vectors(texts)
                    q = vectors([query])[0]
                    trace["embedding_model"] = {
                        "provider": "legacy_hash",
                        "semantic": False,
                    }
                trace["timings"]["embedding_ms"] = (time.perf_counter() - t) * 1000
                t = time.perf_counter()
                semantic = matrix @ q
                trace["timings"]["vector_search_ms"] = (time.perf_counter() - t) * 1000
                trace["semantic_enabled"] = dense
            except Exception as exc:
                trace["fallbacks"].append(
                    {"channel": "semantic", "reason": str(exc)[:500]}
                )
                if not cfg.allow_local_fallback:
                    raise
                if strategy == "vector":
                    return self.finish([], trace, started, cfg)
        t = time.perf_counter()
        use_lexical = strategy != "vector" or semantic is None
        ln = normalize(lexical, cfg.fusion) if use_lexical else np.zeros(len(chunks))
        sn = (
            normalize(semantic, cfg.fusion)
            if semantic is not None
            else np.zeros(len(chunks))
        )
        weight = cfg.lexical_weight
        if cfg.dynamic_weights and "technical" in intent["labels"]:
            weight = max(weight, 0.65)
        if semantic is None:
            weight = 1.0
        elif not use_lexical:
            weight = 0.0
        combined = weight * ln + (1 - weight) * sn
        if cfg.fusion == "rrf":
            combined = combined * 61
        # Expand prefixes until enough distinct documents, never force irrelevant zero lexical scores.
        orders = []
        if use_lexical:
            orders.append(
                (
                    [
                        int(i)
                        for i in np.argsort(-lexical, kind="stable")
                        if lexical[i] > 0
                    ],
                    cfg.lexical_chunks,
                    "BM25",
                )
            )
        if semantic is not None:
            orders.append(
                (
                    [
                        int(i)
                        for i in np.argsort(-semantic, kind="stable")
                        if semantic[i] > 0
                    ],
                    cfg.semantic_chunks,
                    "SEMANTIC" if dense else "LEGACY_HASH",
                )
            )
        selected = {}
        requested = {}
        for order, limit, channel in orders:
            requested[channel] = limit
            count = 0
            docs = set()
            for i in order:
                if count >= limit and len(docs) >= cfg.candidate_documents:
                    break
                selected.setdefault(i, []).append(channel)
                docs.add(chunks[i]["source"].id)
                count += 1
        grouped = {}
        for i in selected:
            grouped.setdefault(chunks[i]["source"].id, []).append(i)
        if source_ids is not None:
            for sid in sources:
                if sid not in grouped:
                    available = [
                        i for i, c in enumerate(chunks) if c["source"].id == sid
                    ]
                    if available:
                        best = max(available, key=lambda i: combined[i])
                        grouped[sid] = [best]
                        selected[best] = ["ATTACHED_SOURCE"]
            cfg.candidate_documents = max(cfg.candidate_documents, len(grouped))
        ranked = sorted(
            grouped,
            key=lambda sid: (
                -aggregate([float(combined[i]) for i in grouped[sid]], cfg.aggregation),
                logical_document(sources[sid], project_id),
            ),
        )[: cfg.candidate_documents]
        trace.update(
            requested_top_k_chunks=requested,
            actual_chunk_count=len(selected),
            requested_candidate_documents=cfg.candidate_documents,
            actual_candidate_documents=len(ranked),
            requested_final_k=cfg.final_k,
        )
        docs = []
        source_order = {}
        if cfg.rerank and cfg.reranker.get("input_mode") == "context":
            for i, chunk in enumerate(chunks):
                source_order.setdefault(chunk["source"].id, []).append(i)
        for sid in ranked:
            ids = sorted(grouped[sid], key=lambda i: -combined[i])
            best = chunks[ids[0]]
            evidence = []
            for i in ids[:3]:
                c = chunks[i]
                evidence.append(
                    {
                        "chunk_id": c["chunk_id"],
                        "chunk_logical_id": c["logical_chunk_id"],
                        "text": c["text"],
                        "meta": c["meta"],
                        "score": float(combined[i]),
                    }
                )
            docs.append(
                {
                    "source_id": sid,
                    "document_id": best["document_id"],
                    "document_logical_id": best["document_id"],
                    "chunk_id": best["chunk_id"],
                    "chunk_logical_id": best["logical_chunk_id"],
                    "title": sources[sid].title,
                    "text": best["text"],
                    "kind": sources[sid].kind,
                    "meta": {**sources[sid].meta, **best["meta"]},
                    "evidence_chunks": evidence,
                    "lexical_score": float(lexical[ids[0]]) if use_lexical else None,
                    "semantic_score": float(semantic[ids[0]])
                    if semantic is not None
                    else None,
                    "candidate_score": aggregate(
                        [float(combined[i]) for i in ids], cfg.aggregation
                    ),
                    "rerank_score": None,
                    "retrieval_channels": sorted(
                        {ch for i in ids for ch in selected[i]}
                    ),
                }
            )
            if source_order:
                order = source_order[sid]
                position = order.index(ids[0])
                docs[-1]["_parent_context"] = "\n".join(
                    chunks[i]["text"]
                    for i in order[max(0, position - 1) : position + 2]
                )
        trace["timings"]["candidate_merge_ms"] = (time.perf_counter() - t) * 1000
        trace["candidate_documents"] = [
            {
                "source_id": d["source_id"],
                "candidate_score": d["candidate_score"],
                "chunks": [e["chunk_logical_id"] for e in d["evidence_chunks"]],
            }
            for d in docs
        ]
        if cfg.rerank and docs:
            t = time.perf_counter()
            try:
                if self.reranker_provider:
                    ranker = self.reranker_provider
                elif cfg.reranker.get("provider") == "local_cross_encoder":
                    ranker = LocalCrossEncoderReranker(
                        cfg.reranker, self.root / "cross_encoder_cache.db"
                    )
                elif cfg.reranker.get("provider", "ollama") == "ollama":
                    ranker = OllamaReranker(
                        cfg.reranker, self.root / "rerank_cache.db"
                    )
                else:
                    raise ValueError("Unknown reranker provider")
                input_mode = cfg.reranker.get("input_mode", "evidence")
                scores = ranker.rerank(
                    query,
                    [
                        {
                            "id": d["source_id"],
                            "title": d["title"],
                            "text": rerank_passage(d, input_mode),
                        }
                        for d in docs
                    ],
                )
                if set(scores) != set(d["source_id"] for d in docs):
                    raise ValueError("Incomplete reranker output")
                if any(
                    not math.isfinite(float(v)) or not 0 <= float(v) <= 1
                    for v in scores.values()
                ):
                    raise ValueError("Invalid reranker score")
                for d in docs:
                    d["rerank_score"] = float(scores[d["source_id"]])
                trace.update(rerank_applied=True, reranker_model=ranker.metadata())
            except Exception as exc:
                trace["fallbacks"].append(
                    {"channel": "reranker", "reason": str(exc)[:500]}
                )
                if not cfg.allow_local_fallback:
                    raise
            trace["timings"]["rerank_ms"] = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
        for d in docs:
            if sources[d["source_id"]].status == "SUPERSEDED":
                d["meta"] = {**d["meta"], "lifecycle_status": "SUPERSEDED"}
            relevance = (
                d["rerank_score"] if trace["rerank_applied"] else d["candidate_score"]
            )
            auth, temp = metadata_adjustment(
                d["meta"], intent, relevance, cfg.authority, cfg.temporal
            )
            d.update(
                authority_score=auth,
                temporal_score=temp,
                final_score=relevance + auth + temp,
                rerank_applied=trace["rerank_applied"],
                fallback_reason=trace["fallbacks"],
            )
            # Relative fused score is not confidence. Require absolute lexical coverage
            # or cosine evidence; metadata must never make unsupported evidence accepted.
            coverage = len(set(qt) & set(tokens(d["text"] + " " + d["title"]))) / max(
                1, len(set(qt))
            )
            support = (
                d["rerank_score"]
                if trace["rerank_applied"]
                else max(coverage, d["semantic_score"] or 0)
            )
            if missing_exact_identifier(query, d):
                support = 0.0
                d["admission_exclusion"] = "Exact identifier absent from cited passages"
            d.update(
                qualify(
                    sources[d["source_id"]],
                    min(1, max(0, support)),
                    "v2: candidate aggregation; bounded metadata; see retrieval trace",
                    support >= cfg.minimum_score,
                    settings,
                )
            )
            d.update(
                score=d["final_score"],
                reason=d["case_relevance_reason"],
                qualification_method="v2 source-supported relevance (not probability)",
                document_version=d["meta"].get(
                    "version", d["meta"].get("document_version")
                ),
                document_status=d["meta"].get(
                    "document_status", d["meta"].get("lifecycle_status", "UNKNOWN")
                ),
            )
            # Historical evidence remains visible but cannot silently become current key evidence.
            if str(d["document_status"]).upper() in {"SUPERSEDED", "EXPIRED", "DRAFT"}:
                d["temporal_status"] = str(d["document_status"]).upper()
                if d["evidence_role"] == "KEY":
                    d["evidence_role"] = "CONTEXT"
        docs.sort(key=lambda d: (-d["final_score"], d["document_id"]))
        for d in docs:
            d.pop("_parent_context", None)
        trace["timings"]["metadata_ranking_ms"] = (time.perf_counter() - t) * 1000
        return self.finish(docs[: cfg.final_k], trace, started, cfg)

    def finish(self, results, trace, started, cfg):
        trace["actual_final_k"] = len(results)
        trace["accepted_evidence"] = any(
            r.get("evidence_role") in {"KEY", "SUPPORTING"} for r in results
        )
        trace["timings"]["total_ms"] = (time.perf_counter() - started) * 1000
        for i, r in enumerate(results, 1):
            r.update(rank=i, retrieval_trace_id=trace["trace_id"])
        if cfg.trace:
            path = self.root / "traces"
            path.mkdir(parents=True, exist_ok=True)
            (path / (trace["trace_id"] + ".json")).write_text(
                json.dumps(trace, ensure_ascii=False), encoding="utf-8"
            )
        return {
            "results": results,
            "trace": trace,
            "accepted_evidence": trace["accepted_evidence"],
        }
