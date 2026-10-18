"""Add review and immutable IT delivery records."""

from alembic import op
from sqlalchemy import inspect

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    from app.db import Base
    bind = op.get_bind()
    names = set(inspect(bind).get_table_names())
    for name in ("technical_reviews", "delivery_packages"):
        if name not in names:
            Base.metadata.tables[name].create(bind)


def downgrade():
    op.drop_table("delivery_packages")
    op.drop_table("technical_reviews")
