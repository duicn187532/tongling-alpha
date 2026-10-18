"""Durable demonstration jobs and cross-process resource leases."""

from alembic import op
from sqlalchemy import inspect

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    from app.db import Base
    bind = op.get_bind()
    names = set(inspect(bind).get_table_names())
    for name in ("app_jobs", "resource_leases"):
        if name not in names:
            Base.metadata.tables[name].create(bind)


def downgrade():
    op.drop_table("resource_leases")
    op.drop_table("app_jobs")
