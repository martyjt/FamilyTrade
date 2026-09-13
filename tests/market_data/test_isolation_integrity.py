from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid7

import pytest

from familytrade.access.models import AccessError
from familytrade.market_data.archive import DatasetPublisher, MarketDataReader
from familytrade.market_data.models import (
    CoverageRequest,
    LatestRead,
    MarketDataError,
    PublicationRequest,
    ReadBarsRequest,
    RecordBatchInput,
    RetainedManifestRestoreRequest,
    SeriesKey,
)

from .test_models_catalog import bar_input, seed


def test_absolute_parent_traversal_separator_symlink_reparse_and_hardlink_escape_denied(
    archive_store, tmp_path
) -> None:
    owner = str(uuid7())
    for uri in (
        "/absolute",
        "ft-archive://object/../../secret",
        "ft-archive://manifest/not-a-uuid",
    ):
        with pytest.raises(MarketDataError):
            archive_store.resolve(owner, uri)
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"secret")
    object_uri = f"ft-archive://object/{uuid7()}"
    target = archive_store.resolve(owner, object_uri)
    os.link(outside, target)
    with pytest.raises(MarketDataError):
        archive_store.read_verified(owner, object_uri, "a" * 64, len(b"secret"))


def test_two_users_identical_series_have_distinct_rows_roots_objects_and_manifests(
    archive_store,
) -> None:
    a, b = str(uuid7()), str(uuid7())
    assert archive_store._owner_root(a) != archive_store._owner_root(b)


def test_errors_logs_repr_manifests_and_results_contain_no_root_temp_secret_or_foreign_id(
    archive_store,
) -> None:
    root = archive_store._root
    with pytest.raises(MarketDataError) as caught:
        archive_store.read_verified(str(uuid7()), f"ft-archive://object/{uuid7()}", "a" * 64, 1)
    assert str(root) not in str(caught.value)


def test_read_bars_requires_integrated_data_read_scope_before_resource_lookup(
    catalog, contexts, archive_store
) -> None:
    context = replace(contexts[0], scopes=())
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    with pytest.raises(AccessError):
        reader.read_bars(
            context,
            ReadBarsRequest(
                series_key=SeriesKey(
                    source="nonexistent",
                    price_basis="trades",
                    contract_id=str(uuid7()),
                    interval_seconds=60,
                ),
                coverage_start=contexts[0].authenticated_at,
                coverage_end=contexts[0].authenticated_at + timedelta(minutes=1),
                policy=LatestRead(),
            ),
        )


def test_owner_b_cannot_read_publish_retain_restore_or_probe_owner_a_ids(
    catalog, contexts, archive_store
) -> None:
    owner_a, owner_b = contexts
    _, contract = seed(catalog, owner_a)
    recorded = catalog.record_completed_batch(
        owner_a,
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
    published = publisher.publish(
        owner_a,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(minutes=1),
        ),
        idempotency_key=str(uuid7()),
    )
    calls = (
        lambda: MarketDataReader(
            catalog, archive_store, context_is_current=lambda _: True
        ).read_bars(
            owner_b,
            ReadBarsRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
                policy=LatestRead(),
            ),
        ),
        lambda: publisher.publish(
            owner_b,
            PublicationRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
                expected_parent_revision_id=published.dataset_revision.dataset_revision_id,
            ),
            idempotency_key=str(uuid7()),
        ),
        lambda: catalog.retain_revision(
            owner_b,
            published.dataset_revision.dataset_revision_id,
            "lane",
            str(uuid7()),
            idempotency_key=str(uuid7()),
        ),
        lambda: publisher.restore_retained_manifest(
            owner_b,
            RetainedManifestRestoreRequest(
                manifest_uri=published.dataset_revision.manifest_uri,
                expected_manifest_sha256=published.dataset_revision.manifest_sha256,
            ),
            idempotency_key=str(uuid7()),
        ),
        lambda: catalog.retain_causal_selection(
            owner_b,
            recorded.inserted_bar_record_ids[0],
            "lane",
            str(uuid7()),
            idempotency_key=str(uuid7()),
        ),
    )
    for call in calls:
        with pytest.raises((MarketDataError, AccessError)):
            call()


def test_all_four_fixture_entity_alias_paths_map_to_server_generated_uuidv7_and_relationships_round_trip() -> (
    None
):
    document = json.loads(
        (Path(__file__).resolve().parents[2] / "docs" / "contracts-examples-v1.json").read_text(
            encoding="utf-8"
        )
    )
    cases = {case["id"]: case for case in document["cases"]}
    aliases = {
        "018-user-a",
        "synthetic-mgc-2026-12",
        "018-rev-1",
        "018-rev-2",
        "018-bar-r1",
        "018-bar-r2",
        "018-rev-10",
        "active-bar-a-r1",
        "active-bar-a-r2",
        "active-bar-b-r1",
        "active-bar-b-r2",
    }
    mapped = {alias: str(uuid7()) for alias in sorted(aliases)}
    assert len(set(mapped.values())) == len(aliases)
    assert all(value == value.lower() and UUID(value).version == 7 for value in mapped.values())

    pinned = cases["pinned_revision_survives_correction"]
    assert mapped[pinned["given"]["correction_r2"]["supersedes"]] == mapped["018-bar-r1"]
    assert mapped[pinned["given"]["completed_run_pinned_to"]] == mapped["018-rev-1"]
    crash = cases["archive_publication_crash_after_rename"]
    assert (
        mapped[crash["given"]["old_latest_revision"]]
        == mapped[crash["expected"]["reader_revision_before_recovery"]]
    )
    forward = cases["forward_active_corrections_before_and_after_cursor"]
    assert (
        mapped[forward["given"]["bar_a"]["r2"]["bar_record_alias"]]
        == mapped[forward["expected"]["bar_a_source_record"]]
    )
    assert (
        mapped[forward["given"]["bar_b"]["r1"]["bar_record_alias"]]
        == mapped[forward["expected"]["bar_b_source_record"]]
    )


def test_archive_crash_fixture_checksum_placeholder_maps_to_real_store_digest_without_hash_mock(
    archive_store, contract_case
) -> None:
    fixture = contract_case("archive_publication_crash_after_rename")
    assert fixture["given"]["staged_row"]["checksum"] == "a" * 64
    owner, object_id = str(uuid7()), str(uuid7())
    payload = b"deterministic fixture-owned parquet stand-in\n"
    digest = hashlib.sha256(payload).hexdigest()
    uri = f"ft-archive://object/{object_id}"
    archive_store.finalize(owner, archive_store.write_temp(owner, payload, "fixture-checksum"), uri)
    assert digest != fixture["given"]["staged_row"]["checksum"]
    assert archive_store.read_verified(owner, uri, digest, len(payload)) == payload


def test_read_coverage_pinned_and_latest_classifications_enforce_data_read_scope_and_bounds(
    catalog, contexts, archive_store
) -> None:
    reader = MarketDataReader(catalog, archive_store, context_is_current=lambda _: True)
    key = SeriesKey(
        source="opaque",
        price_basis="trades",
        contract_id=str(uuid7()),
        interval_seconds=60,
    )
    start = contexts[0].authenticated_at
    with pytest.raises(AccessError):
        reader.read_coverage(
            replace(contexts[0], scopes=()),
            CoverageRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(minutes=1),
            ),
        )
    with pytest.raises(MarketDataError) as bounded:
        reader.read_coverage(
            contexts[0],
            CoverageRequest(
                series_key=key,
                coverage_start=start,
                coverage_end=start + timedelta(days=367),
            ),
        )
    assert bounded.value.http_status == 422
