"""Let users clear finished job and activity notices."""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    inspector = inspect(op.get_bind())
    for table in ("app_jobs", "ai_activities"):
        if "dismissed_at" not in {row["name"] for row in inspector.get_columns(table)}:
            op.add_column(table, Column("dismissed_at", String, nullable=True))


def downgrade():
    op.drop_column("ai_activities", "dismissed_at")
    op.drop_column("app_jobs", "dismissed_at")
