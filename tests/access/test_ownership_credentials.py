from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid7

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from familytrade.access.credentials import (
    CredentialEnvelope,
    EnvelopeCipher,
    EnvironmentKekProvider,
)
from familytrade.access.models import AccessError, BrokerAccountCreate, ErrorCode
from familytrade.access.repository import (
    AccessRepository,
    audit_events,
    broker_accounts,
    credential_envelopes,
)
from familytrade.access.service import AccessService


@dataclass(frozen=True)
class StubKek:
    raw: bytes
    version: str = "test-v1"

    def key(self) -> bytes:
        return self.raw


class FailingVerificationCipher(EnvelopeCipher):
    def verify(self, envelope: CredentialEnvelope, expected: bytes, **binding: str) -> None:
        del envelope, expected, binding
        raise AccessError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, "Credential key material is unavailable.", 503
        )


def make_service(engine: Engine, cipher: EnvelopeCipher | None = None) -> AccessService:
    return AccessService(
        AccessRepository(engine),
        cipher or EnvelopeCipher(StubKek(b"K" * 32)),
        allowed_origins={"https://familytrade.test"},
        clock=lambda: datetime(2026, 9, 13, 1, 0, tzinfo=UTC),
    )


def logged_in(service: AccessService, name: str, scope: str = "lanes:read") -> Any:
    service.invite_user(name, f"test-password-{name}!", {scope})
    login = service.login(name, f"test-password-{name}!")
    return service.authorize_browser_write(
        login.session_token,
        csrf_token=login.csrf_token,
        origin="https://familytrade.test",
        request_id=f"request-{name}",
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
        bob,
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
            alice,
            bob_account.broker_account_id,
            b"malicious-replacement",
            expected_version=1,
            idempotency_key=str(uuid7()),
        ),
        lambda: service.revoke_credential(
            alice,
            bob_account.broker_account_id,
            expected_version=1,
            idempotency_key=str(uuid7()),
        ),
        lambda: service.replace_credential(
            alice,
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
    with pytest.raises(ValidationError):
        service.create_broker_account(alice, malicious, idempotency_key=str(uuid7()))

    account_a = service.create_broker_account(
        alice,
        account_payload("ACCOUNT-1111", b"secret-one"),
        idempotency_key=str(uuid7()),
    )
    account_b = service.create_broker_account(
        alice,
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
    account = service.create_broker_account(alice, payload, idempotency_key=str(uuid7()))

    public = json.dumps(asdict(account), default=str)
    assert secret.decode() not in public
    assert "PRIVATE-REFERENCE" not in public
    assert not hasattr(account, "credential")

    repository = AccessRepository(postgres_engine)
    rows = repository.envelope_rows_for_test(account.broker_account_id)
    assert len(rows) == 2
    assert all(secret not in bytes(row["ciphertext"]) for row in rows)
    assert secret.decode() not in repr(rows)
    parsed_payload = BrokerAccountCreate.model_validate(payload)
    assert secret.decode() not in repr(parsed_payload)

    with postgres_engine.connect() as connection:
        stored_ref = connection.scalar(
            select(broker_accounts.c.provider_account_ref_last_four).where(
                broker_accounts.c.broker_account_id == account.broker_account_id
            )
        )
    assert stored_ref == "9876"


def test_missing_wrong_and_binding_mismatched_kek_are_safe(postgres_engine: Engine) -> None:
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

    correct = EnvelopeCipher(StubKek(b"K" * 32))
    envelope = correct.encrypt(
        b"test-secret",
        owner_user_id="user-a",
        account_id="account-a",
        provider="synthetic",
        purpose="provider_credential",
    )
    for cipher, owner in (
        (EnvelopeCipher(StubKek(b"W" * 32)), "user-a"),
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
        alice,
        account_payload("ACCOUNT-1111", b"old-test-secret"),
        idempotency_key=str(uuid7()),
    )
    repository = AccessRepository(postgres_engine)
    before = repository.envelope_rows_for_test(account.broker_account_id)
    old_credential_id = next(
        row["credential_envelope_id"] for row in before if row["purpose"] == "provider_credential"
    )

    failing = make_service(postgres_engine, FailingVerificationCipher(StubKek(b"K" * 32)))
    with pytest.raises(AccessError):
        failing.replace_credential(
            alice,
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

    wrong_kek = make_service(postgres_engine, EnvelopeCipher(StubKek(b"W" * 32)))
    with pytest.raises(AccessError) as wrong:
        wrong_kek.replace_credential(
            alice,
            account.broker_account_id,
            b"wrong-kek-secret",
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    assert wrong.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert service.get_broker_account(alice, account.broker_account_id).record_version == 1

    replaced = service.replace_credential(
        alice,
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

    with pytest.raises(AccessError) as stale:
        service.revoke_credential(
            alice,
            account.broker_account_id,
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    assert stale.value.code is ErrorCode.STALE_VERSION

    revoked = service.revoke_credential(
        alice,
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
            alice,
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

    created = service.create_broker_account(alice, payload, idempotency_key=create_key)
    replayed_create = service.create_broker_account(alice, payload, idempotency_key=create_key)
    assert replayed_create == created
    assert len(service.list_broker_accounts(alice)) == 1

    changed_payload = account_payload("ACCOUNT-2222", b"other-test-secret")
    with pytest.raises(AccessError) as conflict:
        service.create_broker_account(alice, changed_payload, idempotency_key=create_key)
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

    replace_key = str(uuid7())
    replaced = service.replace_credential(
        alice,
        created.broker_account_id,
        b"new-test-secret",
        expected_version=1,
        idempotency_key=replace_key,
    )
    replayed_replace = service.replace_credential(
        alice,
        created.broker_account_id,
        b"new-test-secret",
        expected_version=1,
        idempotency_key=replace_key,
    )
    assert replayed_replace == replaced
    assert replayed_replace.record_version == 2

    with pytest.raises(AccessError) as invalid:
        service.revoke_credential(
            alice,
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

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                service.create_broker_account,
                alice,
                payload,
                idempotency_key=idempotency_key,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert results[0] == results[1]
    assert len(service.list_broker_accounts(alice)) == 1


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
        bob,
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
