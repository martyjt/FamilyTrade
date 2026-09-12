"""PostgreSQL persistence with owner predicates at every resource boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid7

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    and_,
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
    BrokerAccountCreate,
    BrokerAccountStatus,
    BrokerAccountView,
    BrokerEnvironment,
    ErrorCode,
    UserContext,
    not_found,
    stale_version,
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
    Column("enabled", Boolean, nullable=False),
    Column("credential_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("credential_version >= 1", name="ck_access_users_credential_version"),
)

sessions = Table(
    "access_sessions",
    access_metadata,
    StringColumn("auth_session_id", primary_key=True),
    StringColumn("user_id", ForeignKey("access_users.user_id"), nullable=False, index=True),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("csrf_hash", LargeBinary, nullable=False),
    Column("scopes", ARRAY(Text), nullable=False),
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
)

broker_accounts = Table(
    "access_broker_accounts",
    access_metadata,
    StringColumn("broker_account_id", primary_key=True),
    StringColumn("schema_version", nullable=False),
    StringColumn("owner_user_id", ForeignKey("access_users.user_id"), nullable=False, index=True),
    StringColumn("provider", nullable=False),
    StringColumn("provider_account_ref_ciphertext_id", nullable=False),
    StringColumn("provider_account_ref_last_four", nullable=False),
    StringColumn("environment", nullable=False),
    StringColumn("status", nullable=False),
    Column("capabilities", ARRAY(Text), nullable=False),
    StringColumn("credential_envelope_id", nullable=False),
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
                        enabled=True,
                        credential_version=1,
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
        scopes: Sequence[str],
        credential_version: int,
        authenticated_at: datetime,
        idle_expires_at: datetime,
        absolute_expires_at: datetime,
    ) -> str:
        session_id = str(uuid7())
        with self._engine.begin() as connection:
            connection.execute(
                insert(sessions).values(
                    auth_session_id=session_id,
                    user_id=user_id,
                    token_hash=token_hash,
                    csrf_hash=csrf_hash,
                    scopes=list(scopes),
                    credential_version=credential_version,
                    authenticated_at=authenticated_at,
                    last_seen_at=authenticated_at,
                    idle_expires_at=idle_expires_at,
                    absolute_expires_at=absolute_expires_at,
                    revoked_at=None,
                )
            )
        return session_id

    def get_session(self, token_hash: bytes) -> RowMapping | None:
        statement = (
            select(
                sessions,
                users.c.enabled,
                users.c.credential_version.label("current_credential_version"),
            )
            .join(users, sessions.c.user_id == users.c.user_id)
            .where(sessions.c.token_hash == token_hash)
        )
        with self._engine.connect() as connection:
            return connection.execute(statement).mappings().one_or_none()

    def context_is_current(self, context: UserContext, now: datetime) -> bool:
        statement = (
            select(sessions.c.auth_session_id)
            .join(users, sessions.c.user_id == users.c.user_id)
            .where(
                sessions.c.auth_session_id == context.auth_session_id,
                sessions.c.user_id == context.user_id,
                sessions.c.credential_version == context.credential_version,
                users.c.credential_version == context.credential_version,
                users.c.enabled.is_(True),
                sessions.c.revoked_at.is_(None),
                sessions.c.idle_expires_at > now,
                sessions.c.absolute_expires_at > now,
            )
        )
        with self._engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none() is not None

    def touch_session(self, session_id: str, now: datetime, idle_expires_at: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(sessions)
                .where(sessions.c.auth_session_id == session_id)
                .values(last_seen_at=now, idle_expires_at=idle_expires_at)
            )

    def revoke_session(self, session_id: str, now: datetime) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(sessions)
                .where(sessions.c.auth_session_id == session_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    def disable_user(self, user_id: str, now: datetime) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(users)
                .where(users.c.user_id == user_id)
                .values(
                    enabled=False, credential_version=users.c.credential_version + 1, updated_at=now
                )
            )
            if result.rowcount != 1:
                raise not_found()
            connection.execute(
                update(sessions)
                .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    def change_password(self, user_id: str, password_hash: str, now: datetime) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(users)
                .where(users.c.user_id == user_id, users.c.enabled.is_(True))
                .values(
                    password_hash=password_hash,
                    credential_version=users.c.credential_version + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise not_found()
            connection.execute(
                update(sessions)
                .where(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    def create_broker_account(
        self,
        context: UserContext,
        create: BrokerAccountCreate,
        idempotency_key: str,
        cipher: EnvelopeCipher,
        now: datetime,
    ) -> BrokerAccountView:
        account_id = str(uuid7())
        reference_id = str(uuid7())
        credential_id = str(uuid7())
        reference_bytes = create.provider_account_reference.encode("utf-8")
        binding = {
            "owner_user_id": context.user_id,
            "account_id": account_id,
            "provider": create.provider,
        }
        reference_envelope = cipher.encrypt(
            reference_bytes, purpose="provider_account_ref", **binding
        )
        credential_envelope = cipher.encrypt(
            create.credential, purpose="provider_credential", **binding
        )
        request_sha256 = _request_hash(
            {
                "capabilities": sorted(set(create.capabilities)),
                "credential_sha256": hashlib.sha256(create.credential).hexdigest(),
                "environment": create.environment.value,
                "provider": create.provider,
                "provider_account_reference": create.provider_account_reference,
            }
        )
        with self._engine.begin() as connection:
            replay = self._begin_idempotent(
                connection,
                context,
                "broker_account.create",
                idempotency_key,
                request_sha256,
                now,
            )
            if replay is not None:
                return replay
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
                    provider_account_ref_last_four=create.provider_account_reference[-4:],
                    environment=create.environment.value,
                    status=BrokerAccountStatus.PENDING_VERIFICATION.value,
                    capabilities=sorted(set(create.capabilities)),
                    credential_envelope_id=credential_id,
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
        context: UserContext,
        account_id: str,
        new_secret: bytes,
        expected_version: int,
        idempotency_key: str,
        cipher: EnvelopeCipher,
        now: datetime,
    ) -> BrokerAccountView:
        request_sha256 = _request_hash(
            {
                "account_id": account_id,
                "credential_sha256": hashlib.sha256(new_secret).hexdigest(),
                "expected_version": expected_version,
            }
        )
        with self._engine.begin() as connection:
            account = self._owned_account_for_update(connection, context, account_id)
            replay = self._begin_idempotent(
                connection,
                context,
                "credential.replace",
                idempotency_key,
                request_sha256,
                now,
            )
            if replay is not None:
                return replay
            current_version = cast(int, account["record_version"])
            if current_version != expected_version:
                raise stale_version(expected_version, current_version)
            current_envelope = (
                connection.execute(
                    select(credential_envelopes)
                    .where(
                        credential_envelopes.c.credential_envelope_id
                        == account["credential_envelope_id"],
                        credential_envelopes.c.owner_user_id == context.user_id,
                        credential_envelopes.c.status == "active",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if current_envelope is None:
                raise AccessError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "Credential is unavailable.",
                    503,
                )
            cipher.verify_available(
                _credential_envelope(current_envelope),
                owner_user_id=context.user_id,
                account_id=account_id,
                provider=cast(str, account["provider"]),
                purpose="provider_credential",
            )
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
            new_id = str(uuid7())
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
            return result

    def revoke_credential(
        self,
        context: UserContext,
        account_id: str,
        expected_version: int,
        idempotency_key: str,
        now: datetime,
    ) -> BrokerAccountView:
        request_sha256 = _request_hash(
            {"account_id": account_id, "expected_version": expected_version}
        )
        with self._engine.begin() as connection:
            account = self._owned_account_for_update(connection, context, account_id)
            replay = self._begin_idempotent(
                connection,
                context,
                "credential.revoke",
                idempotency_key,
                request_sha256,
                now,
            )
            if replay is not None:
                return replay
            current_version = cast(int, account["record_version"])
            if current_version != expected_version:
                raise stale_version(expected_version, current_version)
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

    def audit_not_found(self, context: UserContext, now: datetime) -> None:
        """Audit an opaque denial without persisting the guessed identifier."""
        with self._engine.begin() as connection:
            self._audit(connection, context.user_id, "RESOURCE_NOT_FOUND", None, now)

    @staticmethod
    def _begin_idempotent(
        connection: Connection,
        context: UserContext,
        operation: str,
        idempotency_key: str,
        request_sha256: bytes,
        now: datetime,
    ) -> BrokerAccountView | None:
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
        return _account_view_from_record(stored_result) if stored_result is not None else None

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
            .values(result=_account_view_record(result))
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
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


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
