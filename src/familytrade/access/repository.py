"""PostgreSQL persistence with owner predicates at every resource boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from familytrade.access.credentials import CredentialEnvelope, EnvelopeCipher, mask_reference
from familytrade.access.models import (
    AccessError,
    BrokerAccountStatus,
    BrokerAccountView,
    BrokerEnvironment,
    BrowserWriteAuthorization,
    ErrorCode,
    PasswordChangeResult,
    SessionLogoutResult,
    UserContext,
    UserDisableResult,
    not_found,
    parse_broker_account_create,
    stale_version,
    unauthenticated,
    validate_secret_bytes,
)

access_metadata = MetaData()


def StringColumn(name: str, *args: Any, **kwargs: Any) -> Column[str]:
    """Keep string identifiers portable while migrations use PostgreSQL."""
    return Column(name, String(100), *args, **kwargs)


users = Table(
    "access_users",
    access_metadata,
    StringColumn("user_id", primary_key=True),
    Column("username_normalized", String(254), unique=True, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("scopes", ARRAY(Text), nullable=False),
    Column("is_administrator", Boolean, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("credential_version", Integer, nullable=False),
    Column("record_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("credential_version >= 1", name="ck_access_users_credential_version"),
    CheckConstraint("record_version >= 1", name="ck_access_users_record_version"),
)

sessions = Table(
    "access_sessions",
    access_metadata,
    StringColumn("auth_session_id", primary_key=True),
    StringColumn("user_id", ForeignKey("access_users.user_id"), nullable=False, index=True),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("csrf_hash", LargeBinary, nullable=False),
    Column("scopes", ARRAY(Text), nullable=False),
    Column("is_administrator", Boolean, nullable=False),
    Column("credential_version", Integer, nullable=False),
    Column("authenticated_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("idle_expires_at", DateTime(timezone=True), nullable=False),
    Column("absolute_expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    CheckConstraint(
        "idle_expires_at > authenticated_at AND absolute_expires_at > authenticated_at",
        name="ck_access_sessions_expiry",
    ),
)

credential_envelopes = Table(
    "access_credential_envelopes",
    access_metadata,
    StringColumn("credential_envelope_id", primary_key=True),
    StringColumn("owner_user_id", ForeignKey("access_users.user_id"), nullable=False, index=True),
    StringColumn("broker_account_id", nullable=False, index=True),
    StringColumn("provider", nullable=False),
    StringColumn("purpose", nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("wrapped_dek", LargeBinary, nullable=False),
    Column("wrap_nonce", LargeBinary, nullable=False),
    StringColumn("key_version", nullable=False),
    StringColumn("status", nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    CheckConstraint(
        "purpose IN ('provider_account_ref', 'provider_credential')",
        name="ck_access_credential_purpose",
    ),
    CheckConstraint("status IN ('active', 'revoked')", name="ck_access_credential_status"),
    UniqueConstraint(
        "credential_envelope_id",
        "owner_user_id",
        "broker_account_id",
        "provider",
        "purpose",
        name="uq_access_envelope_identity_binding",
    ),
    ForeignKeyConstraint(
        ["broker_account_id", "owner_user_id", "provider"],
        [
            "access_broker_accounts.broker_account_id",
            "access_broker_accounts.owner_user_id",
            "access_broker_accounts.provider",
        ],
        name="fk_access_envelope_account_owner_provider",
        deferrable=True,
        initially="DEFERRED",
        use_alter=True,
    ),
)

broker_accounts = Table(
    "access_broker_accounts",
    access_metadata,
    StringColumn("broker_account_id", primary_key=True),
    StringColumn("schema_version", nullable=False),
    StringColumn("owner_user_id", ForeignKey("access_users.user_id"), nullable=False, index=True),
    StringColumn("provider", nullable=False),
    StringColumn("provider_account_ref_ciphertext_id", nullable=False),
    StringColumn("provider_account_ref_purpose", nullable=False),
    StringColumn("provider_account_ref_last_four", nullable=False),
    StringColumn("environment", nullable=False),
    StringColumn("status", nullable=False),
    Column("capabilities", ARRAY(Text), nullable=False),
    StringColumn("credential_envelope_id", nullable=False),
    StringColumn("credential_purpose", nullable=False),
    Column("verified_at", DateTime(timezone=True)),
    Column("last_reconciled_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("record_version", Integer, nullable=False),
    CheckConstraint("environment IN ('paper', 'live')", name="ck_access_broker_environment"),
    CheckConstraint(
        "status IN ('pending_verification', 'active', 'reauth_required', 'disabled')",
        name="ck_access_broker_status",
    ),
    CheckConstraint("record_version >= 1", name="ck_access_broker_record_version"),
    CheckConstraint(
        "provider_account_ref_purpose = 'provider_account_ref'",
        name="ck_access_broker_ref_purpose",
    ),
    CheckConstraint(
        "credential_purpose = 'provider_credential'",
        name="ck_access_broker_credential_purpose",
    ),
    UniqueConstraint(
        "broker_account_id",
        "owner_user_id",
        "provider",
        name="uq_access_broker_owner_provider",
    ),
    ForeignKeyConstraint(
        [
            "provider_account_ref_ciphertext_id",
            "owner_user_id",
            "broker_account_id",
            "provider",
            "provider_account_ref_purpose",
        ],
        [
            "access_credential_envelopes.credential_envelope_id",
            "access_credential_envelopes.owner_user_id",
            "access_credential_envelopes.broker_account_id",
            "access_credential_envelopes.provider",
            "access_credential_envelopes.purpose",
        ],
        name="fk_access_broker_reference_envelope",
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        [
            "credential_envelope_id",
            "owner_user_id",
            "broker_account_id",
            "provider",
            "credential_purpose",
        ],
        [
            "access_credential_envelopes.credential_envelope_id",
            "access_credential_envelopes.owner_user_id",
            "access_credential_envelopes.broker_account_id",
            "access_credential_envelopes.provider",
            "access_credential_envelopes.purpose",
        ],
        name="fk_access_broker_credential_envelope",
        deferrable=True,
        initially="DEFERRED",
    ),
)

Index(
    "uq_access_active_envelope_per_purpose",
    credential_envelopes.c.owner_user_id,
    credential_envelopes.c.broker_account_id,
    credential_envelopes.c.purpose,
    unique=True,
    postgresql_where=credential_envelopes.c.status == "active",
)

audit_events = Table(
    "access_audit_events",
    access_metadata,
    StringColumn("audit_event_id", primary_key=True),
    StringColumn("actor_user_id", ForeignKey("access_users.user_id"), nullable=False),
    StringColumn("event_type", nullable=False),
    StringColumn("broker_account_id"),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

idempotency_records = Table(
    "access_idempotency_records",
    access_metadata,
    StringColumn("owner_user_id", ForeignKey("access_users.user_id"), primary_key=True),
    StringColumn("operation", primary_key=True),
    StringColumn("idempotency_key", primary_key=True),
    Column("request_sha256", LargeBinary, nullable=False),
    Column("result", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

write_authorizations = Table(
    "access_write_authorizations",
    access_metadata,
    Column("authorization_hash", LargeBinary, primary_key=True),
    StringColumn(
        "auth_session_id", ForeignKey("access_sessions.auth_session_id"), nullable=False, index=True
    ),
    StringColumn("request_id", nullable=False),
    StringColumn("operation", nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class AccessRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_schema(self) -> None:
        access_metadata.create_all(self._engine)

    def create_invited_user(
        self,
        username_normalized: str,
        password_hash: str,
        scopes_granted: Sequence[str],
        is_administrator: bool,
        now: datetime,
    ) -> str:
        user_id = str(uuid7())
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(users).values(
                        user_id=user_id,
                        username_normalized=username_normalized,
                        password_hash=password_hash,
                        scopes=list(scopes_granted),
                        is_administrator=is_administrator,
                        enabled=True,
                        credential_version=1,
                        record_version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError as exc:
            raise AccessError(ErrorCode.CONFLICT, "Invited user already exists.", 409) from exc
        return user_id

    def get_user_for_login(self, username_normalized: str) -> RowMapping | None:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    select(users).where(users.c.username_normalized == username_normalized)
                )
                .mappings()
                .one_or_none()
            )

    def create_session(
        self,
        *,
        user_id: str,
        token_hash: bytes,
        csrf_hash: bytes,
        expected_credential_version: int,
        idle_timeout: timedelta,
        absolute_timeout: timedelta,
    ) -> tuple[str, datetime]:
        session_id = str(uuid7())
        with self._engine.begin() as connection:
            user = (
                connection.execute(
                    select(users).where(users.c.user_id == user_id).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                user is None
                or not cast(bool, user["enabled"])
                or user["credential_version"] != expected_credential_version
            ):
                raise unauthenticated()
            authenticated_at = cast(datetime, connection.scalar(select(func.clock_timestamp())))
            idle_expires_at = authenticated_at + idle_timeout
            absolute_expires_at = authenticated_at + absolute_timeout
            connection.execute(
                insert(sessions).values(
                    auth_session_id=session_id,
                    user_id=user_id,
                    token_hash=token_hash,
                    csrf_hash=csrf_hash,
                    scopes=list(cast(list[str], user["scopes"])),
                    is_administrator=cast(bool, user["is_administrator"]),
                    credential_version=cast(int, user["credential_version"]),
                    authenticated_at=authenticated_at,
                    last_seen_at=authenticated_at,
                    idle_expires_at=idle_expires_at,
                    absolute_expires_at=absolute_expires_at,
                    revoked_at=None,
                )
            )
        return session_id, absolute_expires_at

    def authenticate_session(
        self,
        token_hash: bytes,
        *,
        request_id: str,
        required_scope: str | None,
        touch: bool,
        idle_timeout: timedelta,
    ) -> tuple[UserContext, bytes]:
        with self._engine.begin() as connection:
            session = (
                connection.execute(
                    select(
                        sessions,
                        users.c.enabled,
                        users.c.credential_version.label("current_credential_version"),
                        users.c.is_administrator.label("current_is_administrator"),
                    )
                    .join(users, sessions.c.user_id == users.c.user_id)
                    .where(sessions.c.token_hash == token_hash)
                    .with_for_update(of=[sessions, users])
                )
                .mappings()
                .one_or_none()
            )
            now = cast(datetime, connection.scalar(select(func.clock_timestamp())))
            if (
                session is None
                or session["revoked_at"] is not None
                or not cast(bool, session["enabled"])
                or now >= cast(datetime, session["idle_expires_at"])
                or now >= cast(datetime, session["absolute_expires_at"])
                or session["credential_version"] != session["current_credential_version"]
                or session["is_administrator"] != session["current_is_administrator"]
            ):
                raise unauthenticated()
            scopes = tuple(sorted(set(cast(list[str], session["scopes"]))))
            if required_scope is not None and required_scope not in scopes:
                raise AccessError(
                    ErrorCode.INSUFFICIENT_SCOPE,
                    "The authenticated identity lacks the required scope.",
                    403,
                )
            next_idle_expiry = min(
                now + idle_timeout, cast(datetime, session["absolute_expires_at"])
            )
            if touch:
                connection.execute(
                    update(sessions)
                    .where(sessions.c.auth_session_id == session["auth_session_id"])
                    .values(last_seen_at=now, idle_expires_at=next_idle_expiry)
                )
            return (
                UserContext(
                    schema_version="v1",
                    user_id=cast(str, session["user_id"]),
                    auth_session_id=cast(str, session["auth_session_id"]),
                    auth_method="browser_session",
                    scopes=scopes,
                    authenticated_at=cast(datetime, session["authenticated_at"]),
                    expires_at=(
                        next_idle_expiry
                        if touch
                        else min(
                            cast(datetime, session["idle_expires_at"]),
                            cast(datetime, session["absolute_expires_at"]),
                        )
                    ),
                    request_id=request_id,
                    credential_version=cast(int, session["credential_version"]),
                    is_administrator=cast(bool, session["is_administrator"]),
                ),
                cast(bytes, session["csrf_hash"]),
            )

    def context_is_current(self, context: UserContext) -> bool:
        statement = (
            select(sessions.c.auth_session_id)
            .join(users, sessions.c.user_id == users.c.user_id)
            .where(
                sessions.c.auth_session_id == context.auth_session_id,
                sessions.c.user_id == context.user_id,
                sessions.c.credential_version == context.credential_version,
                users.c.credential_version == context.credential_version,
                sessions.c.is_administrator == context.is_administrator,
                users.c.is_administrator == context.is_administrator,
                sessions.c.scopes == list(context.scopes),
                users.c.enabled.is_(True),
                sessions.c.revoked_at.is_(None),
                sessions.c.idle_expires_at > func.clock_timestamp(),
                sessions.c.absolute_expires_at > func.clock_timestamp(),
            )
        )
        with self._engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def create_write_authorization(
        self,
        context: UserContext,
        authorization_token: str,
        request_id: str,
        operation: str,
        lifetime: timedelta,
    ) -> datetime:
        with self._engine.begin() as connection:
            session = (
                connection.execute(
                    select(
                        sessions,
                        users.c.enabled,
                        users.c.credential_version.label("current_credential_version"),
                        users.c.is_administrator.label("current_is_administrator"),
                    )
                    .join(users, sessions.c.user_id == users.c.user_id)
                    .where(
                        sessions.c.auth_session_id == context.auth_session_id,
                        sessions.c.user_id == context.user_id,
                    )
                    .with_for_update(of=[sessions, users])
                )
                .mappings()
                .one_or_none()
            )
            now = cast(datetime, connection.scalar(select(func.clock_timestamp())))
            if (
                session is None
                or not cast(bool, session["enabled"])
                or session["revoked_at"] is not None
                or now >= cast(datetime, session["idle_expires_at"])
                or now >= cast(datetime, session["absolute_expires_at"])
                or session["credential_version"] != session["current_credential_version"]
                or session["is_administrator"] != session["current_is_administrator"]
            ):
                raise unauthenticated()
            expires_at = min(
                now + lifetime,
                cast(datetime, session["idle_expires_at"]),
                cast(datetime, session["absolute_expires_at"]),
            )
            connection.execute(
                insert(write_authorizations).values(
                    authorization_hash=_token_hash(authorization_token),
                    auth_session_id=context.auth_session_id,
                    request_id=request_id,
                    operation=operation,
                    expires_at=expires_at,
                    consumed_at=None,
                    created_at=now,
                )
            )
            return expires_at

    def revoke_session(self, session_id: str, now: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(sessions)
                .where(sessions.c.auth_session_id == session_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    def revoke_authorized_session(
        self, authorization: BrowserWriteAuthorization, idempotency_key: str
    ) -> SessionLogoutResult:
        with self._engine.begin() as connection:
            try:
                context, now = self._consume_write_authorization(
                    connection, authorization, "session.logout", require_administrator=False
                )
            except AccessError:
                replay_session_id = self._bound_authorization_session_id(
                    connection, authorization, "session.logout"
                )
                if replay_session_id is None:
                    raise
                request_sha256 = _request_hash({"auth_session_id": replay_session_id})
                replay = self._replay_bound_session_mutation(
                    connection,
                    authorization,
                    "session.logout",
                    idempotency_key,
                    request_sha256,
                )
                if replay is None:
                    raise
                if isinstance(replay, AccessError):
                    raise replay
                return SessionLogoutResult(schema_version="v1", revoked=True)
            request_sha256 = _request_hash({"auth_session_id": context.auth_session_id})
            replay = self._begin_simple_idempotent(
                connection, context, "session.logout", idempotency_key, request_sha256, now
            )
            context, now = self._consume_write_authorization(
                connection, authorization, "session.logout", require_administrator=False
            )
            if isinstance(replay, AccessError):
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise replay
            if replay is not None:
                self._finish_write_authorization(connection, authorization, now)
                return SessionLogoutResult(schema_version="v1", revoked=True)
            result = SessionLogoutResult(schema_version="v1", revoked=True)
            self._finish_simple_idempotent(
                connection,
                context,
                "session.logout",
                idempotency_key,
                {"schema_version": result.schema_version, "revoked": result.revoked},
            )
            self._finish_write_authorization(connection, authorization, now)
            connection.execute(
                update(sessions)
                .where(
                    sessions.c.auth_session_id == context.auth_session_id,
                    sessions.c.user_id == context.user_id,
                    sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            return result

    def change_authorized_password(
        self,
        authorization: BrowserWriteAuthorization,
        password_hash: str,
        password_sha256: str,
        expected_version: int,
        idempotency_key: str,
    ) -> PasswordChangeResult:
        with self._engine.begin() as connection:
            request_sha256 = _request_hash(
                {"password_sha256": password_sha256, "expected_version": expected_version}
            )
            try:
                context, now = self._consume_write_authorization(
                    connection,
                    authorization,
                    "password.change",
                    require_administrator=False,
                )
            except AccessError:
                replay = self._replay_bound_session_mutation(
                    connection,
                    authorization,
                    "password.change",
                    idempotency_key,
                    request_sha256,
                )
                if replay is None:
                    raise
                if isinstance(replay, AccessError):
                    raise replay
                return PasswordChangeResult(
                    schema_version="v1", record_version=cast(int, replay["record_version"])
                )
            replay = self._begin_simple_idempotent(
                connection, context, "password.change", idempotency_key, request_sha256, now
            )
            context, now = self._consume_write_authorization(
                connection,
                authorization,
                "password.change",
                require_administrator=False,
            )
            if isinstance(replay, AccessError):
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise replay
            if replay is not None:
                self._finish_write_authorization(connection, authorization, now)
                return PasswordChangeResult(
                    schema_version="v1", record_version=cast(int, replay["record_version"])
                )
            current_version = connection.scalar(
                select(users.c.record_version).where(users.c.user_id == context.user_id)
            )
            if current_version != expected_version:
                error = stale_version(expected_version, cast(int, current_version))
                self._finish_simple_idempotent_error(
                    connection, context, "password.change", idempotency_key, error
                )
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise error
            connection.execute(
                update(users)
                .where(users.c.user_id == context.user_id, users.c.enabled.is_(True))
                .values(
                    password_hash=password_hash,
                    credential_version=users.c.credential_version + 1,
                    record_version=users.c.record_version + 1,
                    updated_at=now,
                )
            )
            changed = PasswordChangeResult(schema_version="v1", record_version=expected_version + 1)
            self._finish_simple_idempotent(
                connection,
                context,
                "password.change",
                idempotency_key,
                {
                    "schema_version": changed.schema_version,
                    "record_version": changed.record_version,
                },
            )
            self._finish_write_authorization(connection, authorization, now)
            connection.execute(
                update(sessions)
                .where(sessions.c.user_id == context.user_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            return changed

    def disable_authorized_user(
        self,
        authorization: BrowserWriteAuthorization,
        target_user_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> UserDisableResult:
        with self._engine.begin() as connection:
            context, now = self._consume_write_authorization(
                connection, authorization, "user.disable"
            )
            target = (
                connection.execute(
                    select(users.c.user_id, users.c.enabled, users.c.record_version)
                    .where(users.c.user_id == target_user_id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            request_sha256 = _request_hash(
                {"target_user_id": target_user_id, "expected_version": expected_version}
            )
            replay = self._begin_user_disable_idempotent(
                connection,
                context,
                idempotency_key,
                request_sha256,
                now,
            )
            context, now = self._consume_write_authorization(
                connection, authorization, "user.disable"
            )
            if isinstance(replay, AccessError):
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise replay
            if replay is not None:
                self._finish_write_authorization(connection, authorization, now)
                return replay
            try:
                if target_user_id == context.user_id:
                    raise AccessError(
                        ErrorCode.VALIDATION_ERROR,
                        "An administrator cannot disable their own user.",
                        422,
                        details={"path": "/target_user_id"},
                    )
                if target is None or not cast(bool, target["enabled"]):
                    raise not_found()
                current_version = cast(int, target["record_version"])
                if current_version != expected_version:
                    raise stale_version(expected_version, current_version)
            except AccessError as error:
                self._finish_idempotent_error(
                    connection, context, "user.disable", idempotency_key, error
                )
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise
            result = connection.execute(
                update(users)
                .where(users.c.user_id == target_user_id, users.c.enabled.is_(True))
                .values(
                    enabled=False,
                    credential_version=users.c.credential_version + 1,
                    record_version=users.c.record_version + 1,
                    updated_at=now,
                )
            )
            assert result.rowcount == 1
            connection.execute(
                update(sessions)
                .where(sessions.c.user_id == target_user_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            self._audit(connection, context.user_id, "USER_DISABLED", None, now)
            disabled = UserDisableResult(
                schema_version="v1",
                target_user_id=target_user_id,
                enabled=False,
                record_version=expected_version + 1,
            )
            self._finish_user_disable_idempotent(connection, context, idempotency_key, disabled)
            self._finish_write_authorization(connection, authorization, now)
            return disabled

    def change_password(self, user_id: str, password_hash: str, now: datetime) -> None:
        with self._engine.begin() as connection:
            locked_user = connection.execute(
                select(users.c.user_id)
                .where(users.c.user_id == user_id, users.c.enabled.is_(True))
                .with_for_update()
            ).scalar_one_or_none()
            if locked_user is None:
                raise not_found()
            result = connection.execute(
                update(users)
                .where(users.c.user_id == user_id, users.c.enabled.is_(True))
                .values(
                    password_hash=password_hash,
                    credential_version=users.c.credential_version + 1,
                    record_version=users.c.record_version + 1,
                    updated_at=now,
                )
            )
            assert result.rowcount == 1
            connection.execute(
                update(sessions)
                .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    def create_broker_account(
        self,
        authorization: BrowserWriteAuthorization,
        payload: Mapping[str, object],
        idempotency_key: str,
        cipher: EnvelopeCipher,
    ) -> BrokerAccountView:
        with self._engine.begin() as connection:
            context, now = self._consume_write_authorization(
                connection, authorization, "broker_account.create"
            )
            create = parse_broker_account_create(dict(payload))
            request_sha256 = _request_hash(
                {
                    "capabilities": sorted(set(create.capabilities)),
                    "credential_sha256": hashlib.sha256(create.credential).hexdigest(),
                    "environment": create.environment.value,
                    "provider": create.provider,
                    "provider_account_reference": create.provider_account_reference,
                }
            )
            replay = self._begin_idempotent(
                connection,
                context,
                "broker_account.create",
                idempotency_key,
                request_sha256,
                now,
            )
            context, now = self._consume_write_authorization(
                connection, authorization, "broker_account.create"
            )
            if isinstance(replay, AccessError):
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise replay
            if replay is not None:
                self._finish_write_authorization(connection, authorization, now)
                return replay
            account_id = str(uuid7())
            reference_id = str(uuid7())
            credential_id = str(uuid7())
            binding = {
                "owner_user_id": context.user_id,
                "account_id": account_id,
                "provider": create.provider,
            }
            try:
                reference_envelope = cipher.encrypt(
                    create.provider_account_reference.encode("utf-8"),
                    purpose="provider_account_ref",
                    **binding,
                )
                credential_envelope = cipher.encrypt(
                    create.credential, purpose="provider_credential", **binding
                )
            except AccessError as error:
                self._finish_idempotent_error(
                    connection,
                    context,
                    "broker_account.create",
                    idempotency_key,
                    error,
                )
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise
            self._insert_envelope(
                connection,
                reference_id,
                context.user_id,
                account_id,
                create.provider,
                "provider_account_ref",
                reference_envelope,
                now,
            )
            self._insert_envelope(
                connection,
                credential_id,
                context.user_id,
                account_id,
                create.provider,
                "provider_credential",
                credential_envelope,
                now,
            )
            connection.execute(
                insert(broker_accounts).values(
                    broker_account_id=account_id,
                    schema_version="v1",
                    owner_user_id=context.user_id,
                    provider=create.provider,
                    provider_account_ref_ciphertext_id=reference_id,
                    provider_account_ref_purpose="provider_account_ref",
                    provider_account_ref_last_four=create.provider_account_reference[-4:],
                    environment=create.environment.value,
                    status=BrokerAccountStatus.PENDING_VERIFICATION.value,
                    capabilities=sorted(set(create.capabilities)),
                    credential_envelope_id=credential_id,
                    credential_purpose="provider_credential",
                    verified_at=None,
                    last_reconciled_at=None,
                    created_at=now,
                    updated_at=now,
                    record_version=1,
                )
            )
            self._audit(connection, context.user_id, "BROKER_ACCOUNT_CREATED", account_id, now)
            result = self._get_broker_account(connection, context, account_id)
            self._finish_idempotent(
                connection, context, "broker_account.create", idempotency_key, result
            )
            self._finish_write_authorization(connection, authorization, now)
            return result

    def get_broker_account(self, context: UserContext, account_id: str) -> BrokerAccountView:
        with self._engine.connect() as connection:
            return self._get_broker_account(connection, context, account_id)

    @staticmethod
    def _get_broker_account(
        connection: Connection, context: UserContext, account_id: str
    ) -> BrokerAccountView:
        statement = (
            select(broker_accounts, credential_envelopes.c.status.label("credential_status"))
            .join(
                credential_envelopes,
                and_(
                    broker_accounts.c.credential_envelope_id
                    == credential_envelopes.c.credential_envelope_id,
                    broker_accounts.c.broker_account_id == credential_envelopes.c.broker_account_id,
                    broker_accounts.c.provider == credential_envelopes.c.provider,
                    credential_envelopes.c.purpose == "provider_credential",
                ),
            )
            .where(
                broker_accounts.c.broker_account_id == account_id,
                broker_accounts.c.owner_user_id == context.user_id,
                credential_envelopes.c.owner_user_id == context.user_id,
            )
        )
        row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            raise not_found()
        return _account_view(row)

    def list_broker_accounts(self, context: UserContext) -> tuple[BrokerAccountView, ...]:
        statement = (
            select(broker_accounts, credential_envelopes.c.status.label("credential_status"))
            .join(
                credential_envelopes,
                and_(
                    broker_accounts.c.credential_envelope_id
                    == credential_envelopes.c.credential_envelope_id,
                    broker_accounts.c.broker_account_id == credential_envelopes.c.broker_account_id,
                    broker_accounts.c.provider == credential_envelopes.c.provider,
                    credential_envelopes.c.purpose == "provider_credential",
                ),
            )
            .where(
                broker_accounts.c.owner_user_id == context.user_id,
                credential_envelopes.c.owner_user_id == context.user_id,
            )
            .order_by(broker_accounts.c.created_at, broker_accounts.c.broker_account_id)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_account_view(row) for row in rows)

    def replace_credential(
        self,
        authorization: BrowserWriteAuthorization,
        account_id: str,
        new_secret: bytes,
        expected_version: int,
        idempotency_key: str,
        cipher: EnvelopeCipher,
    ) -> BrokerAccountView:
        with self._engine.begin() as connection:
            context, now = self._consume_write_authorization(
                connection, authorization, "credential.replace"
            )
            account = self._owned_account_for_update(connection, context, account_id)
            validate_secret_bytes(new_secret, path="/credential")
            request_sha256 = _request_hash(
                {
                    "account_id": account_id,
                    "credential_sha256": hashlib.sha256(new_secret).hexdigest(),
                    "expected_version": expected_version,
                }
            )
            replay = self._begin_idempotent(
                connection,
                context,
                "credential.replace",
                idempotency_key,
                request_sha256,
                now,
            )
            context, now = self._consume_write_authorization(
                connection, authorization, "credential.replace"
            )
            if isinstance(replay, AccessError):
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise replay
            if replay is not None:
                self._finish_write_authorization(connection, authorization, now)
                return replay
            try:
                if account["status"] == BrokerAccountStatus.DISABLED.value:
                    raise AccessError(
                        ErrorCode.DEPENDENCY_UNAVAILABLE,
                        "Broker account is disabled.",
                        503,
                    )
                current_version = cast(int, account["record_version"])
                if current_version != expected_version:
                    raise stale_version(expected_version, current_version)
                new_envelope = cipher.encrypt(
                    new_secret,
                    owner_user_id=context.user_id,
                    account_id=account_id,
                    provider=cast(str, account["provider"]),
                    purpose="provider_credential",
                )
                cipher.verify(
                    new_envelope,
                    new_secret,
                    owner_user_id=context.user_id,
                    account_id=account_id,
                    provider=cast(str, account["provider"]),
                    purpose="provider_credential",
                )
            except AccessError as error:
                self._finish_idempotent_error(
                    connection, context, "credential.replace", idempotency_key, error
                )
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise
            new_id = str(uuid7())
            old_id = cast(str, account["credential_envelope_id"])
            connection.execute(
                update(credential_envelopes)
                .where(
                    credential_envelopes.c.credential_envelope_id == old_id,
                    credential_envelopes.c.owner_user_id == context.user_id,
                    credential_envelopes.c.status == "active",
                )
                .values(status="revoked", revoked_at=now)
            )
            self._insert_envelope(
                connection,
                new_id,
                context.user_id,
                account_id,
                cast(str, account["provider"]),
                "provider_credential",
                new_envelope,
                now,
            )
            connection.execute(
                update(broker_accounts)
                .where(
                    broker_accounts.c.broker_account_id == account_id,
                    broker_accounts.c.owner_user_id == context.user_id,
                )
                .values(
                    credential_envelope_id=new_id,
                    status=BrokerAccountStatus.REAUTH_REQUIRED.value,
                    updated_at=now,
                    record_version=broker_accounts.c.record_version + 1,
                )
            )
            self._audit(connection, context.user_id, "CREDENTIAL_REPLACED", account_id, now)
            result = self._get_broker_account(connection, context, account_id)
            self._finish_idempotent(
                connection, context, "credential.replace", idempotency_key, result
            )
            self._finish_write_authorization(connection, authorization, now)
            return result

    def revoke_credential(
        self,
        authorization: BrowserWriteAuthorization,
        account_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> BrokerAccountView:
        with self._engine.begin() as connection:
            context, now = self._consume_write_authorization(
                connection, authorization, "credential.revoke"
            )
            account = self._owned_account_for_update(connection, context, account_id)
            request_sha256 = _request_hash(
                {"account_id": account_id, "expected_version": expected_version}
            )
            replay = self._begin_idempotent(
                connection,
                context,
                "credential.revoke",
                idempotency_key,
                request_sha256,
                now,
            )
            context, now = self._consume_write_authorization(
                connection, authorization, "credential.revoke"
            )
            if isinstance(replay, AccessError):
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise replay
            if replay is not None:
                self._finish_write_authorization(connection, authorization, now)
                return replay
            try:
                current_version = cast(int, account["record_version"])
                if current_version != expected_version:
                    raise stale_version(expected_version, current_version)
            except AccessError as error:
                self._finish_idempotent_error(
                    connection, context, "credential.revoke", idempotency_key, error
                )
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise
            credential_id = cast(str, account["credential_envelope_id"])
            connection.execute(
                update(credential_envelopes)
                .where(
                    credential_envelopes.c.credential_envelope_id == credential_id,
                    credential_envelopes.c.owner_user_id == context.user_id,
                )
                .values(status="revoked", revoked_at=now)
            )
            connection.execute(
                update(broker_accounts)
                .where(
                    broker_accounts.c.broker_account_id == account_id,
                    broker_accounts.c.owner_user_id == context.user_id,
                )
                .values(
                    status=BrokerAccountStatus.DISABLED.value,
                    updated_at=now,
                    record_version=broker_accounts.c.record_version + 1,
                )
            )
            self._audit(connection, context.user_id, "CREDENTIAL_REVOKED", account_id, now)
            result = self._get_broker_account(connection, context, account_id)
            self._finish_idempotent(
                connection, context, "credential.revoke", idempotency_key, result
            )
            self._finish_write_authorization(connection, authorization, now)
            return result

    def rewrap_credentials(
        self,
        authorization: BrowserWriteAuthorization,
        account_id: str,
        expected_version: int,
        idempotency_key: str,
        cipher: EnvelopeCipher,
    ) -> BrokerAccountView:
        with self._engine.begin() as connection:
            context, now = self._consume_write_authorization(
                connection, authorization, "credential.rewrap"
            )
            account = self._owned_account_for_update(connection, context, account_id)
            request_sha256 = _request_hash(
                {"account_id": account_id, "expected_version": expected_version}
            )
            replay = self._begin_idempotent(
                connection,
                context,
                "credential.rewrap",
                idempotency_key,
                request_sha256,
                now,
            )
            context, now = self._consume_write_authorization(
                connection, authorization, "credential.rewrap"
            )
            if isinstance(replay, AccessError):
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise replay
            if replay is not None:
                self._finish_write_authorization(connection, authorization, now)
                return replay
            savepoint = connection.begin_nested()
            try:
                current_version = cast(int, account["record_version"])
                if current_version != expected_version:
                    raise stale_version(expected_version, current_version)
                rows = (
                    connection.execute(
                        select(credential_envelopes)
                        .where(
                            credential_envelopes.c.owner_user_id == context.user_id,
                            credential_envelopes.c.broker_account_id == account_id,
                            credential_envelopes.c.status == "active",
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .all()
                )
                if {cast(str, row["purpose"]) for row in rows} != {
                    "provider_account_ref",
                    "provider_credential",
                }:
                    raise AccessError(
                        ErrorCode.DEPENDENCY_UNAVAILABLE,
                        "Credential is unavailable.",
                        503,
                    )
                for row in rows:
                    purpose = cast(str, row["purpose"])
                    rewrapped = cipher.rewrap(
                        _credential_envelope(row),
                        owner_user_id=context.user_id,
                        account_id=account_id,
                        provider=cast(str, account["provider"]),
                        purpose=purpose,
                    )
                    connection.execute(
                        update(credential_envelopes)
                        .where(
                            credential_envelopes.c.credential_envelope_id
                            == row["credential_envelope_id"],
                            credential_envelopes.c.owner_user_id == context.user_id,
                        )
                        .values(
                            wrapped_dek=rewrapped.wrapped_dek,
                            wrap_nonce=rewrapped.wrap_nonce,
                            key_version=rewrapped.key_version,
                        )
                    )
            except AccessError as error:
                savepoint.rollback()
                self._finish_idempotent_error(
                    connection, context, "credential.rewrap", idempotency_key, error
                )
                self._finish_write_authorization(connection, authorization, now)
                connection.commit()
                raise
            else:
                savepoint.commit()
            connection.execute(
                update(broker_accounts)
                .where(
                    broker_accounts.c.broker_account_id == account_id,
                    broker_accounts.c.owner_user_id == context.user_id,
                )
                .values(
                    updated_at=now,
                    record_version=broker_accounts.c.record_version + 1,
                )
            )
            self._audit(connection, context.user_id, "CREDENTIAL_REWRAPPED", account_id, now)
            result = self._get_broker_account(connection, context, account_id)
            self._finish_idempotent(
                connection, context, "credential.rewrap", idempotency_key, result
            )
            self._finish_write_authorization(connection, authorization, now)
            return result

    def envelope_rows_for_test(self, account_id: str) -> list[RowMapping]:
        """Return encrypted persistence rows; only test code uses this evidence hook."""
        with self._engine.connect() as connection:
            return list(
                connection.execute(
                    select(credential_envelopes)
                    .where(credential_envelopes.c.broker_account_id == account_id)
                    .order_by(credential_envelopes.c.created_at)
                ).mappings()
            )

    def audit_not_found(self, context: UserContext) -> None:
        """Audit an opaque denial without persisting the guessed identifier."""
        with self._engine.begin() as connection:
            now = cast(datetime, connection.scalar(select(func.clock_timestamp())))
            self._audit(connection, context.user_id, "RESOURCE_NOT_FOUND", None, now)

    def audit_authorized_not_found(self, authorization: BrowserWriteAuthorization) -> None:
        with self._engine.begin() as connection:
            now = cast(datetime, connection.scalar(select(func.clock_timestamp())))
            actor_user_id = connection.scalar(
                select(sessions.c.user_id)
                .join(
                    write_authorizations,
                    write_authorizations.c.auth_session_id == sessions.c.auth_session_id,
                )
                .where(
                    write_authorizations.c.authorization_hash
                    == _token_hash(authorization.authorization_token),
                    write_authorizations.c.request_id == authorization.request_id,
                )
            )
            if actor_user_id is not None:
                self._audit(
                    connection,
                    cast(str, actor_user_id),
                    "RESOURCE_NOT_FOUND",
                    None,
                    now,
                )

    @staticmethod
    def _consume_write_authorization(
        connection: Connection,
        authorization: BrowserWriteAuthorization,
        expected_operation: str,
        *,
        require_administrator: bool = True,
    ) -> tuple[UserContext, datetime]:
        statement = (
            select(
                write_authorizations,
                sessions.c.user_id,
                sessions.c.scopes,
                sessions.c.is_administrator.label("session_is_administrator"),
                sessions.c.credential_version,
                sessions.c.authenticated_at,
                sessions.c.idle_expires_at,
                sessions.c.absolute_expires_at,
                sessions.c.revoked_at,
                users.c.enabled,
                users.c.is_administrator.label("current_is_administrator"),
                users.c.credential_version.label("current_credential_version"),
            )
            .join(
                sessions,
                write_authorizations.c.auth_session_id == sessions.c.auth_session_id,
            )
            .join(users, sessions.c.user_id == users.c.user_id)
            .where(
                write_authorizations.c.authorization_hash
                == _token_hash(authorization.authorization_token),
                write_authorizations.c.request_id == authorization.request_id,
                write_authorizations.c.operation == expected_operation,
            )
            .with_for_update(of=[write_authorizations, sessions, users])
        )
        row = connection.execute(statement).mappings().one_or_none()
        now = cast(datetime, connection.scalar(select(func.clock_timestamp())))
        if (
            row is None
            or authorization.operation != expected_operation
            or row["consumed_at"] is not None
            or now >= cast(datetime, row["expires_at"])
        ):
            raise AccessError(
                ErrorCode.INVALID_CSRF, "Browser write authorization is invalid.", 403
            )
        if (
            not cast(bool, row["enabled"])
            or row["revoked_at"] is not None
            or now >= cast(datetime, row["idle_expires_at"])
            or now >= cast(datetime, row["absolute_expires_at"])
            or row["credential_version"] != row["current_credential_version"]
        ):
            raise unauthenticated()
        is_administrator = cast(bool, row["session_is_administrator"]) and cast(
            bool, row["current_is_administrator"]
        )
        if require_administrator and not is_administrator:
            raise AccessError(
                ErrorCode.INSUFFICIENT_SCOPE,
                "Administrator authorization is required.",
                403,
            )
        expires_at = min(
            cast(datetime, row["idle_expires_at"]),
            cast(datetime, row["absolute_expires_at"]),
        )
        return UserContext(
            schema_version="v1",
            user_id=cast(str, row["user_id"]),
            auth_session_id=cast(str, row["auth_session_id"]),
            auth_method="browser_session",
            scopes=tuple(sorted(set(cast(list[str], row["scopes"])))),
            authenticated_at=cast(datetime, row["authenticated_at"]),
            expires_at=expires_at,
            request_id=authorization.request_id,
            credential_version=cast(int, row["credential_version"]),
            is_administrator=is_administrator,
        ), now

    @staticmethod
    def _finish_write_authorization(
        connection: Connection, authorization: BrowserWriteAuthorization, now: datetime
    ) -> None:
        result = connection.execute(
            update(write_authorizations)
            .where(
                write_authorizations.c.authorization_hash
                == _token_hash(authorization.authorization_token),
                write_authorizations.c.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        if result.rowcount != 1:
            raise AccessError(
                ErrorCode.INVALID_CSRF, "Browser write authorization is invalid.", 403
            )

    @staticmethod
    def _begin_idempotent(
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        request_sha256: bytes,
        now: datetime,
    ) -> BrokerAccountView | AccessError | None:
        normalized_key = _normalize_idempotency_key(idempotency_key)
        connection.execute(
            pg_insert(idempotency_records)
            .values(
                owner_user_id=context.user_id,
                operation=operation,
                idempotency_key=normalized_key,
                request_sha256=request_sha256,
                result=None,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    idempotency_records.c.owner_user_id,
                    idempotency_records.c.operation,
                    idempotency_records.c.idempotency_key,
                ]
            )
        )
        record = (
            connection.execute(
                select(idempotency_records)
                .where(
                    idempotency_records.c.owner_user_id == context.user_id,
                    idempotency_records.c.operation == operation,
                    idempotency_records.c.idempotency_key == normalized_key,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if not hmac.compare_digest(cast(bytes, record["request_sha256"]), request_sha256):
            raise AccessError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "The idempotency key was reused with a different request.",
                409,
            )
        stored_result = cast(dict[str, Any] | None, record["result"])
        if stored_result is None:
            return None
        if stored_result.get("outcome") == "error":
            stored_error = cast(dict[str, Any], stored_result["error"])
            return AccessError(
                ErrorCode(cast(str, stored_error["code"])),
                cast(str, stored_error["message"]),
                cast(int, stored_error["http_status"]),
                retryable=cast(bool, stored_error["retryable"]),
                details=cast(dict[str, Any], stored_error["details"]),
            )
        if stored_result.get("outcome") == "success":
            return _account_view_from_record(cast(dict[str, Any], stored_result["value"]))
        return _account_view_from_record(stored_result)

    @staticmethod
    def _finish_idempotent(
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        result: BrokerAccountView,
    ) -> None:
        connection.execute(
            update(idempotency_records)
            .where(
                idempotency_records.c.owner_user_id == context.user_id,
                idempotency_records.c.operation == operation,
                idempotency_records.c.idempotency_key
                == _normalize_idempotency_key(idempotency_key),
            )
            .values(result={"outcome": "success", "value": _account_view_record(result)})
        )

    @staticmethod
    def _finish_idempotent_error(
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        error: AccessError,
    ) -> None:
        safe_detail_keys = {"expected_version", "current_version", "path", "paths"}
        safe_details = {
            key: value for key, value in error.details.items() if key in safe_detail_keys
        }
        connection.execute(
            update(idempotency_records)
            .where(
                idempotency_records.c.owner_user_id == context.user_id,
                idempotency_records.c.operation == operation,
                idempotency_records.c.idempotency_key
                == _normalize_idempotency_key(idempotency_key),
            )
            .values(
                result={
                    "outcome": "error",
                    "error": {
                        "code": error.code.value,
                        "message": error.message,
                        "http_status": error.http_status,
                        "retryable": error.retryable,
                        "details": safe_details,
                    },
                }
            )
        )

    @staticmethod
    def _begin_simple_idempotent(
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        request_sha256: bytes,
        now: datetime,
    ) -> dict[str, Any] | AccessError | None:
        normalized_key = _normalize_idempotency_key(idempotency_key)
        connection.execute(
            pg_insert(idempotency_records)
            .values(
                owner_user_id=context.user_id,
                operation=operation,
                idempotency_key=normalized_key,
                request_sha256=request_sha256,
                result=None,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    idempotency_records.c.owner_user_id,
                    idempotency_records.c.operation,
                    idempotency_records.c.idempotency_key,
                ]
            )
        )
        record = (
            connection.execute(
                select(idempotency_records)
                .where(
                    idempotency_records.c.owner_user_id == context.user_id,
                    idempotency_records.c.operation == operation,
                    idempotency_records.c.idempotency_key == normalized_key,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        return _decode_simple_idempotent(record, request_sha256)

    @staticmethod
    def _finish_simple_idempotent(
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        value: dict[str, Any],
    ) -> None:
        connection.execute(
            update(idempotency_records)
            .where(
                idempotency_records.c.owner_user_id == context.user_id,
                idempotency_records.c.operation == operation,
                idempotency_records.c.idempotency_key
                == _normalize_idempotency_key(idempotency_key),
            )
            .values(
                result={
                    "outcome": "success",
                    "auth_session_id": context.auth_session_id,
                    "value": value,
                }
            )
        )

    @staticmethod
    def _finish_simple_idempotent_error(
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        error: AccessError,
    ) -> None:
        safe_detail_keys = {"expected_version", "current_version", "path", "paths"}
        safe_details = {
            key: value for key, value in error.details.items() if key in safe_detail_keys
        }
        connection.execute(
            update(idempotency_records)
            .where(
                idempotency_records.c.owner_user_id == context.user_id,
                idempotency_records.c.operation == operation,
                idempotency_records.c.idempotency_key
                == _normalize_idempotency_key(idempotency_key),
            )
            .values(
                result={
                    "outcome": "error",
                    "auth_session_id": context.auth_session_id,
                    "error": {
                        "code": error.code.value,
                        "message": error.message,
                        "http_status": error.http_status,
                        "retryable": error.retryable,
                        "details": safe_details,
                    },
                }
            )
        )

    @staticmethod
    def _bound_authorization_session_id(
        connection: Connection,
        authorization: BrowserWriteAuthorization,
        operation: str,
    ) -> str | None:
        if authorization.operation != operation:
            return None
        return cast(
            str | None,
            connection.scalar(
                select(write_authorizations.c.auth_session_id).where(
                    write_authorizations.c.authorization_hash
                    == _token_hash(authorization.authorization_token),
                    write_authorizations.c.request_id == authorization.request_id,
                    write_authorizations.c.operation == operation,
                )
            ),
        )

    @staticmethod
    def _replay_bound_session_mutation(
        connection: Connection,
        authorization: BrowserWriteAuthorization,
        operation: str,
        idempotency_key: str,
        request_sha256: bytes,
    ) -> dict[str, Any] | AccessError | None:
        if authorization.operation != operation:
            return None
        authorization_row = (
            connection.execute(
                select(
                    sessions.c.user_id,
                    sessions.c.auth_session_id,
                    sessions.c.revoked_at,
                    write_authorizations.c.consumed_at,
                )
                .join(
                    sessions,
                    write_authorizations.c.auth_session_id == sessions.c.auth_session_id,
                )
                .where(
                    write_authorizations.c.authorization_hash
                    == _token_hash(authorization.authorization_token),
                    write_authorizations.c.request_id == authorization.request_id,
                    write_authorizations.c.operation == operation,
                )
            )
            .mappings()
            .one_or_none()
        )
        if authorization_row is None or (
            authorization_row["consumed_at"] is None and authorization_row["revoked_at"] is None
        ):
            return None
        record = (
            connection.execute(
                select(idempotency_records).where(
                    idempotency_records.c.owner_user_id == authorization_row["user_id"],
                    idempotency_records.c.operation == operation,
                    idempotency_records.c.idempotency_key
                    == _normalize_idempotency_key(idempotency_key),
                    idempotency_records.c.result.is_not(None),
                )
            )
            .mappings()
            .one_or_none()
        )
        if record is None:
            return None
        stored_result = cast(dict[str, Any], record["result"])
        if stored_result.get("auth_session_id") != authorization_row["auth_session_id"]:
            return None
        return _decode_simple_idempotent(record, request_sha256)

    @staticmethod
    def _begin_user_disable_idempotent(
        connection: Connection,
        context: UserContext,
        idempotency_key: str,
        request_sha256: bytes,
        now: datetime,
    ) -> UserDisableResult | AccessError | None:
        normalized_key = _normalize_idempotency_key(idempotency_key)
        connection.execute(
            pg_insert(idempotency_records)
            .values(
                owner_user_id=context.user_id,
                operation="user.disable",
                idempotency_key=normalized_key,
                request_sha256=request_sha256,
                result=None,
                created_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    idempotency_records.c.owner_user_id,
                    idempotency_records.c.operation,
                    idempotency_records.c.idempotency_key,
                ]
            )
        )
        record = (
            connection.execute(
                select(idempotency_records)
                .where(
                    idempotency_records.c.owner_user_id == context.user_id,
                    idempotency_records.c.operation == "user.disable",
                    idempotency_records.c.idempotency_key == normalized_key,
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )
        if not hmac.compare_digest(cast(bytes, record["request_sha256"]), request_sha256):
            raise AccessError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "The idempotency key was reused with a different request.",
                409,
            )
        stored_result = cast(dict[str, Any] | None, record["result"])
        if stored_result is None:
            return None
        if stored_result.get("outcome") == "error":
            stored_error = cast(dict[str, Any], stored_result["error"])
            return AccessError(
                ErrorCode(cast(str, stored_error["code"])),
                cast(str, stored_error["message"]),
                cast(int, stored_error["http_status"]),
                retryable=cast(bool, stored_error["retryable"]),
                details=cast(dict[str, Any], stored_error["details"]),
            )
        value = cast(dict[str, Any], stored_result["value"])
        return UserDisableResult(
            schema_version="v1",
            target_user_id=cast(str, value["target_user_id"]),
            enabled=False,
            record_version=cast(int, value["record_version"]),
        )

    @staticmethod
    def _finish_user_disable_idempotent(
        connection: Connection,
        context: UserContext,
        idempotency_key: str,
        result: UserDisableResult,
    ) -> None:
        connection.execute(
            update(idempotency_records)
            .where(
                idempotency_records.c.owner_user_id == context.user_id,
                idempotency_records.c.operation == "user.disable",
                idempotency_records.c.idempotency_key
                == _normalize_idempotency_key(idempotency_key),
            )
            .values(
                result={
                    "outcome": "success",
                    "value": {
                        "schema_version": result.schema_version,
                        "target_user_id": result.target_user_id,
                        "enabled": result.enabled,
                        "record_version": result.record_version,
                    },
                }
            )
        )

    @staticmethod
    def _insert_envelope(
        connection: Connection,
        envelope_id: str,
        owner_user_id: str,
        account_id: str,
        provider: str,
        purpose: str,
        envelope: CredentialEnvelope,
        now: datetime,
    ) -> None:
        connection.execute(
            insert(credential_envelopes).values(
                credential_envelope_id=envelope_id,
                owner_user_id=owner_user_id,
                broker_account_id=account_id,
                provider=provider,
                purpose=purpose,
                ciphertext=envelope.ciphertext,
                nonce=envelope.nonce,
                wrapped_dek=envelope.wrapped_dek,
                wrap_nonce=envelope.wrap_nonce,
                key_version=envelope.key_version,
                status="active",
                created_at=now,
                revoked_at=None,
            )
        )

    @staticmethod
    def _owned_account_for_update(
        connection: Connection, context: UserContext, account_id: str
    ) -> RowMapping:
        row = (
            connection.execute(
                select(broker_accounts)
                .where(
                    broker_accounts.c.broker_account_id == account_id,
                    broker_accounts.c.owner_user_id == context.user_id,
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise not_found()
        return row

    @staticmethod
    def _audit(
        connection: Connection,
        actor_user_id: str,
        event_type: str,
        account_id: str | None,
        now: datetime,
    ) -> None:
        connection.execute(
            insert(audit_events).values(
                audit_event_id=str(uuid7()),
                actor_user_id=actor_user_id,
                event_type=event_type,
                broker_account_id=account_id,
                created_at=now,
            )
        )


def _account_view(row: RowMapping) -> BrokerAccountView:
    return BrokerAccountView(
        schema_version="v1",
        broker_account_id=cast(str, row["broker_account_id"]),
        owner_user_id=cast(str, row["owner_user_id"]),
        provider=cast(str, row["provider"]),
        provider_account_reference=mask_reference(cast(str, row["provider_account_ref_last_four"])),
        environment=BrokerEnvironment(cast(str, row["environment"])),
        status=BrokerAccountStatus(cast(str, row["status"])),
        capabilities=tuple(cast(Sequence[str], row["capabilities"])),
        credential_status=cast(Any, row["credential_status"]),
        verified_at=cast(datetime | None, row["verified_at"]),
        last_reconciled_at=cast(datetime | None, row["last_reconciled_at"]),
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
        record_version=cast(int, row["record_version"]),
    )


def _credential_envelope(row: RowMapping) -> CredentialEnvelope:
    return CredentialEnvelope(
        ciphertext=cast(bytes, row["ciphertext"]),
        nonce=cast(bytes, row["nonce"]),
        wrapped_dek=cast(bytes, row["wrapped_dek"]),
        wrap_nonce=cast(bytes, row["wrap_nonce"]),
        key_version=cast(str, row["key_version"]),
    )


def _normalize_idempotency_key(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError) as exc:
        raise AccessError(
            ErrorCode.VALIDATION_ERROR,
            "idempotency_key must be a UUID.",
            422,
            details={"path": "/idempotency_key"},
        ) from exc


def _request_hash(value: dict[str, Any]) -> bytes:
    canonical = json.dumps(
        _normalize_canonical_json(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def _normalize_canonical_json(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", key): _normalize_canonical_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_canonical_json(item) for item in value]
    return value


def _decode_simple_idempotent(
    record: RowMapping, request_sha256: bytes
) -> dict[str, Any] | AccessError | None:
    if not hmac.compare_digest(cast(bytes, record["request_sha256"]), request_sha256):
        raise AccessError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "The idempotency key was reused with a different request.",
            409,
        )
    stored_result = cast(dict[str, Any] | None, record["result"])
    if stored_result is None:
        return None
    if stored_result.get("outcome") == "error":
        stored_error = cast(dict[str, Any], stored_result["error"])
        return AccessError(
            ErrorCode(cast(str, stored_error["code"])),
            cast(str, stored_error["message"]),
            cast(int, stored_error["http_status"]),
            retryable=cast(bool, stored_error["retryable"]),
            details=cast(dict[str, Any], stored_error["details"]),
        )
    return cast(dict[str, Any], stored_result["value"])


def _token_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _account_view_record(view: BrokerAccountView) -> dict[str, Any]:
    return {
        "schema_version": view.schema_version,
        "broker_account_id": view.broker_account_id,
        "owner_user_id": view.owner_user_id,
        "provider": view.provider,
        "provider_account_reference": view.provider_account_reference,
        "environment": view.environment.value,
        "status": view.status.value,
        "capabilities": list(view.capabilities),
        "credential_status": view.credential_status,
        "verified_at": view.verified_at.isoformat() if view.verified_at else None,
        "last_reconciled_at": (
            view.last_reconciled_at.isoformat() if view.last_reconciled_at else None
        ),
        "created_at": view.created_at.isoformat(),
        "updated_at": view.updated_at.isoformat(),
        "record_version": view.record_version,
    }


def _account_view_from_record(record: dict[str, Any]) -> BrokerAccountView:
    return BrokerAccountView(
        schema_version="v1",
        broker_account_id=cast(str, record["broker_account_id"]),
        owner_user_id=cast(str, record["owner_user_id"]),
        provider=cast(str, record["provider"]),
        provider_account_reference=cast(str, record["provider_account_reference"]),
        environment=BrokerEnvironment(cast(str, record["environment"])),
        status=BrokerAccountStatus(cast(str, record["status"])),
        capabilities=tuple(cast(list[str], record["capabilities"])),
        credential_status=cast(Any, record["credential_status"]),
        verified_at=(
            datetime.fromisoformat(cast(str, record["verified_at"]))
            if record["verified_at"] is not None
            else None
        ),
        last_reconciled_at=(
            datetime.fromisoformat(cast(str, record["last_reconciled_at"]))
            if record["last_reconciled_at"] is not None
            else None
        ),
        created_at=datetime.fromisoformat(cast(str, record["created_at"])),
        updated_at=datetime.fromisoformat(cast(str, record["updated_at"])),
        record_version=cast(int, record["record_version"]),
    )
