"""Track recently active client sessions for host monitoring."""

from alembic import op
from sqlalchemy import Column, Integer, inspect

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    columns = {row["name"] for row in inspect(op.get_bind()).get_columns("client_sessions")}
    if "last_seen_at" not in columns:
        op.add_column("client_sessions", Column("last_seen_at", Integer, nullable=False, server_default="0"))


def downgrade():
    op.drop_column("client_sessions", "last_seen_at")
