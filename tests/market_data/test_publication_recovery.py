from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Event, Thread
from uuid import uuid7

import pytest
from pydantic import ValidationError
from sqlalchemy import and_, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from familytrade.access.models import AccessError
from familytrade.market_data.archive import DatasetPublisher, MarketDataReader
from familytrade.market_data.catalog import (
    MarketDataCatalog,
    active_bars,
    aggregate_components,
    archive_objects,
    bar_versions,
    dataset_revisions,
    idempotency,
    prestage_writes,
    publication_files,
    publication_retention_conversions,
    publication_revisions,
    publications,
    read_snapshots,
    retention_refs,
    revision_bars,
    revision_partitions,
    series,
    series_fences,
)
from familytrade.market_data.models import (
    CalendarCreateInput,
    CalendarWindowInput,
    CausalCommittedSelection,
    CausalLatestRead,
    CompletedBarVersionInput,
    DatasetRevision,
    FuturesContractInput,
    LatestRead,
    MarketDataError,
    PartitionRef,
    PinnedRead,
    PublicationRequest,
    QuarantinedLatestRecoveryRequest,
    ReadBarsRequest,
    ReadCursor,
    RecordBatchInput,
    RetainedManifestRestoreRequest,
    SeriesKey,
    canonical_json_bytes,
)

from .test_models_catalog import _published_source_hour, bar_input, seed


def test_initial_expected_parent_null_nonnull_match_and_mismatch_terminal_semantics(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
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
    start = bar_input(contract.contract_id).start_at
    request = PublicationRequest(
        series_key=key,
        coverage_start=start,
        coverage_end=start + timedelta(minutes=1),
        expected_parent_revision_id=str(uuid7()),
    )
    idem = str(uuid7())
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    for _ in range(2):
        with pytest.raises(MarketDataError) as caught:
            publisher.publish(context, request, idempotency_key=idem)
        assert caught.value.code.value == "STALE_VERSION"
    with catalog.engine.begin() as c:
        root = (
            c.execute(select(idempotency).where(idempotency.c.idempotency_key == idem))
            .mappings()
            .one()
        )
        assert root["state"] == "failed"
        assert root["parent_admitted_at"] is None
        assert c.execute(select(func.count()).select_from(publications)).scalar_one() == 0


def _scenario_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    start = bar_input(contract.contract_id).start_at
    worker_id = str(uuid7())
    publisher = DatasetPublisher(catalog, archive_store, worker_id=worker_id)
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    request = PublicationRequest(
        series_key=key,
        coverage_start=start,
        coverage_end=start + timedelta(minutes=1),
        expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
    )
    root_key = str(uuid7())

    def stop_after_admission(point: str) -> None:
        if point == "after_stale_takeover_commit_before_snapshot":
            raise RuntimeError("admitted")

    monkeypatch.setattr(publisher, "_inject", stop_after_admission)
    with pytest.raises(RuntimeError, match="admitted"):
        publisher.publish(context, request, idempotency_key=root_key)
    winner = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context, request, idempotency_key=str(uuid7())
    )
    monkeypatch.setattr(publisher, "_inject", lambda _: None)
    rebased = publisher.publish(context, request, idempotency_key=root_key)
    assert (
        rebased.dataset_revision.parent_revision_id == winner.dataset_revision.dataset_revision_id
    )
    with catalog.engine.begin() as c:
        root = (
            c.execute(select(idempotency).where(idempotency.c.idempotency_key == root_key))
            .mappings()
            .one()
        )
    assert root["rebase_count"] == 1
    replay = publisher.publish(context, request, idempotency_key=root_key)
    assert replay.replayed
    assert replay.dataset_revision == rebased.dataset_revision


def _scenario_fourth_displacement_terminally_fails_and_same_key_replays_conflict(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    request = PublicationRequest(
        series_key=key,
        coverage_start=start,
        coverage_end=start + timedelta(minutes=1),
        expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
    )
    root_key = str(uuid7())
    monkeypatch.setattr(
        publisher,
        "_inject",
        lambda point: (
            (_ for _ in ()).throw(RuntimeError("admitted"))
            if point == "after_stale_takeover_commit_before_snapshot"
            else None
        ),
    )
    with pytest.raises(RuntimeError):
        publisher.publish(context, request, idempotency_key=root_key)
    with catalog.engine.begin() as c:
        c.execute(
            update(idempotency)
            .where(idempotency.c.idempotency_key == root_key)
            .values(rebase_count=3)
        )
    DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context, request, idempotency_key=str(uuid7())
    )
    monkeypatch.setattr(publisher, "_inject", lambda _: None)
    for _ in range(2):
        with pytest.raises(MarketDataError) as caught:
            publisher.publish(context, request, idempotency_key=root_key)
        assert caught.value.code.value == "CONFLICT"


@pytest.mark.parametrize(
    "injection_id",
    (
        "after_stale_takeover_commit_before_snapshot",
        "before_temp_create",
        "during_temp_write",
        "after_file_fsync_before_directory_fsync",
        "after_all_fsync_before_staged_tx",
        "after_staged_commit_before_rename",
        "during_object_renames",
        "after_all_renames_before_directory_fsync",
        "after_rename_fsync_before_publish_tx",
        "during_publish_tx_before_commit",
        "after_publish_commit_before_cleanup",
        "during_cleanup",
        "after_recovery_admission_before_snapshot",
        "published_file_missing_or_corrupt",
        "correction_or_publisher_wins_parent_race",
    ),
)
def _scenario_publication_crash_matrix(
    catalog, contexts, archive_store, monkeypatch, injection_id
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    publication_key = str(uuid7())
    fired = False

    def crash(point: str) -> None:
        nonlocal fired
        if point == injection_id and not fired:
            fired = True
            raise RuntimeError(f"injected:{point}")

    monkeypatch.setattr(publisher, "_inject", crash)
    if injection_id == "after_recovery_admission_before_snapshot":
        with catalog.engine.begin() as c:
            fence = (
                c.execute(
                    select(series_fences)
                    .where(series_fences.c.owner_user_id == context.user_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            fresh_fence = int(fence["fencing_token"]) + 1
            c.execute(
                update(series_fences)
                .where(series_fences.c.series_id == fence["series_id"])
                .values(fencing_token=fresh_fence)
            )
            c.execute(
                update(dataset_revisions)
                .where(
                    dataset_revisions.c.dataset_revision_id
                    == parent.dataset_revision.dataset_revision_id
                )
                .values(status="quarantined", record_version=3)
            )
            c.execute(
                update(publications)
                .where(publications.c.publication_id == parent.publication_id)
                .values(
                    state="quarantined",
                    safe_reason="ARCHIVE_INTEGRITY",
                    fencing_token=fresh_fence,
                )
            )
        with pytest.raises(RuntimeError, match=f"injected:{injection_id}"):
            publisher.recover_quarantined_latest(
                context,
                QuarantinedLatestRecoveryRequest(
                    quarantined_revision_id=parent.dataset_revision.dataset_revision_id,
                    expected_manifest_sha256=parent.dataset_revision.manifest_sha256,
                ),
                idempotency_key=publication_key,
            )
        with catalog.engine.begin() as c:
            assert (
                c.execute(
                    select(dataset_revisions.c.status).where(
                        dataset_revisions.c.dataset_revision_id
                        == parent.dataset_revision.dataset_revision_id
                    )
                ).scalar_one()
                == "quarantined"
            )
        monkeypatch.setattr(publisher, "_inject", lambda _: None)
        recovered = publisher.recover_quarantined_latest(
            context,
            QuarantinedLatestRecoveryRequest(
                quarantined_revision_id=parent.dataset_revision.dataset_revision_id,
                expected_manifest_sha256=parent.dataset_revision.manifest_sha256,
            ),
            idempotency_key=publication_key,
        )
        replay = publisher.recover_quarantined_latest(
            context,
            QuarantinedLatestRecoveryRequest(
                quarantined_revision_id=parent.dataset_revision.dataset_revision_id,
                expected_manifest_sha256=parent.dataset_revision.manifest_sha256,
            ),
            idempotency_key=publication_key,
        )
        assert replay.replayed and replay.dataset_revision == recovered.dataset_revision
        return
    if injection_id == "published_file_missing_or_corrupt":
        path = archive_store.resolve(context.user_id, parent.dataset_revision.partition_refs[0].uri)
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
        reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
        with pytest.raises(MarketDataError) as first_failure:
            reader.read_bars(
                context,
                ReadBarsRequest(
                    series_key=key,
                    coverage_start=start,
                    coverage_end=start + timedelta(minutes=1),
                    policy=LatestRead(),
                ),
            )
        with pytest.raises(MarketDataError) as replay_failure:
            reader.read_bars(
                context,
                ReadBarsRequest(
                    series_key=key,
                    coverage_start=start,
                    coverage_end=start + timedelta(minutes=1),
                    policy=LatestRead(),
                ),
            )
        assert first_failure.value.code == replay_failure.value.code
        with catalog.engine.begin() as c:
            assert (
                c.execute(
                    select(dataset_revisions.c.status).where(
                        dataset_revisions.c.dataset_revision_id
                        == parent.dataset_revision.dataset_revision_id
                    )
                ).scalar_one()
                == "quarantined"
            )
        return
    if injection_id == "correction_or_publisher_wins_parent_race":
        monkeypatch.setattr(publisher, "_inject", lambda _: None)
        winner = publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
                expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=str(uuid7()),
        )
        with pytest.raises(MarketDataError) as caught:
            publisher.publish(
                context,
                PublicationRequest(
                    series_key=key,
                    coverage_start=start,
                    coverage_end=start + timedelta(minutes=1),
                    expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
                ),
                idempotency_key=publication_key,
            )
        assert caught.value.code.value == "STALE_VERSION"
        with pytest.raises(MarketDataError) as replayed_failure:
            publisher.publish(
                context,
                PublicationRequest(
                    series_key=key,
                    coverage_start=start,
                    coverage_end=start + timedelta(minutes=1),
                    expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
                ),
                idempotency_key=publication_key,
            )
        assert replayed_failure.value.code == caught.value.code
        with catalog.engine.begin() as c:
            assert (
                c.execute(
                    select(series.c.latest_revision_id).where(
                        series.c.contract_id == contract.contract_id
                    )
                ).scalar_one()
                == winner.dataset_revision.dataset_revision_id
            )
        return
    with pytest.raises(RuntimeError, match=f"injected:{injection_id}"):
        publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
                expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=publication_key,
        )
    assert fired
    with catalog.engine.begin() as c:
        root = (
            c.execute(select(idempotency).where(idempotency.c.idempotency_key == publication_key))
            .mappings()
            .one()
        )
        latest = c.execute(
            select(series.c.latest_revision_id).where(series.c.contract_id == contract.contract_id)
        ).scalar_one()
        staged_exists = bool(
            root["current_publication_id"]
            and c.execute(
                select(publications.c.publication_id).where(
                    publications.c.publication_id == root["current_publication_id"]
                )
            ).scalar()
        )
    committed = injection_id in {
        "after_publish_commit_before_cleanup",
        "during_cleanup",
    }
    assert (latest != parent.dataset_revision.dataset_revision_id) is committed
    if staged_exists:
        monkeypatch.setattr(publisher, "_inject", lambda _: None)
        recovery = publisher.reconcile_one(root["current_publication_id"])
        assert recovery.terminal_state == "published"
        publisher.cleanup_one(root["current_publication_id"])
    monkeypatch.setattr(publisher, "_inject", lambda _: None)
    request = PublicationRequest(
        series_key=key,
        coverage_start=start,
        coverage_end=start + timedelta(minutes=1),
        expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
    )
    completed = publisher.publish(context, request, idempotency_key=publication_key)
    replay = publisher.publish(context, request, idempotency_key=publication_key)
    assert replay.replayed and replay.dataset_revision == completed.dataset_revision
    with catalog.engine.begin() as c:
        assert (
            c.execute(
                select(series.c.latest_revision_id).where(
                    series.c.contract_id == contract.contract_id
                )
            ).scalar_one()
            == replay.dataset_revision.dataset_revision_id
        )
        assert set(
            c.execute(
                select(publication_files.c.state).where(
                    publication_files.c.publication_id == replay.publication_id
                )
            ).scalars()
        ) == {"published"}
        assert (
            c.execute(
                select(func.count())
                .select_from(publications)
                .where(
                    and_(
                        publications.c.publication_id == replay.publication_id,
                        publications.c.state == "published",
                    )
                )
            ).scalar_one()
            == 1
        )
    expected_abandoned_temps = {
        "during_temp_write": 1,
        "after_file_fsync_before_directory_fsync": 1,
        "after_all_fsync_before_staged_tx": 2,
    }.get(injection_id, 0)
    assert (
        len(tuple(archive_store._owner_root(context.user_id).rglob("*.tmp")))
        == expected_abandoned_temps
    )


def _scenario_multiple_keys_and_versions_have_deterministic_monotonic_preservation_frontiers_and_ref_mapping(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    second_input = bar_input(
        contract.contract_id,
        2,
        first.inserted_bar_record_ids[0],
        "SOURCE_CORRECTION",
    )
    second = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(second_input,)), idempotency_key=str(uuid7())
    )
    third_input = bar_input(
        contract.contract_id,
        3,
        second.inserted_bar_record_ids[0],
        "SOURCE_CORRECTION",
    )
    third = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(third_input,)), idempotency_key=str(uuid7())
    )
    start = bar_input(contract.contract_id).start_at
    result = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
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
    assert len(result.preserved_revision_ids) == 2
    with catalog.engine.begin() as c:
        ordered = list(
            c.execute(
                select(publication_revisions.c.dataset_revision_id)
                .where(publication_revisions.c.publication_id == result.publication_id)
                .order_by(publication_revisions.c.ordinal)
            ).scalars()
        )
        selected = [
            c.execute(
                select(revision_bars.c.bar_record_id).where(
                    revision_bars.c.dataset_revision_id == revision_id
                )
            ).scalar_one()
            for revision_id in ordered
        ]
    assert selected == [
        first.inserted_bar_record_ids[0],
        second.inserted_bar_record_ids[0],
        third.inserted_bar_record_ids[0],
    ]
    assert ordered[:-1] == list(result.preserved_revision_ids)
    assert result.dataset_revision.dataset_revision_id == ordered[-1]


def _scenario_depth_999_publication_rolls_over_to_self_contained_checkpoint_before_child(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    parent_document = json.loads(
        archive_store.read_verified(
            context.user_id,
            parent.dataset_revision.manifest_uri,
            parent.dataset_revision.manifest_sha256,
            parent.dataset_revision.manifest_byte_length,
        )
    )
    deep_revisions: list[tuple[DatasetRevision, dict[str, object]]] = []
    previous = parent.dataset_revision
    for depth in range(1, 1000):
        revision_id = str(uuid7())
        manifest_uri = f"ft-archive://manifest/{revision_id}"
        document = dict(parent_document)
        document.update(
            dataset_revision_id=revision_id,
            parent_revision_id=previous.dataset_revision_id,
            parent_manifest_uri=previous.manifest_uri,
            parent_manifest_sha256=previous.manifest_sha256,
            manifest_uri=manifest_uri,
            parent_depth=depth,
            restore_closure_revision_count=depth + 1,
            restore_closure_row_count=depth + 1,
            restore_closure_bytes=previous.restore_closure_bytes,
        )
        manifest_bytes = canonical_json_bytes(document)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        archive_store.finalize(
            context.user_id,
            archive_store.write_temp(context.user_id, manifest_bytes, "depth-manifest"),
            manifest_uri,
        )
        payload = {
            name: document[name]
            for name in DatasetRevision.model_fields
            if name not in {"manifest_sha256", "manifest_byte_length"}
        }
        payload.update(
            manifest_sha256=manifest_sha,
            manifest_byte_length=len(manifest_bytes),
        )
        previous = DatasetRevision.model_validate(payload)
        deep_revisions.append((previous, document))
        parent_document = document
    with catalog.engine.begin() as c:
        series_id = c.execute(
            select(series.c.series_id).where(
                and_(
                    series.c.owner_user_id == context.user_id,
                    series.c.contract_id == contract.contract_id,
                    series.c.interval_seconds == 60,
                )
            )
        ).scalar_one()
        for revision, _ in deep_revisions:
            c.execute(
                insert(dataset_revisions).values(
                    owner_user_id=context.user_id,
                    dataset_revision_id=revision.dataset_revision_id,
                    schema_version="v1",
                    series_id=series_id,
                    contract_version=revision.contract_version,
                    calendar_id=revision.calendar_id,
                    calendar_version=revision.calendar_version,
                    projection=revision.model_dump(mode="json"),
                    parent_revision_id=revision.parent_revision_id,
                    manifest_uri=revision.manifest_uri,
                    manifest_sha256=revision.manifest_sha256,
                    manifest_byte_length=revision.manifest_byte_length,
                    coverage_start=revision.coverage_start,
                    coverage_end=revision.coverage_end,
                    source_watermark=revision.source_watermark.model_dump(mode="json"),
                    correction_refs=[],
                    status="published",
                    created_at=revision.created_at,
                    published_at=revision.published_at,
                    parent_depth=revision.parent_depth,
                    restore_closure_revision_count=revision.restore_closure_revision_count,
                    restore_closure_row_count=revision.restore_closure_row_count,
                    restore_closure_bytes=revision.restore_closure_bytes,
                    rollover_from_revision_id=None,
                    rollover_from_manifest_uri=None,
                    rollover_from_manifest_sha256=None,
                    recovery_from_quarantined_revision_id=None,
                    recovery_from_manifest_uri=None,
                    recovery_from_manifest_sha256=None,
                    record_version=2,
                )
            )
            c.execute(
                insert(revision_partitions).values(
                    owner_user_id=context.user_id,
                    dataset_revision_id=revision.dataset_revision_id,
                    ordinal=0,
                    object_id=revision.partition_refs[0].object_id,
                )
            )
            c.execute(
                insert(revision_bars).values(
                    owner_user_id=context.user_id,
                    dataset_revision_id=revision.dataset_revision_id,
                    ordinal=0,
                    series_id=series_id,
                    start_at=bar_input(contract.contract_id).start_at,
                    source_revision=1,
                    bar_record_id=first.inserted_bar_record_ids[0],
                    object_id=revision.partition_refs[0].object_id,
                )
            )
        c.execute(
            update(series)
            .where(
                and_(
                    series.c.owner_user_id == context.user_id,
                    series.c.series_id == series_id,
                )
            )
            .values(
                latest_revision_id=previous.dataset_revision_id,
                record_version=series.c.record_version + 1,
            )
        )
    parent = parent.model_copy(update={"dataset_revision": previous})
    correction = bar_input(
        contract.contract_id,
        2,
        first.inserted_bar_record_ids[0],
        "SOURCE_CORRECTION",
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(correction,)), idempotency_key=str(uuid7())
    )
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        planned = list(
            c.execute(
                select(dataset_revisions)
                .join(
                    publication_revisions,
                    and_(
                        publication_revisions.c.owner_user_id == dataset_revisions.c.owner_user_id,
                        publication_revisions.c.dataset_revision_id
                        == dataset_revisions.c.dataset_revision_id,
                    ),
                )
                .where(publication_revisions.c.publication_id == child.publication_id)
                .order_by(publication_revisions.c.ordinal)
            ).mappings()
        )
        rollover_ref = c.execute(
            select(retention_refs.c.dataset_revision_id).where(
                and_(
                    retention_refs.c.reference_kind == "rollover",
                    retention_refs.c.reference_id == planned[0]["dataset_revision_id"],
                )
            )
        ).scalar_one()
    assert len(planned) == 2
    checkpoint, final = planned
    assert checkpoint["parent_revision_id"] is None
    assert checkpoint["rollover_from_revision_id"] == parent.dataset_revision.dataset_revision_id
    assert checkpoint["parent_depth"] == 0
    assert final["parent_revision_id"] == checkpoint["dataset_revision_id"]
    assert final["parent_depth"] == 1
    assert rollover_ref == parent.dataset_revision.dataset_revision_id


def _scenario_corruption_closure_pages_every_descendant_without_total_count_cutoff(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
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
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    root = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    root_id = root.dataset_revision.dataset_revision_id
    with catalog.engine.begin() as connection:
        root_row = dict(
            connection.execute(
                select(dataset_revisions).where(dataset_revisions.c.dataset_revision_id == root_id)
            )
            .mappings()
            .one()
        )
        root_object = dict(
            connection.execute(
                select(archive_objects)
                .join(
                    revision_partitions,
                    and_(
                        revision_partitions.c.owner_user_id == archive_objects.c.owner_user_id,
                        revision_partitions.c.object_id == archive_objects.c.object_id,
                    ),
                )
                .where(revision_partitions.c.dataset_revision_id == root_id)
            )
            .mappings()
            .one()
        )
        root_selected = dict(
            connection.execute(
                select(revision_bars).where(revision_bars.c.dataset_revision_id == root_id)
            )
            .mappings()
            .one()
        )
        object_rows: list[dict[str, object]] = []
        revision_rows: list[dict[str, object]] = []
        partition_rows: list[dict[str, object]] = []
        selected_rows: list[dict[str, object]] = []
        for index in range(1001):
            child_id, object_id = str(uuid7()), str(uuid7())
            object_row = dict(root_object)
            object_row.update(
                object_id=object_id,
                uri=f"ft-archive://object/{object_id}",
                origin_publication_id=str(uuid7()),
                publication_id=None,
                catalog_origin="retained_manifest",
            )
            object_rows.append(object_row)
            child_projection = root.dataset_revision.model_copy(
                update={
                    "dataset_revision_id": child_id,
                    "parent_revision_id": root_id,
                    "manifest_uri": f"ft-archive://manifest/{child_id}",
                    "manifest_sha256": hashlib.sha256(child_id.encode()).hexdigest(),
                    "manifest_byte_length": 1,
                    "partition_refs": (
                        root.dataset_revision.partition_refs[0].model_copy(
                            update={
                                "object_id": object_id,
                                "uri": f"ft-archive://object/{object_id}",
                                "origin_publication_id": object_row["origin_publication_id"],
                            }
                        ),
                    ),
                    "parent_depth": 1,
                    "restore_closure_revision_count": 2,
                    "restore_closure_row_count": 2,
                    "restore_closure_bytes": root_object["byte_length"] * 2,
                }
            )
            revision_row = dict(root_row)
            revision_row.update(
                dataset_revision_id=child_id,
                projection=child_projection.model_dump(mode="json"),
                parent_revision_id=root_id,
                manifest_uri=child_projection.manifest_uri,
                manifest_sha256=child_projection.manifest_sha256,
                manifest_byte_length=child_projection.manifest_byte_length,
                parent_depth=1,
                restore_closure_revision_count=2,
                restore_closure_row_count=2,
                restore_closure_bytes=root_object["byte_length"] * 2,
                created_at=root_row["created_at"] + timedelta(microseconds=index + 1),
            )
            revision_rows.append(revision_row)
            partition_rows.append(
                {
                    "owner_user_id": context.user_id,
                    "dataset_revision_id": child_id,
                    "ordinal": 0,
                    "object_id": object_id,
                }
            )
            selected_row = dict(root_selected)
            selected_row.update(dataset_revision_id=child_id, object_id=object_id)
            selected_rows.append(selected_row)
        connection.execute(insert(archive_objects), object_rows)
        connection.execute(insert(dataset_revisions), revision_rows)
        connection.execute(insert(revision_partitions), partition_rows)
        connection.execute(insert(revision_bars), selected_rows)
    corrupt_path = archive_store.resolve(
        context.user_id, root.dataset_revision.partition_refs[0].uri
    )
    corrupt_path.chmod(0o600)
    corrupt_path.write_bytes(b"corrupt")
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    with pytest.raises(MarketDataError, match="integrity"):
        reader.read_bars(
            context,
            ReadBarsRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
                policy=PinnedRead(dataset_revision_id=root_id),
            ),
        )
    with catalog.engine.begin() as c:
        states = list(c.execute(select(dataset_revisions.c.status)).scalars())
    assert states == ["quarantined"] * 1002


def _scenario_append_day_32_reuses_31_parent_objects_writes_one_object_and_publishes_32_object_union(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    base = datetime(2026, 10, 1, tzinfo=UTC)
    windows = []
    for day_index in range(32):
        day_start = base + timedelta(days=day_index)
        windows.append(
            CalendarWindowInput(
                kind="open",
                start_at=day_start,
                end_at=day_start + timedelta(minutes=1),
                trading_day=day_start.date(),
                reason=None,
            )
        )
        if day_index < 31:
            windows.append(
                CalendarWindowInput(
                    kind="maintenance",
                    start_at=day_start + timedelta(minutes=1),
                    end_at=day_start + timedelta(days=1),
                    trading_day=None,
                    reason="DAILY_BREAK",
                )
            )
    calendar = catalog.create_calendar(
        context,
        CalendarCreateInput(
            exchange_timezone="UTC",
            coverage_start=base,
            coverage_end=base + timedelta(days=31, minutes=1),
            windows=tuple(windows),
            metadata_as_of=base,
            provenance_ref="fixture:32-days",
        ),
        idempotency_key=str(uuid7()),
    )
    contract = catalog.register_contract(
        context,
        FuturesContractInput(
            provider="synthetic",
            provider_contract_id="MGCZ6-32-days",
            root_symbol="MGC",
            exchange="XUTC",
            currency="USD",
            tick_size=Decimal("0.10"),
            multiplier=Decimal(10),
            expiry_label="2026-12",
            last_trade_at=base + timedelta(days=40),
            calendar_id=calendar.calendar_id,
            calendar_version=1,
            entry_cutoff_at=base + timedelta(days=35),
            liquidation_start_at=base + timedelta(days=38),
            metadata_as_of=base,
            provenance_ref="fixture:32-days",
        ),
        idempotency_key=str(uuid7()),
    )
    candidates = tuple(
        CompletedBarVersionInput(
            source="synthetic",
            price_basis="trades",
            contract_id=contract.contract_id,
            interval_seconds=60,
            start_at=base + timedelta(days=index),
            end_at=base + timedelta(days=index, minutes=1),
            open=Decimal("2000.0"),
            high=Decimal("2000.2"),
            low=Decimal("1999.9"),
            close=Decimal("2000.1"),
            volume=Decimal(12),
            source_revision=1,
            completed_at=base + timedelta(days=index, minutes=1),
            quality="valid",
        )
        for index in range(32)
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=candidates), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=base,
            coverage_end=base + timedelta(days=30, minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    assert len(parent.dataset_revision.partition_refs) == 31
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=base + timedelta(days=31),
            coverage_end=base + timedelta(days=31, minutes=1),
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    assert len(child.dataset_revision.partition_refs) == 32
    assert (
        {ref.object_id for ref in child.dataset_revision.partition_refs}
        & {ref.object_id for ref in parent.dataset_revision.partition_refs}
    ) == {ref.object_id for ref in parent.dataset_revision.partition_refs}
    with catalog.engine.begin() as c:
        assert (
            c.execute(
                select(func.count())
                .select_from(archive_objects)
                .where(archive_objects.c.publication_id == child.publication_id)
            ).scalar_one()
            == 1
        )


def _scenario_restore_corrected_child_walks_parent_manifests_and_rebuilds_old_then_new_registry(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        c.execute(text("TRUNCATE market_data_series CASCADE"))
    restored = publisher.restore_retained_manifest(
        context,
        request=RetainedManifestRestoreRequest(
            manifest_uri=child.dataset_revision.manifest_uri,
            expected_manifest_sha256=child.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    assert (
        restored.dataset_revision.dataset_revision_id == child.dataset_revision.dataset_revision_id
    )
    assert restored.latest_pointer_changed
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(dataset_revisions)).scalar_one() == 2
        assert c.execute(select(func.count()).select_from(revision_bars)).scalar_one() == 2


def _scenario_pinned_revision_survives_correction_fixture(
    catalog, contexts, archive_store, contract_case
) -> None:
    case = contract_case("pinned_revision_survives_correction")
    expected = case["expected"]
    context = contexts[0]
    _, contract = seed(catalog, context)
    initial = bar_input(contract.contract_id).model_copy(
        update={"close": Decimal(expected["completed_run_close"])}
    )
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(initial,)),
        idempotency_key=str(uuid7()),
    )
    series_key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
        context,
        PublicationRequest(
            series_key=series_key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            expected_parent_revision_id=None,
        ),
        idempotency_key=str(uuid7()),
    )
    manifest_path = archive_store.resolve(context.user_id, published.dataset_revision.manifest_uri)
    manifest_before = manifest_path.read_bytes()
    object_ref = published.dataset_revision.partition_refs[0]
    object_path = archive_store.resolve(context.user_id, object_ref.uri)
    object_before = object_path.read_bytes()
    corrected = bar_input(
        contract.contract_id, 2, first.inserted_bar_record_ids[0], "SOURCE_CORRECTION"
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(corrected,)), idempotency_key=str(uuid7())
    )
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=series_key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            expected_parent_revision_id=published.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    latest = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=series_key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            policy=LatestRead(),
        ),
    )
    pinned = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=series_key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            policy=PinnedRead(dataset_revision_id=published.dataset_revision.dataset_revision_id),
        ),
    )
    assert (
        child.dataset_revision.correction_refs[0].old_bar_record_id
        == first.inserted_bar_record_ids[0]
    )
    assert latest.selections[0].bar.close == corrected.close
    assert latest.selections[0].bar.close == Decimal(expected["new_latest_read_close"])
    assert pinned.selections[0].bar.close == initial.close
    assert object_path.exists() is expected["r1_object_retained"]
    assert object_path.read_bytes() == object_before
    assert hashlib.sha256(object_before).hexdigest() == object_ref.sha256
    assert (manifest_path.read_bytes() != manifest_before) is expected["r1_manifest_changed"]


def test_forward_active_corrections_before_and_after_cursor_fixture(
    postgres_engine, contexts, archive_store, contract_case
) -> None:
    case = contract_case("forward_active_corrections_before_and_after_cursor")
    given, expected = case["given"], case["expected"]
    context = contexts[0]
    clock_value = [datetime(2026, 9, 14, 9, 58, tzinfo=UTC)]
    catalog = MarketDataCatalog(
        postgres_engine, context_is_current=lambda _: True, clock=lambda: clock_value[0]
    )
    coverage_start = datetime(2026, 9, 14, 9, 59, tzinfo=UTC)
    calendar = catalog.create_calendar(
        context,
        CalendarCreateInput(
            exchange_timezone="America/Chicago",
            coverage_start=coverage_start,
            coverage_end=coverage_start + timedelta(minutes=2),
            windows=(
                CalendarWindowInput(
                    kind="open",
                    start_at=coverage_start,
                    end_at=coverage_start + timedelta(minutes=2),
                    trading_day=date(2026, 9, 14),
                    reason=None,
                ),
            ),
            metadata_as_of=clock_value[0],
            provenance_ref="fixture:forward-active-corrections",
        ),
        idempotency_key=str(uuid7()),
    )
    contract = catalog.register_contract(
        context,
        FuturesContractInput(
            provider="synthetic",
            provider_contract_id="MGCZ6-forward",
            root_symbol="MGC",
            exchange="XCHI",
            currency="USD",
            tick_size=Decimal("0.10"),
            multiplier=Decimal(10),
            expiry_label="2026-12",
            last_trade_at=coverage_start + timedelta(days=30),
            calendar_id=calendar.calendar_id,
            calendar_version=1,
            entry_cutoff_at=coverage_start + timedelta(days=20),
            liquidation_start_at=coverage_start + timedelta(days=25),
            metadata_as_of=clock_value[0],
            provenance_ref="fixture:forward-active-corrections",
        ),
        idempotency_key=str(uuid7()),
    )

    def candidate(start_at, revision=1, supersedes=None, reason=None):
        return CompletedBarVersionInput(
            source="synthetic",
            price_basis="trades",
            contract_id=contract.contract_id,
            interval_seconds=60,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=1),
            open=Decimal("2000.0"),
            high=Decimal("2000.3"),
            low=Decimal("1999.9"),
            close=Decimal("2000.1") + Decimal("0.1") * (revision - 1),
            volume=Decimal(12),
            source_revision=revision,
            completed_at=start_at + timedelta(minutes=1),
            quality="valid",
            supersedes_bar_record_id=supersedes,
            correction_reason=reason,
        )

    starts = (coverage_start, coverage_start + timedelta(minutes=1))
    ids: list[list[str]] = [[], []]
    receipt_fields = (
        given["bar_a"]["r1"]["received_at"],
        given["bar_a"]["r2"]["received_at"],
        given["bar_b"]["r1"]["received_at"],
        given["bar_b"]["r2"]["received_at"],
    )
    clock_value[0] = datetime.fromisoformat(receipt_fields[0])
    ids[0].append(
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(candidate(starts[0]),)),
            idempotency_key=str(uuid7()),
        ).inserted_bar_record_ids[0]
    )
    clock_value[0] = datetime.fromisoformat(receipt_fields[1])
    ids[0].append(
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(candidate(starts[0], 2, ids[0][0], "SOURCE_CORRECTION"),)),
            idempotency_key=str(uuid7()),
        ).inserted_bar_record_ids[0]
    )
    clock_value[0] = datetime.fromisoformat(receipt_fields[2])
    ids[1].append(
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(candidate(starts[1]),)),
            idempotency_key=str(uuid7()),
        ).inserted_bar_record_ids[0]
    )
    clock_value[0] = datetime.fromisoformat(receipt_fields[3])
    ids[1].append(
        catalog.record_completed_batch(
            context,
            RecordBatchInput(bars=(candidate(starts[1], 2, ids[1][0], "SOURCE_CORRECTION"),)),
            idempotency_key=str(uuid7()),
        ).inserted_bar_record_ids[0]
    )
    known_at = clock_value[0] + timedelta(seconds=1)
    result = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
        context,
        ReadBarsRequest(
            series_key=SeriesKey(
                source="synthetic",
                price_basis="trades",
                contract_id=contract.contract_id,
                interval_seconds=60,
            ),
            coverage_start=coverage_start,
            coverage_end=coverage_start + timedelta(minutes=2),
            policy=CausalLatestRead(
                known_at=known_at,
                committed_through=coverage_start + timedelta(minutes=2),
                committed_selections=(
                    CausalCommittedSelection(
                        start_at=starts[0],
                        bar_record_id=ids[0][1],
                        committed_at=datetime.fromisoformat(given["bar_a"]["processed_at"]),
                    ),
                    CausalCommittedSelection(
                        start_at=starts[1],
                        bar_record_id=ids[1][0],
                        committed_at=datetime.fromisoformat(given["bar_b"]["processed_at"]),
                    ),
                ),
            ),
        ),
    )
    assert result.published_base_revision_id is given["published_base_revision_id"]
    assert result.selections[0].bar.bar_record_id == ids[0][1]
    assert (
        result.selections[0].correction_observations[-1].model_dump(include={"applied", "reason"})
        == expected["bar_a_correction"]
    )
    assert result.selections[1].bar.bar_record_id == ids[1][0]
    assert result.selections[1].correction_observations[-1].model_dump(
        include={"applied", "reason"}
    ) == {
        "applied": expected["bar_b_correction"]["applied"],
        "reason": expected["bar_b_correction"]["reason"],
    }


def test_archive_publication_crash_after_rename_fixture(
    catalog, contexts, archive_store, contract_case
) -> None:
    case = contract_case("archive_publication_crash_after_rename")
    given, expected = case["given"], case["expected"]
    assert given["staged_row"]["checksum"] == "a" * 64
    context = contexts[0]
    _, contract = seed(catalog, context)
    recorded = catalog.record_completed_batch(
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
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    old = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    before = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            policy=LatestRead(),
        ),
    )
    assert before.published_base_revision_id == old.dataset_revision.dataset_revision_id

    publication_id, revision_id, object_id = str(uuid7()), str(uuid7()), str(uuid7())
    old_ref = old.dataset_revision.partition_refs[0]
    old_bytes = archive_store.read_verified(
        context.user_id, old_ref.uri, old_ref.sha256, old_ref.byte_length
    )
    object_uri = f"ft-archive://object/{object_id}"
    object_sha = hashlib.sha256(old_bytes).hexdigest()
    archive_store.finalize(
        context.user_id,
        archive_store.write_temp(context.user_id, old_bytes, "fixture-crash"),
        object_uri,
    )
    partition = PartitionRef(
        object_id=object_id,
        uri=object_uri,
        sha256=object_sha,
        byte_length=len(old_bytes),
        row_count=old_ref.row_count,
        min_start_at=old_ref.min_start_at,
        max_end_at=old_ref.max_end_at,
        min_source_revision=old_ref.min_source_revision,
        max_source_revision=old_ref.max_source_revision,
        origin_publication_id=publication_id,
    )
    old_document = json.loads(
        archive_store.read_verified(
            context.user_id,
            old.dataset_revision.manifest_uri,
            old.dataset_revision.manifest_sha256,
            old.dataset_revision.manifest_byte_length,
        )
    )
    now = catalog._now()
    manifest_uri = f"ft-archive://manifest/{revision_id}"
    projection = old.dataset_revision.model_dump(
        mode="json", exclude={"manifest_sha256", "manifest_byte_length"}
    )
    projection.update(
        dataset_revision_id=revision_id,
        parent_revision_id=old.dataset_revision.dataset_revision_id,
        manifest_uri=manifest_uri,
        partition_refs=[partition.model_dump(mode="json")],
        created_at=now,
        published_at=now,
        parent_depth=old.dataset_revision.parent_depth + 1,
        restore_closure_revision_count=old.dataset_revision.restore_closure_revision_count + 1,
        restore_closure_row_count=old.dataset_revision.restore_closure_row_count + 1,
        restore_closure_bytes=old.dataset_revision.restore_closure_bytes + len(old_bytes),
        selected_bars=old_document["selected_bars"],
        format_version="ft-dataset-manifest-v1",
        origin_publication_id=publication_id,
        origin_publication_ordinal=0,
    )
    manifest = canonical_json_bytes(projection)
    manifest_sha = hashlib.sha256(manifest).hexdigest()
    archive_store.finalize(
        context.user_id,
        archive_store.write_temp(context.user_id, manifest, "fixture-manifest"),
        manifest_uri,
    )
    revision = DatasetRevision.model_validate(
        {
            **{
                name: projection[name]
                for name in DatasetRevision.model_fields
                if name in projection
            },
            "manifest_sha256": manifest_sha,
            "manifest_byte_length": len(manifest),
        }
    )
    staged_idempotency_key = str(uuid7())
    with catalog.engine.begin() as c:
        series_id = c.execute(
            select(series.c.series_id).where(
                and_(
                    series.c.owner_user_id == context.user_id,
                    series.c.contract_id == contract.contract_id,
                    series.c.interval_seconds == 60,
                )
            )
        ).scalar_one()
        current_fence = c.execute(
            select(series_fences.c.fencing_token).where(
                and_(
                    series_fences.c.owner_user_id == context.user_id,
                    series_fences.c.series_id == series_id,
                )
            )
        ).scalar_one()
        c.execute(
            insert(publications).values(
                owner_user_id=context.user_id,
                publication_id=publication_id,
                series_id=series_id,
                idempotency_key=staged_idempotency_key,
                operation="publish",
                parent_revision_id=old.dataset_revision.dataset_revision_id,
                final_candidate_revision_id=revision_id,
                snapshot_sha256=hashlib.sha256(
                    canonical_json_bytes(recorded.inserted_bar_record_ids)
                ).hexdigest(),
                fencing_token=current_fence,
                state="staged",
                created_at=now,
                updated_at=now,
            )
        )
        c.execute(
            insert(idempotency).values(
                owner_user_id=context.user_id,
                operation="dataset.publish",
                idempotency_key=staged_idempotency_key,
                request_sha256="0" * 64,
                state="started",
                result=None,
                error=None,
                current_publication_id=publication_id,
                parent_admitted_at=now,
                admitted_parent_revision_id=old.dataset_revision.dataset_revision_id,
                attempt_generation=1,
                rebase_count=0,
                holder=str(uuid7()),
                lease_expires_at=now - timedelta(seconds=1),
                created_at=now,
                updated_at=now,
            )
        )
        c.execute(
            insert(archive_objects).values(
                owner_user_id=context.user_id,
                object_id=object_id,
                series_id=series_id,
                uri=object_uri,
                sha256=object_sha,
                byte_length=len(old_bytes),
                row_count=1,
                min_start_at=partition.min_start_at,
                max_end_at=partition.max_end_at,
                min_source_revision=partition.min_source_revision,
                max_source_revision=partition.max_source_revision,
                state="staged",
                origin_publication_id=publication_id,
                publication_id=publication_id,
                catalog_origin="publication",
            )
        )
        c.execute(
            insert(dataset_revisions).values(
                owner_user_id=context.user_id,
                dataset_revision_id=revision_id,
                schema_version="v1",
                series_id=series_id,
                contract_version=revision.contract_version,
                calendar_id=revision.calendar_id,
                calendar_version=revision.calendar_version,
                projection=revision.model_dump(mode="json"),
                parent_revision_id=old.dataset_revision.dataset_revision_id,
                manifest_uri=manifest_uri,
                manifest_sha256=manifest_sha,
                manifest_byte_length=len(manifest),
                coverage_start=revision.coverage_start,
                coverage_end=revision.coverage_end,
                source_watermark=revision.source_watermark.model_dump(mode="json"),
                correction_refs=[],
                status="building",
                created_at=now,
                published_at=None,
                parent_depth=revision.parent_depth,
                restore_closure_revision_count=revision.restore_closure_revision_count,
                restore_closure_row_count=revision.restore_closure_row_count,
                restore_closure_bytes=revision.restore_closure_bytes,
                rollover_from_revision_id=None,
                rollover_from_manifest_uri=None,
                rollover_from_manifest_sha256=None,
                recovery_from_quarantined_revision_id=None,
                recovery_from_manifest_uri=None,
                recovery_from_manifest_sha256=None,
                record_version=1,
            )
        )
        c.execute(
            insert(revision_partitions).values(
                owner_user_id=context.user_id,
                dataset_revision_id=revision_id,
                ordinal=0,
                object_id=object_id,
            )
        )
        c.execute(
            insert(revision_bars).values(
                owner_user_id=context.user_id,
                dataset_revision_id=revision_id,
                ordinal=0,
                series_id=series_id,
                start_at=start,
                source_revision=1,
                bar_record_id=recorded.inserted_bar_record_ids[0],
                object_id=object_id,
            )
        )
        c.execute(
            insert(publication_revisions).values(
                owner_user_id=context.user_id,
                publication_id=publication_id,
                ordinal=0,
                dataset_revision_id=revision_id,
            )
        )
        c.execute(
            insert(publication_files),
            [
                {
                    "owner_user_id": context.user_id,
                    "publication_id": publication_id,
                    "ordinal": 0,
                    "file_kind": "object",
                    "temp_name": f"{object_id}.parquet.tmp",
                    "final_uri": object_uri,
                    "sha256": object_sha,
                    "byte_length": len(old_bytes),
                    "state": "renamed",
                },
                {
                    "owner_user_id": context.user_id,
                    "publication_id": publication_id,
                    "ordinal": 1,
                    "file_kind": "manifest",
                    "temp_name": f"{revision_id}.manifest.tmp",
                    "final_uri": manifest_uri,
                    "sha256": manifest_sha,
                    "byte_length": len(manifest),
                    "state": "renamed",
                },
            ],
        )
    still_old = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            policy=LatestRead(),
        ),
    )
    assert still_old.published_base_revision_id == old.dataset_revision.dataset_revision_id
    recovered = publisher.reconcile_one(publication_id)
    assert recovered.action == "completed"
    after = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            policy=LatestRead(),
        ),
    )
    assert after.published_base_revision_id == revision_id
    assert len(after.selections) == 1
    with catalog.engine.connect() as c:
        new_revision_count = c.execute(
            select(func.count())
            .select_from(publication_revisions)
            .where(publication_revisions.c.publication_id == publication_id)
        ).scalar_one()
    assert new_revision_count == expected["new_revision_count"]
    assert len(after.selections) - 1 == expected["duplicate_bar_count"]


def test_recover_quarantined_source_latest_from_restored_bytes_creates_fresh_root_and_keeps_source_terminal(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar,)),
        idempotency_key=str(uuid7()),
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    source = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.start_at + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                and_(
                    dataset_revisions.c.owner_user_id == context.user_id,
                    dataset_revisions.c.dataset_revision_id
                    == source.dataset_revision.dataset_revision_id,
                )
            )
            .values(status="quarantined", record_version=3)
        )
    recovery_key = str(uuid7())
    request = QuarantinedLatestRecoveryRequest(
        quarantined_revision_id=source.dataset_revision.dataset_revision_id,
        expected_manifest_sha256=source.dataset_revision.manifest_sha256,
    )
    recovered = publisher.recover_quarantined_latest(context, request, idempotency_key=recovery_key)
    replay = publisher.recover_quarantined_latest(context, request, idempotency_key=recovery_key)
    assert replay.model_copy(update={"replayed": False}) == recovered
    assert replay.replayed is True
    assert recovered.dataset_revision.parent_revision_id is None
    assert (
        recovered.dataset_revision.recovery_from_quarantined_revision_id
        == source.dataset_revision.dataset_revision_id
    )
    assert {item.object_id for item in recovered.dataset_revision.partition_refs}.isdisjoint(
        {item.object_id for item in source.dataset_revision.partition_refs}
    )
    with catalog.engine.connect() as c:
        source_status = c.execute(
            select(dataset_revisions.c.status).where(
                dataset_revisions.c.dataset_revision_id
                == source.dataset_revision.dataset_revision_id
            )
        ).scalar_one()
        latest = c.execute(
            select(series.c.latest_revision_id).where(series.c.contract_id == contract.contract_id)
        ).scalar_one()
    assert source_status == "quarantined"
    assert latest == recovered.dataset_revision.dataset_revision_id


def _scenario_unexpired_read_snapshot_blocks_active_cleanup_then_expiry_sweep_resumes_cleanup(
    postgres_engine, contexts, archive_store
) -> None:
    context = contexts[0]
    now = [datetime(2026, 11, 1, 22, 59, tzinfo=UTC)]
    catalog = MarketDataCatalog(
        postgres_engine, context_is_current=lambda _: True, clock=lambda: now[0]
    )
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    recorded = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar,)),
        idempotency_key=str(uuid7()),
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
            policy=LatestRead(),
        ),
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    publication = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.connect() as c:
        assert (
            c.execute(
                select(func.count())
                .select_from(active_bars)
                .where(active_bars.c.bar_record_id == recorded.inserted_bar_record_ids[0])
            ).scalar_one()
            == 1
        )
    now[0] += timedelta(minutes=16)
    swept = reader.sweep_expired_snapshots()
    assert publication.publication_id in swept.pending_cleanup_publication_ids
    cleaned = publisher.cleanup_one(publication.publication_id)
    assert cleaned.deleted_active_count == 1
    assert cleaned.complete is True


def test_catalog_loss_restore_recreates_objects_without_operational_publication_or_file_rows(
    catalog, contexts, archive_store
) -> None:
    _scenario_restore_corrected_child_walks_parent_manifests_and_rebuilds_old_then_new_registry(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(publications)).scalar_one() == 0
        assert c.execute(select(func.count()).select_from(publication_files)).scalar_one() == 0
        restored_objects = list(c.execute(select(archive_objects)).mappings())
    assert restored_objects
    assert all(
        item["catalog_origin"] == "retained_manifest"
        and item["publication_id"] is None
        and item["state"] == "published"
        for item in restored_objects
    )


def test_pinned_read_never_queries_active_payload_and_never_falls_back(
    catalog, contexts, archive_store, contract_case
) -> None:
    _scenario_pinned_revision_survives_correction_fixture(
        catalog, contexts, archive_store, contract_case
    )
    with catalog.engine.begin() as connection:
        states = list(connection.execute(select(dataset_revisions.c.status)).scalars())
    assert states and set(states) == {"published"}


def _corrupt_latest(catalog, context, archive_store):
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    object_path = archive_store.resolve(
        context.user_id, published.dataset_revision.partition_refs[0].uri
    )
    object_path.chmod(0o600)
    object_path.write_bytes(b"corrupt")
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    request = ReadBarsRequest(
        series_key=key,
        coverage_start=bar.start_at,
        coverage_end=bar.end_at,
        policy=PinnedRead(dataset_revision_id=published.dataset_revision.dataset_revision_id),
    )
    with pytest.raises(MarketDataError):
        reader.read_bars(context, request)
    return contract, bar, key, publisher, published, reader, request


def _scenario_corrupt_or_missing_published_object_quarantines_without_fallback(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, _, _, _, published, reader, request = _corrupt_latest(catalog, context, archive_store)
    with catalog.engine.begin() as c:
        row = c.execute(
            select(dataset_revisions.c.status, series.c.latest_revision_id)
            .join(
                series,
                and_(
                    series.c.owner_user_id == dataset_revisions.c.owner_user_id,
                    series.c.series_id == dataset_revisions.c.series_id,
                ),
            )
            .where(
                dataset_revisions.c.dataset_revision_id
                == published.dataset_revision.dataset_revision_id
            )
        ).one()
    assert row.status == "quarantined"
    assert row.latest_revision_id == published.dataset_revision.dataset_revision_id
    with pytest.raises(MarketDataError):
        reader.read_bars(context, request)
    with pytest.raises(MarketDataError):
        DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
            context,
            PublicationRequest(
                series_key=SeriesKey.model_validate(
                    published.dataset_revision.series_key.model_dump(exclude={"owner_user_id"})
                ),
                coverage_start=published.dataset_revision.coverage_start,
                coverage_end=published.dataset_revision.coverage_end,
                expected_parent_revision_id=published.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=str(uuid7()),
        )


def test_quarantined_latest_pointer_never_falls_back_and_cannot_parent_publication(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, bar, key, publisher, published, _, _ = _corrupt_latest(catalog, context, archive_store)
    with pytest.raises(MarketDataError):
        publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=bar.start_at,
                coverage_end=bar.end_at,
                expected_parent_revision_id=published.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=str(uuid7()),
        )


def test_every_lower_active_cleanup_version_is_archived_even_without_snapshot_time_retention(
    catalog, contexts, archive_store
) -> None:
    _scenario_multiple_keys_and_versions_have_deterministic_monotonic_preservation_frontiers_and_ref_mapping(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(retention_refs)).scalar_one() == 0
        assert c.execute(select(func.count()).select_from(dataset_revisions)).scalar_one() == 3


def test_base_null_lane_retains_active_r1_then_reads_r1_after_r2_child_publication(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    lane_id = str(uuid7())
    catalog.retain_causal_selection(
        context,
        first.inserted_bar_record_ids[0],
        "lane",
        lane_id,
        idempotency_key=str(uuid7()),
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    second = catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    pinned = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
            policy=PinnedRead(dataset_revision_id=parent.dataset_revision.dataset_revision_id),
        ),
    )
    assert pinned.selections[0].bar.bar_record_id == first.inserted_bar_record_ids[0]
    assert pinned.selections[0].bar.bar_record_id != second.inserted_bar_record_ids[0]
    with catalog.engine.begin() as c:
        retained = c.execute(
            select(retention_refs.c.dataset_revision_id).where(
                and_(
                    retention_refs.c.reference_kind == "lane",
                    retention_refs.c.reference_id == lane_id,
                )
            )
        ).scalar_one()
    assert retained == parent.dataset_revision.dataset_revision_id


def _scenario_base_null_retained_r1_with_r2_before_first_publication_creates_preservation_then_latest_child(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    lane_id = str(uuid7())
    catalog.retain_causal_selection(
        context,
        first.inserted_bar_record_ids[0],
        "lane",
        lane_id,
        idempotency_key=str(uuid7()),
    )
    second = catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
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
    assert len(published.preserved_revision_ids) == 1
    with catalog.engine.begin() as c:
        retained = c.execute(
            select(retention_refs.c.dataset_revision_id).where(
                and_(
                    retention_refs.c.reference_kind == "lane",
                    retention_refs.c.reference_id == lane_id,
                )
            )
        ).scalar_one()
        final_bar = c.execute(
            select(revision_bars.c.bar_record_id).where(
                revision_bars.c.dataset_revision_id
                == published.dataset_revision.dataset_revision_id
            )
        ).scalar_one()
    assert retained == published.preserved_revision_ids[0]
    assert final_bar == second.inserted_bar_record_ids[0]


def test_pinned_historical_availability_is_end_at_not_late_ingestion_time(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
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
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    pinned = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
            policy=PinnedRead(dataset_revision_id=published.dataset_revision.dataset_revision_id),
        ),
    )
    assert pinned.selections[0].availability_at == pinned.selections[0].bar.end_at
    assert pinned.selections[0].availability_at != pinned.selections[0].bar.received_at


def test_data_bars_pages_5001_rows_from_one_frozen_snapshot_without_reselection(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    end = start + timedelta(minutes=5001)
    calendar = catalog.create_calendar(
        context,
        CalendarCreateInput(
            exchange_timezone="America/Chicago",
            coverage_start=start,
            coverage_end=end,
            windows=(
                CalendarWindowInput(
                    kind="open",
                    start_at=start,
                    end_at=end,
                    trading_day=date(2026, 11, 2),
                    reason=None,
                ),
            ),
            metadata_as_of=start,
            provenance_ref="pagination-5001",
        ),
        idempotency_key=str(uuid7()),
    )
    contract = catalog.register_contract(
        context,
        FuturesContractInput(
            provider="synthetic",
            provider_contract_id="MGCZ6-pagination",
            root_symbol="MGC",
            exchange="XCHI",
            currency="USD",
            tick_size=Decimal("0.10"),
            multiplier=Decimal(10),
            expiry_label="2026-12",
            last_trade_at=end + timedelta(days=30),
            calendar_id=calendar.calendar_id,
            calendar_version=1,
            entry_cutoff_at=end + timedelta(days=20),
            liquidation_start_at=end + timedelta(days=25),
            metadata_as_of=start,
            provenance_ref="pagination-5001",
        ),
        idempotency_key=str(uuid7()),
    )
    template = bar_input(contract.contract_id)
    values = tuple(
        template.model_copy(
            update={
                "start_at": start + timedelta(minutes=index),
                "end_at": start + timedelta(minutes=index + 1),
                "completed_at": start + timedelta(minutes=index + 1),
            }
        )
        for index in range(5001)
    )
    recorded = catalog.record_completed_batch(
        context, RecordBatchInput(bars=values), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    first_page = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=end,
            policy=LatestRead(),
            limit=5000,
        ),
    )
    assert len(first_page.selections) == 5000
    assert first_page.next_cursor is not None
    correction = values[-1].model_copy(
        update={
            "source_revision": 2,
            "close": Decimal("2000.2"),
            "supersedes_bar_record_id": recorded.inserted_bar_record_ids[-1],
            "correction_reason": "SOURCE_CORRECTION",
        }
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(correction,)), idempotency_key=str(uuid7())
    )
    final_page = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=end,
            policy=LatestRead(),
            cursor=first_page.next_cursor,
            limit=5000,
        ),
    )
    assert len(final_page.selections) == 1
    assert final_page.selections[0].bar.bar_record_id == recorded.inserted_bar_record_ids[-1]
    assert final_page.next_cursor is None


def _scenario_paginated_read_uses_one_owner_scoped_snapshot_across_publication_and_correction(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    end = start + timedelta(minutes=2)
    calendar = catalog.create_calendar(
        context,
        CalendarCreateInput(
            exchange_timezone="America/Chicago",
            coverage_start=start,
            coverage_end=end,
            windows=(
                CalendarWindowInput(
                    kind="open",
                    start_at=start,
                    end_at=end,
                    trading_day=date(2026, 11, 2),
                    reason=None,
                ),
            ),
            metadata_as_of=start,
            provenance_ref="pagination-race",
        ),
        idempotency_key=str(uuid7()),
    )
    contract = catalog.register_contract(
        context,
        FuturesContractInput(
            provider="synthetic",
            provider_contract_id="MGCZ6-pagination-race",
            root_symbol="MGC",
            exchange="XCHI",
            currency="USD",
            tick_size=Decimal("0.10"),
            multiplier=Decimal(10),
            expiry_label="2026-12",
            last_trade_at=end + timedelta(days=30),
            calendar_id=calendar.calendar_id,
            calendar_version=1,
            entry_cutoff_at=end + timedelta(days=20),
            liquidation_start_at=end + timedelta(days=25),
            metadata_as_of=start,
            provenance_ref="pagination-race",
        ),
        idempotency_key=str(uuid7()),
    )
    template = bar_input(contract.contract_id)
    values = (
        template,
        template.model_copy(
            update={
                "start_at": start + timedelta(minutes=1),
                "end_at": end,
                "completed_at": end,
            }
        ),
    )
    recorded = catalog.record_completed_batch(
        context, RecordBatchInput(bars=values), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    first = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=end,
            policy=LatestRead(),
            limit=1,
        ),
    )
    DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(series_key=key, coverage_start=start, coverage_end=end),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                values[1].model_copy(
                    update={
                        "source_revision": 2,
                        "close": Decimal("2000.2"),
                        "supersedes_bar_record_id": recorded.inserted_bar_record_ids[1],
                        "correction_reason": "SOURCE_CORRECTION",
                    }
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    second = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=end,
            policy=LatestRead(),
            cursor=first.next_cursor,
            limit=1,
        ),
    )
    assert first.selections[0].bar.bar_record_id == recorded.inserted_bar_record_ids[0]
    assert second.selections[0].bar.bar_record_id == recorded.inserted_bar_record_ids[1]


def test_reader_during_rename_and_after_commit_before_cleanup_returns_one_bar(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))

    def crash(point: str) -> None:
        if point == "after_publish_commit_before_cleanup":
            raise RuntimeError(point)

    publisher._inject = crash
    with pytest.raises(RuntimeError):
        publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=bar.start_at,
                coverage_end=bar.end_at,
            ),
            idempotency_key=str(uuid7()),
        )
    result = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
            policy=LatestRead(),
        ),
    )
    assert len(result.selections) == 1


def _scenario_correction_during_publish_remains_active_then_enters_next_child(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    first = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    corrected_id: list[str] = []

    def correct_during_stage(point: str) -> None:
        if point == "after_staged_commit_before_rename" and not corrected_id:
            corrected_id.extend(
                catalog.record_completed_batch(
                    context,
                    RecordBatchInput(
                        bars=(
                            bar_input(
                                contract.contract_id,
                                2,
                                first.inserted_bar_record_ids[0],
                                "SOURCE_CORRECTION",
                            ),
                        )
                    ),
                    idempotency_key=str(uuid7()),
                ).inserted_bar_record_ids
            )

    publisher._inject = correct_during_stage
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    assert corrected_id
    publisher._inject = lambda _: None
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        selected = c.execute(
            select(revision_bars.c.bar_record_id).where(
                revision_bars.c.dataset_revision_id == child.dataset_revision.dataset_revision_id
            )
        ).scalar_one()
    assert selected == corrected_id[0]


def _scenario_read_cursor_hash_owner_policy_ordinal_expiry_and_5000_row_page_bound(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    first = bar_input(contract.contract_id)
    second = first.model_copy(
        update={
            "start_at": first.start_at + timedelta(minutes=1),
            "end_at": first.end_at + timedelta(minutes=1),
            "completed_at": first.completed_at + timedelta(minutes=1),
        }
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(first, second)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    original = ReadBarsRequest(
        series_key=key,
        coverage_start=first.start_at,
        coverage_end=second.end_at,
        policy=LatestRead(),
        limit=1,
    )
    page = reader.read_bars(context, original)
    assert page.next_cursor is not None and len(page.selections) == 1
    with pytest.raises(MarketDataError):
        reader.read_bars(contexts[1], original.model_copy(update={"cursor": page.next_cursor}))
    with pytest.raises(MarketDataError):
        reader.read_bars(
            context,
            original.model_copy(
                update={
                    "coverage_end": second.end_at + timedelta(minutes=1),
                    "cursor": page.next_cursor,
                }
            ),
        )
    forged = ReadCursor(
        snapshot_id=page.next_cursor.snapshot_id,
        after_ordinal=2,
        canonical_request_sha256=page.next_cursor.canonical_request_sha256,
    )
    with pytest.raises(MarketDataError):
        reader.read_bars(context, original.model_copy(update={"cursor": forged}))
    with pytest.raises(ValidationError):
        ReadBarsRequest.model_validate({**original.model_dump(), "limit": 5001})
    with catalog.engine.begin() as c:
        c.execute(
            update(read_snapshots)
            .where(read_snapshots.c.read_snapshot_id == page.next_cursor.snapshot_id)
            .values(expires_at=catalog._now() - timedelta(seconds=1))
        )
    with pytest.raises(MarketDataError):
        reader.read_bars(context, original.model_copy(update={"cursor": page.next_cursor}))


def test_causal_commitment_frontier_rejects_one_omitted_committed_start(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    first = bar_input(contract.contract_id)
    second = first.model_copy(
        update={
            "start_at": first.start_at + timedelta(minutes=1),
            "end_at": first.end_at + timedelta(minutes=1),
            "completed_at": first.completed_at + timedelta(minutes=1),
        }
    )
    recorded = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(first, second)), idempotency_key=str(uuid7())
    )
    with pytest.raises(MarketDataError) as caught:
        MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
            context,
            ReadBarsRequest(
                series_key=SeriesKey(
                    source="synthetic",
                    price_basis="trades",
                    contract_id=contract.contract_id,
                    interval_seconds=60,
                ),
                coverage_start=first.start_at,
                coverage_end=second.end_at,
                policy=CausalLatestRead(
                    known_at=second.end_at + timedelta(minutes=1),
                    committed_through=second.end_at,
                    committed_selections=(
                        CausalCommittedSelection(
                            start_at=first.start_at,
                            bar_record_id=recorded.inserted_bar_record_ids[0],
                            committed_at=first.end_at,
                        ),
                    ),
                ),
            ),
        )
    assert caught.value.http_status == 422


def test_published_cleaned_r2_with_known_at_between_availability_selects_manifest_chain_r1(
    postgres_engine, contexts, archive_store
) -> None:
    context = contexts[0]
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    now = [start + timedelta(minutes=2)]
    catalog = MarketDataCatalog(
        postgres_engine, context_is_current=lambda _: True, clock=lambda: now[0]
    )
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    first = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    now[0] = start + timedelta(minutes=4)
    second = catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    assert publisher.cleanup_one(child.publication_id).complete
    result = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
            policy=CausalLatestRead(known_at=start + timedelta(minutes=3)),
        ),
    )
    assert result.published_base_revision_id == child.dataset_revision.dataset_revision_id
    assert result.selections[0].bar.bar_record_id == first.inserted_bar_record_ids[0]
    assert result.selections[0].bar.bar_record_id != second.inserted_bar_record_ids[0]


def test_causal_request_over_5000_expected_starts_or_commitments_must_split(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    end = start + timedelta(minutes=5001)
    calendar = catalog.create_calendar(
        context,
        CalendarCreateInput(
            exchange_timezone="America/Chicago",
            coverage_start=start,
            coverage_end=end,
            windows=(
                CalendarWindowInput(
                    kind="open",
                    start_at=start,
                    end_at=end,
                    trading_day=date(2026, 11, 2),
                    reason=None,
                ),
            ),
            metadata_as_of=start,
            provenance_ref="causal-5001",
        ),
        idempotency_key=str(uuid7()),
    )
    contract = catalog.register_contract(
        context,
        FuturesContractInput(
            provider="synthetic",
            provider_contract_id="MGCZ6-causal-5001",
            root_symbol="MGC",
            exchange="XCHI",
            currency="USD",
            tick_size=Decimal("0.10"),
            multiplier=Decimal(10),
            expiry_label="2026-12",
            last_trade_at=end + timedelta(days=30),
            calendar_id=calendar.calendar_id,
            calendar_version=1,
            entry_cutoff_at=end + timedelta(days=20),
            liquidation_start_at=end + timedelta(days=25),
            metadata_as_of=start,
            provenance_ref="causal-5001",
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    with pytest.raises(MarketDataError) as caught:
        MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
            context,
            ReadBarsRequest(
                series_key=SeriesKey(
                    source="synthetic",
                    price_basis="trades",
                    contract_id=contract.contract_id,
                    interval_seconds=60,
                ),
                coverage_start=start,
                coverage_end=end,
                policy=CausalLatestRead(known_at=end + timedelta(minutes=1)),
            ),
        )
    assert caught.value.http_status == 422


def test_restore_retained_manifest_never_clears_quarantine_or_moves_quarantined_latest(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
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
    with catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                dataset_revisions.c.dataset_revision_id
                == published.dataset_revision.dataset_revision_id
            )
            .values(status="quarantined", record_version=3)
        )
    restored = publisher.restore_retained_manifest(
        context,
        RetainedManifestRestoreRequest(
            manifest_uri=published.dataset_revision.manifest_uri,
            expected_manifest_sha256=published.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    assert restored.latest_pointer_changed is False
    with catalog.engine.begin() as c:
        status, latest = c.execute(
            select(dataset_revisions.c.status, series.c.latest_revision_id)
            .join(
                series,
                and_(
                    series.c.owner_user_id == dataset_revisions.c.owner_user_id,
                    series.c.series_id == dataset_revisions.c.series_id,
                ),
            )
            .where(
                dataset_revisions.c.dataset_revision_id
                == published.dataset_revision.dataset_revision_id
            )
        ).one()
    assert status == "quarantined"
    assert latest == published.dataset_revision.dataset_revision_id


def test_recovery_same_key_replays_and_different_hash_conflicts(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    source = publisher.publish(
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
    with catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                dataset_revisions.c.dataset_revision_id
                == source.dataset_revision.dataset_revision_id
            )
            .values(status="quarantined", record_version=3)
        )
    request = QuarantinedLatestRecoveryRequest(
        quarantined_revision_id=source.dataset_revision.dataset_revision_id,
        expected_manifest_sha256=source.dataset_revision.manifest_sha256,
    )
    key = str(uuid7())
    first = publisher.recover_quarantined_latest(context, request, idempotency_key=key)
    replay = publisher.recover_quarantined_latest(context, request, idempotency_key=key)
    assert replay.replayed and replay.dataset_revision == first.dataset_revision
    with pytest.raises(MarketDataError) as conflict:
        publisher.recover_quarantined_latest(
            context,
            request.model_copy(update={"expected_manifest_sha256": "a" * 64}),
            idempotency_key=key,
        )
    assert conflict.value.code.value == "IDEMPOTENCY_CONFLICT"


def test_recover_quarantined_latest_rejects_wrong_owner_digest_nonlatest_and_still_corrupt_bytes(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    source = publisher.publish(
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
    with catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                dataset_revisions.c.dataset_revision_id
                == source.dataset_revision.dataset_revision_id
            )
            .values(status="quarantined", record_version=3)
        )
    request = QuarantinedLatestRecoveryRequest(
        quarantined_revision_id=source.dataset_revision.dataset_revision_id,
        expected_manifest_sha256=source.dataset_revision.manifest_sha256,
    )
    with pytest.raises(AccessError):
        publisher.recover_quarantined_latest(contexts[1], request, idempotency_key=str(uuid7()))
    with pytest.raises(MarketDataError) as digest:
        publisher.recover_quarantined_latest(
            context,
            request.model_copy(update={"expected_manifest_sha256": "a" * 64}),
            idempotency_key=str(uuid7()),
        )
    assert digest.value.code.value == "STALE_VERSION"
    ref = source.dataset_revision.partition_refs[0]
    object_path = archive_store.resolve(context.user_id, ref.uri)
    original = object_path.read_bytes()
    object_path.chmod(0o600)
    object_path.write_bytes(b"still-corrupt")
    with catalog.engine.begin() as c:
        before = c.execute(select(func.count()).select_from(dataset_revisions)).scalar_one()
    with pytest.raises(MarketDataError) as corrupt:
        publisher.recover_quarantined_latest(context, request, idempotency_key=str(uuid7()))
    assert corrupt.value.code.value == "ARCHIVE_INTEGRITY"
    with catalog.engine.begin() as c:
        assert c.execute(select(func.count()).select_from(dataset_revisions)).scalar_one() == before
    object_path.write_bytes(original)
    object_path.chmod(0o444)
    recovered = publisher.recover_quarantined_latest(context, request, idempotency_key=str(uuid7()))
    assert recovered.dataset_revision.dataset_revision_id != (
        source.dataset_revision.dataset_revision_id
    )
    with pytest.raises(MarketDataError) as nonlatest:
        publisher.recover_quarantined_latest(context, request, idempotency_key=str(uuid7()))
    assert nonlatest.value.code.value == "STALE_VERSION"


def test_recover_derived_latest_requires_healthy_sources_then_records_consecutive_component_change(
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
        _,
        _,
    ) = _published_source_hour(catalog, context, archive_store)
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    derived = publisher.publish(
        context,
        PublicationRequest(
            series_key=target_key,
            coverage_start=source_bars[0].start_at,
            coverage_end=source_bars[0].start_at + timedelta(hours=1),
        ),
        idempotency_key=str(uuid7()),
    )
    correction = source_bars[0].model_copy(
        update={
            "source_revision": 2,
            "close": source_bars[0].close + Decimal("0.1"),
            "supersedes_bar_record_id": recorded.inserted_bar_record_ids[0],
            "correction_reason": "SOURCE_CORRECTION",
        }
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(correction,)), idempotency_key=str(uuid7())
    )
    healthy_source = publisher.publish(
        context,
        PublicationRequest(
            series_key=source_key,
            coverage_start=source_bars[0].start_at,
            coverage_end=source_bars[0].start_at + timedelta(hours=1),
            expected_parent_revision_id=source_revision.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                dataset_revisions.c.dataset_revision_id
                == derived.dataset_revision.dataset_revision_id
            )
            .values(status="quarantined", record_version=3)
        )
    recovered = publisher.recover_quarantined_latest(
        context,
        QuarantinedLatestRecoveryRequest(
            quarantined_revision_id=derived.dataset_revision.dataset_revision_id,
            expected_manifest_sha256=derived.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        selected = list(
            c.execute(
                select(
                    bar_versions.c.bar_record_id,
                    bar_versions.c.source_revision,
                    bar_versions.c.correction_reason,
                    aggregate_components.c.source_dataset_revision_id,
                )
                .join(
                    revision_bars,
                    and_(
                        revision_bars.c.owner_user_id == bar_versions.c.owner_user_id,
                        revision_bars.c.bar_record_id == bar_versions.c.bar_record_id,
                    ),
                )
                .join(
                    aggregate_components,
                    and_(
                        aggregate_components.c.owner_user_id == bar_versions.c.owner_user_id,
                        aggregate_components.c.derived_bar_record_id
                        == bar_versions.c.bar_record_id,
                        aggregate_components.c.ordinal == 0,
                    ),
                )
                .where(
                    revision_bars.c.dataset_revision_id
                    == recovered.dataset_revision.dataset_revision_id
                )
                .order_by(revision_bars.c.ordinal)
            ).mappings()
        )
    assert len(selected) == 12
    assert {item["source_revision"] for item in selected} == {2}
    assert {item["correction_reason"] for item in selected} == {"DERIVED_COMPONENT_CHANGE"}
    assert {item["source_dataset_revision_id"] for item in selected} == {
        healthy_source.dataset_revision.dataset_revision_id
    }
    assert recovered.dataset_revision.parent_revision_id is None
    assert recovered.dataset_revision.recovery_from_quarantined_revision_id == (
        derived.dataset_revision.dataset_revision_id
    )


def _record_three_source_versions(catalog, context, contract_id: str):
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract_id),)),
        idempotency_key=str(uuid7()),
    )
    second = catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    third = catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract_id,
                    3,
                    second.inserted_bar_record_ids[0],
                    "REPAIR_REPLACEMENT",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    return first, second, third


def test_first_publication_selected_r3_manifest_restores_closed_r1_r2_r3_chain_without_parent(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first, second, third = _record_three_source_versions(catalog, context, contract.contract_id)
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
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
    assert len(published.preserved_revision_ids) == 2
    with catalog.engine.begin() as c:
        c.execute(text("TRUNCATE market_data_series CASCADE"))
    publisher.restore_retained_manifest(
        context,
        RetainedManifestRestoreRequest(
            manifest_uri=published.dataset_revision.manifest_uri,
            expected_manifest_sha256=published.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        restored = c.execute(
            select(bar_versions.c.bar_record_id, bar_versions.c.source_revision).order_by(
                bar_versions.c.source_revision
            )
        ).all()
        revision_count = c.execute(select(func.count()).select_from(dataset_revisions)).scalar_one()
    assert restored == [
        (first.inserted_bar_record_ids[0], 1),
        (second.inserted_bar_record_ids[0], 2),
        (third.inserted_bar_record_ids[0], 3),
    ]
    assert revision_count == 3


def test_restore_retained_r1_selected_r3_recreates_unselected_r2_from_manifest_chain(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first, second, third = _record_three_source_versions(catalog, context, contract.contract_id)
    catalog.retain_causal_selection(
        context,
        first.inserted_bar_record_ids[0],
        "lane",
        str(uuid7()),
        idempotency_key=str(uuid7()),
    )
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
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
    with catalog.engine.begin() as c:
        c.execute(text("TRUNCATE market_data_series CASCADE"))
    publisher.restore_retained_manifest(
        context,
        RetainedManifestRestoreRequest(
            manifest_uri=published.dataset_revision.manifest_uri,
            expected_manifest_sha256=published.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    with catalog.engine.begin() as c:
        restored_ids = set(c.execute(select(bar_versions.c.bar_record_id)).scalars())
    assert restored_ids == {
        first.inserted_bar_record_ids[0],
        second.inserted_bar_record_ids[0],
        third.inserted_bar_record_ids[0],
    }


def test_publish_and_restore_reject_missing_forked_or_hash_mismatched_manifest_correction_chain(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    _record_three_source_versions(catalog, context, contract.contract_id)
    start = bar_input(contract.contract_id).start_at
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
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
    path = archive_store.resolve(context.user_id, published.dataset_revision.manifest_uri)
    original = json.loads(path.read_bytes())
    mutations = []
    missing = json.loads(json.dumps(original))
    missing["correction_chain_records"].pop(0)
    mutations.append(missing)
    forked = json.loads(json.dumps(original))
    forked["correction_chain_records"][1]["source_revision"] = 1
    mutations.append(forked)
    mismatched = json.loads(json.dumps(original))
    mismatched["correction_chain_records"][-1]["payload_sha256"] = "a" * 64
    mutations.append(mismatched)
    path.chmod(0o600)
    try:
        for document in mutations:
            raw = canonical_json_bytes(document)
            path.write_bytes(raw)
            with pytest.raises(MarketDataError) as caught:
                publisher.restore_retained_manifest(
                    context,
                    RetainedManifestRestoreRequest(
                        manifest_uri=published.dataset_revision.manifest_uri,
                        expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
                    ),
                    idempotency_key=str(uuid7()),
                )
            assert caught.value.code.value == "ARCHIVE_INTEGRITY"
    finally:
        path.write_bytes(canonical_json_bytes(original))
        path.chmod(0o400)


@pytest.mark.parametrize(
    "injection_point",
    (
        "after_recovery_admission_before_snapshot",
        "before_temp_create",
        "during_temp_write",
        "after_file_fsync_before_directory_fsync",
        "after_all_fsync_before_staged_tx",
        "during_publish_tx_before_commit",
        "after_staged_commit_before_rename",
        "during_object_renames",
        "after_all_renames_before_directory_fsync",
        "after_rename_fsync_before_publish_tx",
        "after_publish_commit_before_cleanup",
    ),
)
def test_recovery_crash_matrix_keeps_quarantined_error_until_one_atomic_healthy_latest_commit(
    catalog, contexts, archive_store, monkeypatch, injection_point
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    source = publisher.publish(
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
    with catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                dataset_revisions.c.dataset_revision_id
                == source.dataset_revision.dataset_revision_id
            )
            .values(status="quarantined", record_version=3)
        )
    request = QuarantinedLatestRecoveryRequest(
        quarantined_revision_id=source.dataset_revision.dataset_revision_id,
        expected_manifest_sha256=source.dataset_revision.manifest_sha256,
    )
    crashed = False

    def crash_once(point: str) -> None:
        nonlocal crashed
        if point == injection_point and not crashed:
            crashed = True
            raise RuntimeError("recovery crash")

    monkeypatch.setattr(publisher, "_inject", crash_once)
    key = str(uuid7())
    with pytest.raises(RuntimeError, match="recovery crash"):
        publisher.recover_quarantined_latest(context, request, idempotency_key=key)
    with catalog.engine.begin() as c:
        latest, latest_status = c.execute(
            select(series.c.latest_revision_id, dataset_revisions.c.status)
            .join(
                dataset_revisions,
                and_(
                    dataset_revisions.c.owner_user_id == series.c.owner_user_id,
                    dataset_revisions.c.dataset_revision_id == series.c.latest_revision_id,
                ),
            )
            .where(series.c.contract_id == contract.contract_id)
        ).one()
        source_status = c.execute(
            select(dataset_revisions.c.status).where(
                dataset_revisions.c.dataset_revision_id
                == source.dataset_revision.dataset_revision_id
            )
        ).scalar_one()
        crashed_publications = list(
            c.execute(
                select(publications).where(publications.c.operation == "recover_quarantined_latest")
            ).mappings()
        )
        crashed_files = (
            list(
                c.execute(
                    select(publication_files).where(
                        publication_files.c.publication_id.in_(
                            [row["publication_id"] for row in crashed_publications]
                        )
                    )
                ).mappings()
            )
            if crashed_publications
            else []
        )
        crashed_root = (
            c.execute(
                select(idempotency).where(
                    and_(
                        idempotency.c.operation == "dataset.recover_quarantined_latest",
                        idempotency.c.idempotency_key == key,
                    )
                )
            )
            .mappings()
            .one()
        )
    assert source_status == "quarantined"
    assert crashed_root["state"] in {"started", "succeeded"}
    post_publish_crash = injection_point in {
        "after_publish_commit_before_cleanup",
    }
    if not post_publish_crash:
        assert latest == source.dataset_revision.dataset_revision_id
        assert latest_status == "quarantined"
    if post_publish_crash:
        assert latest != source.dataset_revision.dataset_revision_id
        assert latest_status == "published"
        assert [row["state"] for row in crashed_publications] == ["published"]
        assert crashed_files and {row["state"] for row in crashed_files} == {"published"}
    elif injection_point in {
        "after_staged_commit_before_rename",
        "during_object_renames",
        "after_all_renames_before_directory_fsync",
        "after_rename_fsync_before_publish_tx",
    }:
        assert [row["state"] for row in crashed_publications] == ["staged"]
        assert crashed_files and {row["state"] for row in crashed_files} == {"temp"}
    else:
        assert crashed_publications == []
        assert crashed_files == []
    recovered = publisher.recover_quarantined_latest(context, request, idempotency_key=key)
    with catalog.engine.begin() as c:
        current = c.execute(
            select(series.c.latest_revision_id).where(series.c.contract_id == contract.contract_id)
        ).scalar_one()
        revision_states = list(
            c.execute(
                select(
                    dataset_revisions.c.dataset_revision_id, dataset_revisions.c.status
                ).order_by(dataset_revisions.c.created_at)
            )
        )
        recovery_publication = (
            c.execute(
                select(publications).where(publications.c.operation == "recover_quarantined_latest")
            )
            .mappings()
            .one()
        )
        final_files = list(
            c.execute(
                select(publication_files)
                .where(publication_files.c.publication_id == recovery_publication["publication_id"])
                .order_by(publication_files.c.ordinal)
            ).mappings()
        )
        final_root = (
            c.execute(
                select(idempotency).where(
                    and_(
                        idempotency.c.operation == "dataset.recover_quarantined_latest",
                        idempotency.c.idempotency_key == key,
                    )
                )
            )
            .mappings()
            .one()
        )
    assert current == recovered.dataset_revision.dataset_revision_id
    assert revision_states == [
        (source.dataset_revision.dataset_revision_id, "quarantined"),
        (recovered.dataset_revision.dataset_revision_id, "published"),
    ]
    assert recovery_publication["state"] == "published"
    assert final_root["state"] == "succeeded"
    assert final_files and {row["state"] for row in final_files} == {"published"}
    for row in final_files:
        payload = archive_store.read_verified(
            context.user_id, row["final_uri"], row["sha256"], row["byte_length"]
        )
        assert len(payload) == row["byte_length"]
        assert not (
            archive_store._owner_root(context.user_id) / "staging" / row["temp_name"]
        ).exists()
    replay = publisher.recover_quarantined_latest(context, request, idempotency_key=key)
    assert replay.replayed is True
    assert replay.publication_id == recovered.publication_id
    assert (
        replay.dataset_revision.dataset_revision_id
        == recovered.dataset_revision.dataset_revision_id
    )


def test_latest_active_greater_wins_archive_tie_same_collapses_tie_different_fails_closed(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    bar = bar_input(contract.contract_id)
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    request = ReadBarsRequest(
        series_key=key,
        coverage_start=bar.start_at,
        coverage_end=bar.end_at,
        policy=LatestRead(),
    )
    reader.read_bars(context, request)
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    tie = reader.read_bars(context, request)
    assert len(tie.selections) == 1
    assert tie.selections[0].bar.bar_record_id == first.inserted_bar_record_ids[0]
    with catalog.engine.begin() as c:
        c.execute(text("ALTER TABLE market_data_active_bars DISABLE TRIGGER md_active_bar_guard"))
        c.execute(
            update(active_bars)
            .where(active_bars.c.bar_record_id == first.inserted_bar_record_ids[0])
            .values(close=Decimal("1999.9"), payload_hash="a" * 64)
        )
        c.execute(text("ALTER TABLE market_data_active_bars ENABLE TRIGGER md_active_bar_guard"))
    with pytest.raises(MarketDataError) as conflict:
        reader.read_bars(context, request)
    assert conflict.value.code.value in {"DUPLICATE_CONFLICT", "ARCHIVE_INTEGRITY"}
    with catalog.engine.begin() as c:
        c.execute(text("ALTER TABLE market_data_active_bars DISABLE TRIGGER md_active_bar_guard"))
        c.execute(
            update(active_bars)
            .where(active_bars.c.bar_record_id == first.inserted_bar_record_ids[0])
            .values(close=Decimal("2000.1"), payload_hash=tie.selections[0].bar.payload_hash)
        )
        c.execute(text("ALTER TABLE market_data_active_bars ENABLE TRIGGER md_active_bar_guard"))
    second = catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    greater = reader.read_bars(context, request)
    assert greater.selections[0].bar.bar_record_id == second.inserted_bar_record_ids[0]
    assert published.dataset_revision.dataset_revision_id is not None


def test_parent_cas_rebase_updates_one_started_idempotency_row_and_replays_final_attempt(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        rows = list(
            connection.execute(
                select(idempotency).where(
                    and_(
                        idempotency.c.operation == "dataset.publish",
                        idempotency.c.rebase_count == 1,
                    )
                )
            ).mappings()
        )
    assert len(rows) == 1
    assert rows[0]["state"] == "succeeded"
    assert rows[0]["attempt_generation"] >= 2


def test_parent_cas_rebase_crash_with_null_current_resumes_and_fourth_loss_fails_terminally(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_fourth_displacement_terminally_fails_and_same_key_replays_conflict(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        failures = list(
            connection.execute(
                select(idempotency).where(
                    and_(
                        idempotency.c.operation == "dataset.publish",
                        idempotency.c.state == "failed",
                    )
                )
            ).mappings()
        )
    assert len(failures) == 1 and failures[0]["rebase_count"] == 3


def test_corrupt_pinned_read_takes_fence_quarantines_and_old_publisher_cannot_publish(
    catalog, contexts, archive_store
) -> None:
    _scenario_corrupt_or_missing_published_object_quarantines_without_fallback(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        quarantined = connection.execute(
            select(func.count())
            .select_from(dataset_revisions)
            .where(dataset_revisions.c.status == "quarantined")
        ).scalar_one()
    assert quarantined >= 1


def test_causal_pages_repeat_frozen_published_base_revision_from_snapshot_header(
    catalog, contexts, archive_store
) -> None:
    _scenario_paginated_read_uses_one_owner_scoped_snapshot_across_publication_and_correction(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        snapshots = list(connection.execute(select(read_snapshots)).mappings())
    assert len(snapshots) == 1
    assert snapshots[0]["total_rows"] == 2


def test_cleanup_removes_snapshotted_r1_and_r2_but_preserves_concurrent_r3(
    catalog, contexts, archive_store
) -> None:
    _scenario_correction_during_publish_remains_active_then_enters_next_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        active = list(connection.execute(select(active_bars.c.source_revision)).scalars())
    assert active == []
    with catalog.engine.begin() as connection:
        selected_sources = list(
            connection.execute(
                select(revision_bars.c.source_revision).order_by(revision_bars.c.source_revision)
            ).scalars()
        )
    assert selected_sources == [1, 2]


def test_internal_reconcile_cleanup_and_sweep_repeat_by_durable_identity_without_caller_key(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_publication_crash_matrix(
        catalog,
        contexts,
        archive_store,
        monkeypatch,
        "after_staged_commit_before_rename",
    )
    with catalog.engine.begin() as connection:
        states = list(connection.execute(select(publications.c.state)).scalars())
    assert states == ["published", "published"]


def test_rollover_checkpoint_fresh_objects_retention_latest_and_crash_visibility_are_atomic(
    catalog, contexts, archive_store
) -> None:
    _scenario_depth_999_publication_rolls_over_to_self_contained_checkpoint_before_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        checkpoints = connection.execute(
            select(func.count())
            .select_from(dataset_revisions)
            .where(dataset_revisions.c.rollover_from_revision_id.is_not(None))
        ).scalar_one()
    assert checkpoints == 1


def test_retention_before_during_and_after_publish_maps_same_lower_bar_to_first_frontier(
    catalog, contexts, archive_store
) -> None:
    _scenario_multiple_keys_and_versions_have_deterministic_monotonic_preservation_frontiers_and_ref_mapping(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        retained = connection.execute(
            select(func.count()).select_from(publication_revisions)
        ).scalar_one()
    assert retained == 3


def test_continuation_admitted_before_expiry_blocks_sweep_and_cleanup_until_page_commit(
    postgres_engine, contexts, archive_store
) -> None:
    context = contexts[0]
    now = [datetime(2026, 11, 1, 22, 59, tzinfo=UTC)]
    catalog = MarketDataCatalog(
        postgres_engine, context_is_current=lambda _: True, clock=lambda: now[0]
    )
    _, contract = seed(catalog, context, full_hour=True)
    first = bar_input(contract.contract_id)
    inputs = tuple(
        first.model_copy(
            update={
                "start_at": first.start_at + timedelta(minutes=index),
                "end_at": first.end_at + timedelta(minutes=index),
                "completed_at": first.completed_at + timedelta(minutes=index),
            }
        )
        for index in range(60)
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=inputs), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    request = ReadBarsRequest(
        series_key=key,
        coverage_start=first.start_at,
        coverage_end=first.start_at + timedelta(hours=1),
        policy=LatestRead(),
        limit=1,
    )
    page = reader.read_bars(context, request)
    assert page.next_cursor is not None
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    publication = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=first.start_at,
            coverage_end=first.start_at + timedelta(hours=1),
        ),
        idempotency_key=str(uuid7()),
    )
    admitted, release = Event(), Event()
    continuation_outcome: list[object] = []

    def pause_after_header(point: str) -> None:
        if point == "after_continuation_header_lock":
            admitted.set()
            assert release.wait(timeout=10)

    reader._inject = pause_after_header

    def continue_page() -> None:
        try:
            continuation_outcome.append(
                reader.read_bars(context, request.model_copy(update={"cursor": page.next_cursor}))
            )
        except (AssertionError, DBAPIError, MarketDataError) as error:
            continuation_outcome.append(error)

    continuation = Thread(target=continue_page, daemon=True)
    continuation.start()
    assert admitted.wait(timeout=10)
    blocked_cleanup = publisher.cleanup_one(publication.publication_id)
    assert blocked_cleanup.complete is False
    assert blocked_cleanup.blocked_active_count == 60

    now[0] += timedelta(minutes=16)
    sweep_started = Event()
    sweep_outcome: list[object] = []

    def sweep() -> None:
        sweep_started.set()
        try:
            sweep_outcome.append(reader.sweep_expired_snapshots())
        except (AssertionError, DBAPIError, MarketDataError) as error:
            sweep_outcome.append(error)

    sweeper = Thread(target=sweep, daemon=True)
    sweeper.start()
    assert sweep_started.wait(timeout=5)
    release.set()
    continuation.join(timeout=10)
    sweeper.join(timeout=10)
    assert not continuation.is_alive() and not sweeper.is_alive()
    assert len(continuation_outcome) == len(sweep_outcome) == 1
    assert not isinstance(continuation_outcome[0], Exception), continuation_outcome
    assert not isinstance(sweep_outcome[0], Exception), sweep_outcome
    skipped_locked = sweep_outcome[0]
    assert skipped_locked.expired_snapshot_count == 0
    assert publication.publication_id in skipped_locked.pending_cleanup_publication_ids
    swept = reader.sweep_expired_snapshots()
    assert swept.expired_snapshot_count == 1
    assert publication.publication_id in swept.pending_cleanup_publication_ids
    cleanup_publishers = [
        DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())) for _ in range(2)
    ]
    cleanup_locked, release_cleanup = Event(), Event()

    def pause_cleanup(point: str) -> None:
        if point == "during_cleanup" and not cleanup_locked.is_set():
            cleanup_locked.set()
            assert release_cleanup.wait(timeout=10)

    cleanup_publishers[0]._inject = pause_cleanup
    cleanup_outcomes: dict[int, object] = {}

    def cleanup(index: int) -> None:
        try:
            cleanup_outcomes[index] = cleanup_publishers[index].cleanup_one(
                publication.publication_id
            )
        except (AssertionError, DBAPIError, MarketDataError) as error:
            cleanup_outcomes[index] = error

    cleanup_threads = [Thread(target=cleanup, args=(index,), daemon=True) for index in range(2)]
    cleanup_threads[0].start()
    assert cleanup_locked.wait(timeout=10)
    cleanup_threads[1].start()
    release_cleanup.set()
    for thread in cleanup_threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in cleanup_threads)
    assert len(cleanup_outcomes) == 2
    assert all(not isinstance(item, Exception) for item in cleanup_outcomes.values())
    cleanup_results = list(cleanup_outcomes.values())
    assert all(item.complete for item in cleanup_results)
    assert sum(item.deleted_active_count for item in cleanup_results) == 60
    assert sum(bool(item.replayed) for item in cleanup_results) == 1


def test_expiry_sweep_winner_makes_waiting_continuation_return_stale_version_without_rows(
    catalog, contexts, archive_store
) -> None:
    _scenario_read_cursor_hash_owner_policy_ordinal_expiry_and_5000_row_page_bound(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        open_expired = connection.execute(
            select(func.count())
            .select_from(read_snapshots)
            .where(
                and_(read_snapshots.c.state == "open", read_snapshots.c.expires_at <= func.now())
            )
        ).scalar_one()
    assert open_expired == 1


def test_recover_quarantined_latest_over_31_partitions_uses_complete_checkpoint_allowance(
    catalog, contexts, archive_store
) -> None:
    _scenario_append_day_32_reuses_31_parent_objects_writes_one_object_and_publishes_32_object_union(
        catalog, contexts, archive_store
    )
    context = contexts[0]
    with catalog.engine.begin() as c:
        source = (
            c.execute(
                select(dataset_revisions)
                .join(
                    series,
                    and_(
                        series.c.owner_user_id == dataset_revisions.c.owner_user_id,
                        series.c.latest_revision_id == dataset_revisions.c.dataset_revision_id,
                    ),
                )
                .where(dataset_revisions.c.owner_user_id == context.user_id)
            )
            .mappings()
            .one()
        )
        c.execute(
            update(dataset_revisions)
            .where(dataset_revisions.c.dataset_revision_id == source["dataset_revision_id"])
            .values(status="quarantined", record_version=3)
        )
    recovered = DatasetPublisher(
        catalog, archive_store, worker_id=str(uuid7())
    ).recover_quarantined_latest(
        context,
        QuarantinedLatestRecoveryRequest(
            quarantined_revision_id=source["dataset_revision_id"],
            expected_manifest_sha256=source["manifest_sha256"],
        ),
        idempotency_key=str(uuid7()),
    )
    assert len(recovered.dataset_revision.partition_refs) == 32
    assert {item.object_id for item in recovered.dataset_revision.partition_refs}.isdisjoint(
        {item["object_id"] for item in source["projection"]["partition_refs"]}
    )


def _scenario_publication_rejects_partial_day_and_open_gap_and_child_coverage_is_parent_request_union(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    template = bar_input(contract.contract_id)
    first = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(template,)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    before_files = set(archive_store._owner_root(context.user_id).rglob("*"))
    with pytest.raises(MarketDataError) as partial:
        publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=template.start_at,
                coverage_end=template.end_at,
            ),
            idempotency_key=str(uuid7()),
        )
    assert partial.value.code.value == "VALIDATION_ERROR"
    assert set(archive_store._owner_root(context.user_id).rglob("*")) == before_files
    remaining = tuple(
        template.model_copy(
            update={
                "start_at": template.start_at + timedelta(minutes=ordinal),
                "end_at": template.end_at + timedelta(minutes=ordinal),
                "completed_at": template.completed_at + timedelta(minutes=ordinal),
            }
        )
        for ordinal in range(1, 60)
    )
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=remaining), idempotency_key=str(uuid7())
    )
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=template.start_at,
            coverage_end=template.start_at + timedelta(hours=1),
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=template.start_at,
            coverage_end=template.start_at + timedelta(hours=1),
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    assert child.dataset_revision.coverage_start == parent.dataset_revision.coverage_start
    assert child.dataset_revision.coverage_end == parent.dataset_revision.coverage_end
    assert sum(item.row_count for item in child.dataset_revision.partition_refs) == 60


def _scenario_publisher_checks_reused_object_bytes_and_cannot_publish_corrupt_child(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    object_path = archive_store.resolve(
        context.user_id, parent.dataset_revision.partition_refs[0].uri
    )
    object_path.chmod(0o600)
    object_path.write_bytes(b"corrupt-parent-object")
    before = set(archive_store._owner_root(context.user_id).rglob("*"))
    with pytest.raises(MarketDataError) as corrupt:
        publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=bar_input(contract.contract_id).start_at,
                coverage_end=bar_input(contract.contract_id).end_at,
                expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=str(uuid7()),
        )
    assert corrupt.value.code.value == "ARCHIVE_INTEGRITY"
    assert set(archive_store._owner_root(context.user_id).rglob("*")) == before
    with catalog.engine.begin() as c:
        status = c.execute(
            select(dataset_revisions.c.status).where(
                dataset_revisions.c.dataset_revision_id
                == parent.dataset_revision.dataset_revision_id
            )
        ).scalar_one()
    assert status == "quarantined"


def test_touched_replaced_parent_object_is_verified_through_ancestor_dag_before_child_staging(
    catalog, contexts, archive_store
) -> None:
    _scenario_publisher_checks_reused_object_bytes_and_cannot_publish_corrupt_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(dataset_revisions)
                .where(dataset_revisions.c.status == "quarantined")
            ).scalar_one()
            >= 1
        )


def test_missing_or_corrupt_parent_manifest_quarantines_dependency_closure_before_new_files(
    catalog, contexts, archive_store
) -> None:
    _scenario_publisher_checks_reused_object_bytes_and_cannot_publish_corrupt_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(publication_files)
                .where(publication_files.c.state == "quarantined")
            ).scalar_one()
            >= 1
        )


def test_cleanup_before_initial_snapshot_commit_conflicts_with_shared_active_lock_then_read_retries(
    postgres_engine, contexts, archive_store, monkeypatch
) -> None:
    context = contexts[0]
    now = [datetime(2026, 11, 1, 22, 59, tzinfo=UTC)]
    catalog = MarketDataCatalog(
        postgres_engine, context_is_current=lambda _: True, clock=lambda: now[0]
    )
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    recorded = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    real_cleanup = publisher.cleanup_one
    monkeypatch.setattr(publisher, "cleanup_one", lambda _: None)
    publication = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    monkeypatch.setattr(publisher, "cleanup_one", real_cleanup)
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    inside_snapshot, release_snapshot = Event(), Event()
    real_coverage = reader._coverage

    def paused_coverage(*args, **kwargs):
        inside_snapshot.set()
        assert release_snapshot.wait(10)
        return real_coverage(*args, **kwargs)

    monkeypatch.setattr(reader, "_coverage", paused_coverage)
    read_result: list[object] = []
    cleanup_result: list[object] = []
    read_thread = Thread(
        target=lambda: read_result.append(
            reader.read_bars(
                context,
                ReadBarsRequest(
                    series_key=key,
                    coverage_start=bar.start_at,
                    coverage_end=bar.end_at,
                    policy=LatestRead(),
                ),
            )
        )
    )
    read_thread.start()
    assert inside_snapshot.wait(10)
    cleanup_thread = Thread(
        target=lambda: cleanup_result.append(real_cleanup(publication.publication_id))
    )
    cleanup_thread.start()
    cleanup_thread.join(0.25)
    assert cleanup_thread.is_alive()
    release_snapshot.set()
    read_thread.join(10)
    cleanup_thread.join(10)
    assert not read_thread.is_alive() and not cleanup_thread.is_alive()
    assert len(read_result) == len(cleanup_result) == 1
    assert len(read_result[0].selections) == 1
    assert cleanup_result[0].deleted_active_count == 0
    assert cleanup_result[0].blocked_active_count == 1
    with catalog.engine.connect() as c:
        assert (
            c.execute(
                select(func.count())
                .select_from(active_bars)
                .where(active_bars.c.bar_record_id == recorded.inserted_bar_record_ids[0])
            ).scalar_one()
            == 1
        )


def test_concurrent_publishers_same_parent_produce_one_latest_and_loser_rebases_or_quarantines(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    original = catalog.record_completed_batch(
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
    start = bar_input(contract.contract_id).start_at
    base = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())).publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    original.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    request = PublicationRequest(
        series_key=key,
        coverage_start=start,
        coverage_end=start + timedelta(minutes=1),
        expected_parent_revision_id=base.dataset_revision.dataset_revision_id,
    )
    publishers = [
        DatasetPublisher(catalog, archive_store, worker_id=str(uuid7())) for _ in range(2)
    ]
    idempotency_keys = [str(uuid7()), str(uuid7())]
    outcomes: dict[int, object] = {}
    first_admitted, release_first = Event(), Event()

    def run(index: int) -> None:
        publisher = publishers[index]

        def synchronize(point: str) -> None:
            if index == 0 and point == "after_stale_takeover_commit_before_snapshot":
                first_admitted.set()
                assert release_first.wait(timeout=10)

        publisher._inject = synchronize
        try:
            outcomes[index] = publisher.publish(
                context, request, idempotency_key=idempotency_keys[index]
            )
        except (AssertionError, DBAPIError, MarketDataError) as error:
            outcomes[index] = error

    threads = [Thread(target=run, args=(index,), daemon=True) for index in range(2)]
    threads[0].start()
    assert first_admitted.wait(timeout=10)
    threads[1].start()
    threads[1].join(timeout=15)
    release_first.set()
    threads[0].join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2
    successes = [item for item in outcomes.values() if not isinstance(item, Exception)]
    failures = [item for item in outcomes.values() if isinstance(item, Exception)]
    assert len(successes) == 1, outcomes
    assert len(failures) == 1, outcomes
    assert isinstance(failures[0], MarketDataError)
    assert failures[0].code.value in {"CONFLICT", "STALE_VERSION"}

    loser_index = next(index for index, item in outcomes.items() if isinstance(item, Exception))
    # The losing durable root remains retryable and rebases from the winning latest.
    publishers[loser_index]._inject = lambda _: None
    retry = publishers[loser_index].publish(
        context,
        request,
        idempotency_key=idempotency_keys[loser_index],
    )
    with catalog.engine.begin() as connection:
        latest = connection.execute(select(series.c.latest_revision_id)).scalar_one()
        published = connection.execute(
            select(func.count())
            .select_from(dataset_revisions)
            .where(dataset_revisions.c.status == "published")
        ).scalar_one()
    assert latest == retry.dataset_revision.dataset_revision_id
    assert published == 3


def test_displaced_root_clears_attempt_and_same_key_retry_rebases_without_resurrecting_candidate(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        rebased = list(
            connection.execute(
                select(idempotency).where(idempotency.c.rebase_count == 1)
            ).mappings()
        )
    assert len(rebased) == 1 and rebased[0]["current_publication_id"] is not None


def test_fence_takeover_prevents_stale_stage_publish_quarantine_and_cleanup(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_publication_crash_matrix(
        catalog,
        contexts,
        archive_store,
        monkeypatch,
        "after_staged_commit_before_rename",
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(publications)
                .where(publications.c.state == "staged")
            ).scalar_one()
            == 0
        )


def test_fence_takeover_locks_multiple_stale_idempotency_roots_before_series_fence(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_fourth_displacement_terminally_fails_and_same_key_replays_conflict(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        roots = list(
            connection.execute(
                select(idempotency).where(idempotency.c.operation == "dataset.publish")
            ).mappings()
        )
    assert all(row["state"] in {"succeeded", "failed"} for row in roots)


def test_parent_cas_loser_cannot_quarantine_until_reconciler_acquires_fresh_fence(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(publications)
                .where(publications.c.state == "quarantined")
            ).scalar_one()
            == 0
        )


def test_parent_cas_rebase_cancels_old_conversion_before_new_attempt_converts_same_active_ref(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(publication_retention_conversions)
                .where(publication_retention_conversions.c.state == "planned")
            ).scalar_one()
            == 0
        )


def test_multi_revision_preservation_publication_is_atomic_at_every_crash_point(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_publication_crash_matrix(
        catalog,
        contexts,
        archive_store,
        monkeypatch,
        "during_publish_tx_before_commit",
    )
    with catalog.engine.begin() as connection:
        statuses = list(connection.execute(select(dataset_revisions.c.status)).scalars())
    assert statuses and "building" not in statuses


def test_overlapping_full_day_correction_rewrites_complete_partition_without_dropping_parent_rows(
    catalog, contexts, archive_store
) -> None:
    _scenario_publication_rejects_partial_day_and_open_gap_and_child_coverage_is_parent_request_union(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        latest = connection.execute(
            select(dataset_revisions.c.coverage_end - dataset_revisions.c.coverage_start).join(
                series, series.c.latest_revision_id == dataset_revisions.c.dataset_revision_id
            )
        ).scalar_one()
    assert latest == timedelta(hours=1)


def test_publish_transaction_converts_causal_retention_before_visibility_or_rolls_back_all(
    catalog, contexts, archive_store
) -> None:
    _scenario_base_null_retained_r1_with_r2_before_first_publication_creates_preservation_then_latest_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        conversions = list(
            connection.execute(select(publication_retention_conversions.c.state)).scalars()
        )
    assert conversions and set(conversions) == {"converted"}


def test_repeatable_read_injection_between_manifest_and_active_queries_never_mixes_generations(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    r1_input = bar_input(contract.contract_id)
    r1 = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(r1_input,)), idempotency_key=str(uuid7())
    )
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    base = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=r1_input.start_at,
            coverage_end=r1_input.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    r2_input = bar_input(
        contract.contract_id,
        2,
        r1.inserted_bar_record_ids[0],
        "SOURCE_CORRECTION",
    )
    r2 = catalog.record_completed_batch(
        context, RecordBatchInput(bars=(r2_input,)), idempotency_key=str(uuid7())
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    published_during_read: list[object] = []

    def publish_between_queries(point: str) -> None:
        assert point == "between_manifest_and_active_queries"
        if published_during_read:
            return
        published_during_read.append(
            publisher.publish(
                context,
                PublicationRequest(
                    series_key=key,
                    coverage_start=r2_input.start_at,
                    coverage_end=r2_input.end_at,
                    expected_parent_revision_id=base.dataset_revision.dataset_revision_id,
                ),
                idempotency_key=str(uuid7()),
            )
        )

    monkeypatch.setattr(reader, "_inject", publish_between_queries)
    during = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=r2_input.start_at,
            coverage_end=r2_input.end_at,
            policy=LatestRead(),
        ),
    )
    # Cleanup won the first snapshot's active-row update race, so the reader's
    # single whole-request retry must be entirely post-publication.
    assert (
        during.published_base_revision_id
        == published_during_read[0].dataset_revision.dataset_revision_id
    )
    assert [item.bar.bar_record_id for item in during.selections] == [r2.inserted_bar_record_ids[0]]
    assert [item.origin for item in during.selections] == ["archive"]
    monkeypatch.setattr(reader, "_inject", lambda _: None)
    after = reader.read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=r2_input.start_at,
            coverage_end=r2_input.end_at,
            policy=LatestRead(),
        ),
    )
    assert (
        after.published_base_revision_id
        == published_during_read[0].dataset_revision.dataset_revision_id
    )
    assert [item.bar.bar_record_id for item in after.selections] == [r2.inserted_bar_record_ids[0]]


def test_shared_corrupt_object_quarantines_all_parent_and_aggregate_dependents_under_ordered_fences(
    catalog, contexts, archive_store
) -> None:
    _scenario_corruption_closure_pages_every_descendant_without_total_count_cutoff(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        quarantined = connection.execute(
            select(func.count())
            .select_from(dataset_revisions)
            .where(dataset_revisions.c.status == "quarantined")
        ).scalar_one()
    assert quarantined == 1002


def test_manifest_final_projection_bytes_equal_published_and_recovered_catalog_lifecycle_fields(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
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

    def assert_manifest_projection(result) -> None:
        document = json.loads(
            archive_store.read_verified(
                context.user_id,
                result.dataset_revision.manifest_uri,
                result.dataset_revision.manifest_sha256,
                result.dataset_revision.manifest_byte_length,
            )
        )
        projection = result.dataset_revision.model_dump(mode="json")
        for name, value in projection.items():
            if name not in {"manifest_sha256", "manifest_byte_length"}:
                assert document[name] == value
        with catalog.engine.begin() as c:
            stored = c.execute(
                select(dataset_revisions.c.projection).where(
                    dataset_revisions.c.dataset_revision_id
                    == result.dataset_revision.dataset_revision_id
                )
            ).scalar_one()
        assert stored == projection

    assert_manifest_projection(published)
    with catalog.engine.begin() as c:
        c.execute(
            update(dataset_revisions)
            .where(
                dataset_revisions.c.dataset_revision_id
                == published.dataset_revision.dataset_revision_id
            )
            .values(status="quarantined", record_version=3)
        )
    recovered = publisher.recover_quarantined_latest(
        context,
        QuarantinedLatestRecoveryRequest(
            quarantined_revision_id=published.dataset_revision.dataset_revision_id,
            expected_manifest_sha256=published.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    assert_manifest_projection(recovered)


def test_restore_retained_manifest_rehydrates_pinned_catalog_without_moving_newer_latest(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    child = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
            expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    restored = publisher.restore_retained_manifest(
        context,
        RetainedManifestRestoreRequest(
            manifest_uri=parent.dataset_revision.manifest_uri,
            expected_manifest_sha256=parent.dataset_revision.manifest_sha256,
        ),
        idempotency_key=str(uuid7()),
    )
    assert not restored.latest_pointer_changed
    with catalog.engine.begin() as c:
        assert (
            c.execute(
                select(series.c.latest_revision_id).where(
                    series.c.contract_id == contract.contract_id
                )
            ).scalar_one()
            == child.dataset_revision.dataset_revision_id
        )
    pinned = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True).read_bars(
        context,
        ReadBarsRequest(
            series_key=key,
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
            policy=PinnedRead(dataset_revision_id=parent.dataset_revision.dataset_revision_id),
        ),
    )
    assert pinned.selections[0].bar.bar_record_id == first.inserted_bar_record_ids[0]


def test_run_lane_retention_reference_is_non_authoritative_and_legal_hold_id_is_server_generated(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    revision = (
        DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
        .publish(
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
        .dataset_revision
    )
    run_id, lane_id = str(uuid7()), str(uuid7())
    run = catalog.retain_revision(
        context,
        revision.dataset_revision_id,
        "run",
        run_id,
        idempotency_key=str(uuid7()),
    )
    lane = catalog.retain_revision(
        context,
        revision.dataset_revision_id,
        "lane",
        lane_id,
        idempotency_key=str(uuid7()),
    )
    admin = replace(context, is_administrator=True)
    hold = catalog.place_legal_hold(
        admin, revision.dataset_revision_id, idempotency_key=str(uuid7())
    )
    assert (run.reference_id, lane.reference_id) == (run_id, lane_id)
    assert hold.reference_kind == "legal_hold"
    assert hold.reference_id not in {run_id, lane_id}
    assert __import__("uuid").UUID(hold.reference_id).version == 7


def test_orphan_temp_sweep_is_singleton_owner_derived_aged_and_never_touches_staged_or_live_prestage_files(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    context = contexts[0]
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    fsynced: list[object] = []
    original_fsync = archive_store._fsync_directory

    def observe_fsync(path) -> None:
        fsynced.append(path)
        original_fsync(path)

    monkeypatch.setattr(archive_store, "_fsync_directory", observe_fsync)
    aged = archive_store.write_temp(context.user_id, b"aged", "orphan")
    fresh = archive_store.write_temp(context.user_id, b"fresh", "orphan")
    old = (catalog._now() - timedelta(hours=25)).timestamp()
    os.utime(aged, (old, old))
    swept = publisher.sweep_orphan_temps()
    assert swept.abandoned_count == swept.quarantined_count == 1
    assert not aged.exists() and fresh.exists()
    owner_root = archive_store._owner_root(context.user_id)
    assert owner_root / "staging" in fsynced
    assert owner_root / "quarantine" in fsynced
    replay = publisher.sweep_orphan_temps()
    assert replay.abandoned_count == 0


def test_live_prestage_writer_renewal_excludes_concurrent_orphan_sweep(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=(bar_input(contract.contract_id),)),
        idempotency_key=str(uuid7()),
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()), lease_seconds=2)
    renewals: list[datetime] = []
    swept_during_write = False

    def observe_production_renewal(point: str) -> None:
        nonlocal swept_during_write
        if point == "lease_renewed":
            with catalog.engine.begin() as connection:
                lease = connection.execute(
                    select(prestage_writes.c.lease_expires_at)
                    .where(prestage_writes.c.owner_user_id == context.user_id)
                    .order_by(prestage_writes.c.lease_expires_at.desc())
                ).scalar()
            if lease is not None:
                renewals.append(lease)
        if point == "during_temp_write" and not swept_during_write:
            with catalog.engine.begin() as connection:
                journal = (
                    connection.execute(
                        select(prestage_writes)
                        .where(prestage_writes.c.owner_user_id == context.user_id)
                        .order_by(prestage_writes.c.lease_expires_at.desc())
                    )
                    .mappings()
                    .one()
                )
            temp_path = (
                archive_store._owner_root(context.user_id)
                / "staging"
                / journal["expected_temp_names"][0]
            )
            os.utime(temp_path, ((catalog._now() - timedelta(hours=25)).timestamp(),) * 2)
            live = DatasetPublisher(
                catalog, archive_store, worker_id=str(uuid7())
            ).sweep_orphan_temps()
            assert live.skipped_live_count >= 1
            assert live.quarantined_count == 0
            assert temp_path.exists()
            swept_during_write = True

    monkeypatch.setattr(publisher, "_inject", observe_production_renewal)
    start = bar_input(contract.contract_id).start_at
    published = publisher.publish(
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
    assert swept_during_write
    assert len(renewals) >= 2
    assert renewals == sorted(renewals)
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(dataset_revisions.c.status).where(
                    dataset_revisions.c.dataset_revision_id
                    == published.dataset_revision.dataset_revision_id
                )
            ).scalar_one()
            == "published"
        )


def test_fence_takeover_cancels_staged_conversion_before_competing_publication_converts_same_active_ref(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(publication_retention_conversions)
                .where(publication_retention_conversions.c.state == "planned")
            ).scalar_one()
            == 0
        )


def test_publication_rejects_unreconstructible_closure_before_filesystem_mutation(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    first = catalog.record_completed_batch(
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
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    first.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    manifest_path = archive_store.resolve(context.user_id, parent.dataset_revision.manifest_uri)
    manifest_path.chmod(0o600)
    manifest_path.unlink()
    before = set(archive_store._owner_root(context.user_id).rglob("*"))
    with pytest.raises(MarketDataError) as caught:
        publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=bar_input(contract.contract_id).start_at,
                coverage_end=bar_input(contract.contract_id).end_at,
                expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=str(uuid7()),
        )
    assert caught.value.code.value == "ARCHIVE_INTEGRITY"
    assert set(archive_store._owner_root(context.user_id).rglob("*")) == before
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(dataset_revisions.c.status).where(
                    dataset_revisions.c.dataset_revision_id
                    == parent.dataset_revision.dataset_revision_id
                )
            ).scalar_one()
            == "quarantined"
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(publication_files)
                .where(publication_files.c.state == "staged")
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    "corruption",
    ("schema", "owner", "uri", "length", "sha", "catalog_projection"),
)
def test_publisher_verifies_parent_manifest_schema_owner_uri_length_sha_and_catalog_projection_before_staging(
    catalog, contexts, archive_store, monkeypatch, corruption
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    recorded = catalog.record_completed_batch(
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
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    parent = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=bar_input(contract.contract_id).start_at,
            coverage_end=bar_input(contract.contract_id).end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    2,
                    recorded.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    original_read = archive_store.read_verified

    def corrupt_manifest(owner, uri, sha256, byte_length):
        raw = original_read(owner, uri, sha256, byte_length)
        if uri != parent.dataset_revision.manifest_uri:
            return raw
        document = json.loads(raw)
        if corruption == "schema":
            document["unexpected"] = True
        elif corruption == "owner":
            document["owner_user_id"] = contexts[1].user_id
        elif corruption == "uri":
            document["manifest_uri"] = f"ft-archive://manifest/{uuid7()}"
        elif corruption == "length":
            document["partition_refs"][0]["byte_length"] += 1
        elif corruption == "sha":
            document["partition_refs"][0]["sha256"] = "a" * 64
        else:
            document["source_watermark"]["max_bar_record_id"] = str(uuid7())
        return canonical_json_bytes(document)

    monkeypatch.setattr(archive_store, "read_verified", corrupt_manifest)
    before = set(archive_store._owner_root(context.user_id).rglob("*"))
    with pytest.raises(MarketDataError) as caught:
        publisher.publish(
            context,
            PublicationRequest(
                series_key=key,
                coverage_start=bar_input(contract.contract_id).start_at,
                coverage_end=bar_input(contract.contract_id).end_at,
                expected_parent_revision_id=parent.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=str(uuid7()),
        )
    assert caught.value.code.value == "ARCHIVE_INTEGRITY"
    assert set(archive_store._owner_root(context.user_id).rglob("*")) == before
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(dataset_revisions.c.status).where(
                    dataset_revisions.c.dataset_revision_id
                    == parent.dataset_revision.dataset_revision_id
                )
            ).scalar_one()
            == "quarantined"
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(publications)
                .where(publications.c.state == "staged")
            ).scalar_one()
            == 0
        )


def test_coverage_more_than_10000_coalesced_spans_fails_whole_request_without_truncation(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    start = datetime(2026, 11, 1, 23, tzinfo=UTC)
    end = start + timedelta(minutes=10001)
    calendar = catalog.create_calendar(
        context,
        CalendarCreateInput(
            exchange_timezone="America/Chicago",
            coverage_start=start,
            coverage_end=end,
            windows=(
                CalendarWindowInput(
                    kind="open",
                    start_at=start,
                    end_at=end,
                    trading_day=date(2026, 11, 2),
                    reason=None,
                ),
            ),
            metadata_as_of=start,
            provenance_ref="coverage-cap",
        ),
        idempotency_key=str(uuid7()),
    )
    contract = catalog.register_contract(
        context,
        FuturesContractInput(
            provider="synthetic",
            provider_contract_id="coverage-cap",
            root_symbol="MGC",
            exchange="XCHI",
            currency="USD",
            tick_size=Decimal("0.10"),
            multiplier=Decimal(10),
            expiry_label="2026-12",
            last_trade_at=end + timedelta(days=30),
            calendar_id=calendar.calendar_id,
            calendar_version=1,
            entry_cutoff_at=end + timedelta(days=20),
            liquidation_start_at=end + timedelta(days=25),
            metadata_as_of=start,
            provenance_ref="coverage-cap",
        ),
        idempotency_key=str(uuid7()),
    )
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
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    template = (
        reader.read_bars(
            context,
            ReadBarsRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
                policy=LatestRead(),
            ),
        )
        .selections[0]
        .bar
    )
    with catalog.engine.begin() as c:
        sr = catalog._series_for(c, context.user_id, key)
        alternating = [
            template.model_copy(
                update={
                    "start_at": start + timedelta(minutes=ordinal),
                    "end_at": start + timedelta(minutes=ordinal + 1),
                }
            )
            for ordinal in range(0, 10001, 2)
        ]
        with pytest.raises(MarketDataError) as capped:
            reader._coverage(c, context, sr, start, end, alternating, None)
    assert capped.value.code.value == "VALIDATION_ERROR"


def test_read_snapshot_rejects_more_than_500000_selected_rows_before_insertion(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
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
            coverage_start=bar.start_at,
            coverage_end=bar.end_at,
        ),
        idempotency_key=str(uuid7()),
    )
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    frozen = (
        reader.read_bars(
            context,
            ReadBarsRequest(
                series_key=key,
                coverage_start=bar.start_at,
                coverage_end=bar.end_at,
                policy=PinnedRead(
                    dataset_revision_id=published.dataset_revision.dataset_revision_id
                ),
            ),
        )
        .selections[0]
        .bar
    )
    monkeypatch.setattr(reader, "_archive_rows", lambda *_: [frozen] * 500001)
    with pytest.raises(MarketDataError) as capped:
        reader.read_bars(
            context,
            ReadBarsRequest(
                series_key=key,
                coverage_start=bar.start_at,
                coverage_end=bar.end_at,
                policy=PinnedRead(
                    dataset_revision_id=published.dataset_revision.dataset_revision_id
                ),
            ),
        )
    assert capped.value.code.value == "VALIDATION_ERROR"


def test_complete_revision_over_2000_objects_2000000_rows_or_10gib_fails_before_filesystem_mutation(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    revision = (
        DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
        .publish(
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
        .dataset_revision
    )
    before = set(archive_store._owner_root(context.user_id).rglob("*"))
    for change in (
        {"partition_refs": revision.partition_refs * 2001},
        {"restore_closure_row_count": 2_000_001},
        {"restore_closure_bytes": 10 * 1024**3 + 1},
    ):
        payload = revision.model_dump()
        payload.update(change)
        with pytest.raises(ValidationError):
            DatasetRevision.model_validate(payload)
        assert set(archive_store._owner_root(context.user_id).rglob("*")) == before


def test_restore_missing_cyclic_cross_owner_or_over_bound_parent_chain_fails_before_mutation(
    catalog, contexts, archive_store
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    published = publisher.publish(
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
    path = archive_store.resolve(context.user_id, published.dataset_revision.manifest_uri)
    original = json.loads(path.read_bytes())
    with catalog.engine.begin() as c:
        c.execute(text("TRUNCATE market_data_series CASCADE"))
    missing = json.loads(json.dumps(original))
    missing.update(
        parent_revision_id=str(uuid7()),
        parent_manifest_uri=f"ft-archive://manifest/{uuid7()}",
        parent_manifest_sha256="a" * 64,
        parent_depth=1,
        restore_closure_revision_count=2,
    )
    cyclic = json.loads(json.dumps(missing))
    cyclic["parent_revision_id"] = cyclic["dataset_revision_id"]
    cyclic["parent_manifest_uri"] = cyclic["manifest_uri"]
    cross_owner = json.loads(json.dumps(original))
    cross_owner["owner_user_id"] = contexts[1].user_id
    cross_owner["series_key"]["owner_user_id"] = contexts[1].user_id
    over_bound = json.loads(json.dumps(original))
    over_bound["restore_closure_revision_count"] = 1001
    path.chmod(0o600)
    try:
        for document in (missing, cyclic, cross_owner, over_bound):
            raw = canonical_json_bytes(document)
            path.write_bytes(raw)
            with pytest.raises((MarketDataError, AccessError)):
                publisher.restore_retained_manifest(
                    context,
                    RetainedManifestRestoreRequest(
                        manifest_uri=published.dataset_revision.manifest_uri,
                        expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
                    ),
                    idempotency_key=str(uuid7()),
                )
            with catalog.engine.begin() as c:
                assert c.execute(select(func.count()).select_from(series)).scalar_one() == 0
    finally:
        path.write_bytes(canonical_json_bytes(original))
        path.chmod(0o400)


@pytest.mark.parametrize(
    "injection_id",
    (
        "restore_after_preflight_before_transaction",
        "restore_during_transaction_before_commit",
        "restore_after_transaction_commit",
    ),
)
def test_restore_crash_before_during_after_transaction_is_idempotent(
    catalog, contexts, archive_store, monkeypatch, injection_id
) -> None:
    context = contexts[0]
    _, contract = seed(catalog, context)
    bar = bar_input(contract.contract_id)
    catalog.record_completed_batch(
        context, RecordBatchInput(bars=(bar,)), idempotency_key=str(uuid7())
    )
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    source = publisher.publish(
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
    ).dataset_revision
    with catalog.engine.begin() as c:
        c.execute(text("TRUNCATE market_data_series CASCADE"))
    request = RetainedManifestRestoreRequest(
        manifest_uri=source.manifest_uri,
        expected_manifest_sha256=source.manifest_sha256,
    )
    key = str(uuid7())
    fired = False

    def crash(point: str) -> None:
        nonlocal fired
        if point == injection_id and not fired:
            fired = True
            raise RuntimeError(f"injected:{point}")

    monkeypatch.setattr(publisher, "_inject", crash)
    with pytest.raises(RuntimeError, match=f"injected:{injection_id}"):
        publisher.restore_retained_manifest(context, request, idempotency_key=key)
    assert fired
    with catalog.engine.begin() as connection:
        count_after_crash = connection.execute(
            select(func.count()).select_from(dataset_revisions)
        ).scalar_one()
    assert count_after_crash == (1 if injection_id == "restore_after_transaction_commit" else 0)
    monkeypatch.setattr(publisher, "_inject", lambda _: None)
    first = publisher.restore_retained_manifest(context, request, idempotency_key=key)
    replay = publisher.restore_retained_manifest(context, request, idempotency_key=key)
    assert first.dataset_revision.dataset_revision_id == source.dataset_revision_id
    assert replay.replayed and replay.dataset_revision == first.dataset_revision


def test_rollover_checkpoint_restore_does_not_traverse_audit_only_source_provenance(
    catalog, contexts, archive_store
) -> None:
    _scenario_depth_999_publication_rolls_over_to_self_contained_checkpoint_before_child(
        catalog, contexts, archive_store
    )
    context = contexts[0]
    with catalog.engine.begin() as c:
        final = (
            c.execute(
                select(dataset_revisions).join(
                    series,
                    and_(
                        series.c.owner_user_id == dataset_revisions.c.owner_user_id,
                        series.c.latest_revision_id == dataset_revisions.c.dataset_revision_id,
                    ),
                )
            )
            .mappings()
            .one()
        )
        checkpoint = (
            c.execute(
                select(dataset_revisions).where(
                    dataset_revisions.c.dataset_revision_id == final["parent_revision_id"]
                )
            )
            .mappings()
            .one()
        )
    assert checkpoint["rollover_from_manifest_uri"] is not None
    audit_source = archive_store.resolve(context.user_id, checkpoint["rollover_from_manifest_uri"])
    audit_source.chmod(0o600)
    audit_source.write_bytes(b"corrupt audit-only rollover provenance")
    with catalog.engine.begin() as c:
        c.execute(text("TRUNCATE market_data_series CASCADE"))
    restored = DatasetPublisher(
        catalog, archive_store, worker_id=str(uuid7())
    ).restore_retained_manifest(
        context,
        RetainedManifestRestoreRequest(
            manifest_uri=final["manifest_uri"],
            expected_manifest_sha256=final["manifest_sha256"],
        ),
        idempotency_key=str(uuid7()),
    )
    assert restored.dataset_revision.dataset_revision_id == final["dataset_revision_id"]


def test_rollover_over_31_parent_partitions_uses_complete_checkpoint_allowance_and_publishes(
    catalog, contexts, archive_store
) -> None:
    _scenario_append_day_32_reuses_31_parent_objects_writes_one_object_and_publishes_32_object_union(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        latest = (
            connection.execute(
                select(dataset_revisions).join(
                    series,
                    and_(
                        series.c.owner_user_id == dataset_revisions.c.owner_user_id,
                        series.c.latest_revision_id == dataset_revisions.c.dataset_revision_id,
                    ),
                )
            )
            .mappings()
            .one()
        )
        refs = connection.execute(
            select(func.count())
            .select_from(revision_partitions)
            .where(revision_partitions.c.dataset_revision_id == latest["dataset_revision_id"])
        ).scalar_one()
    assert refs == 32
    assert latest["parent_revision_id"] is not None


def test_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_post_admission_parent_race_rebases_but_initial_stale_parent_never_does(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        roots = list(
            connection.execute(
                select(idempotency).where(idempotency.c.rebase_count == 1)
            ).mappings()
        )
    assert len(roots) == 1 and roots[0]["state"] == "succeeded"


def test_fourth_displacement_terminally_fails_and_same_key_replays_conflict(
    catalog, contexts, archive_store, monkeypatch
) -> None:
    _scenario_fourth_displacement_terminally_fails_and_same_key_replays_conflict(
        catalog, contexts, archive_store, monkeypatch
    )
    with catalog.engine.begin() as connection:
        roots = list(
            connection.execute(
                select(idempotency).where(idempotency.c.rebase_count == 3)
            ).mappings()
        )
    assert len(roots) == 1 and roots[0]["state"] == "failed"


@pytest.mark.parametrize(
    "injection_id",
    (
        "after_stale_takeover_commit_before_snapshot",
        "before_temp_create",
        "during_temp_write",
        "after_file_fsync_before_directory_fsync",
        "after_all_fsync_before_staged_tx",
        "after_staged_commit_before_rename",
        "during_object_renames",
        "after_all_renames_before_directory_fsync",
        "after_rename_fsync_before_publish_tx",
        "during_publish_tx_before_commit",
        "after_publish_commit_before_cleanup",
        "during_cleanup",
        "after_recovery_admission_before_snapshot",
        "published_file_missing_or_corrupt",
        "correction_or_publisher_wins_parent_race",
    ),
)
def test_publication_crash_matrix(
    catalog, contexts, archive_store, monkeypatch, injection_id
) -> None:
    _scenario_publication_crash_matrix(catalog, contexts, archive_store, monkeypatch, injection_id)
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(publications)
                .where(publications.c.state == "staged")
            ).scalar_one()
            == 0
        )


def test_multiple_keys_and_versions_have_deterministic_monotonic_preservation_frontiers_and_ref_mapping(
    catalog, contexts, archive_store
) -> None:
    _scenario_multiple_keys_and_versions_have_deterministic_monotonic_preservation_frontiers_and_ref_mapping(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(select(func.count()).select_from(publication_revisions)).scalar_one()
            == 3
        )


def test_depth_999_publication_rolls_over_to_self_contained_checkpoint_before_child(
    catalog, contexts, archive_store
) -> None:
    _scenario_depth_999_publication_rolls_over_to_self_contained_checkpoint_before_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(dataset_revisions)
                .where(dataset_revisions.c.rollover_from_revision_id.is_not(None))
            ).scalar_one()
            == 1
        )


def test_corruption_closure_pages_every_descendant_without_total_count_cutoff(
    catalog, contexts, archive_store
) -> None:
    _scenario_corruption_closure_pages_every_descendant_without_total_count_cutoff(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        count = connection.execute(
            select(func.count())
            .select_from(dataset_revisions)
            .where(dataset_revisions.c.status == "quarantined")
        ).scalar_one()
    assert count == 1002


def test_append_day_32_reuses_31_parent_objects_writes_one_object_and_publishes_32_object_union(
    catalog, contexts, archive_store
) -> None:
    _scenario_append_day_32_reuses_31_parent_objects_writes_one_object_and_publishes_32_object_union(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        latest = connection.execute(select(series.c.latest_revision_id)).scalar_one()
        count = connection.execute(
            select(func.count())
            .select_from(revision_partitions)
            .where(revision_partitions.c.dataset_revision_id == latest)
        ).scalar_one()
    assert count == 32


def test_restore_corrected_child_walks_parent_manifests_and_rebuilds_old_then_new_registry(
    catalog, contexts, archive_store
) -> None:
    _scenario_restore_corrected_child_walks_parent_manifests_and_rebuilds_old_then_new_registry(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert connection.execute(select(func.count()).select_from(bar_versions)).scalar_one() == 2


def test_pinned_revision_survives_correction_fixture(
    catalog, contexts, archive_store, contract_case
) -> None:
    _scenario_pinned_revision_survives_correction_fixture(
        catalog, contexts, archive_store, contract_case
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(select(func.count()).select_from(dataset_revisions)).scalar_one()
            == 2
        )


def test_unexpired_read_snapshot_blocks_active_cleanup_then_expiry_sweep_resumes_cleanup(
    postgres_engine, contexts, archive_store
) -> None:
    _scenario_unexpired_read_snapshot_blocks_active_cleanup_then_expiry_sweep_resumes_cleanup(
        postgres_engine, contexts, archive_store
    )
    with postgres_engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(read_snapshots)
                .where(read_snapshots.c.state == "expired")
            ).scalar_one()
            == 0
        )


def test_corrupt_or_missing_published_object_quarantines_without_fallback(
    catalog, contexts, archive_store
) -> None:
    _scenario_corrupt_or_missing_published_object_quarantines_without_fallback(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(dataset_revisions)
                .where(dataset_revisions.c.status == "quarantined")
            ).scalar_one()
            >= 1
        )


def test_base_null_retained_r1_with_r2_before_first_publication_creates_preservation_then_latest_child(
    catalog, contexts, archive_store
) -> None:
    _scenario_base_null_retained_r1_with_r2_before_first_publication_creates_preservation_then_latest_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        states = list(
            connection.execute(select(publication_retention_conversions.c.state)).scalars()
        )
    assert states and set(states) == {"converted"}


def test_paginated_read_uses_one_owner_scoped_snapshot_across_publication_and_correction(
    catalog, contexts, archive_store
) -> None:
    _scenario_paginated_read_uses_one_owner_scoped_snapshot_across_publication_and_correction(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        snapshots = list(connection.execute(select(read_snapshots)).mappings())
    assert len(snapshots) == 1 and snapshots[0]["total_rows"] > 1


def test_correction_during_publish_remains_active_then_enters_next_child(
    catalog, contexts, archive_store
) -> None:
    _scenario_correction_during_publish_remains_active_then_enters_next_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert list(connection.execute(select(active_bars.c.source_revision)).scalars()) == []
        assert list(
            connection.execute(
                select(revision_bars.c.source_revision).order_by(revision_bars.c.source_revision)
            ).scalars()
        ) == [1, 2]


def test_read_cursor_hash_owner_policy_ordinal_expiry_and_5000_row_page_bound(
    catalog, contexts, archive_store
) -> None:
    _scenario_read_cursor_hash_owner_policy_ordinal_expiry_and_5000_row_page_bound(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        totals = list(connection.execute(select(read_snapshots.c.total_rows)).scalars())
    assert totals and max(totals) <= 500000


def test_publication_rejects_partial_day_and_open_gap_and_child_coverage_is_parent_request_union(
    catalog, contexts, archive_store
) -> None:
    _scenario_publication_rejects_partial_day_and_open_gap_and_child_coverage_is_parent_request_union(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        latest = (
            connection.execute(
                select(dataset_revisions).join(
                    series, series.c.latest_revision_id == dataset_revisions.c.dataset_revision_id
                )
            )
            .mappings()
            .one()
        )
    assert latest["coverage_start"] < latest["coverage_end"]


def test_publisher_checks_reused_object_bytes_and_cannot_publish_corrupt_child(
    catalog, contexts, archive_store
) -> None:
    _scenario_publisher_checks_reused_object_bytes_and_cannot_publish_corrupt_child(
        catalog, contexts, archive_store
    )
    with catalog.engine.begin() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(publications)
                .where(publications.c.state == "quarantined")
            ).scalar_one()
            >= 1
        )
