"""FT-05 owner-scoped market-data catalog.

Revision ID: 20260913_0002
Revises: 20260913_0001
"""

from alembic import op

from familytrade.market_data.catalog import market_data_metadata

revision = "20260913_0002"
down_revision = "20260913_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    market_data_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    op.execute(
        """
        CREATE FUNCTION market_data_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'immutable market-data row';
        END $$;

        CREATE TRIGGER md_calendar_versions_immutable BEFORE UPDATE OR DELETE ON market_data_calendar_versions
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_calendar_windows_immutable BEFORE UPDATE OR DELETE ON market_data_calendar_windows
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_contract_versions_immutable BEFORE UPDATE OR DELETE ON market_data_contract_versions
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_bar_versions_immutable BEFORE UPDATE OR DELETE ON market_data_bar_versions
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_revision_bars_immutable BEFORE UPDATE OR DELETE ON market_data_revision_bars
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_revision_partitions_immutable BEFORE UPDATE OR DELETE ON market_data_revision_partitions
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_correction_refs_immutable BEFORE UPDATE OR DELETE ON market_data_correction_refs
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_aggregate_components_immutable BEFORE UPDATE OR DELETE ON market_data_aggregate_components
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();
        CREATE TRIGGER md_retention_refs_additive BEFORE UPDATE OR DELETE ON market_data_retention_refs
          FOR EACH ROW EXECUTE FUNCTION market_data_reject_mutation();

        CREATE FUNCTION market_data_calendar_window_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM market_data_calendar_windows w
             WHERE w.owner_user_id=NEW.owner_user_id AND w.calendar_id=NEW.calendar_id
               AND w.calendar_version=NEW.calendar_version AND w.ordinal<>NEW.ordinal
               AND tstzrange(w.start_at,w.end_at,'[)') && tstzrange(NEW.start_at,NEW.end_at,'[)')
          ) THEN RAISE EXCEPTION 'overlapping calendar windows'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER md_calendar_window_guard BEFORE INSERT ON market_data_calendar_windows
          FOR EACH ROW EXECUTE FUNCTION market_data_calendar_window_guard();

        CREATE FUNCTION market_data_revision_bar_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actual_series text;
        BEGIN
          SELECT series_id INTO actual_series FROM market_data_bar_versions
           WHERE owner_user_id=NEW.owner_user_id AND bar_record_id=NEW.bar_record_id;
          IF actual_series IS NULL OR actual_series<>NEW.series_id THEN
            RAISE EXCEPTION 'revision bar series mismatch';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER md_revision_bar_guard BEFORE INSERT ON market_data_revision_bars
          FOR EACH ROW EXECUTE FUNCTION market_data_revision_bar_guard();

        CREATE FUNCTION market_data_active_bar_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE series_row record; contract_row record;
        BEGIN
          SELECT * INTO series_row FROM market_data_series
           WHERE owner_user_id=NEW.owner_user_id AND series_id=NEW.series_id;
          SELECT * INTO contract_row FROM market_data_contract_versions
           WHERE owner_user_id=NEW.owner_user_id AND contract_id=NEW.contract_id
             AND contract_version=NEW.contract_version;
          IF series_row.series_id IS NULL OR series_row.contract_id<>NEW.contract_id
             OR series_row.contract_version<>NEW.contract_version
             OR contract_row.contract_id IS NULL
             OR (contract_row.first_trade_at IS NOT NULL AND NEW.start_at<contract_row.first_trade_at)
             OR NEW.end_at>contract_row.last_trade_at
             OR NOT EXISTS (
               SELECT 1 FROM market_data_calendar_windows w
                WHERE w.owner_user_id=NEW.owner_user_id
                  AND w.calendar_id=series_row.calendar_id
                  AND w.calendar_version=series_row.calendar_version
                  AND w.kind='open' AND w.start_at<=NEW.start_at AND NEW.end_at<=w.end_at
             ) THEN RAISE EXCEPTION 'active bar calendar or contract mismatch'; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER md_active_bar_guard BEFORE INSERT OR UPDATE ON market_data_active_bars
          FOR EACH ROW EXECUTE FUNCTION market_data_active_bar_guard();

        CREATE FUNCTION market_data_revision_bar_contiguous() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE total integer; maximum integer;
        BEGIN
          SELECT count(*), coalesce(max(ordinal),-1) INTO total,maximum
            FROM market_data_revision_bars
           WHERE owner_user_id=NEW.owner_user_id AND dataset_revision_id=NEW.dataset_revision_id;
          IF total<>maximum+1 THEN RAISE EXCEPTION 'revision bar ordinals are not contiguous'; END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_revision_bar_contiguous
          AFTER INSERT ON market_data_revision_bars DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_revision_bar_contiguous();

        CREATE FUNCTION market_data_correction_ref_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE old_row record; new_row record;
        BEGIN
          SELECT * INTO old_row FROM market_data_bar_versions
           WHERE owner_user_id=NEW.owner_user_id AND bar_record_id=NEW.old_bar_record_id;
          SELECT * INTO new_row FROM market_data_bar_versions
           WHERE owner_user_id=NEW.owner_user_id AND bar_record_id=NEW.new_bar_record_id;
          IF old_row.series_id IS NULL OR new_row.series_id<>old_row.series_id
             OR new_row.start_at<>old_row.start_at
             OR new_row.source_revision<>old_row.source_revision+1
             OR new_row.supersedes_bar_record_id<>old_row.bar_record_id
             OR new_row.correction_reason<>NEW.reason
             OR new_row.received_at<>NEW.received_at THEN
            RAISE EXCEPTION 'invalid correction reference';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER md_correction_ref_guard BEFORE INSERT ON market_data_correction_refs
          FOR EACH ROW EXECUTE FUNCTION market_data_correction_ref_guard();

        CREATE FUNCTION market_data_revision_lifecycle_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable dataset revision'; END IF;
          IF OLD.status='building' AND NEW.status='published' AND NEW.record_version=2 THEN RETURN NEW; END IF;
          IF OLD.status IN ('building','published') AND NEW.status='quarantined' AND NEW.record_version=3 THEN RETURN NEW; END IF;
          IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'invalid dataset revision transition';
        END $$;
        CREATE TRIGGER md_revision_lifecycle BEFORE UPDATE OR DELETE ON market_data_dataset_revisions
          FOR EACH ROW EXECUTE FUNCTION market_data_revision_lifecycle_guard();

        CREATE FUNCTION market_data_publication_lifecycle_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable publication'; END IF;
          IF OLD.state='staged' AND NEW.state IN ('published','quarantined') THEN RETURN NEW; END IF;
          IF OLD.state='published' AND NEW.state='quarantined' AND NEW.safe_reason='ARCHIVE_INTEGRITY' THEN RETURN NEW; END IF;
          IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'invalid publication transition';
        END $$;
        CREATE TRIGGER md_publication_lifecycle BEFORE UPDATE OR DELETE ON market_data_publications
          FOR EACH ROW EXECUTE FUNCTION market_data_publication_lifecycle_guard();
        """
    )


def downgrade() -> None:
    market_data_metadata.drop_all(bind=op.get_bind(), checkfirst=True)
    op.execute("DROP FUNCTION IF EXISTS market_data_publication_lifecycle_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_revision_lifecycle_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_correction_ref_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_revision_bar_contiguous()")
    op.execute("DROP FUNCTION IF EXISTS market_data_revision_bar_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_active_bar_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_calendar_window_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_reject_mutation()")
