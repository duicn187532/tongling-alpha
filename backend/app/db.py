import os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import (
    create_engine,
    event,
    String,
    Text,
    JSON,
    Integer,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
engine = create_engine(
    os.getenv("DATABASE_URL", f"sqlite:///{DATA / 'app.db'}"),
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def sqlite_setup(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")


Session = sessionmaker(engine, expire_on_commit=False)


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Row:
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    created_at: Mapped[str] = mapped_column(String, default=now)


class Workspace(Row, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String, default="我的工作空間")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)


class ClientSession(Row, Base):
    __tablename__ = "client_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(128), index=True)
    expires_at: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_at: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="PAIRED", server_default="PAIRED")


class AppJob(Row, Base):
    __tablename__ = "app_jobs"
    __table_args__ = (UniqueConstraint("session_id", "idempotency_key"),)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24))
    queue_class: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String, default=now)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    heartbeat_at: Mapped[str] = mapped_column(String, default=now)
    dismissed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class ResourceLease(Row, Base):
    __tablename__ = "resource_leases"
    resource: Mapped[str] = mapped_column(String(24), index=True)
    owner_id: Mapped[str] = mapped_column(String(128), index=True)
    expires_at: Mapped[str] = mapped_column(String)


class KnowledgeServer(Row, Base):
    __tablename__ = "knowledge_servers"
    name: Mapped[str] = mapped_column(String)
    base_url: Mapped[str] = mapped_column(String)
    enabled: Mapped[int] = mapped_column(Integer, default=0)
    auth_env_var: Mapped[str] = mapped_column(String, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    tls_verify: Mapped[int] = mapped_column(Integer, default=1)
    cache_policy: Mapped[str] = mapped_column(String, default="METADATA_ONLY")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)


class RemoteQueryAudit(Row, Base):
    __tablename__ = "remote_query_audit"
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    server_id: Mapped[str] = mapped_column(String)
    query_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String, default="")


class Project(Row, Base):
    __tablename__ = "projects"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="ACTIVE")
    mode: Mapped[str] = mapped_column(String, default="AGENT")
    runtime: Mapped[str] = mapped_column(String, default="READY")
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    current_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_by: Mapped[str] = mapped_column(String, default="")
    delete_reason: Mapped[str] = mapped_column(Text, default="")
    purge_after: Mapped[str | None] = mapped_column(String, nullable=True)


class Source(Row, Base):
    __tablename__ = "sources"
    title: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="USER_UPLOAD")
    provider: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="ACTIVE")


class Link(Base):
    __tablename__ = "project_source_links"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, default="EVIDENCE")


class Chunk(Row, Base):
    __tablename__ = "chunks"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Assertion(Row, Base):
    __tablename__ = "source_assertions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    topic: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON)


class Requirement(Row, Base):
    __tablename__ = "requirements"
    __table_args__ = (UniqueConstraint("project_id", "topic"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    topic: Mapped[str] = mapped_column(String)
    statement: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)


class Meeting(Row, Base):
    __tablename__ = "meetings"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    title: Mapped[str] = mapped_column(String)
    meeting_date: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String, default="")
    timezone: Mapped[str] = mapped_column(String, default="Asia/Taipei")
    participants: Mapped[list] = mapped_column(JSON, default=list)
    transcript: Mapped[str] = mapped_column(Text, default="")
    events: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="IMPORTED")
    analysis_error: Mapped[str] = mapped_column(Text, default="")
    record_source_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Revision(Row, Base):
    __tablename__ = "prd_revisions"
    __table_args__ = (UniqueConstraint("project_id", "version"),)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("prd_revisions.id"), nullable=True
    )
    version: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="DRAFT")
    content: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String)
    diff: Mapped[list] = mapped_column(JSON, default=list)
    changes: Mapped[list] = mapped_column(JSON, default=list)
    docx_path: Mapped[str] = mapped_column(String, default="")
    baselined_at: Mapped[str | None] = mapped_column(String, nullable=True)
    presentation: Mapped[dict] = mapped_column(JSON, default=dict)


class TechnicalReview(Row, Base):
    __tablename__ = "technical_reviews"
    revision_id: Mapped[str] = mapped_column(ForeignKey("prd_revisions.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(12))
    reviewer: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(Text, default="")


class DeliveryPackage(Row, Base):
    __tablename__ = "delivery_packages"
    revision_id: Mapped[str] = mapped_column(ForeignKey("prd_revisions.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    path: Mapped[str] = mapped_column(Text)


class Change(Row, Base):
    __tablename__ = "change_requests"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    source_ref: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="OPEN")
    impact: Mapped[dict] = mapped_column(JSON, default=dict)
    resolution: Mapped[dict] = mapped_column(JSON, default=dict)


class Run(Row, Base):
    __tablename__ = "workflow_runs"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="RUNNING")
    error: Mapped[str] = mapped_column(Text, default="")
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)


class Activity(Row, Base):
    __tablename__ = "ai_activities"
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="QUEUED")
    phase: Mapped[str] = mapped_column(String, default="排隊等待執行")
    detail: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String, default="")
    heartbeat_at: Mapped[str] = mapped_column(String, default=now)
    updated_at: Mapped[str] = mapped_column(String, default=now)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    dismissed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class Interruption(Row, Base):
    __tablename__ = "human_interruptions"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="OPEN")


class Event(Base):
    __tablename__ = "agent_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=now)


class PRDTemplate(Row, Base):
    __tablename__ = "prd_templates"
    name: Mapped[str] = mapped_column(String)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class GraphNode(Base):
    __tablename__ = "graph_nodes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    origin: Mapped[str] = mapped_column(ForeignKey("graph_nodes.id"), index=True)
    target: Mapped[str] = mapped_column(ForeignKey("graph_nodes.id"), index=True)
    relation: Mapped[str] = mapped_column(String)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    chunk_id: Mapped[str] = mapped_column(String)
    meta: Mapped[dict] = mapped_column(JSON)


class KnowledgeJob(Row, Base):
    __tablename__ = "knowledge_jobs"
    kind: Mapped[str] = mapped_column(String)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="QUEUED")
    stage: Mapped[str] = mapped_column(String, default="DISCOVER")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[str] = mapped_column(String, default=now)


class KnowledgeItem(Row, Base):
    __tablename__ = "knowledge_items"
    __table_args__ = (UniqueConstraint("job_id", "chunk_id"),)
    job_id: Mapped[str] = mapped_column(ForeignKey("knowledge_jobs.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    chunk_id: Mapped[str] = mapped_column(String)
    source_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="QUEUED")
    stage: Mapped[str] = mapped_column(String, default="PARSE_CHECK")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String, default=now)
    lease_until: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_token: Mapped[str] = mapped_column(String, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class ResearchRun(Row, Base):
    __tablename__ = "research_runs"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    status: Mapped[str] = mapped_column(String, default="QUEUED")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ProjectMemory(Row, Base):
    __tablename__ = "project_memory"
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    topic_key: Mapped[str] = mapped_column(String)
    canonical_ref: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE")
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Connector(Row, Base):
    __tablename__ = "source_connectors"
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(Text)
    inventory: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)


def serialize(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def log(db, project_id, kind, message, data=None):
    db.add(Event(project_id=project_id, kind=kind, message=message, data=data or {}))
