from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
from sqlalchemy import func, text, update
from sqlalchemy.engine import Engine

from familytrade.access.credentials import EnvelopeCipher
from familytrade.access.models import AccessError, BrowserWriteAuthorization, ErrorCode
from familytrade.access.repository import AccessRepository, sessions
from familytrade.access.service import ABSOLUTE_TIMEOUT, IDLE_TIMEOUT, AccessService


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


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


def service_for(engine: Engine, clock: MutableClock) -> AccessService:
    return AccessService(
        AccessRepository(engine),
        EnvelopeCipher(StubKekRing({"test-v1": b"K" * 32})),
        allowed_origins={"https://familytrade.test"},
        clock=clock,
    )


def authorize(
    service: AccessService,
    session_token: str,
    csrf_token: str,
    operation: str,
    *,
    request_id: str = "write-request",
) -> BrowserWriteAuthorization:
    return service.authorize_browser_write(
        session_token,
        csrf_token=csrf_token,
        origin="https://familytrade.test",
        request_id=request_id,
        operation=operation,
    )


def test_invited_login_derives_context_and_cookie_contract(postgres_engine: Engine) -> None:
    clock = MutableClock(datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    user_id = service.invite_user(
        " Alice@Example.test ",
        "test-password-A!",
        {"lanes:read"},
        is_administrator=True,
    )
    login = service.login("alice@example.test", "test-password-A!")
    context = service.authenticate_browser(
        login.session_token, request_id="018-request", required_scope="lanes:read"
    )

    assert context.user_id == user_id
    assert context.auth_method == "browser_session"
    assert context.scopes == ("lanes:read",)
    assert context.is_administrator is True
    assert context.auth_session_id not in login.session_token
    assert (
        IDLE_TIMEOUT
        <= context.expires_at - context.authenticated_at
        < IDLE_TIMEOUT + timedelta(seconds=5)
    )
    assert login.expires_at - context.authenticated_at == ABSOLUTE_TIMEOUT
    assert service.cookie_settings.secure is True
    assert service.cookie_settings.httponly is True
    assert service.cookie_settings.samesite == "lax"
    assert service.cookie_settings.path == "/"
    assert service.cookie_settings.domain is None
    assert login.session_token not in repr(login)
    assert login.csrf_token not in repr(login)


def test_unknown_wrong_and_disabled_users_share_safe_failure(postgres_engine: Engine) -> None:
    clock = MutableClock(datetime(2000, 1, 1, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    user_id = service.invite_user(
        "alice", "test-password-A!", {"lanes:read"}, is_administrator=True
    )

    for username, password in (("missing", "wrong-password!"), ("alice", "wrong-password!")):
        with pytest.raises(AccessError) as captured:
            service.login(username, password)
        assert captured.value.envelope("r")["error"] == {
            "code": "UNAUTHENTICATED",
            "message": "Authentication is required.",
            "retryable": False,
            "details": {},
        }
    admin_login = service.login("alice", "test-password-A!")
    disable_authorization = authorize(
        service, admin_login.session_token, admin_login.csrf_token, "user.disable"
    )
    service.disable_user(
        disable_authorization,
        user_id,
        expected_version=1,
        idempotency_key=str(uuid7()),
    )
    with pytest.raises(AccessError, match="Authentication is required"):
        service.login("alice", "test-password-A!")


def test_idle_absolute_revoked_and_password_rotated_sessions_are_rejected(
    postgres_engine: Engine,
) -> None:
    clock = MutableClock(datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    user_id = service.invite_user(
        "alice", "test-password-A!", {"lanes:read"}, is_administrator=True
    )

    idle_login = service.login("alice", "test-password-A!")
    idle_context = service.authenticate_browser(idle_login.session_token, request_id="initial-idle")
    with postgres_engine.begin() as connection:
        connection.execute(
            update(sessions)
            .where(sessions.c.auth_session_id == idle_context.auth_session_id)
            .values(
                authenticated_at=func.clock_timestamp() - text("interval '2 hours'"),
                idle_expires_at=func.clock_timestamp() - text("interval '1 second'"),
            )
        )
    with pytest.raises(AccessError) as idle_error:
        service.authenticate_browser(idle_login.session_token, request_id="idle")
    assert idle_error.value.code is ErrorCode.UNAUTHENTICATED

    absolute_login = service.login("alice", "test-password-A!")
    absolute_context = service.authenticate_browser(
        absolute_login.session_token, request_id="initial-absolute"
    )
    with postgres_engine.begin() as connection:
        connection.execute(
            update(sessions)
            .where(sessions.c.auth_session_id == absolute_context.auth_session_id)
            .values(
                authenticated_at=func.clock_timestamp() - text("interval '2 hours'"),
                absolute_expires_at=func.clock_timestamp() - text("interval '1 second'"),
            )
        )
    with pytest.raises(AccessError):
        service.authenticate_browser(absolute_login.session_token, request_id="absolute")

    revoked_login = service.login("alice", "test-password-A!")
    logout_authorization = authorize(
        service, revoked_login.session_token, revoked_login.csrf_token, "session.logout"
    )
    service.logout(logout_authorization)
    with pytest.raises(AccessError):
        service.authenticate_browser(revoked_login.session_token, request_id="revoked")
    with pytest.raises(AccessError) as reused:
        service.logout(logout_authorization)
    assert reused.value.code is ErrorCode.INVALID_CSRF

    changed_login = service.login("alice", "test-password-A!")
    change_authorization = authorize(
        service, changed_login.session_token, changed_login.csrf_token, "password.change"
    )
    service.change_password(change_authorization, "replacement-pass-A!")
    with pytest.raises(AccessError):
        service.authenticate_browser(changed_login.session_token, request_id="rotated")
    replacement_login = service.login("alice", "replacement-pass-A!")
    disable_authorization = authorize(
        service,
        replacement_login.session_token,
        replacement_login.csrf_token,
        "user.disable",
    )
    service.disable_user(
        disable_authorization,
        user_id,
        expected_version=2,
        idempotency_key=str(uuid7()),
    )


def test_login_and_idle_projection_ignore_ahead_app_clock(postgres_engine: Engine) -> None:
    clock = MutableClock(datetime(2099, 1, 1, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    service.invite_user("alice", "test-password-A!", {"lanes:read"})
    with postgres_engine.connect() as connection:
        before = connection.scalar(func.clock_timestamp())
    login = service.login("alice", "test-password-A!")
    context = service.authenticate_browser(login.session_token, request_id="database-clock")
    with postgres_engine.connect() as connection:
        after = connection.scalar(func.clock_timestamp())
    assert before is not None and after is not None
    assert before <= context.authenticated_at <= after
    assert context.authenticated_at.year != 2099
    assert login.expires_at == context.authenticated_at + ABSOLUTE_TIMEOUT
    assert before + IDLE_TIMEOUT <= context.expires_at <= after + IDLE_TIMEOUT


def test_scope_csrf_token_binding_origin_operation_and_forgery(postgres_engine: Engine) -> None:
    clock = MutableClock(datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    service.invite_user("alice", "test-password-A!", {"lanes:read"})
    service.invite_user("bob", "test-password-B!", {"lanes:read"})
    alice = service.login("alice", "test-password-A!")
    bob = service.login("bob", "test-password-B!")

    with pytest.raises(AccessError) as scope_error:
        service.authenticate_browser(
            alice.session_token, request_id="scope", required_scope="lanes:control"
        )
    assert scope_error.value.code is ErrorCode.INSUFFICIENT_SCOPE

    authorization = service.authorize_browser_write(
        alice.session_token,
        csrf_token=alice.csrf_token,
        origin="https://familytrade.test",
        request_id="write",
        operation="credential.revoke",
        required_scope="lanes:read",
    )
    assert authorization.operation == "credential.revoke"
    assert alice.csrf_token not in repr(authorization)

    cases = [
        (alice.session_token, bob.csrf_token, "https://familytrade.test", ErrorCode.INVALID_CSRF),
        (alice.session_token, alice.csrf_token, "https://evil.test", ErrorCode.INVALID_ORIGIN),
        (alice.session_token, alice.csrf_token, None, ErrorCode.INVALID_ORIGIN),
    ]
    for token, csrf, origin, code in cases:
        with pytest.raises(AccessError) as denied:
            service.authorize_browser_write(
                token,
                csrf_token=csrf,
                origin=origin,
                request_id="denied",
                operation="credential.revoke",
            )
        assert denied.value.code is code
        assert alice.session_token not in str(denied.value)
        assert alice.csrf_token not in str(denied.value)

    forged = BrowserWriteAuthorization(
        authorization_token="fabricated-capability",
        request_id="write",
        operation="credential.revoke",
        expires_at=clock.value + timedelta(minutes=5),
    )
    with pytest.raises(AccessError) as forged_error:
        service.revoke_credential(
            forged,
            "fabricated-account",
            expected_version=1,
            idempotency_key="00000000-0000-0000-0000-000000000001",
        )
    assert forged_error.value.code is ErrorCode.INVALID_CSRF

    mismatched = BrowserWriteAuthorization(
        authorization_token=authorization.authorization_token,
        request_id=authorization.request_id,
        operation="credential.replace",
        expires_at=authorization.expires_at,
    )
    with pytest.raises(AccessError) as mismatched_error:
        service.revoke_credential(
            mismatched,
            "fabricated-account",
            expected_version=1,
            idempotency_key="00000000-0000-0000-0000-000000000002",
        )
    assert mismatched_error.value.code is ErrorCode.INVALID_CSRF


def test_disable_user_requires_current_administrator_and_keeps_targets_opaque(
    postgres_engine: Engine,
) -> None:
    clock = MutableClock(datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    service.invite_user("admin", "test-password-admin!", {"lanes:read"}, is_administrator=True)
    target_id = service.invite_user("target", "test-password-target!", {"lanes:read"})
    service.invite_user("reader", "test-password-reader!", {"lanes:read"})
    admin = service.login("admin", "test-password-admin!")
    reader = service.login("reader", "test-password-reader!")

    reader_authorization = authorize(
        service, reader.session_token, reader.csrf_token, "user.disable"
    )
    with pytest.raises(AccessError) as non_admin:
        service.disable_user(
            reader_authorization,
            target_id,
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    assert non_admin.value.code is ErrorCode.INSUFFICIENT_SCOPE
    assert service.login("target", "test-password-target!")

    missing_authorization = authorize(
        service, admin.session_token, admin.csrf_token, "user.disable"
    )
    with pytest.raises(AccessError) as missing:
        service.disable_user(
            missing_authorization,
            "00000000-0000-0000-0000-000000000000",
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    assert missing.value.code is ErrorCode.NOT_FOUND

    disable_authorization = authorize(
        service, admin.session_token, admin.csrf_token, "user.disable"
    )
    disable_key = str(uuid7())
    disabled = service.disable_user(
        disable_authorization,
        target_id,
        expected_version=1,
        idempotency_key=disable_key,
    )
    assert disabled.target_user_id == target_id
    assert disabled.enabled is False
    assert disabled.record_version == 2
    with pytest.raises(AccessError) as disabled_login:
        service.login("target", "test-password-target!")
    assert disabled_login.value.code is ErrorCode.UNAUTHENTICATED

    replay_authorization = authorize(service, admin.session_token, admin.csrf_token, "user.disable")
    assert (
        service.disable_user(
            replay_authorization,
            target_id,
            expected_version=1,
            idempotency_key=disable_key,
        )
        == disabled
    )

    conflict_authorization = authorize(
        service, admin.session_token, admin.csrf_token, "user.disable"
    )
    with pytest.raises(AccessError) as conflict:
        service.disable_user(
            conflict_authorization,
            target_id,
            expected_version=2,
            idempotency_key=disable_key,
        )
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

    repeat_authorization = authorize(service, admin.session_token, admin.csrf_token, "user.disable")
    with pytest.raises(AccessError) as repeated:
        service.disable_user(
            repeat_authorization,
            target_id,
            expected_version=1,
            idempotency_key=str(uuid7()),
        )
    assert repeated.value.code is ErrorCode.NOT_FOUND


def test_kek_test_value_is_base64_not_a_repository_secret() -> None:
    assert len(base64.b64encode(b"K" * 32)) == 44


def test_browser_origin_configuration_requires_https(postgres_engine: Engine) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        AccessService(
            AccessRepository(postgres_engine),
            EnvelopeCipher(StubKekRing({"test-v1": b"K" * 32})),
            allowed_origins={"http://familytrade.test"},
        )
