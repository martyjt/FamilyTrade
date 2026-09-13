"""Application access operations; identity is always derived from authentication."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

from pwdlib import PasswordHash

from familytrade.access.credentials import EnvelopeCipher
from familytrade.access.models import (
    AccessError,
    BrokerAccountView,
    BrowserWriteAuthorization,
    CookieSettings,
    ErrorCode,
    LoginResult,
    PasswordChangeResult,
    SessionLogoutResult,
    UserContext,
    UserDisableResult,
    unauthenticated,
)
from familytrade.access.repository import AccessRepository

IDLE_TIMEOUT = timedelta(hours=12)
ABSOLUTE_TIMEOUT = timedelta(days=7)
WRITE_AUTHORIZATION_TIMEOUT = timedelta(minutes=5)
BROWSER_WRITE_OPERATIONS = frozenset(
    {
        "broker_account.create",
        "credential.replace",
        "credential.revoke",
        "credential.rewrap",
        "password.change",
        "session.logout",
        "user.disable",
    }
)
SUPPORTED_SCOPES = frozenset(
    {
        "data:read",
        "strategy:read",
        "strategy:write",
        "runs:read",
        "runs:write",
        "lanes:read",
        "lanes:control",
    }
)


class AccessService:
    cookie_settings = CookieSettings()

    def __init__(
        self,
        repository: AccessRepository,
        cipher: EnvelopeCipher,
        *,
        allowed_origins: Collection[str],
        clock: Callable[[], datetime] | None = None,
        password_hash: PasswordHash | None = None,
    ) -> None:
        self._repository = repository
        self._cipher = cipher
        self._allowed_origins = frozenset(allowed_origins)
        if not self._allowed_origins or any(
            not _is_https_origin(origin) for origin in self._allowed_origins
        ):
            raise ValueError("Every allowed browser origin must be an explicit HTTPS origin.")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._password_hash = password_hash or PasswordHash.recommended()
        self._dummy_password_hash = self._password_hash.hash(secrets.token_bytes(32))

    def invite_user(
        self,
        username: str,
        initial_password: str,
        scopes: Collection[str],
        *,
        is_administrator: bool = False,
    ) -> str:
        """Administrator-only bootstrap operation; it is deliberately not a public route."""
        normalized = _normalize_username(username)
        _validate_password(initial_password)
        normalized_scopes = _normalize_scopes(scopes)
        return self._repository.create_invited_user(
            normalized,
            self._password_hash.hash(initial_password),
            normalized_scopes,
            is_administrator,
            self._now(),
        )

    def login(self, username: str, password: str) -> LoginResult:
        normalized = _normalize_username(username)
        user = self._repository.get_user_for_login(normalized)
        password_hash = (
            cast(str, user["password_hash"]) if user is not None else self._dummy_password_hash
        )
        password_valid = self._password_hash.verify(password, password_hash)
        if user is None or not password_valid or not cast(bool, user["enabled"]):
            raise unauthenticated()
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        _, absolute_expires = self._repository.create_session(
            user_id=cast(str, user["user_id"]),
            token_hash=_token_hash(session_token),
            csrf_hash=_token_hash(csrf_token),
            expected_credential_version=cast(int, user["credential_version"]),
            idle_timeout=IDLE_TIMEOUT,
            absolute_timeout=ABSOLUTE_TIMEOUT,
        )
        return LoginResult(
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=absolute_expires,
        )

    def authenticate_browser(
        self, session_token: str, *, request_id: str, required_scope: str | None = None
    ) -> UserContext:
        context, _ = self._authenticate_browser(
            session_token, request_id=request_id, required_scope=required_scope, touch=True
        )
        return context

    def _authenticate_browser(
        self,
        session_token: str,
        *,
        request_id: str,
        required_scope: str | None,
        touch: bool,
    ) -> tuple[UserContext, bytes]:
        return self._repository.authenticate_session(
            _token_hash(session_token),
            request_id=request_id,
            required_scope=required_scope,
            touch=touch,
            idle_timeout=IDLE_TIMEOUT,
        )

    def authorize_browser_write(
        self,
        session_token: str,
        *,
        csrf_token: str,
        origin: str | None,
        request_id: str,
        operation: str,
        required_scope: str | None = None,
    ) -> BrowserWriteAuthorization:
        if operation not in BROWSER_WRITE_OPERATIONS:
            raise AccessError(ErrorCode.VALIDATION_ERROR, "Invalid browser write operation.", 422)
        context, expected_csrf_hash = self._authenticate_browser(
            session_token,
            request_id=request_id,
            required_scope=required_scope,
            touch=False,
        )
        if origin is None or origin not in self._allowed_origins:
            raise AccessError(ErrorCode.INVALID_ORIGIN, "Request origin is not allowed.", 403)
        supplied_csrf_hash = _token_hash(csrf_token)
        if not secrets.compare_digest(supplied_csrf_hash, expected_csrf_hash):
            raise AccessError(ErrorCode.INVALID_CSRF, "CSRF validation failed.", 403)
        authorization_token = secrets.token_urlsafe(32)
        expires_at = self._repository.create_write_authorization(
            context,
            authorization_token,
            request_id,
            operation,
            WRITE_AUTHORIZATION_TIMEOUT,
        )
        return BrowserWriteAuthorization(
            authorization_token=authorization_token,
            request_id=request_id,
            operation=operation,
            expires_at=expires_at,
        )

    def logout(
        self, authorization: BrowserWriteAuthorization, *, idempotency_key: str
    ) -> SessionLogoutResult:
        return self._repository.revoke_authorized_session(authorization, idempotency_key)

    def disable_user(
        self,
        authorization: BrowserWriteAuthorization,
        target_user_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> UserDisableResult:
        """Disable an invited user through a server-derived administrator session."""
        try:
            return self._repository.disable_authorized_user(
                authorization, target_user_id, expected_version, idempotency_key
            )
        except AccessError as error:
            self._audit_write_denial(authorization, error)
            raise

    def change_password(
        self,
        authorization: BrowserWriteAuthorization,
        new_password: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> PasswordChangeResult:
        _validate_password(new_password)
        return self._repository.change_authorized_password(
            authorization,
            self._password_hash.hash(new_password),
            hashlib.sha256(new_password.encode("utf-8")).hexdigest(),
            expected_version,
            idempotency_key,
        )

    def create_broker_account(
        self,
        authorization: BrowserWriteAuthorization,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> BrokerAccountView:
        return self._repository.create_broker_account(
            authorization, payload, idempotency_key, self._cipher
        )

    def get_broker_account(self, context: UserContext, account_id: str) -> BrokerAccountView:
        self._require_current_context(context)
        self._require_administrator(context)
        try:
            return self._repository.get_broker_account(context, account_id)
        except AccessError as exc:
            self._audit_opaque_denial(context, exc)
            raise

    def list_broker_accounts(self, context: UserContext) -> tuple[BrokerAccountView, ...]:
        self._require_current_context(context)
        self._require_administrator(context)
        return self._repository.list_broker_accounts(context)

    def replace_credential(
        self,
        authorization: BrowserWriteAuthorization,
        account_id: str,
        new_secret: bytes,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BrokerAccountView:
        try:
            return self._repository.replace_credential(
                authorization,
                account_id,
                new_secret,
                expected_version,
                idempotency_key,
                self._cipher,
            )
        except AccessError as error:
            self._audit_write_denial(authorization, error)
            raise

    def revoke_credential(
        self,
        authorization: BrowserWriteAuthorization,
        account_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BrokerAccountView:
        try:
            return self._repository.revoke_credential(
                authorization, account_id, expected_version, idempotency_key
            )
        except AccessError as error:
            self._audit_write_denial(authorization, error)
            raise

    def rewrap_credentials(
        self,
        authorization: BrowserWriteAuthorization,
        account_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> BrokerAccountView:
        try:
            return self._repository.rewrap_credentials(
                authorization,
                account_id,
                expected_version,
                idempotency_key,
                self._cipher,
            )
        except AccessError as error:
            self._audit_write_denial(authorization, error)
            raise

    def _audit_opaque_denial(self, context: UserContext, error: AccessError) -> None:
        if error.code is ErrorCode.NOT_FOUND:
            self._repository.audit_not_found(context)

    def _audit_write_denial(
        self, authorization: BrowserWriteAuthorization, error: AccessError
    ) -> None:
        if error.code is ErrorCode.NOT_FOUND:
            self._repository.audit_authorized_not_found(authorization)

    def _require_current_context(self, context: UserContext) -> None:
        if not self._repository.context_is_current(context):
            raise unauthenticated()

    @staticmethod
    def _require_administrator(context: UserContext) -> None:
        if not context.is_administrator:
            raise AccessError(
                ErrorCode.INSUFFICIENT_SCOPE,
                "Administrator authorization is required.",
                403,
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Access clocks must return timezone-aware UTC timestamps.")
        return now.astimezone(UTC)


def _normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if not normalized or len(normalized) > 254:
        raise AccessError(ErrorCode.VALIDATION_ERROR, "Invalid username.", 422)
    return normalized


def _validate_password(password: str) -> None:
    if len(password) < 12 or len(password) > 1024:
        raise AccessError(
            ErrorCode.VALIDATION_ERROR,
            "Password must contain between 12 and 1024 characters.",
            422,
        )


def _normalize_scopes(scopes: Collection[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(scopes)))
    if not normalized or any(scope not in SUPPORTED_SCOPES for scope in normalized):
        raise AccessError(ErrorCode.VALIDATION_ERROR, "Invalid scope grant.", 422)
    return normalized


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _is_https_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
    )
