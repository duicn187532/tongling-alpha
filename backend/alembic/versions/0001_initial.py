"""Personal runtime schema with immutable baseline enforcement."""

from alembic import op
from app.db import Base

revision = "0001"
down_revision = None


def upgrade():
    Base.metadata.create_all(op.get_bind())
    op.execute("""CREATE TRIGGER immutable_revision_update BEFORE UPDATE ON prd_revisions
    WHEN OLD.status IN ('BASELINED','SUPERSEDED') AND (
      NEW.content != OLD.content OR NEW.content_hash != OLD.content_hash OR
      NEW.docx_path != OLD.docx_path OR NEW.version != OLD.version OR
      NEW.project_id != OLD.project_id OR NEW.diff != OLD.diff OR NEW.changes != OLD.changes OR
      NEW.parent_revision_id IS NOT OLD.parent_revision_id OR
      NEW.baselined_at IS NOT OLD.baselined_at OR NEW.created_at != OLD.created_at OR
      NEW.status NOT IN ('BASELINED','SUPERSEDED') OR
      (OLD.status = 'SUPERSEDED' AND NEW.status != 'SUPERSEDED'))
    BEGIN SELECT RAISE(ABORT, 'Baseline is immutable'); END""")
    op.execute("""CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON prd_revisions
    WHEN OLD.status IN ('BASELINED','SUPERSEDED')
    BEGIN SELECT RAISE(ABORT, 'Baseline is immutable'); END""")


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS immutable_revision_update")
    op.execute("DROP TRIGGER IF EXISTS immutable_revision_delete")
    Base.metadata.drop_all(op.get_bind())
