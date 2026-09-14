"""PostgreSQL catalog for immutable owner-scoped market data."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid7

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine, RowMapping

from familytrade.access.models import (
    AccessError,
    ErrorCode,
    UserContext,
    not_found,
    unauthenticated,
)
from familytrade.access.repository import access_metadata, users
from familytrade.market_data.calendar import validate_calendar
from familytrade.market_data.models import (
    AggregatedFullBar,
    BarRetentionRef,
    CalendarCreateInput,
    CalendarVersion,
    CalendarVersionAppendInput,
    CalendarWindow,
    CompletedBar,
    FuturesContract,
    FuturesContractInput,
    MarketDataCode,
    MarketDataError,
    RecordBatchInput,
    RecordBatchResult,
    RetentionRef,
    SeriesKey,
    canonical_sha256,
)

market_data_metadata = MetaData()
if users.metadata is not access_metadata:  # pragma: no cover - import binding invariant
    raise RuntimeError("Integrated access metadata binding is inconsistent.")


def _id(name: str, primary_key: bool = False) -> Column[str]:
    return Column(name, String(36), primary_key=primary_key, nullable=False)


calendar_versions = Table(
    "market_data_calendar_versions",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("calendar_id", True),
    Column("calendar_version", Integer, primary_key=True),
    Column("schema_version", String(8), nullable=False),
    Column("exchange_timezone", String(128), nullable=False),
    Column("coverage_start", DateTime(timezone=True), nullable=False),
    Column("coverage_end", DateTime(timezone=True), nullable=False),
    Column("metadata_as_of", DateTime(timezone=True), nullable=False),
    Column("provenance_ref", Text, nullable=False),
    Column("payload_sha256", CHAR(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record_version", Integer, nullable=False),
    ForeignKeyConstraint(["owner_user_id"], [users.c.user_id]),
    CheckConstraint(
        "schema_version='v1' AND coverage_start < coverage_end AND record_version=calendar_version",
        name="ck_md_calendar_core",
    ),
)
calendar_windows = Table(
    "market_data_calendar_windows",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("calendar_id", True),
    Column("calendar_version", Integer, primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("end_at", DateTime(timezone=True), nullable=False),
    Column("trading_day", Date),
    Column("reason", String(128)),
    ForeignKeyConstraint(
        ["owner_user_id", "calendar_id", "calendar_version"],
        [
            calendar_versions.c.owner_user_id,
            calendar_versions.c.calendar_id,
            calendar_versions.c.calendar_version,
        ],
        ondelete="CASCADE",
    ),
    CheckConstraint("start_at < end_at", name="ck_md_calendar_window_range"),
    CheckConstraint(
        "(kind='open' AND trading_day IS NOT NULL AND reason IS NULL) OR (kind IN ('maintenance','scheduled_closed') AND trading_day IS NULL AND reason IS NOT NULL)",
        name="ck_md_calendar_window_kind",
    ),
)
contracts = Table(
    "market_data_contracts",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("contract_id", True),
    Column("provider", String(80), nullable=False),
    Column("provider_contract_id", String(200), nullable=False),
    Column("current_contract_version", Integer, nullable=False),
    Column("series_binding_version", Integer),
    ForeignKeyConstraint(["owner_user_id"], [users.c.user_id]),
    UniqueConstraint(
        "owner_user_id", "provider", "provider_contract_id", name="uq_md_contract_provider"
    ),
    CheckConstraint(
        "current_contract_version >= 1 AND (series_binding_version IS NULL OR series_binding_version >= 1)",
        name="ck_md_contract_head_versions",
    ),
)
contract_versions = Table(
    "market_data_contract_versions",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("contract_id", True),
    Column("contract_version", Integer, primary_key=True),
    Column("schema_version", String(8), nullable=False),
    Column("provider", String(80), nullable=False),
    Column("provider_contract_id", String(200), nullable=False),
    Column("root_symbol", String(80), nullable=False),
    Column("exchange", String(80), nullable=False),
    Column("currency", String(8), nullable=False),
    Column("tick_size", Numeric, nullable=False),
    Column("multiplier", Numeric, nullable=False),
    Column("expiry_label", String(80), nullable=False),
    Column("first_trade_at", DateTime(timezone=True)),
    Column("last_trade_at", DateTime(timezone=True), nullable=False),
    Column("exchange_timezone", String(128), nullable=False),
    _id("calendar_id"),
    Column("calendar_version", Integer, nullable=False),
    Column("entry_cutoff_at", DateTime(timezone=True), nullable=False),
    Column("liquidation_start_at", DateTime(timezone=True), nullable=False),
    Column("metadata_as_of", DateTime(timezone=True), nullable=False),
    Column("provenance_ref", Text, nullable=False),
    Column("projection_sha256", CHAR(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record_version", Integer, nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "contract_id"],
        [contracts.c.owner_user_id, contracts.c.contract_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "calendar_id", "calendar_version"],
        [
            calendar_versions.c.owner_user_id,
            calendar_versions.c.calendar_id,
            calendar_versions.c.calendar_version,
        ],
    ),
    UniqueConstraint(
        "owner_user_id",
        "contract_id",
        "contract_version",
        "tick_size",
        name="uq_md_contract_tick_ref",
    ),
    CheckConstraint(
        "schema_version='v1' AND tick_size > 0 AND multiplier > 0 AND record_version=contract_version "
        "AND projection_sha256 ~ '^[0-9a-f]{64}$' "
        "AND (first_trade_at IS NULL OR first_trade_at < last_trade_at)",
        name="ck_md_contract_core",
    ),
    CheckConstraint(
        "entry_cutoff_at < liquidation_start_at AND liquidation_start_at < last_trade_at",
        name="ck_md_contract_expiry",
    ),
)
series = Table(
    "market_data_series",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("series_id", True),
    Column("source", String(80), nullable=False),
    Column("price_basis", String(16), nullable=False),
    _id("contract_id"),
    Column("interval_seconds", Integer, nullable=False),
    Column("contract_version", Integer, nullable=False),
    _id("calendar_id"),
    Column("calendar_version", Integer, nullable=False),
    Column("latest_revision_id", String(36)),
    Column("record_version", Integer, nullable=False, default=1),
    ForeignKeyConstraint(
        ["owner_user_id", "contract_id", "contract_version"],
        [
            contract_versions.c.owner_user_id,
            contract_versions.c.contract_id,
            contract_versions.c.contract_version,
        ],
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "calendar_id", "calendar_version"],
        [
            calendar_versions.c.owner_user_id,
            calendar_versions.c.calendar_id,
            calendar_versions.c.calendar_version,
        ],
    ),
    UniqueConstraint(
        "owner_user_id",
        "series_id",
        "contract_version",
        "calendar_id",
        "calendar_version",
        name="uq_md_series_binding",
    ),
    UniqueConstraint(
        "owner_user_id",
        "source",
        "price_basis",
        "contract_id",
        "interval_seconds",
        name="uq_md_series_key",
    ),
    CheckConstraint(
        "price_basis='trades' AND interval_seconds IN (60,300,900,1800,3600) "
        "AND record_version >= 1",
        name="ck_md_series_contract",
    ),
)
bar_versions = Table(
    "market_data_bar_versions",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("bar_record_id", True),
    Column("schema_version", String(8), nullable=False),
    _id("series_id"),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("payload_hash", CHAR(64), nullable=False),
    Column("supersedes_bar_record_id", String(36)),
    Column("correction_reason", String(40)),
    Column("aggregate_lineage_sha256", CHAR(64)),
    Column("version_fingerprint_sha256", CHAR(64), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("quality", String(24), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record_version", Integer, nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "supersedes_bar_record_id"],
        ["market_data_bar_versions.owner_user_id", "market_data_bar_versions.bar_record_id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    UniqueConstraint(
        "owner_user_id", "series_id", "start_at", "source_revision", name="uq_md_bar_revision"
    ),
    CheckConstraint(
        "source_revision > 0 AND quality='valid' AND schema_version='v1' AND record_version=1",
        name="ck_md_bar_registry_core",
    ),
    CheckConstraint(
        "payload_hash ~ '^[0-9a-f]{64}$' AND version_fingerprint_sha256 ~ '^[0-9a-f]{64}$' "
        "AND (aggregate_lineage_sha256 IS NULL OR aggregate_lineage_sha256 ~ '^[0-9a-f]{64}$')",
        name="ck_md_bar_registry_hashes",
    ),
    CheckConstraint(
        "schema_version='v1' AND quality='valid' AND source_revision > 0 AND created_at=received_at AND record_version=1",
        name="ck_md_bar_version",
    ),
    CheckConstraint(
        "(source_revision=1 AND supersedes_bar_record_id IS NULL AND correction_reason IS NULL) OR "
        "(source_revision>1 AND supersedes_bar_record_id IS NOT NULL AND correction_reason IN "
        "('SOURCE_CORRECTION','REPAIR_REPLACEMENT','DERIVED_COMPONENT_CHANGE'))",
        name="ck_md_bar_chain_shape",
    ),
)
active_bars = Table(
    "market_data_active_bars",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("bar_record_id", True),
    _id("series_id"),
    Column("schema_version", String(8), nullable=False),
    Column("source", String(80), nullable=False),
    Column("price_basis", String(16), nullable=False),
    _id("contract_id"),
    Column("contract_version", Integer, nullable=False),
    Column("interval_seconds", Integer, nullable=False),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("end_at", DateTime(timezone=True), nullable=False),
    Column("open", Numeric, nullable=False),
    Column("high", Numeric, nullable=False),
    Column("low", Numeric, nullable=False),
    Column("close", Numeric, nullable=False),
    Column("volume", Numeric, nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("quality", String(24), nullable=False),
    Column("supersedes_bar_record_id", String(36)),
    Column("payload_hash", CHAR(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("record_version", Integer, nullable=False),
    Column("tick_size", Numeric, nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "contract_id", "contract_version", "tick_size"],
        [
            contract_versions.c.owner_user_id,
            contract_versions.c.contract_id,
            contract_versions.c.contract_version,
            contract_versions.c.tick_size,
        ],
    ),
    CheckConstraint(
        "schema_version='v1' AND quality='valid' AND record_version=1 AND volume >= 0 AND trunc(volume)=volume",
        name="ck_md_active_core",
    ),
    CheckConstraint(
        "low <= open AND low <= close AND open <= high AND close <= high", name="ck_md_active_ohlc"
    ),
    CheckConstraint(
        "end_at=start_at + make_interval(secs => interval_seconds)", name="ck_md_active_duration"
    ),
    CheckConstraint(
        "mod(open,tick_size)=0 AND mod(high,tick_size)=0 AND mod(low,tick_size)=0 AND mod(close,tick_size)=0",
        name="ck_md_active_ticks",
    ),
)
dataset_revisions = Table(
    "market_data_dataset_revisions",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("dataset_revision_id", True),
    Column("schema_version", String(8), nullable=False),
    _id("series_id"),
    Column("contract_version", Integer, nullable=False),
    _id("calendar_id"),
    Column("calendar_version", Integer, nullable=False),
    Column("projection", JSONB, nullable=False),
    Column("parent_revision_id", String(36)),
    Column("manifest_uri", String(100), nullable=False),
    Column("manifest_sha256", CHAR(64), nullable=False),
    Column("manifest_byte_length", BigInteger, nullable=False),
    Column("coverage_start", DateTime(timezone=True), nullable=False),
    Column("coverage_end", DateTime(timezone=True), nullable=False),
    Column("source_watermark", JSONB, nullable=False),
    Column("correction_refs", JSONB, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True)),
    Column("parent_depth", Integer, nullable=False),
    Column("restore_closure_revision_count", Integer, nullable=False),
    Column("restore_closure_row_count", Integer, nullable=False),
    Column("restore_closure_bytes", BigInteger, nullable=False),
    Column("rollover_from_revision_id", String(36)),
    Column("rollover_from_manifest_uri", String(100)),
    Column("rollover_from_manifest_sha256", CHAR(64)),
    Column("recovery_from_quarantined_revision_id", String(36)),
    Column("recovery_from_manifest_uri", String(100)),
    Column("recovery_from_manifest_sha256", CHAR(64)),
    Column("record_version", Integer, nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    ForeignKeyConstraint(
        [
            "owner_user_id",
            "series_id",
            "contract_version",
            "calendar_id",
            "calendar_version",
        ],
        [
            series.c.owner_user_id,
            series.c.series_id,
            series.c.contract_version,
            series.c.calendar_id,
            series.c.calendar_version,
        ],
    ),
    UniqueConstraint("owner_user_id", "manifest_uri", name="uq_md_manifest_uri"),
    CheckConstraint(
        "schema_version='v1' AND coverage_start < coverage_end AND "
        "status IN ('building','published','quarantined') AND parent_depth BETWEEN 0 AND 999 AND "
        "restore_closure_revision_count BETWEEN 1 AND 1000 AND "
        "restore_closure_row_count BETWEEN 0 AND 2000000 AND "
        "restore_closure_bytes BETWEEN 0 AND 10737418240",
        name="ck_md_revision_core",
    ),
    CheckConstraint(
        "manifest_sha256 ~ '^[0-9a-f]{64}$' AND manifest_byte_length > 0 AND "
        "((status='building' AND record_version=1 AND published_at IS NULL) OR "
        "(status='published' AND record_version=2 AND published_at IS NOT NULL) OR "
        "(status='quarantined' AND record_version IN (2,3)))",
        name="ck_md_revision_lifecycle_projection",
    ),
    CheckConstraint(
        "(rollover_from_revision_id IS NULL AND rollover_from_manifest_uri IS NULL AND rollover_from_manifest_sha256 IS NULL) OR "
        "(rollover_from_revision_id IS NOT NULL AND rollover_from_manifest_uri IS NOT NULL AND rollover_from_manifest_sha256 IS NOT NULL)",
        name="ck_md_revision_rollover_shape",
    ),
    CheckConstraint(
        "(recovery_from_quarantined_revision_id IS NULL AND recovery_from_manifest_uri IS NULL AND recovery_from_manifest_sha256 IS NULL) OR "
        "(recovery_from_quarantined_revision_id IS NOT NULL AND recovery_from_manifest_uri IS NOT NULL AND recovery_from_manifest_sha256 IS NOT NULL)",
        name="ck_md_revision_recovery_shape",
    ),
    CheckConstraint(
        "NOT (rollover_from_revision_id IS NOT NULL AND recovery_from_quarantined_revision_id IS NOT NULL)",
        name="ck_md_revision_provenance_exclusive",
    ),
)
revision_bars = Table(
    "market_data_revision_bars",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("dataset_revision_id", True),
    Column("ordinal", Integer, primary_key=True),
    _id("series_id"),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("source_revision", Integer, nullable=False),
    _id("bar_record_id"),
    _id("object_id"),
    ForeignKeyConstraint(
        ["owner_user_id", "dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "object_id"],
        ["market_data_archive_objects.owner_user_id", "market_data_archive_objects.object_id"],
    ),
    UniqueConstraint(
        "owner_user_id", "dataset_revision_id", "start_at", name="uq_md_revision_logical_bar"
    ),
    UniqueConstraint(
        "owner_user_id",
        "dataset_revision_id",
        "bar_record_id",
        name="uq_md_revision_selected_bar",
    ),
)
archive_objects = Table(
    "market_data_archive_objects",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("object_id", True),
    _id("series_id"),
    Column("uri", String(100), nullable=False),
    Column("sha256", CHAR(64), nullable=False),
    Column("byte_length", BigInteger, nullable=False),
    Column("row_count", Integer, nullable=False),
    Column("min_start_at", DateTime(timezone=True), nullable=False),
    Column("max_end_at", DateTime(timezone=True), nullable=False),
    Column("min_source_revision", Integer, nullable=False),
    Column("max_source_revision", Integer, nullable=False),
    Column("state", String(16), nullable=False),
    _id("origin_publication_id"),
    Column("publication_id", String(36)),
    Column("catalog_origin", String(24), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    UniqueConstraint("owner_user_id", "uri", name="uq_md_object_uri"),
    CheckConstraint(
        "(catalog_origin='publication' AND publication_id IS NOT NULL AND publication_id=origin_publication_id) OR "
        "(catalog_origin='retained_manifest' AND publication_id IS NULL)",
        name="ck_md_object_origin",
    ),
    CheckConstraint(
        "byte_length > 0 AND row_count > 0 AND min_start_at < max_end_at AND "
        "min_source_revision > 0 AND min_source_revision <= max_source_revision AND "
        "state IN ('staged','published','quarantined')",
        name="ck_md_object_bounds",
    ),
)
revision_partitions = Table(
    "market_data_revision_partitions",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("dataset_revision_id", True),
    Column("ordinal", Integer, primary_key=True),
    _id("object_id"),
    ForeignKeyConstraint(
        ["owner_user_id", "dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "object_id"],
        [archive_objects.c.owner_user_id, archive_objects.c.object_id],
    ),
    UniqueConstraint(
        "owner_user_id", "dataset_revision_id", "object_id", name="uq_md_revision_object"
    ),
)
retention_refs = Table(
    "market_data_retention_refs",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("dataset_revision_id", True),
    Column("reference_kind", String(20), primary_key=True),
    _id("reference_id", True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
    ),
)
bar_retention_refs = Table(
    "market_data_bar_retention_refs",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("bar_record_id", True),
    Column("reference_kind", String(20), primary_key=True),
    _id("reference_id", True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
)
idempotency = Table(
    "market_data_idempotency_records",
    market_data_metadata,
    _id("owner_user_id", True),
    Column("operation", String(80), primary_key=True),
    _id("idempotency_key", True),
    Column("request_sha256", CHAR(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("result", JSONB),
    Column("error", JSONB),
    Column("current_publication_id", String(36)),
    Column("parent_admitted_at", DateTime(timezone=True)),
    Column("admitted_parent_revision_id", String(36)),
    Column("attempt_generation", Integer, nullable=False),
    Column("rebase_count", Integer, nullable=False),
    Column("holder", String(36)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["owner_user_id"], [users.c.user_id]),
    CheckConstraint(
        "state IN ('started','succeeded','failed') AND attempt_generation >= 1 AND rebase_count BETWEEN 0 AND 3",
        name="ck_md_idempotency_state",
    ),
)
read_snapshots = Table(
    "market_data_read_snapshots",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("read_snapshot_id", True),
    _id("series_id"),
    Column("canonical_request_sha256", CHAR(64), nullable=False),
    Column("policy", JSONB, nullable=False),
    Column("published_base_revision_id", String(36)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("state", String(16), nullable=False),
    Column("total_rows", Integer, nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    CheckConstraint(
        "state IN ('open','expired') AND total_rows BETWEEN 0 AND 500000",
        name="ck_md_read_snapshot",
    ),
)
read_snapshot_bars = Table(
    "market_data_read_snapshot_bars",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("read_snapshot_id", True),
    Column("ordinal", Integer, primary_key=True),
    _id("series_id"),
    Column("start_at", DateTime(timezone=True), nullable=False),
    _id("bar_record_id"),
    Column("origin_kind", String(24), nullable=False),
    Column("dataset_revision_id", String(36)),
    Column("object_id", String(36)),
    Column("source_ordinal", Integer),
    Column("active_marker", Boolean, nullable=False, default=False),
    Column("availability_at", DateTime(timezone=True), nullable=False),
    Column("correction_observations", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "read_snapshot_id"],
        [read_snapshots.c.owner_user_id, read_snapshots.c.read_snapshot_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "object_id"],
        [archive_objects.c.owner_user_id, archive_objects.c.object_id],
    ),
    UniqueConstraint(
        "owner_user_id",
        "read_snapshot_id",
        "start_at",
        name="uq_md_snapshot_logical_start",
    ),
    CheckConstraint(
        "origin_kind IN ('archive_object','archive_chain','active')",
        name="ck_md_snapshot_bar_origin",
    ),
    CheckConstraint(
        "(origin_kind='active' AND active_marker AND dataset_revision_id IS NULL AND object_id IS NULL AND source_ordinal IS NULL) OR "
        "(origin_kind='archive_object' AND NOT active_marker AND dataset_revision_id IS NOT NULL AND object_id IS NOT NULL AND source_ordinal IS NOT NULL) OR "
        "(origin_kind='archive_chain' AND NOT active_marker AND dataset_revision_id IS NOT NULL AND object_id IS NULL AND source_ordinal IS NOT NULL)",
        name="ck_md_snapshot_bar_reference_shape",
    ),
)
bar_conflicts = Table(
    "market_data_bar_conflicts",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("conflict_id", True),
    _id("series_id"),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("existing_fingerprint", CHAR(64), nullable=False),
    Column("attempted_fingerprint", CHAR(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    UniqueConstraint(
        "owner_user_id",
        "series_id",
        "start_at",
        "source_revision",
        "existing_fingerprint",
        "attempted_fingerprint",
        name="uq_md_conflict_audit",
    ),
)
quality_observations = Table(
    "market_data_quality_observations",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("observation_id", True),
    _id("series_id"),
    Column("start_at", DateTime(timezone=True), nullable=False),
    Column("source_revision", Integer, nullable=False),
    Column("attempted_payload_hash", CHAR(64), nullable=False),
    Column("quality", String(16), nullable=False),
    Column("reason", String(32), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    UniqueConstraint(
        "owner_user_id",
        "series_id",
        "start_at",
        "source_revision",
        "attempted_payload_hash",
        "reason",
        name="uq_md_quality_audit",
    ),
    CheckConstraint(
        "quality='invalid' AND reason IN ('DURATION','OHLC','TICK','VOLUME','CALENDAR_ALIGNMENT','WINDOW','CONTRACT_LIFETIME')",
        name="ck_md_quality_reason",
    ),
)
correction_refs = Table(
    "market_data_correction_refs",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("dataset_revision_id", True),
    Column("ordinal", Integer, primary_key=True),
    _id("old_bar_record_id"),
    _id("new_bar_record_id"),
    Column("reason", String(40), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "old_bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "new_bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
    CheckConstraint(
        "reason IN ('SOURCE_CORRECTION','REPAIR_REPLACEMENT','DERIVED_COMPONENT_CHANGE')",
        name="ck_md_correction_reason",
    ),
)
aggregate_components = Table(
    "market_data_aggregate_components",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("derived_bar_record_id", True),
    Column("ordinal", Integer, primary_key=True),
    _id("source_bar_record_id"),
    _id("source_dataset_revision_id"),
    ForeignKeyConstraint(
        ["owner_user_id", "derived_bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "source_bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "source_dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
    ),
    UniqueConstraint(
        "owner_user_id",
        "derived_bar_record_id",
        "source_bar_record_id",
        name="uq_md_aggregate_component",
    ),
)
series_fences = Table(
    "market_data_series_fences",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("series_id", True),
    Column("fencing_token", BigInteger, nullable=False),
    _id("holder"),
    Column("lease_expires_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"],
        [series.c.owner_user_id, series.c.series_id],
        ondelete="CASCADE",
    ),
    CheckConstraint("fencing_token >= 0", name="ck_md_fence_token"),
)
prestage_writes = Table(
    "market_data_prestage_writes",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("series_id", True),
    _id("idempotency_key", True),
    _id("temp_uuid", True),
    _id("holder"),
    Column("fencing_token", BigInteger, nullable=False),
    Column("lease_expires_at", DateTime(timezone=True), nullable=False),
    Column("expected_temp_names", JSONB, nullable=False),
    Column("state", String(16), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    CheckConstraint("state IN ('writing','staged','abandoned')", name="ck_md_prestage_state"),
)
publications = Table(
    "market_data_publications",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("publication_id", True),
    _id("series_id"),
    _id("idempotency_key"),
    Column("operation", String(40), nullable=False),
    Column("quarantined_source_revision_id", String(36)),
    Column("parent_revision_id", String(36)),
    Column("final_candidate_revision_id", String(36)),
    Column("snapshot_sha256", CHAR(64), nullable=False),
    Column("fencing_token", BigInteger, nullable=False),
    Column("state", String(16), nullable=False),
    Column("safe_reason", String(80)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "series_id"], [series.c.owner_user_id, series.c.series_id]
    ),
    CheckConstraint(
        "operation IN ('publish','recover_quarantined_latest') AND state IN ('staged','published','quarantined')",
        name="ck_md_publication_state",
    ),
)
publication_revisions = Table(
    "market_data_publication_revisions",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("publication_id", True),
    Column("ordinal", Integer, primary_key=True),
    _id("dataset_revision_id"),
    ForeignKeyConstraint(
        ["owner_user_id", "publication_id"],
        [publications.c.owner_user_id, publications.c.publication_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
    ),
)
publication_retention_conversions = Table(
    "market_data_publication_retention_conversions",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("publication_id", True),
    _id("bar_record_id", True),
    Column("reference_kind", String(20), primary_key=True),
    _id("reference_id", True),
    _id("dataset_revision_id"),
    Column("state", String(16), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "publication_id"],
        [publications.c.owner_user_id, publications.c.publication_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "dataset_revision_id"],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
    ),
    CheckConstraint("state IN ('planned','converted','cancelled')", name="ck_md_conversion_state"),
)
publication_files = Table(
    "market_data_publication_files",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("publication_id", True),
    Column("ordinal", Integer, primary_key=True),
    Column("file_kind", String(16), nullable=False),
    Column("temp_name", String(100), nullable=False),
    Column("final_uri", String(100), nullable=False),
    Column("sha256", CHAR(64), nullable=False),
    Column("byte_length", BigInteger, nullable=False),
    Column("state", String(16), nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "publication_id"],
        [publications.c.owner_user_id, publications.c.publication_id],
        ondelete="CASCADE",
    ),
    CheckConstraint(
        "file_kind IN ('object','manifest') AND state IN ('temp','renamed','published','quarantined')",
        name="ck_md_publication_file_state",
    ),
)
publication_cleanup_bars = Table(
    "market_data_publication_cleanup_bars",
    market_data_metadata,
    _id("owner_user_id", True),
    _id("publication_id", True),
    _id("bar_record_id", True),
    Column("cleaned_at", DateTime(timezone=True)),
    ForeignKeyConstraint(
        ["owner_user_id", "publication_id"],
        [publications.c.owner_user_id, publications.c.publication_id],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["owner_user_id", "bar_record_id"],
        [bar_versions.c.owner_user_id, bar_versions.c.bar_record_id],
    ),
)
dataset_revisions.append_constraint(
    ForeignKeyConstraint(
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.parent_revision_id],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
        name="fk_md_revision_parent",
        deferrable=True,
        initially="DEFERRED",
    )
)
publications.append_constraint(
    ForeignKeyConstraint(
        [publications.c.owner_user_id, publications.c.final_candidate_revision_id],
        [dataset_revisions.c.owner_user_id, dataset_revisions.c.dataset_revision_id],
        name="fk_md_publication_final_candidate",
        deferrable=True,
        initially="DEFERRED",
    )
)
archive_objects.append_constraint(
    ForeignKeyConstraint(
        [archive_objects.c.owner_user_id, archive_objects.c.publication_id],
        [publications.c.owner_user_id, publications.c.publication_id],
        name="fk_md_archive_publication",
        deferrable=True,
        initially="DEFERRED",
    )
)

# Every owner-bearing market-data table independently proves that its owner exists.
# Composite ownership FKs are still required for relationship integrity, but cannot
# substitute for this tenant-root constraint: a later relationship refactor must not
# accidentally detach any table from the integrated access repository.
for _owner_table in market_data_metadata.tables.values():
    if "owner_user_id" not in _owner_table.c:
        continue
    if any(
        len(constraint.elements) == 1
        and constraint.elements[0].parent is _owner_table.c.owner_user_id
        and constraint.elements[0].target_fullname == "access_users.user_id"
        for constraint in _owner_table.foreign_key_constraints
    ):
        continue
    _owner_table.append_constraint(
        ForeignKeyConstraint(
            [_owner_table.c.owner_user_id],
            [users.c.user_id],
            name=f"fk_{_owner_table.name}_owner_user",
        )
    )


def _aligned_open_start(
    calendar: CalendarVersion, start_at: datetime, interval_seconds: int
) -> bool:
    step = timedelta(seconds=interval_seconds)
    return any(
        window.kind == "open"
        and window.start_at <= start_at
        and start_at + step <= window.end_at
        and int((start_at - window.start_at).total_seconds()) % interval_seconds == 0
        for window in calendar.windows
    )


class MarketDataCatalog:
    def __init__(
        self,
        engine: Engine,
        *,
        context_is_current: Callable[[UserContext], bool],
        clock: Callable[[], datetime],
    ) -> None:
        self.engine, self.context_is_current, self.clock = engine, context_is_current, clock

    def _context(self, context: UserContext) -> None:
        if not self.context_is_current(context):
            raise unauthenticated()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("Market-data clocks must return UTC.")
        return value.astimezone(UTC)

    @staticmethod
    def _key(value: str) -> None:
        try:
            parsed = UUID(value)
        except ValueError:
            raise MarketDataError(
                MarketDataCode.VALIDATION_ERROR, "Invalid idempotency key.", 422
            ) from None
        if str(parsed) != value or parsed.version != 7:
            raise MarketDataError(MarketDataCode.VALIDATION_ERROR, "Invalid idempotency key.", 422)

    def _idempotent(
        self, connection: Any, context: UserContext, operation: str, key: str, request: Any
    ) -> dict[str, Any] | None:
        self._key(key)
        digest = canonical_sha256(request)
        now = self._now()
        connection.execute(
            pg_insert(idempotency)
            .values(
                owner_user_id=context.user_id,
                operation=operation,
                idempotency_key=key,
                request_sha256=digest,
                state="started",
                result=None,
                error=None,
                current_publication_id=None,
                parent_admitted_at=None,
                admitted_parent_revision_id=None,
                attempt_generation=1,
                rebase_count=0,
                holder=None,
                lease_expires_at=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    idempotency.c.owner_user_id,
                    idempotency.c.operation,
                    idempotency.c.idempotency_key,
                ]
            )
        )
        row = (
            connection.execute(
                select(idempotency)
                .where(
                    and_(
                        idempotency.c.owner_user_id == context.user_id,
                        idempotency.c.operation == operation,
                        idempotency.c.idempotency_key == key,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if row["request_sha256"] != digest:
            raise MarketDataError(
                MarketDataCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key was reused with different input.",
                409,
            )
        if row["state"] == "failed":
            error = row["error"] or {}
            if error.get("code") == ErrorCode.NOT_FOUND.value:
                raise not_found()
            raise MarketDataError(
                MarketDataCode(error.get("code", MarketDataCode.CONFLICT.value)),
                error.get("message", "The prior attempt failed."),
                int(error.get("http_status", 409)),
                retryable=bool(error.get("retryable", False)),
            )
        return cast(dict[str, Any] | None, row["result"])

    def _save_result(
        self, connection: Any, context: UserContext, operation: str, key: str, result: Any
    ) -> None:
        connection.execute(
            update(idempotency)
            .where(
                and_(
                    idempotency.c.owner_user_id == context.user_id,
                    idempotency.c.operation == operation,
                    idempotency.c.idempotency_key == key,
                )
            )
            .values(
                state="succeeded",
                result=result.model_dump(mode="json"),
                updated_at=self._now(),
            )
        )

    def create_calendar(
        self, context: UserContext, value: CalendarCreateInput, *, idempotency_key: str
    ) -> CalendarVersion:
        self._context(context)
        validate_calendar(value)
        with self.engine.begin() as c:
            replay = self._idempotent(c, context, "calendar.create", idempotency_key, value)
            if replay:
                return CalendarVersion.model_validate(replay)
            calendar_id, now = str(uuid7()), self._now()
            windows = tuple(
                CalendarWindow(ordinal=i, **w.model_dump()) for i, w in enumerate(value.windows)
            )
            result = CalendarVersion(
                calendar_id=calendar_id,
                owner_user_id=context.user_id,
                calendar_version=1,
                exchange_timezone=value.exchange_timezone,
                coverage_start=value.coverage_start,
                coverage_end=value.coverage_end,
                windows=windows,
                metadata_as_of=value.metadata_as_of,
                provenance_ref=value.provenance_ref,
                created_at=now,
                record_version=1,
            )
            c.execute(
                insert(calendar_versions).values(
                    owner_user_id=context.user_id,
                    calendar_id=calendar_id,
                    calendar_version=1,
                    schema_version="v1",
                    exchange_timezone=value.exchange_timezone,
                    coverage_start=value.coverage_start,
                    coverage_end=value.coverage_end,
                    metadata_as_of=value.metadata_as_of,
                    provenance_ref=value.provenance_ref,
                    payload_sha256=canonical_sha256(result),
                    created_at=now,
                    record_version=1,
                )
            )
            c.execute(
                insert(calendar_windows),
                [
                    dict(
                        owner_user_id=context.user_id,
                        calendar_id=calendar_id,
                        calendar_version=1,
                        **w.model_dump(),
                    )
                    for w in windows
                ],
            )
            self._save_result(c, context, "calendar.create", idempotency_key, result)
            return result

    def append_calendar_version(
        self,
        context: UserContext,
        calendar_id: str,
        value: CalendarVersionAppendInput,
        *,
        idempotency_key: str,
    ) -> CalendarVersion:
        self._context(context)
        validate_calendar(value)
        with self.engine.begin() as c:
            replay = self._idempotent(
                c,
                context,
                "calendar.append",
                idempotency_key,
                {"calendar_id": calendar_id, **value.model_dump()},
            )
            if replay:
                return CalendarVersion.model_validate(replay)
            current = c.execute(
                select(calendar_versions.c.calendar_version)
                .where(
                    and_(
                        calendar_versions.c.owner_user_id == context.user_id,
                        calendar_versions.c.calendar_id == calendar_id,
                    )
                )
                .order_by(calendar_versions.c.calendar_version.desc())
                .limit(1)
                .with_for_update()
            ).scalar()
            if current is None:
                raise not_found()
            if current != value.expected_previous_version:
                raise MarketDataError(
                    MarketDataCode.STALE_VERSION, "Calendar version is stale.", 409
                )
            version, now = current + 1, self._now()
            windows = tuple(
                CalendarWindow(ordinal=i, **w.model_dump()) for i, w in enumerate(value.windows)
            )
            result = CalendarVersion(
                calendar_id=calendar_id,
                owner_user_id=context.user_id,
                calendar_version=version,
                exchange_timezone=value.exchange_timezone,
                coverage_start=value.coverage_start,
                coverage_end=value.coverage_end,
                windows=windows,
                metadata_as_of=value.metadata_as_of,
                provenance_ref=value.provenance_ref,
                created_at=now,
                record_version=version,
            )
            c.execute(
                insert(calendar_versions).values(
                    owner_user_id=context.user_id,
                    calendar_id=calendar_id,
                    calendar_version=version,
                    schema_version="v1",
                    exchange_timezone=value.exchange_timezone,
                    coverage_start=value.coverage_start,
                    coverage_end=value.coverage_end,
                    metadata_as_of=value.metadata_as_of,
                    provenance_ref=value.provenance_ref,
                    payload_sha256=canonical_sha256(result),
                    created_at=now,
                    record_version=version,
                )
            )
            c.execute(
                insert(calendar_windows),
                [
                    dict(
                        owner_user_id=context.user_id,
                        calendar_id=calendar_id,
                        calendar_version=version,
                        **w.model_dump(),
                    )
                    for w in windows
                ],
            )
            self._save_result(c, context, "calendar.append", idempotency_key, result)
            return result

    def _calendar(self, c: Any, owner: str, calendar_id: str, version: int) -> CalendarVersion:
        row = (
            c.execute(
                select(calendar_versions).where(
                    and_(
                        calendar_versions.c.owner_user_id == owner,
                        calendar_versions.c.calendar_id == calendar_id,
                        calendar_versions.c.calendar_version == version,
                    )
                )
            )
            .mappings()
            .first()
        )
        if not row:
            raise not_found()
        ws = c.execute(
            select(calendar_windows)
            .where(
                and_(
                    calendar_windows.c.owner_user_id == owner,
                    calendar_windows.c.calendar_id == calendar_id,
                    calendar_windows.c.calendar_version == version,
                )
            )
            .order_by(calendar_windows.c.ordinal)
        ).mappings()
        return CalendarVersion(
            calendar_id=calendar_id,
            owner_user_id=owner,
            calendar_version=version,
            exchange_timezone=row["exchange_timezone"],
            coverage_start=row["coverage_start"],
            coverage_end=row["coverage_end"],
            windows=tuple(
                CalendarWindow.model_validate(
                    {name: w[name] for name in CalendarWindow.model_fields}
                )
                for w in ws
            ),
            metadata_as_of=row["metadata_as_of"],
            provenance_ref=row["provenance_ref"],
            created_at=row["created_at"],
            record_version=row["record_version"],
        )

    def register_contract(
        self, context: UserContext, value: FuturesContractInput, *, idempotency_key: str
    ) -> FuturesContract:
        self._context(context)
        with self.engine.begin() as c:
            replay = self._idempotent(c, context, "contract.register", idempotency_key, value)
            if replay:
                return FuturesContract.model_validate(replay)
            cal = self._calendar(c, context.user_id, value.calendar_id, value.calendar_version)
            if (
                value.tick_size <= 0
                or value.multiplier <= 0
                or (
                    value.first_trade_at is not None and value.first_trade_at >= value.last_trade_at
                )
                or not value.entry_cutoff_at < value.liquidation_start_at < value.last_trade_at
            ):
                raise MarketDataError(
                    MarketDataCode.VALIDATION_ERROR, "Invalid contract values.", 422
                )
            now = self._now()
            if value.contract_id is None:
                if value.expected_version is not None:
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR,
                        "Create cannot carry expected version.",
                        422,
                    )
                contract_id, version = str(uuid7()), 1
                c.execute(
                    insert(contracts).values(
                        owner_user_id=context.user_id,
                        contract_id=contract_id,
                        provider=value.provider,
                        provider_contract_id=value.provider_contract_id,
                        current_contract_version=version,
                    )
                )
            else:
                head = (
                    c.execute(
                        select(contracts)
                        .where(
                            and_(
                                contracts.c.owner_user_id == context.user_id,
                                contracts.c.contract_id == value.contract_id,
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if not head:
                    raise not_found()
                if value.expected_version != head["current_contract_version"]:
                    raise MarketDataError(
                        MarketDataCode.STALE_VERSION, "Contract version is stale.", 409
                    )
                if (
                    value.provider != head["provider"]
                    or value.provider_contract_id != head["provider_contract_id"]
                ):
                    raise MarketDataError(
                        MarketDataCode.CONFLICT,
                        "Stable provider contract identity cannot change.",
                        409,
                    )
                prior = (
                    c.execute(
                        select(contract_versions).where(
                            and_(
                                contract_versions.c.owner_user_id == context.user_id,
                                contract_versions.c.contract_id == value.contract_id,
                                contract_versions.c.contract_version
                                == head["current_contract_version"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                material = (
                    "provider",
                    "provider_contract_id",
                    "root_symbol",
                    "exchange",
                    "currency",
                    "tick_size",
                    "multiplier",
                    "expiry_label",
                    "first_trade_at",
                    "last_trade_at",
                    "calendar_id",
                    "calendar_version",
                    "entry_cutoff_at",
                    "liquidation_start_at",
                )
                if head["series_binding_version"] is not None and any(
                    prior[k] != getattr(value, k) for k in material
                ):
                    raise MarketDataError(
                        MarketDataCode.CONFLICT,
                        "Material contract fields are sealed after series use.",
                        409,
                    )
                if (
                    value.metadata_as_of <= prior["metadata_as_of"]
                    or value.provenance_ref == prior["provenance_ref"]
                ):
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR,
                        "Contract updates require later metadata and new provenance.",
                        422,
                    )
                contract_id, version = value.contract_id, head["current_contract_version"] + 1
                c.execute(
                    update(contracts)
                    .where(
                        and_(
                            contracts.c.owner_user_id == context.user_id,
                            contracts.c.contract_id == contract_id,
                        )
                    )
                    .values(current_contract_version=version)
                )
            result = FuturesContract(
                contract_id=contract_id,
                owner_user_id=context.user_id,
                provider=value.provider,
                provider_contract_id=value.provider_contract_id,
                root_symbol=value.root_symbol,
                exchange=value.exchange,
                currency=value.currency,
                tick_size=value.tick_size,
                multiplier=value.multiplier,
                expiry_label=value.expiry_label,
                first_trade_at=value.first_trade_at,
                last_trade_at=value.last_trade_at,
                exchange_timezone=cal.exchange_timezone,
                calendar_id=value.calendar_id,
                calendar_version=value.calendar_version,
                entry_cutoff_at=value.entry_cutoff_at,
                liquidation_start_at=value.liquidation_start_at,
                metadata_as_of=value.metadata_as_of,
                provenance_ref=value.provenance_ref,
                created_at=now,
                record_version=version,
            )
            vals = result.model_dump()
            vals.update(contract_version=version, projection_sha256=canonical_sha256(result))
            c.execute(insert(contract_versions).values(**vals))
            self._save_result(c, context, "contract.register", idempotency_key, result)
            return result

    def _series_for(
        self, c: Any, owner: str, key: SeriesKey, *, create: bool = False
    ) -> RowMapping:
        where = and_(
            series.c.owner_user_id == owner,
            series.c.source == key.source,
            series.c.price_basis == key.price_basis,
            series.c.contract_id == key.contract_id,
            series.c.interval_seconds == key.interval_seconds,
        )
        row = c.execute(select(series).where(where).with_for_update()).mappings().first()
        if row or not create:
            if not row:
                raise not_found()
            return cast(RowMapping, row)
        head = (
            c.execute(
                select(contracts)
                .where(
                    and_(
                        contracts.c.owner_user_id == owner,
                        contracts.c.contract_id == key.contract_id,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if not head:
            raise not_found()
        binding = head["series_binding_version"] or head["current_contract_version"]
        cv = (
            c.execute(
                select(contract_versions).where(
                    and_(
                        contract_versions.c.owner_user_id == owner,
                        contract_versions.c.contract_id == key.contract_id,
                        contract_versions.c.contract_version == binding,
                    )
                )
            )
            .mappings()
            .one()
        )
        if head["series_binding_version"] is None:
            c.execute(
                update(contracts)
                .where(
                    and_(
                        contracts.c.owner_user_id == owner,
                        contracts.c.contract_id == key.contract_id,
                    )
                )
                .values(series_binding_version=binding)
            )
        sid = str(uuid7())
        c.execute(
            insert(series).values(
                owner_user_id=owner,
                series_id=sid,
                source=key.source,
                price_basis=key.price_basis,
                contract_id=key.contract_id,
                interval_seconds=key.interval_seconds,
                contract_version=binding,
                calendar_id=cv["calendar_id"],
                calendar_version=cv["calendar_version"],
                record_version=1,
            )
        )
        c.execute(
            insert(series_fences).values(
                owner_user_id=owner,
                series_id=sid,
                fencing_token=0,
                holder=str(uuid7()),
                lease_expires_at=self._now(),
            )
        )
        return cast(
            RowMapping,
            c.execute(
                select(series).where(
                    and_(series.c.owner_user_id == owner, series.c.series_id == sid)
                )
            )
            .mappings()
            .one(),
        )

    @contextmanager
    def _logical_bar_locks(self, context: UserContext, value: RecordBatchInput) -> Iterator[None]:
        keys = tuple(
            sorted(
                {
                    "|".join(
                        (
                            context.user_id,
                            bar.source,
                            bar.price_basis,
                            bar.contract_id,
                            str(bar.interval_seconds),
                            bar.start_at.isoformat(),
                            str(bar.source_revision),
                        )
                    )
                    for bar in value.bars
                }
            )
        )
        with self.engine.connect() as lock_connection:
            for key in keys:
                lock_connection.execute(
                    text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
            lock_connection.commit()
            try:
                yield
            finally:
                for key in reversed(keys):
                    lock_connection.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                        {"key": key},
                    )
                lock_connection.commit()

    @contextmanager
    def _retention_bar_locks(
        self, owner_user_id: str, bar_record_ids: tuple[str, ...]
    ) -> Iterator[None]:
        keys = tuple(
            f"market-data-retention|{owner_user_id}|{bar_record_id}"
            for bar_record_id in sorted(set(bar_record_ids))
        )
        with self.engine.connect() as lock_connection:
            for key in keys:
                lock_connection.execute(
                    text("SELECT pg_advisory_lock(hashtextextended(:key, 0))"),
                    {"key": key},
                )
            lock_connection.commit()
            try:
                yield
            finally:
                for key in reversed(keys):
                    lock_connection.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                        {"key": key},
                    )
                lock_connection.commit()

    def record_completed_batch(
        self, context: UserContext, value: RecordBatchInput, *, idempotency_key: str
    ) -> RecordBatchResult:
        self._context(context)
        self._key(idempotency_key)
        with self._logical_bar_locks(context, value):
            return self._record_completed_batch_locked(
                context, value, idempotency_key=idempotency_key
            )

    def _record_completed_batch_locked(
        self, context: UserContext, value: RecordBatchInput, *, idempotency_key: str
    ) -> RecordBatchResult:
        self._context(context)
        preflight_error: MarketDataError | None = None
        with self.engine.begin() as audit:
            replay = self._idempotent(audit, context, "bars.record", idempotency_key, value)
            if replay:
                return RecordBatchResult.model_validate(replay)
            for bar in sorted(
                value.bars,
                key=lambda item: (
                    item.contract_id,
                    item.source,
                    item.interval_seconds,
                    item.start_at,
                    item.source_revision,
                ),
            ):
                key = SeriesKey(
                    source=bar.source,
                    price_basis=bar.price_basis,
                    contract_id=bar.contract_id,
                    interval_seconds=bar.interval_seconds,
                )
                sr = (
                    audit.execute(
                        select(series)
                        .where(
                            and_(
                                series.c.owner_user_id == context.user_id,
                                series.c.source == key.source,
                                series.c.price_basis == key.price_basis,
                                series.c.contract_id == key.contract_id,
                                series.c.interval_seconds == key.interval_seconds,
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                head = (
                    audit.execute(
                        select(contracts)
                        .where(
                            and_(
                                contracts.c.owner_user_id == context.user_id,
                                contracts.c.contract_id == bar.contract_id,
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if head is None:
                    raise not_found()
                binding_version = (
                    sr["contract_version"]
                    if sr is not None
                    else head["series_binding_version"] or head["current_contract_version"]
                )
                cv = (
                    audit.execute(
                        select(contract_versions).where(
                            and_(
                                contract_versions.c.owner_user_id == context.user_id,
                                contract_versions.c.contract_id == bar.contract_id,
                                contract_versions.c.contract_version == binding_version,
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                cal = self._calendar(
                    audit,
                    context.user_id,
                    cv["calendar_id"] if sr is None else sr["calendar_id"],
                    cv["calendar_version"] if sr is None else sr["calendar_version"],
                )
                payload = {
                    name: item
                    for name, item in bar.model_dump().items()
                    if name != "correction_reason"
                }
                payload_hash = canonical_sha256(payload)
                fingerprint = canonical_sha256(
                    {
                        "payload_hash": payload_hash,
                        "correction_reason": bar.correction_reason,
                        "aggregate_lineage_sha256": None,
                    }
                )
                same = (
                    None
                    if sr is None
                    else (
                        audit.execute(
                            select(bar_versions).where(
                                and_(
                                    bar_versions.c.owner_user_id == context.user_id,
                                    bar_versions.c.series_id == sr["series_id"],
                                    bar_versions.c.start_at == bar.start_at,
                                    bar_versions.c.source_revision == bar.source_revision,
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                )
                if same and same["version_fingerprint_sha256"] != fingerprint:
                    assert sr is not None
                    audit.execute(
                        pg_insert(bar_conflicts)
                        .values(
                            owner_user_id=context.user_id,
                            conflict_id=str(uuid7()),
                            series_id=sr["series_id"],
                            start_at=bar.start_at,
                            source_revision=bar.source_revision,
                            existing_fingerprint=same["version_fingerprint_sha256"],
                            attempted_fingerprint=fingerprint,
                            observed_at=self._now(),
                        )
                        .on_conflict_do_nothing()
                    )
                    preflight_error = MarketDataError(
                        MarketDataCode.DUPLICATE_CONFLICT, "Conflicting bar revision.", 409
                    )
                    continue
                if (
                    bar.source_revision == 1
                    and (
                        bar.supersedes_bar_record_id is not None
                        or bar.correction_reason is not None
                    )
                ) or (
                    bar.source_revision > 1
                    and (bar.supersedes_bar_record_id is None or bar.correction_reason is None)
                ):
                    if preflight_error is None:
                        preflight_error = MarketDataError(
                            MarketDataCode.VALIDATION_ERROR,
                            "Correction metadata is incomplete or unexpected.",
                            422,
                        )
                    continue
                reason: str | None = None
                if bar.end_at != bar.start_at + timedelta(seconds=bar.interval_seconds):
                    reason = "DURATION"
                elif bar.low > min(bar.open, bar.close) or max(bar.open, bar.close) > bar.high:
                    reason = "OHLC"
                elif any(
                    number % cv["tick_size"] for number in (bar.open, bar.high, bar.low, bar.close)
                ):
                    reason = "TICK"
                elif bar.volume < 0 or bar.volume != bar.volume.to_integral_value():
                    reason = "VOLUME"
                else:
                    if not _aligned_open_start(cal, bar.start_at, bar.interval_seconds):
                        reason = "CALENDAR_ALIGNMENT"
                    elif (
                        cv["first_trade_at"] is not None and bar.start_at < cv["first_trade_at"]
                    ) or bar.end_at > cv["last_trade_at"]:
                        reason = "CONTRACT_LIFETIME"
                if reason is not None:
                    if sr is not None:
                        audit.execute(
                            pg_insert(quality_observations)
                            .values(
                                owner_user_id=context.user_id,
                                observation_id=str(uuid7()),
                                series_id=sr["series_id"],
                                start_at=bar.start_at,
                                source_revision=bar.source_revision,
                                attempted_payload_hash=payload_hash,
                                quality="invalid",
                                reason=reason,
                                observed_at=self._now(),
                            )
                            .on_conflict_do_nothing()
                        )
                    if preflight_error is None:
                        preflight_error = MarketDataError(
                            MarketDataCode.VALIDATION_ERROR, "Invalid completed bar.", 422
                        )
            if preflight_error is not None:
                audit.execute(
                    update(idempotency)
                    .where(
                        and_(
                            idempotency.c.owner_user_id == context.user_id,
                            idempotency.c.operation == "bars.record",
                            idempotency.c.idempotency_key == idempotency_key,
                        )
                    )
                    .values(
                        state="failed",
                        error={
                            "code": preflight_error.code.value,
                            "message": preflight_error.message,
                            "http_status": preflight_error.http_status,
                            "retryable": preflight_error.retryable,
                        },
                        updated_at=self._now(),
                    )
                )
        if preflight_error is not None:
            raise preflight_error
        with self.engine.begin() as c:
            replay = self._idempotent(c, context, "bars.record", idempotency_key, value)
            if replay:
                return RecordBatchResult.model_validate(replay)
            inserted: list[str] = []
            replayed: list[str] = []
            for bar in sorted(
                value.bars,
                key=lambda b: (
                    b.contract_id,
                    b.source,
                    b.interval_seconds,
                    b.start_at,
                    b.source_revision,
                ),
            ):
                key = SeriesKey(
                    source=bar.source,
                    price_basis=bar.price_basis,
                    contract_id=bar.contract_id,
                    interval_seconds=bar.interval_seconds,
                )
                sr = self._series_for(c, context.user_id, key, create=True)
                cv = (
                    c.execute(
                        select(contract_versions).where(
                            and_(
                                contract_versions.c.owner_user_id == context.user_id,
                                contract_versions.c.contract_id == bar.contract_id,
                                contract_versions.c.contract_version == sr["contract_version"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                cal = self._calendar(c, context.user_id, sr["calendar_id"], sr["calendar_version"])
                payload = {k: v for k, v in bar.model_dump().items() if k != "correction_reason"}
                ph = canonical_sha256(payload)
                fingerprint = canonical_sha256(
                    {
                        "payload_hash": ph,
                        "correction_reason": bar.correction_reason,
                        "aggregate_lineage_sha256": None,
                    }
                )
                existing = (
                    c.execute(
                        select(bar_versions)
                        .where(
                            and_(
                                bar_versions.c.owner_user_id == context.user_id,
                                bar_versions.c.series_id == sr["series_id"],
                                bar_versions.c.start_at == bar.start_at,
                            )
                        )
                        .order_by(bar_versions.c.source_revision)
                    )
                    .mappings()
                    .all()
                )
                same = next(
                    (r for r in existing if r["source_revision"] == bar.source_revision), None
                )
                if same:
                    if same["version_fingerprint_sha256"] != fingerprint:
                        raise MarketDataError(
                            MarketDataCode.DUPLICATE_CONFLICT, "Conflicting bar revision.", 409
                        )
                    replayed.append(same["bar_record_id"])
                    continue
                if (
                    bar.end_at != bar.start_at + timedelta(seconds=bar.interval_seconds)
                    or bar.low > min(bar.open, bar.close)
                    or max(bar.open, bar.close) > bar.high
                    or bar.volume < 0
                    or bar.volume != bar.volume.to_integral_value()
                    or any(v % cv["tick_size"] for v in (bar.open, bar.high, bar.low, bar.close))
                ):
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR, "Invalid completed bar.", 422
                    )
                if (
                    not _aligned_open_start(cal, bar.start_at, bar.interval_seconds)
                    or (cv["first_trade_at"] and bar.start_at < cv["first_trade_at"])
                    or bar.end_at > cv["last_trade_at"]
                ):
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR, "Bar is outside contract calendar.", 422
                    )
                prior = existing[-1] if existing else None
                if (
                    prior is None
                    and (
                        bar.source_revision != 1
                        or bar.supersedes_bar_record_id is not None
                        or bar.correction_reason is not None
                    )
                ) or (
                    prior is not None
                    and (
                        bar.source_revision != prior["source_revision"] + 1
                        or bar.supersedes_bar_record_id != prior["bar_record_id"]
                        or bar.correction_reason is None
                    )
                ):
                    raise MarketDataError(
                        MarketDataCode.DUPLICATE_CONFLICT, "Invalid correction chain.", 409
                    )
                rid, now = str(uuid7()), self._now()
                c.execute(
                    insert(bar_versions).values(
                        owner_user_id=context.user_id,
                        bar_record_id=rid,
                        schema_version="v1",
                        series_id=sr["series_id"],
                        start_at=bar.start_at,
                        source_revision=bar.source_revision,
                        payload_hash=ph,
                        supersedes_bar_record_id=bar.supersedes_bar_record_id,
                        correction_reason=bar.correction_reason,
                        aggregate_lineage_sha256=None,
                        version_fingerprint_sha256=fingerprint,
                        received_at=now,
                        quality="valid",
                        created_at=now,
                        record_version=1,
                    )
                )
                completed = CompletedBar(
                    bar_record_id=rid,
                    owner_user_id=context.user_id,
                    received_at=now,
                    created_at=now,
                    payload_hash=ph,
                    record_version=1,
                    **payload,
                )
                c.execute(
                    insert(active_bars).values(
                        **completed.model_dump(),
                        series_id=sr["series_id"],
                        contract_version=sr["contract_version"],
                        tick_size=cv["tick_size"],
                    )
                )
                inserted.append(rid)
            result = RecordBatchResult(
                batch_id=str(uuid7()),
                inserted_bar_record_ids=tuple(inserted),
                replayed_bar_record_ids=tuple(replayed),
            )
            self._save_result(c, context, "bars.record", idempotency_key, result)
            return result

    def record_aggregated_batch(
        self,
        context: UserContext,
        source_dataset_revision_id: str,
        source_series_key: SeriesKey,
        target_series_key: SeriesKey,
        full_bars: tuple[AggregatedFullBar, ...],
        *,
        idempotency_key: str,
    ) -> RecordBatchResult:
        self._context(context)
        if (
            source_series_key.interval_seconds >= target_series_key.interval_seconds
            or target_series_key.interval_seconds % source_series_key.interval_seconds
        ):
            raise MarketDataError(
                MarketDataCode.VALIDATION_ERROR,
                "Aggregate source interval must strictly divide target interval.",
                422,
            )
        request_projection = {
            "source_dataset_revision_id": source_dataset_revision_id,
            "source_series_key": source_series_key,
            "target_series_key": target_series_key,
            "full_bars": full_bars,
        }
        with self.engine.begin() as c:
            replay = self._idempotent(
                c, context, "bars.record_aggregate", idempotency_key, request_projection
            )
            if replay:
                return RecordBatchResult.model_validate(replay)
            source_series = self._series_for(c, context.user_id, source_series_key)
            revision = (
                c.execute(
                    select(dataset_revisions).where(
                        and_(
                            dataset_revisions.c.owner_user_id == context.user_id,
                            dataset_revisions.c.dataset_revision_id == source_dataset_revision_id,
                            dataset_revisions.c.series_id == source_series["series_id"],
                            dataset_revisions.c.status == "published",
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not revision:
                raise not_found()
            target_series = self._series_for(c, context.user_id, target_series_key, create=True)
            target_contract = (
                c.execute(
                    select(contract_versions).where(
                        and_(
                            contract_versions.c.owner_user_id == context.user_id,
                            contract_versions.c.contract_id == target_series_key.contract_id,
                            contract_versions.c.contract_version
                            == target_series["contract_version"],
                        )
                    )
                )
                .mappings()
                .one()
            )
            inserted: list[str] = []
            replayed: list[str] = []
            for aggregate in sorted(full_bars, key=lambda item: item.start_at):
                if (
                    aggregate.target_interval_seconds != target_series_key.interval_seconds
                    or aggregate.end_at
                    != aggregate.start_at + timedelta(seconds=target_series_key.interval_seconds)
                    or not aggregate.source_bar_record_ids
                    or len(set(aggregate.source_bar_record_ids))
                    != len(aggregate.source_bar_record_ids)
                ):
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR, "Invalid aggregate bar.", 422
                    )
                component_rows = (
                    c.execute(
                        select(
                            revision_bars.c.ordinal,
                            revision_bars.c.bar_record_id,
                            bar_versions.c.source_revision,
                            bar_versions.c.start_at,
                        )
                        .join(
                            bar_versions,
                            and_(
                                bar_versions.c.owner_user_id == revision_bars.c.owner_user_id,
                                bar_versions.c.bar_record_id == revision_bars.c.bar_record_id,
                            ),
                        )
                        .where(
                            and_(
                                revision_bars.c.owner_user_id == context.user_id,
                                revision_bars.c.dataset_revision_id == source_dataset_revision_id,
                                revision_bars.c.series_id == source_series["series_id"],
                                revision_bars.c.bar_record_id.in_(aggregate.source_bar_record_ids),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                by_id = {row["bar_record_id"]: row for row in component_rows}
                if set(by_id) != set(aggregate.source_bar_record_ids):
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR,
                        "Aggregate component is not selected by the source revision.",
                        422,
                    )
                ordered_components = [
                    by_id[component_id] for component_id in aggregate.source_bar_record_ids
                ]
                expected_component_starts = [
                    aggregate.start_at
                    + timedelta(seconds=source_series_key.interval_seconds * ordinal)
                    for ordinal in range(
                        target_series_key.interval_seconds // source_series_key.interval_seconds
                    )
                ]
                if [row["start_at"] for row in ordered_components] != expected_component_starts:
                    raise MarketDataError(
                        MarketDataCode.VALIDATION_ERROR,
                        "Aggregate components do not exactly cover the target bucket.",
                        422,
                    )
                lineage = canonical_sha256(
                    {
                        "source_dataset_revision_id": source_dataset_revision_id,
                        "source_series_key": source_series_key,
                        "target_series_key": target_series_key,
                        "start_at": aggregate.start_at,
                        "end_at": aggregate.end_at,
                        "source_bar_record_ids": aggregate.source_bar_record_ids,
                    }
                )
                existing = (
                    c.execute(
                        select(bar_versions)
                        .where(
                            and_(
                                bar_versions.c.owner_user_id == context.user_id,
                                bar_versions.c.series_id == target_series["series_id"],
                                bar_versions.c.start_at == aggregate.start_at,
                            )
                        )
                        .order_by(bar_versions.c.source_revision)
                    )
                    .mappings()
                    .all()
                )
                prior = existing[-1] if existing else None
                same_lineage = prior is not None and prior["aggregate_lineage_sha256"] == lineage
                if prior is None:
                    source_revision = 1
                    supersedes = None
                    reason = None
                elif same_lineage:
                    source_revision = prior["source_revision"]
                    supersedes = prior["supersedes_bar_record_id"]
                    reason = prior["correction_reason"]
                else:
                    source_revision = prior["source_revision"] + 1
                    supersedes = prior["bar_record_id"]
                    reason = "DERIVED_COMPONENT_CHANGE"
                now = self._now()
                payload = {
                    "source": target_series_key.source,
                    "price_basis": target_series_key.price_basis,
                    "contract_id": target_series_key.contract_id,
                    "interval_seconds": target_series_key.interval_seconds,
                    "start_at": aggregate.start_at,
                    "end_at": aggregate.end_at,
                    "open": aggregate.open,
                    "high": aggregate.high,
                    "low": aggregate.low,
                    "close": aggregate.close,
                    "volume": aggregate.volume,
                    "source_revision": source_revision,
                    "completed_at": revision["published_at"],
                    "quality": "valid",
                    "supersedes_bar_record_id": supersedes,
                }
                payload_hash = canonical_sha256(payload)
                fingerprint = canonical_sha256(
                    {
                        "payload_hash": payload_hash,
                        "correction_reason": reason,
                        "aggregate_lineage_sha256": lineage,
                    }
                )
                if same_lineage and prior is not None:
                    if prior["version_fingerprint_sha256"] != fingerprint:
                        raise MarketDataError(
                            MarketDataCode.DUPLICATE_CONFLICT,
                            "Conflicting aggregate bar revision.",
                            409,
                        )
                    replayed.append(prior["bar_record_id"])
                    continue
                bar_record_id = str(uuid7())
                c.execute(
                    insert(bar_versions).values(
                        owner_user_id=context.user_id,
                        bar_record_id=bar_record_id,
                        schema_version="v1",
                        series_id=target_series["series_id"],
                        start_at=aggregate.start_at,
                        source_revision=source_revision,
                        payload_hash=payload_hash,
                        supersedes_bar_record_id=payload["supersedes_bar_record_id"],
                        correction_reason=reason,
                        aggregate_lineage_sha256=lineage,
                        version_fingerprint_sha256=fingerprint,
                        received_at=now,
                        quality="valid",
                        created_at=now,
                        record_version=1,
                    )
                )
                completed = CompletedBar.model_validate(
                    {
                        **payload,
                        "bar_record_id": bar_record_id,
                        "owner_user_id": context.user_id,
                        "received_at": now,
                        "created_at": now,
                        "payload_hash": payload_hash,
                        "record_version": 1,
                    }
                )
                c.execute(
                    insert(active_bars).values(
                        **completed.model_dump(),
                        series_id=target_series["series_id"],
                        contract_version=target_series["contract_version"],
                        tick_size=target_contract["tick_size"],
                    )
                )
                c.execute(
                    insert(aggregate_components),
                    [
                        {
                            "owner_user_id": context.user_id,
                            "derived_bar_record_id": bar_record_id,
                            "ordinal": ordinal,
                            "source_bar_record_id": component_id,
                            "source_dataset_revision_id": source_dataset_revision_id,
                        }
                        for ordinal, component_id in enumerate(aggregate.source_bar_record_ids)
                    ],
                )
                inserted.append(bar_record_id)
            result = RecordBatchResult(
                batch_id=str(uuid7()),
                inserted_bar_record_ids=tuple(inserted),
                replayed_bar_record_ids=tuple(replayed),
            )
            self._save_result(c, context, "bars.record_aggregate", idempotency_key, result)
            return result

    def retain_revision(
        self,
        context: UserContext,
        dataset_revision_id: str,
        reference_kind: Literal["run", "lane"],
        reference_id: str,
        *,
        idempotency_key: str,
    ) -> RetentionRef:
        self._context(context)
        return self._retain(
            context, dataset_revision_id, reference_kind, reference_id, idempotency_key
        )

    def _retain(
        self,
        context: UserContext,
        revision_id: str,
        kind: Literal["run", "lane", "legal_hold"],
        reference_id: str,
        key: str,
    ) -> RetentionRef:
        with self.engine.begin() as c:
            replay = self._idempotent(
                c,
                context,
                "revision.retain",
                key,
                {"revision": revision_id, "kind": kind, "reference": reference_id},
            )
            if replay:
                return RetentionRef.model_validate(replay)
            if not c.execute(
                select(dataset_revisions.c.dataset_revision_id).where(
                    and_(
                        dataset_revisions.c.owner_user_id == context.user_id,
                        dataset_revisions.c.dataset_revision_id == revision_id,
                    )
                )
            ).scalar():
                raise not_found()
            now = self._now()
            c.execute(
                pg_insert(retention_refs)
                .values(
                    owner_user_id=context.user_id,
                    dataset_revision_id=revision_id,
                    reference_kind=kind,
                    reference_id=reference_id,
                    created_at=now,
                )
                .on_conflict_do_nothing()
            )
            result = RetentionRef(
                owner_user_id=context.user_id,
                dataset_revision_id=revision_id,
                reference_kind=kind,
                reference_id=reference_id,
                created_at=now,
                replayed=False,
            )
            self._save_result(c, context, "revision.retain", key, result)
            return result

    def retain_causal_selection(
        self,
        context: UserContext,
        bar_record_id: str,
        reference_kind: Literal["lane"],
        reference_id: str,
        *,
        idempotency_key: str,
    ) -> BarRetentionRef:
        self._context(context)
        if reference_kind != "lane":
            raise MarketDataError(
                MarketDataCode.VALIDATION_ERROR, "Only lane causal retention is supported.", 422
            )
        with self._retention_bar_locks(context.user_id, (bar_record_id,)), self.engine.begin() as c:
            replay = self._idempotent(
                c,
                context,
                "bar.retain_causal",
                idempotency_key,
                {
                    "bar_record_id": bar_record_id,
                    "reference_kind": reference_kind,
                    "reference_id": reference_id,
                },
            )
            if replay:
                return BarRetentionRef.model_validate(replay)
            if not c.execute(
                select(bar_versions.c.bar_record_id).where(
                    and_(
                        bar_versions.c.owner_user_id == context.user_id,
                        bar_versions.c.bar_record_id == bar_record_id,
                    )
                )
            ).scalar():
                raise not_found()
            found = c.execute(
                select(revision_bars.c.dataset_revision_id)
                .join(
                    dataset_revisions,
                    and_(
                        dataset_revisions.c.owner_user_id == revision_bars.c.owner_user_id,
                        dataset_revisions.c.dataset_revision_id
                        == revision_bars.c.dataset_revision_id,
                    ),
                )
                .outerjoin(
                    publication_revisions,
                    and_(
                        publication_revisions.c.owner_user_id == revision_bars.c.owner_user_id,
                        publication_revisions.c.dataset_revision_id
                        == revision_bars.c.dataset_revision_id,
                    ),
                )
                .where(
                    and_(
                        revision_bars.c.owner_user_id == context.user_id,
                        revision_bars.c.bar_record_id == bar_record_id,
                        dataset_revisions.c.status == "published",
                    )
                )
                .order_by(
                    dataset_revisions.c.published_at,
                    publication_revisions.c.ordinal.asc().nulls_last(),
                    dataset_revisions.c.dataset_revision_id,
                )
            ).scalar()
            now = self._now()
            if found:
                c.execute(
                    pg_insert(retention_refs)
                    .values(
                        owner_user_id=context.user_id,
                        dataset_revision_id=found,
                        reference_kind="lane",
                        reference_id=reference_id,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )
                result = BarRetentionRef(
                    owner_user_id=context.user_id,
                    bar_record_id=bar_record_id,
                    reference_kind="lane",
                    reference_id=reference_id,
                    state="revision",
                    dataset_revision_id=found,
                    created_at=now,
                    replayed=False,
                )
            else:
                c.execute(
                    pg_insert(bar_retention_refs)
                    .values(
                        owner_user_id=context.user_id,
                        bar_record_id=bar_record_id,
                        reference_kind="lane",
                        reference_id=reference_id,
                        created_at=now,
                    )
                    .on_conflict_do_nothing()
                )
                result = BarRetentionRef(
                    owner_user_id=context.user_id,
                    bar_record_id=bar_record_id,
                    reference_kind="lane",
                    reference_id=reference_id,
                    state="active",
                    dataset_revision_id=None,
                    created_at=now,
                    replayed=False,
                )
            self._save_result(c, context, "bar.retain_causal", idempotency_key, result)
            return result

    def place_legal_hold(
        self, context: UserContext, dataset_revision_id: str, *, idempotency_key: str
    ) -> RetentionRef:
        self._context(context)
        if not context.is_administrator:
            raise AccessError(
                ErrorCode.INSUFFICIENT_SCOPE, "Administrator authorization is required.", 403
            )
        return self._retain(
            context, dataset_revision_id, "legal_hold", str(uuid7()), idempotency_key
        )
