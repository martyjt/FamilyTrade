"""Owner-scoped append-only FT-06 strategy definition persistence."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID, uuid7

from pydantic import ValidationError
from sqlalchemy import (
    CHAR,
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
    null,
    or_,
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
    StrategyDraftFromVersionInput,
    StrategyDraftValidateInput,
    StrategyListInput,
    StrategyListItem,
    StrategyPage,
    StrategyValidationResult,
    StrategyVersion,
    ValidationIssue,
)
from familytrade.strategies.validation import (
    _as_model_input,
    _nonfinite_issues,
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
    Column("canonical_definition_sha256", CHAR(64), nullable=False),
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
    CheckConstraint(
        "(status = 'draft' AND record_version = 1) OR (status = 'validated' AND record_version = 2)",
        name="ck_strategy_versions_status_record_version",
    ),
    CheckConstraint(
        "char_length(name) BETWEEN 1 AND 120 AND name = btrim(name)",
        name="ck_strategy_versions_name",
    ),
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
    CheckConstraint(
        "jsonb_typeof(definition) = 'object'", name="ck_strategy_versions_definition_object"
    ),
    CheckConstraint(
        "required_warmup_bars BETWEEN 0 AND 5100050", name="ck_strategy_versions_warmup"
    ),
    CheckConstraint(
        "strategy_version_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
        name="ck_strategy_versions_uuid7",
    ),
    CheckConstraint(
        "created_from_version_id IS NULL OR created_from_version_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
        name="ck_strategy_versions_source_uuid7",
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
    Column("request_sha256", CHAR(64), nullable=False),
    Column("result", JSONB),
    Column("error", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "operation IN ('strategy.create','strategy.edit','strategy.validate')",
        name="ck_strategy_idempotency_operation",
    ),
    CheckConstraint("(result IS NULL) <> (error IS NULL)", name="ck_strategy_idempotency_outcome"),
    CheckConstraint(
        "idempotency_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'",
        name="ck_strategy_idempotency_key_uuid7",
    ),
    CheckConstraint(
        "request_sha256 ~ '^[0-9a-f]{64}$'", name="ck_strategy_idempotency_request_sha256"
    ),
    CheckConstraint(
        "error IS NULL OR jsonb_typeof(error) = 'object'",
        name="ck_strategy_idempotency_error_object",
    ),
)


class StrategyRepository:
    def __init__(self, engine: Engine, *, access_repository: AccessRepository) -> None:
        self.engine, self.access_repository = engine, access_repository

    @staticmethod
    def _normalize_names(value: dict[str, object]) -> dict[str, object]:
        """Apply the public NFC name contract without touching tagged-union fields."""
        normalized = dict(value)
        if isinstance(normalized.get("name"), str):
            normalized["name"] = unicodedata.normalize("NFC", cast(str, normalized["name"]))
        definition = normalized.get("definition")
        if isinstance(definition, dict):
            definition = dict(definition)
            if isinstance(definition.get("name"), str):
                definition["name"] = unicodedata.normalize("NFC", cast(str, definition["name"]))
            normalized["definition"] = definition
        return normalized

    def _authorise(self, connection: Connection, context: UserContext, scope: str) -> None:
        if not self.access_repository.context_is_current_on_connection(connection, context):
            raise unauthenticated()
        if scope not in context.scopes:
            raise AccessError(
                ErrorCode.INSUFFICIENT_SCOPE,
                "The authenticated identity lacks the required scope.",
                403,
            )

    def _mutation_gate(
        self,
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        value: dict[str, object],
    ) -> tuple[str, str]:
        """Authorize and serialize one caller/operation/key outcome on this connection."""
        self._authorise(connection, context, "strategy:write")
        key = self._normalize_idempotency_key(idempotency_key)
        try:
            digest = hashlib.sha256(canonical_operation_request_bytes(value)).hexdigest()
        except (TypeError, ValueError) as error:
            nonfinite = _nonfinite_issues(value)
            if nonfinite:
                errors = [item.model_dump(mode="json") for item in nonfinite]
                for item in errors:
                    if item["path"].startswith("/definition/"):
                        item["path"] = item["path"][11:]
                raise self._validation(errors) from error
            code = (
                "DUPLICATE_KEY"
                if "duplicate normalized object key" in str(error)
                else "NONFINITE"
                if "non-finite" in str(error)
                else "INVALID_TYPE"
            )
            raise self._validation(
                [{"path": "/", "code": code, "message": "Invalid operation input."}]
            ) from error
        lock_key = int.from_bytes(
            hashlib.sha256(
                json.dumps(
                    [context.user_id, operation, key],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).digest()[:8],
            byteorder="big",
            signed=True,
        )
        connection.execute(select(func.pg_advisory_xact_lock(lock_key)))
        self._authorise(connection, context, "strategy:write")
        return key, digest

    @staticmethod
    def _normalize_idempotency_key(value: str) -> str:
        try:
            return str(UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise AccessError(
                ErrorCode.VALIDATION_ERROR,
                "idempotency_key must be a UUID.",
                422,
                details={"path": "/idempotency_key"},
            ) from error

    @staticmethod
    def _is_safe_operation_error(error: AccessError) -> bool:
        return error.code in {
            ErrorCode.VALIDATION_ERROR,
            ErrorCode.NOT_FOUND,
            ErrorCode.CONFLICT,
            ErrorCode.STALE_VERSION,
        }

    @staticmethod
    def _validation(errors: object = ()) -> AccessError:
        if isinstance(errors, ValidationError):
            normalized: list[dict[str, str]] = []
            for item in errors.errors(include_input=False):
                raw_loc = tuple(item["loc"])
                wrappers = {
                    "feature",
                    "constant",
                    "arithmetic",
                    "compare",
                    "temporal_compare",
                    "group",
                    "one_position_v1",
                    "confirmed_pivot_zones_v1",
                    "reversal_setup_v1",
                    "breakout_retest_v1",
                }
                loc = tuple(
                    part
                    for index, part in enumerate(raw_loc)
                    if not (
                        part in wrappers
                        and index >= 2
                        and isinstance(raw_loc[index - 1], int)
                        and raw_loc[index - 2] in {"nodes", "setup_modules"}
                    )
                )
                if loc[:1] == ("definition",):
                    loc = loc[1:]
                path = "/" + "/".join(str(part) for part in loc)
                kind = str(item["type"])
                code = (
                    "UNKNOWN_FIELD"
                    if kind == "extra_forbidden"
                    else "REQUIRED"
                    if kind == "missing"
                    else "OUT_OF_RANGE"
                    if kind in {"greater_than_equal", "less_than_equal", "string_pattern_mismatch"}
                    else "INVALID_ENUM"
                    if "literal" in kind or "enum" in kind or "tag" in kind
                    else "NONFINITE"
                    if "finite" in kind or "nan" in kind
                    else "INVALID_TYPE"
                )
                normalized.append(
                    {"path": path, "code": code, "message": "Invalid operation input."}
                )
            nonfinite_paths = {item["path"] for item in normalized if item["code"] == "NONFINITE"}
            errors = [
                item
                for item in normalized
                if item["code"] != "INVALID_TYPE" or item["path"] not in nonfinite_paths
            ]
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
        connection.execute(
            insert(strategy_versions).values(**version.model_dump(mode="json"), updated_at=now)
        )
        return version

    @staticmethod
    def _stored_version(row: Mapping[str, object]) -> StrategyVersion:
        version = StrategyVersion.model_validate(
            _as_model_input({key: value for key, value in dict(row).items() if key != "updated_at"})
        )
        if canonical_definition_sha256(version.definition) != version.canonical_definition_sha256:
            raise RuntimeError("stored strategy definition hash does not match")
        return version

    def _replay(
        self,
        connection: Connection,
        context: UserContext,
        operation: str,
        key: str,
        digest: str,
    ) -> StrategyVersion | StrategyValidationResult | AccessError | None:
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
            if "validation_result" in row["result"]:
                return StrategyValidationResult.model_validate_json(
                    json.dumps(row["result"]["validation_result"])
                )
            return StrategyVersion.model_validate_json(json.dumps(row["result"]))
        return self._error_from_record(cast(dict[str, object], row["error"]))

    @staticmethod
    def _error_from_record(record: dict[str, object]) -> AccessError:
        return AccessError(
            ErrorCode(cast(str, record["code"])),
            cast(str, record["message"]),
            cast(int, record["http_status"]),
            retryable=cast(bool, record["retryable"]),
            details=cast(dict[str, object], record["details"]),
        )

    def _save_replay(
        self,
        connection: Connection,
        context: UserContext,
        operation: str,
        key: str,
        digest: str,
        result: StrategyVersion | StrategyValidationResult,
    ) -> None:
        connection.execute(
            insert(strategy_idempotency_records).values(
                owner_user_id=context.user_id,
                operation=operation,
                idempotency_key=key,
                request_sha256=digest,
                result=(
                    result.model_dump(mode="json")
                    if isinstance(result, StrategyVersion)
                    else {"validation_result": result.model_dump(mode="json")}
                ),
                error=null(),
                created_at=func.clock_timestamp(),
            )
        )

    def _save_safe_error(
        self,
        connection: Connection,
        context: UserContext,
        operation: str,
        key: str,
        digest: str,
        error: AccessError,
    ) -> AccessError:
        record: dict[str, object] = json.loads(
            json.dumps(
                {
                    "code": error.code.value,
                    "message": error.message,
                    "http_status": error.http_status,
                    "retryable": error.retryable,
                    "details": error.details,
                }
            )
        )
        connection.execute(
            insert(strategy_idempotency_records).values(
                owner_user_id=context.user_id,
                operation=operation,
                idempotency_key=key,
                request_sha256=digest,
                result=null(),
                error=record,
                created_at=func.clock_timestamp(),
            )
        )
        return self._error_from_record(record)

    def create_draft(
        self, context: UserContext, value: dict[str, object], *, idempotency_key: str
    ) -> StrategyVersion:
        value = self._normalize_names(value)
        result: StrategyVersion | None = None
        public_error: AccessError | None = None
        with self.engine.begin() as connection:
            key, digest = self._mutation_gate(
                connection, context, "strategy.create", idempotency_key, value
            )
            replay = self._replay(connection, context, "strategy.create", key, digest)
            if isinstance(replay, AccessError):
                public_error = replay
            elif replay is not None:
                assert isinstance(replay, StrategyVersion)
                result = replay
            else:
                savepoint = connection.begin_nested()
                try:
                    result = self._create_draft(connection, context, value)
                except AccessError as error:
                    if not self._is_safe_operation_error(error):
                        raise
                    savepoint.rollback()
                    public_error = self._save_safe_error(
                        connection, context, "strategy.create", key, digest, error
                    )
                else:
                    savepoint.commit()
                    self._save_replay(connection, context, "strategy.create", key, digest, result)
        if public_error is not None:
            raise public_error
        assert result is not None
        return result

    def _create_draft(
        self, connection: Connection, context: UserContext, value: dict[str, object]
    ) -> StrategyVersion:
        value = self._normalize_names(value)
        create_kind = value.get("kind")
        if "kind" not in value:
            raise self._validation(
                [{"path": "/kind", "code": "REQUIRED", "message": "Create kind is required."}]
            )
        if create_kind not in {"definition", "source_version"}:
            raise self._validation(
                [{"path": "/kind", "code": "INVALID_ENUM", "message": "Create kind is invalid."}]
            )
        if create_kind == "definition":
            try:
                parsed = StrategyDraftFromDefinitionInput.model_validate(_as_model_input(value))
            except ValidationError as error:
                raise self._validation(error) from error
            return self._insert(connection, context, parsed, None)
        try:
            source_input = StrategyDraftFromVersionInput.model_validate(value)
        except ValidationError as error:
            raise self._validation(error) from error
        source = (
            connection.execute(
                select(strategy_versions).where(
                    and_(
                        strategy_versions.c.owner_user_id == context.user_id,
                        strategy_versions.c.strategy_version_id == source_input.source_version_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            raise not_found()
        if source["status"] != "validated":
            raise AccessError(ErrorCode.CONFLICT, "Source strategy version must be validated.", 409)
        stored = self._stored_version(cast(Mapping[str, object], source))
        parsed = StrategyDraftFromDefinitionInput.model_validate(
            _as_model_input(
                {
                    "kind": "definition",
                    "schema_version": "v1",
                    "name": source_input.name,
                    "definition_schema_version": stored.definition_schema_version,
                    "definition": {
                        **stored.definition.model_dump(mode="json"),
                        "name": source_input.name,
                    },
                    "catalogue_version": stored.catalogue_version,
                    "execution_interval_seconds": stored.execution_interval_seconds,
                    "fill_interval_seconds": stored.fill_interval_seconds,
                }
            )
        )
        return self._insert(connection, context, parsed, source_input.source_version_id)
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
        return self._insert(connection, context, parsed, None)

    def edit_draft(
        self, context: UserContext, value: dict[str, object], *, idempotency_key: str
    ) -> StrategyVersion:
        value = self._normalize_names(value)
        result: StrategyVersion | None = None
        public_error: AccessError | None = None
        with self.engine.begin() as connection:
            key, digest = self._mutation_gate(
                connection, context, "strategy.edit", idempotency_key, value
            )
            replay = self._replay(connection, context, "strategy.edit", key, digest)
            if isinstance(replay, AccessError):
                public_error = replay
            elif replay is not None:
                assert isinstance(replay, StrategyVersion)
                result = replay
            else:
                savepoint = connection.begin_nested()
                try:
                    result = self._edit_draft(connection, context, value)
                except AccessError as error:
                    if not self._is_safe_operation_error(error):
                        raise
                    savepoint.rollback()
                    public_error = self._save_safe_error(
                        connection, context, "strategy.edit", key, digest, error
                    )
                else:
                    savepoint.commit()
                    self._save_replay(connection, context, "strategy.edit", key, digest, result)
        if public_error is not None:
            raise public_error
        assert result is not None
        return result

    def _edit_draft(
        self, connection: Connection, context: UserContext, value: dict[str, object]
    ) -> StrategyVersion:
        value = self._normalize_names(value)
        try:
            parsed = StrategyDraftEditInput.model_validate(_as_model_input(value))
        except ValidationError as error:
            raise self._validation(error) from error
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
        self._authorise(connection, context, "strategy:write")
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
        return self._insert(connection, context, parsed, parsed.draft_id)

    def validate_draft(
        self, context: UserContext, value: dict[str, object], *, idempotency_key: str
    ) -> StrategyValidationResult:
        result: StrategyValidationResult | None = None
        public_error: AccessError | None = None
        with self.engine.begin() as connection:
            key, digest = self._mutation_gate(
                connection, context, "strategy.validate", idempotency_key, value
            )
            replay = self._replay(connection, context, "strategy.validate", key, digest)
            if isinstance(replay, AccessError):
                public_error = replay
            elif isinstance(replay, StrategyValidationResult):
                result = replay
            elif replay is not None:
                assert isinstance(replay, StrategyVersion)
                result = StrategyValidationResult(valid=True, strategy_version=replay, errors=())
            else:
                savepoint = connection.begin_nested()
                try:
                    result = self._validate_draft(connection, context, value)
                except AccessError as error:
                    if not self._is_safe_operation_error(error):
                        raise
                    savepoint.rollback()
                    public_error = self._save_safe_error(
                        connection, context, "strategy.validate", key, digest, error
                    )
                else:
                    savepoint.commit()
                    self._save_replay(
                        connection,
                        context,
                        "strategy.validate",
                        key,
                        digest,
                        result,
                    )
        if public_error is not None:
            raise public_error
        assert result is not None
        return result

    def _validate_draft(
        self, connection: Connection, context: UserContext, value: dict[str, object]
    ) -> StrategyValidationResult:
        try:
            parsed = StrategyDraftValidateInput.model_validate(value)
        except ValidationError as error:
            raise self._validation(error) from error
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
        self._authorise(connection, context, "strategy:write")
        if row is None:
            raise not_found()
        if row["record_version"] != parsed.expected_version:
            raise stale_version(parsed.expected_version, cast(int, row["record_version"]))
        if row["status"] != "draft":
            raise AccessError(ErrorCode.CONFLICT, "Draft is already validated.", 409)
        stored = self._stored_version(cast(Mapping[str, object], row))
        try:
            checked = self._check(
                connection,
                context,
                StrategyDraftEditInput.model_validate(
                    _as_model_input(
                        {
                            "schema_version": "v1",
                            "draft_id": stored.strategy_version_id,
                            "expected_version": 1,
                            "name": stored.name,
                            "definition_schema_version": stored.definition_schema_version,
                            "definition": stored.definition.model_dump(mode="json"),
                            "catalogue_version": stored.catalogue_version,
                            "execution_interval_seconds": stored.execution_interval_seconds,
                            "fill_interval_seconds": stored.fill_interval_seconds,
                        }
                    )
                ),
            )
        except AccessError as error:
            if error.code is not ErrorCode.VALIDATION_ERROR:
                raise
            raw_errors = cast(list[dict[str, object]], error.details.get("errors", []))
            return StrategyValidationResult(
                valid=False,
                strategy_version=None,
                errors=tuple(ValidationIssue.model_validate(item) for item in raw_errors),
            )
        assert checked.valid
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
            strategy_version=self._stored_version(cast(Mapping[str, object], updated)),
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
            return self._stored_version(cast(Mapping[str, object], row))

    @staticmethod
    def _cursor(context: UserContext, status: str | None, row: Mapping[str, object]) -> str:
        """Bind a keyset position to its owner and immutable list shape."""
        created_at = row["created_at"]
        assert isinstance(created_at, datetime)
        payload = {
            "v": 1,
            "owner_sha256": hashlib.sha256(context.user_id.encode("utf-8")).hexdigest(),
            "operation": "strategy.list",
            "status": status,
            "order": "created_at_desc_strategy_version_id_desc",
            "after_created_at": created_at.isoformat(),
            "after_strategy_version_id": row["strategy_version_id"],
        }
        return (
            base64.urlsafe_b64encode(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )
            .decode("ascii")
            .rstrip("=")
        )

    @staticmethod
    def _parse_cursor(
        context: UserContext, status: str | None, cursor: str
    ) -> tuple[datetime, str]:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {
                "v",
                "owner_sha256",
                "operation",
                "status",
                "order",
                "after_created_at",
                "after_strategy_version_id",
            }:
                raise ValueError
            if (
                payload["v"] != 1
                or payload["owner_sha256"]
                != hashlib.sha256(context.user_id.encode("utf-8")).hexdigest()
                or payload["operation"] != "strategy.list"
                or payload["status"] != status
                or payload["order"] != "created_at_desc_strategy_version_id_desc"
                or not isinstance(payload["after_strategy_version_id"], str)
            ):
                raise ValueError
            created_at = datetime.fromisoformat(payload["after_created_at"])
            if created_at.tzinfo is None:
                raise ValueError
            return created_at, payload["after_strategy_version_id"]
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StrategyRepository._validation() from error

    def list_versions(self, context: UserContext, value: dict[str, object]) -> StrategyPage:
        try:
            parsed = StrategyListInput.model_validate(value)
        except ValidationError as error:
            raise self._validation(error) from error
        with self.engine.connect() as connection:
            self._authorise(connection, context, "strategy:read")
            query = select(strategy_versions).where(
                strategy_versions.c.owner_user_id == context.user_id
            )
            if parsed.status is not None:
                query = query.where(strategy_versions.c.status == parsed.status)
            if parsed.cursor is not None:
                created_at, strategy_version_id = self._parse_cursor(
                    context, parsed.status, parsed.cursor
                )
                query = query.where(
                    or_(
                        strategy_versions.c.created_at < created_at,
                        and_(
                            strategy_versions.c.created_at == created_at,
                            strategy_versions.c.strategy_version_id < strategy_version_id,
                        ),
                    )
                )
            rows = (
                connection.execute(
                    query.order_by(
                        strategy_versions.c.created_at.desc(),
                        strategy_versions.c.strategy_version_id.desc(),
                    ).limit(parsed.limit + 1)
                )
                .mappings()
                .all()
            )
            page_rows = rows[: parsed.limit]
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
                    for row in page_rows
                ),
                next_cursor=(
                    self._cursor(context, parsed.status, dict(page_rows[-1]))
                    if len(rows) > parsed.limit
                    else None
                ),
            )
