"""Durable AI execution status and worker heartbeat."""

from alembic import op
from app.db import Activity

revision = "0003"
down_revision = "0002"


def upgrade():
    Activity.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    Activity.__table__.drop(op.get_bind(), checkfirst=True)
