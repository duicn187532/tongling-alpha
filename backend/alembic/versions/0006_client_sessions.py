"""Persist paired client sessions across process restarts."""

from alembic import op
from app.db import ClientSession

revision = "0006"
down_revision = "0005"


def upgrade():
    ClientSession.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    ClientSession.__table__.drop(op.get_bind(), checkfirst=True)
