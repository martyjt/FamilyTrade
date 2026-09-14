"""Safe immutable filesystem archive, bounded readers, and crash-aware publication."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal, cast
from uuid import uuid7

import polars as pl
from sqlalchemy import and_, case, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError

from familytrade.access.models import (
    AccessError,
    ErrorCode,
    UserContext,
    not_found,
    unauthenticated,
)
from familytrade.access.repository import users
from familytrade.market_data.calendar import expected_bar_starts
from familytrade.market_data.catalog import (
    MarketDataCatalog,
    active_bars,
    aggregate_components,
    archive_objects,
    bar_conflicts,
    bar_retention_refs,
    bar_versions,
    calendar_versions,
    calendar_windows,
    contract_versions,
    contracts,
    dataset_revisions,
    idempotency,
    prestage_writes,
    publication_cleanup_bars,
    publication_files,
    publication_retention_conversions,
    publication_revisions,
    publications,
    quality_observations,
    read_snapshot_bars,
    read_snapshots,
    retention_refs,
    revision_bars,
    revision_partitions,
    series,
    series_fences,
)
from familytrade.market_data.catalog import correction_refs as correction_ref_rows
from familytrade.market_data.models import (
    AggregatedFullBar,
    BarSelection,
    CalendarVersion,
    CausalLatestRead,
    CleanupResult,
    CompletedBar,
    CorrectionObservation,
    CorrectionRef,
    CoverageRequest,
    CoverageResult,
    CoverageSpan,
    DatasetRevision,
    FuturesContract,
    LatestRead,
    LogicalBarKey,
    MarketDataCode,
    MarketDataError,
    OrphanSweepResult,
    OwnedSeriesKey,
    PartitionRef,
    PinnedRead,
    PublicationRequest,
    PublicationResult,
    QuarantinedLatestRecoveryRequest,
    ReadBarsRequest,
    ReadBarsResult,
    ReadCursor,
    ReadSnapshotSweepResult,
    RecoveryResult,
    RestoreResult,
    RetainedManifestRestoreRequest,
    SeriesKey,
    canonical_json_bytes,
    canonical_sha256,
)

_URI = re.compile(r"^ft-archive://(object|manifest)/([0-9a-f-]{36})$")


class ArchiveStore:
    def __init__(self, root: Path) -> None:
        configured = root.absolute()
        configured.mkdir(parents=True, exist_ok=True)
        self._reject_link_or_reparse(configured)
        self._root = configured.resolve(strict=True)

    @staticmethod
    def _reject_link_or_reparse(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY, "Unsafe archive path.", 500
            ) from None
        is_reparse = bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        is_junction = bool(getattr(path, "is_junction", lambda: False)())
        if path.is_symlink() or is_reparse or is_junction:
            raise MarketDataError(MarketDataCode.ARCHIVE_INTEGRITY, "Unsafe archive path.", 500)

    def _assert_beneath_owner(self, owner_root: Path, path: Path) -> Path:
        current = owner_root
        self._reject_link_or_reparse(current)
        try:
            relative = path.relative_to(owner_root)
        except ValueError:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY, "Unsafe archive path.", 500
            ) from None
        for component in relative.parts:
            current = current / component
            if current.exists() or current.is_symlink():
                self._reject_link_or_reparse(current)
        resolved = path.resolve(strict=False)
        resolved_owner = owner_root.resolve(strict=True)
        if resolved != resolved_owner and resolved_owner not in resolved.parents:
            raise MarketDataError(MarketDataCode.ARCHIVE_INTEGRITY, "Unsafe archive path.", 500)
        return resolved

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(directory, flags)
            os.fsync(descriptor)
        except OSError:
            # Win32 does not provide POSIX directory handles through os.open.
            # No other platform may silently skip this durability boundary.
            if os.name != "nt":
                raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _assert_owner_only_mode(path: Path, expected: int) -> None:
        if os.name == "nt":
            return
        actual = stat.S_IMODE(path.stat().st_mode)
        if actual != expected:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "Archive path permissions are unsafe.",
                500,
            )

    def _owner_root(self, owner: str) -> Path:
        try:
            if str(__import__("uuid").UUID(owner)) != owner:
                raise ValueError
        except ValueError:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY, "Invalid archive owner.", 500
            ) from None
        path = self._root / owner
        if path.exists() or path.is_symlink():
            self._reject_link_or_reparse(path)
        else:
            path.mkdir(mode=0o700)
        self._assert_owner_only_mode(path, 0o700)
        for name in ("staging", "objects", "manifests", "quarantine"):
            directory = path / name
            directory.mkdir(mode=0o700, exist_ok=True)
            self._reject_link_or_reparse(directory)
            self._assert_owner_only_mode(directory, 0o700)
            self._assert_beneath_owner(path, directory)
        return self._assert_beneath_owner(path, path)

    def resolve(self, owner: str, uri: str) -> Path:
        match = _URI.fullmatch(uri)
        if not match:
            raise MarketDataError(MarketDataCode.ARCHIVE_INTEGRITY, "Invalid archive URI.", 500)
        kind, identifier = match.groups()
        parsed = __import__("uuid").UUID(identifier)
        if parsed.version != 7 or str(parsed) != identifier:
            raise MarketDataError(MarketDataCode.ARCHIVE_INTEGRITY, "Invalid archive URI.", 500)
        folder = "objects" if kind == "object" else "manifests"
        suffix = ".parquet" if kind == "object" else ".json"
        owner_root = self._owner_root(owner)
        path = self._assert_beneath_owner(owner_root, owner_root / folder / f"{identifier}{suffix}")
        if (
            owner_root not in path.parents
            or path.is_symlink()
            or (path.exists() and path.stat().st_nlink != 1)
        ):
            raise MarketDataError(MarketDataCode.ARCHIVE_INTEGRITY, "Unsafe archive path.", 500)
        return path

    def write_temp(self, owner: str, data: bytes, suffix: str) -> Path:
        staging = self._owner_root(owner) / "staging"
        path = staging / f"{uuid7()}.{suffix}.tmp"
        with path.open("xb") as handle:
            path.chmod(0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        self._assert_owner_only_mode(path, 0o600)
        return path

    def finalize(
        self,
        owner: str,
        temp: Path,
        uri: str,
        *,
        check_fence: Callable[[], None] | None = None,
    ) -> Path:
        check = check_fence or (lambda: None)
        check()
        staging = (self._owner_root(owner) / "staging").resolve()
        resolved_temp = temp.resolve()
        if (
            resolved_temp.parent != staging
            or resolved_temp.is_symlink()
            or not resolved_temp.is_file()
            or resolved_temp.stat().st_nlink != 1
        ):
            raise MarketDataError(MarketDataCode.ARCHIVE_INTEGRITY, "Unsafe archive path.", 500)
        final = self.resolve(owner, uri)
        check()
        os.replace(resolved_temp, final)
        check()
        final.chmod(0o400)
        check()
        self._fsync_directory(final.parent)
        check()
        return final

    def read_verified(self, owner: str, uri: str, digest: str, length: int) -> bytes:
        path = self.resolve(owner, uri)
        try:
            if path.stat().st_nlink != 1:
                raise OSError
            data = path.read_bytes()
        except OSError:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY, "Archived bytes are unavailable.", 500
            ) from None
        if len(data) != length or hashlib.sha256(data).hexdigest() != digest:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "Archived bytes failed integrity verification.",
                500,
            )
        return data


def _bar(row: dict[str, Any]) -> CompletedBar:
    allowed = set(CompletedBar.model_fields)
    return CompletedBar.model_validate({k: v for k, v in row.items() if k in allowed})


def _validate_manifest_correction_history(document: dict[str, Any]) -> None:
    """Validate the manifest's self-contained immutable bar registry projection."""

    selected = tuple(CompletedBar.model_validate(item) for item in document["selected_bars"])
    if len({bar.start_at for bar in selected}) != len(selected):
        raise ValueError("duplicate selected logical bar")
    records = tuple(document["correction_chain_records"])
    by_start: dict[datetime, list[tuple[CompletedBar, dict[str, Any]]]] = {}
    for record in records:
        bar = CompletedBar.model_validate(record["completed_bar"])
        payload_hash = canonical_sha256(
            {
                name: getattr(bar, name)
                for name in (
                    "source",
                    "price_basis",
                    "contract_id",
                    "interval_seconds",
                    "start_at",
                    "end_at",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "source_revision",
                    "completed_at",
                    "quality",
                    "supersedes_bar_record_id",
                )
            }
        )
        lineage = record["aggregate_lineage_sha256"]
        if lineage is not None and not re.fullmatch(r"[0-9a-f]{64}", lineage):
            raise ValueError("invalid aggregate lineage hash")
        if (
            record["bar_record_id"] != bar.bar_record_id
            or record["source_revision"] != bar.source_revision
            or record["received_at"] != bar.received_at.isoformat().replace("+00:00", "Z")
            or record["supersedes_bar_record_id"] != bar.supersedes_bar_record_id
            or record["payload_sha256"] != payload_hash
            or bar.payload_hash != payload_hash
            or record["version_fingerprint_sha256"]
            != canonical_sha256(
                {
                    "payload_hash": payload_hash,
                    "correction_reason": record["correction_reason"],
                    "aggregate_lineage_sha256": lineage,
                }
            )
        ):
            raise ValueError("manifest correction record hash or projection mismatch")
        by_start.setdefault(bar.start_at, []).append((bar, record))
    if set(by_start) != {bar.start_at for bar in selected}:
        raise ValueError("manifest correction records do not cover selected bars")
    for selected_bar in selected:
        chain = sorted(
            by_start[selected_bar.start_at],
            key=lambda item: (item[0].source_revision, item[0].bar_record_id),
        )
        if len({bar.source_revision for bar, _ in chain}) != len(chain):
            raise ValueError("forked manifest correction chain")
        for ordinal, (bar, record) in enumerate(chain, start=1):
            prior_id = None if ordinal == 1 else chain[ordinal - 2][0].bar_record_id
            reason = record["correction_reason"]
            if (
                bar.source_revision != ordinal
                or bar.supersedes_bar_record_id != prior_id
                or (ordinal == 1 and reason is not None)
                or (ordinal > 1 and reason is None)
            ):
                raise ValueError("non-consecutive manifest correction chain")
        if chain[-1][0].bar_record_id != selected_bar.bar_record_id:
            raise ValueError("selected bar is not the correction-chain head")


def _archive_rows_from_revision(
    store: ArchiveStore, owner: str, revision: dict[str, Any]
) -> list[CompletedBar]:
    manifest = store.read_verified(
        owner,
        revision["manifest_uri"],
        revision["manifest_sha256"],
        revision["manifest_byte_length"],
    )
    doc = json.loads(manifest)
    result: list[CompletedBar] = []
    for ref in doc["partition_refs"]:
        data = store.read_verified(owner, ref["uri"], ref["sha256"], ref["byte_length"])
        temp = store.write_temp(owner, data, "read")
        try:
            frame = pl.read_parquet(temp)
            result.extend(_bar(row) for row in frame.to_dicts())
        finally:
            temp.unlink(missing_ok=True)
    return result


_MANIFEST_EXTRA_FIELDS = {
    "parent_manifest_uri",
    "parent_manifest_sha256",
    "selected_bars",
    "correction_chain_records",
    "format_version",
    "origin_publication_id",
    "origin_publication_ordinal",
    "contract_projection",
    "contract_projection_sha256",
    "calendar_projection",
}
_CHAIN_RECORD_FIELDS = {
    "completed_bar",
    "payload_sha256",
    "correction_reason",
    "aggregate_lineage_sha256",
    "version_fingerprint_sha256",
    "bar_record_id",
    "source_revision",
    "received_at",
    "supersedes_bar_record_id",
    "aggregate_components",
}
_AGGREGATE_COMPONENT_FIELDS = {
    "ordinal",
    "source_bar_record_id",
    "source_dataset_revision_id",
    "source_manifest_uri",
    "source_manifest_sha256",
    "source_manifest_byte_length",
}


def _validate_manifest_schema(document: Any) -> None:
    if not isinstance(document, dict):
        raise TypeError("manifest must be an object")
    expected = (
        set(DatasetRevision.model_fields) - {"manifest_sha256", "manifest_byte_length"}
    ) | _MANIFEST_EXTRA_FIELDS
    if set(document) != expected or document.get("format_version") != "ft-dataset-manifest-v1":
        raise ValueError("manifest has an invalid strict schema")
    FuturesContract.model_validate(document["contract_projection"])
    CalendarVersion.model_validate(document["calendar_projection"])
    if document["contract_projection_sha256"] != canonical_sha256(document["contract_projection"]):
        raise ValueError("manifest contract projection digest differs")
    if not isinstance(document["selected_bars"], list) or not isinstance(
        document["correction_chain_records"], list
    ):
        raise TypeError("manifest arrays have an invalid schema")
    for item in document["selected_bars"]:
        CompletedBar.model_validate(item)
    for record in document["correction_chain_records"]:
        if not isinstance(record, dict) or set(record) != _CHAIN_RECORD_FIELDS:
            raise ValueError("manifest correction record has an invalid strict schema")
        CompletedBar.model_validate(record["completed_bar"])
        components = record["aggregate_components"]
        if not isinstance(components, list) or any(
            not isinstance(component, dict) or set(component) != _AGGREGATE_COMPONENT_FIELDS
            for component in components
        ):
            raise ValueError("manifest aggregate component has an invalid strict schema")


def _verify_catalog_reconstruction_dag(
    connection: Any,
    store: ArchiveStore,
    owner: str,
    seed_revision_id: str,
    *,
    allow_quarantined_seed: bool = False,
    allow_integrity_quarantined_closure: bool = False,
    heartbeat: Callable[[], None] | None = None,
) -> list[CompletedBar]:
    """Verify the complete catalog-backed parent/aggregate DAG before staging."""

    if sys.getrecursionlimit() < 4096:
        sys.setrecursionlimit(4096)
    captured_ids = _catalog_reconstruction_ids(
        connection,
        owner,
        {seed_revision_id},
        allow_quarantined_seeds={seed_revision_id} if allow_quarantined_seed else set(),
        allow_integrity_quarantined_closure=allow_integrity_quarantined_closure,
    )
    revision_rows = {
        row["dataset_revision_id"]: dict(row)
        for row in connection.execute(
            select(dataset_revisions).where(
                and_(
                    dataset_revisions.c.owner_user_id == owner,
                    dataset_revisions.c.dataset_revision_id.in_(captured_ids),
                )
            )
        ).mappings()
    }
    contract_hashes = {
        (row["contract_id"], row["contract_version"]): row["projection_sha256"]
        for row in connection.execute(
            select(
                contract_versions.c.contract_id,
                contract_versions.c.contract_version,
                contract_versions.c.projection_sha256,
            ).where(contract_versions.c.owner_user_id == owner)
        ).mappings()
    }
    partitions_by_revision: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        select(
            revision_partitions.c.ordinal,
            revision_partitions.c.dataset_revision_id,
            archive_objects,
        )
        .join(
            archive_objects,
            and_(
                archive_objects.c.owner_user_id == revision_partitions.c.owner_user_id,
                archive_objects.c.object_id == revision_partitions.c.object_id,
            ),
        )
        .where(
            and_(
                revision_partitions.c.owner_user_id == owner,
                revision_partitions.c.dataset_revision_id.in_(captured_ids),
            )
        )
        .order_by(revision_partitions.c.dataset_revision_id, revision_partitions.c.ordinal)
    ).mappings():
        partitions_by_revision.setdefault(row["dataset_revision_id"], []).append(dict(row))
    selected_by_revision: dict[str, list[str]] = {}
    for row in connection.execute(
        select(
            revision_bars.c.dataset_revision_id,
            revision_bars.c.ordinal,
            revision_bars.c.bar_record_id,
        )
        .where(
            and_(
                revision_bars.c.owner_user_id == owner,
                revision_bars.c.dataset_revision_id.in_(captured_ids),
            )
        )
        .order_by(revision_bars.c.dataset_revision_id, revision_bars.c.ordinal)
    ).mappings():
        selected_by_revision.setdefault(row["dataset_revision_id"], []).append(row["bar_record_id"])
    # All catalog state is detached before manifest, Parquet, hashing, or filesystem work.
    connection.commit()
    verified: dict[str, tuple[dict[str, Any], list[CompletedBar]]] = {}
    verified_projections: dict[str, DatasetRevision] = {}
    closures: dict[str, set[str]] = {}
    visiting: set[str] = set()
    verified_objects: dict[str, list[CompletedBar]] = {}

    def verify(revision_id: str, *, target_interval: int | None = None) -> set[str]:
        if heartbeat is not None:
            heartbeat()
        if revision_id in verified:
            if (
                target_interval is not None
                and verified[revision_id][0]["series_key"]["interval_seconds"] >= target_interval
            ):
                raise ValueError("aggregate dependency interval is not smaller")
            return closures[revision_id]
        if revision_id in visiting or len(verified) + len(visiting) >= 1000:
            raise ValueError("cyclic or over-bound reconstruction DAG")
        visiting.add(revision_id)
        row = revision_rows.get(revision_id)
        allowed_seed_state = (
            allow_quarantined_seed
            and revision_id == seed_revision_id
            and row is not None
            and _is_integrity_quarantined_revision(row)
        )
        allowed_closure_state = (
            allow_integrity_quarantined_closure
            and row is not None
            and _is_integrity_quarantined_revision(row)
        )
        if row is None or (
            row["status"] != "published" and not allowed_seed_state and not allowed_closure_state
        ):
            raise ValueError("reconstruction revision is absent or not published")
        manifest_bytes = store.read_verified(
            owner, row["manifest_uri"], row["manifest_sha256"], row["manifest_byte_length"]
        )
        document = json.loads(manifest_bytes)
        _validate_manifest_schema(document)
        _validate_manifest_correction_history(document)
        payload = {
            name: document[name]
            for name in DatasetRevision.model_fields
            if name not in {"manifest_sha256", "manifest_byte_length"}
        }
        payload.update(
            manifest_sha256=row["manifest_sha256"],
            manifest_byte_length=row["manifest_byte_length"],
        )
        projection = DatasetRevision.model_validate(payload)
        catalog_projection = DatasetRevision.model_validate(row["projection"])
        contract_hash = contract_hashes.get(
            (projection.series_key.contract_id, projection.contract_version)
        )
        differing_projection_fields = [
            name
            for name in DatasetRevision.model_fields
            if getattr(projection, name) != getattr(catalog_projection, name)
        ]
        if (
            differing_projection_fields
            or projection.dataset_revision_id != revision_id
            or projection.owner_user_id != owner
            or projection.manifest_uri != row["manifest_uri"]
            or projection.parent_revision_id != row["parent_revision_id"]
            or projection.parent_depth != row["parent_depth"]
            or contract_hash != document["contract_projection_sha256"]
        ):
            raise ValueError(
                "manifest and catalog projection differ: " + ",".join(differing_projection_fields)
            )
        if (
            target_interval is not None
            and projection.series_key.interval_seconds >= target_interval
        ):
            raise ValueError("aggregate dependency interval is not smaller")
        catalog_partitions = partitions_by_revision.get(revision_id, [])
        if len(catalog_partitions) != len(projection.partition_refs):
            raise ValueError("manifest partition count differs from catalog")
        object_bars: list[CompletedBar] = []
        for ordinal, (ref, catalog_object) in enumerate(
            zip(projection.partition_refs, catalog_partitions, strict=True)
        ):
            allowed_object_state = (allowed_seed_state or allowed_closure_state) and catalog_object[
                "state"
            ] == "quarantined"
            if (
                catalog_object["ordinal"] != ordinal
                or catalog_object["object_id"] != ref.object_id
                or catalog_object["series_id"] != row["series_id"]
                or catalog_object["uri"] != ref.uri
                or catalog_object["sha256"] != ref.sha256
                or catalog_object["byte_length"] != ref.byte_length
                or catalog_object["row_count"] != ref.row_count
                or catalog_object["min_start_at"] != ref.min_start_at
                or catalog_object["max_end_at"] != ref.max_end_at
                or catalog_object["min_source_revision"] != ref.min_source_revision
                or catalog_object["max_source_revision"] != ref.max_source_revision
                or (catalog_object["state"] != "published" and not allowed_object_state)
            ):
                raise ValueError("manifest object metadata differs from catalog")
            partition_bars = verified_objects.get(ref.object_id)
            if partition_bars is None:
                parquet_bytes = store.read_verified(owner, ref.uri, ref.sha256, ref.byte_length)
                partition_bars = [
                    _bar(item) for item in pl.read_parquet(io.BytesIO(parquet_bytes)).to_dicts()
                ]
                verified_objects[ref.object_id] = partition_bars
                if heartbeat is not None:
                    heartbeat()
            if len(partition_bars) != ref.row_count or partition_bars != sorted(
                partition_bars, key=lambda item: (item.start_at, item.bar_record_id)
            ):
                raise ValueError("Parquet rows violate manifest ordering or count")
            object_bars.extend(partition_bars)
        selected = [CompletedBar.model_validate(item) for item in document["selected_bars"]]
        if object_bars != selected:
            raise ValueError("Parquet rows differ from manifest selected bars")
        catalog_selected = selected_by_revision.get(revision_id, [])
        if catalog_selected != [bar.bar_record_id for bar in selected]:
            raise ValueError("manifest selection differs from catalog")
        closure_ids = {revision_id}
        if projection.parent_revision_id is not None:
            parent_uri = document.get("parent_manifest_uri")
            parent_sha = document.get("parent_manifest_sha256")
            parent = revision_rows.get(projection.parent_revision_id)
            if (
                parent is None
                or parent_uri != parent["manifest_uri"]
                or parent_sha != parent["manifest_sha256"]
            ):
                raise ValueError("parent locator differs from catalog")
            closure_ids.update(verify(projection.parent_revision_id))
        for record in document.get("correction_chain_records", []):
            for component in record.get("aggregate_components", []):
                dependency_id = component["source_dataset_revision_id"]
                dependency = revision_rows.get(dependency_id)
                if (
                    dependency is None
                    or component["source_manifest_uri"] != dependency["manifest_uri"]
                    or component["source_manifest_sha256"] != dependency["manifest_sha256"]
                ):
                    raise ValueError("aggregate dependency locator differs from catalog")
                closure_ids.update(
                    verify(dependency_id, target_interval=projection.series_key.interval_seconds)
                )
        visiting.remove(revision_id)
        verified[revision_id] = (document, selected)
        verified_projections[revision_id] = projection
        closures[revision_id] = closure_ids
        unique_objects = {
            ref.object_id: ref
            for closure_revision_id in closure_ids
            for ref in verified_projections[closure_revision_id].partition_refs
        }
        if (
            projection.restore_closure_revision_count != len(closure_ids)
            or projection.restore_closure_row_count
            != sum(len(verified[item][1]) for item in closure_ids)
            or projection.restore_closure_bytes
            != sum(ref.byte_length for ref in unique_objects.values())
        ):
            raise ValueError("manifest reconstruction closure summaries differ")
        return closure_ids

    verify(seed_revision_id)
    if set(verified) != captured_ids:
        raise ValueError("catalog and manifest reconstruction closures differ")
    revision_count = len(verified)
    row_count = sum(len(item[1]) for item in verified.values())
    unique_referenced_objects = {
        (owner, ref.object_id): ref
        for projection in verified_projections.values()
        for ref in projection.partition_refs
    }
    referenced_bytes = sum(ref.byte_length for ref in unique_referenced_objects.values())
    if revision_count > 1000 or row_count > 2_000_000 or referenced_bytes > 10 * 1024**3:
        raise ValueError("reconstruction DAG exceeds bounds")
    return verified[seed_revision_id][1]


def _catalog_reconstruction_ids(
    connection: Any,
    owner: str,
    seed_revision_ids: set[str],
    *,
    allow_quarantined_seeds: set[str] | None = None,
    allow_integrity_quarantined_closure: bool = False,
) -> set[str]:
    allowed_quarantined = allow_quarantined_seeds or set()
    closure: set[str] = set()
    frontier = set(seed_revision_ids)
    while frontier:
        if len(closure | frontier) > 1000:
            raise ValueError("reconstruction DAG exceeds bounds")
        batch = tuple(sorted(frontier))
        rows = list(
            connection.execute(
                select(
                    dataset_revisions.c.dataset_revision_id,
                    dataset_revisions.c.parent_revision_id,
                    dataset_revisions.c.status,
                    dataset_revisions.c.projection,
                    dataset_revisions.c.published_at,
                    dataset_revisions.c.record_version,
                ).where(
                    and_(
                        dataset_revisions.c.owner_user_id == owner,
                        dataset_revisions.c.dataset_revision_id.in_(batch),
                    )
                )
            ).mappings()
        )
        if len(rows) != len(batch) or any(
            row["status"] != "published"
            and not (
                row["dataset_revision_id"] in allowed_quarantined
                and _is_integrity_quarantined_revision(row)
            )
            and not (
                allow_integrity_quarantined_closure and _is_integrity_quarantined_revision(row)
            )
            for row in rows
        ):
            raise ValueError("reconstruction DAG contains an unavailable revision")
        aggregate_ids = set(
            connection.execute(
                select(aggregate_components.c.source_dataset_revision_id)
                .join(
                    revision_bars,
                    and_(
                        revision_bars.c.owner_user_id == aggregate_components.c.owner_user_id,
                        revision_bars.c.bar_record_id
                        == aggregate_components.c.derived_bar_record_id,
                    ),
                )
                .where(
                    and_(
                        revision_bars.c.owner_user_id == owner,
                        revision_bars.c.dataset_revision_id.in_(batch),
                    )
                )
            ).scalars()
        )
        closure.update(batch)
        frontier = (
            aggregate_ids | {row["parent_revision_id"] for row in rows if row["parent_revision_id"]}
        ) - closure
    return closure


def _is_integrity_quarantined_revision(row: Any) -> bool:
    projection = row.get("projection") if hasattr(row, "get") else None
    return bool(
        row["status"] == "quarantined"
        and row["published_at"] is not None
        and row["record_version"] == 3
        and isinstance(projection, dict)
        and projection.get("status") == "published"
        and projection.get("record_version") == 2
    )


def _quarantine_dependency_closure(
    catalog: MarketDataCatalog, owner: str, seed_revision_id: str
) -> tuple[str, ...]:
    """Build, fence, recheck, and quarantine the complete dependency closure."""

    expansion = text(
        """
        WITH candidates AS (
          SELECT r.dataset_revision_id
            FROM market_data_dataset_revisions r
            JOIN ft05_corruption_work w ON w.dataset_revision_id=r.parent_revision_id
           WHERE r.owner_user_id=:owner
          UNION
          SELECT rb.dataset_revision_id
            FROM market_data_revision_bars rb
            JOIN market_data_aggregate_components ac
              ON ac.owner_user_id=rb.owner_user_id
             AND ac.derived_bar_record_id=rb.bar_record_id
            JOIN ft05_corruption_work w
              ON w.dataset_revision_id=ac.source_dataset_revision_id
           WHERE rb.owner_user_id=:owner
        ), page AS (
          SELECT DISTINCT candidate.dataset_revision_id
            FROM candidates candidate
            LEFT JOIN ft05_corruption_work existing
              ON existing.dataset_revision_id=candidate.dataset_revision_id
           WHERE existing.dataset_revision_id IS NULL
           ORDER BY dataset_revision_id
           LIMIT 1000
        )
        INSERT INTO ft05_corruption_work(dataset_revision_id)
        SELECT dataset_revision_id FROM page
        ON CONFLICT DO NOTHING
        """
    )

    class _ClosureChanged(Exception):
        pass

    for attempt in range(3):
        try:
            with (
                catalog.engine.connect().execution_options(
                    isolation_level="SERIALIZABLE"
                ) as connection,
                connection.begin(),
            ):
                connection.execute(
                    text(
                        "CREATE TEMP TABLE ft05_corruption_work "
                        "(dataset_revision_id varchar(36) PRIMARY KEY) ON COMMIT DROP"
                    )
                )
                connection.execute(
                    text("INSERT INTO ft05_corruption_work VALUES (:seed) ON CONFLICT DO NOTHING"),
                    {"seed": seed_revision_id},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO ft05_corruption_work
                        SELECT DISTINCT shared.dataset_revision_id
                          FROM market_data_revision_partitions seed
                          JOIN market_data_revision_partitions shared
                            ON shared.owner_user_id=seed.owner_user_id
                           AND shared.object_id=seed.object_id
                         WHERE seed.owner_user_id=:owner
                           AND seed.dataset_revision_id=:seed
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {"owner": owner, "seed": seed_revision_id},
                )
                while connection.execute(expansion, {"owner": owner}).rowcount:
                    pass
                closure = tuple(
                    connection.execute(
                        text(
                            "SELECT dataset_revision_id FROM ft05_corruption_work "
                            "ORDER BY dataset_revision_id"
                        )
                    ).scalars()
                )
                affected_publications = tuple(
                    connection.execute(
                        select(publications)
                        .join(
                            publication_revisions,
                            and_(
                                publication_revisions.c.owner_user_id
                                == publications.c.owner_user_id,
                                publication_revisions.c.publication_id
                                == publications.c.publication_id,
                            ),
                        )
                        .where(
                            and_(
                                publications.c.owner_user_id == owner,
                                publication_revisions.c.dataset_revision_id.in_(closure),
                            )
                        )
                        .distinct()
                        .order_by(publications.c.operation, publications.c.idempotency_key)
                    ).mappings()
                )
                for publication in affected_publications:
                    root_operation = (
                        "dataset.publish"
                        if publication["operation"] == "publish"
                        else "dataset.recover_quarantined_latest"
                    )
                    connection.execute(
                        select(idempotency)
                        .where(
                            and_(
                                idempotency.c.owner_user_id == owner,
                                idempotency.c.operation == root_operation,
                                idempotency.c.idempotency_key == publication["idempotency_key"],
                            )
                        )
                        .with_for_update()
                    ).first()
                affected_series = tuple(
                    connection.execute(
                        select(dataset_revisions.c.series_id)
                        .where(
                            and_(
                                dataset_revisions.c.owner_user_id == owner,
                                dataset_revisions.c.dataset_revision_id.in_(closure),
                            )
                        )
                        .distinct()
                        .order_by(dataset_revisions.c.series_id)
                    ).scalars()
                )
                fences = tuple(
                    connection.execute(
                        select(series_fences)
                        .where(
                            and_(
                                series_fences.c.owner_user_id == owner,
                                series_fences.c.series_id.in_(affected_series),
                            )
                        )
                        .order_by(series_fences.c.series_id)
                        .with_for_update()
                    ).mappings()
                )
                # No edge or affected series may appear between discovery and fencing.
                if connection.execute(expansion, {"owner": owner}).rowcount:
                    raise _ClosureChanged
                stable_series = tuple(
                    connection.execute(
                        select(dataset_revisions.c.series_id)
                        .where(
                            and_(
                                dataset_revisions.c.owner_user_id == owner,
                                dataset_revisions.c.dataset_revision_id.in_(
                                    select(text("dataset_revision_id")).select_from(
                                        text("ft05_corruption_work")
                                    )
                                ),
                            )
                        )
                        .distinct()
                        .order_by(dataset_revisions.c.series_id)
                    ).scalars()
                )
                if stable_series != affected_series:
                    raise _ClosureChanged
                now = catalog._now()
                holder = str(uuid7())
                fresh_fences: dict[str, int] = {}
                for fence in fences:
                    fresh_fences[fence["series_id"]] = int(fence["fencing_token"]) + 1
                    connection.execute(
                        update(series_fences)
                        .where(
                            and_(
                                series_fences.c.owner_user_id == owner,
                                series_fences.c.series_id == fence["series_id"],
                            )
                        )
                        .values(
                            fencing_token=fence["fencing_token"] + 1,
                            holder=holder,
                            lease_expires_at=now,
                        )
                    )
                connection.execute(
                    update(dataset_revisions)
                    .where(
                        and_(
                            dataset_revisions.c.owner_user_id == owner,
                            dataset_revisions.c.dataset_revision_id.in_(closure),
                            dataset_revisions.c.status.in_(("building", "published")),
                        )
                    )
                    .values(
                        status="quarantined",
                        record_version=case((dataset_revisions.c.status == "building", 2), else_=3),
                    )
                )
                publication_ids = tuple(p["publication_id"] for p in affected_publications)
                if publication_ids:
                    for publication in affected_publications:
                        connection.execute(
                            update(publications)
                            .where(
                                and_(
                                    publications.c.owner_user_id == owner,
                                    publications.c.publication_id == publication["publication_id"],
                                    publications.c.state.in_(("staged", "published")),
                                )
                            )
                            .values(
                                state="quarantined",
                                safe_reason="ARCHIVE_INTEGRITY",
                                fencing_token=fresh_fences[publication["series_id"]],
                                updated_at=now,
                            )
                        )
                    connection.execute(
                        update(publication_files)
                        .where(
                            and_(
                                publication_files.c.owner_user_id == owner,
                                publication_files.c.publication_id.in_(publication_ids),
                            )
                        )
                        .values(state="quarantined")
                    )
                    connection.execute(
                        update(publication_retention_conversions)
                        .where(
                            and_(
                                publication_retention_conversions.c.owner_user_id == owner,
                                publication_retention_conversions.c.publication_id.in_(
                                    publication_ids
                                ),
                                publication_retention_conversions.c.state == "planned",
                            )
                        )
                        .values(state="cancelled")
                    )
                object_ids = tuple(
                    connection.execute(
                        select(revision_partitions.c.object_id)
                        .where(
                            and_(
                                revision_partitions.c.owner_user_id == owner,
                                revision_partitions.c.dataset_revision_id.in_(closure),
                            )
                        )
                        .distinct()
                    ).scalars()
                )
                if object_ids:
                    connection.execute(
                        update(archive_objects)
                        .where(
                            and_(
                                archive_objects.c.owner_user_id == owner,
                                archive_objects.c.object_id.in_(object_ids),
                                archive_objects.c.state == "published",
                            )
                        )
                        .values(state="quarantined")
                    )
                return closure
        except _ClosureChanged:
            if attempt == 2:
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "Corruption closure changed while fencing.",
                    409,
                    retryable=True,
                ) from None
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) != "40001" or attempt == 2:
                raise
    raise AssertionError("unreachable")


class MarketDataReader:
    def __init__(
        self, catalog: MarketDataCatalog, store: ArchiveStore, *, context_is_current: Any
    ) -> None:
        self.catalog, self.store, self.context_is_current = catalog, store, context_is_current

    def _inject(self, point: str) -> None:
        pass

    def _auth(self, context: UserContext) -> None:
        if not self.context_is_current(context):
            raise unauthenticated()
        if "data:read" not in context.scopes:
            raise AccessError(ErrorCode.INSUFFICIENT_SCOPE, "The data:read scope is required.", 403)

    def _range(self, start: datetime, end: datetime) -> None:
        if start.tzinfo is None or end <= start or end - start > timedelta(days=366):
            raise MarketDataError(MarketDataCode.VALIDATION_ERROR, "Invalid read range.", 422)

    def _archive_rows(
        self, c: Any, owner: str, revision_id: str, *, include_history: bool = False
    ) -> list[CompletedBar]:
        rev = (
            c.execute(
                select(dataset_revisions).where(
                    and_(
                        dataset_revisions.c.owner_user_id == owner,
                        dataset_revisions.c.dataset_revision_id == revision_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if not rev:
            raise not_found()
        if rev["status"] != "published":
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY, "Dataset revision is not readable.", 500
            )
        try:
            selected = _archive_rows_from_revision(self.store, owner, dict(rev))
            if not include_history:
                return selected
            manifest = self.store.read_verified(
                owner,
                rev["manifest_uri"],
                rev["manifest_sha256"],
                rev["manifest_byte_length"],
            )
            document = json.loads(manifest)
            historical = [
                CompletedBar.model_validate(item["completed_bar"])
                for item in document.get("correction_chain_records", [])
            ]
            return list({bar.bar_record_id: bar for bar in [*selected, *historical]}.values())
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
            _quarantine_dependency_closure(self.catalog, owner, revision_id)
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "Dataset correction history is invalid.",
                500,
            ) from error
        except MarketDataError as error:
            if error.code == MarketDataCode.ARCHIVE_INTEGRITY:
                _quarantine_dependency_closure(self.catalog, owner, revision_id)
            raise

    def _archive_snapshot_references(
        self, c: Any, owner: str, revision_id: str
    ) -> dict[str, dict[str, Any]]:
        revision = (
            c.execute(
                select(dataset_revisions).where(
                    and_(
                        dataset_revisions.c.owner_user_id == owner,
                        dataset_revisions.c.dataset_revision_id == revision_id,
                    )
                )
            )
            .mappings()
            .one()
        )
        manifest_bytes = self.store.read_verified(
            owner,
            revision["manifest_uri"],
            revision["manifest_sha256"],
            revision["manifest_byte_length"],
        )
        document = json.loads(manifest_bytes)
        references: dict[str, dict[str, Any]] = {}
        for partition in document["partition_refs"]:
            parquet_bytes = self.store.read_verified(
                owner,
                partition["uri"],
                partition["sha256"],
                partition["byte_length"],
            )
            temp = self.store.write_temp(owner, parquet_bytes, "snapshot-read")
            try:
                for ordinal, row in enumerate(pl.read_parquet(temp).to_dicts()):
                    references[row["bar_record_id"]] = {
                        "origin_kind": "archive_object",
                        "dataset_revision_id": revision_id,
                        "object_id": partition["object_id"],
                        "source_ordinal": ordinal,
                        "active_marker": False,
                    }
            finally:
                temp.unlink(missing_ok=True)
        for ordinal, record in enumerate(document.get("correction_chain_records", [])):
            references.setdefault(
                record["bar_record_id"],
                {
                    "origin_kind": "archive_chain",
                    "dataset_revision_id": revision_id,
                    "object_id": None,
                    "source_ordinal": ordinal,
                    "active_marker": False,
                },
            )
        return references

    @contextmanager
    def _read_transaction(self) -> Any:
        with (
            self.catalog.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection,
            connection.begin(),
        ):
            yield connection

    def _decode_snapshot_row(
        self,
        connection: Any,
        owner: str,
        row: Any,
        object_cache: dict[str, list[CompletedBar]],
        manifest_cache: dict[str, dict[str, Any]],
    ) -> CompletedBar:
        if row["origin_kind"] == "active":
            active = (
                connection.execute(
                    select(active_bars).where(
                        and_(
                            active_bars.c.owner_user_id == owner,
                            active_bars.c.bar_record_id == row["bar_record_id"],
                        )
                    )
                )
                .mappings()
                .first()
            )
            if active is None:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "A frozen active selection is unavailable.",
                    500,
                )
            decoded = _bar(dict(active))
        elif row["origin_kind"] == "archive_object":
            object_id = cast(str, row["object_id"])
            if object_id not in object_cache:
                archived = (
                    connection.execute(
                        select(archive_objects).where(
                            and_(
                                archive_objects.c.owner_user_id == owner,
                                archive_objects.c.object_id == object_id,
                                archive_objects.c.state == "published",
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if archived is None:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "A frozen archive object is unavailable.",
                        500,
                    )
                data = self.store.read_verified(
                    owner, archived["uri"], archived["sha256"], archived["byte_length"]
                )
                temp = self.store.write_temp(owner, data, "snapshot-page")
                try:
                    object_cache[object_id] = [
                        _bar(item) for item in pl.read_parquet(temp).to_dicts()
                    ]
                finally:
                    temp.unlink(missing_ok=True)
            source_ordinal = cast(int, row["source_ordinal"])
            try:
                decoded = object_cache[object_id][source_ordinal]
            except IndexError:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "A frozen archive row reference is invalid.",
                    500,
                ) from None
        else:
            revision_id = cast(str, row["dataset_revision_id"])
            if revision_id not in manifest_cache:
                revision = (
                    connection.execute(
                        select(dataset_revisions).where(
                            and_(
                                dataset_revisions.c.owner_user_id == owner,
                                dataset_revisions.c.dataset_revision_id == revision_id,
                                dataset_revisions.c.status == "published",
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if revision is None:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "A frozen archive chain is unavailable.",
                        500,
                    )
                manifest_cache[revision_id] = json.loads(
                    self.store.read_verified(
                        owner,
                        revision["manifest_uri"],
                        revision["manifest_sha256"],
                        revision["manifest_byte_length"],
                    )
                )
            source_ordinal = cast(int, row["source_ordinal"])
            try:
                record = manifest_cache[revision_id]["correction_chain_records"][source_ordinal]
                decoded = CompletedBar.model_validate(record["completed_bar"])
            except IndexError, KeyError, TypeError, ValueError:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "A frozen archive-chain reference is invalid.",
                    500,
                ) from None
        if decoded.bar_record_id != row["bar_record_id"] or decoded.start_at != row["start_at"]:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "A frozen read reference does not match its immutable registry row.",
                500,
            )
        return decoded

    def read_bars(self, context: UserContext, request: ReadBarsRequest) -> ReadBarsResult:
        for attempt in range(2):
            try:
                return self._read_bars_once(context, request)
            except OperationalError:
                if not attempt:
                    continue
                raise MarketDataError(
                    MarketDataCode.DEPENDENCY_UNAVAILABLE,
                    "The market-data snapshot could not be completed.",
                    503,
                    retryable=True,
                ) from None
        raise AssertionError("unreachable")

    def _read_bars_once(self, context: UserContext, request: ReadBarsRequest) -> ReadBarsResult:
        self._auth(context)
        self._range(request.coverage_start, request.coverage_end)
        request_hash = hashlib.sha256(
            canonical_json_bytes(request.model_copy(update={"cursor": None, "limit": 5000}))
        ).hexdigest()
        if request.cursor is not None:
            with self._read_transaction() as c:
                header = (
                    c.execute(
                        select(read_snapshots)
                        .where(
                            and_(
                                read_snapshots.c.owner_user_id == context.user_id,
                                read_snapshots.c.read_snapshot_id == request.cursor.snapshot_id,
                            )
                        )
                        .with_for_update(read=True)
                    )
                    .mappings()
                    .first()
                )
                if (
                    not header
                    or header["state"] != "open"
                    or header["expires_at"] <= self.catalog._now()
                    or header["canonical_request_sha256"] != request_hash
                    or request.cursor.canonical_request_sha256 != request_hash
                    or request.cursor.after_ordinal < -1
                    or request.cursor.after_ordinal >= header["total_rows"]
                ):
                    raise MarketDataError(
                        MarketDataCode.STALE_VERSION, "Read cursor is stale.", 409
                    )
                self._inject("after_continuation_header_lock")
                page_rows = list(
                    c.execute(
                        select(read_snapshot_bars)
                        .where(
                            and_(
                                read_snapshot_bars.c.owner_user_id == context.user_id,
                                read_snapshot_bars.c.read_snapshot_id == request.cursor.snapshot_id,
                                read_snapshot_bars.c.ordinal > request.cursor.after_ordinal,
                            )
                        )
                        .order_by(read_snapshot_bars.c.ordinal)
                        .limit(request.limit)
                        .with_for_update(read=True)
                    ).mappings()
                )
                all_rows = list(
                    c.execute(
                        select(read_snapshot_bars)
                        .where(
                            and_(
                                read_snapshot_bars.c.owner_user_id == context.user_id,
                                read_snapshot_bars.c.read_snapshot_id == request.cursor.snapshot_id,
                            )
                        )
                        .order_by(read_snapshot_bars.c.ordinal)
                        .with_for_update(read=True)
                    ).mappings()
                )
                active_ids = sorted(
                    row["bar_record_id"] for row in all_rows if row["origin_kind"] == "active"
                )
                if active_ids:
                    tuple(
                        c.execute(
                            select(active_bars.c.bar_record_id)
                            .where(
                                and_(
                                    active_bars.c.owner_user_id == context.user_id,
                                    active_bars.c.bar_record_id.in_(active_ids),
                                )
                            )
                            .order_by(active_bars.c.bar_record_id)
                            .with_for_update(read=True)
                        ).scalars()
                    )
                revision_ids = sorted(
                    {
                        row["dataset_revision_id"]
                        for row in all_rows
                        if row["dataset_revision_id"] is not None
                    }
                )
                if revision_ids:
                    tuple(
                        c.execute(
                            select(dataset_revisions.c.dataset_revision_id)
                            .where(
                                and_(
                                    dataset_revisions.c.owner_user_id == context.user_id,
                                    dataset_revisions.c.dataset_revision_id.in_(revision_ids),
                                )
                            )
                            .order_by(dataset_revisions.c.dataset_revision_id)
                            .with_for_update(read=True)
                        ).scalars()
                    )
                object_ids = sorted(
                    {row["object_id"] for row in all_rows if row["object_id"] is not None}
                )
                if object_ids:
                    tuple(
                        c.execute(
                            select(archive_objects.c.object_id)
                            .where(
                                and_(
                                    archive_objects.c.owner_user_id == context.user_id,
                                    archive_objects.c.object_id.in_(object_ids),
                                )
                            )
                            .order_by(archive_objects.c.object_id)
                            .with_for_update(read=True)
                        ).scalars()
                    )
                object_cache: dict[str, list[CompletedBar]] = {}
                manifest_cache: dict[str, dict[str, Any]] = {}
                decoded_all = [
                    self._decode_snapshot_row(c, context.user_id, row, object_cache, manifest_cache)
                    for row in all_rows
                ]
                decoded_by_ordinal = {
                    row["ordinal"]: decoded
                    for row, decoded in zip(all_rows, decoded_all, strict=True)
                }
                decoded_page = [decoded_by_ordinal[row["ordinal"]] for row in page_rows]
                selections = tuple(
                    BarSelection(
                        bar=decoded,
                        origin="archive" if row["origin_kind"] != "active" else "active",
                        availability_at=row["availability_at"],
                        correction_observations=tuple(
                            CorrectionObservation.model_validate(item)
                            for item in row["correction_observations"]
                        ),
                    )
                    for row, decoded in zip(page_rows, decoded_page, strict=True)
                )
                last = page_rows[-1]["ordinal"] if page_rows else request.cursor.after_ordinal
                next_cursor = (
                    None
                    if last >= header["total_rows"] - 1
                    else ReadCursor(
                        snapshot_id=request.cursor.snapshot_id,
                        after_ordinal=last,
                        canonical_request_sha256=request_hash,
                    )
                )
                sr = (
                    c.execute(
                        select(series).where(
                            and_(
                                series.c.owner_user_id == context.user_id,
                                series.c.series_id == header["series_id"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                coverage = self._coverage(
                    c,
                    context,
                    sr,
                    request.coverage_start,
                    request.coverage_end,
                    decoded_all,
                    header["published_base_revision_id"]
                    if isinstance(request.policy, PinnedRead)
                    else None,
                )
                return ReadBarsResult(
                    selections=selections,
                    coverage=coverage,
                    next_cursor=next_cursor,
                    published_base_revision_id=header["published_base_revision_id"],
                )
        with self._read_transaction() as c:
            sr = (
                c.execute(
                    select(series).where(
                        and_(
                            series.c.owner_user_id == context.user_id,
                            series.c.source == request.series_key.source,
                            series.c.price_basis == request.series_key.price_basis,
                            series.c.contract_id == request.series_key.contract_id,
                            series.c.interval_seconds == request.series_key.interval_seconds,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if sr is None:
                raise not_found()
            archive_rows: list[CompletedBar] = []
            base = sr["latest_revision_id"]
            if isinstance(request.policy, PinnedRead):
                base = request.policy.dataset_revision_id
                archive_rows = self._archive_rows(c, context.user_id, base)
            elif base:
                archive_rows = self._archive_rows(
                    c,
                    context.user_id,
                    base,
                    include_history=isinstance(request.policy, CausalLatestRead),
                )
            self._inject("between_manifest_and_active_queries")
            active = []
            if not isinstance(request.policy, PinnedRead):
                active = [
                    _bar(dict(r))
                    for r in c.execute(
                        select(active_bars)
                        .where(
                            and_(
                                active_bars.c.owner_user_id == context.user_id,
                                active_bars.c.series_id == sr["series_id"],
                            )
                        )
                        .order_by(active_bars.c.bar_record_id)
                        .with_for_update(read=True)
                    ).mappings()
                ]
            observations_by_start: dict[datetime, tuple[CorrectionObservation, ...]] = {}
            if isinstance(request.policy, PinnedRead):
                candidates = archive_rows
            elif isinstance(request.policy, CausalLatestRead):
                all_versions: dict[datetime, list[CompletedBar]] = {}
                for bar in [*archive_rows, *active]:
                    all_versions.setdefault(bar.start_at, []).append(bar)
                committed = {item.start_at: item for item in request.policy.committed_selections}
                expected = expected_bar_starts(
                    self.catalog._calendar(
                        c, context.user_id, sr["calendar_id"], sr["calendar_version"]
                    ),
                    request.coverage_start,
                    request.coverage_end,
                    sr["interval_seconds"],
                )
                required = {
                    start
                    for start in expected
                    if request.policy.committed_through is not None
                    and start < request.policy.committed_through
                }
                if set(committed) != required or len(expected) > 5000:
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR,
                        "Causal commitment frontier is incomplete.",
                        422,
                    )
                chosen_causal: list[CompletedBar] = []
                for start, versions in sorted(all_versions.items()):
                    versions.sort(key=lambda bar: (bar.source_revision, bar.bar_record_id))
                    commit = committed.get(start)
                    if commit:
                        selected = next(
                            (bar for bar in versions if bar.bar_record_id == commit.bar_record_id),
                            None,
                        )
                        eligible_at_commit = [
                            bar
                            for bar in versions
                            if max(bar.end_at, bar.completed_at, bar.received_at)
                            <= commit.committed_at
                        ]
                        if (
                            selected is None
                            or not selected.end_at <= commit.committed_at <= request.policy.known_at
                            or not eligible_at_commit
                            or eligible_at_commit[-1].bar_record_id != selected.bar_record_id
                        ):
                            raise MarketDataError(
                                MarketDataCode.ARCHIVE_INTEGRITY,
                                "Invalid committed causal selection.",
                                500,
                            )
                        later = [
                            bar
                            for bar in versions
                            if bar.source_revision > selected.source_revision
                            and max(bar.end_at, bar.completed_at, bar.received_at)
                            <= request.policy.known_at
                        ]
                        observations_by_start[start] = tuple(
                            [
                                CorrectionObservation(
                                    start_at=start,
                                    old_bar_record_id=eligible_at_commit[index - 1].bar_record_id,
                                    new_bar_record_id=bar.bar_record_id,
                                    received_at=bar.received_at,
                                    applied=True,
                                    reason="BEFORE_CURSOR_APPLIED",
                                )
                                for index, bar in enumerate(eligible_at_commit)
                                if index > 0
                            ]
                            + [
                                CorrectionObservation(
                                    start_at=start,
                                    old_bar_record_id=versions[index - 1].bar_record_id,
                                    new_bar_record_id=bar.bar_record_id,
                                    received_at=bar.received_at,
                                    applied=False,
                                    reason="LATE_AFTER_CURSOR",
                                )
                                for bar in later
                                for index in [versions.index(bar)]
                                if index > 0
                            ]
                        )
                    else:
                        eligible = [
                            bar
                            for bar in versions
                            if max(bar.end_at, bar.completed_at, bar.received_at)
                            <= request.policy.known_at
                        ]
                        if not eligible:
                            continue
                        selected = eligible[-1]
                        observations_by_start[start] = tuple(
                            CorrectionObservation(
                                start_at=start,
                                old_bar_record_id=eligible[index - 1].bar_record_id,
                                new_bar_record_id=bar.bar_record_id,
                                received_at=bar.received_at,
                                applied=True,
                                reason="BEFORE_CURSOR_APPLIED",
                            )
                            for index, bar in enumerate(eligible)
                            if index > 0
                        )
                    chosen_causal.append(selected)
                candidates = chosen_causal
            else:
                by = {(b.start_at): b for b in archive_rows}
                for b in active:
                    current = by.get(b.start_at)
                    if current is None or b.source_revision > current.source_revision:
                        by[b.start_at] = b
                    elif (
                        b.source_revision == current.source_revision
                        and b.payload_hash != current.payload_hash
                    ):
                        raise MarketDataError(
                            MarketDataCode.DUPLICATE_CONFLICT,
                            "Archive and active versions conflict.",
                            409,
                        )
                candidates = list(by.values())
            chosen = sorted(
                (
                    b
                    for b in candidates
                    if request.coverage_start <= b.start_at < request.coverage_end
                ),
                key=lambda b: (b.start_at, b.source_revision, b.bar_record_id),
            )
            if len(chosen) > 500000:
                raise MarketDataError(
                    MarketDataCode.VALIDATION_ERROR, "Read selection exceeds 500000 rows.", 422
                )
            coverage = self._coverage(
                c,
                context,
                sr,
                request.coverage_start,
                request.coverage_end,
                chosen,
                base if isinstance(request.policy, PinnedRead) else None,
            )
            if (
                request.require_complete
                and coverage.quality_counts["missing"] + coverage.quality_counts["invalid"]
            ):
                raise MarketDataError(
                    MarketDataCode.STALE_DATA,
                    "Requested data is incomplete.",
                    409,
                    public_details={
                        "missing": coverage.quality_counts["missing"],
                        "invalid": coverage.quality_counts["invalid"],
                    },
                )
            selections_all = tuple(
                BarSelection(
                    bar=b,
                    origin="archive" if b in archive_rows else "active",
                    availability_at=b.end_at
                    if isinstance(request.policy, PinnedRead)
                    else max(b.end_at, b.completed_at, b.received_at),
                    correction_observations=observations_by_start.get(b.start_at, ()),
                )
                for b in chosen
            )
            archive_references = (
                self._archive_snapshot_references(c, context.user_id, base)
                if base and archive_rows
                else {}
            )
            snapshot_id = str(uuid7())
            now = self.catalog._now()
            c.execute(
                insert(read_snapshots).values(
                    owner_user_id=context.user_id,
                    read_snapshot_id=snapshot_id,
                    series_id=sr["series_id"],
                    canonical_request_sha256=request_hash,
                    policy=request.policy.model_dump(mode="json"),
                    published_base_revision_id=base,
                    created_at=now,
                    expires_at=now + timedelta(minutes=15),
                    state="open",
                    total_rows=len(selections_all),
                )
            )
            if selections_all:
                c.execute(
                    insert(read_snapshot_bars),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "read_snapshot_id": snapshot_id,
                            "ordinal": ordinal,
                            "series_id": sr["series_id"],
                            "start_at": selection.bar.start_at,
                            "bar_record_id": selection.bar.bar_record_id,
                            **(
                                {
                                    "origin_kind": "active",
                                    "dataset_revision_id": None,
                                    "object_id": None,
                                    "source_ordinal": None,
                                    "active_marker": True,
                                }
                                if selection.origin == "active"
                                else archive_references[selection.bar.bar_record_id]
                            ),
                            "availability_at": selection.availability_at,
                            "correction_observations": [
                                item.model_dump(mode="json")
                                for item in selection.correction_observations
                            ],
                        }
                        for ordinal, selection in enumerate(selections_all)
                    ],
                )
            selections = selections_all[: request.limit]
            cursor = (
                None
                if len(selections_all) <= len(selections)
                else ReadCursor(
                    snapshot_id=snapshot_id,
                    after_ordinal=len(selections) - 1,
                    canonical_request_sha256=request_hash,
                )
            )
            return ReadBarsResult(
                selections=selections,
                coverage=coverage,
                next_cursor=cursor,
                published_base_revision_id=base,
            )

    def _coverage(
        self,
        c: Any,
        context: UserContext,
        sr: Any,
        start: datetime,
        end: datetime,
        bars: list[CompletedBar],
        revision_id: str | None,
    ) -> CoverageResult:
        cal = self.catalog._calendar(c, context.user_id, sr["calendar_id"], sr["calendar_version"])
        selected = {b.start_at for b in bars}
        invalid_starts = (
            set()
            if revision_id is not None
            else set(
                c.execute(
                    select(quality_observations.c.start_at).where(
                        and_(
                            quality_observations.c.owner_user_id == context.user_id,
                            quality_observations.c.series_id == sr["series_id"],
                            quality_observations.c.start_at >= start,
                            quality_observations.c.start_at < end,
                            quality_observations.c.quality == "invalid",
                        )
                    )
                ).scalars()
            )
        )
        conflict_count = (
            0
            if revision_id is not None
            else c.execute(
                select(func.count())
                .select_from(bar_conflicts)
                .where(
                    and_(
                        bar_conflicts.c.owner_user_id == context.user_id,
                        bar_conflicts.c.series_id == sr["series_id"],
                        bar_conflicts.c.start_at >= start,
                        bar_conflicts.c.start_at < end,
                    )
                )
            ).scalar_one()
        )
        spans: list[CoverageSpan] = []
        counts = {
            "valid": 0,
            "missing": 0,
            "invalid": 0,
            "duplicate_conflict": conflict_count,
        }
        step = timedelta(seconds=sr["interval_seconds"])
        for w in cal.windows:
            left = max(start, w.start_at)
            right = min(end, w.end_at)
            if left >= right:
                continue
            if w.kind != "open":
                closed_kind: Literal["maintenance", "scheduled_closed"] = w.kind
                closed_reason: Literal["MAINTENANCE", "SCHEDULED_CLOSED"] = (
                    "MAINTENANCE" if w.kind == "maintenance" else "SCHEDULED_CLOSED"
                )
                spans.append(
                    CoverageSpan(
                        start_at=left,
                        end_at=right,
                        kind=closed_kind,
                        reason=closed_reason,
                        selected_bar_count=0,
                    )
                )
                continue
            cursor = w.start_at
            while cursor < right:
                if cursor >= left:
                    exists = cursor in selected
                    invalid = not exists and cursor in invalid_starts
                    kind: Literal["open", "missing", "invalid"] = (
                        "open" if exists else "invalid" if invalid else "missing"
                    )
                    reason: Literal["OPEN", "NO_VALID_BAR", "INVALID_BAR"] = (
                        "OPEN" if exists else "INVALID_BAR" if invalid else "NO_VALID_BAR"
                    )
                    counts["valid" if exists else "invalid" if invalid else "missing"] += 1
                    spans.append(
                        CoverageSpan(
                            start_at=cursor,
                            end_at=min(cursor + step, right),
                            kind=kind,
                            reason=reason,
                            selected_bar_count=int(exists),
                        )
                    )
                cursor += step
        coalesced: list[CoverageSpan] = []
        for span in spans:
            if (
                coalesced
                and coalesced[-1].end_at == span.start_at
                and (coalesced[-1].kind, coalesced[-1].reason) == (span.kind, span.reason)
            ):
                prior = coalesced.pop()
                coalesced.append(
                    prior.model_copy(
                        update={
                            "end_at": span.end_at,
                            "selected_bar_count": prior.selected_bar_count
                            + span.selected_bar_count,
                        }
                    )
                )
            else:
                coalesced.append(span)
        if len(coalesced) > 10000:
            raise MarketDataError(
                MarketDataCode.VALIDATION_ERROR,
                "Coverage exceeds 10000 spans.",
                422,
                public_details={"max_spans": 10000},
            )
        return CoverageResult(
            spans=tuple(coalesced), quality_counts=counts, dataset_revision_id=revision_id
        )

    def read_coverage(self, context: UserContext, request: CoverageRequest) -> CoverageResult:
        policy = (
            PinnedRead(dataset_revision_id=request.dataset_revision_id)
            if request.dataset_revision_id
            else LatestRead()
        )
        result = self.read_bars(
            context,
            ReadBarsRequest(
                series_key=request.series_key,
                coverage_start=request.coverage_start,
                coverage_end=request.coverage_end,
                policy=policy,
                limit=5000,
            ),
        )
        return result.coverage

    def sweep_expired_snapshots(self) -> ReadSnapshotSweepResult:
        now = self.catalog._now()
        with self.catalog.engine.begin() as c:
            if not c.execute(select(func.pg_try_advisory_xact_lock(0x4654534E))).scalar_one():
                return ReadSnapshotSweepResult(
                    expired_snapshot_count=0,
                    deleted_snapshot_row_count=0,
                    pending_cleanup_publication_ids=(),
                )
            owners = tuple(c.execute(select(users.c.user_id)).scalars())
            expired = (
                c.execute(
                    select(
                        read_snapshots.c.owner_user_id,
                        read_snapshots.c.read_snapshot_id,
                    )
                    .where(
                        and_(
                            read_snapshots.c.owner_user_id.in_(owners),
                            read_snapshots.c.expires_at <= now,
                        )
                    )
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            deleted_rows = 0
            for snapshot in expired:
                c.execute(
                    update(read_snapshots)
                    .where(
                        and_(
                            read_snapshots.c.owner_user_id == snapshot["owner_user_id"],
                            read_snapshots.c.read_snapshot_id == snapshot["read_snapshot_id"],
                        )
                    )
                    .values(state="expired")
                )
                deleted_rows += int(
                    c.execute(
                        delete(read_snapshot_bars).where(
                            and_(
                                read_snapshot_bars.c.owner_user_id == snapshot["owner_user_id"],
                                read_snapshot_bars.c.read_snapshot_id
                                == snapshot["read_snapshot_id"],
                            )
                        )
                    ).rowcount
                    or 0
                )
                c.execute(
                    delete(read_snapshots).where(
                        and_(
                            read_snapshots.c.owner_user_id == snapshot["owner_user_id"],
                            read_snapshots.c.read_snapshot_id == snapshot["read_snapshot_id"],
                        )
                    )
                )
            pending = tuple(
                c.execute(
                    select(publication_cleanup_bars.c.publication_id)
                    .where(publication_cleanup_bars.c.cleaned_at.is_(None))
                    .distinct()
                    .order_by(publication_cleanup_bars.c.publication_id)
                ).scalars()
            )
        return ReadSnapshotSweepResult(
            expired_snapshot_count=len(expired),
            deleted_snapshot_row_count=deleted_rows,
            pending_cleanup_publication_ids=pending,
        )


class _LeaseHeartbeatState:
    """Thread-visible cancellation state shared with one bounded work section."""

    def __init__(self) -> None:
        self.cancelled = Event()
        self.failure: BaseException | None = None

    def fail(self, error: BaseException) -> None:
        if self.failure is None:
            self.failure = error
            self.cancelled.set()

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure


class DatasetPublisher:
    def __init__(
        self,
        catalog: MarketDataCatalog,
        store: ArchiveStore,
        *,
        worker_id: str,
        lease_seconds: int = 120,
        renew_seconds: int = 30,
    ) -> None:
        self.catalog, self.store, self.worker_id = catalog, store, worker_id
        self.lease_seconds, self.renew_seconds = lease_seconds, renew_seconds
        self._last_lease_renewal: dict[tuple[str, str, str, str, int], datetime] = {}

    def _inject(self, point: str) -> None:
        """Deterministic no-op seam used by crash-contract tests."""

    def _write_named_temp(
        self,
        owner: str,
        name: str,
        data: bytes,
        *,
        check_fence: Callable[[], None] | None = None,
    ) -> Path:
        check = check_fence or (lambda: None)
        check()
        path = self.store._owner_root(owner) / "staging" / name
        check()
        with path.open("xb", buffering=0) as stream:
            check()
            path.chmod(0o600)
            check()
            view = memoryview(data)
            for offset in range(0, len(view), 1024 * 1024):
                chunk = view[offset : offset + 1024 * 1024]
                written = 0
                while written < len(chunk):
                    check()
                    count = stream.write(chunk[written:])
                    if count is None or count <= 0:
                        raise OSError("Archive temp write made no progress.")
                    written += count
                    self._inject("after_temp_write_chunk")
                    check()
            stream.flush()
            check()
            os.fsync(stream.fileno())
            check()
        self.store._assert_owner_only_mode(path, 0o600)
        check()
        return path

    def _take_fence(self, connection: Any, owner: str, series_id: str) -> int:
        row = (
            connection.execute(
                select(series_fences)
                .where(
                    and_(
                        series_fences.c.owner_user_id == owner,
                        series_fences.c.series_id == series_id,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        token = row["fencing_token"] + 1
        connection.execute(
            update(series_fences)
            .where(
                and_(
                    series_fences.c.owner_user_id == owner,
                    series_fences.c.series_id == series_id,
                )
            )
            .values(
                fencing_token=token,
                holder=self.worker_id,
                lease_expires_at=self.catalog._now() + timedelta(seconds=self.lease_seconds),
            )
        )
        return int(token)

    def _renew_lease(
        self,
        owner: str,
        series_id: str,
        operation: str,
        idempotency_key: str,
        fencing_token: int,
    ) -> None:
        now = self.catalog._now()
        expires = now + timedelta(seconds=self.lease_seconds)
        with self.catalog.engine.begin() as connection:
            root = (
                connection.execute(
                    select(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == owner,
                            idempotency.c.operation == operation,
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            fence = (
                connection.execute(
                    select(series_fences)
                    .where(
                        and_(
                            series_fences.c.owner_user_id == owner,
                            series_fences.c.series_id == series_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                root["state"] != "started"
                or root["holder"] != self.worker_id
                or root["lease_expires_at"] is None
                or root["lease_expires_at"] <= now
                or fence["fencing_token"] != fencing_token
                or fence["holder"] != self.worker_id
                or fence["lease_expires_at"] <= now
            ):
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "Publication lease was displaced.",
                    409,
                    retryable=True,
                )
            connection.execute(
                update(idempotency)
                .where(
                    and_(
                        idempotency.c.owner_user_id == owner,
                        idempotency.c.operation == operation,
                        idempotency.c.idempotency_key == idempotency_key,
                    )
                )
                .values(lease_expires_at=expires, updated_at=now)
            )
            connection.execute(
                update(series_fences)
                .where(
                    and_(
                        series_fences.c.owner_user_id == owner,
                        series_fences.c.series_id == series_id,
                    )
                )
                .values(lease_expires_at=expires)
            )
            connection.execute(
                update(prestage_writes)
                .where(
                    and_(
                        prestage_writes.c.owner_user_id == owner,
                        prestage_writes.c.series_id == series_id,
                        prestage_writes.c.idempotency_key == idempotency_key,
                        prestage_writes.c.state == "writing",
                    )
                )
                .values(lease_expires_at=expires)
            )
        self._inject("lease_renewed")

    def _heartbeat_lease(
        self,
        owner: str,
        series_id: str,
        operation: str,
        idempotency_key: str,
        fencing_token: int,
        *,
        force: bool = False,
    ) -> None:
        heartbeat_key = (owner, series_id, operation, idempotency_key, fencing_token)
        now = self.catalog._now()
        previous = self._last_lease_renewal.get(heartbeat_key)
        if force or previous is None or now - previous >= timedelta(seconds=self.renew_seconds):
            self._renew_lease(owner, series_id, operation, idempotency_key, fencing_token)
            self._last_lease_renewal[heartbeat_key] = self.catalog._now()

    @contextmanager
    def _continuous_lease_heartbeat(
        self,
        owner: str,
        series_id: str,
        operation: str,
        idempotency_key: str,
        fencing_token: int,
    ) -> Any:
        """Renew a live lease while one filesystem or codec call cannot yield."""

        stop = Event()
        state = _LeaseHeartbeatState()
        poll_seconds = min(1.0, max(0.05, self.renew_seconds / 4))

        def renew_until_stopped() -> None:
            while not stop.wait(poll_seconds):
                try:
                    self._heartbeat_lease(
                        owner,
                        series_id,
                        operation,
                        idempotency_key,
                        fencing_token,
                        force=True,
                    )
                except BaseException as error:  # noqa: BLE001 - transferred to caller thread
                    state.fail(error)
                    self._inject("lease_lost")
                    stop.set()
                    return

        thread = Thread(target=renew_until_stopped, daemon=True)
        thread.start()
        body_error: BaseException | None = None
        try:
            state.check()
            yield state
        except BaseException as error:  # noqa: BLE001 - preserve heartbeat precedence
            body_error = error
        finally:
            stop.set()
            thread.join(timeout=max(5.0, poll_seconds * 2))
        if thread.is_alive():
            raise RuntimeError("Lease heartbeat worker did not stop safely.")
        if state.failure is not None:
            if body_error is not None and body_error is not state.failure:
                raise state.failure from body_error
            raise state.failure
        if body_error is not None:
            raise body_error.with_traceback(body_error.__traceback__)

    def publish(
        self, context: UserContext, request: PublicationRequest, *, idempotency_key: str
    ) -> PublicationResult:
        self.catalog._context(context)
        self.catalog._key(idempotency_key)
        publication_id, revision_id, object_id = str(uuid7()), str(uuid7()), str(uuid7())
        effective_parent = request.expected_parent_revision_id
        terminal_error: AccessError | MarketDataError | None = None
        resume_publication_id: str | None = None
        fencing_token: int | None = None
        with self.catalog.engine.begin() as root_transaction:
            replay = self.catalog._idempotent(
                root_transaction, context, "dataset.publish", idempotency_key, request
            )
            if replay:
                result = PublicationResult.model_validate(replay)
                return result.model_copy(update={"replayed": True})
        with (
            self.catalog.engine.connect().execution_options(
                isolation_level="SERIALIZABLE"
            ) as admission,
            admission.begin(),
        ):
            series_filter = and_(
                series.c.owner_user_id == context.user_id,
                series.c.source == request.series_key.source,
                series.c.price_basis == request.series_key.price_basis,
                series.c.contract_id == request.series_key.contract_id,
                series.c.interval_seconds == request.series_key.interval_seconds,
            )
            series_identity = admission.execute(
                select(series.c.series_id).where(series_filter)
            ).scalar_one_or_none()
            if series_identity is None:
                raise not_found()
            staged_root_keys = tuple(
                admission.execute(
                    select(publications.c.idempotency_key)
                    .where(
                        and_(
                            publications.c.owner_user_id == context.user_id,
                            publications.c.series_id == series_identity,
                            publications.c.state == "staged",
                        )
                    )
                    .order_by(publications.c.idempotency_key)
                ).scalars()
            )
            root_keys = tuple(sorted(set(staged_root_keys) | {idempotency_key}))
            locked_roots = list(
                admission.execute(
                    select(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == context.user_id,
                            idempotency.c.operation == "dataset.publish",
                            idempotency.c.idempotency_key.in_(root_keys),
                        )
                    )
                    .order_by(idempotency.c.operation, idempotency.c.idempotency_key)
                    .with_for_update()
                ).mappings()
            )
            root = next(row for row in locked_roots if row["idempotency_key"] == idempotency_key)
            fence_row = (
                admission.execute(
                    select(series_fences)
                    .where(
                        and_(
                            series_fences.c.owner_user_id == context.user_id,
                            series_fences.c.series_id == series_identity,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            staged_publication_ids = tuple(
                admission.execute(
                    select(publications.c.publication_id)
                    .where(
                        and_(
                            publications.c.owner_user_id == context.user_id,
                            publications.c.series_id == series_identity,
                            publications.c.state == "staged",
                        )
                    )
                    .order_by(publications.c.publication_id)
                ).scalars()
            )
            locked_publications = list(
                admission.execute(
                    select(publications)
                    .where(publications.c.publication_id.in_(staged_publication_ids))
                    .order_by(publications.c.publication_id)
                    .with_for_update()
                ).mappings()
            )
            admitted_series = (
                admission.execute(select(series).where(series_filter).with_for_update())
                .mappings()
                .one()
            )
            stable_publications = tuple(
                admission.execute(
                    select(publications.c.publication_id)
                    .where(
                        and_(
                            publications.c.owner_user_id == context.user_id,
                            publications.c.series_id == series_identity,
                            publications.c.state == "staged",
                        )
                    )
                    .order_by(publications.c.publication_id)
                ).scalars()
            )
            if stable_publications != staged_publication_ids:
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "Publication admission changed while locks were acquired.",
                    409,
                    retryable=True,
                )
            current_parent = admitted_series["latest_revision_id"]
            now = self.catalog._now()
            roots_by_key = {row["idempotency_key"]: row for row in locked_roots}
            competing = [
                {**publication, **roots_by_key[publication["idempotency_key"]]}
                for publication in locked_publications
                if publication["idempotency_key"] != idempotency_key
                and roots_by_key[publication["idempotency_key"]]["state"] == "started"
            ]
            if any(
                competitor["lease_expires_at"] is not None and competitor["lease_expires_at"] > now
                for competitor in competing
            ):
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "A publication attempt already has a live lease.",
                    409,
                    retryable=True,
                )
            for competitor in competing:
                stale_publication_id = competitor["publication_id"]
                admission.execute(
                    update(publications)
                    .where(publications.c.publication_id == stale_publication_id)
                    .values(
                        state="quarantined",
                        safe_reason="DISPLACED",
                        fencing_token=int(fence_row["fencing_token"]) + 1,
                        updated_at=now,
                    )
                )
                admission.execute(
                    update(publication_retention_conversions)
                    .where(
                        and_(
                            publication_retention_conversions.c.publication_id
                            == stale_publication_id,
                            publication_retention_conversions.c.state == "planned",
                        )
                    )
                    .values(state="cancelled")
                )
                if competitor["rebase_count"] >= 3:
                    admission.execute(
                        update(idempotency)
                        .where(
                            and_(
                                idempotency.c.owner_user_id == context.user_id,
                                idempotency.c.operation == "dataset.publish",
                                idempotency.c.idempotency_key == competitor["idempotency_key"],
                            )
                        )
                        .values(
                            state="failed",
                            error={
                                "code": MarketDataCode.CONFLICT.value,
                                "message": "Publication was displaced too many times.",
                                "http_status": 409,
                                "retryable": False,
                            },
                            holder=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
                else:
                    admission.execute(
                        update(idempotency)
                        .where(
                            and_(
                                idempotency.c.owner_user_id == context.user_id,
                                idempotency.c.operation == "dataset.publish",
                                idempotency.c.idempotency_key == competitor["idempotency_key"],
                            )
                        )
                        .values(
                            current_publication_id=None,
                            attempt_generation=competitor["attempt_generation"] + 1,
                            rebase_count=competitor["rebase_count"] + 1,
                            holder=None,
                            lease_expires_at=None,
                            updated_at=now,
                        )
                    )
            root_filter = and_(
                idempotency.c.owner_user_id == context.user_id,
                idempotency.c.operation == "dataset.publish",
                idempotency.c.idempotency_key == idempotency_key,
            )
            if root["parent_admitted_at"] is None:
                if current_parent != request.expected_parent_revision_id:
                    terminal_error = MarketDataError(
                        MarketDataCode.STALE_VERSION, "Publication parent is stale.", 409
                    )
                    admission.execute(
                        update(idempotency)
                        .where(root_filter)
                        .values(
                            state="failed",
                            error={
                                "code": terminal_error.code.value,
                                "message": terminal_error.message,
                                "http_status": terminal_error.http_status,
                                "retryable": terminal_error.retryable,
                            },
                            updated_at=now,
                        )
                    )
                else:
                    admission.execute(
                        update(idempotency)
                        .where(root_filter)
                        .values(
                            current_publication_id=publication_id,
                            parent_admitted_at=now,
                            admitted_parent_revision_id=current_parent,
                            holder=self.worker_id,
                            lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                        )
                    )
            elif (
                root["lease_expires_at"] is not None
                and root["lease_expires_at"] > now
                and root["holder"] != self.worker_id
            ):
                terminal_error = MarketDataError(
                    MarketDataCode.CONFLICT,
                    "A publication attempt already has a live lease.",
                    409,
                    retryable=True,
                )
            else:
                old_publication_id = root["current_publication_id"]
                if old_publication_id:
                    old_state = admission.execute(
                        select(publications.c.state).where(
                            and_(
                                publications.c.owner_user_id == context.user_id,
                                publications.c.publication_id == old_publication_id,
                            )
                        )
                    ).scalar()
                    if old_state == "staged" and root["holder"] == self.worker_id:
                        resume_publication_id = old_publication_id
                    elif old_state == "staged":
                        admission.execute(
                            update(publications)
                            .where(publications.c.publication_id == old_publication_id)
                            .values(
                                state="quarantined",
                                safe_reason="DISPLACED",
                                fencing_token=int(fence_row["fencing_token"]) + 1,
                                updated_at=now,
                            )
                        )
                        admission.execute(
                            update(publication_retention_conversions)
                            .where(
                                and_(
                                    publication_retention_conversions.c.publication_id
                                    == old_publication_id,
                                    publication_retention_conversions.c.state == "planned",
                                )
                            )
                            .values(state="cancelled")
                        )
                if resume_publication_id is None:
                    rebase = current_parent != root["admitted_parent_revision_id"]
                    if rebase and root["rebase_count"] >= 3:
                        terminal_error = MarketDataError(
                            MarketDataCode.CONFLICT,
                            "Publication was displaced too many times.",
                            409,
                        )
                        admission.execute(
                            update(idempotency)
                            .where(root_filter)
                            .values(
                                state="failed",
                                error={
                                    "code": terminal_error.code.value,
                                    "message": terminal_error.message,
                                    "http_status": terminal_error.http_status,
                                    "retryable": False,
                                },
                                holder=None,
                                lease_expires_at=None,
                                updated_at=now,
                            )
                        )
                    else:
                        effective_parent = current_parent
                        admission.execute(
                            update(idempotency)
                            .where(root_filter)
                            .values(
                                current_publication_id=publication_id,
                                attempt_generation=root["attempt_generation"] + 1,
                                rebase_count=root["rebase_count"] + (1 if rebase else 0),
                                holder=self.worker_id,
                                lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                                updated_at=now,
                            )
                        )
            if terminal_error is None and resume_publication_id is None:
                fencing_token = int(fence_row["fencing_token"]) + 1
                admission.execute(
                    update(series_fences)
                    .where(
                        and_(
                            series_fences.c.owner_user_id == context.user_id,
                            series_fences.c.series_id == series_identity,
                        )
                    )
                    .values(
                        fencing_token=fencing_token,
                        holder=self.worker_id,
                        lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                    )
                )
        if terminal_error is not None:
            raise terminal_error
        if resume_publication_id is not None:
            recovered = self.reconcile_one(resume_publication_id)
            if recovered.terminal_state != "published":
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Publication could not be resumed safely.",
                    500,
                )
            with self.catalog.engine.begin() as connection:
                saved = connection.execute(
                    select(idempotency.c.result).where(
                        and_(
                            idempotency.c.owner_user_id == context.user_id,
                            idempotency.c.operation == "dataset.publish",
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one()
            return PublicationResult.model_validate(saved).model_copy(update={"replayed": True})
        if fencing_token is None:
            raise MarketDataError(
                MarketDataCode.DEPENDENCY_UNAVAILABLE,
                "Publication control transaction did not allocate a fence.",
                503,
                retryable=True,
            )
        with self.catalog.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as c:
            sr = c.execute(select(series).where(series_filter)).mappings().one()
            if sr["latest_revision_id"] != effective_parent:
                raise MarketDataError(
                    MarketDataCode.STALE_VERSION, "Publication parent is stale.", 409
                )
            active_rows = [
                _bar(dict(r))
                for r in c.execute(
                    select(active_bars)
                    .where(
                        and_(
                            active_bars.c.owner_user_id == context.user_id,
                            active_bars.c.series_id == sr["series_id"],
                            active_bars.c.start_at >= request.coverage_start,
                            active_bars.c.start_at < request.coverage_end,
                        )
                    )
                    .order_by(active_bars.c.start_at, active_bars.c.source_revision)
                ).mappings()
            ]
            registry_rows = {
                row["bar_record_id"]: row
                for row in c.execute(
                    select(bar_versions).where(
                        and_(
                            bar_versions.c.owner_user_id == context.user_id,
                            bar_versions.c.series_id == sr["series_id"],
                        )
                    )
                ).mappings()
            }
            parent_revision: DatasetRevision | None = None
            parent_rows: list[CompletedBar] = []
            if sr["latest_revision_id"]:
                parent_row = (
                    c.execute(
                        select(dataset_revisions).where(
                            and_(
                                dataset_revisions.c.owner_user_id == context.user_id,
                                dataset_revisions.c.dataset_revision_id == sr["latest_revision_id"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if parent_row["status"] != "published":
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "A quarantined revision cannot parent publication.",
                        500,
                    )
                parent_revision = DatasetRevision.model_validate(parent_row["projection"])
            calendar = self.catalog._calendar(
                c, context.user_id, sr["calendar_id"], sr["calendar_version"]
            )
            contract_row = (
                c.execute(
                    select(contract_versions).where(
                        and_(
                            contract_versions.c.owner_user_id == context.user_id,
                            contract_versions.c.contract_id == sr["contract_id"],
                            contract_versions.c.contract_version == sr["contract_version"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            contract_projection = FuturesContract.model_validate(
                {name: contract_row[name] for name in FuturesContract.model_fields}
            )
            expected = expected_bar_starts(
                calendar,
                request.coverage_start,
                request.coverage_end,
                sr["interval_seconds"],
            )
            open_windows = [window for window in calendar.windows if window.kind == "open"]
            requested_days = {
                window.trading_day
                for window in open_windows
                if window.start_at < request.coverage_end and window.end_at > request.coverage_start
            }
            requested_day_windows = [
                window for window in open_windows if window.trading_day in requested_days
            ]
            if (
                not 1 <= len(requested_days) <= 31
                or not requested_day_windows
                or request.coverage_start
                != min(window.start_at for window in requested_day_windows)
                or request.coverage_end != max(window.end_at for window in requested_day_windows)
                or any(
                    window.start_at < request.coverage_start or window.end_at > request.coverage_end
                    for window in requested_day_windows
                )
            ):
                raise MarketDataError(
                    MarketDataCode.VALIDATION_ERROR,
                    "Publication coverage must contain one through 31 complete trading days.",
                    422,
                )

            def trading_day_for(start_at: datetime) -> Any:
                match = next(
                    (
                        window.trading_day
                        for window in open_windows
                        if window.start_at <= start_at < window.end_at
                    ),
                    None,
                )
                if match is None:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "A selected bar is outside its materialized calendar.",
                        500,
                    )
                return match

            self._inject("after_stale_takeover_commit_before_snapshot")

            def heartbeat() -> None:
                self._heartbeat_lease(
                    context.user_id,
                    sr["series_id"],
                    "dataset.publish",
                    idempotency_key,
                    fencing_token,
                )

            def check_fence() -> None:
                self._heartbeat_lease(
                    context.user_id,
                    sr["series_id"],
                    "dataset.publish",
                    idempotency_key,
                    fencing_token,
                    force=True,
                )

            def continuous_heartbeat() -> Any:
                return self._continuous_lease_heartbeat(
                    context.user_id,
                    sr["series_id"],
                    "dataset.publish",
                    idempotency_key,
                    fencing_token,
                )

            if parent_revision is not None:
                try:
                    with continuous_heartbeat():
                        parent_rows = _verify_catalog_reconstruction_dag(
                            c,
                            self.store,
                            context.user_id,
                            parent_revision.dataset_revision_id,
                            heartbeat=heartbeat,
                        )
                except (MarketDataError, KeyError, TypeError, ValueError) as error:
                    if isinstance(error, MarketDataError) and error.code is not (
                        MarketDataCode.ARCHIVE_INTEGRITY
                    ):
                        raise
                    _quarantine_dependency_closure(
                        self.catalog,
                        context.user_id,
                        parent_revision.dataset_revision_id,
                    )
                    if isinstance(error, MarketDataError):
                        raise
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Publication parent reconstruction closure is invalid.",
                        500,
                    ) from error
            by = {bar.start_at: bar for bar in parent_rows}
            for b in active_rows:
                by[b.start_at] = b
            rows = sorted(
                by.values(),
                key=lambda bar: (bar.start_at, bar.source_revision, bar.bar_record_id),
            )
            versions_by_start: dict[datetime, dict[str, CompletedBar]] = {}
            for bar in [*parent_rows, *active_rows]:
                versions_by_start.setdefault(bar.start_at, {})[bar.bar_record_id] = bar
            correction_refs: list[CorrectionRef] = []
            correction_chain_records: list[dict[str, Any]] = []
            for selected_bar in rows:
                chain = sorted(
                    (
                        bar
                        for bar in versions_by_start.get(selected_bar.start_at, {}).values()
                        if bar.source_revision <= selected_bar.source_revision
                    ),
                    key=lambda bar: (bar.source_revision, bar.bar_record_id),
                )
                for index, chain_bar in enumerate(chain):
                    registry = registry_rows.get(chain_bar.bar_record_id)
                    correction_chain_records.append(
                        {
                            "completed_bar": chain_bar.model_dump(mode="json"),
                            "payload_sha256": chain_bar.payload_hash,
                            "correction_reason": None
                            if registry is None
                            else registry["correction_reason"],
                            "aggregate_lineage_sha256": None
                            if registry is None
                            else registry["aggregate_lineage_sha256"],
                            "version_fingerprint_sha256": None
                            if registry is None
                            else registry["version_fingerprint_sha256"],
                            "bar_record_id": chain_bar.bar_record_id,
                            "source_revision": chain_bar.source_revision,
                            "received_at": chain_bar.received_at,
                            "supersedes_bar_record_id": chain_bar.supersedes_bar_record_id,
                        }
                    )
                    if index and chain_bar.bar_record_id == selected_bar.bar_record_id:
                        reason = None if registry is None else registry["correction_reason"]
                        if reason is None:
                            raise MarketDataError(
                                MarketDataCode.ARCHIVE_INTEGRITY,
                                "Selected correction has no immutable reason.",
                                500,
                            )
                        correction_refs.append(
                            CorrectionRef.model_validate(
                                {
                                    "logical_bar_key": LogicalBarKey(
                                        owner_user_id=context.user_id,
                                        source=request.series_key.source,
                                        price_basis=request.series_key.price_basis,
                                        contract_id=request.series_key.contract_id,
                                        interval_seconds=request.series_key.interval_seconds,
                                        start_at=selected_bar.start_at,
                                    ),
                                    "old_bar_record_id": chain[index - 1].bar_record_id,
                                    "new_bar_record_id": chain_bar.bar_record_id,
                                    "reason": reason,
                                    "received_at": chain_bar.received_at,
                                }
                            )
                        )
            selected_request_rows = [
                bar for bar in rows if request.coverage_start <= bar.start_at < request.coverage_end
            ]
            if not selected_request_rows:
                raise MarketDataError(
                    MarketDataCode.STALE_DATA, "No complete bars to publish.", 409
                )
            if [bar.start_at for bar in selected_request_rows] != list(expected):
                raise MarketDataError(
                    MarketDataCode.STALE_DATA,
                    "Publication coverage contains an open-session gap.",
                    409,
                )
            revision_coverage_start = (
                min(request.coverage_start, parent_revision.coverage_start)
                if parent_revision
                else request.coverage_start
            )
            revision_coverage_end = (
                max(request.coverage_end, parent_revision.coverage_end)
                if parent_revision
                else request.coverage_end
            )
            union_expected = expected_bar_starts(
                calendar,
                revision_coverage_start,
                revision_coverage_end,
                sr["interval_seconds"],
            )
            if [bar.start_at for bar in rows] != list(union_expected):
                raise MarketDataError(
                    MarketDataCode.STALE_DATA,
                    "Publication union contains an open-session gap.",
                    409,
                )

            # Load the parent's frozen correction payload so cleaned intervening versions
            # remain available when a child freezes its complete reconstruction chain.
            parent_document: dict[str, Any] | None = None
            if parent_revision is not None:
                parent_manifest = self.store.read_verified(
                    context.user_id,
                    parent_row["manifest_uri"],
                    parent_row["manifest_sha256"],
                    parent_row["manifest_byte_length"],
                )
                parent_document = json.loads(parent_manifest)
                for item in parent_document.get("correction_chain_records", []):
                    chain_bar = CompletedBar.model_validate(item["completed_bar"])
                    versions_by_start.setdefault(chain_bar.start_at, {})[
                        chain_bar.bar_record_id
                    ] = chain_bar

            published_bar_ids = set(
                c.execute(
                    select(revision_bars.c.bar_record_id)
                    .join(
                        dataset_revisions,
                        and_(
                            dataset_revisions.c.owner_user_id == revision_bars.c.owner_user_id,
                            dataset_revisions.c.dataset_revision_id
                            == revision_bars.c.dataset_revision_id,
                        ),
                    )
                    .where(
                        and_(
                            revision_bars.c.owner_user_id == context.user_id,
                            dataset_revisions.c.series_id == sr["series_id"],
                            dataset_revisions.c.status == "published",
                        )
                    )
                ).scalars()
            )
            active_ids = {bar.bar_record_id for bar in active_rows}
            final_by_start = {bar.start_at: bar for bar in rows}
            frontier = {bar.start_at: bar for bar in parent_rows}
            for start, candidates in versions_by_start.items():
                if start not in frontier and start in final_by_start:
                    frontier[start] = min(
                        candidates.values(),
                        key=lambda bar: (bar.source_revision, bar.bar_record_id),
                    )

            planned: list[tuple[list[CompletedBar], Literal["rollover", "preserve", "final"]]] = []

            def frozen_frontier() -> list[CompletedBar]:
                return sorted(frontier.values(), key=lambda bar: bar.start_at)

            if any(
                bar.bar_record_id in active_ids
                and bar.bar_record_id not in published_bar_ids
                and bar.source_revision < final_by_start[start].source_revision
                for start, bar in frontier.items()
            ):
                planned.append((frozen_frontier(), "preserve"))
            while True:
                eligible: list[CompletedBar] = []
                for start, current_bar in frontier.items():
                    final_bar = final_by_start[start]
                    if current_bar.source_revision >= final_bar.source_revision:
                        continue
                    next_bar = next(
                        (
                            bar
                            for bar in sorted(
                                versions_by_start[start].values(),
                                key=lambda value: (value.source_revision, value.bar_record_id),
                            )
                            if bar.source_revision == current_bar.source_revision + 1
                        ),
                        None,
                    )
                    if next_bar is None:
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "A correction chain is not consecutive.",
                            500,
                        )
                    if next_bar.source_revision < final_bar.source_revision:
                        eligible.append(next_bar)
                if not eligible:
                    break
                advance = min(
                    eligible,
                    key=lambda bar: (
                        bar.received_at,
                        bar.start_at,
                        bar.source_revision,
                        bar.bar_record_id,
                    ),
                )
                frontier[advance.start_at] = advance
                if advance.bar_record_id not in published_bar_ids:
                    planned.append((frozen_frontier(), "preserve"))

            rollover = bool(
                parent_revision
                and (
                    parent_revision.parent_depth >= 999
                    or parent_revision.restore_closure_revision_count + len(planned) + 1 > 1000
                    or parent_revision.restore_closure_row_count
                    + sum(len(selection) for selection, _ in planned)
                    + len(rows)
                    > 2_000_000
                    or parent_revision.restore_closure_bytes >= 10 * 1024**3
                )
            )
            if rollover:
                planned.insert(0, (sorted(parent_rows, key=lambda bar: bar.start_at), "rollover"))
            planned.append((rows, "final"))
            if len(planned) > 1000 or any(len(selection) > 2_000_000 for selection, _ in planned):
                raise MarketDataError(
                    MarketDataCode.VALIDATION_ERROR, "Publication closure exceeds bounds.", 422
                )
            physical_rows = sum(len(selection) for selection, _ in planned)
            if physical_rows > (4_000_000 if rollover else 2_000_000):
                raise MarketDataError(
                    MarketDataCode.VALIDATION_ERROR, "Publication write exceeds bounds.", 422
                )

            now = self.catalog._now()
            key = OwnedSeriesKey(owner_user_id=context.user_id, **request.series_key.model_dump())
            parent_refs_by_day: dict[Any, PartitionRef] = {}
            if parent_revision is not None:
                for ref in parent_revision.partition_refs:
                    day = trading_day_for(ref.min_start_at)
                    day_windows = [window for window in open_windows if window.trading_day == day]
                    if ref.max_end_at <= max(window.end_at for window in day_windows):
                        parent_refs_by_day[day] = ref
            artifacts: list[dict[str, Any]] = []
            prior_id = sr["latest_revision_id"]
            prior_manifest_uri = parent_revision.manifest_uri if parent_revision else None
            prior_manifest_sha = parent_revision.manifest_sha256 if parent_revision else None
            prior_depth = -1 if parent_revision is None else parent_revision.parent_depth
            prior_count = (
                0 if parent_revision is None else parent_revision.restore_closure_revision_count
            )
            prior_rows = 0 if parent_revision is None else parent_revision.restore_closure_row_count
            prior_bytes = 0 if parent_revision is None else parent_revision.restore_closure_bytes
            known_database_closure = (
                set()
                if parent_revision is None
                else _catalog_reconstruction_ids(
                    c, context.user_id, {parent_revision.dataset_revision_id}
                )
            )
            known_object_ids = (
                set(
                    c.execute(
                        select(revision_partitions.c.object_id).where(
                            and_(
                                revision_partitions.c.owner_user_id == context.user_id,
                                revision_partitions.c.dataset_revision_id.in_(
                                    tuple(known_database_closure)
                                ),
                            )
                        )
                    ).scalars()
                )
                if known_database_closure
                else set()
            )
            planned_bar_ids = {
                chain_bar.bar_record_id
                for selection, _ in planned
                for selected_bar in selection
                for chain_bar in versions_by_start[selected_bar.start_at].values()
                if chain_bar.source_revision <= selected_bar.source_revision
            }
            aggregate_lineage_by_bar: dict[str, list[dict[str, Any]]] = {
                bar_record_id: [] for bar_record_id in planned_bar_ids
            }
            if planned_bar_ids:
                component_rows = list(
                    c.execute(
                        select(
                            aggregate_components.c.derived_bar_record_id,
                            aggregate_components.c.ordinal,
                            aggregate_components.c.source_bar_record_id,
                            aggregate_components.c.source_dataset_revision_id,
                            dataset_revisions.c.manifest_uri.label("source_manifest_uri"),
                            dataset_revisions.c.manifest_sha256.label("source_manifest_sha256"),
                            dataset_revisions.c.manifest_byte_length.label(
                                "source_manifest_byte_length"
                            ),
                        )
                        .join(
                            dataset_revisions,
                            and_(
                                dataset_revisions.c.owner_user_id
                                == aggregate_components.c.owner_user_id,
                                dataset_revisions.c.dataset_revision_id
                                == aggregate_components.c.source_dataset_revision_id,
                            ),
                        )
                        .where(
                            and_(
                                aggregate_components.c.owner_user_id == context.user_id,
                                aggregate_components.c.derived_bar_record_id.in_(
                                    tuple(planned_bar_ids)
                                ),
                            )
                        )
                        .order_by(
                            aggregate_components.c.derived_bar_record_id,
                            aggregate_components.c.ordinal,
                        )
                    ).mappings()
                )
                for component in component_rows:
                    aggregate_lineage_by_bar[component["derived_bar_record_id"]].append(
                        {
                            name: value
                            for name, value in component.items()
                            if name != "derived_bar_record_id"
                        }
                    )
            dependency_seeds = {
                component["source_dataset_revision_id"]
                for components in aggregate_lineage_by_bar.values()
                for component in components
            }
            dependency_closure_by_seed: dict[str, set[str]] = {}
            for dependency_id in sorted(dependency_seeds):
                with continuous_heartbeat():
                    _verify_catalog_reconstruction_dag(
                        c,
                        self.store,
                        context.user_id,
                        dependency_id,
                        heartbeat=heartbeat,
                    )
                dependency_closure_by_seed[dependency_id] = _catalog_reconstruction_ids(
                    c, context.user_id, {dependency_id}
                )
            all_dependency_ids = (
                set().union(*dependency_closure_by_seed.values())
                if dependency_closure_by_seed
                else set()
            )
            dependency_row_counts: dict[str, int] = {}
            dependency_objects_by_revision: dict[str, list[dict[str, Any]]] = {}
            if all_dependency_ids:
                dependency_row_counts = {
                    revision: int(count)
                    for revision, count in c.execute(
                        select(
                            revision_bars.c.dataset_revision_id,
                            func.count(),
                        )
                        .where(
                            and_(
                                revision_bars.c.owner_user_id == context.user_id,
                                revision_bars.c.dataset_revision_id.in_(tuple(all_dependency_ids)),
                            )
                        )
                        .group_by(revision_bars.c.dataset_revision_id)
                    )
                }
                for dependency_object in c.execute(
                    select(
                        revision_partitions.c.dataset_revision_id,
                        archive_objects.c.object_id,
                        archive_objects.c.byte_length,
                    )
                    .join(
                        archive_objects,
                        and_(
                            archive_objects.c.owner_user_id == revision_partitions.c.owner_user_id,
                            archive_objects.c.object_id == revision_partitions.c.object_id,
                        ),
                    )
                    .where(
                        and_(
                            revision_partitions.c.owner_user_id == context.user_id,
                            revision_partitions.c.dataset_revision_id.in_(
                                tuple(all_dependency_ids)
                            ),
                        )
                    )
                ).mappings():
                    dependency_objects_by_revision.setdefault(
                        dependency_object["dataset_revision_id"], []
                    ).append(dict(dependency_object))
            # The repeatable-read snapshot is now fully materialized.  Parquet
            # encoding, hashing, and filesystem writes must not hold a DB transaction.
            c.commit()
            for ordinal, (selection, kind) in enumerate(planned):
                planned_revision_id = revision_id if kind == "final" else str(uuid7())
                grouped: dict[Any, list[CompletedBar]] = {}
                for bar in selection:
                    grouped.setdefault(trading_day_for(bar.start_at), []).append(bar)
                object_items: list[dict[str, Any]] = []
                partition_refs: list[PartitionRef] = []
                for partition_ordinal, (day, day_rows) in enumerate(sorted(grouped.items())):
                    if (
                        kind != "rollover"
                        and day not in requested_days
                        and day in parent_refs_by_day
                    ):
                        partition_refs.append(parent_refs_by_day[day])
                        continue
                    planned_object_id = (
                        object_id if kind == "final" and partition_ordinal == 0 else str(uuid7())
                    )
                    object_uri = f"ft-archive://object/{planned_object_id}"
                    encoded = io.BytesIO()
                    check_fence()
                    with continuous_heartbeat():
                        pl.DataFrame(
                            [bar.model_dump(mode="json") for bar in day_rows]
                        ).write_parquet(encoded, compression="zstd")
                    check_fence()
                    with continuous_heartbeat():
                        data = encoded.getvalue()
                        data_sha256 = hashlib.sha256(data).hexdigest()
                    check_fence()
                    pref = PartitionRef(
                        object_id=planned_object_id,
                        uri=object_uri,
                        sha256=data_sha256,
                        byte_length=len(data),
                        row_count=len(day_rows),
                        min_start_at=min(bar.start_at for bar in day_rows),
                        max_end_at=max(bar.end_at for bar in day_rows),
                        min_source_revision=min(bar.source_revision for bar in day_rows),
                        max_source_revision=max(bar.source_revision for bar in day_rows),
                        origin_publication_id=publication_id,
                    )
                    partition_refs.append(pref)
                    object_items.append({"object_id": planned_object_id, "data": data, "ref": pref})
                partition_refs.sort(key=lambda ref: (ref.min_start_at, ref.object_id))
                if len(partition_refs) > 2000 or (kind != "rollover" and len(object_items) > 31):
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR,
                        "Publication partition count exceeds bounds.",
                        422,
                    )
                manifest_uri = f"ft-archive://manifest/{planned_revision_id}"
                is_rollover = kind == "rollover"
                parent_id = None if is_rollover else prior_id
                depth = 0 if is_rollover else prior_depth + 1
                if is_rollover:
                    known_database_closure = set()
                    known_object_ids = {ref.object_id for ref in partition_refs}
                closure_count = 1 if is_rollover else prior_count + 1
                closure_rows = len(selection) if is_rollover else prior_rows + len(selection)
                written_bytes = sum(len(value["data"]) for value in object_items)
                closure_bytes = written_bytes if is_rollover else prior_bytes + written_bytes
                if (
                    depth > 999
                    or closure_count > 1000
                    or closure_rows > 2_000_000
                    or closure_bytes > 10 * 1024**3
                ):
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR, "Publication closure exceeds bounds.", 422
                    )
                local_correction_refs: list[CorrectionRef] = []
                local_chain_records: list[dict[str, Any]] = []
                for selected_bar in selection:
                    chain = sorted(
                        (
                            bar
                            for bar in versions_by_start[selected_bar.start_at].values()
                            if bar.source_revision <= selected_bar.source_revision
                        ),
                        key=lambda bar: (bar.source_revision, bar.bar_record_id),
                    )
                    if [bar.source_revision for bar in chain] != list(
                        range(1, selected_bar.source_revision + 1)
                    ):
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "A correction chain is not reconstructible.",
                            500,
                        )
                    for index, chain_bar in enumerate(chain):
                        registry = registry_rows[chain_bar.bar_record_id]
                        aggregate_lineage = aggregate_lineage_by_bar[chain_bar.bar_record_id]
                        local_chain_records.append(
                            {
                                "completed_bar": chain_bar.model_dump(mode="json"),
                                "payload_sha256": registry["payload_hash"],
                                "correction_reason": registry["correction_reason"],
                                "aggregate_lineage_sha256": registry["aggregate_lineage_sha256"],
                                "version_fingerprint_sha256": registry[
                                    "version_fingerprint_sha256"
                                ],
                                "bar_record_id": chain_bar.bar_record_id,
                                "source_revision": chain_bar.source_revision,
                                "received_at": chain_bar.received_at,
                                "supersedes_bar_record_id": chain_bar.supersedes_bar_record_id,
                                "aggregate_components": [dict(item) for item in aggregate_lineage],
                            }
                        )
                        if index:
                            local_correction_refs.append(
                                CorrectionRef(
                                    logical_bar_key=LogicalBarKey(
                                        owner_user_id=context.user_id,
                                        source=request.series_key.source,
                                        price_basis=request.series_key.price_basis,
                                        contract_id=request.series_key.contract_id,
                                        interval_seconds=request.series_key.interval_seconds,
                                        start_at=selected_bar.start_at,
                                    ),
                                    old_bar_record_id=chain[index - 1].bar_record_id,
                                    new_bar_record_id=chain_bar.bar_record_id,
                                    reason=registry["correction_reason"],
                                    received_at=chain_bar.received_at,
                                )
                            )
                watermark_bar = max(selection, key=lambda bar: (bar.received_at, bar.bar_record_id))
                local_dependency_seeds = {
                    component["source_dataset_revision_id"]
                    for record in local_chain_records
                    for component in record["aggregate_components"]
                }
                dependency_closure = (
                    set().union(
                        *(dependency_closure_by_seed[item] for item in local_dependency_seeds)
                    )
                    if local_dependency_seeds
                    else set()
                )
                new_dependency_ids = dependency_closure - known_database_closure
                if new_dependency_ids:
                    closure_count += len(new_dependency_ids)
                    closure_rows += sum(dependency_row_counts[item] for item in new_dependency_ids)
                    dependency_objects = {
                        item["object_id"]: item
                        for revision in new_dependency_ids
                        for item in dependency_objects_by_revision[revision]
                    }.values()
                    closure_bytes += sum(
                        item["byte_length"]
                        for item in dependency_objects
                        if item["object_id"] not in known_object_ids
                    )
                    known_object_ids.update(item["object_id"] for item in dependency_objects)
                    known_database_closure.update(new_dependency_ids)
                known_object_ids.update(ref.object_id for ref in partition_refs)
                if closure_count > 1000 or closure_rows > 2_000_000 or closure_bytes > 10 * 1024**3:
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR,
                        "Publication reconstruction DAG exceeds bounds.",
                        422,
                    )
                projection: dict[str, Any] = {
                    "schema_version": "v1",
                    "dataset_revision_id": planned_revision_id,
                    "owner_user_id": context.user_id,
                    "series_key": key.model_dump(mode="json"),
                    "contract_version": sr["contract_version"],
                    "parent_revision_id": parent_id,
                    "parent_manifest_uri": None if is_rollover else prior_manifest_uri,
                    "parent_manifest_sha256": None if is_rollover else prior_manifest_sha,
                    "manifest_uri": manifest_uri,
                    "partition_refs": [ref.model_dump(mode="json") for ref in partition_refs],
                    "calendar_id": sr["calendar_id"],
                    "calendar_version": sr["calendar_version"],
                    "coverage_start": parent_revision.coverage_start
                    if is_rollover and parent_revision
                    else revision_coverage_start,
                    "coverage_end": parent_revision.coverage_end
                    if is_rollover and parent_revision
                    else revision_coverage_end,
                    "source_watermark": {
                        "kind": "familytrade_received_v1",
                        "max_received_at": watermark_bar.received_at,
                        "max_bar_record_id": watermark_bar.bar_record_id,
                    },
                    "correction_refs": [
                        value.model_dump(mode="json") for value in local_correction_refs
                    ],
                    "status": "published",
                    "created_at": now,
                    "published_at": now,
                    "parent_depth": depth,
                    "restore_closure_revision_count": closure_count,
                    "restore_closure_row_count": closure_rows,
                    "restore_closure_bytes": closure_bytes,
                    "rollover_from_revision_id": sr["latest_revision_id"] if is_rollover else None,
                    "rollover_from_manifest_uri": parent_revision.manifest_uri
                    if is_rollover and parent_revision
                    else None,
                    "rollover_from_manifest_sha256": parent_revision.manifest_sha256
                    if is_rollover and parent_revision
                    else None,
                    "recovery_from_quarantined_revision_id": None,
                    "recovery_from_manifest_uri": None,
                    "recovery_from_manifest_sha256": None,
                    "record_version": 2,
                    "selected_bars": [bar.model_dump(mode="json") for bar in selection],
                    "correction_chain_records": local_chain_records,
                    "format_version": "ft-dataset-manifest-v1",
                    "origin_publication_id": publication_id,
                    "origin_publication_ordinal": ordinal,
                    "contract_projection": contract_projection.model_dump(mode="json"),
                    "contract_projection_sha256": canonical_sha256(contract_projection),
                    "calendar_projection": calendar.model_dump(mode="json"),
                }
                check_fence()
                with continuous_heartbeat():
                    manifest = canonical_json_bytes(projection)
                    manifest_sha256 = hashlib.sha256(manifest).hexdigest()
                check_fence()
                projection["manifest_sha256"] = manifest_sha256
                projection["manifest_byte_length"] = len(manifest)
                revision = DatasetRevision.model_validate(
                    {
                        name: value
                        for name, value in projection.items()
                        if name in DatasetRevision.model_fields
                    }
                )
                artifacts.append(
                    {
                        "kind": kind,
                        "revision": revision,
                        "selection": selection,
                        "objects": object_items,
                        "manifest": manifest,
                        "chain_records": local_chain_records,
                        "correction_refs": local_correction_refs,
                    }
                )
                prior_id, prior_depth, prior_count, prior_rows, prior_bytes = (
                    planned_revision_id,
                    depth,
                    closure_count,
                    closure_rows,
                    closure_bytes,
                )
                prior_manifest_uri = revision.manifest_uri
                prior_manifest_sha = revision.manifest_sha256
            if sum(
                sum(len(obj["data"]) for obj in item["objects"]) + len(item["manifest"])
                for item in artifacts
            ) > (20 * 1024**3 if rollover else 10 * 1024**3):
                raise MarketDataError(
                    MarketDataCode.VALIDATION_ERROR, "Publication bytes exceed bounds.", 422
                )

            temp_names: list[str] = []
            for item in artifacts:
                for obj in item["objects"]:
                    obj["temp_name"] = f"{uuid7()}.parquet.tmp"
                    temp_names.append(obj["temp_name"])
                item["manifest_temp_name"] = f"{uuid7()}.manifest.tmp"
                temp_names.append(item["manifest_temp_name"])
            temp_uuid = str(uuid7())
            with self.catalog.engine.begin() as journal:
                journal.execute(
                    insert(prestage_writes).values(
                        owner_user_id=context.user_id,
                        series_id=sr["series_id"],
                        idempotency_key=idempotency_key,
                        temp_uuid=temp_uuid,
                        holder=self.worker_id,
                        fencing_token=fencing_token,
                        lease_expires_at=self.catalog._now()
                        + timedelta(seconds=self.lease_seconds),
                        expected_temp_names=temp_names,
                        state="writing",
                    )
                )
            self._inject("before_temp_create")
            for item in artifacts:
                for obj in item["objects"]:
                    check_fence()
                    with continuous_heartbeat() as heartbeat_state:
                        self._write_named_temp(
                            context.user_id,
                            obj["temp_name"],
                            obj["data"],
                            check_fence=heartbeat_state.check,
                        )
                    self._inject("during_temp_write")
                    check_fence()
                    self._inject("after_file_fsync_before_directory_fsync")
                check_fence()
                with continuous_heartbeat() as heartbeat_state:
                    self._write_named_temp(
                        context.user_id,
                        item["manifest_temp_name"],
                        item["manifest"],
                        check_fence=heartbeat_state.check,
                    )
                check_fence()
            with continuous_heartbeat() as heartbeat_state:
                heartbeat_state.check()
                staging = self.store._owner_root(context.user_id) / "staging"
                heartbeat_state.check()
                self.store._fsync_directory(staging)
                heartbeat_state.check()
            self._inject("after_all_fsync_before_staged_tx")
            locked_parent = c.execute(
                select(series.c.latest_revision_id)
                .where(
                    and_(
                        series.c.owner_user_id == context.user_id,
                        series.c.series_id == sr["series_id"],
                    )
                )
                .with_for_update()
            ).scalar_one()
            if locked_parent != effective_parent:
                raise MarketDataError(
                    MarketDataCode.STALE_VERSION,
                    "Publication parent changed after staging.",
                    409,
                )
            if not c.execute(
                select(series_fences.c.series_id)
                .where(
                    and_(
                        series_fences.c.owner_user_id == context.user_id,
                        series_fences.c.series_id == sr["series_id"],
                        series_fences.c.fencing_token == fencing_token,
                        series_fences.c.holder == self.worker_id,
                        series_fences.c.lease_expires_at > self.catalog._now(),
                    )
                )
                .with_for_update()
            ).scalar():
                raise MarketDataError(
                    MarketDataCode.CONFLICT, "Publication lease was displaced.", 409
                )
            final_revision = artifacts[-1]["revision"]
            c.execute(
                insert(publications).values(
                    owner_user_id=context.user_id,
                    publication_id=publication_id,
                    series_id=sr["series_id"],
                    idempotency_key=idempotency_key,
                    operation="publish",
                    parent_revision_id=sr["latest_revision_id"],
                    final_candidate_revision_id=final_revision.dataset_revision_id,
                    snapshot_sha256=hashlib.sha256(
                        canonical_json_bytes([b.bar_record_id for b in rows])
                    ).hexdigest(),
                    fencing_token=fencing_token,
                    state="staged",
                    created_at=now,
                    updated_at=now,
                )
            )
            file_rows: list[dict[str, Any]] = []
            file_ordinal = 0
            for ordinal, item in enumerate(artifacts):
                revision = item["revision"]
                for obj in item["objects"]:
                    pref = obj["ref"]
                    c.execute(
                        insert(archive_objects).values(
                            owner_user_id=context.user_id,
                            object_id=obj["object_id"],
                            series_id=sr["series_id"],
                            uri=pref.uri,
                            sha256=pref.sha256,
                            byte_length=pref.byte_length,
                            row_count=pref.row_count,
                            min_start_at=pref.min_start_at,
                            max_end_at=pref.max_end_at,
                            min_source_revision=pref.min_source_revision,
                            max_source_revision=pref.max_source_revision,
                            state="staged",
                            origin_publication_id=publication_id,
                            publication_id=publication_id,
                            catalog_origin="publication",
                        )
                    )
                c.execute(
                    insert(dataset_revisions).values(
                        owner_user_id=context.user_id,
                        dataset_revision_id=revision.dataset_revision_id,
                        schema_version="v1",
                        series_id=sr["series_id"],
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
                        correction_refs=[
                            value.model_dump(mode="json") for value in revision.correction_refs
                        ],
                        status="building",
                        created_at=now,
                        published_at=None,
                        parent_depth=revision.parent_depth,
                        restore_closure_revision_count=revision.restore_closure_revision_count,
                        restore_closure_row_count=revision.restore_closure_row_count,
                        restore_closure_bytes=revision.restore_closure_bytes,
                        rollover_from_revision_id=revision.rollover_from_revision_id,
                        rollover_from_manifest_uri=revision.rollover_from_manifest_uri,
                        rollover_from_manifest_sha256=revision.rollover_from_manifest_sha256,
                        recovery_from_quarantined_revision_id=None,
                        recovery_from_manifest_uri=None,
                        recovery_from_manifest_sha256=None,
                        record_version=1,
                    )
                )
                c.execute(
                    insert(revision_partitions),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "dataset_revision_id": revision.dataset_revision_id,
                            "ordinal": partition_ordinal,
                            "object_id": ref.object_id,
                        }
                        for partition_ordinal, ref in enumerate(revision.partition_refs)
                    ],
                )
                c.execute(
                    insert(publication_revisions).values(
                        owner_user_id=context.user_id,
                        publication_id=publication_id,
                        ordinal=ordinal,
                        dataset_revision_id=revision.dataset_revision_id,
                    )
                )
                c.execute(
                    insert(revision_bars),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "dataset_revision_id": revision.dataset_revision_id,
                            "ordinal": index,
                            "series_id": sr["series_id"],
                            "start_at": bar.start_at,
                            "source_revision": bar.source_revision,
                            "bar_record_id": bar.bar_record_id,
                            "object_id": next(
                                ref.object_id
                                for ref in item["revision"].partition_refs
                                if ref.min_start_at <= bar.start_at < ref.max_end_at
                            ),
                        }
                        for index, bar in enumerate(item["selection"])
                    ],
                )
                if item["correction_refs"]:
                    c.execute(
                        insert(correction_ref_rows),
                        [
                            {
                                "owner_user_id": context.user_id,
                                "dataset_revision_id": revision.dataset_revision_id,
                                "ordinal": index,
                                "old_bar_record_id": value.old_bar_record_id,
                                "new_bar_record_id": value.new_bar_record_id,
                                "reason": value.reason,
                                "received_at": value.received_at,
                            }
                            for index, value in enumerate(item["correction_refs"])
                        ],
                    )
                for obj in item["objects"]:
                    pref = obj["ref"]
                    file_rows.append(
                        {
                            "owner_user_id": context.user_id,
                            "publication_id": publication_id,
                            "ordinal": file_ordinal,
                            "file_kind": "object",
                            "temp_name": obj["temp_name"],
                            "final_uri": pref.uri,
                            "sha256": pref.sha256,
                            "byte_length": pref.byte_length,
                            "state": "temp",
                        }
                    )
                    file_ordinal += 1
                file_rows.append(
                    {
                        "owner_user_id": context.user_id,
                        "publication_id": publication_id,
                        "ordinal": file_ordinal,
                        "file_kind": "manifest",
                        "temp_name": item["manifest_temp_name"],
                        "final_uri": revision.manifest_uri,
                        "sha256": revision.manifest_sha256,
                        "byte_length": revision.manifest_byte_length,
                        "state": "temp",
                    }
                )
                file_ordinal += 1
            c.execute(insert(publication_files), file_rows)
            c.execute(
                update(prestage_writes)
                .where(
                    and_(
                        prestage_writes.c.owner_user_id == context.user_id,
                        prestage_writes.c.series_id == sr["series_id"],
                        prestage_writes.c.idempotency_key == idempotency_key,
                        prestage_writes.c.temp_uuid == temp_uuid,
                    )
                )
                .values(state="staged")
            )
            selected_ids = {bar.bar_record_id for item in artifacts for bar in item["selection"]}
            causal_refs = (
                c.execute(
                    select(bar_retention_refs).where(
                        and_(
                            bar_retention_refs.c.owner_user_id == context.user_id,
                            bar_retention_refs.c.bar_record_id.in_(selected_ids),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for causal_ref in causal_refs:
                destination = next(
                    item["revision"].dataset_revision_id
                    for item in artifacts
                    if causal_ref["bar_record_id"]
                    in {bar.bar_record_id for bar in item["selection"]}
                )
                c.execute(
                    insert(publication_retention_conversions).values(
                        owner_user_id=context.user_id,
                        publication_id=publication_id,
                        bar_record_id=causal_ref["bar_record_id"],
                        reference_kind=causal_ref["reference_kind"],
                        reference_id=causal_ref["reference_id"],
                        dataset_revision_id=destination,
                        state="planned",
                    )
                )
            cleanup_rows = [
                {
                    "owner_user_id": context.user_id,
                    "publication_id": publication_id,
                    "bar_record_id": b.bar_record_id,
                    "cleaned_at": None,
                }
                for b in active_rows
            ]
            if cleanup_rows:
                c.execute(insert(publication_cleanup_bars), cleanup_rows)
            result = PublicationResult(
                publication_id=publication_id,
                dataset_revision=final_revision,
                preserved_revision_ids=tuple(
                    item["revision"].dataset_revision_id
                    for item in artifacts[:-1]
                    if item["kind"] == "preserve"
                ),
                replayed=False,
            )
            c.commit()
        self._inject("after_staged_commit_before_rename")
        for item in artifacts:
            for obj in item["objects"]:
                check_fence()
                with continuous_heartbeat() as heartbeat_state:
                    self.store.finalize(
                        context.user_id,
                        staging / obj["temp_name"],
                        obj["ref"].uri,
                        check_fence=heartbeat_state.check,
                    )
                check_fence()
                self._inject("during_object_renames")
            check_fence()
            with continuous_heartbeat() as heartbeat_state:
                self.store.finalize(
                    context.user_id,
                    staging / item["manifest_temp_name"],
                    item["revision"].manifest_uri,
                    check_fence=heartbeat_state.check,
                )
            check_fence()
        self._inject("after_all_renames_before_directory_fsync")
        self._inject("after_rename_fsync_before_publish_tx")
        recovered = self.reconcile_one(publication_id)
        if recovered.terminal_state != "published":
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "Publication could not be completed safely.",
                500,
            )
        self._inject("after_publish_commit_before_cleanup")
        with self.catalog.engine.begin() as completed:
            self.catalog._save_result(
                completed, context, "dataset.publish", idempotency_key, result
            )
        self.cleanup_one(publication_id)
        return result

    def restore_retained_manifest(
        self, context: UserContext, request: RetainedManifestRestoreRequest, *, idempotency_key: str
    ) -> RestoreResult:
        self.catalog._context(context)
        self.catalog._key(idempotency_key)
        with self.catalog.engine.begin() as admission:
            replay = self.catalog._idempotent(
                admission,
                context,
                "dataset.restore_retained_manifest",
                idempotency_key,
                request,
            )
            if replay:
                result = RestoreResult.model_validate(replay)
                return result.model_copy(update={"replayed": True})
        path = self.store.resolve(context.user_id, request.manifest_uri)
        try:
            manifest = path.read_bytes()
        except OSError:
            raise not_found() from None
        if hashlib.sha256(manifest).hexdigest() != request.expected_manifest_sha256:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY, "Retained manifest failed verification.", 500
            )
        import json

        try:
            document = json.loads(manifest)
            _validate_manifest_schema(document)
            _validate_manifest_correction_history(document)
            revision_payload = {
                name: document[name]
                for name in DatasetRevision.model_fields
                if name not in {"manifest_sha256", "manifest_byte_length"}
            }
            revision_payload.update(
                manifest_sha256=request.expected_manifest_sha256,
                manifest_byte_length=len(manifest),
            )
            revision = DatasetRevision.model_validate(revision_payload)
            bars = tuple(CompletedBar.model_validate(item) for item in document["selected_bars"])
            restored_calendar = CalendarVersion.model_validate(document["calendar_projection"])
            restored_contract = FuturesContract.model_validate(document["contract_projection"])
        except KeyError, ValueError, TypeError:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY, "Retained manifest schema is invalid.", 500
            ) from None
        if (
            revision.owner_user_id != context.user_id
            or revision.manifest_uri != request.manifest_uri
        ):
            raise not_found()
        closure: list[tuple[DatasetRevision, dict[str, Any], tuple[CompletedBar, ...]]] = [
            (revision, document, bars)
        ]
        seen = {revision.dataset_revision_id}
        cursor_revision, cursor_document = revision, document
        while cursor_revision.parent_revision_id is not None:
            if len(closure) >= 1000:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest closure exceeds bounds.",
                    500,
                )
            parent_uri = cursor_document.get("parent_manifest_uri")
            parent_sha = cursor_document.get("parent_manifest_sha256")
            if not isinstance(parent_uri, str) or not isinstance(parent_sha, str):
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest parent link is invalid.",
                    500,
                )
            parent_path = self.store.resolve(context.user_id, parent_uri)
            try:
                parent_bytes = parent_path.read_bytes()
            except OSError:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest parent is unavailable.",
                    500,
                ) from None
            if hashlib.sha256(parent_bytes).hexdigest() != parent_sha:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest parent failed verification.",
                    500,
                )
            try:
                parent_document = json.loads(parent_bytes)
                _validate_manifest_schema(parent_document)
                _validate_manifest_correction_history(parent_document)
                parent_payload = {
                    name: parent_document[name]
                    for name in DatasetRevision.model_fields
                    if name not in {"manifest_sha256", "manifest_byte_length"}
                }
                parent_payload.update(
                    manifest_sha256=parent_sha,
                    manifest_byte_length=len(parent_bytes),
                )
                parsed_parent = DatasetRevision.model_validate(parent_payload)
                parent_bars = tuple(
                    CompletedBar.model_validate(item) for item in parent_document["selected_bars"]
                )
            except KeyError, ValueError, TypeError:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest parent schema is invalid.",
                    500,
                ) from None
            if (
                parsed_parent.dataset_revision_id != cursor_revision.parent_revision_id
                or parsed_parent.dataset_revision_id in seen
                or parsed_parent.owner_user_id != context.user_id
                or parsed_parent.series_key != revision.series_key
                or parsed_parent.parent_depth + 1 != cursor_revision.parent_depth
            ):
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest parent chain is invalid.",
                    500,
                )
            seen.add(parsed_parent.dataset_revision_id)
            closure.append((parsed_parent, parent_document, parent_bars))
            cursor_revision, cursor_document = parsed_parent, parent_document
        closure.reverse()
        dependency_order: list[tuple[str, str, str]] = []
        dependency_cache: dict[
            str,
            tuple[DatasetRevision, dict[str, Any], list[tuple[DatasetRevision, dict[str, Any]]]],
        ] = {}
        visiting = {item[0].dataset_revision_id for item in closure}

        def load_dependency(
            dependency_uri: str,
            dependency_sha: str,
            expected_revision_id: str,
            required_bar_record_id: str,
            target_interval_seconds: int,
        ) -> None:
            cached = dependency_cache.get(expected_revision_id)
            if cached is None:
                if expected_revision_id in visiting:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Aggregate dependency graph contains a cycle.",
                        500,
                    )
                visiting.add(expected_revision_id)
                try:
                    dependency_bytes = self.store.resolve(
                        context.user_id, dependency_uri
                    ).read_bytes()
                except OSError:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Aggregate dependency manifest is unavailable.",
                        500,
                    ) from None
                if hashlib.sha256(dependency_bytes).hexdigest() != dependency_sha:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Aggregate dependency manifest failed verification.",
                        500,
                    )
                try:
                    dependency_document = json.loads(dependency_bytes)
                    _validate_manifest_schema(dependency_document)
                    _validate_manifest_correction_history(dependency_document)
                    dependency_payload = {
                        name: dependency_document[name]
                        for name in DatasetRevision.model_fields
                        if name not in {"manifest_sha256", "manifest_byte_length"}
                    }
                    dependency_payload.update(
                        manifest_sha256=dependency_sha,
                        manifest_byte_length=len(dependency_bytes),
                    )
                    dependency_revision = DatasetRevision.model_validate(dependency_payload)
                except KeyError, ValueError, TypeError:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Aggregate dependency manifest schema is invalid.",
                        500,
                    ) from None
                if (
                    dependency_revision.dataset_revision_id != expected_revision_id
                    or dependency_revision.owner_user_id != context.user_id
                    or dependency_revision.manifest_uri != dependency_uri
                    or dependency_revision.series_key.interval_seconds >= target_interval_seconds
                ):
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Aggregate dependency identity or interval is invalid.",
                        500,
                    )
                dependency_chain = [(dependency_revision, dependency_document)]
                parent_revision = dependency_revision
                parent_document = dependency_document
                parent_seen = {dependency_revision.dataset_revision_id}
                while parent_revision.parent_revision_id is not None:
                    if len(dependency_chain) >= 1000:
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "Aggregate dependency parent closure exceeds bounds.",
                            500,
                        )
                    parent_uri = parent_document.get("parent_manifest_uri")
                    parent_sha = parent_document.get("parent_manifest_sha256")
                    if not isinstance(parent_uri, str) or not isinstance(parent_sha, str):
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "Aggregate dependency parent link is invalid.",
                            500,
                        )
                    try:
                        parent_bytes = self.store.resolve(context.user_id, parent_uri).read_bytes()
                    except OSError:
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "Aggregate dependency parent is unavailable.",
                            500,
                        ) from None
                    if hashlib.sha256(parent_bytes).hexdigest() != parent_sha:
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "Aggregate dependency parent failed verification.",
                            500,
                        )
                    try:
                        parsed_document = json.loads(parent_bytes)
                        _validate_manifest_schema(parsed_document)
                        _validate_manifest_correction_history(parsed_document)
                        parsed_payload = {
                            name: parsed_document[name]
                            for name in DatasetRevision.model_fields
                            if name not in {"manifest_sha256", "manifest_byte_length"}
                        }
                        parsed_payload.update(
                            manifest_sha256=parent_sha,
                            manifest_byte_length=len(parent_bytes),
                        )
                        parsed_revision = DatasetRevision.model_validate(parsed_payload)
                    except KeyError, ValueError, TypeError:
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "Aggregate dependency parent schema is invalid.",
                            500,
                        ) from None
                    if (
                        parsed_revision.dataset_revision_id != parent_revision.parent_revision_id
                        or parsed_revision.dataset_revision_id in parent_seen
                        or parsed_revision.owner_user_id != context.user_id
                        or parsed_revision.series_key != dependency_revision.series_key
                        or parsed_revision.parent_depth + 1 != parent_revision.parent_depth
                    ):
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "Aggregate dependency parent chain is invalid.",
                            500,
                        )
                    parent_seen.add(parsed_revision.dataset_revision_id)
                    dependency_chain.append((parsed_revision, parsed_document))
                    parent_revision, parent_document = parsed_revision, parsed_document
                dependency_chain.reverse()
                for chain_revision, chain_document in dependency_chain:
                    for ref in chain_revision.partition_refs:
                        self.store.read_verified(
                            context.user_id, ref.uri, ref.sha256, ref.byte_length
                        )
                    for chain_item in chain_document.get("correction_chain_records", []):
                        for nested in chain_item.get("aggregate_components", []):
                            load_dependency(
                                nested["source_manifest_uri"],
                                nested["source_manifest_sha256"],
                                nested["source_dataset_revision_id"],
                                nested["source_bar_record_id"],
                                chain_revision.series_key.interval_seconds,
                            )
                dependency_cache[expected_revision_id] = (
                    dependency_revision,
                    dependency_document,
                    dependency_chain,
                )
                visiting.remove(expected_revision_id)
                dependency_order.append(
                    (dependency_revision.dataset_revision_id, dependency_uri, dependency_sha)
                )
                cached = dependency_cache[expected_revision_id]
            if (
                cached[0].manifest_uri != dependency_uri
                or cached[0].manifest_sha256 != dependency_sha
            ):
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Aggregate dependency locator conflicts within the manifest graph.",
                    500,
                )
            selected_ids = {item["bar_record_id"] for item in cached[1].get("selected_bars", [])}
            if required_bar_record_id not in selected_ids:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Aggregate dependency bar is not selected by its source revision.",
                    500,
                )

        for closure_revision, closure_document, _ in closure:
            for chain_item in closure_document.get("correction_chain_records", []):
                for component in chain_item.get("aggregate_components", []):
                    load_dependency(
                        component["source_manifest_uri"],
                        component["source_manifest_sha256"],
                        component["source_dataset_revision_id"],
                        component["source_bar_record_id"],
                        closure_revision.series_key.interval_seconds,
                    )
        for closure_revision, _, _ in closure:
            for ref in closure_revision.partition_refs:
                self.store.read_verified(context.user_id, ref.uri, ref.sha256, ref.byte_length)
        manifest_nodes: dict[str, tuple[DatasetRevision, dict[str, Any]]] = {
            item.dataset_revision_id: (item, item_document) for item, item_document, _ in closure
        }
        for _, _, dependency_chain in dependency_cache.values():
            for dependency_revision, dependency_document in dependency_chain:
                manifest_nodes[dependency_revision.dataset_revision_id] = (
                    dependency_revision,
                    dependency_document,
                )
        calculated_closures: dict[str, set[str]] = {}

        def calculate_closure(revision_id: str, active: set[str]) -> set[str]:
            if revision_id in calculated_closures:
                return calculated_closures[revision_id]
            if revision_id in active or revision_id not in manifest_nodes:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest reconstruction graph is incomplete or cyclic.",
                    500,
                )
            active.add(revision_id)
            node_revision, node_document = manifest_nodes[revision_id]
            result = {revision_id}
            if node_revision.parent_revision_id is not None:
                result.update(calculate_closure(node_revision.parent_revision_id, active))
            for chain_item in node_document["correction_chain_records"]:
                for component in chain_item["aggregate_components"]:
                    result.update(
                        calculate_closure(component["source_dataset_revision_id"], active)
                    )
            active.remove(revision_id)
            calculated_closures[revision_id] = result
            unique_objects = {
                ref.object_id: ref
                for member in result
                for ref in manifest_nodes[member][0].partition_refs
            }
            if (
                node_revision.restore_closure_revision_count != len(result)
                or node_revision.restore_closure_row_count
                != sum(len(manifest_nodes[member][1]["selected_bars"]) for member in result)
                or node_revision.restore_closure_bytes
                != sum(ref.byte_length for ref in unique_objects.values())
            ):
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest reconstruction summaries differ.",
                    500,
                )
            return result

        for node_id, (node_revision, node_document) in manifest_nodes.items():
            calculate_closure(node_id, set())
            decoded = _archive_rows_from_revision(
                self.store,
                context.user_id,
                {
                    "manifest_uri": node_revision.manifest_uri,
                    "manifest_sha256": node_revision.manifest_sha256,
                    "manifest_byte_length": node_revision.manifest_byte_length,
                },
            )
            expected_rows = [
                CompletedBar.model_validate(item) for item in node_document["selected_bars"]
            ]
            if decoded != expected_rows:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained manifest Parquet rows differ from its selection.",
                    500,
                )
        self._inject("restore_after_preflight_before_transaction")
        committed_result: RestoreResult | None = None
        with (
            self.catalog.engine.connect().execution_options(isolation_level="SERIALIZABLE") as c,
            c.begin(),
        ):
            existing = (
                c.execute(
                    select(dataset_revisions).where(
                        and_(
                            dataset_revisions.c.owner_user_id == context.user_id,
                            dataset_revisions.c.dataset_revision_id == revision.dataset_revision_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["manifest_sha256"] != request.expected_manifest_sha256:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY, "Revision digest conflicts.", 500
                    )
                result = RestoreResult(
                    dataset_revision=revision,
                    catalog_rows_restored=0,
                    latest_pointer_changed=False,
                    replayed=True,
                )
                self.catalog._save_result(
                    c,
                    context,
                    "dataset.restore_retained_manifest",
                    idempotency_key,
                    result,
                )
                return result
            if not c.execute(
                select(calendar_versions.c.calendar_id).where(
                    and_(
                        calendar_versions.c.owner_user_id == context.user_id,
                        calendar_versions.c.calendar_id == restored_calendar.calendar_id,
                        calendar_versions.c.calendar_version == restored_calendar.calendar_version,
                    )
                )
            ).scalar():
                c.execute(
                    insert(calendar_versions).values(
                        owner_user_id=context.user_id,
                        calendar_id=restored_calendar.calendar_id,
                        calendar_version=restored_calendar.calendar_version,
                        schema_version="v1",
                        exchange_timezone=restored_calendar.exchange_timezone,
                        coverage_start=restored_calendar.coverage_start,
                        coverage_end=restored_calendar.coverage_end,
                        metadata_as_of=restored_calendar.metadata_as_of,
                        provenance_ref=restored_calendar.provenance_ref,
                        payload_sha256=canonical_sha256(restored_calendar),
                        created_at=restored_calendar.created_at,
                        record_version=restored_calendar.record_version,
                    )
                )
                c.execute(
                    insert(calendar_windows),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "calendar_id": restored_calendar.calendar_id,
                            "calendar_version": restored_calendar.calendar_version,
                            **window.model_dump(),
                        }
                        for window in restored_calendar.windows
                    ],
                )
            contract_head = (
                c.execute(
                    select(contracts).where(
                        and_(
                            contracts.c.owner_user_id == context.user_id,
                            contracts.c.contract_id == restored_contract.contract_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if contract_head is None:
                c.execute(
                    insert(contracts).values(
                        owner_user_id=context.user_id,
                        contract_id=restored_contract.contract_id,
                        provider=restored_contract.provider,
                        provider_contract_id=restored_contract.provider_contract_id,
                        current_contract_version=restored_contract.record_version,
                        series_binding_version=restored_contract.record_version,
                    )
                )
            elif (
                contract_head["provider"] != restored_contract.provider
                or contract_head["provider_contract_id"] != restored_contract.provider_contract_id
            ):
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Retained contract identity conflicts with the catalog.",
                    500,
                )
            if not c.execute(
                select(contract_versions.c.contract_id).where(
                    and_(
                        contract_versions.c.owner_user_id == context.user_id,
                        contract_versions.c.contract_id == restored_contract.contract_id,
                        contract_versions.c.contract_version == restored_contract.record_version,
                    )
                )
            ).scalar():
                contract_values = restored_contract.model_dump()
                contract_values.update(
                    contract_version=restored_contract.record_version,
                    projection_sha256=canonical_sha256(restored_contract),
                )
                c.execute(insert(contract_versions).values(**contract_values))
            sr = self.catalog._series_for(
                c,
                context.user_id,
                SeriesKey(**revision.series_key.model_dump(exclude={"owner_user_id"})),
                create=True,
            )
            restored = 0

            def restore_dependency_revision(
                dependency_revision: DatasetRevision, dependency_document: dict[str, Any]
            ) -> None:
                nonlocal restored
                existing_dependency = c.execute(
                    select(dataset_revisions.c.manifest_sha256).where(
                        and_(
                            dataset_revisions.c.owner_user_id == context.user_id,
                            dataset_revisions.c.dataset_revision_id
                            == dependency_revision.dataset_revision_id,
                        )
                    )
                ).scalar()
                if existing_dependency is not None:
                    if existing_dependency != dependency_revision.manifest_sha256:
                        raise MarketDataError(
                            MarketDataCode.ARCHIVE_INTEGRITY,
                            "Aggregate dependency digest conflicts with catalog.",
                            500,
                        )
                    return
                dependency_calendar = CalendarVersion.model_validate(
                    dependency_document["calendar_projection"]
                )
                dependency_contract = FuturesContract.model_validate(
                    dependency_document["contract_projection"]
                )
                calendar_hash = c.execute(
                    select(calendar_versions.c.payload_sha256).where(
                        and_(
                            calendar_versions.c.owner_user_id == context.user_id,
                            calendar_versions.c.calendar_id == dependency_calendar.calendar_id,
                            calendar_versions.c.calendar_version
                            == dependency_calendar.calendar_version,
                        )
                    )
                ).scalar()
                contract_hash = c.execute(
                    select(contract_versions.c.projection_sha256).where(
                        and_(
                            contract_versions.c.owner_user_id == context.user_id,
                            contract_versions.c.contract_id == dependency_contract.contract_id,
                            contract_versions.c.contract_version
                            == dependency_contract.record_version,
                        )
                    )
                ).scalar()
                if calendar_hash != canonical_sha256(
                    dependency_calendar
                ) or contract_hash != canonical_sha256(dependency_contract):
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Aggregate dependency metadata binding is unavailable.",
                        500,
                    )
                dependency_series = self.catalog._series_for(
                    c,
                    context.user_id,
                    SeriesKey(
                        **dependency_revision.series_key.model_dump(exclude={"owner_user_id"})
                    ),
                    create=True,
                )
                for chain_item in dependency_document.get("correction_chain_records", []):
                    chain_bar = CompletedBar.model_validate(chain_item["completed_bar"])
                    insertion = c.execute(
                        pg_insert(bar_versions)
                        .values(
                            owner_user_id=context.user_id,
                            bar_record_id=chain_bar.bar_record_id,
                            schema_version="v1",
                            series_id=dependency_series["series_id"],
                            start_at=chain_bar.start_at,
                            source_revision=chain_bar.source_revision,
                            payload_hash=chain_item["payload_sha256"],
                            supersedes_bar_record_id=chain_item["supersedes_bar_record_id"],
                            correction_reason=chain_item["correction_reason"],
                            aggregate_lineage_sha256=chain_item["aggregate_lineage_sha256"],
                            version_fingerprint_sha256=chain_item["version_fingerprint_sha256"],
                            received_at=chain_bar.received_at,
                            quality="valid",
                            created_at=chain_bar.created_at,
                            record_version=1,
                        )
                        .on_conflict_do_nothing()
                    )
                    restored += insertion.rowcount
                for ref in dependency_revision.partition_refs:
                    insertion = c.execute(
                        pg_insert(archive_objects)
                        .values(
                            owner_user_id=context.user_id,
                            object_id=ref.object_id,
                            series_id=dependency_series["series_id"],
                            uri=ref.uri,
                            sha256=ref.sha256,
                            byte_length=ref.byte_length,
                            row_count=ref.row_count,
                            min_start_at=ref.min_start_at,
                            max_end_at=ref.max_end_at,
                            min_source_revision=ref.min_source_revision,
                            max_source_revision=ref.max_source_revision,
                            state="published",
                            origin_publication_id=ref.origin_publication_id,
                            publication_id=None,
                            catalog_origin="retained_manifest",
                        )
                        .on_conflict_do_nothing()
                    )
                    restored += insertion.rowcount
                c.execute(
                    insert(dataset_revisions).values(
                        owner_user_id=context.user_id,
                        dataset_revision_id=dependency_revision.dataset_revision_id,
                        schema_version="v1",
                        series_id=dependency_series["series_id"],
                        contract_version=dependency_revision.contract_version,
                        calendar_id=dependency_revision.calendar_id,
                        calendar_version=dependency_revision.calendar_version,
                        projection=dependency_revision.model_dump(mode="json"),
                        parent_revision_id=dependency_revision.parent_revision_id,
                        manifest_uri=dependency_revision.manifest_uri,
                        manifest_sha256=dependency_revision.manifest_sha256,
                        manifest_byte_length=dependency_revision.manifest_byte_length,
                        coverage_start=dependency_revision.coverage_start,
                        coverage_end=dependency_revision.coverage_end,
                        source_watermark=dependency_revision.source_watermark.model_dump(
                            mode="json"
                        ),
                        correction_refs=[
                            item.model_dump(mode="json")
                            for item in dependency_revision.correction_refs
                        ],
                        status="published",
                        created_at=dependency_revision.created_at,
                        published_at=dependency_revision.published_at,
                        parent_depth=dependency_revision.parent_depth,
                        restore_closure_revision_count=dependency_revision.restore_closure_revision_count,
                        restore_closure_row_count=dependency_revision.restore_closure_row_count,
                        restore_closure_bytes=dependency_revision.restore_closure_bytes,
                        rollover_from_revision_id=dependency_revision.rollover_from_revision_id,
                        rollover_from_manifest_uri=dependency_revision.rollover_from_manifest_uri,
                        rollover_from_manifest_sha256=dependency_revision.rollover_from_manifest_sha256,
                        recovery_from_quarantined_revision_id=dependency_revision.recovery_from_quarantined_revision_id,
                        recovery_from_manifest_uri=dependency_revision.recovery_from_manifest_uri,
                        recovery_from_manifest_sha256=dependency_revision.recovery_from_manifest_sha256,
                        record_version=dependency_revision.record_version,
                    )
                )
                restored += 1
                selected_dependency_bars = [
                    CompletedBar.model_validate(item)
                    for item in dependency_document["selected_bars"]
                ]
                if selected_dependency_bars:
                    c.execute(
                        insert(revision_bars),
                        [
                            {
                                "owner_user_id": context.user_id,
                                "dataset_revision_id": dependency_revision.dataset_revision_id,
                                "ordinal": ordinal,
                                "series_id": dependency_series["series_id"],
                                "start_at": bar.start_at,
                                "source_revision": bar.source_revision,
                                "bar_record_id": bar.bar_record_id,
                                "object_id": next(
                                    ref.object_id
                                    for ref in dependency_revision.partition_refs
                                    if ref.min_start_at <= bar.start_at < ref.max_end_at
                                ),
                            }
                            for ordinal, bar in enumerate(selected_dependency_bars)
                        ],
                    )
                    restored += len(selected_dependency_bars)
                if dependency_revision.partition_refs:
                    c.execute(
                        insert(revision_partitions),
                        [
                            {
                                "owner_user_id": context.user_id,
                                "dataset_revision_id": dependency_revision.dataset_revision_id,
                                "ordinal": ordinal,
                                "object_id": ref.object_id,
                            }
                            for ordinal, ref in enumerate(dependency_revision.partition_refs)
                        ],
                    )
                    restored += len(dependency_revision.partition_refs)
                if dependency_revision.correction_refs:
                    c.execute(
                        insert(correction_ref_rows),
                        [
                            {
                                "owner_user_id": context.user_id,
                                "dataset_revision_id": dependency_revision.dataset_revision_id,
                                "ordinal": ordinal,
                                "old_bar_record_id": item.old_bar_record_id,
                                "new_bar_record_id": item.new_bar_record_id,
                                "reason": item.reason,
                                "received_at": item.received_at,
                            }
                            for ordinal, item in enumerate(dependency_revision.correction_refs)
                        ],
                    )
                    restored += len(dependency_revision.correction_refs)
                for chain_item in dependency_document.get("correction_chain_records", []):
                    for component in chain_item.get("aggregate_components", []):
                        insertion = c.execute(
                            pg_insert(aggregate_components)
                            .values(
                                owner_user_id=context.user_id,
                                derived_bar_record_id=chain_item["bar_record_id"],
                                ordinal=component["ordinal"],
                                source_bar_record_id=component["source_bar_record_id"],
                                source_dataset_revision_id=component["source_dataset_revision_id"],
                            )
                            .on_conflict_do_nothing()
                        )
                        restored += insertion.rowcount

            restored_dependency_revisions: set[str] = set()
            for dependency_revision_id, _, _ in dependency_order:
                dependency_root, _, dependency_chain = dependency_cache[dependency_revision_id]
                for dependency_revision, dependency_document in dependency_chain:
                    if dependency_revision.dataset_revision_id in restored_dependency_revisions:
                        continue
                    restore_dependency_revision(dependency_revision, dependency_document)
                    restored_dependency_revisions.add(dependency_revision.dataset_revision_id)
                dependency_series = self.catalog._series_for(
                    c,
                    context.user_id,
                    SeriesKey(**dependency_root.series_key.model_dump(exclude={"owner_user_id"})),
                )
                if dependency_series["latest_revision_id"] is None:
                    c.execute(
                        update(series)
                        .where(
                            and_(
                                series.c.owner_user_id == context.user_id,
                                series.c.series_id == dependency_series["series_id"],
                            )
                        )
                        .values(
                            latest_revision_id=dependency_root.dataset_revision_id,
                            record_version=series.c.record_version + 1,
                        )
                    )
            for ancestor, ancestor_document, ancestor_bars in closure[:-1]:
                if c.execute(
                    select(dataset_revisions.c.dataset_revision_id).where(
                        and_(
                            dataset_revisions.c.owner_user_id == context.user_id,
                            dataset_revisions.c.dataset_revision_id == ancestor.dataset_revision_id,
                        )
                    )
                ).scalar():
                    continue
                for chain_item in ancestor_document.get("correction_chain_records", []):
                    chain_bar = CompletedBar.model_validate(chain_item["completed_bar"])
                    if not c.execute(
                        select(bar_versions.c.bar_record_id).where(
                            and_(
                                bar_versions.c.owner_user_id == context.user_id,
                                bar_versions.c.bar_record_id == chain_bar.bar_record_id,
                            )
                        )
                    ).scalar():
                        c.execute(
                            insert(bar_versions).values(
                                owner_user_id=context.user_id,
                                bar_record_id=chain_bar.bar_record_id,
                                schema_version="v1",
                                series_id=sr["series_id"],
                                start_at=chain_bar.start_at,
                                source_revision=chain_bar.source_revision,
                                payload_hash=chain_item["payload_sha256"],
                                supersedes_bar_record_id=chain_item["supersedes_bar_record_id"],
                                correction_reason=chain_item["correction_reason"],
                                aggregate_lineage_sha256=chain_item["aggregate_lineage_sha256"],
                                version_fingerprint_sha256=chain_item["version_fingerprint_sha256"],
                                received_at=chain_bar.received_at,
                                quality="valid",
                                created_at=chain_bar.created_at,
                                record_version=1,
                            )
                        )
                        restored += 1
                for ref in ancestor.partition_refs:
                    if not c.execute(
                        select(archive_objects.c.object_id).where(
                            and_(
                                archive_objects.c.owner_user_id == context.user_id,
                                archive_objects.c.object_id == ref.object_id,
                            )
                        )
                    ).scalar():
                        c.execute(
                            insert(archive_objects).values(
                                owner_user_id=context.user_id,
                                object_id=ref.object_id,
                                series_id=sr["series_id"],
                                uri=ref.uri,
                                sha256=ref.sha256,
                                byte_length=ref.byte_length,
                                row_count=ref.row_count,
                                min_start_at=ref.min_start_at,
                                max_end_at=ref.max_end_at,
                                min_source_revision=ref.min_source_revision,
                                max_source_revision=ref.max_source_revision,
                                state="published",
                                origin_publication_id=ref.origin_publication_id,
                                publication_id=None,
                                catalog_origin="retained_manifest",
                            )
                        )
                        restored += 1
                c.execute(
                    insert(dataset_revisions).values(
                        owner_user_id=context.user_id,
                        dataset_revision_id=ancestor.dataset_revision_id,
                        schema_version="v1",
                        series_id=sr["series_id"],
                        contract_version=ancestor.contract_version,
                        calendar_id=ancestor.calendar_id,
                        calendar_version=ancestor.calendar_version,
                        projection=ancestor.model_dump(mode="json"),
                        parent_revision_id=ancestor.parent_revision_id,
                        manifest_uri=ancestor.manifest_uri,
                        manifest_sha256=ancestor.manifest_sha256,
                        manifest_byte_length=ancestor.manifest_byte_length,
                        coverage_start=ancestor.coverage_start,
                        coverage_end=ancestor.coverage_end,
                        source_watermark=ancestor.source_watermark.model_dump(mode="json"),
                        correction_refs=[
                            value.model_dump(mode="json") for value in ancestor.correction_refs
                        ],
                        status="published",
                        created_at=ancestor.created_at,
                        published_at=ancestor.published_at,
                        parent_depth=ancestor.parent_depth,
                        restore_closure_revision_count=ancestor.restore_closure_revision_count,
                        restore_closure_row_count=ancestor.restore_closure_row_count,
                        restore_closure_bytes=ancestor.restore_closure_bytes,
                        rollover_from_revision_id=ancestor.rollover_from_revision_id,
                        rollover_from_manifest_uri=ancestor.rollover_from_manifest_uri,
                        rollover_from_manifest_sha256=ancestor.rollover_from_manifest_sha256,
                        recovery_from_quarantined_revision_id=ancestor.recovery_from_quarantined_revision_id,
                        recovery_from_manifest_uri=ancestor.recovery_from_manifest_uri,
                        recovery_from_manifest_sha256=ancestor.recovery_from_manifest_sha256,
                        record_version=ancestor.record_version,
                    )
                )
                restored += 1
                c.execute(
                    insert(revision_bars),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "dataset_revision_id": ancestor.dataset_revision_id,
                            "ordinal": ordinal,
                            "series_id": sr["series_id"],
                            "start_at": bar.start_at,
                            "source_revision": bar.source_revision,
                            "bar_record_id": bar.bar_record_id,
                            "object_id": next(
                                ref.object_id
                                for ref in ancestor.partition_refs
                                if ref.min_start_at <= bar.start_at < ref.max_end_at
                            ),
                        }
                        for ordinal, bar in enumerate(ancestor_bars)
                    ],
                )
                restored += len(ancestor_bars)
                c.execute(
                    insert(revision_partitions),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "dataset_revision_id": ancestor.dataset_revision_id,
                            "ordinal": ordinal,
                            "object_id": ref.object_id,
                        }
                        for ordinal, ref in enumerate(ancestor.partition_refs)
                    ],
                )
                restored += len(ancestor.partition_refs)
                if ancestor.correction_refs:
                    c.execute(
                        insert(correction_ref_rows),
                        [
                            {
                                "owner_user_id": context.user_id,
                                "dataset_revision_id": ancestor.dataset_revision_id,
                                "ordinal": ordinal,
                                "old_bar_record_id": value.old_bar_record_id,
                                "new_bar_record_id": value.new_bar_record_id,
                                "reason": value.reason,
                                "received_at": value.received_at,
                            }
                            for ordinal, value in enumerate(ancestor.correction_refs)
                        ],
                    )
                    restored += len(ancestor.correction_refs)
                for chain_item in ancestor_document.get("correction_chain_records", []):
                    for component in chain_item.get("aggregate_components", []):
                        insertion = c.execute(
                            pg_insert(aggregate_components)
                            .values(
                                owner_user_id=context.user_id,
                                derived_bar_record_id=chain_item["bar_record_id"],
                                ordinal=component["ordinal"],
                                source_bar_record_id=component["source_bar_record_id"],
                                source_dataset_revision_id=component["source_dataset_revision_id"],
                            )
                            .on_conflict_do_nothing()
                        )
                        restored += insertion.rowcount
            for chain_item in document.get("correction_chain_records", []):
                chain_bar = CompletedBar.model_validate(chain_item["completed_bar"])
                if not c.execute(
                    select(bar_versions.c.bar_record_id).where(
                        and_(
                            bar_versions.c.owner_user_id == context.user_id,
                            bar_versions.c.bar_record_id == chain_bar.bar_record_id,
                        )
                    )
                ).scalar():
                    c.execute(
                        insert(bar_versions).values(
                            owner_user_id=context.user_id,
                            bar_record_id=chain_bar.bar_record_id,
                            schema_version="v1",
                            series_id=sr["series_id"],
                            start_at=chain_bar.start_at,
                            source_revision=chain_bar.source_revision,
                            payload_hash=chain_item["payload_sha256"],
                            supersedes_bar_record_id=chain_item["supersedes_bar_record_id"],
                            correction_reason=chain_item["correction_reason"],
                            aggregate_lineage_sha256=chain_item["aggregate_lineage_sha256"],
                            version_fingerprint_sha256=chain_item["version_fingerprint_sha256"],
                            received_at=chain_bar.received_at,
                            quality="valid",
                            created_at=chain_bar.created_at,
                            record_version=1,
                        )
                    )
                    restored += 1
            for ref in revision.partition_refs:
                if c.execute(
                    select(archive_objects.c.object_id).where(
                        and_(
                            archive_objects.c.owner_user_id == context.user_id,
                            archive_objects.c.object_id == ref.object_id,
                        )
                    )
                ).scalar():
                    continue
                c.execute(
                    insert(archive_objects).values(
                        owner_user_id=context.user_id,
                        object_id=ref.object_id,
                        series_id=sr["series_id"],
                        uri=ref.uri,
                        sha256=ref.sha256,
                        byte_length=ref.byte_length,
                        row_count=ref.row_count,
                        min_start_at=ref.min_start_at,
                        max_end_at=ref.max_end_at,
                        min_source_revision=ref.min_source_revision,
                        max_source_revision=ref.max_source_revision,
                        state="published",
                        origin_publication_id=ref.origin_publication_id,
                        publication_id=None,
                        catalog_origin="retained_manifest",
                    )
                )
                restored += 1
            c.execute(
                insert(dataset_revisions).values(
                    owner_user_id=context.user_id,
                    dataset_revision_id=revision.dataset_revision_id,
                    schema_version="v1",
                    series_id=sr["series_id"],
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
                    correction_refs=[
                        item.model_dump(mode="json") for item in revision.correction_refs
                    ],
                    status="published",
                    created_at=revision.created_at,
                    published_at=revision.published_at,
                    parent_depth=revision.parent_depth,
                    restore_closure_revision_count=revision.restore_closure_revision_count,
                    restore_closure_row_count=revision.restore_closure_row_count,
                    restore_closure_bytes=revision.restore_closure_bytes,
                    rollover_from_revision_id=revision.rollover_from_revision_id,
                    rollover_from_manifest_uri=revision.rollover_from_manifest_uri,
                    rollover_from_manifest_sha256=revision.rollover_from_manifest_sha256,
                    recovery_from_quarantined_revision_id=revision.recovery_from_quarantined_revision_id,
                    recovery_from_manifest_uri=revision.recovery_from_manifest_uri,
                    recovery_from_manifest_sha256=revision.recovery_from_manifest_sha256,
                    record_version=revision.record_version,
                )
            )
            restored += 1
            for ordinal, bar in enumerate(bars):
                registry = c.execute(
                    select(bar_versions).where(
                        and_(
                            bar_versions.c.owner_user_id == context.user_id,
                            bar_versions.c.bar_record_id == bar.bar_record_id,
                        )
                    )
                ).scalar()
                if registry is None:
                    c.execute(
                        insert(bar_versions).values(
                            owner_user_id=context.user_id,
                            bar_record_id=bar.bar_record_id,
                            schema_version="v1",
                            series_id=sr["series_id"],
                            start_at=bar.start_at,
                            source_revision=bar.source_revision,
                            payload_hash=bar.payload_hash,
                            supersedes_bar_record_id=bar.supersedes_bar_record_id,
                            correction_reason=None
                            if bar.source_revision == 1
                            else "SOURCE_CORRECTION",
                            aggregate_lineage_sha256=None,
                            version_fingerprint_sha256=hashlib.sha256(
                                canonical_json_bytes(
                                    {
                                        "payload_hash": bar.payload_hash,
                                        "correction_reason": None
                                        if bar.source_revision == 1
                                        else "SOURCE_CORRECTION",
                                        "aggregate_lineage_sha256": None,
                                    }
                                )
                            ).hexdigest(),
                            received_at=bar.received_at,
                            quality="valid",
                            created_at=bar.created_at,
                            record_version=1,
                        )
                    )
                    restored += 1
                c.execute(
                    insert(revision_bars).values(
                        owner_user_id=context.user_id,
                        dataset_revision_id=revision.dataset_revision_id,
                        ordinal=ordinal,
                        series_id=sr["series_id"],
                        start_at=bar.start_at,
                        source_revision=bar.source_revision,
                        bar_record_id=bar.bar_record_id,
                        object_id=next(
                            ref.object_id
                            for ref in revision.partition_refs
                            if ref.min_start_at <= bar.start_at < ref.max_end_at
                        ),
                    )
                )
                restored += 1
            for ordinal, ref in enumerate(revision.partition_refs):
                c.execute(
                    insert(revision_partitions).values(
                        owner_user_id=context.user_id,
                        dataset_revision_id=revision.dataset_revision_id,
                        ordinal=ordinal,
                        object_id=ref.object_id,
                    )
                )
                restored += 1
            if revision.correction_refs:
                c.execute(
                    insert(correction_ref_rows),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "dataset_revision_id": revision.dataset_revision_id,
                            "ordinal": ordinal,
                            "old_bar_record_id": value.old_bar_record_id,
                            "new_bar_record_id": value.new_bar_record_id,
                            "reason": value.reason,
                            "received_at": value.received_at,
                        }
                        for ordinal, value in enumerate(revision.correction_refs)
                    ],
                )
                restored += len(revision.correction_refs)
            for chain_item in document.get("correction_chain_records", []):
                for component in chain_item.get("aggregate_components", []):
                    insertion = c.execute(
                        pg_insert(aggregate_components)
                        .values(
                            owner_user_id=context.user_id,
                            derived_bar_record_id=chain_item["bar_record_id"],
                            ordinal=component["ordinal"],
                            source_bar_record_id=component["source_bar_record_id"],
                            source_dataset_revision_id=component["source_dataset_revision_id"],
                        )
                        .on_conflict_do_nothing()
                    )
                    restored += insertion.rowcount
            move_latest = sr["latest_revision_id"] is None
            if move_latest:
                c.execute(
                    update(series)
                    .where(
                        and_(
                            series.c.owner_user_id == context.user_id,
                            series.c.series_id == sr["series_id"],
                        )
                    )
                    .values(
                        latest_revision_id=revision.dataset_revision_id,
                        record_version=series.c.record_version + 1,
                    )
                )
            result = RestoreResult(
                dataset_revision=revision,
                catalog_rows_restored=restored,
                latest_pointer_changed=move_latest,
                replayed=False,
            )
            self.catalog._save_result(
                c,
                context,
                "dataset.restore_retained_manifest",
                idempotency_key,
                result,
            )
            self._inject("restore_during_transaction_before_commit")
            committed_result = result
        self._inject("restore_after_transaction_commit")
        if committed_result is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("restore transaction completed without a result")
        return committed_result

    def recover_quarantined_latest(
        self,
        context: UserContext,
        request: QuarantinedLatestRecoveryRequest,
        *,
        idempotency_key: str,
    ) -> PublicationResult:
        try:
            result = self._recover_quarantined_latest(
                context, request, idempotency_key=idempotency_key
            )
            self.cleanup_one(result.publication_id)
            return result
        except MarketDataError as error:
            if error.code == MarketDataCode.ARCHIVE_INTEGRITY:
                self._persist_recovery_integrity_failure(context.user_id, idempotency_key, error)
            raise

    def _persist_recovery_integrity_failure(
        self, owner: str, idempotency_key: str, error: MarketDataError
    ) -> None:
        now = self.catalog._now()
        with self.catalog.engine.begin() as connection:
            root = (
                connection.execute(
                    select(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == owner,
                            idempotency.c.operation == "dataset.recover_quarantined_latest",
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if root is None or root["state"] != "started":
                return
            source_id = root["admitted_parent_revision_id"]
            series_id = (
                connection.execute(
                    select(dataset_revisions.c.series_id).where(
                        and_(
                            dataset_revisions.c.owner_user_id == owner,
                            dataset_revisions.c.dataset_revision_id == source_id,
                        )
                    )
                ).scalar_one_or_none()
                if source_id is not None
                else None
            )
            if series_id is not None:
                fence = (
                    connection.execute(
                        select(series_fences)
                        .where(
                            and_(
                                series_fences.c.owner_user_id == owner,
                                series_fences.c.series_id == series_id,
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if fence["holder"] == root["holder"]:
                    connection.execute(
                        update(series_fences)
                        .where(
                            and_(
                                series_fences.c.owner_user_id == owner,
                                series_fences.c.series_id == series_id,
                            )
                        )
                        .values(lease_expires_at=now)
                    )
            connection.execute(
                update(idempotency)
                .where(
                    and_(
                        idempotency.c.owner_user_id == owner,
                        idempotency.c.operation == "dataset.recover_quarantined_latest",
                        idempotency.c.idempotency_key == idempotency_key,
                        idempotency.c.state == "started",
                    )
                )
                .values(
                    state="failed",
                    error={
                        "code": MarketDataCode.ARCHIVE_INTEGRITY.value,
                        "message": "Recovery archive integrity verification failed.",
                        "http_status": 500,
                        "retryable": False,
                    },
                    holder=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )

    def _recover_quarantined_latest(
        self,
        context: UserContext,
        request: QuarantinedLatestRecoveryRequest,
        *,
        idempotency_key: str,
    ) -> PublicationResult:
        self.catalog._context(context)
        self.catalog._key(idempotency_key)
        publication_id, revision_id = str(uuid7()), str(uuid7())
        terminal_error: AccessError | MarketDataError | None = None
        resume_publication_id: str | None = None
        fencing_token: int | None = None
        with (
            self.catalog.engine.connect().execution_options(
                isolation_level="SERIALIZABLE"
            ) as admission,
            admission.begin(),
        ):
            replay = self.catalog._idempotent(
                admission,
                context,
                "dataset.recover_quarantined_latest",
                idempotency_key,
                request,
            )
            if replay:
                result = PublicationResult.model_validate(replay)
                return result.model_copy(update={"replayed": True})
            root = (
                admission.execute(
                    select(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == context.user_id,
                            idempotency.c.operation == "dataset.recover_quarantined_latest",
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            source = (
                admission.execute(
                    select(dataset_revisions).where(
                        and_(
                            dataset_revisions.c.owner_user_id == context.user_id,
                            dataset_revisions.c.dataset_revision_id
                            == request.quarantined_revision_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not source:
                terminal_error = not_found()
            if terminal_error is None:
                assert source is not None
                fence = (
                    admission.execute(
                        select(series_fences)
                        .where(
                            and_(
                                series_fences.c.owner_user_id == context.user_id,
                                series_fences.c.series_id == source["series_id"],
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
            else:
                fence = None
            sr = (
                (
                    admission.execute(
                        select(series)
                        .where(
                            and_(
                                series.c.owner_user_id == context.user_id,
                                series.c.series_id == source["series_id"],
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                if source is not None
                else None
            )
            now = self.catalog._now()
            if terminal_error is None:
                assert source is not None and sr is not None
                if (
                    source["status"] != "quarantined"
                    or sr["latest_revision_id"] != request.quarantined_revision_id
                    or source["manifest_sha256"] != request.expected_manifest_sha256
                ):
                    terminal_error = MarketDataError(
                        MarketDataCode.STALE_VERSION,
                        "The named revision is not the matching quarantined latest.",
                        409,
                    )
            if terminal_error is None and root["current_publication_id"]:
                staged = admission.execute(
                    select(publications.c.state)
                    .where(
                        and_(
                            publications.c.owner_user_id == context.user_id,
                            publications.c.publication_id == root["current_publication_id"],
                        )
                    )
                    .with_for_update()
                ).scalar()
                if staged == "staged":
                    resume_publication_id = root["current_publication_id"]
            if terminal_error is None and resume_publication_id is None:
                assert source is not None and fence is not None
                fencing_token = int(fence["fencing_token"]) + 1
                expires = now + timedelta(seconds=self.lease_seconds)
                admission.execute(
                    update(series_fences)
                    .where(
                        and_(
                            series_fences.c.owner_user_id == context.user_id,
                            series_fences.c.series_id == source["series_id"],
                        )
                    )
                    .values(
                        fencing_token=fencing_token,
                        holder=self.worker_id,
                        lease_expires_at=expires,
                    )
                )
                admission.execute(
                    update(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == context.user_id,
                            idempotency.c.operation == "dataset.recover_quarantined_latest",
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                    .values(
                        current_publication_id=publication_id,
                        parent_admitted_at=now,
                        admitted_parent_revision_id=request.quarantined_revision_id,
                        attempt_generation=idempotency.c.attempt_generation + 1,
                        holder=self.worker_id,
                        lease_expires_at=expires,
                        updated_at=now,
                    )
                )
            if terminal_error is not None:
                admission.execute(
                    update(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == context.user_id,
                            idempotency.c.operation == "dataset.recover_quarantined_latest",
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                    .values(
                        state="failed",
                        error={
                            "code": terminal_error.code.value,
                            "message": terminal_error.message,
                            "http_status": terminal_error.http_status,
                            "retryable": terminal_error.retryable,
                        },
                        holder=None,
                        lease_expires_at=None,
                        updated_at=now,
                    )
                )
            selected = list(
                admission.execute(
                    select(revision_bars)
                    .where(
                        and_(
                            revision_bars.c.owner_user_id == context.user_id,
                            revision_bars.c.dataset_revision_id == request.quarantined_revision_id,
                        )
                    )
                    .order_by(revision_bars.c.ordinal)
                    .with_for_update(read=True)
                ).mappings()
            )
        if terminal_error is not None:
            raise terminal_error
        if resume_publication_id is not None:
            recovered = self.reconcile_one(resume_publication_id)
            if recovered.terminal_state != "published":
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Recovery could not be resumed safely.",
                    500,
                )
            with self.catalog.engine.begin() as connection:
                saved = connection.execute(
                    select(idempotency.c.result).where(
                        and_(
                            idempotency.c.owner_user_id == context.user_id,
                            idempotency.c.operation == "dataset.recover_quarantined_latest",
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                ).scalar_one()
            return PublicationResult.model_validate(saved).model_copy(update={"replayed": True})
        if source is None or sr is None or fencing_token is None:
            raise MarketDataError(
                MarketDataCode.DEPENDENCY_UNAVAILABLE,
                "Recovery control transaction did not allocate a fence.",
                503,
                retryable=True,
            )

        def recovery_heartbeat() -> None:
            self._heartbeat_lease(
                context.user_id,
                source["series_id"],
                "dataset.recover_quarantined_latest",
                idempotency_key,
                fencing_token,
            )

        def check_recovery_fence() -> None:
            self._heartbeat_lease(
                context.user_id,
                source["series_id"],
                "dataset.recover_quarantined_latest",
                idempotency_key,
                fencing_token,
                force=True,
            )

        def continuous_recovery_heartbeat() -> Any:
            return self._continuous_lease_heartbeat(
                context.user_id,
                source["series_id"],
                "dataset.recover_quarantined_latest",
                idempotency_key,
                fencing_token,
            )

        source_projection = DatasetRevision.model_validate(source["projection"])
        self._inject("after_recovery_admission_before_snapshot")
        try:
            with (
                self.catalog.engine.connect() as verification,
                continuous_recovery_heartbeat(),
            ):
                verified_source_bars = _verify_catalog_reconstruction_dag(
                    verification,
                    self.store,
                    context.user_id,
                    request.quarantined_revision_id,
                    allow_quarantined_seed=True,
                    allow_integrity_quarantined_closure=True,
                    heartbeat=recovery_heartbeat,
                )
        except (KeyError, TypeError, ValueError) as error:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "Quarantined reconstruction closure is invalid.",
                500,
            ) from error
        check_recovery_fence()
        with continuous_recovery_heartbeat():
            source_manifest = self.store.read_verified(
                context.user_id,
                source["manifest_uri"],
                request.expected_manifest_sha256,
                source["manifest_byte_length"],
            )
        import json

        try:
            source_document = json.loads(source_manifest)
            _validate_manifest_schema(source_document)
            _validate_manifest_correction_history(source_document)
            if source_document["dataset_revision_id"] != request.quarantined_revision_id:
                raise ValueError
        except KeyError, ValueError, TypeError:
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "Quarantined manifest schema is invalid.",
                500,
            ) from None
        aggregate_sources: dict[str, dict[str, Any]] = {}
        for chain_item in source_document.get("correction_chain_records", []):
            for component in chain_item.get("aggregate_components", []):
                dependency_id = component.get("source_dataset_revision_id")
                dependency_uri = component.get("source_manifest_uri")
                dependency_sha = component.get("source_manifest_sha256")
                if not all(
                    isinstance(value, str)
                    for value in (dependency_id, dependency_uri, dependency_sha)
                ):
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Derived recovery dependency metadata is invalid.",
                        500,
                    )
                try:
                    dependency_bytes = self.store.resolve(
                        context.user_id, dependency_uri
                    ).read_bytes()
                except OSError:
                    raise MarketDataError(
                        MarketDataCode.STALE_DATA,
                        "A healthy source dependency must be recovered first.",
                        409,
                        public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                    ) from None
                if hashlib.sha256(dependency_bytes).hexdigest() != dependency_sha:
                    raise MarketDataError(
                        MarketDataCode.STALE_DATA,
                        "A healthy source dependency must be recovered first.",
                        409,
                        public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                    )
                try:
                    dependency_document = json.loads(dependency_bytes)
                    dependency_key = SeriesKey.model_validate(
                        {
                            name: value
                            for name, value in dependency_document["series_key"].items()
                            if name != "owner_user_id"
                        }
                    )
                except KeyError, ValueError, TypeError:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "Derived recovery dependency schema is invalid.",
                        500,
                    ) from None
                aggregate_sources[dependency_id] = {
                    "series_key": dependency_key,
                    "required_start": source_projection.coverage_start,
                    "required_end": source_projection.coverage_end,
                }
        if aggregate_sources:
            for dependency in aggregate_sources.values():
                with self.catalog.engine.connect().execution_options(
                    isolation_level="REPEATABLE READ"
                ) as healthy_dependency:
                    check_recovery_fence()
                    source_series = self.catalog._series_for(
                        healthy_dependency,
                        context.user_id,
                        dependency["series_key"],
                    )
                    healthy = (
                        healthy_dependency.execute(
                            select(dataset_revisions).where(
                                and_(
                                    dataset_revisions.c.owner_user_id == context.user_id,
                                    dataset_revisions.c.dataset_revision_id
                                    == source_series["latest_revision_id"],
                                    dataset_revisions.c.status == "published",
                                    dataset_revisions.c.coverage_start
                                    <= dependency["required_start"],
                                    dataset_revisions.c.coverage_end >= dependency["required_end"],
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if healthy is None:
                        raise MarketDataError(
                            MarketDataCode.STALE_DATA,
                            "A healthy source dependency must be recovered first.",
                            409,
                            public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                        )
                    try:
                        with continuous_recovery_heartbeat():
                            healthy_rows = _verify_catalog_reconstruction_dag(
                                healthy_dependency,
                                self.store,
                                context.user_id,
                                healthy["dataset_revision_id"],
                                heartbeat=recovery_heartbeat,
                            )
                    except KeyError, MarketDataError, TypeError, ValueError:
                        raise MarketDataError(
                            MarketDataCode.STALE_DATA,
                            "A healthy source dependency must be recovered first.",
                            409,
                            public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                        ) from None
                    dependency["series_id"] = source_series["series_id"]
                    dependency["healthy_revision"] = dict(healthy)
                    dependency["healthy_rows"] = healthy_rows
                    check_recovery_fence()
        dependency_expectations = tuple(
            sorted(
                (
                    {
                        "series_id": item["series_id"],
                        "dataset_revision_id": item["healthy_revision"]["dataset_revision_id"],
                        "manifest_uri": item["healthy_revision"]["manifest_uri"],
                        "manifest_sha256": item["healthy_revision"]["manifest_sha256"],
                        "manifest_byte_length": item["healthy_revision"]["manifest_byte_length"],
                    }
                    for item in aggregate_sources.values()
                ),
                key=lambda item: item["series_id"],
            )
        )
        recovery_bars = verified_source_bars
        final_selection = [
            {
                "start_at": row["start_at"],
                "source_revision": row["source_revision"],
                "bar_record_id": row["bar_record_id"],
            }
            for row in selected
        ]
        recovery_chain_records = list(source_document.get("correction_chain_records", []))
        recovery_correction_refs = list(source_projection.correction_refs)
        if aggregate_sources:
            dependency_values = list(aggregate_sources.values())
            source_keys = {canonical_sha256(item["series_key"]) for item in dependency_values}
            healthy_ids = {
                item["healthy_revision"]["dataset_revision_id"] for item in dependency_values
            }
            if len(source_keys) != 1 or len(healthy_ids) != 1:
                raise MarketDataError(
                    MarketDataCode.STALE_DATA,
                    "A single healthy source frontier is required for derived recovery.",
                    409,
                    public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                )
            dependency = dependency_values[0]
            source_key = dependency["series_key"]
            target_key = SeriesKey.model_validate(
                source_projection.series_key.model_dump(exclude={"owner_user_id"})
            )
            healthy_revision_id = dependency["healthy_revision"]["dataset_revision_id"]
            healthy_rows = sorted(dependency["healthy_rows"], key=lambda item: item.start_at)
            aggregates: list[AggregatedFullBar] = []
            for target_bar in sorted(recovery_bars, key=lambda item: item.start_at):
                check_recovery_fence()
                components = [
                    item
                    for item in healthy_rows
                    if target_bar.start_at <= item.start_at < target_bar.end_at
                ]
                expected_count = target_key.interval_seconds // source_key.interval_seconds
                if (
                    len(components) != expected_count
                    or components[0].start_at != target_bar.start_at
                    or components[-1].end_at != target_bar.end_at
                ):
                    raise MarketDataError(
                        MarketDataCode.STALE_DATA,
                        "Healthy source coverage is incomplete for derived recovery.",
                        409,
                        public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                    )
                aggregates.append(
                    AggregatedFullBar(
                        target_interval_seconds=cast(
                            Literal[300, 900, 1800, 3600], target_key.interval_seconds
                        ),
                        start_at=target_bar.start_at,
                        end_at=target_bar.end_at,
                        open=components[0].open,
                        high=max(item.high for item in components),
                        low=min(item.low for item in components),
                        close=components[-1].close,
                        volume=sum((item.volume for item in components), Decimal(0)),
                        source_bar_record_ids=tuple(item.bar_record_id for item in components),
                    )
                )
                check_recovery_fence()
            aggregate_result = self.catalog.record_aggregated_batch(
                context,
                healthy_revision_id,
                source_key,
                target_key,
                tuple(aggregates),
                idempotency_key=idempotency_key,
            )
            recovered_ids = (
                aggregate_result.inserted_bar_record_ids or aggregate_result.replayed_bar_record_ids
            )
            with self.catalog.engine.begin() as aggregate_catalog:
                recovered_rows = list(
                    aggregate_catalog.execute(
                        select(active_bars)
                        .where(
                            and_(
                                active_bars.c.owner_user_id == context.user_id,
                                active_bars.c.bar_record_id.in_(recovered_ids),
                            )
                        )
                        .order_by(active_bars.c.start_at)
                    ).mappings()
                )
                recovery_bars = [_bar(dict(item)) for item in recovered_rows]
                for recovered_bar in recovery_bars:
                    registry = (
                        aggregate_catalog.execute(
                            select(bar_versions).where(
                                and_(
                                    bar_versions.c.owner_user_id == context.user_id,
                                    bar_versions.c.bar_record_id == recovered_bar.bar_record_id,
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    component_rows = list(
                        aggregate_catalog.execute(
                            select(
                                aggregate_components.c.ordinal,
                                aggregate_components.c.source_bar_record_id,
                                aggregate_components.c.source_dataset_revision_id,
                                dataset_revisions.c.manifest_uri.label("source_manifest_uri"),
                                dataset_revisions.c.manifest_sha256.label("source_manifest_sha256"),
                                dataset_revisions.c.manifest_byte_length.label(
                                    "source_manifest_byte_length"
                                ),
                            )
                            .join(
                                dataset_revisions,
                                and_(
                                    dataset_revisions.c.owner_user_id
                                    == aggregate_components.c.owner_user_id,
                                    dataset_revisions.c.dataset_revision_id
                                    == aggregate_components.c.source_dataset_revision_id,
                                ),
                            )
                            .where(
                                and_(
                                    aggregate_components.c.owner_user_id == context.user_id,
                                    aggregate_components.c.derived_bar_record_id
                                    == recovered_bar.bar_record_id,
                                )
                            )
                            .order_by(aggregate_components.c.ordinal)
                        ).mappings()
                    )
                    recovery_chain_records.append(
                        {
                            "completed_bar": recovered_bar.model_dump(mode="json"),
                            "payload_sha256": registry["payload_hash"],
                            "correction_reason": registry["correction_reason"],
                            "aggregate_lineage_sha256": registry["aggregate_lineage_sha256"],
                            "version_fingerprint_sha256": registry["version_fingerprint_sha256"],
                            "bar_record_id": recovered_bar.bar_record_id,
                            "source_revision": recovered_bar.source_revision,
                            "received_at": recovered_bar.received_at,
                            "supersedes_bar_record_id": recovered_bar.supersedes_bar_record_id,
                            "aggregate_components": [dict(item) for item in component_rows],
                        }
                    )
                    previous = next(
                        item
                        for item in source_document["selected_bars"]
                        if item["start_at"]
                        == recovered_bar.start_at.isoformat().replace("+00:00", "Z")
                    )
                    recovery_correction_refs.append(
                        CorrectionRef(
                            logical_bar_key=LogicalBarKey(
                                owner_user_id=context.user_id,
                                source=target_key.source,
                                price_basis=target_key.price_basis,
                                contract_id=target_key.contract_id,
                                interval_seconds=target_key.interval_seconds,
                                start_at=recovered_bar.start_at,
                            ),
                            old_bar_record_id=previous["bar_record_id"],
                            new_bar_record_id=recovered_bar.bar_record_id,
                            reason="DERIVED_COMPONENT_CHANGE",
                            received_at=recovered_bar.received_at,
                        )
                    )
            recovered_selection = [
                {
                    "start_at": item.start_at,
                    "source_revision": item.source_revision,
                    "bar_record_id": item.bar_record_id,
                }
                for item in recovery_bars
            ]
            final_selection = recovered_selection
        partition_refs: list[PartitionRef] = []
        publication_file_rows: list[dict[str, Any]] = []
        recovery_payloads: list[tuple[PartitionRef, bytes, list[CompletedBar] | None]] = []
        recovery_staged: list[tuple[PartitionRef, bytes, str]] = []
        if aggregate_sources:
            with self.catalog.engine.begin() as calendar_connection:
                recovery_windows = list(
                    calendar_connection.execute(
                        select(
                            calendar_windows.c.trading_day,
                            calendar_windows.c.start_at,
                            calendar_windows.c.end_at,
                        )
                        .where(
                            and_(
                                calendar_windows.c.owner_user_id == context.user_id,
                                calendar_windows.c.calendar_id == source_projection.calendar_id,
                                calendar_windows.c.calendar_version
                                == source_projection.calendar_version,
                                calendar_windows.c.kind == "open",
                            )
                        )
                        .order_by(calendar_windows.c.start_at)
                    ).mappings()
                )
            bars_by_day: dict[Any, list[CompletedBar]] = {}
            for recovery_bar in recovery_bars:
                trading_day = next(
                    (
                        window["trading_day"]
                        for window in recovery_windows
                        if window["start_at"] <= recovery_bar.start_at < window["end_at"]
                    ),
                    None,
                )
                if trading_day is None:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "A derived recovery bar is outside its materialized calendar.",
                        500,
                    )
                bars_by_day.setdefault(trading_day, []).append(recovery_bar)
            for index, trading_day in enumerate(sorted(bars_by_day)):
                day_bars = sorted(
                    bars_by_day[trading_day],
                    key=lambda item: (item.start_at, item.source_revision, item.bar_record_id),
                )
                encoded = io.BytesIO()
                check_recovery_fence()
                with continuous_recovery_heartbeat():
                    pl.DataFrame([item.model_dump(mode="json") for item in day_bars]).write_parquet(
                        encoded, compression="zstd"
                    )
                check_recovery_fence()
                template = source_projection.partition_refs[
                    min(index, len(source_projection.partition_refs) - 1)
                ]
                recovery_payloads.append((template, encoded.getvalue(), day_bars))
        else:
            for old_ref in source_projection.partition_refs:
                with continuous_recovery_heartbeat():
                    old_bytes = self.store.read_verified(
                        context.user_id, old_ref.uri, old_ref.sha256, old_ref.byte_length
                    )
                recovery_payloads.append((old_ref, old_bytes, None))
        for ordinal, (old_ref, old_bytes, payload_bars) in enumerate(recovery_payloads):
            object_id = str(uuid7())
            object_uri = f"ft-archive://object/{object_id}"
            with continuous_recovery_heartbeat():
                object_sha = hashlib.sha256(old_bytes).hexdigest()
            temp_name = f"{uuid7()}.recovery-object.tmp"
            partition_ref = old_ref.model_copy(
                update={
                    "object_id": object_id,
                    "uri": object_uri,
                    "sha256": object_sha,
                    "byte_length": len(old_bytes),
                    "row_count": len(payload_bars)
                    if payload_bars is not None
                    else old_ref.row_count,
                    "min_start_at": min(item.start_at for item in payload_bars)
                    if payload_bars is not None
                    else old_ref.min_start_at,
                    "max_end_at": max(item.end_at for item in payload_bars)
                    if payload_bars is not None
                    else old_ref.max_end_at,
                    "min_source_revision": min(item.source_revision for item in payload_bars)
                    if payload_bars is not None
                    else old_ref.min_source_revision,
                    "max_source_revision": max(item.source_revision for item in payload_bars)
                    if payload_bars is not None
                    else old_ref.max_source_revision,
                    "origin_publication_id": publication_id,
                }
            )
            partition_refs.append(partition_ref)
            recovery_staged.append((partition_ref, old_bytes, temp_name))
            publication_file_rows.append(
                {
                    "owner_user_id": context.user_id,
                    "publication_id": publication_id,
                    "ordinal": ordinal,
                    "file_kind": "object",
                    "temp_name": temp_name,
                    "final_uri": object_uri,
                    "sha256": object_sha,
                    "byte_length": len(old_bytes),
                    "state": "temp",
                }
            )
        now = self.catalog._now()
        manifest_uri = f"ft-archive://manifest/{revision_id}"
        dependency_revision_count = 0
        dependency_row_count = 0
        dependency_object_bytes = 0
        if aggregate_sources:
            healthy_ids = {
                item["healthy_revision"]["dataset_revision_id"]
                for item in aggregate_sources.values()
            }
            with self.catalog.engine.begin() as closure_connection:
                dependency_ids = _catalog_reconstruction_ids(
                    closure_connection, context.user_id, healthy_ids
                )
                dependency_revision_count = len(dependency_ids)
                dependency_row_count = int(
                    closure_connection.execute(
                        select(func.count())
                        .select_from(revision_bars)
                        .where(
                            and_(
                                revision_bars.c.owner_user_id == context.user_id,
                                revision_bars.c.dataset_revision_id.in_(dependency_ids),
                            )
                        )
                    ).scalar_one()
                )
                dependency_objects = list(
                    closure_connection.execute(
                        select(archive_objects.c.object_id, archive_objects.c.byte_length)
                        .join(
                            revision_partitions,
                            and_(
                                revision_partitions.c.owner_user_id
                                == archive_objects.c.owner_user_id,
                                revision_partitions.c.object_id == archive_objects.c.object_id,
                            ),
                        )
                        .where(
                            and_(
                                revision_partitions.c.owner_user_id == context.user_id,
                                revision_partitions.c.dataset_revision_id.in_(dependency_ids),
                            )
                        )
                        .distinct()
                    ).mappings()
                )
                dependency_object_bytes = sum(
                    int(item["byte_length"]) for item in dependency_objects
                )
        projection = source_projection.model_dump(
            mode="json", exclude={"manifest_sha256", "manifest_byte_length"}
        )
        projection.update(
            dataset_revision_id=revision_id,
            parent_revision_id=None,
            parent_manifest_uri=None,
            parent_manifest_sha256=None,
            manifest_uri=manifest_uri,
            partition_refs=[item.model_dump(mode="json") for item in partition_refs],
            status="published",
            created_at=now,
            published_at=now,
            parent_depth=0,
            restore_closure_revision_count=1 + dependency_revision_count,
            restore_closure_row_count=len(final_selection) + dependency_row_count,
            restore_closure_bytes=sum(item.byte_length for item in partition_refs)
            + dependency_object_bytes,
            rollover_from_revision_id=None,
            rollover_from_manifest_uri=None,
            rollover_from_manifest_sha256=None,
            recovery_from_quarantined_revision_id=request.quarantined_revision_id,
            recovery_from_manifest_uri=source["manifest_uri"],
            recovery_from_manifest_sha256=request.expected_manifest_sha256,
            record_version=2,
            source_watermark={
                "kind": "familytrade_received_v1",
                "max_received_at": max(
                    recovery_bars, key=lambda item: (item.received_at, item.bar_record_id)
                ).received_at,
                "max_bar_record_id": max(
                    recovery_bars, key=lambda item: (item.received_at, item.bar_record_id)
                ).bar_record_id,
            },
            correction_refs=[item.model_dump(mode="json") for item in recovery_correction_refs],
            selected_bars=[item.model_dump(mode="json") for item in recovery_bars],
            correction_chain_records=recovery_chain_records,
            format_version="ft-dataset-manifest-v1",
            origin_publication_id=publication_id,
            origin_publication_ordinal=0,
            contract_projection=source_document["contract_projection"],
            contract_projection_sha256=source_document["contract_projection_sha256"],
            calendar_projection=source_document["calendar_projection"],
        )
        check_recovery_fence()
        with continuous_recovery_heartbeat():
            manifest = canonical_json_bytes(projection)
        check_recovery_fence()
        with continuous_recovery_heartbeat():
            manifest_sha = hashlib.sha256(manifest).hexdigest()
        manifest_temp_name = f"{uuid7()}.recovery-manifest.tmp"
        publication_file_rows.append(
            {
                "owner_user_id": context.user_id,
                "publication_id": publication_id,
                "ordinal": len(partition_refs),
                "file_kind": "manifest",
                "temp_name": manifest_temp_name,
                "final_uri": manifest_uri,
                "sha256": manifest_sha,
                "byte_length": len(manifest),
                "state": "temp",
            }
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
        temp_uuid = str(uuid7())
        expected_temp_names = [row["temp_name"] for row in publication_file_rows]
        self._inject("after_recovery_dependency_verification_before_staging")
        with self.catalog.engine.begin() as journal:
            journal.execute(
                insert(prestage_writes).values(
                    owner_user_id=context.user_id,
                    series_id=source["series_id"],
                    idempotency_key=idempotency_key,
                    temp_uuid=temp_uuid,
                    holder=self.worker_id,
                    fencing_token=fencing_token,
                    lease_expires_at=self.catalog._now() + timedelta(seconds=self.lease_seconds),
                    expected_temp_names=expected_temp_names,
                    state="writing",
                )
            )

        self._inject("before_temp_create")
        for _ref, payload, temp_name in recovery_staged:
            check_recovery_fence()
            with continuous_recovery_heartbeat() as heartbeat_state:
                self._write_named_temp(
                    context.user_id,
                    temp_name,
                    payload,
                    check_fence=heartbeat_state.check,
                )
            self._inject("during_temp_write")
            check_recovery_fence()
            self._inject("after_file_fsync_before_directory_fsync")
        check_recovery_fence()
        with continuous_recovery_heartbeat() as heartbeat_state:
            self._write_named_temp(
                context.user_id,
                manifest_temp_name,
                manifest,
                check_fence=heartbeat_state.check,
            )
        check_recovery_fence()
        with continuous_recovery_heartbeat() as heartbeat_state:
            heartbeat_state.check()
            staging = self.store._owner_root(context.user_id) / "staging"
            heartbeat_state.check()
            self.store._fsync_directory(staging)
            heartbeat_state.check()
        self._inject("after_all_fsync_before_staged_tx")
        with (
            self.catalog.engine.connect().execution_options(isolation_level="SERIALIZABLE") as c,
            c.begin(),
        ):
            c.execute(
                select(idempotency.c.idempotency_key)
                .where(
                    and_(
                        idempotency.c.owner_user_id == context.user_id,
                        idempotency.c.operation == "dataset.recover_quarantined_latest",
                        idempotency.c.idempotency_key == idempotency_key,
                    )
                )
                .with_for_update()
            ).one()
            affected_series_ids = tuple(
                sorted(
                    {source["series_id"]} | {item["series_id"] for item in dependency_expectations}
                )
            )
            locked_fences = {
                row["series_id"]: row
                for row in c.execute(
                    select(series_fences)
                    .where(
                        and_(
                            series_fences.c.owner_user_id == context.user_id,
                            series_fences.c.series_id.in_(affected_series_ids),
                        )
                    )
                    .order_by(series_fences.c.series_id)
                    .with_for_update()
                ).mappings()
            }
            if len(locked_fences) != len(affected_series_ids):
                raise MarketDataError(
                    MarketDataCode.STALE_DATA,
                    "A healthy source dependency must be recovered first.",
                    409,
                    public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                )
            fence = locked_fences[source["series_id"]]
            if (
                fence["fencing_token"] != fencing_token
                or fence["holder"] != self.worker_id
                or fence["lease_expires_at"] <= self.catalog._now()
            ):
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "Recovery lease was displaced.",
                    409,
                    retryable=True,
                )
            locked_series = {
                row["series_id"]: row
                for row in c.execute(
                    select(series)
                    .where(
                        and_(
                            series.c.owner_user_id == context.user_id,
                            series.c.series_id.in_(affected_series_ids),
                        )
                    )
                    .order_by(series.c.series_id)
                    .with_for_update()
                ).mappings()
            }
            if len(locked_series) != len(affected_series_ids):
                raise MarketDataError(
                    MarketDataCode.STALE_DATA,
                    "A healthy source dependency must be recovered first.",
                    409,
                    public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                )
            locked = locked_series[source["series_id"]]
            if locked["latest_revision_id"] != request.quarantined_revision_id:
                raise MarketDataError(
                    MarketDataCode.STALE_VERSION,
                    "Quarantined latest changed during recovery.",
                    409,
                )
            if dependency_expectations:
                dependency_revision_ids = tuple(
                    item["dataset_revision_id"] for item in dependency_expectations
                )
                locked_dependencies = {
                    row["dataset_revision_id"]: row
                    for row in c.execute(
                        select(dataset_revisions)
                        .where(
                            and_(
                                dataset_revisions.c.owner_user_id == context.user_id,
                                dataset_revisions.c.dataset_revision_id.in_(
                                    dependency_revision_ids
                                ),
                            )
                        )
                        .order_by(dataset_revisions.c.dataset_revision_id)
                        .with_for_update()
                    ).mappings()
                }
                dependencies_are_current = all(
                    item["dataset_revision_id"] in locked_dependencies
                    and locked_series[item["series_id"]]["latest_revision_id"]
                    == item["dataset_revision_id"]
                    and locked_dependencies[item["dataset_revision_id"]]["series_id"]
                    == item["series_id"]
                    and locked_dependencies[item["dataset_revision_id"]]["status"] == "published"
                    and locked_dependencies[item["dataset_revision_id"]]["manifest_uri"]
                    == item["manifest_uri"]
                    and locked_dependencies[item["dataset_revision_id"]]["manifest_sha256"]
                    == item["manifest_sha256"]
                    and locked_dependencies[item["dataset_revision_id"]]["manifest_byte_length"]
                    == item["manifest_byte_length"]
                    for item in dependency_expectations
                )
                if not dependencies_are_current:
                    raise MarketDataError(
                        MarketDataCode.STALE_DATA,
                        "A healthy source dependency must be recovered first.",
                        409,
                        public_details={"reason": "RECOVER_SOURCE_DEPENDENCY_FIRST"},
                    )
            c.execute(
                insert(publications).values(
                    owner_user_id=context.user_id,
                    publication_id=publication_id,
                    series_id=source["series_id"],
                    idempotency_key=idempotency_key,
                    operation="recover_quarantined_latest",
                    quarantined_source_revision_id=request.quarantined_revision_id,
                    parent_revision_id=request.quarantined_revision_id,
                    final_candidate_revision_id=revision_id,
                    snapshot_sha256=hashlib.sha256(
                        canonical_json_bytes([row["bar_record_id"] for row in final_selection])
                    ).hexdigest(),
                    fencing_token=fencing_token,
                    state="staged",
                    created_at=now,
                    updated_at=now,
                )
            )
            c.execute(
                insert(archive_objects),
                [
                    {
                        "owner_user_id": context.user_id,
                        "object_id": ref.object_id,
                        "series_id": source["series_id"],
                        "uri": ref.uri,
                        "sha256": ref.sha256,
                        "byte_length": ref.byte_length,
                        "row_count": ref.row_count,
                        "min_start_at": ref.min_start_at,
                        "max_end_at": ref.max_end_at,
                        "min_source_revision": ref.min_source_revision,
                        "max_source_revision": ref.max_source_revision,
                        "state": "staged",
                        "origin_publication_id": publication_id,
                        "publication_id": publication_id,
                        "catalog_origin": "publication",
                    }
                    for ref in partition_refs
                ],
            )
            c.execute(
                insert(dataset_revisions).values(
                    owner_user_id=context.user_id,
                    dataset_revision_id=revision_id,
                    schema_version="v1",
                    series_id=source["series_id"],
                    contract_version=revision.contract_version,
                    calendar_id=revision.calendar_id,
                    calendar_version=revision.calendar_version,
                    projection=revision.model_dump(mode="json"),
                    parent_revision_id=None,
                    manifest_uri=manifest_uri,
                    manifest_sha256=manifest_sha,
                    manifest_byte_length=len(manifest),
                    coverage_start=revision.coverage_start,
                    coverage_end=revision.coverage_end,
                    source_watermark=revision.source_watermark.model_dump(mode="json"),
                    correction_refs=[
                        item.model_dump(mode="json") for item in revision.correction_refs
                    ],
                    status="building",
                    created_at=now,
                    published_at=None,
                    parent_depth=revision.parent_depth,
                    restore_closure_revision_count=revision.restore_closure_revision_count,
                    restore_closure_row_count=revision.restore_closure_row_count,
                    restore_closure_bytes=revision.restore_closure_bytes,
                    rollover_from_revision_id=revision.rollover_from_revision_id,
                    rollover_from_manifest_uri=revision.rollover_from_manifest_uri,
                    rollover_from_manifest_sha256=revision.rollover_from_manifest_sha256,
                    recovery_from_quarantined_revision_id=revision.recovery_from_quarantined_revision_id,
                    recovery_from_manifest_uri=revision.recovery_from_manifest_uri,
                    recovery_from_manifest_sha256=revision.recovery_from_manifest_sha256,
                    record_version=1,
                )
            )
            c.execute(
                insert(revision_partitions),
                [
                    {
                        "owner_user_id": context.user_id,
                        "dataset_revision_id": revision_id,
                        "ordinal": ordinal,
                        "object_id": ref.object_id,
                    }
                    for ordinal, ref in enumerate(partition_refs)
                ],
            )
            c.execute(
                insert(revision_bars),
                [
                    {
                        "owner_user_id": context.user_id,
                        "dataset_revision_id": revision_id,
                        "ordinal": ordinal,
                        "series_id": source["series_id"],
                        "start_at": row["start_at"],
                        "source_revision": row["source_revision"],
                        "bar_record_id": row["bar_record_id"],
                        "object_id": next(
                            ref.object_id
                            for ref in partition_refs
                            if ref.min_start_at <= row["start_at"] < ref.max_end_at
                        ),
                    }
                    for ordinal, row in enumerate(final_selection)
                ],
            )
            c.execute(
                insert(publication_revisions).values(
                    owner_user_id=context.user_id,
                    publication_id=publication_id,
                    ordinal=0,
                    dataset_revision_id=revision_id,
                )
            )
            c.execute(insert(publication_files), publication_file_rows)
            recovery_cleanup_ids = tuple(
                sorted(bar.bar_record_id for bar in recovery_bars if aggregate_sources)
            )
            if recovery_cleanup_ids:
                c.execute(
                    insert(publication_cleanup_bars),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "publication_id": publication_id,
                            "bar_record_id": bar_record_id,
                            "cleaned_at": None,
                        }
                        for bar_record_id in recovery_cleanup_ids
                    ],
                )
            c.execute(
                update(prestage_writes)
                .where(
                    and_(
                        prestage_writes.c.owner_user_id == context.user_id,
                        prestage_writes.c.series_id == source["series_id"],
                        prestage_writes.c.idempotency_key == idempotency_key,
                        prestage_writes.c.temp_uuid == temp_uuid,
                    )
                )
                .values(state="staged")
            )
            result = PublicationResult(
                publication_id=publication_id,
                dataset_revision=revision,
                preserved_revision_ids=(),
                replayed=False,
            )
            self._inject("during_publish_tx_before_commit")
        self._inject("after_staged_commit_before_rename")
        for ref, _payload, temp_name in recovery_staged:
            check_recovery_fence()
            with continuous_recovery_heartbeat() as heartbeat_state:
                self.store.finalize(
                    context.user_id,
                    staging / temp_name,
                    ref.uri,
                    check_fence=heartbeat_state.check,
                )
            check_recovery_fence()
            self._inject("during_object_renames")
        check_recovery_fence()
        with continuous_recovery_heartbeat() as heartbeat_state:
            self.store.finalize(
                context.user_id,
                staging / manifest_temp_name,
                manifest_uri,
                check_fence=heartbeat_state.check,
            )
        check_recovery_fence()
        self._inject("after_all_renames_before_directory_fsync")
        self._inject("after_rename_fsync_before_publish_tx")
        recovered = self.reconcile_one(publication_id)
        if recovered.terminal_state != "published":
            raise MarketDataError(
                MarketDataCode.ARCHIVE_INTEGRITY,
                "Recovery could not be completed safely.",
                500,
            )
        self._inject("after_publish_commit_before_cleanup")
        return result

    def _lock_reconcile_roots(
        self, connection: Any, identity: dict[str, Any]
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        operation = (
            "dataset.publish"
            if identity["operation"] == "publish"
            else "dataset.recover_quarantined_latest"
        )
        staged_roots = tuple(
            connection.execute(
                select(publications.c.operation, publications.c.idempotency_key)
                .where(
                    and_(
                        publications.c.owner_user_id == identity["owner_user_id"],
                        publications.c.series_id == identity["series_id"],
                        publications.c.state == "staged",
                    )
                )
                .order_by(publications.c.operation, publications.c.idempotency_key)
            )
        )
        root_pairs = tuple(
            sorted(
                {
                    (
                        "dataset.publish"
                        if item.operation == "publish"
                        else "dataset.recover_quarantined_latest",
                        item.idempotency_key,
                    )
                    for item in staged_roots
                }
                | {(operation, identity["idempotency_key"])}
            )
        )
        for root_operation, root_key in root_pairs:
            connection.execute(
                select(idempotency)
                .where(
                    and_(
                        idempotency.c.owner_user_id == identity["owner_user_id"],
                        idempotency.c.operation == root_operation,
                        idempotency.c.idempotency_key == root_key,
                    )
                )
                .with_for_update()
            ).one()
        stable_roots = tuple(
            connection.execute(
                select(publications.c.operation, publications.c.idempotency_key)
                .where(
                    and_(
                        publications.c.owner_user_id == identity["owner_user_id"],
                        publications.c.series_id == identity["series_id"],
                        publications.c.state == "staged",
                    )
                )
                .order_by(publications.c.operation, publications.c.idempotency_key)
            )
        )
        if stable_roots != staged_roots:
            raise MarketDataError(
                MarketDataCode.CONFLICT,
                "Staged publication roots changed during reconciliation.",
                409,
                retryable=True,
            )
        return operation, root_pairs

    def _quarantine_staged_publication(
        self, identity: dict[str, Any], publication_id: str, fencing_token: int, reason: str
    ) -> RecoveryResult:
        owner = identity["owner_user_id"]
        with (
            self.catalog.engine.connect().execution_options(isolation_level="SERIALIZABLE") as c,
            c.begin(),
        ):
            self._lock_reconcile_roots(c, identity)
            fence = (
                c.execute(
                    select(series_fences)
                    .where(
                        and_(
                            series_fences.c.owner_user_id == owner,
                            series_fences.c.series_id == identity["series_id"],
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if fence["fencing_token"] != fencing_token or fence["holder"] != self.worker_id:
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "Publication fence was displaced during reconciliation.",
                    409,
                    retryable=True,
                )
            publication = (
                c.execute(
                    select(publications)
                    .where(
                        and_(
                            publications.c.owner_user_id == owner,
                            publications.c.publication_id == publication_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if publication["state"] in ("published", "quarantined"):
                return RecoveryResult(
                    publication_id=publication_id,
                    terminal_state=publication["state"],
                    action="noop",
                    replayed=True,
                )
            planned_revision_ids = tuple(
                c.execute(
                    select(publication_revisions.c.dataset_revision_id)
                    .where(
                        and_(
                            publication_revisions.c.owner_user_id == owner,
                            publication_revisions.c.publication_id == publication_id,
                        )
                    )
                    .order_by(publication_revisions.c.ordinal)
                ).scalars()
            )
            now = self.catalog._now()
            c.execute(
                update(publications)
                .where(
                    and_(
                        publications.c.owner_user_id == owner,
                        publications.c.publication_id == publication_id,
                    )
                )
                .values(
                    state="quarantined",
                    safe_reason=reason,
                    fencing_token=fencing_token,
                    updated_at=now,
                )
            )
            c.execute(
                update(publication_files)
                .where(
                    and_(
                        publication_files.c.owner_user_id == owner,
                        publication_files.c.publication_id == publication_id,
                    )
                )
                .values(state="quarantined")
            )
            c.execute(
                update(archive_objects)
                .where(
                    and_(
                        archive_objects.c.owner_user_id == owner,
                        archive_objects.c.publication_id == publication_id,
                        archive_objects.c.state == "staged",
                    )
                )
                .values(state="quarantined")
            )
            if planned_revision_ids:
                c.execute(
                    update(dataset_revisions)
                    .where(
                        and_(
                            dataset_revisions.c.owner_user_id == owner,
                            dataset_revisions.c.dataset_revision_id.in_(planned_revision_ids),
                            dataset_revisions.c.status == "building",
                        )
                    )
                    .values(status="quarantined", record_version=2)
                )
            c.execute(
                update(publication_retention_conversions)
                .where(
                    and_(
                        publication_retention_conversions.c.owner_user_id == owner,
                        publication_retention_conversions.c.publication_id == publication_id,
                        publication_retention_conversions.c.state == "planned",
                    )
                )
                .values(state="cancelled")
            )
        return RecoveryResult(
            publication_id=publication_id,
            terminal_state="quarantined",
            action="quarantined",
            replayed=False,
        )

    def reconcile_one(self, publication_id: str) -> RecoveryResult:
        with self.catalog.engine.connect() as lookup:
            identity = (
                lookup.execute(
                    select(publications).where(publications.c.publication_id == publication_id)
                )
                .mappings()
                .first()
            )
        if not identity:
            raise not_found()
        if identity["state"] in ("published", "quarantined"):
            return RecoveryResult(
                publication_id=publication_id,
                terminal_state=identity["state"],
                action="noop",
                replayed=True,
            )
        with (
            self.catalog.engine.connect().execution_options(isolation_level="SERIALIZABLE") as c,
            c.begin(),
        ):
            operation, _ = self._lock_reconcile_roots(c, dict(identity))
            terminal = c.execute(
                select(publications.c.state).where(
                    and_(
                        publications.c.owner_user_id == identity["owner_user_id"],
                        publications.c.publication_id == publication_id,
                    )
                )
            ).scalar_one()
            if terminal in ("published", "quarantined"):
                return RecoveryResult(
                    publication_id=publication_id,
                    terminal_state=terminal,
                    action="noop",
                    replayed=True,
                )
            fresh_fence = self._take_fence(c, identity["owner_user_id"], identity["series_id"])
            expires = self.catalog._now() + timedelta(seconds=self.lease_seconds)
            c.execute(
                update(idempotency)
                .where(
                    and_(
                        idempotency.c.owner_user_id == identity["owner_user_id"],
                        idempotency.c.operation == operation,
                        idempotency.c.idempotency_key == identity["idempotency_key"],
                    )
                )
                .values(holder=self.worker_id, lease_expires_at=expires)
            )
            publication = (
                c.execute(
                    select(publications)
                    .where(
                        and_(
                            publications.c.owner_user_id == identity["owner_user_id"],
                            publications.c.publication_id == publication_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            files_for_io = [
                dict(row)
                for row in c.execute(
                    select(publication_files)
                    .where(
                        and_(
                            publication_files.c.owner_user_id == identity["owner_user_id"],
                            publication_files.c.publication_id == publication_id,
                        )
                    )
                    .order_by(publication_files.c.ordinal)
                ).mappings()
            ]
        self._inject("after_reconcile_fence_before_filesystem")

        def continuous_reconcile_heartbeat() -> Any:
            return self._continuous_lease_heartbeat(
                identity["owner_user_id"],
                identity["series_id"],
                operation,
                identity["idempotency_key"],
                fresh_fence,
            )

        try:
            for file in files_for_io:
                self._renew_lease(
                    identity["owner_user_id"],
                    identity["series_id"],
                    operation,
                    identity["idempotency_key"],
                    fresh_fence,
                )
                self._inject("during_reconcile_filesystem")
                final_path = self.store.resolve(identity["owner_user_id"], file["final_uri"])
                if not final_path.exists():
                    temp_path = (
                        self.store._owner_root(identity["owner_user_id"])
                        / "staging"
                        / file["temp_name"]
                    )
                    with continuous_reconcile_heartbeat() as heartbeat_state:
                        self.store.finalize(
                            identity["owner_user_id"],
                            temp_path,
                            file["final_uri"],
                            check_fence=heartbeat_state.check,
                        )
                with continuous_reconcile_heartbeat():
                    self.store.read_verified(
                        identity["owner_user_id"],
                        file["final_uri"],
                        file["sha256"],
                        file["byte_length"],
                    )
                self._renew_lease(
                    identity["owner_user_id"],
                    identity["series_id"],
                    operation,
                    identity["idempotency_key"],
                    fresh_fence,
                )
        except MarketDataError as error:
            if error.code == MarketDataCode.CONFLICT:
                raise
            return self._quarantine_staged_publication(
                dict(identity), publication_id, fresh_fence, "ARCHIVE_INTEGRITY"
            )
        except OSError:
            return self._quarantine_staged_publication(
                dict(identity), publication_id, fresh_fence, "ARCHIVE_INTEGRITY"
            )
        with self.catalog.engine.connect() as cleanup_lookup:
            cleanup_ids = tuple(
                cleanup_lookup.execute(
                    select(publication_cleanup_bars.c.bar_record_id)
                    .where(
                        and_(
                            publication_cleanup_bars.c.owner_user_id == identity["owner_user_id"],
                            publication_cleanup_bars.c.publication_id == publication_id,
                        )
                    )
                    .order_by(publication_cleanup_bars.c.bar_record_id)
                ).scalars()
            )
        self._inject("after_reconcile_filesystem_before_publish")
        with (
            self.catalog._retention_bar_locks(
                identity["owner_user_id"],
                cleanup_ids,
            ),
            self.catalog.engine.connect().execution_options(isolation_level="SERIALIZABLE") as c,
            c.begin(),
        ):
            operation = (
                "dataset.publish"
                if identity["operation"] == "publish"
                else "dataset.recover_quarantined_latest"
            )
            staged_roots = tuple(
                c.execute(
                    select(publications.c.operation, publications.c.idempotency_key)
                    .where(
                        and_(
                            publications.c.owner_user_id == identity["owner_user_id"],
                            publications.c.series_id == identity["series_id"],
                            publications.c.state == "staged",
                        )
                    )
                    .order_by(publications.c.operation, publications.c.idempotency_key)
                )
            )
            root_pairs = tuple(
                sorted(
                    {
                        (
                            "dataset.publish"
                            if item.operation == "publish"
                            else "dataset.recover_quarantined_latest",
                            item.idempotency_key,
                        )
                        for item in staged_roots
                    }
                    | {(operation, identity["idempotency_key"])}
                )
            )
            for root_operation, root_key in root_pairs:
                c.execute(
                    select(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == identity["owner_user_id"],
                            idempotency.c.operation == root_operation,
                            idempotency.c.idempotency_key == root_key,
                        )
                    )
                    .with_for_update()
                ).one()
            stable_roots = tuple(
                c.execute(
                    select(publications.c.operation, publications.c.idempotency_key)
                    .where(
                        and_(
                            publications.c.owner_user_id == identity["owner_user_id"],
                            publications.c.series_id == identity["series_id"],
                            publications.c.state == "staged",
                        )
                    )
                    .order_by(publications.c.operation, publications.c.idempotency_key)
                )
            )
            if stable_roots != staged_roots:
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "Staged publication roots changed during reconciliation.",
                    409,
                    retryable=True,
                )
            # Terminality is immutable and every publishing transition holds this
            # root. The filesystem phase already acquired the fresh fence; the
            # publish transaction only verifies that same token.
            terminal = c.execute(
                select(publications.c.state).where(
                    and_(
                        publications.c.owner_user_id == identity["owner_user_id"],
                        publications.c.publication_id == publication_id,
                    )
                )
            ).scalar_one()
            if terminal in ("published", "quarantined"):
                return RecoveryResult(
                    publication_id=publication_id,
                    terminal_state=terminal,
                    action="noop",
                    replayed=True,
                )
            final_fence = (
                c.execute(
                    select(series_fences)
                    .where(
                        and_(
                            series_fences.c.owner_user_id == identity["owner_user_id"],
                            series_fences.c.series_id == identity["series_id"],
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            if (
                final_fence["fencing_token"] != fresh_fence
                or final_fence["holder"] != self.worker_id
                or final_fence["lease_expires_at"] <= self.catalog._now()
            ):
                raise MarketDataError(
                    MarketDataCode.CONFLICT,
                    "Publication fence was displaced before final commit.",
                    409,
                    retryable=True,
                )
            publication = (
                c.execute(
                    select(publications)
                    .where(
                        and_(
                            publications.c.owner_user_id == identity["owner_user_id"],
                            publications.c.publication_id == publication_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            owner = publication["owner_user_id"]
            files = (
                c.execute(
                    select(publication_files)
                    .where(
                        and_(
                            publication_files.c.owner_user_id == owner,
                            publication_files.c.publication_id == publication_id,
                        )
                    )
                    .order_by(publication_files.c.ordinal)
                )
                .mappings()
                .all()
            )
            planned_revision_ids = list(
                c.execute(
                    select(publication_revisions.c.dataset_revision_id)
                    .where(
                        and_(
                            publication_revisions.c.owner_user_id == owner,
                            publication_revisions.c.publication_id == publication_id,
                        )
                    )
                    .order_by(publication_revisions.c.ordinal)
                ).scalars()
            )
            frozen_file_projection = [
                (
                    file["ordinal"],
                    file["file_kind"],
                    file["temp_name"],
                    file["final_uri"],
                    file["sha256"],
                    file["byte_length"],
                )
                for file in files_for_io
            ]
            current_file_projection = [
                (
                    file["ordinal"],
                    file["file_kind"],
                    file["temp_name"],
                    file["final_uri"],
                    file["sha256"],
                    file["byte_length"],
                )
                for file in files
            ]
            if current_file_projection != frozen_file_projection:
                c.execute(
                    update(publications)
                    .where(
                        and_(
                            publications.c.owner_user_id == owner,
                            publications.c.publication_id == publication_id,
                        )
                    )
                    .values(
                        state="quarantined",
                        safe_reason="ARCHIVE_INTEGRITY",
                        fencing_token=fresh_fence,
                        updated_at=self.catalog._now(),
                    )
                )
                c.execute(
                    update(publication_files)
                    .where(
                        and_(
                            publication_files.c.owner_user_id == owner,
                            publication_files.c.publication_id == publication_id,
                        )
                    )
                    .values(state="quarantined")
                )
                c.execute(
                    update(archive_objects)
                    .where(
                        and_(
                            archive_objects.c.owner_user_id == owner,
                            archive_objects.c.publication_id == publication_id,
                            archive_objects.c.state == "staged",
                        )
                    )
                    .values(state="quarantined")
                )
                if planned_revision_ids:
                    c.execute(
                        update(dataset_revisions)
                        .where(
                            and_(
                                dataset_revisions.c.owner_user_id == owner,
                                dataset_revisions.c.dataset_revision_id.in_(planned_revision_ids),
                                dataset_revisions.c.status == "building",
                            )
                        )
                        .values(status="quarantined", record_version=2)
                    )
                c.execute(
                    update(publication_retention_conversions)
                    .where(
                        and_(
                            publication_retention_conversions.c.owner_user_id == owner,
                            publication_retention_conversions.c.publication_id == publication_id,
                            publication_retention_conversions.c.state == "planned",
                        )
                    )
                    .values(state="cancelled")
                )
                return RecoveryResult(
                    publication_id=publication_id,
                    terminal_state="quarantined",
                    action="quarantined",
                    replayed=False,
                )
            candidate = publication["final_candidate_revision_id"]
            if not candidate:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Staged publication has no final candidate.",
                    500,
                )
            current = c.execute(
                select(series.c.latest_revision_id)
                .where(
                    and_(
                        series.c.owner_user_id == owner,
                        series.c.series_id == publication["series_id"],
                    )
                )
                .with_for_update()
            ).scalar_one()
            if current != publication["parent_revision_id"]:
                c.execute(
                    update(publications)
                    .where(
                        and_(
                            publications.c.owner_user_id == owner,
                            publications.c.publication_id == publication_id,
                        )
                    )
                    .values(
                        state="quarantined",
                        safe_reason="STALE_PARENT",
                        fencing_token=fresh_fence,
                        updated_at=self.catalog._now(),
                    )
                )
                c.execute(
                    update(publication_files)
                    .where(
                        and_(
                            publication_files.c.owner_user_id == owner,
                            publication_files.c.publication_id == publication_id,
                            publication_files.c.state == "staged",
                        )
                    )
                    .values(state="quarantined")
                )
                c.execute(
                    update(archive_objects)
                    .where(
                        and_(
                            archive_objects.c.owner_user_id == owner,
                            archive_objects.c.publication_id == publication_id,
                            archive_objects.c.state == "staged",
                        )
                    )
                    .values(state="quarantined")
                )
                if planned_revision_ids:
                    c.execute(
                        update(dataset_revisions)
                        .where(
                            and_(
                                dataset_revisions.c.owner_user_id == owner,
                                dataset_revisions.c.dataset_revision_id.in_(planned_revision_ids),
                                dataset_revisions.c.status == "building",
                            )
                        )
                        .values(status="quarantined", record_version=2)
                    )
                c.execute(
                    update(publication_retention_conversions)
                    .where(
                        and_(
                            publication_retention_conversions.c.owner_user_id == owner,
                            publication_retention_conversions.c.publication_id == publication_id,
                            publication_retention_conversions.c.state == "planned",
                        )
                    )
                    .values(state="cancelled")
                )
                return RecoveryResult(
                    publication_id=publication_id,
                    terminal_state="quarantined",
                    action="quarantined",
                    replayed=False,
                )
            c.execute(
                update(archive_objects)
                .where(
                    and_(
                        archive_objects.c.owner_user_id == owner,
                        archive_objects.c.publication_id == publication_id,
                    )
                )
                .values(state="published")
            )
            locked_cleanup_ids = tuple(
                c.execute(
                    select(publication_cleanup_bars.c.bar_record_id)
                    .where(
                        and_(
                            publication_cleanup_bars.c.owner_user_id == owner,
                            publication_cleanup_bars.c.publication_id == publication_id,
                        )
                    )
                    .order_by(publication_cleanup_bars.c.bar_record_id)
                    .with_for_update()
                ).scalars()
            )
            if locked_cleanup_ids != cleanup_ids:
                raise MarketDataError(
                    MarketDataCode.ARCHIVE_INTEGRITY,
                    "Publication cleanup identity changed before retention conversion.",
                    500,
                )
            self._inject("before_retention_reenumeration")
            active_retention_rows = list(
                c.execute(
                    select(bar_retention_refs)
                    .where(
                        and_(
                            bar_retention_refs.c.owner_user_id == owner,
                            bar_retention_refs.c.bar_record_id.in_(locked_cleanup_ids),
                        )
                    )
                    .order_by(
                        bar_retention_refs.c.bar_record_id,
                        bar_retention_refs.c.reference_kind,
                        bar_retention_refs.c.reference_id,
                    )
                    .with_for_update()
                ).mappings()
            )
            self._inject("during_retention_reenumeration")
            for retention_row in active_retention_rows:
                destination = c.execute(
                    select(publication_revisions.c.dataset_revision_id)
                    .join(
                        revision_bars,
                        and_(
                            revision_bars.c.owner_user_id == publication_revisions.c.owner_user_id,
                            revision_bars.c.dataset_revision_id
                            == publication_revisions.c.dataset_revision_id,
                        ),
                    )
                    .where(
                        and_(
                            publication_revisions.c.owner_user_id == owner,
                            publication_revisions.c.publication_id == publication_id,
                            revision_bars.c.bar_record_id == retention_row["bar_record_id"],
                        )
                    )
                    .order_by(publication_revisions.c.ordinal)
                    .limit(1)
                ).scalar_one_or_none()
                if destination is None:
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "A retained cleanup bar has no selecting publication frontier.",
                        500,
                    )
                existing_conversion = c.execute(
                    select(publication_retention_conversions.c.state).where(
                        and_(
                            publication_retention_conversions.c.owner_user_id == owner,
                            publication_retention_conversions.c.publication_id == publication_id,
                            publication_retention_conversions.c.bar_record_id
                            == retention_row["bar_record_id"],
                            publication_retention_conversions.c.reference_kind
                            == retention_row["reference_kind"],
                            publication_retention_conversions.c.reference_id
                            == retention_row["reference_id"],
                        )
                    )
                ).scalar_one_or_none()
                if existing_conversion is None:
                    c.execute(
                        insert(retention_refs).values(
                            owner_user_id=owner,
                            dataset_revision_id=destination,
                            reference_kind=retention_row["reference_kind"],
                            reference_id=retention_row["reference_id"],
                            created_at=self.catalog._now(),
                        )
                    )
                    c.execute(
                        delete(bar_retention_refs).where(
                            and_(
                                bar_retention_refs.c.owner_user_id == owner,
                                bar_retention_refs.c.bar_record_id
                                == retention_row["bar_record_id"],
                                bar_retention_refs.c.reference_kind
                                == retention_row["reference_kind"],
                                bar_retention_refs.c.reference_id == retention_row["reference_id"],
                            )
                        )
                    )
                    c.execute(
                        insert(publication_retention_conversions).values(
                            owner_user_id=owner,
                            publication_id=publication_id,
                            bar_record_id=retention_row["bar_record_id"],
                            reference_kind=retention_row["reference_kind"],
                            reference_id=retention_row["reference_id"],
                            dataset_revision_id=destination,
                            state="converted",
                        )
                    )
            self._inject("after_retention_reenumeration")
            conversions = (
                c.execute(
                    select(publication_retention_conversions)
                    .where(
                        and_(
                            publication_retention_conversions.c.owner_user_id == owner,
                            publication_retention_conversions.c.publication_id == publication_id,
                            publication_retention_conversions.c.state == "planned",
                        )
                    )
                    .order_by(
                        publication_retention_conversions.c.bar_record_id,
                        publication_retention_conversions.c.reference_kind,
                        publication_retention_conversions.c.reference_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            for conversion in conversions:
                if not c.execute(
                    select(bar_retention_refs.c.bar_record_id).where(
                        and_(
                            bar_retention_refs.c.owner_user_id == owner,
                            bar_retention_refs.c.bar_record_id == conversion["bar_record_id"],
                            bar_retention_refs.c.reference_kind == conversion["reference_kind"],
                            bar_retention_refs.c.reference_id == conversion["reference_id"],
                        )
                    )
                ).scalar():
                    raise MarketDataError(
                        MarketDataCode.ARCHIVE_INTEGRITY,
                        "A staged causal retention conversion is incomplete.",
                        500,
                    )
                c.execute(
                    insert(retention_refs).values(
                        owner_user_id=owner,
                        dataset_revision_id=conversion["dataset_revision_id"],
                        reference_kind=conversion["reference_kind"],
                        reference_id=conversion["reference_id"],
                        created_at=self.catalog._now(),
                    )
                )
                c.execute(
                    delete(bar_retention_refs).where(
                        and_(
                            bar_retention_refs.c.owner_user_id == owner,
                            bar_retention_refs.c.bar_record_id == conversion["bar_record_id"],
                            bar_retention_refs.c.reference_kind == conversion["reference_kind"],
                            bar_retention_refs.c.reference_id == conversion["reference_id"],
                        )
                    )
                )
                c.execute(
                    update(publication_retention_conversions)
                    .where(
                        and_(
                            publication_retention_conversions.c.owner_user_id == owner,
                            publication_retention_conversions.c.publication_id == publication_id,
                            publication_retention_conversions.c.bar_record_id
                            == conversion["bar_record_id"],
                            publication_retention_conversions.c.reference_kind
                            == conversion["reference_kind"],
                            publication_retention_conversions.c.reference_id
                            == conversion["reference_id"],
                        )
                    )
                    .values(state="converted")
                )
            if publication["operation"] == "recover_quarantined_latest":
                c.execute(
                    pg_insert(retention_refs)
                    .values(
                        owner_user_id=owner,
                        dataset_revision_id=publication["quarantined_source_revision_id"],
                        reference_kind="recovery_source",
                        reference_id=candidate,
                        created_at=self.catalog._now(),
                    )
                    .on_conflict_do_nothing()
                )
            c.execute(
                update(series)
                .where(
                    and_(
                        series.c.owner_user_id == owner,
                        series.c.series_id == publication["series_id"],
                    )
                )
                .values(
                    latest_revision_id=candidate,
                    record_version=series.c.record_version + 1,
                )
            )
            rollover_rows = list(
                c.execute(
                    select(
                        dataset_revisions.c.dataset_revision_id,
                        dataset_revisions.c.rollover_from_revision_id,
                    ).where(
                        and_(
                            dataset_revisions.c.owner_user_id == owner,
                            dataset_revisions.c.dataset_revision_id.in_(planned_revision_ids),
                            dataset_revisions.c.rollover_from_revision_id.is_not(None),
                        )
                    )
                ).mappings()
            )
            for rollover_row in rollover_rows:
                c.execute(
                    insert(retention_refs).values(
                        owner_user_id=owner,
                        dataset_revision_id=rollover_row["rollover_from_revision_id"],
                        reference_kind="rollover",
                        reference_id=rollover_row["dataset_revision_id"],
                        created_at=self.catalog._now(),
                    )
                )
            c.execute(
                update(dataset_revisions)
                .where(
                    and_(
                        dataset_revisions.c.owner_user_id == owner,
                        dataset_revisions.c.dataset_revision_id.in_(planned_revision_ids),
                    )
                )
                .values(
                    status="published",
                    published_at=text("(projection->>'published_at')::timestamptz"),
                    record_version=2,
                )
            )
            c.execute(
                update(publication_files)
                .where(
                    and_(
                        publication_files.c.owner_user_id == owner,
                        publication_files.c.publication_id == publication_id,
                    )
                )
                .values(state="published")
            )
            c.execute(
                update(publications)
                .where(
                    and_(
                        publications.c.owner_user_id == owner,
                        publications.c.publication_id == publication_id,
                    )
                )
                .values(
                    state="published",
                    fencing_token=fresh_fence,
                    updated_at=self.catalog._now(),
                )
            )
            candidate_row = (
                c.execute(
                    select(dataset_revisions.c.projection).where(
                        and_(
                            dataset_revisions.c.owner_user_id == owner,
                            dataset_revisions.c.dataset_revision_id == candidate,
                        )
                    )
                )
                .mappings()
                .one()
            )
            candidate_revision = DatasetRevision.model_validate(candidate_row["projection"])
            operation = (
                "dataset.publish"
                if publication["operation"] == "publish"
                else "dataset.recover_quarantined_latest"
            )
            result = PublicationResult(
                publication_id=publication_id,
                dataset_revision=candidate_revision,
                preserved_revision_ids=tuple(
                    revision_id
                    for revision_id in planned_revision_ids[:-1]
                    if not c.execute(
                        select(dataset_revisions.c.rollover_from_revision_id).where(
                            and_(
                                dataset_revisions.c.owner_user_id == owner,
                                dataset_revisions.c.dataset_revision_id == revision_id,
                            )
                        )
                    ).scalar()
                ),
                replayed=False,
            )
            c.execute(
                update(idempotency)
                .where(
                    and_(
                        idempotency.c.owner_user_id == owner,
                        idempotency.c.operation == operation,
                        idempotency.c.idempotency_key == publication["idempotency_key"],
                    )
                )
                .values(
                    state="succeeded",
                    result=result.model_dump(mode="json"),
                    updated_at=self.catalog._now(),
                )
            )
            self._inject("during_publish_tx_before_commit")
            return RecoveryResult(
                publication_id=publication_id,
                terminal_state="published",
                action="completed",
                replayed=False,
            )

    def cleanup_one(self, publication_id: str) -> CleanupResult:
        with self.catalog.engine.connect() as lookup:
            identity = (
                lookup.execute(
                    select(publications).where(publications.c.publication_id == publication_id)
                )
                .mappings()
                .first()
            )
        if not identity:
            raise not_found()
        if identity["state"] != "published":
            return CleanupResult(
                publication_id=publication_id,
                deleted_active_count=0,
                blocked_active_count=0,
                complete=False,
                replayed=True,
            )
        with self.catalog.engine.begin() as c:
            operation = (
                "dataset.publish"
                if identity["operation"] == "publish"
                else "dataset.recover_quarantined_latest"
            )
            c.execute(
                select(idempotency)
                .where(
                    and_(
                        idempotency.c.owner_user_id == identity["owner_user_id"],
                        idempotency.c.operation == operation,
                        idempotency.c.idempotency_key == identity["idempotency_key"],
                    )
                )
                .with_for_update()
            ).one()
            self._take_fence(c, identity["owner_user_id"], identity["series_id"])
            publication = (
                c.execute(
                    select(publications)
                    .where(
                        and_(
                            publications.c.owner_user_id == identity["owner_user_id"],
                            publications.c.publication_id == publication_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not publication:
                raise not_found()
            owner = publication["owner_user_id"]
            if publication["state"] != "published":
                return CleanupResult(
                    publication_id=publication_id,
                    deleted_active_count=0,
                    blocked_active_count=0,
                    complete=False,
                    replayed=True,
                )
            incomplete_catalog = any(
                c.execute(statement).scalar_one() > 0
                for statement in (
                    select(func.count())
                    .select_from(dataset_revisions)
                    .join(
                        publication_revisions,
                        and_(
                            publication_revisions.c.owner_user_id
                            == dataset_revisions.c.owner_user_id,
                            publication_revisions.c.dataset_revision_id
                            == dataset_revisions.c.dataset_revision_id,
                        ),
                    )
                    .where(
                        and_(
                            publication_revisions.c.owner_user_id == owner,
                            publication_revisions.c.publication_id == publication_id,
                            dataset_revisions.c.status != "published",
                        )
                    ),
                    select(func.count())
                    .select_from(archive_objects)
                    .where(
                        and_(
                            archive_objects.c.owner_user_id == owner,
                            archive_objects.c.publication_id == publication_id,
                            archive_objects.c.state != "published",
                        )
                    ),
                    select(func.count())
                    .select_from(publication_files)
                    .where(
                        and_(
                            publication_files.c.owner_user_id == owner,
                            publication_files.c.publication_id == publication_id,
                            publication_files.c.state != "published",
                        )
                    ),
                    select(func.count())
                    .select_from(publication_retention_conversions)
                    .where(
                        and_(
                            publication_retention_conversions.c.owner_user_id == owner,
                            publication_retention_conversions.c.publication_id == publication_id,
                            publication_retention_conversions.c.state != "converted",
                        )
                    ),
                )
            )
            if incomplete_catalog:
                return CleanupResult(
                    publication_id=publication_id,
                    deleted_active_count=0,
                    blocked_active_count=0,
                    complete=False,
                    replayed=True,
                )
            pending = (
                c.execute(
                    select(publication_cleanup_bars)
                    .where(
                        and_(
                            publication_cleanup_bars.c.owner_user_id == owner,
                            publication_cleanup_bars.c.publication_id == publication_id,
                            publication_cleanup_bars.c.cleaned_at.is_(None),
                        )
                    )
                    .order_by(publication_cleanup_bars.c.bar_record_id)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            deleted_count = 0
            blocked_count = 0
            now = self.catalog._now()
            for row in pending:
                self._inject("during_cleanup")
                locked_active = c.execute(
                    select(active_bars.c.bar_record_id)
                    .where(
                        and_(
                            active_bars.c.owner_user_id == owner,
                            active_bars.c.bar_record_id == row["bar_record_id"],
                        )
                    )
                    .with_for_update()
                ).scalar()
                if locked_active is None:
                    c.execute(
                        update(publication_cleanup_bars)
                        .where(
                            and_(
                                publication_cleanup_bars.c.owner_user_id == owner,
                                publication_cleanup_bars.c.publication_id == publication_id,
                                publication_cleanup_bars.c.bar_record_id == row["bar_record_id"],
                            )
                        )
                        .values(cleaned_at=now)
                    )
                    continue
                active_retention = c.execute(
                    select(bar_retention_refs.c.bar_record_id)
                    .where(
                        and_(
                            bar_retention_refs.c.owner_user_id == owner,
                            bar_retention_refs.c.bar_record_id == row["bar_record_id"],
                        )
                    )
                    .limit(1)
                ).scalar()
                snapshot_reference = c.execute(
                    select(read_snapshot_bars.c.read_snapshot_id)
                    .join(
                        read_snapshots,
                        and_(
                            read_snapshots.c.owner_user_id == read_snapshot_bars.c.owner_user_id,
                            read_snapshots.c.read_snapshot_id
                            == read_snapshot_bars.c.read_snapshot_id,
                        ),
                    )
                    .where(
                        and_(
                            read_snapshot_bars.c.owner_user_id == owner,
                            read_snapshots.c.expires_at > now,
                            read_snapshot_bars.c.bar_record_id == row["bar_record_id"],
                        )
                    )
                    .limit(1)
                ).scalar()
                if active_retention or snapshot_reference:
                    blocked_count += 1
                    continue
                deleted_count += int(
                    c.execute(
                        delete(active_bars).where(
                            and_(
                                active_bars.c.owner_user_id == owner,
                                active_bars.c.bar_record_id == row["bar_record_id"],
                            )
                        )
                    ).rowcount
                    or 0
                )
                c.execute(
                    update(publication_cleanup_bars)
                    .where(
                        and_(
                            publication_cleanup_bars.c.owner_user_id == owner,
                            publication_cleanup_bars.c.publication_id == publication_id,
                            publication_cleanup_bars.c.bar_record_id == row["bar_record_id"],
                        )
                    )
                    .values(cleaned_at=now)
                )
            return CleanupResult(
                publication_id=publication_id,
                deleted_active_count=deleted_count,
                blocked_active_count=blocked_count,
                complete=blocked_count == 0,
                replayed=not pending,
            )

    def sweep_orphan_temps(self) -> OrphanSweepResult:
        scanned = abandoned = quarantined = skipped = 0
        cutoff = self.catalog._now() - timedelta(hours=24)
        temp_name = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.[A-Za-z0-9-]{1,40}\.tmp$"
        )
        with self.catalog.engine.begin() as c:
            if not c.execute(select(func.pg_try_advisory_xact_lock(0x46543035))).scalar_one():
                return OrphanSweepResult(
                    scanned_count=0,
                    abandoned_count=0,
                    quarantined_count=0,
                    skipped_live_count=0,
                )
            owners = tuple(c.execute(select(users.c.user_id).order_by(users.c.user_id)).scalars())
            now = self.catalog._now()
            for owner_id in owners:
                prestage_rows = (
                    c.execute(
                        select(prestage_writes).where(
                            and_(
                                prestage_writes.c.owner_user_id == owner_id,
                                prestage_writes.c.state.in_(("writing", "staged")),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                protected = {
                    name
                    for row in prestage_rows
                    if row["lease_expires_at"] > now
                    for name in row["expected_temp_names"]
                }
                protected.update(
                    c.execute(
                        select(publication_files.c.temp_name)
                        .join(
                            publications,
                            and_(
                                publications.c.owner_user_id == publication_files.c.owner_user_id,
                                publications.c.publication_id == publication_files.c.publication_id,
                            ),
                        )
                        .where(
                            and_(
                                publication_files.c.owner_user_id == owner_id,
                                publications.c.state == "staged",
                            )
                        )
                    ).scalars()
                )
                owner_root = self.store._owner_root(owner_id)
                staging = owner_root / "staging"
                if not staging.is_dir() or staging.is_symlink():
                    continue
                for path in staging.glob("*.tmp"):
                    scanned += 1
                    if (
                        not temp_name.fullmatch(path.name)
                        or path.name in protected
                        or path.is_symlink()
                        or path.stat().st_nlink != 1
                        or datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff
                    ):
                        skipped += 1
                        continue
                    target = owner_root / "quarantine" / str(uuid7())
                    os.replace(path, target)
                    self.store._fsync_directory(staging)
                    self.store._fsync_directory(target.parent)
                    abandoned += 1
                    quarantined += 1
                    for row in prestage_rows:
                        if path.name in row["expected_temp_names"]:
                            c.execute(
                                update(prestage_writes)
                                .where(
                                    and_(
                                        prestage_writes.c.owner_user_id == owner_id,
                                        prestage_writes.c.series_id == row["series_id"],
                                        prestage_writes.c.idempotency_key == row["idempotency_key"],
                                        prestage_writes.c.temp_uuid == row["temp_uuid"],
                                    )
                                )
                                .values(state="abandoned")
                            )
        return OrphanSweepResult(
            scanned_count=scanned,
            abandoned_count=abandoned,
            quarantined_count=quarantined,
            skipped_live_count=skipped,
        )
