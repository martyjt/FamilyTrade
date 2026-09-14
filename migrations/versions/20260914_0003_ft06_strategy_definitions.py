"""FT-06 immutable strategy-definition records.

Revision ID: 20260914_0003
Revises: 20260913_0002
"""

from alembic import op

from familytrade.strategies.repository import strategy_metadata

revision = "20260914_0003"
down_revision = "20260913_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    strategy_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    op.execute("""
    CREATE FUNCTION strategy_version_immutability_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable strategy version'; END IF;
      IF OLD.status='draft' AND NEW.status='validated' AND OLD.record_version=1
         AND NEW.record_version=2 AND (to_jsonb(NEW)-'status'-'record_version'-'updated_at')=(to_jsonb(OLD)-'status'-'record_version'-'updated_at') THEN
        RETURN NEW;
      END IF;
      RAISE EXCEPTION 'invalid strategy version transition';
    END $$;
    CREATE TRIGGER strategy_versions_immutability BEFORE UPDATE OR DELETE ON strategy_versions
      FOR EACH ROW EXECUTE FUNCTION strategy_version_immutability_guard();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS strategy_versions_immutability ON strategy_versions")
    op.execute("DROP FUNCTION IF EXISTS strategy_version_immutability_guard()")
    strategy_metadata.drop_all(bind=op.get_bind(), checkfirst=False)
