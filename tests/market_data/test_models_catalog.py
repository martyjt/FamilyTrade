from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid7

import pytest
from pydantic import ValidationError
from sqlalchemy import and_, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from familytrade.access.models import AccessError
from familytrade.market_data.archive import DatasetPublisher, MarketDataReader
from familytrade.market_data.catalog import (
    active_bars,
    aggregate_components,
    archive_objects,
    bar_conflicts,
    bar_versions,
    calendar_versions,
    contract_versions,
    contracts,
    dataset_revisions,
    idempotency,
    publications,
    quality_observations,
    revision_bars,
    revision_partitions,
    series,
    series_fences,
)
from familytrade.market_data.models import (
    AggregatedFullBar,
    AggregateGap,
    BarRetentionRef,
    CalendarCreateInput,
    CalendarVersion,
    CalendarVersionAppendInput,
    CalendarWindow,
    CalendarWindowInput,
    CompletedBarVersionInput,
    CoverageRequest,
    FuturesContractInput,
    LatestRead,
    MarketDataCode,
    MarketDataError,
    PublicationRequest,
    ReadBarsRequest,
    RecordBatchInput,
    RetainedManifestRestoreRequest,
    RetentionRef,
    SeriesKey,
    canonical_json_bytes,
    canonical_sha256,
)


def calendar_input(*, full_hour: bool = False) -> CalendarCreateInput:
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    open_end = start + (timedelta(hours=1) if full_hour else timedelta(minutes=1))
    return CalendarCreateInput(
        exchange_timezone="America/Chicago",
        coverage_start=start,
        coverage_end=start + timedelta(hours=2),
        windows=(
            CalendarWindowInput(
                kind="open",
                start_at=start,
                end_at=open_end,
                trading_day=date(2026, 11, 2),
                reason=None,
            ),
            CalendarWindowInput(
                kind="maintenance",
                start_at=open_end,
                end_at=start + timedelta(hours=2),
                trading_day=None,
                reason="DAILY_BREAK",
            ),
        ),
        metadata_as_of=start,
        provenance_ref="synthetic-fixture",
    )


def contract_input(calendar_id: str) -> FuturesContractInput:
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    return FuturesContractInput(
        provider="synthetic",
        provider_contract_id="MGCZ6",
        root_symbol="MGC",
        exchange="XCHI",
        currency="USD",
        tick_size=Decimal("0.10"),
        multiplier=Decimal(10),
        expiry_label="2026-12",
        last_trade_at=start + timedelta(days=60),
        calendar_id=calendar_id,
        calendar_version=1,
        entry_cutoff_at=start + timedelta(days=55),
        liquidation_start_at=start + timedelta(days=58),
        metadata_as_of=start,
        provenance_ref="synthetic",
    )


def bar_input(
    contract_id: str, revision: int = 1, supersedes: str | None = None, reason: str | None = None
) -> CompletedBarVersionInput:
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    return CompletedBarVersionInput(
        source="synthetic",
        price_basis="trades",
        contract_id=contract_id,
        interval_seconds=60,
        start_at=start,
        end_at=start + timedelta(minutes=1),
        open=Decimal("2000.0"),
        high=Decimal("2000.2"),
        low=Decimal("1999.9"),
        close=Decimal("2000.1") if revision == 1 else Decimal("2000.2"),
        volume=Decimal(12),
        source_revision=revision,
        completed_at=start + timedelta(minutes=1),
        quality="valid",
        supersedes_bar_record_id=supersedes,
        correction_reason=reason,
    )


def seed(catalog, context, *, full_hour: bool = False):
    calendar = catalog.create_calendar(
        context, calendar_input(full_hour=full_hour), idempotency_key=str(uuid7())
    )
    contract = catalog.register_contract(
        context, contract_input(calendar.calendar_id), idempotency_key=str(uuid7())
    )
    return calendar, contract


def test_canonical_json_recursively_sorts_object_keys_lexically_not_model_declaration_order() -> (
    None
):
    assert (
        canonical_json_bytes({"z": {"b": 1, "a": 2}, "a": Decimal("01.200")})
        == b'{"a":"1.2","z":{"a":2,"b":1}}'
    )
    assert canonical_sha256({"b": 1, "a": 2}) == canonical_sha256({"a": 2, "b": 1})


def test_completed_bar_futures_contract_dataset_revision_and_policy_fields_serialize_exactly() -> (
    None
):
    with pytest.raises(ValidationError):
        SeriesKey(
            source="x",
            price_basis="trades",
            contract_id=str(uuid7()),
            interval_seconds=60,
            owner_user_id=str(uuid7()),
        )


def test_calendar_retention_bar_retention_and_aggregate_gap_records_serialize_exactly() -> None:
    now = datetime(2026, 11, 1, 23, tzinfo=UTC)
    owner = str(uuid7())
    records = (
        CalendarVersion(
            calendar_id=str(uuid7()),
            owner_user_id=owner,
            calendar_version=1,
            exchange_timezone="America/Chicago",
            coverage_start=now,
            coverage_end=now + timedelta(hours=1),
            windows=(
                CalendarWindow(
                    ordinal=0,
                    kind="open",
                    start_at=now,
                    end_at=now + timedelta(hours=1),
                    trading_day=date(2026, 11, 2),
                    reason=None,
                ),
            ),
            metadata_as_of=now,
            provenance_ref="synthetic",
            created_at=now,
            record_version=1,
        ),
        RetentionRef(
            owner_user_id=owner,
            dataset_revision_id=str(uuid7()),
            reference_kind="lane",
            reference_id=str(uuid7()),
            created_at=now,
            replayed=False,
        ),
        BarRetentionRef(
            owner_user_id=owner,
            bar_record_id=str(uuid7()),
            reference_kind="lane",
            reference_id=str(uuid7()),
            state="active",
            dataset_revision_id=None,
            created_at=now,
            replayed=False,
        ),
        AggregateGap(
            start_at=now,
            end_at=now + timedelta(minutes=5),
            reason="MISSING_COMPONENT",
            expected_component_starts=(now, now + timedelta(minutes=1)),
        ),
    )
    for record in records:
        assert json.loads(canonical_json_bytes(record)) == record.model_dump(mode="json")


def test_calendar_create_generates_id_and_version_one_then_append_requires_current_version(
    catalog, contexts
) -> None:
    calendar, _ = seed(catalog, contexts[0])
    assert calendar.calendar_version == calendar.record_version == 1
    assert calendar.owner_user_id == contexts[0].user_id


def test_batch_same_key_same_bytes_replays_original_ids(catalog, contexts) -> None:
    _, contract = seed(catalog, contexts[0])
    value = RecordBatchInput(bars=(bar_input(contract.contract_id),))
    key = str(uuid7())
    first = catalog.record_completed_batch(contexts[0], value, idempotency_key=key)
    second = catalog.record_completed_batch(contexts[0], value, idempotency_key=key)
    assert first == second and len(first.inserted_bar_record_ids) == 1


def test_batch_same_key_different_bytes_is_idempotency_conflict(catalog, contexts) -> None:
    _, contract = seed(catalog, contexts[0])
    key = str(uuid7())
    catalog.record_completed_batch(
        contexts[0], RecordBatchInput(bars=(bar_input(contract.contract_id),)), idempotency_key=key
    )
    changed = bar_input(contract.contract_id).model_copy(update={"volume": Decimal(13)})
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            contexts[0], RecordBatchInput(bars=(changed,)), idempotency_key=key
        )
    assert caught.value.code is MarketDataCode.IDEMPOTENCY_CONFLICT


def test_correction_skip_fork_and_wrong_supersedes_are_rejected(catalog, contexts) -> None:
    _, contract = seed(catalog, contexts[0])
    first = catalog.record_completed_batch(
        contexts[0],
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    bad = bar_input(contract.contract_id, 2, str(uuid7()), "SOURCE_CORRECTION")
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            contexts[0], RecordBatchInput(bars=(bad,)), idempotency_key=str(uuid7())
        )
    assert caught.value.code is MarketDataCode.DUPLICATE_CONFLICT and first.inserted_bar_record_ids


def test_direct_sql_constraints_reject_invalid_ohlc_tick_volume_duration_and_owner_fk(
    catalog, contexts
) -> None:
    _, contract = seed(catalog, contexts[0])
    recorded = catalog.record_completed_batch(
        contexts[0],
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    bar_record_id = recorded.inserted_bar_record_ids[0]
    invalid_updates = (
        {"high": Decimal("1999.8")},
        {"close": Decimal("2000.15")},
        {"volume": Decimal("1.5")},
        {"end_at": bar_input(contract.contract_id).end_at + timedelta(minutes=1)},
        {"owner_user_id": str(uuid7())},
    )
    for values in invalid_updates:
        with pytest.raises(DBAPIError), catalog.engine.begin() as connection:
            connection.execute(
                update(active_bars)
                .where(active_bars.c.bar_record_id == bar_record_id)
                .values(**values)
            )
    with catalog.engine.begin() as connection:
        persisted = (
            connection.execute(
                select(active_bars).where(active_bars.c.bar_record_id == bar_record_id)
            )
            .mappings()
            .one()
        )
    assert persisted["owner_user_id"] == contexts[0].user_id
    assert persisted["close"] == Decimal("2000.1")
    assert persisted["volume"] == Decimal(12)
    empty_calendar_id = str(uuid7())
    now = datetime.now(UTC)
    with pytest.raises(DBAPIError), catalog.engine.begin() as connection:
        connection.execute(
            insert(calendar_versions).values(
                owner_user_id=contexts[0].user_id,
                calendar_id=empty_calendar_id,
                calendar_version=1,
                schema_version="v1",
                exchange_timezone="UTC",
                coverage_start=now,
                coverage_end=now + timedelta(minutes=1),
                metadata_as_of=now,
                provenance_ref="direct-sql-empty-calendar",
                payload_sha256="a" * 64,
                created_at=now,
                record_version=1,
            )
        )
    with catalog.engine.begin() as connection:
        registry = dict(
            connection.execute(
                select(bar_versions).where(bar_versions.c.bar_record_id == bar_record_id)
            )
            .mappings()
            .one()
        )
    registry.update(
        bar_record_id=str(uuid7()),
        source_revision=2,
        supersedes_bar_record_id=bar_record_id,
        correction_reason="SOURCE_CORRECTION",
        version_fingerprint_sha256="f" * 64,
        received_at=registry["received_at"] + timedelta(microseconds=1),
        created_at=registry["received_at"] + timedelta(microseconds=1),
    )
    with pytest.raises(DBAPIError), catalog.engine.begin() as connection:
        connection.execute(insert(bar_versions).values(**registry))


def test_material_contract_update_after_series_exists_is_conflict(catalog, contexts) -> None:
    _, contract = seed(catalog, contexts[0])
    catalog.record_completed_batch(
        contexts[0],
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    changed = contract_input(contract.calendar_id).model_copy(
        update={
            "contract_id": contract.contract_id,
            "expected_version": 1,
            "tick_size": Decimal("0.20"),
        }
    )
    with pytest.raises(MarketDataError) as caught:
        catalog.register_contract(contexts[0], changed, idempotency_key=str(uuid7()))
    assert caught.value.code is MarketDataCode.CONFLICT


def test_record_aggregated_batch_assigns_revision_from_component_lineage_and_replays(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    first = bar_input(contract.contract_id)
    source_bars = tuple(
        first.model_copy(
            update={
                "start_at": first.start_at + timedelta(minutes=index),
                "end_at": first.end_at + timedelta(minutes=index),
                "completed_at": first.completed_at + timedelta(minutes=index),
                "open": Decimal(2000 + index),
                "high": Decimal("2000.2") + index,
                "low": Decimal("1999.9") + index,
                "close": Decimal("2000.1") + index,
            }
        )
        for index in range(60)
    )
    recorded = catalog.record_completed_batch(
        context, RecordBatchInput(bars=source_bars), idempotency_key=str(uuid7())
    )
    source_key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=source_key,
            coverage_start=first.start_at,
            coverage_end=first.start_at + timedelta(hours=1),
        ),
        idempotency_key=str(uuid7()),
    )
    target_key = source_key.model_copy(update={"interval_seconds": 300})
    aggregate = AggregatedFullBar(
        target_interval_seconds=300,
        start_at=first.start_at,
        end_at=first.start_at + timedelta(minutes=5),
        open=source_bars[0].open,
        high=max(bar.high for bar in source_bars),
        low=min(bar.low for bar in source_bars),
        close=source_bars[-1].close,
        volume=sum((bar.volume for bar in source_bars), start=Decimal(0)),
        source_bar_record_ids=recorded.inserted_bar_record_ids[:5],
    )
    key = str(uuid7())
    result = catalog.record_aggregated_batch(
        context,
        published.dataset_revision.dataset_revision_id,
        source_key,
        target_key,
        (aggregate,),
        idempotency_key=key,
    )
    replay = catalog.record_aggregated_batch(
        context,
        published.dataset_revision.dataset_revision_id,
        source_key,
        target_key,
        (aggregate,),
        idempotency_key=key,
    )
    assert replay == result
    assert len(result.inserted_bar_record_ids) == 1


def test_staging_forces_constraints_immediate_and_rejects_missing_or_cross_owner_candidate(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    now = catalog._now()
    with pytest.raises(IntegrityError), catalog.engine.begin() as c:
        series_id = c.execute(
            select(series.c.series_id).where(
                and_(
                    series.c.owner_user_id == context.user_id,
                    series.c.contract_id == contract.contract_id,
                )
            )
        ).scalar_one()
        c.execute(
            insert(publications).values(
                owner_user_id=context.user_id,
                publication_id=str(uuid7()),
                series_id=series_id,
                idempotency_key=str(uuid7()),
                operation="publish",
                parent_revision_id=None,
                final_candidate_revision_id=str(uuid7()),
                snapshot_sha256="a" * 64,
                fencing_token=0,
                state="staged",
                created_at=now,
                updated_at=now,
            )
        )
        c.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_staging_deferred_final_candidate_fk_resolves_after_revision_insert(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    candidate_id, publication_id = str(uuid7()), str(uuid7())
    with catalog.engine.begin() as connection:
        original = (
            connection.execute(
                select(dataset_revisions).where(
                    dataset_revisions.c.dataset_revision_id
                    == published.dataset_revision.dataset_revision_id
                )
            )
            .mappings()
            .one()
        )
        current_fence = connection.execute(
            select(series_fences.c.fencing_token).where(
                and_(
                    series_fences.c.owner_user_id == context.user_id,
                    series_fences.c.series_id == original["series_id"],
                )
            )
        ).scalar_one()
        connection.execute(
            insert(publications).values(
                owner_user_id=context.user_id,
                publication_id=publication_id,
                series_id=original["series_id"],
                idempotency_key=str(uuid7()),
                operation="publish",
                parent_revision_id=None,
                final_candidate_revision_id=candidate_id,
                snapshot_sha256="a" * 64,
                fencing_token=current_fence,
                state="staged",
                created_at=catalog._now(),
                updated_at=catalog._now(),
            )
        )
        projection = dict(original["projection"])
        projection.update(
            dataset_revision_id=candidate_id,
            manifest_uri=f"ft-archive://manifest/{candidate_id}",
        )
        values = dict(original)
        values.update(
            dataset_revision_id=candidate_id,
            projection=projection,
            parent_revision_id=None,
            manifest_uri=f"ft-archive://manifest/{candidate_id}",
            manifest_sha256="b" * 64,
            manifest_byte_length=1,
            parent_depth=0,
            restore_closure_revision_count=1,
            restore_closure_row_count=0,
            restore_closure_bytes=0,
            status="building",
            published_at=None,
            record_version=1,
        )
        connection.execute(insert(dataset_revisions).values(**values))
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(publications.c.final_candidate_revision_id).where(
                    publications.c.publication_id == publication_id
                )
            ).scalar_one()
            == candidate_id
        )


def test_archive_object_catalog_origin_constraint_requires_publication_fk_or_retained_manifest_shape(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    with pytest.raises(IntegrityError), catalog.engine.begin() as c:
        series_id = c.execute(
            select(series.c.series_id).where(
                and_(
                    series.c.owner_user_id == context.user_id,
                    series.c.contract_id == contract.contract_id,
                )
            )
        ).scalar_one()
        object_id = str(uuid7())
        c.execute(
            insert(archive_objects).values(
                owner_user_id=context.user_id,
                object_id=object_id,
                series_id=series_id,
                uri=f"ft-archive://object/{object_id}",
                sha256="a" * 64,
                byte_length=1,
                row_count=1,
                min_start_at=bar_input(contract.contract_id).start_at,
                max_end_at=bar_input(contract.contract_id).end_at,
                min_source_revision=1,
                max_source_revision=1,
                state="staged",
                origin_publication_id=str(uuid7()),
                publication_id=None,
                catalog_origin="publication",
            )
        )


def test_published_catalog_rows_reject_direct_mutation(catalog, contexts, archive_store) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    start = bar_input(contract.contract_id).start_at
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    with pytest.raises(DBAPIError), catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                dataset_revisions.c.dataset_revision_id
                == published.dataset_revision.dataset_revision_id
            )
            .values(parent_depth=10)
        )


def test_same_bar_revision_different_hash_aborts_whole_batch_and_audits_safe_hashes(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    original = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(original,)),
        idempotency_key=str(uuid7()),
    )
    conflicting = original.model_copy(update={"close": Decimal("2000.0")})
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(conflicting,)),
            idempotency_key=str(uuid7()),
        )
    assert caught.value.code == MarketDataCode.DUPLICATE_CONFLICT
    with catalog.engine.begin() as c:
        audit = c.execute(select(bar_conflicts)).mappings().one()
        assert audit["existing_fingerprint"] != audit["attempted_fingerprint"]
        assert c.execute(select(func.count()).select_from(bar_versions)).scalar_one() == 1


def test_invalid_observation_consumes_no_revision_and_later_valid_same_revision_inserts(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    valid = bar_input(contract.contract_id)
    primer = valid.model_copy(
        update={
            "start_at": valid.start_at + timedelta(minutes=1),
            "end_at": valid.end_at + timedelta(minutes=1),
            "completed_at": valid.completed_at + timedelta(minutes=1),
        }
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(primer,)), idempotency_key=str(uuid7())
    )
    invalid = valid.model_copy(update={"low": Decimal("2001.0")})
    invalid_key = str(uuid7())
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(invalid,)),
            idempotency_key=invalid_key,
        )
    assert caught.value.code == MarketDataCode.VALIDATION_ERROR
    with pytest.raises(MarketDataError) as replayed:
        catalog.record_completed_batch(
            context, RecordBatchInput(bars=(invalid,)), idempotency_key=invalid_key
        )
    assert replayed.value.code == caught.value.code
    with catalog.engine.begin() as c:
        observation = c.execute(select(quality_observations)).mappings().one()
        assert observation["reason"] == "OHLC"
        assert c.execute(select(func.count()).select_from(bar_versions)).scalar_one() == 1
        failed_root = (
            c.execute(select(idempotency).where(idempotency.c.idempotency_key == invalid_key))
            .mappings()
            .one()
        )
        assert failed_root["state"] == "failed"
    inserted = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(valid,)),
        idempotency_key=str(uuid7()),
    )
    assert len(inserted.inserted_bar_record_ids) == 1


def test_calendar_append_explicit_id_wrong_owner_is_not_found_and_stale_version_conflicts(
    catalog, contexts
) -> None:
    calendar, _ = seed(catalog, contexts[0])
    append = CalendarVersionAppendInput(
        **calendar_input().model_dump(), expected_previous_version=1
    )
    with pytest.raises(AccessError) as hidden:
        catalog.append_calendar_version(
            contexts[1], calendar.calendar_id, append, idempotency_key=str(uuid7())
        )
    assert hidden.value.http_status == 404
    appended = catalog.append_calendar_version(
        contexts[0], calendar.calendar_id, append, idempotency_key=str(uuid7())
    )
    assert appended.calendar_version == appended.record_version == 2
    with pytest.raises(MarketDataError) as stale:
        catalog.append_calendar_version(
            contexts[0], calendar.calendar_id, append, idempotency_key=str(uuid7())
        )
    assert stale.value.code is MarketDataCode.STALE_VERSION


def test_provenance_only_contract_update_appends_version_while_existing_series_manifest_remains_bound(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    updated_input = contract_input(contract.calendar_id).model_copy(
        update={
            "contract_id": contract.contract_id,
            "expected_version": 1,
            "metadata_as_of": contract.metadata_as_of + timedelta(minutes=1),
            "provenance_ref": "synthetic-corrected-metadata",
        }
    )
    updated = catalog.register_contract(context, updated_input, idempotency_key=str(uuid7()))
    assert updated.record_version == 2
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    assert published.dataset_revision.contract_version == 1
    with catalog.engine.begin() as c:
        assert (
            c.execute(
                select(func.count())
                .select_from(contract_versions)
                .where(
                    and_(
                        contract_versions.c.owner_user_id == context.user_id,
                        contract_versions.c.contract_id == contract.contract_id,
                    )
                )
            ).scalar_one()
            == 2
        )


def test_new_interval_after_provenance_update_reuses_contract_series_binding_version(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    updated = contract_input(contract.calendar_id).model_copy(
        update={
            "contract_id": contract.contract_id,
            "expected_version": 1,
            "metadata_as_of": contract.metadata_as_of + timedelta(minutes=1),
            "provenance_ref": "later-provenance",
        }
    )
    catalog.register_contract(context, updated, idempotency_key=str(uuid7()))
    five_minute = bar_input(contract.contract_id).model_copy(
        update={
            "interval_seconds": 300,
            "end_at": bar_input(contract.contract_id).start_at + timedelta(minutes=5),
        }
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(five_minute,)), idempotency_key=str(uuid7())
    )
    with catalog.engine.begin() as c:
        bindings = c.execute(
            select(series.c.interval_seconds, series.c.contract_version)
            .where(series.c.owner_user_id == context.user_id)
            .order_by(series.c.interval_seconds)
        ).all()
        assert bindings == [(60, 1), (300, 1)]
        assert (
            c.execute(
                select(contracts.c.series_binding_version).where(
                    and_(
                        contracts.c.owner_user_id == context.user_id,
                        contracts.c.contract_id == contract.contract_id,
                    )
                )
            ).scalar_one()
            == 1
        )


def test_bar_start_must_align_to_calendar_open_segment_interval(catalog, contexts) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    misaligned = bar.model_copy(
        update={
            "start_at": bar.start_at + timedelta(seconds=30),
            "end_at": bar.end_at + timedelta(seconds=30),
            "completed_at": bar.completed_at + timedelta(seconds=30),
        }
    )
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            context, RecordBatchInput(bars=(misaligned,)), idempotency_key=str(uuid7())
        )
    assert caught.value.code is MarketDataCode.VALIDATION_ERROR


def test_bar_before_first_trade_or_ending_after_last_trade_is_rejected(catalog, contexts) -> None:
    context = contexts[0]
    calendar = catalog.create_calendar(context, calendar_input(), idempotency_key=str(uuid7()))
    start = calendar.coverage_start
    restricted = contract_input(calendar.calendar_id).model_copy(
        update={
            "first_trade_at": start + timedelta(seconds=30),
            "last_trade_at": start + timedelta(seconds=45),
            "entry_cutoff_at": start + timedelta(seconds=35),
            "liquidation_start_at": start + timedelta(seconds=40),
        }
    )
    contract = catalog.register_contract(context, restricted, idempotency_key=str(uuid7()))
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(bar_input(contract.contract_id),)),
            idempotency_key=str(uuid7()),
        )
    assert caught.value.code is MarketDataCode.VALIDATION_ERROR


def test_caller_supplied_nonvalid_quality_is_rejected_without_bar_or_observation(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    payload = bar_input(contract.contract_id).model_dump()
    payload["quality"] = "invalid"
    with pytest.raises(ValidationError):
        CompletedBarVersionInput.model_validate(payload)
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(bar_versions)).scalar_one() == 0
        assert c.execute(select(func.count()).select_from(quality_observations)).scalar_one() == 0


def test_same_bar_revision_same_hash_collapses_across_batches(catalog, contexts) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    value = RecordBatchInput(bars=(bar_input(contract.contract_id),))
    first = catalog.record_completed_batch(context, value, idempotency_key=str(uuid7()))
    second = catalog.record_completed_batch(context, value, idempotency_key=str(uuid7()))
    assert second.inserted_bar_record_ids == ()
    assert second.replayed_bar_record_ids == first.inserted_bar_record_ids


def test_same_revision_same_ohlcv_different_correction_reason_is_duplicate_conflict(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    corrected = bar_input(
        contract.contract_id,
        revision=2,
        supersedes=first.inserted_bar_record_ids[0],
        reason="SOURCE_CORRECTION",
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(corrected,)), idempotency_key=str(uuid7())
    )
    changed_reason = corrected.model_copy(update={"correction_reason": "REPAIR_REPLACEMENT"})
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(changed_reason,)),
            idempotency_key=str(uuid7()),
        )
    assert caught.value.code is MarketDataCode.DUPLICATE_CONFLICT


def test_correction_reason_is_required_bounded_persisted_and_derived_for_aggregate(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    missing_reason = bar_input(
        contract.contract_id, revision=2, supersedes=first.inserted_bar_record_ids[0]
    )
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(missing_reason,)),
            idempotency_key=str(uuid7()),
        )
    assert caught.value.code is MarketDataCode.VALIDATION_ERROR


def test_contract_exchange_timezone_is_derived_persisted_and_constrained_to_calendar_version(
    catalog, contexts
) -> None:
    calendar, contract = seed(catalog, contexts[0])
    assert contract.exchange_timezone == calendar.exchange_timezone == "America/Chicago"
    with catalog.engine.begin() as c:
        stored = c.execute(
            select(contract_versions.c.exchange_timezone).where(
                and_(
                    contract_versions.c.owner_user_id == contexts[0].user_id,
                    contract_versions.c.contract_id == contract.contract_id,
                    contract_versions.c.contract_version == 1,
                )
            )
        ).scalar_one()
    assert stored == calendar.exchange_timezone


def test_active_tick_fk_targets_immutable_series_contract_version() -> None:
    targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in active_bars.foreign_key_constraints
    }
    assert (
        "market_data_contract_versions.owner_user_id",
        "market_data_contract_versions.contract_id",
        "market_data_contract_versions.contract_version",
        "market_data_contract_versions.tick_size",
    ) in targets


def test_series_calendar_binding_survives_later_standalone_calendar_version_without_relabeling_bars(
    catalog, contexts
) -> None:
    context = contexts[0]
    calendar, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    catalog.append_calendar_version(
        context,
        calendar.calendar_id,
        CalendarVersionAppendInput(**calendar_input().model_dump(), expected_previous_version=1),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        row = c.execute(
            select(series.c.calendar_version, active_bars.c.start_at)
            .join(
                active_bars,
                and_(
                    active_bars.c.owner_user_id == series.c.owner_user_id,
                    active_bars.c.series_id == series.c.series_id,
                ),
            )
            .where(series.c.owner_user_id == context.user_id)
        ).one()
    assert row.calendar_version == 1
    assert row.start_at == bar_input(contract.contract_id).start_at


def test_cross_metadata_version_same_logical_bar_cannot_restart_at_revision_one(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    original = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(original,)), idempotency_key=str(uuid7())
    )
    updated = contract_input(contract.calendar_id).model_copy(
        update={
            "contract_id": contract.contract_id,
            "expected_version": 1,
            "metadata_as_of": contract.metadata_as_of + timedelta(minutes=1),
            "provenance_ref": "new-source-snapshot",
        }
    )
    catalog.register_contract(context, updated, idempotency_key=str(uuid7()))
    conflicting_r1 = original.model_copy(update={"close": Decimal("2000.0")})
    with pytest.raises(MarketDataError) as caught:
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(conflicting_r1,)),
            idempotency_key=str(uuid7()),
        )
    assert caught.value.code is MarketDataCode.DUPLICATE_CONFLICT


def test_core_persisted_entities_require_v1_primary_id_owner_created_at_and_record_version(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    calendar, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    selected = (
        MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
        .read_bars(
            context,
            ReadBarsRequest(
                series_key=key,
                coverage_start=bar_input(contract.contract_id).start_at,
                coverage_end=bar_input(contract.contract_id).end_at,
                policy=LatestRead(),
            ),
        )
        .selections[0]
        .bar
    )
    for entity, identifier in (
        (calendar, calendar.calendar_id),
        (contract, contract.contract_id),
        (selected, selected.bar_record_id),
        (
            published.dataset_revision,
            published.dataset_revision.dataset_revision_id,
        ),
    ):
        assert entity.schema_version == "v1"
        assert entity.owner_user_id == context.user_id
        assert entity.created_at.utcoffset() == timedelta(0)
        assert entity.record_version >= 1
        assert UUID(identifier).version == 7 and str(UUID(identifier)) == identifier


def test_published_object_and_revision_catalog_contain_one_greatest_version_per_logical_key(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    initial = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    corrected = bar_input(
        contract.contract_id,
        revision=2,
        supersedes=initial.inserted_bar_record_ids[0],
        reason="SOURCE_CORRECTION",
    )
    correction = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(corrected,)), idempotency_key=str(uuid7())
    )
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=corrected.start_at,
            coverage_end=corrected.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        selected = (
            c.execute(
                select(revision_bars.c.bar_record_id).where(
                    and_(
                        revision_bars.c.owner_user_id == context.user_id,
                        revision_bars.c.dataset_revision_id
                        == published.dataset_revision.dataset_revision_id,
                        revision_bars.c.start_at == corrected.start_at,
                    )
                )
            )
            .scalars()
            .all()
        )
        objects = (
            c.execute(
                select(archive_objects.c.object_id)
                .join(
                    revision_partitions,
                    and_(
                        revision_partitions.c.owner_user_id == archive_objects.c.owner_user_id,
                        revision_partitions.c.object_id == archive_objects.c.object_id,
                    ),
                )
                .where(
                    and_(
                        archive_objects.c.owner_user_id == context.user_id,
                        archive_objects.c.state == "published",
                        revision_partitions.c.dataset_revision_id
                        == published.dataset_revision.dataset_revision_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert selected == [correction.inserted_bar_record_ids[0]]
    assert objects == [published.dataset_revision.partition_refs[0].object_id]


def test_source_watermark_is_deterministic_internal_receipt_frontier_not_provider_claim(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    result = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        receipt = c.execute(
            select(bar_versions.c.received_at).where(
                bar_versions.c.bar_record_id == result.inserted_bar_record_ids[0]
            )
        ).scalar_one()
    assert published.dataset_revision.source_watermark.kind == "familytrade_received_v1"
    assert published.dataset_revision.source_watermark.max_received_at == receipt
    assert (
        published.dataset_revision.source_watermark.max_bar_record_id
        == result.inserted_bar_record_ids[0]
    )


def _published_source_hour(catalog, context, archive_store):
    _, contract = seed(catalog, context, full_hour=True)
    template = bar_input(contract.contract_id)
    source_bars = tuple(
        template.model_copy(
            update={
                "start_at": template.start_at + timedelta(minutes=index),
                "end_at": template.end_at + timedelta(minutes=index),
                "completed_at": template.completed_at + timedelta(minutes=index),
                "open": Decimal(2000 + index),
                "high": Decimal("2000.2") + index,
                "low": Decimal("1999.9") + index,
                "close": Decimal("2000.1") + index,
            }
        )
        for index in range(60)
    )
    recorded = catalog.record_completed_batch(
        context, RecordBatchInput(bars=source_bars), idempotency_key=str(uuid7())
    )
    source_key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    source_revision = publisher.publish(
        context,
        PublicationRequest(
            series_key=source_key,
            coverage_start=template.start_at,
            coverage_end=template.start_at + timedelta(hours=1),
        ),
        idempotency_key=str(uuid7()),
    )
    target_key = source_key.model_copy(update={"interval_seconds": 300})
    aggregates = tuple(
        AggregatedFullBar(
            target_interval_seconds=300,
            start_at=template.start_at + timedelta(minutes=offset),
            end_at=template.start_at + timedelta(minutes=offset + 5),
            open=source_bars[offset].open,
            high=max(bar.high for bar in source_bars[offset : offset + 5]),
            low=min(bar.low for bar in source_bars[offset : offset + 5]),
            close=source_bars[offset + 4].close,
            volume=sum((bar.volume for bar in source_bars[offset : offset + 5]), Decimal(0)),
            source_bar_record_ids=recorded.inserted_bar_record_ids[offset : offset + 5],
        )
        for offset in range(0, 60, 5)
    )
    derived = catalog.record_aggregated_batch(
        context,
        source_revision.dataset_revision.dataset_revision_id,
        source_key,
        target_key,
        aggregates,
        idempotency_key=str(uuid7()),
    )
    return (
        contract,
        source_bars,
        recorded,
        source_key,
        source_revision,
        target_key,
        aggregates,
        derived,
    )


def test_aggregate_component_order_source_revision_and_manifest_lineage_restore_exactly(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    (
        _,
        source_bars,
        _,
        _,
        source_revision,
        target_key,
        aggregates,
        derived,
    ) = _published_source_hour(catalog, context, archive_store)
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    target_revision = publisher.publish(
        context,
        PublicationRequest(
            series_key=target_key,
            coverage_start=source_bars[0].start_at,
            coverage_end=source_bars[0].start_at + timedelta(hours=1),
        ),
        idempotency_key=str(uuid7()),
    )
    raw = archive_store.read_verified(
        context.user_id,
        target_revision.dataset_revision.manifest_uri,
        target_revision.dataset_revision.manifest_sha256,
        target_revision.dataset_revision.manifest_byte_length,
    )
    document = json.loads(raw)
    first = next(
        item
        for item in document["correction_chain_records"]
        if item["bar_record_id"] == derived.inserted_bar_record_ids[0]
    )
    assert [item["ordinal"] for item in first["aggregate_components"]] == list(range(5))
    assert [item["source_bar_record_id"] for item in first["aggregate_components"]] == list(
        aggregates[0].source_bar_record_ids
    )
    assert {item["source_dataset_revision_id"] for item in first["aggregate_components"]} == {
        source_revision.dataset_revision.dataset_revision_id
    }
    assert {item["source_manifest_uri"] for item in first["aggregate_components"]} == {
        source_revision.dataset_revision.manifest_uri
    }
    with catalog.engine.begin() as c:
        persisted = c.execute(
            select(
                aggregate_components.c.ordinal,
                aggregate_components.c.source_bar_record_id,
            )
            .where(
                and_(
                    aggregate_components.c.owner_user_id == context.user_id,
                    aggregate_components.c.derived_bar_record_id
                    == derived.inserted_bar_record_ids[0],
                )
            )
            .order_by(aggregate_components.c.ordinal)
        ).all()
    assert persisted == list(enumerate(aggregates[0].source_bar_record_ids))


def test_aggregate_lineage_hash_is_separate_persisted_and_changes_revision_when_components_change(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    (
        _,
        source_bars,
        recorded,
        source_key,
        source_revision,
        target_key,
        aggregates,
        derived,
    ) = _published_source_hour(catalog, context, archive_store)
    with catalog.engine.begin() as c:
        before = c.execute(
            select(
                bar_versions.c.aggregate_lineage_sha256,
                bar_versions.c.payload_hash,
                bar_versions.c.source_revision,
            ).where(bar_versions.c.bar_record_id == derived.inserted_bar_record_ids[0])
        ).one()
    correction = source_bars[0].model_copy(
        update={
            "source_revision": 2,
            "close": source_bars[0].close + Decimal("0.1"),
            "supersedes_bar_record_id": recorded.inserted_bar_record_ids[0],
            "correction_reason": "SOURCE_CORRECTION",
        }
    )
    corrected = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(correction,)), idempotency_key=str(uuid7())
    )
    child = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=source_key,
            coverage_start=source_bars[0].start_at,
            coverage_end=source_bars[0].start_at + timedelta(hours=1),
            expected_parent_revision_id=source_revision.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    changed_aggregate = aggregates[0].model_copy(
        update={
            "close": correction.close,
            "source_bar_record_ids": (
                corrected.inserted_bar_record_ids[0],
                *aggregates[0].source_bar_record_ids[1:],
            ),
        }
    )
    changed = catalog.record_aggregated_batch(
        context,
        child.dataset_revision.dataset_revision_id,
        source_key,
        target_key,
        (changed_aggregate,),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        after = c.execute(
            select(
                bar_versions.c.aggregate_lineage_sha256,
                bar_versions.c.payload_hash,
                bar_versions.c.source_revision,
                bar_versions.c.correction_reason,
            ).where(bar_versions.c.bar_record_id == changed.inserted_bar_record_ids[0])
        ).one()
    assert before.aggregate_lineage_sha256 != before.payload_hash
    assert after.aggregate_lineage_sha256 != before.aggregate_lineage_sha256
    assert after.source_revision == before.source_revision + 1
    assert after.correction_reason == "DERIVED_COMPONENT_CHANGE"


def test_restore_follows_same_owner_smaller_interval_aggregate_dependency_dag(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    (
        _,
        source_bars,
        _,
        _,
        source_revision,
        target_key,
        _,
        derived,
    ) = _published_source_hour(catalog, context, archive_store)
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    target_revision = publisher.publish(
        context,
        PublicationRequest(
            series_key=target_key,
            coverage_start=source_bars[0].start_at,
            coverage_end=source_bars[0].start_at + timedelta(hours=1),
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.retain_revision(
        context,
        target_revision.dataset_revision.dataset_revision_id,
        "lane",
        str(uuid7()),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        c.execute(text("TRUNCATE market_data_series CASCADE"))
    restored = publisher.restore_retained_manifest(
        context,
        RetainedManifestRestoreRequest(
            manifest_uri=target_revision.dataset_revision.manifest_uri,
            expected_manifest_sha256=target_revision.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    assert restored.dataset_revision.dataset_revision_id == (
        target_revision.dataset_revision.dataset_revision_id
    )
    with catalog.engine.begin() as c:
        restored_revisions = set(
            c.execute(select(dataset_revisions.c.dataset_revision_id)).scalars()
        )
        restored_components = c.execute(
            select(
                aggregate_components.c.ordinal,
                aggregate_components.c.source_dataset_revision_id,
            )
            .where(
                aggregate_components.c.derived_bar_record_id == derived.inserted_bar_record_ids[0]
            )
            .order_by(aggregate_components.c.ordinal)
        ).all()
    assert source_revision.dataset_revision.dataset_revision_id in restored_revisions
    assert target_revision.dataset_revision.dataset_revision_id in restored_revisions
    assert restored_components == [
        (ordinal, source_revision.dataset_revision.dataset_revision_id) for ordinal in range(5)
    ]


def test_coverage_valid_invalid_missing_and_conflict_precedence_uses_expected_slots(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    first = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(first,)), idempotency_key=str(uuid7())
    )
    invalid = first.model_copy(
        update={
            "start_at": first.start_at + timedelta(minutes=1),
            "end_at": first.end_at + timedelta(minutes=1),
            "completed_at": first.completed_at + timedelta(minutes=1),
            "low": Decimal("2001.0"),
        }
    )
    with pytest.raises(MarketDataError):
        catalog.record_completed_batch(
            context, RecordBatchInput(bars=(invalid,)), idempotency_key=str(uuid7())
        )
    conflict = first.model_copy(update={"close": Decimal("2000.0")})
    with pytest.raises(MarketDataError):
        catalog.record_completed_batch(
            context, RecordBatchInput(bars=(conflict,)), idempotency_key=str(uuid7())
        )
    coverage = MarketDataReader(
        catalog, archive_store, context_is_current=lambda _: True
    ).read_coverage(
        context,
        CoverageRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=first.start_at,
            coverage_end=first.start_at + timedelta(minutes=3),
        ),
    )
    assert [(span.kind, span.reason) for span in coverage.spans] == [
        ("open", "OPEN"),
        ("invalid", "INVALID_BAR"),
        ("missing", "NO_VALID_BAR"),
    ]
    assert coverage.quality_counts == {
        "valid": 1,
        "invalid": 1,
        "missing": 1,
        "duplicate_conflict": 1,
    }


def test_material_contract_update_after_series_exists_is_conflict_and_cannot_create_second_logical_series(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    mutation = contract_input(contract.calendar_id).model_copy(
        update={
            "contract_id": contract.contract_id,
            "expected_version": 1,
            "multiplier": Decimal(20),
        }
    )
    with pytest.raises(MarketDataError) as caught:
        catalog.register_contract(context, mutation, idempotency_key=str(uuid7()))
    assert caught.value.code is MarketDataCode.CONFLICT
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(series)).scalar_one() == 1
        assert c.execute(select(func.count()).select_from(contract_versions)).scalar_one() == 1


def test_semantically_invalid_candidate_commits_safe_observation_but_no_batch_bars(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    valid = bar_input(contract.contract_id)
    primer = valid.model_copy(
        update={
            "start_at": valid.start_at + timedelta(minutes=1),
            "end_at": valid.end_at + timedelta(minutes=1),
            "completed_at": valid.completed_at + timedelta(minutes=1),
        }
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(primer,)), idempotency_key=str(uuid7())
    )
    invalid = valid.model_copy(update={"end_at": valid.end_at + timedelta(seconds=1)})
    key = str(uuid7())
    with pytest.raises(MarketDataError) as failed:
        catalog.record_completed_batch(
            context, RecordBatchInput(bars=(invalid,)), idempotency_key=key
        )
    with pytest.raises(MarketDataError) as replay:
        catalog.record_completed_batch(
            context, RecordBatchInput(bars=(invalid,)), idempotency_key=key
        )
    assert failed.value.code == replay.value.code == MarketDataCode.VALIDATION_ERROR
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(quality_observations)).scalar_one() == 1
        assert c.execute(select(func.count()).select_from(bar_versions)).scalar_one() == 1
        assert (
            c.execute(
                select(idempotency.c.state).where(idempotency.c.idempotency_key == key)
            ).scalar_one()
            == "failed"
        )


def test_payload_hash_projection_and_version_fingerprint_are_canonical_and_restart_stable(
    catalog, contexts
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    candidate = bar_input(contract.contract_id)
    result = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(candidate,)), idempotency_key=str(uuid7())
    )
    payload_hash = canonical_sha256(
        {
            name: value
            for name, value in candidate.model_dump().items()
            if name != "correction_reason"
        }
    )
    expected_fingerprint = canonical_sha256(
        {
            "payload_hash": payload_hash,
            "correction_reason": None,
            "aggregate_lineage_sha256": None,
        }
    )
    with catalog.engine.begin() as c:
        stored = c.execute(
            select(
                bar_versions.c.payload_hash,
                bar_versions.c.version_fingerprint_sha256,
            ).where(bar_versions.c.bar_record_id == result.inserted_bar_record_ids[0])
        ).one()
    assert stored == (payload_hash, expected_fingerprint)
    assert canonical_sha256(candidate.model_dump()) == canonical_sha256(
        dict(reversed(list(candidate.model_dump().items())))
    )


def test_contract_provider_id_and_provenance_round_trip_but_credentials_and_raw_payload_never_leak(
    catalog, contexts
) -> None:
    _, contract = seed(catalog, contexts[0])
    projection = contract.model_dump(mode="json")
    assert projection["provider_contract_id"] == "MGCZ6"
    assert projection["provenance_ref"] == "synthetic"
    assert not {"credential", "secret", "raw_payload"}.intersection(projection)
    assert "provider_contract_reference" not in repr(contract)


def test_same_owner_two_lanes_receive_same_revision_and_object_ids_without_copies(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    published = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    refs = [
        catalog.retain_revision(
            context,
            published.dataset_revision.dataset_revision_id,
            "lane",
            str(uuid7()),
            idempotency_key=str(uuid7()),
        )
        for _ in range(2)
    ]
    assert {item.dataset_revision_id for item in refs} == {
        published.dataset_revision.dataset_revision_id
    }
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(archive_objects)).scalar_one() == len(
            published.dataset_revision.partition_refs
        )
