"""Strict public records for the FT-05 market-data boundary."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @model_validator(mode="after")
    def validate_common_contract_fields(self) -> FrozenModel:
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime) and (
                value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
            ):
                raise ValueError(f"{name} must be an aware UTC datetime")
            if value is None or not isinstance(value, str):
                continue
            if name.endswith("_sha256") and not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
            if name.endswith("_id") and name not in {"provider_contract_id"}:
                try:
                    parsed = __import__("uuid").UUID(value)
                except ValueError:
                    raise ValueError(f"{name} must be a lowercase UUIDv7") from None
                if parsed.version != 7 or str(parsed) != value:
                    raise ValueError(f"{name} must be a lowercase UUIDv7")
        return self


class MarketDataCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"
    ARCHIVE_INTEGRITY = "ARCHIVE_INTEGRITY"
    CALENDAR_COVERAGE_MISSING = "CALENDAR_COVERAGE_MISSING"
    STALE_DATA = "STALE_DATA"
    STALE_VERSION = "STALE_VERSION"
    CONFLICT = "CONFLICT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"


class MarketDataError(Exception):
    def __init__(
        self,
        code: MarketDataCode,
        message: str,
        http_status: int,
        *,
        retryable: bool = False,
        public_details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status
        self.retryable, self.public_details = retryable, public_details or {}

    def envelope(self, request_id: str) -> dict[str, Any]:
        return {
            "schema_version": "v1",
            "request_id": request_id,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.public_details,
            },
        }


def canonical_json_bytes(value: Any) -> bytes:
    def convert(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return convert(item.model_dump(mode="json"))
        if isinstance(item, dict):
            return {
                unicodedata.normalize("NFC", str(k)): convert(v) for k, v in sorted(item.items())
            }
        if isinstance(item, (tuple, list)):
            return [convert(v) for v in item]
        if isinstance(item, Decimal):
            text = format(item, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return "0" if text in {"-0", ""} else text
        if isinstance(item, datetime):
            utc = item.isoformat(timespec="microseconds").replace("+00:00", "Z")
            return utc.replace(".000000Z", "Z")
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, StrEnum):
            return item.value
        return item

    return json.dumps(
        convert(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


PositiveInt = Annotated[int, Field(gt=0)]


class SeriesKey(FrozenModel):
    source: Annotated[str, Field(min_length=1, max_length=80)]
    price_basis: Literal["trades"]
    contract_id: str
    interval_seconds: Literal[60, 300, 900, 1800, 3600]


class OwnedSeriesKey(SeriesKey):
    owner_user_id: str


class LogicalBarKey(OwnedSeriesKey):
    start_at: datetime


class CompletedBar(FrozenModel):
    schema_version: Literal["v1"] = "v1"
    bar_record_id: str
    owner_user_id: str
    source: str
    price_basis: Literal["trades"]
    contract_id: str
    interval_seconds: Literal[60, 300, 900, 1800, 3600]
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_revision: PositiveInt
    received_at: datetime
    completed_at: datetime
    quality: Literal["valid", "missing", "invalid", "duplicate_conflict"]
    supersedes_bar_record_id: str | None
    payload_hash: str
    created_at: datetime
    record_version: Literal[1] = 1


class FuturesContract(FrozenModel):
    schema_version: Literal["v1"] = "v1"
    contract_id: str
    owner_user_id: str
    provider: str
    provider_contract_id: str
    root_symbol: str
    exchange: str
    currency: str
    tick_size: Decimal
    multiplier: Decimal
    expiry_label: str
    first_trade_at: datetime | None
    last_trade_at: datetime
    exchange_timezone: str
    calendar_id: str
    calendar_version: PositiveInt
    entry_cutoff_at: datetime
    liquidation_start_at: datetime
    metadata_as_of: datetime
    provenance_ref: str
    created_at: datetime
    record_version: PositiveInt


class PartitionRef(FrozenModel):
    object_id: str
    uri: str
    sha256: str
    byte_length: PositiveInt
    row_count: PositiveInt
    min_start_at: datetime
    max_end_at: datetime
    min_source_revision: PositiveInt
    max_source_revision: PositiveInt
    origin_publication_id: str

    @model_validator(mode="after")
    def validate_partition_bounds(self) -> PartitionRef:
        if (
            self.uri != f"ft-archive://object/{self.object_id}"
            or self.min_start_at >= self.max_end_at
            or self.min_source_revision > self.max_source_revision
        ):
            raise ValueError("invalid immutable partition bounds")
        return self


CorrectionReason = Literal["SOURCE_CORRECTION", "REPAIR_REPLACEMENT", "DERIVED_COMPONENT_CHANGE"]


class CorrectionRef(FrozenModel):
    logical_bar_key: LogicalBarKey
    old_bar_record_id: str
    new_bar_record_id: str
    reason: CorrectionReason
    received_at: datetime


class SourceWatermark(FrozenModel):
    kind: Literal["familytrade_received_v1"] = "familytrade_received_v1"
    max_received_at: datetime
    max_bar_record_id: str


class DatasetRevision(FrozenModel):
    schema_version: Literal["v1"] = "v1"
    dataset_revision_id: str
    owner_user_id: str
    series_key: OwnedSeriesKey
    contract_version: PositiveInt
    parent_revision_id: str | None
    manifest_uri: str
    manifest_sha256: str
    manifest_byte_length: PositiveInt
    partition_refs: tuple[PartitionRef, ...]
    calendar_id: str
    calendar_version: PositiveInt
    coverage_start: datetime
    coverage_end: datetime
    source_watermark: SourceWatermark
    correction_refs: tuple[CorrectionRef, ...]
    status: Literal["building", "published", "quarantined"]
    created_at: datetime
    published_at: datetime | None
    parent_depth: int
    restore_closure_revision_count: PositiveInt
    restore_closure_row_count: int
    restore_closure_bytes: int
    rollover_from_revision_id: str | None = None
    rollover_from_manifest_uri: str | None = None
    rollover_from_manifest_sha256: str | None = None
    recovery_from_quarantined_revision_id: str | None = None
    recovery_from_manifest_uri: str | None = None
    recovery_from_manifest_sha256: str | None = None
    record_version: PositiveInt

    @model_validator(mode="after")
    def validate_revision_lifecycle(self) -> DatasetRevision:
        rollover = (
            self.rollover_from_revision_id,
            self.rollover_from_manifest_uri,
            self.rollover_from_manifest_sha256,
        )
        recovery = (
            self.recovery_from_quarantined_revision_id,
            self.recovery_from_manifest_uri,
            self.recovery_from_manifest_sha256,
        )
        if (
            self.owner_user_id != self.series_key.owner_user_id
            or self.manifest_uri != f"ft-archive://manifest/{self.dataset_revision_id}"
            or self.coverage_start >= self.coverage_end
            or len(self.partition_refs) > 2000
            or not 0 <= self.parent_depth <= 999
            or self.restore_closure_revision_count > 1000
            or not 0 <= self.restore_closure_row_count <= 2_000_000
            or not 0 <= self.restore_closure_bytes <= 10 * 1024**3
            or (self.status == "published" and self.published_at is None)
            or (self.status == "building" and self.published_at is not None)
            or any(item is None for item in rollover) != all(item is None for item in rollover)
            or any(item is None for item in recovery) != all(item is None for item in recovery)
            or (
                all(item is not None for item in rollover)
                and all(item is not None for item in recovery)
            )
        ):
            raise ValueError("invalid dataset revision lifecycle")
        return self


class CalendarWindowInput(FrozenModel):
    kind: Literal["open", "maintenance", "scheduled_closed"]
    start_at: datetime
    end_at: datetime
    trading_day: date | None
    reason: str | None


class CalendarCreateInput(FrozenModel):
    exchange_timezone: str
    coverage_start: datetime
    coverage_end: datetime
    windows: Annotated[tuple[CalendarWindowInput, ...], Field(min_length=1, max_length=10000)]
    metadata_as_of: datetime
    provenance_ref: str


class CalendarVersionAppendInput(CalendarCreateInput):
    expected_previous_version: PositiveInt


class CalendarWindow(CalendarWindowInput):
    ordinal: int


class CalendarVersion(FrozenModel):
    schema_version: Literal["v1"] = "v1"
    calendar_id: str
    owner_user_id: str
    calendar_version: PositiveInt
    exchange_timezone: str
    coverage_start: datetime
    coverage_end: datetime
    windows: tuple[CalendarWindow, ...]
    metadata_as_of: datetime
    provenance_ref: str
    created_at: datetime
    record_version: PositiveInt


class FuturesContractInput(FrozenModel):
    contract_id: str | None = None
    provider: str
    provider_contract_id: str
    root_symbol: str
    exchange: str
    currency: str
    tick_size: Decimal
    multiplier: Decimal
    expiry_label: str
    first_trade_at: datetime | None = None
    last_trade_at: datetime
    calendar_id: str
    calendar_version: PositiveInt
    entry_cutoff_at: datetime
    liquidation_start_at: datetime
    metadata_as_of: datetime
    provenance_ref: str
    expected_version: int | None = None


class CompletedBarVersionInput(FrozenModel):
    source: str
    price_basis: Literal["trades"]
    contract_id: str
    interval_seconds: Literal[60, 300, 900, 1800, 3600]
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_revision: PositiveInt
    completed_at: datetime
    quality: Literal["valid"]
    supersedes_bar_record_id: str | None = None
    correction_reason: Literal["SOURCE_CORRECTION", "REPAIR_REPLACEMENT"] | None = None


class RecordBatchInput(FrozenModel):
    bars: Annotated[tuple[CompletedBarVersionInput, ...], Field(min_length=1, max_length=10000)]


class RecordBatchResult(FrozenModel):
    batch_id: str
    inserted_bar_record_ids: tuple[str, ...]
    replayed_bar_record_ids: tuple[str, ...]


class PinnedRead(FrozenModel):
    kind: Literal["pinned"] = "pinned"
    dataset_revision_id: str


class LatestRead(FrozenModel):
    kind: Literal["latest"] = "latest"


class CausalCommittedSelection(FrozenModel):
    start_at: datetime
    bar_record_id: str
    committed_at: datetime


class CorrectionObservation(FrozenModel):
    start_at: datetime
    old_bar_record_id: str
    new_bar_record_id: str
    received_at: datetime
    applied: bool
    reason: Literal["BEFORE_CURSOR_APPLIED", "LATE_AFTER_CURSOR"]


class CausalLatestRead(FrozenModel):
    kind: Literal["causal_latest"] = "causal_latest"
    known_at: datetime
    committed_through: datetime | None = None
    committed_selections: Annotated[
        tuple[CausalCommittedSelection, ...], Field(max_length=5000)
    ] = ()

    @model_validator(mode="after")
    def validate_commitment_frontier(self) -> CausalLatestRead:
        starts = tuple(item.start_at for item in self.committed_selections)
        if (
            starts != tuple(sorted(starts))
            or len(set(starts)) != len(starts)
            or any(
                not item.start_at < item.committed_at <= self.known_at
                for item in self.committed_selections
            )
            or (self.committed_through is not None and self.committed_through > self.known_at)
        ):
            raise ValueError("invalid causal commitment frontier")
        return self


ReadPolicy = Annotated[PinnedRead | LatestRead | CausalLatestRead, Field(discriminator="kind")]


class ReadCursor(FrozenModel):
    snapshot_id: str
    after_ordinal: Annotated[int, Field(ge=0)]
    canonical_request_sha256: str


class ReadBarsRequest(FrozenModel):
    series_key: SeriesKey
    coverage_start: datetime
    coverage_end: datetime
    policy: ReadPolicy
    require_complete: bool = False
    cursor: ReadCursor | None = None
    limit: Annotated[int, Field(ge=1, le=5000)] = 5000


class CoverageRequest(FrozenModel):
    series_key: SeriesKey
    coverage_start: datetime
    coverage_end: datetime
    dataset_revision_id: str | None = None


class CoverageSpan(FrozenModel):
    start_at: datetime
    end_at: datetime
    kind: Literal["open", "missing", "invalid", "maintenance", "scheduled_closed"]
    reason: Literal["OPEN", "NO_VALID_BAR", "INVALID_BAR", "MAINTENANCE", "SCHEDULED_CLOSED"]
    selected_bar_count: int


class CoverageResult(FrozenModel):
    spans: tuple[CoverageSpan, ...]
    quality_counts: dict[str, int]
    dataset_revision_id: str | None


class BarSelection(FrozenModel):
    bar: CompletedBar
    origin: Literal["archive", "active"]
    availability_at: datetime
    correction_observations: tuple[CorrectionObservation, ...] = ()


class ReadBarsResult(FrozenModel):
    selections: tuple[BarSelection, ...]
    coverage: CoverageResult
    next_cursor: ReadCursor | None
    published_base_revision_id: str | None


class PublicationRequest(FrozenModel):
    series_key: SeriesKey
    coverage_start: datetime
    coverage_end: datetime
    expected_parent_revision_id: str | None = None


class PublicationResult(FrozenModel):
    publication_id: str
    dataset_revision: DatasetRevision
    preserved_revision_ids: tuple[str, ...]
    replayed: bool


class RetainedManifestRestoreRequest(FrozenModel):
    manifest_uri: str
    expected_manifest_sha256: str


class QuarantinedLatestRecoveryRequest(FrozenModel):
    quarantined_revision_id: str
    expected_manifest_sha256: str


class RestoreResult(FrozenModel):
    dataset_revision: DatasetRevision
    catalog_rows_restored: int
    latest_pointer_changed: bool
    replayed: bool


class RecoveryResult(FrozenModel):
    publication_id: str
    terminal_state: Literal["published", "quarantined"]
    action: Literal["completed", "quarantined", "noop"]
    replayed: bool


class CleanupResult(FrozenModel):
    publication_id: str
    deleted_active_count: int
    blocked_active_count: int
    complete: bool
    replayed: bool


class OrphanSweepResult(FrozenModel):
    scanned_count: int
    abandoned_count: int
    quarantined_count: int
    skipped_live_count: int


class ReadSnapshotSweepResult(FrozenModel):
    expired_snapshot_count: int
    deleted_snapshot_row_count: int
    pending_cleanup_publication_ids: tuple[str, ...]


class AggregatedFullBar(FrozenModel):
    target_interval_seconds: Literal[300, 900, 1800, 3600]
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_bar_record_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=60)]
    scheduled_partial: Literal[False] = False


class ScheduledPartialBar(FrozenModel):
    target_interval_seconds: Literal[300, 900, 1800, 3600]
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_bar_record_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=60)]
    scheduled_partial: Literal[True] = True


class AggregateGap(FrozenModel):
    start_at: datetime
    end_at: datetime
    reason: Literal[
        "MISSING_COMPONENT",
        "INVALID_COMPONENT",
        "UNSCHEDULED_PARTIAL",
        "SCHEDULED_PARTIAL_REJECTED",
    ]
    expected_component_starts: Annotated[tuple[datetime, ...], Field(max_length=60)]

    @model_validator(mode="after")
    def validate_expected_components(self) -> AggregateGap:
        if (
            self.start_at >= self.end_at
            or tuple(sorted(set(self.expected_component_starts))) != self.expected_component_starts
            or any(
                not self.start_at <= item < self.end_at for item in self.expected_component_starts
            )
        ):
            raise ValueError("invalid aggregate gap components")
        return self


class AggregateRequest(FrozenModel):
    source_bars: tuple[CompletedBar, ...]
    calendar: CalendarVersion
    target_interval_seconds: Literal[300, 900, 1800, 3600]
    partial_policy: Literal["reject", "scheduled_partial"] = "reject"


class AggregateResult(FrozenModel):
    full_bars: tuple[AggregatedFullBar, ...]
    scheduled_partial_bars: tuple[ScheduledPartialBar, ...]
    rejected_gaps: tuple[AggregateGap, ...]


class RetentionRef(FrozenModel):
    owner_user_id: str
    dataset_revision_id: str
    reference_kind: Literal["run", "lane", "legal_hold"]
    reference_id: str
    created_at: datetime
    replayed: bool


class BarRetentionRef(FrozenModel):
    owner_user_id: str
    bar_record_id: str
    reference_kind: Literal["lane"]
    reference_id: str
    state: Literal["active", "revision"]
    dataset_revision_id: str | None
    created_at: datetime
    replayed: bool
