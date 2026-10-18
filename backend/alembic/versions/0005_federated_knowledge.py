"""Federated knowledge servers and privacy-safe query audit."""

from alembic import op
from app.db import KnowledgeServer, RemoteQueryAudit

revision = "0005"
down_revision = "0004"


def upgrade():
    for model in (KnowledgeServer, RemoteQueryAudit):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (RemoteQueryAudit, KnowledgeServer):
        model.__table__.drop(op.get_bind(), checkfirst=True)
