"""Distinguish paired client sessions from revocable demo sessions."""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    columns = {row["name"] for row in inspect(op.get_bind()).get_columns("client_sessions")}
    if "kind" not in columns:
        op.add_column("client_sessions", Column("kind", String(16), nullable=False, server_default="PAIRED"))


def downgrade():
    op.drop_column("client_sessions", "kind")
