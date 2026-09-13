from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import uuid7

import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from familytrade.access.credentials import (
    CredentialEnvelope,
    EnvelopeCipher,
    EnvironmentKekProvider,
)
from familytrade.access.models import (
    AccessError,
    BrowserWriteAuthorization,
    ErrorCode,
    UserContext,
    parse_broker_account_create,
)
from familytrade.access.repository import (
    AccessRepository,
    audit_events,
    broker_accounts,
    credential_envelopes,
    idempotency_records,
    sessions,
    users,
    write_authorizations,
)
from familytrade.access.service import AccessService


@dataclass(frozen=True)
class StubKekRing:
    keys: dict[str, bytes]
    active_version: str = "test-v1"

    def key(self, version: str) -> bytes:
        try:
            return self.keys[version]
        except KeyError as exc:
            raise AccessError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Credential key material is unavailable.",
                503,
            ) from exc


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(frozen=True)
class AdminActor(UserContext):
    session_token: str
    csrf_token: str


class FailingVerificationCipher(EnvelopeCipher):
    def verify(self, envelope: CredentialEnvelope, expected: bytes, **binding: str) -> None:
        del envelope, expected, binding
        raise AccessError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, "Credential key material is unavailable.", 503
        )


def make_service(
    engine: Engine,
    cipher: EnvelopeCipher | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AccessService:
    return AccessService(
        AccessRepository(engine),
        cipher or EnvelopeCipher(StubKekRing({"test-v1": b"K" * 32})),
        allowed_origins={"https://familytrade.test"},
        clock=clock or (lambda: datetime(2026, 9, 13, 1, 0, tzinfo=UTC)),
    )


def logged_in(service: AccessService, name: str, scope: str = "lanes:read") -> AdminActor:
    service.invite_user(name, f"test-password-{name}!", {scope}, is_administrator=True)
    login = service.login(name, f"test-password-{name}!")
    context = service.authenticate_browser(login.session_token, request_id=f"request-{name}")
    return AdminActor(
        schema_version=context.schema_version,
        user_id=context.user_id,
        auth_session_id=context.auth_session_id,
        auth_method=context.auth_method,
        scopes=context.scopes,
        authenticated_at=context.authenticated_at,
        expires_at=context.expires_at,
        request_id=context.request_id,
        credential_version=context.credential_version,
        is_administrator=context.is_administrator,
        session_token=login.session_token,
        csrf_token=login.csrf_token,
    )


def authorize(
    service: AccessService, actor: AdminActor, operation: str
) -> BrowserWriteAuthorization:
    return service.authorize_browser_write(
        actor.session_token,
        csrf_token=actor.csrf_token,
        origin="https://familytrade.test",
        request_id=str(uuid7()),
        operation=operation,
    )


def account_payload(reference: str, secret: bytes) -> dict[str, object]:
    return {
        "provider": "synthetic",
        "provider_account_reference": reference,
        "environment": "paper",
        "capabilities": ["market_data"],
        "credential": secret,
    }


def test_two_user_read_write_guess_denial_is_opaque_and_has_no_resource_side_effect(
    postgres_engine: Engine,
) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    bob = logged_in(service, "bob")
    bob_account = service.create_broker_account(
        authorize(service, bob, "broker_account.create"),
        account_payload("BOB-ACCOUNT-5678", b"bob-test-secret"),
        idempotency_key=str(uuid7()),
    )
    before = service.get_broker_account(bob, bob_account.broker_account_id)

    forged = replace(alice, user_id=bob.user_id)
    with pytest.raises(AccessError) as forged_error:
        service.get_broker_account(forged, bob_account.broker_account_id)
    assert forged_error.value.code is ErrorCode.UNAUTHENTICATED

    for operation in (
        lambda: service.get_broker_account(alice, bob_account.broker_account_id),
        lambda: service.get_broker_account(alice, str(uuid7())),
        lambda: service.replace_credential(
            authorize(service, alice, "credential.replace"),
            bob_account.broker_account_id,
            b"malicious-replacement",
            expected_version=1,
            idempotency_key=str(uuid7()),
        ),
        lambda: service.revoke_credential(
            authorize(service, alice, "credential.revoke"),
            bob_account.broker_account_id,
            expected_version=1,
            idempotency_key=str(uuid7()),
        ),
        lambda: service.replace_credential(
            authorize(service, alice, "credential.replace"),
            bob_account.broker_account_id,
            b"",
            expected_version=999,
            idempotency_key="not-a-uuid",
        ),
    ):
        with pytest.raises(AccessError) as denied:
            operation()
        envelope = denied.value.envelope("018-request")
        assert denied.value.http_status == 404
        assert envelope["error"]["code"] == "NOT_FOUND"
        assert "bob" not in json.dumps(envelope).lower()

    after = service.get_broker_account(bob, bob_account.broker_account_id)
    assert after == before
    with postgres_engine.connect() as connection:
        owners = connection.execute(
            select(audit_events.c.actor_user_id, audit_events.c.broker_account_id).where(
                audit_events.c.event_type == "RESOURCE_NOT_FOUND"
            )
        ).all()
    assert owners == [(alice.user_id, None)] * 5


def test_owner_and_server_fields_are_rejected_and_multiple_accounts_stay_independent(
    postgres_engine: Engine,
) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")

    malicious = account_payload("ACCOUNT-1111", b"secret-one")
    malicious["owner_user_id"] = "018-user-b"
    with pytest.raises(AccessError) as invalid_owner:
        service.create_broker_account(
            authorize(service, alice, "broker_account.create"),
            malicious,
            idempotency_key=str(uuid7()),
        )
    assert invalid_owner.value.code is ErrorCode.VALIDATION_ERROR

    account_a = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload("ACCOUNT-1111", b"secret-one"),
        idempotency_key=str(uuid7()),
    )
    account_b = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload("ACCOUNT-2222", b"secret-two"),
        idempotency_key=str(uuid7()),
    )
    accounts = service.list_broker_accounts(alice)
    assert {item.broker_account_id for item in accounts} == {
        account_a.broker_account_id,
        account_b.broker_account_id,
    }
    assert account_a.broker_account_id != account_b.broker_account_id
    assert account_a.provider_account_reference == "****1111"
    assert account_b.provider_account_reference == "****2222"
    assert not hasattr(account_a, "lane_id")
    assert not hasattr(account_a, "position")


def test_secret_is_encrypted_redacted_and_never_returned_or_logged(postgres_engine: Engine) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    secret = b"super-sensitive-test-credential"
    payload = account_payload("PRIVATE-REFERENCE-9876", secret)
    account = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        payload,
        idempotency_key=str(uuid7()),
    )

    public = json.dumps(asdict(account), default=str)
    assert secret.decode() not in public
    assert "PRIVATE-REFERENCE" not in public
    assert not hasattr(account, "credential")

    repository = AccessRepository(postgres_engine)
    rows = repository.envelope_rows_for_test(account.broker_account_id)
    assert len(rows) == 2
    assert all(secret not in bytes(row["ciphertext"]) for row in rows)
    assert secret.decode() not in repr(rows)
    parsed_payload = parse_broker_account_create(payload)
    assert secret.decode() not in repr(parsed_payload)
    assert "PRIVATE-REFERENCE-9876" not in repr(parsed_payload)

    with postgres_engine.connect() as connection:
        stored_ref = connection.scalar(
            select(broker_accounts.c.provider_account_ref_last_four).where(
                broker_accounts.c.broker_account_id == account.broker_account_id
            )
        )
    assert stored_ref == "9876"


def test_missing_wrong_and_binding_mismatched_kek_are_safe(postgres_engine: Engine) -> None:
    environment_secret = "unrelated-environment-test-secret"
    encoded_kek = "S0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0s="
    provider = EnvironmentKekProvider(
        {
            "FAMILYTRADE_CREDENTIAL_KEK_V1": encoded_kek,
            "UNRELATED_SECRET": environment_secret,
        }
    )
    assert encoded_kek not in repr(provider)
    assert environment_secret not in repr(provider)

    with pytest.raises(AccessError) as missing:
        EnvelopeCipher(EnvironmentKekProvider({})).encrypt(
            b"test-secret",
            owner_user_id="user-a",
            account_id="account-a",
            provider="synthetic",
            purpose="provider_credential",
        )
    assert missing.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert "test-secret" not in str(missing.value)

    correct = EnvelopeCipher(StubKekRing({"test-v1": b"K" * 32}))
    envelope = correct.encrypt(
        b"test-secret",
        owner_user_id="user-a",
        account_id="account-a",
        provider="synthetic",
        purpose="provider_credential",
    )
    for cipher, owner in (
        (EnvelopeCipher(StubKekRing({"test-v1": b"W" * 32})), "user-a"),
        (correct, "user-b"),
    ):
        with pytest.raises(AccessError) as unavailable:
            cipher.verify(
                envelope,
                b"test-secret",
                owner_user_id=owner,
                account_id="account-a",
                provider="synthetic",
                purpose="provider_credential",
            )
        assert unavailable.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
        assert unavailable.value.details == {}


def test_replacement_is_atomic_and_revocation_disables_account(postgres_engine: Engine) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    account = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload("ACCOUNT-1111", b"old-test-secret"),
        idempotency_key=str(uuid7()),
    )
    repository = AccessRepository(postgres_engine)
    before = repository.envelope_rows_for_test(account.broker_account_id)
    old_credential_id = next(
        row["credential_envelope_id"] for row in before if row["purpose"] == "provider_credential"
    )

    failing = make_service(
        postgres_engine,
        FailingVerificationCipher(StubKekRing({"test-v1": b"K" * 32})),
    )
    with pytest.raises(AccessError):
        failing.replace_credential(
            authorize(failing, alice, "credential.replace"),
            account.broker_account_id,
            b"failed-new-secret",
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    failed_rows = repository.envelope_rows_for_test(account.broker_account_id)
    assert [(row["credential_envelope_id"], row["status"]) for row in failed_rows] == [
        (row["credential_envelope_id"], row["status"]) for row in before
    ]
    assert service.get_broker_account(alice, account.broker_account_id).record_version == 1

    rotated_without_old_key = make_service(
        postgres_engine,
        EnvelopeCipher(StubKekRing({"test-v2": b"N" * 32}, active_version="test-v2")),
    )
    replaced = rotated_without_old_key.replace_credential(
        authorize(rotated_without_old_key, alice, "credential.replace"),
        account.broker_account_id,
        b"successful-new-secret",
        expected_version=1,
        idempotency_key=str(uuid7()),
    )
    assert replaced.record_version == 2
    assert replaced.status.value == "reauth_required"
    rows = repository.envelope_rows_for_test(account.broker_account_id)
    old_row = next(row for row in rows if row["credential_envelope_id"] == old_credential_id)
    assert old_row["status"] == "revoked"
    active_credentials = [
        row for row in rows if row["purpose"] == "provider_credential" and row["status"] == "active"
    ]
    assert len(active_credentials) == 1
    assert active_credentials[0]["key_version"] == "test-v2"

    with pytest.raises(AccessError) as stale:
        service.revoke_credential(
            authorize(service, alice, "credential.revoke"),
            account.broker_account_id,
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    assert stale.value.code is ErrorCode.STALE_VERSION

    revoked = service.revoke_credential(
        authorize(service, alice, "credential.revoke"),
        account.broker_account_id,
        expected_version=2,
        idempotency_key=str(uuid7()),
    )
    assert revoked.status.value == "disabled"
    assert revoked.credential_status == "revoked"
    assert revoked.record_version == 3
    with postgres_engine.connect() as connection:
        active_count = connection.scalar(
            select(func.count())
            .select_from(credential_envelopes)
            .where(
                credential_envelopes.c.broker_account_id == account.broker_account_id,
                credential_envelopes.c.purpose == "provider_credential",
                credential_envelopes.c.status == "active",
            )
        )
    assert active_count == 0
    with pytest.raises(AccessError) as unavailable:
        service.replace_credential(
            authorize(service, alice, "credential.replace"),
            account.broker_account_id,
            b"must-not-reenable",
            expected_version=3,
            idempotency_key=str(uuid7()),
        )
    assert unavailable.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert service.get_broker_account(alice, account.broker_account_id) == revoked


def test_mutations_are_idempotent_and_key_reuse_with_different_bytes_conflicts(
    postgres_engine: Engine,
) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    create_key = str(uuid7())
    payload = account_payload("ACCOUNT-1111", b"old-test-secret")

    create_authorization = authorize(service, alice, "broker_account.create")
    created = service.create_broker_account(
        create_authorization, payload, idempotency_key=create_key
    )
    with pytest.raises(AccessError) as capability_reuse:
        service.create_broker_account(create_authorization, payload, idempotency_key=create_key)
    assert capability_reuse.value.code is ErrorCode.INVALID_CSRF
    service_without_kek = make_service(
        postgres_engine,
        EnvelopeCipher(StubKekRing({}, active_version="missing-version")),
    )
    replayed_create = service_without_kek.create_broker_account(
        authorize(service_without_kek, alice, "broker_account.create"),
        payload,
        idempotency_key=create_key,
    )
    assert replayed_create == created
    assert len(service.list_broker_accounts(alice)) == 1

    changed_payload = account_payload("ACCOUNT-2222", b"other-test-secret")
    with pytest.raises(AccessError) as conflict:
        service.create_broker_account(
            authorize(service, alice, "broker_account.create"),
            changed_payload,
            idempotency_key=create_key,
        )
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

    replace_key = str(uuid7())
    replaced = service.replace_credential(
        authorize(service, alice, "credential.replace"),
        created.broker_account_id,
        b"new-test-secret",
        expected_version=1,
        idempotency_key=replace_key,
    )
    replayed_replace = service.replace_credential(
        authorize(service, alice, "credential.replace"),
        created.broker_account_id,
        b"new-test-secret",
        expected_version=1,
        idempotency_key=replace_key,
    )
    assert replayed_replace == replaced
    assert replayed_replace.record_version == 2

    revoked = service.revoke_credential(
        authorize(service, alice, "credential.revoke"),
        created.broker_account_id,
        expected_version=2,
        idempotency_key=str(uuid7()),
    )
    assert revoked.status.value == "disabled"
    replayed_after_revoke = service.replace_credential(
        authorize(service, alice, "credential.replace"),
        created.broker_account_id,
        b"new-test-secret",
        expected_version=1,
        idempotency_key=replace_key,
    )
    assert replayed_after_revoke == replaced
    bob = logged_in(service, "bob")
    with pytest.raises(AccessError) as cross_owner:
        service.replace_credential(
            authorize(service, bob, "credential.replace"),
            created.broker_account_id,
            b"new-test-secret",
            expected_version=1,
            idempotency_key=replace_key,
        )
    assert cross_owner.value.code is ErrorCode.NOT_FOUND

    with pytest.raises(AccessError) as invalid:
        service.revoke_credential(
            authorize(service, alice, "credential.revoke"),
            created.broker_account_id,
            expected_version=2,
            idempotency_key="not-a-uuid",
        )
    assert invalid.value.code is ErrorCode.VALIDATION_ERROR


def test_concurrent_create_retry_commits_one_account(postgres_engine: Engine) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    idempotency_key = str(uuid7())
    payload = account_payload("ACCOUNT-1111", b"concurrent-test-secret")
    authorizations = [authorize(service, alice, "broker_account.create") for _ in range(2)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.create_broker_account,
                authorization,
                payload,
                idempotency_key=idempotency_key,
            )
            for authorization in authorizations
        ]
        results = [future.result(timeout=10) for future in futures]

    assert results[0] == results[1]
    assert len(service.list_broker_accounts(alice)) == 1


def test_idempotency_hash_normalizes_unicode_but_not_secret_bytes(postgres_engine: Engine) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    key = str(uuid7())
    composed = "CAF\u00c9-1111"
    decomposed = "CAFE\u0301-1111"
    created = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload(composed, b"same-secret-bytes"),
        idempotency_key=key,
    )
    replayed = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload(decomposed, b"same-secret-bytes"),
        idempotency_key=key,
    )
    assert replayed == created
    with pytest.raises(AccessError) as different_normalized_value:
        service.create_broker_account(
            authorize(service, alice, "broker_account.create"),
            account_payload("CAFE\u0301-2222", b"same-secret-bytes"),
            idempotency_key=key,
        )
    assert different_normalized_value.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(AccessError) as different_bytes:
        service.create_broker_account(
            authorize(service, alice, "broker_account.create"),
            account_payload(decomposed, "same-secret-bytes".encode("utf-16")),
            idempotency_key=key,
        )
    assert different_bytes.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_database_clock_controls_mutation_projection_despite_app_clock_skew(
    postgres_engine: Engine,
) -> None:
    clock = MutableClock(datetime(2099, 9, 13, 1, 0, tzinfo=UTC))
    service = make_service(postgres_engine, clock=clock)
    alice = logged_in(service, "alice")
    with postgres_engine.connect() as connection:
        before = connection.scalar(select(func.clock_timestamp()))
    account = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload("ACCOUNT-1111", b"database-clock-secret"),
        idempotency_key=str(uuid7()),
    )
    with postgres_engine.connect() as connection:
        after = connection.scalar(select(func.clock_timestamp()))
    assert before is not None and after is not None
    assert before <= account.created_at <= after
    assert account.updated_at == account.created_at
    assert account.created_at.year != 2099


def test_failed_idempotent_outcomes_survive_rollback_and_dependency_recovery(
    postgres_engine: Engine,
) -> None:
    healthy = make_service(postgres_engine)
    alice = logged_in(healthy, "alice")
    unavailable = make_service(
        postgres_engine,
        EnvelopeCipher(StubKekRing({}, active_version="missing-version")),
    )
    create_key = str(uuid7())
    create_payload = account_payload("PRIVATE-FAILURE-1111", b"create-failure-secret")
    with pytest.raises(AccessError) as first_create:
        unavailable.create_broker_account(
            authorize(unavailable, alice, "broker_account.create"),
            create_payload,
            idempotency_key=create_key,
        )
    with pytest.raises(AccessError) as changed_create:
        healthy.create_broker_account(
            authorize(healthy, alice, "broker_account.create"),
            account_payload("ACTUALLY-DIFFERENT-9999", b"create-failure-secret"),
            idempotency_key=create_key,
        )
    assert changed_create.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    with pytest.raises(AccessError) as replayed_create:
        healthy.create_broker_account(
            authorize(healthy, alice, "broker_account.create"),
            create_payload,
            idempotency_key=create_key,
        )
    assert first_create.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert replayed_create.value.envelope("replay") == first_create.value.envelope("replay")
    assert healthy.list_broker_accounts(alice) == ()

    account = healthy.create_broker_account(
        authorize(healthy, alice, "broker_account.create"),
        account_payload("ACCOUNT-2222", b"original-secret"),
        idempotency_key=str(uuid7()),
    )
    failing_replace = make_service(
        postgres_engine,
        FailingVerificationCipher(StubKekRing({"test-v1": b"K" * 32})),
    )
    replace_key = str(uuid7())
    with pytest.raises(AccessError) as first_replace:
        failing_replace.replace_credential(
            authorize(failing_replace, alice, "credential.replace"),
            account.broker_account_id,
            b"replacement-failure-secret",
            expected_version=1,
            idempotency_key=replace_key,
        )
    with pytest.raises(AccessError) as replayed_replace:
        healthy.replace_credential(
            authorize(healthy, alice, "credential.replace"),
            account.broker_account_id,
            b"replacement-failure-secret",
            expected_version=1,
            idempotency_key=replace_key,
        )
    assert first_replace.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert replayed_replace.value.envelope("replay") == first_replace.value.envelope("replay")
    assert healthy.get_broker_account(alice, account.broker_account_id).record_version == 1

    revoke_key = str(uuid7())
    with pytest.raises(AccessError) as first_revoke:
        healthy.revoke_credential(
            authorize(healthy, alice, "credential.revoke"),
            account.broker_account_id,
            expected_version=2,
            idempotency_key=revoke_key,
        )
    healthy.replace_credential(
        authorize(healthy, alice, "credential.replace"),
        account.broker_account_id,
        b"successful-replacement",
        expected_version=1,
        idempotency_key=str(uuid7()),
    )
    with pytest.raises(AccessError) as replayed_revoke:
        healthy.revoke_credential(
            authorize(healthy, alice, "credential.revoke"),
            account.broker_account_id,
            expected_version=2,
            idempotency_key=revoke_key,
        )
    assert first_revoke.value.code is ErrorCode.STALE_VERSION
    assert replayed_revoke.value.envelope("replay") == first_revoke.value.envelope("replay")

    with postgres_engine.connect() as connection:
        stored = connection.execute(
            select(idempotency_records.c.result).where(
                idempotency_records.c.owner_user_id == alice.user_id,
                idempotency_records.c.result["outcome"].astext == "error",
            )
        ).scalars()
        serialized = json.dumps(list(stored))
    for forbidden in (
        "PRIVATE-FAILURE-1111",
        "create-failure-secret",
        "replacement-failure-secret",
        "synthetic",
    ):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("expiry_kind", "expected_code"),
    [
        ("authorization", ErrorCode.INVALID_CSRF),
        ("session", ErrorCode.UNAUTHENTICATED),
    ],
)
def test_expiry_while_waiting_for_mutation_locks_cannot_commit(
    postgres_engine: Engine,
    expiry_kind: str,
    expected_code: ErrorCode,
) -> None:
    clock = MutableClock(datetime(2099, 9, 13, 1, 0, tzinfo=UTC))
    service = make_service(postgres_engine, clock=clock)
    alice = logged_in(service, "alice")
    authorization = authorize(service, alice, "broker_account.create")
    query_entered = Event()

    def mutation_query_started(*args: object) -> None:
        statement = str(args[2])
        if "access_write_authorizations" in statement and "FOR UPDATE" in statement:
            query_entered.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with postgres_engine.begin() as blocker:
            if expiry_kind == "authorization":
                blocker.execute(
                    update(write_authorizations)
                    .where(
                        write_authorizations.c.authorization_hash
                        == sha256(authorization.authorization_token.encode()).digest()
                    )
                    .values(expires_at=func.clock_timestamp() - text("interval '1 second'"))
                )
                blocker.execute(
                    select(write_authorizations)
                    .where(
                        write_authorizations.c.authorization_hash
                        == sha256(authorization.authorization_token.encode()).digest()
                    )
                    .with_for_update()
                )
            else:
                blocker.execute(
                    select(users).where(users.c.user_id == alice.user_id).with_for_update()
                )
                blocker.execute(
                    update(sessions)
                    .where(sessions.c.auth_session_id == alice.auth_session_id)
                    .values(
                        authenticated_at=func.clock_timestamp() - text("interval '2 hours'"),
                        idle_expires_at=func.clock_timestamp() - text("interval '1 second'"),
                    )
                )
            event.listen(postgres_engine, "before_cursor_execute", mutation_query_started)
            try:
                future = executor.submit(
                    service.create_broker_account,
                    authorization,
                    account_payload("ACCOUNT-1111", b"must-expire-before-commit"),
                    idempotency_key=str(uuid7()),
                )
                assert query_entered.wait(timeout=5)
            finally:
                event.remove(postgres_engine, "before_cursor_execute", mutation_query_started)
        with pytest.raises(AccessError) as expired:
            future.result(timeout=10)
    assert expired.value.code is expected_code
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(broker_accounts)) == 0


def test_malformed_and_oversized_secrets_never_escape_in_errors_or_repr(
    postgres_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    marker = "DO-NOT-LEAK-SECRET-MARKER"
    for credential in ((marker * 3000).encode(), marker):
        with pytest.raises(AccessError) as rejected:
            service.create_broker_account(
                authorize(service, alice, "broker_account.create"),
                account_payload("ACCOUNT-1111", credential),  # type: ignore[arg-type]
                idempotency_key=str(uuid7()),
            )
        rendered = " ".join(
            (
                str(rejected.value),
                repr(rejected.value),
                json.dumps(rejected.value.envelope("request-safe")),
                caplog.text,
            )
        )
        assert rejected.value.code is ErrorCode.VALIDATION_ERROR
        assert marker not in rendered


def test_non_administrator_is_denied_every_account_operation(postgres_engine: Engine) -> None:
    service = make_service(postgres_engine)
    service.invite_user("reader", "test-password-reader!", {"lanes:read"})
    login = service.login("reader", "test-password-reader!")
    context = service.authenticate_browser(login.session_token, request_id="reader-request")
    authorization = service.authorize_browser_write(
        login.session_token,
        csrf_token=login.csrf_token,
        origin="https://familytrade.test",
        request_id="reader-write",
        operation="broker_account.create",
    )
    with pytest.raises(AccessError) as write_denied:
        service.create_broker_account(
            authorization,
            account_payload("ACCOUNT-1111", b"reader-test-secret"),
            idempotency_key=str(uuid7()),
        )
    with pytest.raises(AccessError) as read_denied:
        service.list_broker_accounts(context)
    assert write_denied.value.code is ErrorCode.INSUFFICIENT_SCOPE
    assert write_denied.value.http_status == 403
    assert read_denied.value.code is ErrorCode.INSUFFICIENT_SCOPE
    assert read_denied.value.http_status == 403
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(broker_accounts)) == 0


def test_versioned_rewrap_and_wrong_or_missing_old_key_are_atomic(
    postgres_engine: Engine,
) -> None:
    old_key = b"K" * 32
    new_key = b"N" * 32
    service = make_service(postgres_engine, EnvelopeCipher(StubKekRing({"test-v1": old_key})))
    alice = logged_in(service, "alice")
    account = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload("ACCOUNT-1111", b"rewrap-test-secret"),
        idempotency_key=str(uuid7()),
    )
    repository = AccessRepository(postgres_engine)
    before = repository.envelope_rows_for_test(account.broker_account_id)
    before_state = [(row["key_version"], row["wrapped_dek"]) for row in before]

    missing_old = make_service(
        postgres_engine,
        EnvelopeCipher(StubKekRing({"test-v2": new_key}, active_version="test-v2")),
    )
    missing_key = str(uuid7())
    with pytest.raises(AccessError) as missing:
        missing_old.rewrap_credentials(
            authorize(missing_old, alice, "credential.rewrap"),
            account.broker_account_id,
            expected_version=1,
            idempotency_key=missing_key,
        )
    assert missing.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert [
        (row["key_version"], row["wrapped_dek"])
        for row in repository.envelope_rows_for_test(account.broker_account_id)
    ] == before_state

    wrong_old = make_service(
        postgres_engine,
        EnvelopeCipher(
            StubKekRing({"test-v1": b"W" * 32, "test-v2": new_key}, active_version="test-v2")
        ),
    )
    with pytest.raises(AccessError) as wrong:
        wrong_old.rewrap_credentials(
            authorize(wrong_old, alice, "credential.rewrap"),
            account.broker_account_id,
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    assert wrong.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert [
        (row["key_version"], row["wrapped_dek"])
        for row in repository.envelope_rows_for_test(account.broker_account_id)
    ] == before_state

    rotating = make_service(
        postgres_engine,
        EnvelopeCipher(
            StubKekRing({"test-v1": old_key, "test-v2": new_key}, active_version="test-v2")
        ),
    )
    with pytest.raises(AccessError) as restored_replay:
        rotating.rewrap_credentials(
            authorize(rotating, alice, "credential.rewrap"),
            account.broker_account_id,
            expected_version=1,
            idempotency_key=missing_key,
        )
    assert restored_replay.value.envelope("replay") == missing.value.envelope("replay")
    rotated = rotating.rewrap_credentials(
        authorize(rotating, alice, "credential.rewrap"),
        account.broker_account_id,
        expected_version=1,
        idempotency_key=str(uuid7()),
    )
    after = repository.envelope_rows_for_test(account.broker_account_id)
    assert rotated.record_version == 2
    assert {row["key_version"] for row in after} == {"test-v2"}
    assert [row["ciphertext"] for row in after] == [row["ciphertext"] for row in before]


def test_database_rejects_orphan_cross_owner_shared_and_second_active_envelopes(
    postgres_engine: Engine,
) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    bob = logged_in(service, "bob")
    first = service.create_broker_account(
        authorize(service, alice, "broker_account.create"),
        account_payload("ACCOUNT-1111", b"alice-test-secret"),
        idempotency_key=str(uuid7()),
    )
    second = service.create_broker_account(
        authorize(service, bob, "broker_account.create"),
        account_payload("ACCOUNT-2222", b"bob-test-secret"),
        idempotency_key=str(uuid7()),
    )
    rows = AccessRepository(postgres_engine).envelope_rows_for_test(first.broker_account_id)
    credential = next(row for row in rows if row["purpose"] == "provider_credential")

    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            update(credential_envelopes)
            .where(
                credential_envelopes.c.credential_envelope_id
                == credential["credential_envelope_id"]
            )
            .values(owner_user_id=bob.user_id)
        )
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(
            update(broker_accounts)
            .where(broker_accounts.c.broker_account_id == second.broker_account_id)
            .values(credential_envelope_id=credential["credential_envelope_id"])
        )
    duplicate = dict(credential)
    duplicate["credential_envelope_id"] = str(uuid7())
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(insert(credential_envelopes).values(**duplicate))
    orphan = dict(credential)
    orphan["credential_envelope_id"] = str(uuid7())
    orphan["broker_account_id"] = str(uuid7())
    with pytest.raises(IntegrityError), postgres_engine.begin() as connection:
        connection.execute(insert(credential_envelopes).values(**orphan))


def test_concurrent_disable_revalidation_prevents_mutation_commit(postgres_engine: Engine) -> None:
    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    authorization = authorize(service, alice, "broker_account.create")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with postgres_engine.begin() as disabling:
            disabling.execute(
                update(users)
                .where(users.c.user_id == alice.user_id)
                .values(enabled=False, credential_version=users.c.credential_version + 1)
            )
            disabling.execute(
                update(sessions)
                .where(sessions.c.user_id == alice.user_id, sessions.c.revoked_at.is_(None))
                .values(revoked_at=datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
            )
            future = executor.submit(
                service.create_broker_account,
                authorization,
                account_payload("ACCOUNT-1111", b"must-not-commit"),
                idempotency_key=str(uuid7()),
            )
        with pytest.raises(AccessError) as denied:
            future.result(timeout=10)
    assert denied.value.code is ErrorCode.UNAUTHENTICATED
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(broker_accounts)) == 0

    bob = logged_in(service, "bob")
    revocation_authorization = authorize(service, bob, "broker_account.create")
    with ThreadPoolExecutor(max_workers=1) as executor:
        with postgres_engine.begin() as revoking:
            revoking.execute(
                update(sessions)
                .where(sessions.c.auth_session_id == bob.auth_session_id)
                .values(revoked_at=datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
            )
            future = executor.submit(
                service.create_broker_account,
                revocation_authorization,
                account_payload("ACCOUNT-2222", b"also-must-not-commit"),
                idempotency_key=str(uuid7()),
            )
        with pytest.raises(AccessError) as revoked:
            future.result(timeout=10)
    assert revoked.value.code is ErrorCode.UNAUTHENTICATED
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(broker_accounts)) == 0


def test_frozen_access_fixture_expected_values(postgres_engine: Engine) -> None:
    fixture_path = Path(__file__).parents[2] / "docs" / "contracts-examples-v1.json"
    fixture = next(
        case
        for case in json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
        if case["id"] == "access_two_user_denial"
    )
    assert fixture["expected"] == {
        "http_status": 404,
        "error_code": "NOT_FOUND",
        "owner_disclosed": False,
        "audit_actor": "018-user-a",
        "side_effect_count": 0,
        "rationale": "Identity comes from authentication; the opaque ID neither grants access nor reveals another user's resource.",
    }

    service = make_service(postgres_engine)
    alice = logged_in(service, "alice")
    bob = logged_in(service, "bob")
    account = service.create_broker_account(
        authorize(service, bob, "broker_account.create"),
        account_payload("ACCOUNT-2222", b"bob-test-secret"),
        idempotency_key=str(uuid7()),
    )
    with pytest.raises(AccessError) as denied:
        service.get_broker_account(alice, account.broker_account_id)
    assert denied.value.http_status == fixture["expected"]["http_status"]
    assert denied.value.code.value == fixture["expected"]["error_code"]
    with postgres_engine.connect() as connection:
        actor = connection.scalar(
            select(audit_events.c.actor_user_id).where(
                audit_events.c.event_type == "RESOURCE_NOT_FOUND"
            )
        )
    assert actor == alice.user_id
