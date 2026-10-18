"""Persist actionable meeting analysis feedback without changing existing records."""

from alembic import op
from sqlalchemy import inspect, Column, Text

revision = "0002"
down_revision = "0001"


def upgrade():
    if "analysis_error" not in {
        c["name"] for c in inspect(op.get_bind()).get_columns("meetings")
    }:
        op.add_column(
            "meetings",
            Column("analysis_error", Text(), nullable=False, server_default=""),
        )
    op.execute(
        "UPDATE meetings SET status='NO_FINDINGS', analysis_error='未抽取到決議或待辦，請補充逐字稿或人工整理紀錄。' WHERE status='ANALYZED' AND events='[]'"
    )


def downgrade():
    op.drop_column("meetings", "analysis_error")
