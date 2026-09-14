"""Owner-scoped append-only FT-06 strategy definition persistence."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import cast
from uuid import uuid7

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine

from familytrade.access.models import (
    AccessError,
    ErrorCode,
    UserContext,
    not_found,
    stale_version,
    unauthenticated,
)
from familytrade.access.repository import AccessRepository, access_metadata, users
from familytrade.market_data.catalog import load_owned_calendar_version
from familytrade.market_data.models import CalendarVersion
from familytrade.strategies.definitions import (
    DefinitionValidationResult,
    StrategyDraftEditInput,
    StrategyDraftFromDefinitionInput,
    StrategyDraftValidateInput,
    StrategyListInput,
    StrategyListItem,
    StrategyPage,
    StrategyValidationResult,
    StrategyVersion,
)
from familytrade.strategies.validation import (
    canonical_definition_sha256,
    canonical_operation_request_bytes,
    validate_rule_definition,
)

strategy_metadata = MetaData()
if users.metadata is not access_metadata:  # pragma: no cover
    raise RuntimeError("strategy metadata must bind access users")

strategy_versions = Table(
    "strategy_versions",
    strategy_metadata,
    Column("owner_user_id", String(36), ForeignKey(users.c.user_id), primary_key=True),
    Column("strategy_version_id", String(36), primary_key=True),
    Column("schema_version", String(8), nullable=False),
    Column("name", String(120), nullable=False),
    Column("status", String(16), nullable=False),
    Column("definition_schema_version", String(64), nullable=False),
    Column("definition", JSONB, nullable=False),
    Column("canonical_definition_sha256", String(64), nullable=False),
    Column("catalogue_version", String(64), nullable=False),
    Column("execution_interval_seconds", Integer, nullable=False),
    Column("fill_interval_seconds", Integer, nullable=False),
    Column("required_warmup_bars", Integer, nullable=False),
    Column("created_from_version_id", String(36)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("record_version", Integer, nullable=False),
    ForeignKeyConstraint(
        ["owner_user_id", "created_from_version_id"],
        ["strategy_versions.owner_user_id", "strategy_versions.strategy_version_id"],
        name="fk_strategy_versions_owner_source",
    ),
    CheckConstraint("schema_version = 'v1'", name="ck_strategy_versions_v1"),
    CheckConstraint("status IN ('draft','validated')", name="ck_strategy_versions_status"),
    CheckConstraint("record_version IN (1,2)", name="ck_strategy_versions_record_version"),
    CheckConstraint(
        "execution_interval_seconds IN (300,900,1800,3600)",
        name="ck_strategy_versions_execution_interval",
    ),
    CheckConstraint(
        "fill_interval_seconds > 0 AND execution_interval_seconds % fill_interval_seconds = 0",
        name="ck_strategy_versions_fill_interval",
    ),
    CheckConstraint(
        "canonical_definition_sha256 ~ '^[0-9a-f]{64}$'", name="ck_strategy_versions_sha256"
    ),
)
Index(
    "ix_strategy_versions_owner_status_created_id",
    strategy_versions.c.owner_user_id,
    strategy_versions.c.status,
    strategy_versions.c.created_at.desc(),
    strategy_versions.c.strategy_version_id.desc(),
)
strategy_idempotency_records = Table(
    "strategy_idempotency_records",
    strategy_metadata,
    Column("owner_user_id", String(36), ForeignKey(users.c.user_id), primary_key=True),
    Column("operation", String(40), primary_key=True),
    Column("idempotency_key", String(36), primary_key=True),
    Column("request_sha256", String(64), nullable=False),
    Column("result", JSONB),
    Column("error", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "operation IN ('strategy.create','strategy.edit','strategy.validate')",
        name="ck_strategy_idempotency_operation",
    ),
    CheckConstraint("(result IS NULL) <> (error IS NULL)", name="ck_strategy_idempotency_outcome"),
)


class StrategyRepository:
    def __init__(self, engine: Engine, *, access_repository: AccessRepository) -> None:
        self.engine, self.access_repository = engine, access_repository

    def _authorise(self, connection: Connection, context: UserContext, scope: str) -> None:
        if not self.access_repository.context_is_current_on_connection(connection, context):
            raise unauthenticated()
        if scope not in context.scopes:
            raise AccessError(
                ErrorCode.INSUFFICIENT_SCOPE,
                "The authenticated identity lacks the required scope.",
                403,
            )

    @staticmethod
    def _validation(errors: object = ()) -> AccessError:
        return AccessError(
            ErrorCode.VALIDATION_ERROR,
            "Invalid strategy definition.",
            422,
            details={"errors": errors},
        )

    def _check(
        self,
        connection: Connection,
        context: UserContext,
        value: StrategyDraftFromDefinitionInput | StrategyDraftEditInput,
    ) -> DefinitionValidationResult:
        calendars: dict[tuple[str, int], CalendarVersion] = {}
        for window in value.definition.constraints.entry_windows:
            key = (window.calendar_id, window.calendar_version)
            if key not in calendars:
                calendars[key] = load_owned_calendar_version(connection, context.user_id, *key)
        result = validate_rule_definition(
            value.definition.model_dump(mode="json"),
            owner_user_id=context.user_id,
            execution_interval_seconds=value.execution_interval_seconds,
            fill_interval_seconds=value.fill_interval_seconds,
            calendar_versions=calendars,
        )
        if not result.valid or result.definition is None:
            raise self._validation([item.model_dump(mode="json") for item in result.errors])
        return result

    def _insert(
        self,
        connection: Connection,
        context: UserContext,
        value: StrategyDraftFromDefinitionInput | StrategyDraftEditInput,
        source: str | None,
    ) -> StrategyVersion:
        check = self._check(connection, context, value)
        definition = check.definition
        assert definition is not None
        now = connection.scalar(select(func.clock_timestamp()))
        assert isinstance(now, datetime)
        version = StrategyVersion(
            schema_version="v1",
            strategy_version_id=str(uuid7()),
            owner_user_id=context.user_id,
            name=value.name,
            status="draft",
            definition_schema_version=value.definition_schema_version,
            definition=definition,
            canonical_definition_sha256=canonical_definition_sha256(definition),
            catalogue_version=value.catalogue_version,
            execution_interval_seconds=value.execution_interval_seconds,
            fill_interval_seconds=value.fill_interval_seconds,
            required_warmup_bars=check.required_warmup_bars or 0,
            created_from_version_id=source,
            created_at=now,
            record_version=1,
        )
        connection.execute(insert(strategy_versions).values(**version.model_dump(mode="json")))
        return version

    def _replay(
        self,
        connection: Connection,
        context: UserContext,
        operation: str,
        key: str,
        value: dict[str, object],
    ) -> StrategyVersion | None:
        digest = hashlib.sha256(canonical_operation_request_bytes(value)).hexdigest()
        row = (
            connection.execute(
                select(strategy_idempotency_records)
                .where(
                    and_(
                        strategy_idempotency_records.c.owner_user_id == context.user_id,
                        strategy_idempotency_records.c.operation == operation,
                        strategy_idempotency_records.c.idempotency_key == key,
                    )
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise AccessError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency key was already used for a different request.",
                409,
            )
        if row["result"] is not None:
            return StrategyVersion.model_validate(row["result"])
        raise AccessError(
            ErrorCode(row["error"]["code"]), row["error"]["message"], row["error"]["http_status"]
        )

    def _save_replay(
        self,
        connection: Connection,
        context: UserContext,
        operation: str,
        key: str,
        value: dict[str, object],
        result: StrategyVersion,
    ) -> None:
        connection.execute(
            insert(strategy_idempotency_records).values(
                owner_user_id=context.user_id,
                operation=operation,
                idempotency_key=key,
                request_sha256=hashlib.sha256(canonical_operation_request_bytes(value)).hexdigest(),
                result=result.model_dump(mode="json"),
                error=None,
                created_at=func.clock_timestamp(),
            )
        )

    def create_draft(
        self, context: UserContext, value: dict[str, object], *, idempotency_key: str
    ) -> StrategyVersion:
        with self.engine.begin() as connection:
            self._authorise(connection, context, "strategy:write")
            replay = self._replay(connection, context, "strategy.create", idempotency_key, value)
            if replay is not None:
                return replay
            try:
                parsed = StrategyDraftFromDefinitionInput.model_validate(value)
            except Exception as error:
                raise self._validation() from error
            if parsed.name != parsed.definition.name:
                raise self._validation(
                    [
                        {
                            "path": "/definition/name",
                            "code": "NAME_MISMATCH",
                            "message": "Name must match definition.name.",
                        }
                    ]
                )
            result = self._insert(connection, context, parsed, None)
            self._save_replay(
                connection, context, "strategy.create", idempotency_key, value, result
            )
            return result

    def edit_draft(
        self, context: UserContext, value: dict[str, object], *, idempotency_key: str
    ) -> StrategyVersion:
        with self.engine.begin() as connection:
            self._authorise(connection, context, "strategy:write")
            replay = self._replay(connection, context, "strategy.edit", idempotency_key, value)
            if replay is not None:
                return replay
            try:
                parsed = StrategyDraftEditInput.model_validate(value)
            except Exception as error:
                raise self._validation() from error
            source = (
                connection.execute(
                    select(strategy_versions)
                    .where(
                        and_(
                            strategy_versions.c.owner_user_id == context.user_id,
                            strategy_versions.c.strategy_version_id == parsed.draft_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if source is None:
                raise not_found()
            if source["record_version"] != parsed.expected_version:
                raise stale_version(parsed.expected_version, cast(int, source["record_version"]))
            if source["status"] != "draft":
                raise AccessError(ErrorCode.CONFLICT, "Only draft versions may be edited.", 409)
            if parsed.name != parsed.definition.name:
                raise self._validation(
                    [
                        {
                            "path": "/definition/name",
                            "code": "NAME_MISMATCH",
                            "message": "Name must match definition.name.",
                        }
                    ]
                )
            result = self._insert(connection, context, parsed, parsed.draft_id)
            self._save_replay(connection, context, "strategy.edit", idempotency_key, value, result)
            return result

    def validate_draft(
        self, context: UserContext, value: dict[str, object], *, idempotency_key: str
    ) -> StrategyValidationResult:
        with self.engine.begin() as connection:
            self._authorise(connection, context, "strategy:write")
            try:
                parsed = StrategyDraftValidateInput.model_validate(value)
            except Exception as error:
                raise self._validation() from error
            row = (
                connection.execute(
                    select(strategy_versions)
                    .where(
                        and_(
                            strategy_versions.c.owner_user_id == context.user_id,
                            strategy_versions.c.strategy_version_id == parsed.draft_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise not_found()
            if row["record_version"] != parsed.expected_version:
                raise stale_version(parsed.expected_version, cast(int, row["record_version"]))
            if row["status"] != "draft":
                raise AccessError(ErrorCode.CONFLICT, "Draft is already validated.", 409)
            connection.execute(
                update(strategy_versions)
                .where(
                    and_(
                        strategy_versions.c.owner_user_id == context.user_id,
                        strategy_versions.c.strategy_version_id == parsed.draft_id,
                    )
                )
                .values(status="validated", record_version=2, updated_at=func.clock_timestamp())
            )
            updated = (
                connection.execute(
                    select(strategy_versions).where(
                        and_(
                            strategy_versions.c.owner_user_id == context.user_id,
                            strategy_versions.c.strategy_version_id == parsed.draft_id,
                        )
                    )
                )
                .mappings()
                .one()
            )
            return StrategyValidationResult(
                valid=True,
                strategy_version=StrategyVersion.model_validate(
                    {key: value for key, value in dict(updated).items() if key != "updated_at"}
                ),
                errors=(),
            )

    def get_version(self, context: UserContext, strategy_version_id: str) -> StrategyVersion:
        with self.engine.connect() as connection:
            self._authorise(connection, context, "strategy:read")
            row = (
                connection.execute(
                    select(strategy_versions).where(
                        and_(
                            strategy_versions.c.owner_user_id == context.user_id,
                            strategy_versions.c.strategy_version_id == strategy_version_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise not_found()
            return StrategyVersion.model_validate(
                {key: value for key, value in dict(row).items() if key != "updated_at"}
            )

    def list_versions(self, context: UserContext, value: dict[str, object]) -> StrategyPage:
        try:
            parsed = StrategyListInput.model_validate(value)
        except Exception as error:
            raise self._validation() from error
        with self.engine.connect() as connection:
            self._authorise(connection, context, "strategy:read")
            query = select(strategy_versions).where(
                strategy_versions.c.owner_user_id == context.user_id
            )
            if parsed.status is not None:
                query = query.where(strategy_versions.c.status == parsed.status)
            rows = (
                connection.execute(
                    query.order_by(
                        strategy_versions.c.created_at.desc(),
                        strategy_versions.c.strategy_version_id.desc(),
                    ).limit(parsed.limit)
                )
                .mappings()
                .all()
            )
            return StrategyPage(
                schema_version="v1",
                items=tuple(
                    StrategyListItem(
                        **{
                            key: value
                            for key, value in dict(row).items()
                            if key not in {"definition", "updated_at"}
                        }
                    )
                    for row in rows
                ),
                next_cursor=None,
            )
