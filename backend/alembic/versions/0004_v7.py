"""V7 structured projections, durable enrichment, graph and trash."""

from alembic import op
from sqlalchemy import inspect, Column, String, Text, JSON
from app.db import (
    PRDTemplate,
    GraphNode,
    GraphEdge,
    KnowledgeJob,
    KnowledgeItem,
    ResearchRun,
    ProjectMemory,
)

revision = "0004"
down_revision = "0003"
tables = [
    PRDTemplate,
    GraphNode,
    GraphEdge,
    KnowledgeJob,
    KnowledgeItem,
    ResearchRun,
    ProjectMemory,
]


def upgrade():
    for table, columns in {
        "projects": [
            Column("deleted_at", String, nullable=True),
            Column("purge_after", String, nullable=True),
            Column("deleted_by", String, nullable=False, server_default=""),
            Column("delete_reason", Text, nullable=False, server_default=""),
        ],
        "prd_revisions": [
            Column("presentation", JSON, nullable=False, server_default="{}")
        ],
    }.items():
        existing = {c["name"] for c in inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    for model in tables:
        model.__table__.create(op.get_bind(), checkfirst=True)
    op.execute("DROP TRIGGER IF EXISTS immutable_revision_delete")
    op.execute("""CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON prd_revisions
    WHEN OLD.status IN ('BASELINED','SUPERSEDED') AND NOT EXISTS
    (SELECT 1 FROM projects WHERE id=OLD.project_id AND status='PURGING')
    BEGIN SELECT RAISE(ABORT,'Baseline is immutable'); END""")
    op.execute("""CREATE TRIGGER immutable_presentation BEFORE UPDATE OF presentation ON prd_revisions
    WHEN OLD.status IN ('BASELINED','SUPERSEDED') AND NEW.presentation != OLD.presentation
    BEGIN SELECT RAISE(ABORT,'Baseline presentation is immutable'); END""")


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS immutable_presentation")
    op.execute("DROP TRIGGER IF EXISTS immutable_revision_delete")
    op.execute("""CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON prd_revisions
    WHEN OLD.status IN ('BASELINED','SUPERSEDED') BEGIN SELECT RAISE(ABORT,'Baseline is immutable'); END""")
    for model in reversed(tables):
        model.__table__.drop(op.get_bind(), checkfirst=True)
    op.drop_column("prd_revisions", "presentation")
    for name in ["deleted_at", "purge_after", "deleted_by", "delete_reason"]:
        op.drop_column("projects", name)
