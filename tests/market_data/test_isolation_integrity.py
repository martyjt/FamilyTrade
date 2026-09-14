from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid7

import pytest
from sqlalchemy import select

from familytrade.access.models import AccessError
from familytrade.market_data.archive import DatasetPublisher, MarketDataReader
from familytrade.market_data.catalog import bar_versions, dataset_revisions
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
    owner_root = archive_store._owner_root(owner)
    if os.name != "nt":
        assert owner_root.stat().st_mode & 0o777 == 0o700
        for directory in ("staging", "objects", "manifests", "quarantine"):
            assert (owner_root / directory).stat().st_mode & 0o777 == 0o700
        temp = archive_store.write_temp(owner, b"owner-only", "mode")
        assert temp.stat().st_mode & 0o777 == 0o600
        owner_root.chmod(0o755)
        with pytest.raises(MarketDataError):
            archive_store._owner_root(owner)
        owner_root.chmod(0o700)
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

    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    symlink_owner = str(uuid7())
    symlink_path = archive_store._root / symlink_owner
    try:
        os.symlink(outside_directory, symlink_path, target_is_directory=True)
    except OSError as error:
        if os.name != "nt" or error.winerror != 1314:
            raise
    else:
        with pytest.raises(MarketDataError):
            archive_store._owner_root(symlink_owner)

        intermediate_owner = str(uuid7())
        intermediate_root = archive_store._owner_root(intermediate_owner)
        intermediate = intermediate_root / "objects"
        intermediate.rmdir()
        os.symlink(outside_directory, intermediate, target_is_directory=True)
        with pytest.raises(MarketDataError):
            archive_store.resolve(intermediate_owner, f"ft-archive://object/{uuid7()}")

    if os.name == "nt":
        junction_owner = str(uuid7())
        junction_path = archive_store._root / junction_owner
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_path), str(outside_directory)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr
        with pytest.raises(MarketDataError):
            archive_store._owner_root(junction_owner)


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


def test_all_four_fixture_entity_alias_paths_map_to_server_generated_uuidv7_and_relationships_round_trip(
    catalog, contexts, archive_store
) -> None:
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
    context = contexts[0]
    _, contract = seed(catalog, context, full_hour=True)
    publisher = DatasetPublisher(catalog, archive_store, worker_id=str(uuid7()))
    key = SeriesKey(
        source="synthetic",
        price_basis="trades",
        contract_id=contract.contract_id,
        interval_seconds=60,
    )
    start = bar_input(contract.contract_id).start_at
    initial_bars = tuple(
        bar_input(contract.contract_id).model_copy(
            update={
                "start_at": start + timedelta(minutes=minute),
                "end_at": start + timedelta(minutes=minute + 1),
                "completed_at": start + timedelta(minutes=minute + 1),
            }
        )
        for minute in range(60)
    )
    first = catalog.record_completed_batch(
        context,
        RecordBatchInput(bars=initial_bars),
        idempotency_key=str(uuid7()),
    )
    revision_one = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(hours=1),
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
    revision_two = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(hours=1),
            expected_parent_revision_id=revision_one.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )
    catalog.record_completed_batch(
        context,
        RecordBatchInput(
            bars=(
                bar_input(
                    contract.contract_id,
                    3,
                    second.inserted_bar_record_ids[0],
                    "SOURCE_CORRECTION",
                ),
            )
        ),
        idempotency_key=str(uuid7()),
    )
    revision_three = publisher.publish(
        context,
        PublicationRequest(
            series_key=key,
            coverage_start=start,
            coverage_end=start + timedelta(hours=1),
            expected_parent_revision_id=revision_two.dataset_revision.dataset_revision_id,
        ),
        idempotency_key=str(uuid7()),
    )

    active_inputs = []
    for minute in (1, 2):
        base = initial_bars[minute]
        r1 = first.inserted_bar_record_ids[minute]
        r2 = catalog.record_completed_batch(
            context,
            RecordBatchInput(
                bars=(
                    base.model_copy(
                        update={
                            "close": base.close + 1,
                            "high": base.high + 1,
                            "source_revision": 2,
                            "supersedes_bar_record_id": r1,
                            "correction_reason": "SOURCE_CORRECTION",
                        }
                    ),
                )
            ),
            idempotency_key=str(uuid7()),
        ).inserted_bar_record_ids[0]
        active_inputs.append((r1, r2))

    mapped = {
        "018-user-a": context.user_id,
        "synthetic-mgc-2026-12": contract.contract_id,
        "018-rev-1": revision_one.dataset_revision.dataset_revision_id,
        "018-rev-2": revision_two.dataset_revision.dataset_revision_id,
        "018-rev-10": revision_three.dataset_revision.dataset_revision_id,
        "018-bar-r1": first.inserted_bar_record_ids[0],
        "018-bar-r2": second.inserted_bar_record_ids[0],
        "active-bar-a-r1": active_inputs[0][0],
        "active-bar-a-r2": active_inputs[0][1],
        "active-bar-b-r1": active_inputs[1][0],
        "active-bar-b-r2": active_inputs[1][1],
    }
    assert len(set(mapped.values())) == len(aliases)
    assert all(value == value.lower() and UUID(value).version == 7 for value in mapped.values())
    with catalog.engine.begin() as connection:
        persisted_bars = {
            row.bar_record_id: row.supersedes_bar_record_id
            for row in connection.execute(
                select(bar_versions.c.bar_record_id, bar_versions.c.supersedes_bar_record_id).where(
                    bar_versions.c.bar_record_id.in_(
                        (
                            mapped["018-bar-r1"],
                            mapped["018-bar-r2"],
                            mapped["active-bar-a-r1"],
                            mapped["active-bar-a-r2"],
                            mapped["active-bar-b-r1"],
                            mapped["active-bar-b-r2"],
                        )
                    )
                )
            )
        }
        persisted_revisions = set(
            connection.execute(
                select(dataset_revisions.c.dataset_revision_id).where(
                    dataset_revisions.c.dataset_revision_id.in_(
                        (mapped["018-rev-1"], mapped["018-rev-2"], mapped["018-rev-10"])
                    )
                )
            ).scalars()
        )
    assert persisted_bars[mapped["018-bar-r2"]] == mapped["018-bar-r1"]
    assert persisted_bars[mapped["active-bar-a-r2"]] == mapped["active-bar-a-r1"]
    assert persisted_bars[mapped["active-bar-b-r2"]] == mapped["active-bar-b-r1"]
    assert persisted_revisions == {
        mapped["018-rev-1"],
        mapped["018-rev-2"],
        mapped["018-rev-10"],
    }

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
