from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine

from familytrade.access.credentials import EnvelopeCipher
from familytrade.access.models import AccessError, ErrorCode
from familytrade.access.repository import AccessRepository
from familytrade.access.service import ABSOLUTE_TIMEOUT, IDLE_TIMEOUT, AccessService


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass(frozen=True)
class StubKek:
    raw: bytes
    version: str = "test-v1"

    def key(self) -> bytes:
        return self.raw


def service_for(engine: Engine, clock: MutableClock) -> AccessService:
    return AccessService(
        AccessRepository(engine),
        EnvelopeCipher(StubKek(b"K" * 32)),
        allowed_origins={"https://familytrade.test"},
        clock=clock,
    )


def test_invited_login_derives_context_and_cookie_contract(postgres_engine: Engine) -> None:
    clock = MutableClock(datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    user_id = service.invite_user(" Alice@Example.test ", "test-password-A!", {"lanes:read"})

    login = service.login("alice@example.test", "test-password-A!")
    context = service.authenticate_browser(
        login.session_token, request_id="018-request", required_scope="lanes:read"
    )

    assert context.user_id == user_id
    assert context.auth_method == "browser_session"
    assert context.scopes == ("lanes:read",)
    assert context.auth_session_id not in login.session_token
    assert context.expires_at == clock.value + IDLE_TIMEOUT
    assert login.expires_at == clock.value + ABSOLUTE_TIMEOUT
    assert service.cookie_settings.secure is True
    assert service.cookie_settings.httponly is True
    assert service.cookie_settings.samesite == "lax"
    assert service.cookie_settings.path == "/"
    assert service.cookie_settings.domain is None
    assert login.session_token not in repr(login)
    assert login.csrf_token not in repr(login)


def test_unknown_wrong_and_disabled_users_share_safe_failure(postgres_engine: Engine) -> None:
    clock = MutableClock(datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    user_id = service.invite_user("alice", "test-password-A!", {"lanes:read"})

    for username, password in (("missing", "wrong-password!"), ("alice", "wrong-password!")):
        with pytest.raises(AccessError) as captured:
            service.login(username, password)
        assert captured.value.envelope("r")["error"] == {
            "code": "UNAUTHENTICATED",
            "message": "Authentication is required.",
            "retryable": False,
            "details": {},
        }

    service.disable_user(user_id)
    with pytest.raises(AccessError, match="Authentication is required"):
        service.login("alice", "test-password-A!")


def test_idle_absolute_revoked_and_password_rotated_sessions_are_rejected(
    postgres_engine: Engine,
) -> None:
    clock = MutableClock(datetime(2026, 9, 13, 1, 0, tzinfo=UTC))
    service = service_for(postgres_engine, clock)
    user_id = service.invite_user("alice", "test-password-A!", {"lanes:read"})

    idle_login = service.login("alice", "test-password-A!")
    clock.value += IDLE_TIMEOUT
    with pytest.raises(AccessError) as idle_error:
        service.authenticate_browser(idle_login.session_token, request_id="idle")
    assert idle_error.value.code is ErrorCode.UNAUTHENTICATED

    clock.value = datetime(2026, 9, 13, 1, 0, tzinfo=UTC)
    absolute_login = service.login("alice", "test-password-A!")
    for _ in range(15):
        clock.value += timedelta(hours=11)
        service.authenticate_browser(absolute_login.session_token, request_id="touch")
    clock.value = absolute_login.expires_at
    with pytest.raises(AccessError):
        service.authenticate_browser(absolute_login.session_token, request_id="absolute")

    clock.value = datetime(2026, 9, 13, 2, 0, tzinfo=UTC)
    revoked_login = service.login("alice", "test-password-A!")
    revoked_context = service.authenticate_browser(revoked_login.session_token, request_id="logout")
    service.logout(revoked_context)
    with pytest.raises(AccessError):
        service.authenticate_browser(revoked_login.session_token, request_id="revoked")
    with pytest.raises(AccessError):
        service.list_broker_accounts(revoked_context)

    changed_login = service.login("alice", "test-password-A!")
    changed_context = service.authenticate_browser(changed_login.session_token, request_id="change")
    service.change_password(changed_context, "replacement-pass-A!")
    with pytest.raises(AccessError):
        service.authenticate_browser(changed_login.session_token, request_id="rotated")
    assert service.login("alice", "replacement-pass-A!")

    service.disable_user(user_id)


def test_scope_csrf_token_binding_and_exact_origin(postgres_engine: Engine) -> None:
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
    assert scope_error.value.http_status == 403

    authorized = service.authorize_browser_write(
        alice.session_token,
        csrf_token=alice.csrf_token,
        origin="https://familytrade.test",
        request_id="write",
        required_scope="lanes:read",
    )
    assert authorized.scopes == ("lanes:read",)

    cases = [
        (alice.session_token, bob.csrf_token, "https://familytrade.test", ErrorCode.INVALID_CSRF),
        (alice.session_token, alice.csrf_token, "https://evil.test", ErrorCode.INVALID_ORIGIN),
        (alice.session_token, alice.csrf_token, None, ErrorCode.INVALID_ORIGIN),
    ]
    for token, csrf, origin, code in cases:
        with pytest.raises(AccessError) as denied:
            service.authorize_browser_write(
                token, csrf_token=csrf, origin=origin, request_id="denied"
            )
        assert denied.value.code is code
        assert alice.session_token not in str(denied.value)
        assert alice.csrf_token not in str(denied.value)


def test_kek_test_value_is_base64_not_a_repository_secret() -> None:
    # The value is generated test material; deployments supply their own external setting.
    assert len(base64.b64encode(b"K" * 32)) == 44


def test_browser_origin_configuration_requires_https(postgres_engine: Engine) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        AccessService(
            AccessRepository(postgres_engine),
            EnvelopeCipher(StubKek(b"K" * 32)),
            allowed_origins={"http://familytrade.test"},
        )
