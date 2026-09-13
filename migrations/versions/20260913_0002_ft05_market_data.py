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

        CREATE FUNCTION market_data_contract_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable contract identity'; END IF;
          IF NEW.owner_user_id<>OLD.owner_user_id OR NEW.contract_id<>OLD.contract_id
             OR NEW.provider<>OLD.provider OR NEW.provider_contract_id<>OLD.provider_contract_id
             OR NEW.current_contract_version NOT IN (OLD.current_contract_version,OLD.current_contract_version+1)
             OR (OLD.series_binding_version IS NOT NULL
                 AND NEW.series_binding_version IS DISTINCT FROM OLD.series_binding_version)
             OR (OLD.series_binding_version IS NULL AND NEW.series_binding_version IS NOT NULL
                 AND NEW.series_binding_version<>NEW.current_contract_version)
             OR NOT EXISTS (
               SELECT 1 FROM market_data_contract_versions v
                WHERE v.owner_user_id=NEW.owner_user_id AND v.contract_id=NEW.contract_id
                  AND v.contract_version=NEW.current_contract_version
             ) THEN RAISE EXCEPTION 'invalid contract head transition'; END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_contract_head
          AFTER UPDATE OR DELETE ON market_data_contracts DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_contract_head_guard();

        CREATE FUNCTION market_data_series_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable series identity'; END IF;
          IF NEW.owner_user_id<>OLD.owner_user_id OR NEW.series_id<>OLD.series_id
             OR NEW.source<>OLD.source OR NEW.price_basis<>OLD.price_basis
             OR NEW.contract_id<>OLD.contract_id OR NEW.interval_seconds<>OLD.interval_seconds
             OR NEW.contract_version<>OLD.contract_version OR NEW.calendar_id<>OLD.calendar_id
             OR NEW.calendar_version<>OLD.calendar_version
             OR NEW.record_version<>OLD.record_version+1 THEN
            RAISE EXCEPTION 'invalid series transition';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER md_series_guard BEFORE UPDATE OR DELETE ON market_data_series
          FOR EACH ROW EXECUTE FUNCTION market_data_series_guard();

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
        DECLARE actual_bar record; revision_series text; object_row record;
        BEGIN
          SELECT * INTO actual_bar FROM market_data_bar_versions
           WHERE owner_user_id=NEW.owner_user_id AND bar_record_id=NEW.bar_record_id;
          SELECT series_id INTO revision_series FROM market_data_dataset_revisions
           WHERE owner_user_id=NEW.owner_user_id AND dataset_revision_id=NEW.dataset_revision_id;
          SELECT * INTO object_row FROM market_data_archive_objects
           WHERE owner_user_id=NEW.owner_user_id AND object_id=NEW.object_id;
          IF actual_bar.series_id IS NULL OR actual_bar.series_id<>NEW.series_id
             OR revision_series<>NEW.series_id OR actual_bar.start_at<>NEW.start_at
             OR actual_bar.source_revision<>NEW.source_revision
             OR object_row.object_id IS NULL
             OR NOT EXISTS (
               SELECT 1 FROM market_data_revision_partitions p
                WHERE p.owner_user_id=NEW.owner_user_id
                  AND p.dataset_revision_id=NEW.dataset_revision_id
                  AND p.object_id=NEW.object_id
             )
             OR NOT (object_row.min_start_at<=NEW.start_at AND NEW.start_at<object_row.max_end_at)
             OR NOT (object_row.min_source_revision<=NEW.source_revision
                     AND NEW.source_revision<=object_row.max_source_revision) THEN
            RAISE EXCEPTION 'revision bar metadata or object mismatch';
          END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_revision_bar_guard AFTER INSERT ON market_data_revision_bars
          DEFERRABLE INITIALLY DEFERRED
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
                  AND mod(extract(epoch from (NEW.start_at-w.start_at))::bigint,NEW.interval_seconds)=0
             ) THEN RAISE EXCEPTION 'active bar calendar or contract mismatch'; END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_active_bar_guard AFTER INSERT OR UPDATE ON market_data_active_bars
          DEFERRABLE INITIALLY DEFERRED
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
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_correction_ref_guard AFTER INSERT ON market_data_correction_refs
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_correction_ref_guard();

        CREATE FUNCTION market_data_revision_lifecycle_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable dataset revision'; END IF;
          IF OLD.status='building' AND NEW.status='published' AND NEW.record_version=2
             AND NEW.published_at=(NEW.projection->>'published_at')::timestamptz
             AND (to_jsonb(NEW)-ARRAY['status','published_at','record_version'])
                 =(to_jsonb(OLD)-ARRAY['status','published_at','record_version']) THEN RETURN NEW; END IF;
          IF OLD.status='building' AND NEW.status='quarantined' AND NEW.record_version=2
             AND (to_jsonb(NEW)-ARRAY['status','record_version'])
                 =(to_jsonb(OLD)-ARRAY['status','record_version']) THEN RETURN NEW; END IF;
          IF OLD.status='published' AND NEW.status='quarantined' AND NEW.record_version=3
             AND (to_jsonb(NEW)-ARRAY['status','record_version'])
                 =(to_jsonb(OLD)-ARRAY['status','record_version']) THEN RETURN NEW; END IF;
          IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'invalid dataset revision transition';
        END $$;
        CREATE TRIGGER md_revision_lifecycle BEFORE UPDATE OR DELETE ON market_data_dataset_revisions
          FOR EACH ROW EXECUTE FUNCTION market_data_revision_lifecycle_guard();

        CREATE FUNCTION market_data_publication_lifecycle_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable publication'; END IF;
          IF OLD.state='staged' AND NEW.state IN ('published','quarantined')
             AND (to_jsonb(NEW)-ARRAY['state','safe_reason','updated_at','fencing_token'])
                 =(to_jsonb(OLD)-ARRAY['state','safe_reason','updated_at','fencing_token']) THEN RETURN NEW; END IF;
          IF OLD.state='published' AND NEW.state='quarantined'
             AND NEW.safe_reason='ARCHIVE_INTEGRITY'
             AND (to_jsonb(NEW)-ARRAY['state','safe_reason','updated_at','fencing_token'])
                 =(to_jsonb(OLD)-ARRAY['state','safe_reason','updated_at','fencing_token']) THEN RETURN NEW; END IF;
          IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'invalid publication transition';
        END $$;
        CREATE TRIGGER md_publication_lifecycle BEFORE UPDATE OR DELETE ON market_data_publications
          FOR EACH ROW EXECUTE FUNCTION market_data_publication_lifecycle_guard();

        CREATE FUNCTION market_data_object_lifecycle_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable archive object'; END IF;
          IF OLD.state='staged' AND NEW.state IN ('published','quarantined')
             AND (to_jsonb(NEW)-'state')=(to_jsonb(OLD)-'state') THEN RETURN NEW; END IF;
          IF OLD.state='published' AND NEW.state='quarantined'
             AND (to_jsonb(NEW)-'state')=(to_jsonb(OLD)-'state') THEN RETURN NEW; END IF;
          IF NEW IS NOT DISTINCT FROM OLD THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'invalid archive object transition';
        END $$;
        CREATE TRIGGER md_object_lifecycle BEFORE UPDATE OR DELETE ON market_data_archive_objects
          FOR EACH ROW EXECUTE FUNCTION market_data_object_lifecycle_guard();

        CREATE FUNCTION market_data_calendar_complete_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE bad boolean;
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM market_data_calendar_windows
             WHERE owner_user_id=NEW.owner_user_id AND calendar_id=NEW.calendar_id
               AND calendar_version=NEW.calendar_version
          ) THEN RAISE EXCEPTION 'calendar must contain materialized windows'; END IF;
          SELECT EXISTS (
            WITH ordered AS (
              SELECT ordinal,start_at,end_at,kind,trading_day,reason,
                     lag(end_at) OVER (ORDER BY ordinal) previous_end,
                     lag(kind) OVER (ORDER BY ordinal) previous_kind,
                     lag(trading_day) OVER (ORDER BY ordinal) previous_day,
                     lag(reason) OVER (ORDER BY ordinal) previous_reason,
                     count(*) OVER () total,
                     max(ordinal) OVER () maximum,
                     first_value(start_at) OVER (ORDER BY ordinal) first_start,
                     first_value(end_at) OVER (ORDER BY ordinal DESC) last_end
                FROM market_data_calendar_windows
               WHERE owner_user_id=NEW.owner_user_id AND calendar_id=NEW.calendar_id
                 AND calendar_version=NEW.calendar_version
            ), calendar AS (
              SELECT coverage_start,coverage_end FROM market_data_calendar_versions
               WHERE owner_user_id=NEW.owner_user_id AND calendar_id=NEW.calendar_id
                 AND calendar_version=NEW.calendar_version
            )
            SELECT 1 FROM ordered,calendar
             WHERE total<>maximum+1 OR first_start<>coverage_start OR last_end<>coverage_end
                OR (ordinal>0 AND previous_end<>start_at)
                OR (ordinal>0 AND previous_kind=kind
                    AND previous_day IS NOT DISTINCT FROM trading_day
                    AND previous_reason IS NOT DISTINCT FROM reason)
          ) INTO bad;
          IF bad THEN RAISE EXCEPTION 'calendar windows must be contiguous, ordered, complete, and coalesced'; END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_calendar_complete
          AFTER INSERT ON market_data_calendar_windows DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_calendar_complete_guard();
        CREATE CONSTRAINT TRIGGER md_calendar_version_complete
          AFTER INSERT ON market_data_calendar_versions DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_calendar_complete_guard();

        CREATE FUNCTION market_data_bar_version_chain_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE predecessor record; expected_fingerprint text; fingerprint_payload text;
        BEGIN
          IF NEW.quality<>'valid' OR NEW.record_version<>1 OR NEW.schema_version<>'v1' THEN
            RAISE EXCEPTION 'invalid immutable bar registry envelope';
          END IF;
          IF NEW.source_revision=1 THEN
            IF NEW.supersedes_bar_record_id IS NOT NULL OR NEW.correction_reason IS NOT NULL THEN
              RAISE EXCEPTION 'revision one cannot supersede';
            END IF;
          ELSE
            SELECT * INTO predecessor FROM market_data_bar_versions
             WHERE owner_user_id=NEW.owner_user_id AND bar_record_id=NEW.supersedes_bar_record_id;
            IF predecessor.bar_record_id IS NULL OR predecessor.series_id<>NEW.series_id
               OR predecessor.start_at<>NEW.start_at
               OR predecessor.source_revision+1<>NEW.source_revision
               OR NEW.correction_reason NOT IN ('SOURCE_CORRECTION','REPAIR_REPLACEMENT','DERIVED_COMPONENT_CHANGE') THEN
              RAISE EXCEPTION 'invalid correction chain';
            END IF;
          END IF;
          fingerprint_payload := '{"aggregate_lineage_sha256":'
            || CASE WHEN NEW.aggregate_lineage_sha256 IS NULL THEN 'null'
                    ELSE '"'||trim(NEW.aggregate_lineage_sha256)||'"' END
            || ',"correction_reason":'
            || CASE WHEN NEW.correction_reason IS NULL THEN 'null'
                    ELSE '"'||NEW.correction_reason||'"' END
            || ',"payload_hash":"'||trim(NEW.payload_hash)||'"}';
          expected_fingerprint := encode(sha256(convert_to(fingerprint_payload,'UTF8')),'hex');
          IF trim(NEW.version_fingerprint_sha256)<>expected_fingerprint
             OR (NEW.aggregate_lineage_sha256 IS NULL AND EXISTS (
                   SELECT 1 FROM market_data_aggregate_components component
                    WHERE component.owner_user_id=NEW.owner_user_id
                      AND component.derived_bar_record_id=NEW.bar_record_id))
             OR (NEW.aggregate_lineage_sha256 IS NOT NULL AND NOT EXISTS (
                   SELECT 1 FROM market_data_aggregate_components component
                    WHERE component.owner_user_id=NEW.owner_user_id
                      AND component.derived_bar_record_id=NEW.bar_record_id)) THEN
            RAISE EXCEPTION 'bar lineage or version fingerprint is invalid';
          END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_bar_version_chain AFTER INSERT ON market_data_bar_versions
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_bar_version_chain_guard();

        CREATE FUNCTION market_data_partition_order_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE bad boolean;
        BEGIN
          SELECT EXISTS (
            SELECT 1 FROM (
              SELECT p.ordinal,o.min_start_at,o.max_end_at,
                     lag(o.max_end_at) OVER (ORDER BY p.ordinal) previous_end,
                     count(*) OVER () total,max(p.ordinal) OVER () maximum
                FROM market_data_revision_partitions p
                JOIN market_data_archive_objects o USING (owner_user_id,object_id)
               WHERE p.owner_user_id=NEW.owner_user_id
                 AND p.dataset_revision_id=NEW.dataset_revision_id
            ) ordered
             WHERE total<>maximum+1 OR (ordinal>0 AND previous_end>min_start_at)
          ) INTO bad;
          IF bad THEN RAISE EXCEPTION 'revision partitions are not contiguous and ordered'; END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_partition_order
          AFTER INSERT ON market_data_revision_partitions DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_partition_order_guard();

        CREATE FUNCTION market_data_snapshot_contiguous_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE total integer; maximum integer; expected integer;
        BEGIN
          SELECT count(*),coalesce(max(ordinal),-1) INTO total,maximum
            FROM market_data_read_snapshot_bars
           WHERE owner_user_id=NEW.owner_user_id AND read_snapshot_id=NEW.read_snapshot_id;
          SELECT total_rows INTO expected FROM market_data_read_snapshots
           WHERE owner_user_id=NEW.owner_user_id AND read_snapshot_id=NEW.read_snapshot_id;
          IF total<>maximum+1 OR total<>expected THEN RAISE EXCEPTION 'snapshot ordinals are incomplete'; END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_snapshot_contiguous
          AFTER INSERT ON market_data_read_snapshot_bars DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_snapshot_contiguous_guard();

        CREATE FUNCTION market_data_active_snapshot_delete_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM market_data_read_snapshot_bars b
            JOIN market_data_read_snapshots s USING (owner_user_id,read_snapshot_id)
             WHERE b.owner_user_id=OLD.owner_user_id AND b.bar_record_id=OLD.bar_record_id
               AND b.origin_kind='active' AND s.state='open' AND s.expires_at>clock_timestamp()
          ) THEN RAISE EXCEPTION 'active bar is protected by read snapshot'; END IF;
          RETURN OLD;
        END $$;
        CREATE TRIGGER md_active_snapshot_delete BEFORE DELETE ON market_data_active_bars
          FOR EACH ROW EXECUTE FUNCTION market_data_active_snapshot_delete_guard();

        CREATE FUNCTION market_data_contract_timezone_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.exchange_timezone IS DISTINCT FROM (
            SELECT exchange_timezone FROM market_data_calendar_versions
             WHERE owner_user_id=NEW.owner_user_id AND calendar_id=NEW.calendar_id
               AND calendar_version=NEW.calendar_version
          ) OR (NEW.first_trade_at IS NOT NULL AND NEW.first_trade_at>=NEW.last_trade_at) THEN
            RAISE EXCEPTION 'contract calendar projection is inconsistent';
          END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_contract_timezone AFTER INSERT ON market_data_contract_versions
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_contract_timezone_guard();

        CREATE FUNCTION market_data_revision_closure_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_row record; actual_revisions integer; actual_rows bigint; actual_bytes bigint;
        BEGIN
          IF NEW.parent_revision_id IS NULL THEN
            IF NEW.parent_depth<>0 THEN RAISE EXCEPTION 'root revision depth must be zero'; END IF;
          ELSE
            SELECT * INTO parent_row FROM market_data_dataset_revisions
             WHERE owner_user_id=NEW.owner_user_id
               AND dataset_revision_id=NEW.parent_revision_id;
            IF parent_row.dataset_revision_id IS NULL OR parent_row.status NOT IN ('building','published')
               OR NEW.parent_depth<>parent_row.parent_depth+1 THEN
              RAISE EXCEPTION 'revision parent is invalid';
            END IF;
          END IF;
          WITH RECURSIVE closure(dataset_revision_id) AS (
            SELECT NEW.dataset_revision_id
            UNION
            SELECT edge.dataset_revision_id
              FROM closure current_revision
              CROSS JOIN LATERAL (
                SELECT revision.parent_revision_id AS dataset_revision_id
                  FROM market_data_dataset_revisions revision
                 WHERE revision.owner_user_id=NEW.owner_user_id
                   AND revision.dataset_revision_id=current_revision.dataset_revision_id
                UNION
                SELECT component.source_dataset_revision_id
                  FROM market_data_revision_bars selected
                  JOIN market_data_aggregate_components component
                    ON component.owner_user_id=selected.owner_user_id
                   AND component.derived_bar_record_id=selected.bar_record_id
                 WHERE selected.owner_user_id=NEW.owner_user_id
                   AND selected.dataset_revision_id=current_revision.dataset_revision_id
              ) edge
             WHERE edge.dataset_revision_id IS NOT NULL
          ), closure_rows AS (
            SELECT dataset_revision_id FROM closure
          )
          SELECT count(*),
                 (SELECT count(*) FROM market_data_revision_bars selected
                   WHERE selected.owner_user_id=NEW.owner_user_id
                     AND selected.dataset_revision_id IN (SELECT dataset_revision_id FROM closure_rows)),
                 (SELECT coalesce(sum(object_row.byte_length),0) FROM (
                    SELECT DISTINCT object.object_id,object.byte_length
                      FROM market_data_revision_partitions partition
                      JOIN market_data_archive_objects object
                        ON object.owner_user_id=partition.owner_user_id
                       AND object.object_id=partition.object_id
                     WHERE partition.owner_user_id=NEW.owner_user_id
                       AND partition.dataset_revision_id IN
                           (SELECT dataset_revision_id FROM closure_rows)
                 ) object_row)
            INTO actual_revisions,actual_rows,actual_bytes
            FROM closure_rows;
          IF actual_revisions<>NEW.restore_closure_revision_count
             OR actual_rows<>NEW.restore_closure_row_count
             OR actual_bytes<>NEW.restore_closure_bytes THEN
            RAISE EXCEPTION 'revision closure summaries are not exact';
          END IF;
          RETURN NEW;
        END $$;
        CREATE CONSTRAINT TRIGGER md_revision_closure
          AFTER INSERT ON market_data_dataset_revisions DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_revision_closure_guard();

        CREATE FUNCTION market_data_aggregate_component_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE component_count integer; maximum integer; derived_row record;
                source_interval integer; source_series_count integer;
                source_revision_count integer; bad boolean; source_row record;
                lineage_payload text; expected_lineage text;
        BEGIN
          SELECT count(*),coalesce(max(ordinal),-1) INTO component_count,maximum
            FROM market_data_aggregate_components
           WHERE owner_user_id=NEW.owner_user_id
             AND derived_bar_record_id=NEW.derived_bar_record_id;
          SELECT b.*,b.start_at + make_interval(secs=>s.interval_seconds) AS end_at,
                 s.interval_seconds,s.source AS series_source,
                 s.price_basis AS series_price_basis,s.contract_id AS series_contract_id
            INTO derived_row
            FROM market_data_bar_versions b
            JOIN market_data_series s ON s.owner_user_id=b.owner_user_id AND s.series_id=b.series_id
           WHERE b.owner_user_id=NEW.owner_user_id AND b.bar_record_id=NEW.derived_bar_record_id;
          SELECT min(source_series.interval_seconds) AS source_interval,
                 count(DISTINCT source_bar.series_id) AS source_series_count,
                 count(DISTINCT component.source_dataset_revision_id) AS source_revision_count,
                 min(source_series.source) AS series_source,
                 min(source_series.price_basis) AS series_price_basis,
                 min(source_series.contract_id) AS series_contract_id,
                 min(component.source_dataset_revision_id) AS source_dataset_revision_id,
                 string_agg('"'||component.source_bar_record_id||'"',',' ORDER BY component.ordinal)
                   AS source_bar_record_ids
            INTO source_row
            FROM market_data_aggregate_components component
            JOIN market_data_bar_versions source_bar
              ON source_bar.owner_user_id=component.owner_user_id
             AND source_bar.bar_record_id=component.source_bar_record_id
            JOIN market_data_series source_series
              ON source_series.owner_user_id=source_bar.owner_user_id
             AND source_series.series_id=source_bar.series_id
           WHERE component.owner_user_id=NEW.owner_user_id
             AND component.derived_bar_record_id=NEW.derived_bar_record_id;
          source_interval := source_row.source_interval;
          source_series_count := source_row.source_series_count;
          source_revision_count := source_row.source_revision_count;
          SELECT EXISTS (
            SELECT 1 FROM market_data_aggregate_components component
            JOIN market_data_bar_versions source_bar
              ON source_bar.owner_user_id=component.owner_user_id
             AND source_bar.bar_record_id=component.source_bar_record_id
            JOIN market_data_revision_bars selected
              ON selected.owner_user_id=component.owner_user_id
             AND selected.dataset_revision_id=component.source_dataset_revision_id
             AND selected.bar_record_id=component.source_bar_record_id
            JOIN market_data_dataset_revisions source_revision
              ON source_revision.owner_user_id=selected.owner_user_id
             AND source_revision.dataset_revision_id=selected.dataset_revision_id
           WHERE component.owner_user_id=NEW.owner_user_id
             AND component.derived_bar_record_id=NEW.derived_bar_record_id
             AND (source_revision.status<>'published'
                  OR source_bar.start_at<>derived_row.start_at
                     + make_interval(secs=>source_interval*component.ordinal))
          ) INTO bad;
          lineage_payload := '{"end_at":"'
            || regexp_replace(to_char(derived_row.end_at AT TIME ZONE 'UTC',
                                      'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                              '\\.000000Z$','Z')
            || '","source_bar_record_ids":['||source_row.source_bar_record_ids||']'
            || ',"source_dataset_revision_id":"'||source_row.source_dataset_revision_id||'"'
            || ',"source_series_key":{"contract_id":"'||source_row.series_contract_id
            || '","interval_seconds":'||source_interval
            || ',"price_basis":"'||source_row.series_price_basis
            || '","source":"'||source_row.series_source||'"}'
            || ',"start_at":"'
            || regexp_replace(to_char(derived_row.start_at AT TIME ZONE 'UTC',
                                      'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                              '\\.000000Z$','Z')
            || '","target_series_key":{"contract_id":"'||derived_row.series_contract_id
            || '","interval_seconds":'||derived_row.interval_seconds
            || ',"price_basis":"'||derived_row.series_price_basis
            || '","source":"'||derived_row.series_source||'"}}';
          expected_lineage := encode(sha256(convert_to(lineage_payload,'UTF8')),'hex');
          IF component_count<>maximum+1 OR derived_row.bar_record_id IS NULL
             OR derived_row.aggregate_lineage_sha256 IS NULL
             OR trim(derived_row.aggregate_lineage_sha256)<>expected_lineage
             OR source_series_count<>1 OR source_revision_count<>1
             OR source_interval IS NULL OR source_interval>=derived_row.interval_seconds
             OR mod(derived_row.interval_seconds,source_interval)<>0
             OR component_count<>derived_row.interval_seconds/source_interval
             OR derived_row.end_at<>derived_row.start_at
                + make_interval(secs=>derived_row.interval_seconds)
             OR bad THEN
            RAISE EXCEPTION 'aggregate component lineage is invalid';
          END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_aggregate_component
          AFTER INSERT ON market_data_aggregate_components DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_aggregate_component_guard();

        CREATE FUNCTION market_data_conversion_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE publication_state text; revision_state text; has_active boolean; has_dataset boolean;
                selected_ordinal integer; first_ordinal integer;
        BEGIN
          SELECT state INTO publication_state FROM market_data_publications
           WHERE owner_user_id=NEW.owner_user_id AND publication_id=NEW.publication_id;
          SELECT status INTO revision_state FROM market_data_dataset_revisions
           WHERE owner_user_id=NEW.owner_user_id AND dataset_revision_id=NEW.dataset_revision_id;
          SELECT EXISTS (
            SELECT 1 FROM market_data_bar_retention_refs
             WHERE owner_user_id=NEW.owner_user_id AND bar_record_id=NEW.bar_record_id
               AND reference_kind=NEW.reference_kind AND reference_id=NEW.reference_id
          ) INTO has_active;
          SELECT EXISTS (
            SELECT 1 FROM market_data_retention_refs
             WHERE owner_user_id=NEW.owner_user_id
               AND dataset_revision_id=NEW.dataset_revision_id
               AND reference_kind=NEW.reference_kind AND reference_id=NEW.reference_id
          ) INTO has_dataset;
          SELECT link.ordinal INTO selected_ordinal
            FROM market_data_publication_revisions link
            JOIN market_data_revision_bars selected
              ON selected.owner_user_id=link.owner_user_id
             AND selected.dataset_revision_id=link.dataset_revision_id
             AND selected.bar_record_id=NEW.bar_record_id
           WHERE link.owner_user_id=NEW.owner_user_id
             AND link.publication_id=NEW.publication_id
             AND link.dataset_revision_id=NEW.dataset_revision_id;
          SELECT min(link.ordinal) INTO first_ordinal
            FROM market_data_publication_revisions link
            JOIN market_data_revision_bars selected
              ON selected.owner_user_id=link.owner_user_id
             AND selected.dataset_revision_id=link.dataset_revision_id
             AND selected.bar_record_id=NEW.bar_record_id
           WHERE link.owner_user_id=NEW.owner_user_id
             AND link.publication_id=NEW.publication_id;
          IF (NEW.state='planned' AND (NOT has_active OR has_dataset))
             OR (NEW.state='converted' AND (has_active OR NOT has_dataset))
             OR (NEW.state='cancelled' AND (publication_state<>'quarantined'
                 OR revision_state<>'quarantined'))
             OR selected_ordinal IS NULL OR selected_ordinal<>first_ordinal THEN
            RAISE EXCEPTION 'retention conversion state is inconsistent';
          END IF;
          IF TG_OP='UPDATE' AND OLD.state IN ('converted','cancelled') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'terminal retention conversion is immutable';
          END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_conversion_state
          AFTER INSERT OR UPDATE ON market_data_publication_retention_conversions
          DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
          EXECUTE FUNCTION market_data_conversion_guard();

        CREATE FUNCTION market_data_publication_fence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_token bigint;
        BEGIN
          SELECT fencing_token INTO current_token FROM market_data_series_fences
           WHERE owner_user_id=NEW.owner_user_id AND series_id=NEW.series_id;
          IF current_token IS NULL OR NEW.fencing_token<>current_token THEN
            RAISE EXCEPTION 'stale publication fence';
          END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_publication_fence
          AFTER INSERT OR UPDATE ON market_data_publications DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_publication_fence_guard();

        CREATE FUNCTION market_data_publication_revision_order_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE publication_row record; bad boolean; total integer; maximum integer; final_revision text;
        BEGIN
          SELECT * INTO publication_row FROM market_data_publications
           WHERE owner_user_id=NEW.owner_user_id AND publication_id=NEW.publication_id;
          SELECT count(*),coalesce(max(ordinal),-1),
                 (array_agg(dataset_revision_id ORDER BY ordinal DESC))[1]
            INTO total,maximum,final_revision
            FROM market_data_publication_revisions
           WHERE owner_user_id=NEW.owner_user_id AND publication_id=NEW.publication_id;
          SELECT EXISTS (
            SELECT 1 FROM (
              SELECT link.ordinal,revision.dataset_revision_id,revision.series_id,
                     revision.parent_revision_id,revision.rollover_from_revision_id,
                     revision.recovery_from_quarantined_revision_id,
                     lag(revision.dataset_revision_id) OVER (ORDER BY link.ordinal) previous_revision_id
                FROM market_data_publication_revisions link
                JOIN market_data_dataset_revisions revision USING(owner_user_id,dataset_revision_id)
               WHERE link.owner_user_id=NEW.owner_user_id
                 AND link.publication_id=NEW.publication_id
            ) ordered
             WHERE series_id<>publication_row.series_id
                OR (ordinal=0 AND publication_row.operation='publish'
                    AND ((rollover_from_revision_id IS NULL
                          AND parent_revision_id IS DISTINCT FROM publication_row.parent_revision_id)
                         OR (rollover_from_revision_id IS NOT NULL
                             AND (parent_revision_id IS NOT NULL
                                  OR rollover_from_revision_id IS DISTINCT FROM
                                     publication_row.parent_revision_id))))
                OR (ordinal=0 AND publication_row.operation='recover_quarantined_latest'
                    AND (parent_revision_id IS NOT NULL
                         OR rollover_from_revision_id IS NOT NULL
                         OR recovery_from_quarantined_revision_id IS DISTINCT FROM
                            publication_row.quarantined_source_revision_id))
                OR (ordinal>0 AND parent_revision_id IS DISTINCT FROM previous_revision_id)
          ) INTO bad;
          IF EXISTS (
            SELECT 1 FROM market_data_publication_cleanup_bars cleanup
            JOIN market_data_bar_retention_refs active_ref
              ON active_ref.owner_user_id=cleanup.owner_user_id
             AND active_ref.bar_record_id=cleanup.bar_record_id
           WHERE cleanup.owner_user_id=NEW.owner_user_id
             AND cleanup.publication_id=NEW.publication_id
             AND NOT EXISTS (
               SELECT 1 FROM market_data_publication_retention_conversions conversion
                WHERE conversion.owner_user_id=cleanup.owner_user_id
                  AND conversion.publication_id=cleanup.publication_id
                  AND conversion.bar_record_id=cleanup.bar_record_id
                  AND conversion.reference_kind=active_ref.reference_kind
                  AND conversion.reference_id=active_ref.reference_id
             )
          ) THEN
            RAISE EXCEPTION 'causal retention is not mapped to a publication frontier';
          END IF;
          IF total<>maximum+1 OR final_revision IS DISTINCT FROM publication_row.final_candidate_revision_id
             OR bad THEN RAISE EXCEPTION 'publication revision ordering is invalid'; END IF;
          RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER md_publication_revision_order
          AFTER INSERT ON market_data_publication_revisions DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION market_data_publication_revision_order_guard();
        """
    )


def downgrade() -> None:
    market_data_metadata.drop_all(bind=op.get_bind(), checkfirst=True)
    op.execute("DROP FUNCTION IF EXISTS market_data_object_lifecycle_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_series_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_contract_head_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_publication_lifecycle_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_publication_revision_order_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_publication_fence_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_conversion_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_aggregate_component_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_revision_closure_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_contract_timezone_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_active_snapshot_delete_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_snapshot_contiguous_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_partition_order_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_bar_version_chain_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_calendar_complete_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_revision_lifecycle_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_correction_ref_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_revision_bar_contiguous()")
    op.execute("DROP FUNCTION IF EXISTS market_data_revision_bar_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_active_bar_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_calendar_window_guard()")
    op.execute("DROP FUNCTION IF EXISTS market_data_reject_mutation()")
