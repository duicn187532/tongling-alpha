import hashlib, re, unicodedata
from typing import Protocol, Literal
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from .db import GraphNode, GraphEdge, Source, Link, Project, now

NODE_TYPES = [
    "DOCUMENT",
    "CHUNK",
    "TAG",
    "TOPIC",
    "ENTITY",
    "PRODUCT",
    "SYSTEM",
    "API_OR_MESSAGE",
    "DATA_OBJECT",
    "ACTOR",
    "REQUIREMENT",
    "BUSINESS_RULE",
    "MEETING",
    "DECISION",
    "POLICY",
    "RISK_OR_CONTROL",
]
RELATIONS = [
    "CONTAINS",
    "TAGGED_WITH",
    "MENTIONS",
    "RELATES_TO",
    "PART_OF_PROJECT",
    "SUPPORTS",
    "CONFLICTS_WITH",
    "SUPERSEDES",
    "AFFECTS",
    "DECIDED_BY",
    "DERIVED_FROM",
    "SAME_TOPIC",
    "REFERENCES",
]
TAXONOMY = [
    "DOMAIN",
    "FUNCTION",
    "PRODUCT",
    "SYSTEM",
    "INTERFACE",
    "DATA",
    "ACTOR",
    "RISK",
    "COMPLIANCE",
    "BUSINESS_RULE",
    "PROCESS",
    "PROJECT_TOPIC",
    "DOCUMENT_TYPE",
]


def normalize(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip()).casefold()


def key(*values):
    return hashlib.sha256("|".join(values).encode()).hexdigest()


class TagItem(BaseModel):
    tag: str = Field(min_length=1, max_length=80)
    tag_type: Literal[
        "DOMAIN",
        "FUNCTION",
        "PRODUCT",
        "SYSTEM",
        "INTERFACE",
        "DATA",
        "ACTOR",
        "RISK",
        "COMPLIANCE",
        "BUSINESS_RULE",
        "PROCESS",
        "PROJECT_TOPIC",
        "DOCUMENT_TYPE",
    ] = "PROJECT_TOPIC"
    confidence: float = Field(default=0.6, ge=0, le=1)


class Tags(BaseModel):
    tags: list[TagItem] = Field(default_factory=list, max_length=12)


class Entity(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = "ENTITY"
    quote: str = Field(min_length=1, max_length=500)
    confidence: float = Field(default=0.6, ge=0, le=1)


class Entities(BaseModel):
    entities: list[Entity] = Field(default_factory=list, max_length=15)


class Relation(BaseModel):
    origin: str
    target: str
    relation: str = "RELATES_TO"
    quote: str = Field(min_length=1, max_length=500)
    confidence: float = Field(default=0.5, ge=0, le=1)


class Relations(BaseModel):
    relations: list[Relation] = Field(default_factory=list, max_length=20)


class GraphStore(Protocol):
    def upsert_nodes(self, nodes): ...
    def upsert_edges(self, edges): ...
    def neighbors(self, node_ids, depth, allowed): ...
    def search_nodes(self, query, top_k, allowed): ...
    def documents_for_nodes(self, node_ids, allowed): ...


def visible_sources(db, project_id=None):
    linked = (
        set(
            db.scalars(
                select(Link.source_id)
                .join(Project, Project.id == Link.project_id)
                .where(Project.id == project_id, Project.status == "ACTIVE")
            )
        )
        if project_id
        else set()
    )
    return [
        s
        for s in db.scalars(select(Source).where(Source.status == "ACTIVE"))
        if s.kind != "MEETING_TRANSCRIPT"
        and (s.meta.get("library_scope") == "GLOBAL" or s.id in linked)
    ]


class SQLiteGraphStore:
    def __init__(self, db):
        self.db = db

    def upsert_nodes(self, nodes):
        for n in nodes:
            old = self.db.get(GraphNode, n["id"])
            if not old:
                self.db.add(GraphNode(**n))
        self.db.flush()

    def upsert_edges(self, edges):
        for e in edges:
            old = self.db.get(GraphEdge, e["id"])
            if old:
                old.meta = e["meta"]
            else:
                self.db.add(GraphEdge(**e))
        self.db.flush()

    def neighbors(self, node_ids, depth, allowed):
        inactive = {
            n.id
            for n in self.db.scalars(select(GraphNode))
            if n.meta.get("status", "ACTIVE") != "ACTIVE"
        }
        found = {x: 0 for x in node_ids}
        paths = []
        front = set(node_ids)
        for distance in range(1, depth + 1):
            following = set()
            for e in self.db.scalars(
                select(GraphEdge).where(GraphEdge.source_id.in_(allowed))
            ):
                if e.origin in inactive or e.target in inactive:
                    continue
                if e.origin in front or e.target in front:
                    other = e.target if e.origin in front else e.origin
                    if other not in found:
                        found[other] = distance
                        following.add(other)
                    paths.append(
                        {
                            "edge_id": e.id,
                            "origin": e.origin,
                            "relation": e.relation,
                            "target": e.target,
                            "source_ref": e.source_id,
                            "confidence": e.meta.get("confidence", 0.5),
                            "distance": distance,
                        }
                    )
            front = following
            if not front:
                break
        return found, paths

    def search_nodes(self, query, top_k, allowed):
        from .evidence import tokens, vectors

        connected = {
            v
            for e in self.db.scalars(
                select(GraphEdge).where(GraphEdge.source_id.in_(allowed))
            )
            for v in (e.origin, e.target)
        }
        nodes = [
            n
            for n in self.db.scalars(select(GraphNode))
            if n.id in connected
            and n.kind not in {"DOCUMENT", "CHUNK"}
            and n.meta.get("status", "ACTIVE") == "ACTIVE"
        ]
        if not nodes:
            return []
        q = set(tokens(query))
        matrix = vectors([n.label + " " + " ".join(n.aliases) for n in nodes] + [query])
        ranked = []
        for i, n in enumerate(nodes):
            score = max(
                float(matrix[i] @ matrix[-1]),
                len(q & set(tokens(n.label))) / max(1, len(q)),
            )
            if score > 0.08:
                ranked.append((n.id, score, n.label))
        return sorted(ranked, key=lambda x: -x[1])[:top_k]

    def documents_for_nodes(self, node_ids, allowed):
        return sorted(
            {
                e.source_id
                for e in self.db.scalars(
                    select(GraphEdge).where(GraphEdge.source_id.in_(allowed))
                )
                if e.origin in node_ids or e.target in node_ids
            }
        )


def persist_chunk(db, source, chunk, result, version, model):
    store = SQLiteGraphStore(db)
    doc = "doc:" + source.id
    unit = "chunk:" + chunk.id
    nodes = [
        {"id": doc, "kind": "DOCUMENT", "label": source.title},
        {"id": unit, "kind": "CHUNK", "label": chunk.text[:60]},
    ]
    edges = []

    def edge(a, relation, b, confidence=1, quote=""):
        edges.append(
            {
                "id": key(a, relation, b, source.id, chunk.id),
                "origin": a,
                "target": b,
                "relation": relation,
                "source_id": source.id,
                "chunk_id": chunk.id,
                "meta": {
                    "confidence": confidence,
                    "quote": quote,
                    "source_ref": source.id,
                    "extractor": model,
                    "created_at": now(),
                    "graph_build_version": version,
                    "source_hash": source.sha256,
                },
            }
        )

    edge(doc, "CONTAINS", unit)
    entities = {}
    for t in result.get("tags", []):
        n = key("TAG", t.get("tag_type", "PROJECT_TOPIC"), normalize(t["tag"]))
        existing = db.get(GraphNode, n)
        if existing and existing.meta.get("status") == "REJECTED":
            continue
        if existing and existing.meta.get("merged_into"):
            n = existing.meta["merged_into"]
        nodes.append(
            {
                "id": n,
                "kind": "TAG",
                "label": t["tag"],
                "meta": {
                    "tag_type": t.get("tag_type")
                    if t.get("tag_type") in TAXONOMY
                    else "PROJECT_TOPIC",
                    "source": "RULE" if model.startswith("mock") else "AI",
                    "status": "ACTIVE",
                },
            }
        )
        edge(unit, "TAGGED_WITH", n, t.get("confidence", 0.5))
    for e in result.get("entities", []):
        if e["quote"] not in chunk.text or e["kind"] not in NODE_TYPES:
            continue
        n = key(e["kind"], normalize(e["name"]))
        entities[e["name"]] = n
        nodes.append({"id": n, "kind": e["kind"], "label": e["name"]})
        edge(unit, "MENTIONS", n, e["confidence"], e["quote"])
    for e in result.get("relations", []):
        if (
            e["origin"] in entities
            and e["target"] in entities
            and e["quote"] in chunk.text
            and e["relation"] in RELATIONS
        ):
            edge(
                entities[e["origin"]],
                e["relation"],
                entities[e["target"]],
                e["confidence"],
                e["quote"],
            )
    store.upsert_nodes(nodes)
    removal = delete(GraphEdge).where(
        GraphEdge.source_id == source.id, GraphEdge.chunk_id == chunk.id
    )
    if "entities" not in result:
        removal = removal.where(GraphEdge.relation == "TAGGED_WITH")
    db.execute(removal)
    store.upsert_edges(edges)


def graph_search(db, query, project_id=None, top_k=3, hops=2):
    sources = {s.id: s for s in visible_sources(db, project_id)}
    store = SQLiteGraphStore(db)
    seeds = store.search_nodes(query, 8, sources)
    scores = {}
    explanations = {}
    for node, score, label in seeds:
        expanded, paths = store.neighbors([node], hops, sources)
        for sid in store.documents_for_nodes(expanded, sources):
            valid = [p for p in paths if p["source_ref"] == sid]
            if not valid:
                continue
            distance = min(p["distance"] for p in valid)
            weight = score * max(p["confidence"] for p in valid) / (distance or 1)
            scores[sid] = max(scores.get(sid, 0), weight)
            explanations.setdefault(sid, []).append(
                {"matched_node": label, "hop_distance": distance, "paths": valid[:4]}
            )
    return [
        {
            "source_id": sid,
            "graph_score": round(score, 4),
            "paths": explanations[sid],
            "title": sources[sid].title,
        }
        for sid, score in sorted(scores.items(), key=lambda x: -x[1])[:top_k]
    ]
