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
    UserContext,
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
        now = self._now()
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        absolute_expires = now + ABSOLUTE_TIMEOUT
        self._repository.create_session(
            user_id=cast(str, user["user_id"]),
            token_hash=_token_hash(session_token),
            csrf_hash=_token_hash(csrf_token),
            scopes=cast(list[str], user["scopes"]),
            is_administrator=cast(bool, user["is_administrator"]),
            credential_version=cast(int, user["credential_version"]),
            authenticated_at=now,
            idle_expires_at=now + IDLE_TIMEOUT,
            absolute_expires_at=absolute_expires,
        )
        return LoginResult(
            session_token=session_token,
            csrf_token=csrf_token,
            expires_at=absolute_expires,
        )

    def authenticate_browser(
        self, session_token: str, *, request_id: str, required_scope: str | None = None
    ) -> UserContext:
        return self._authenticate_browser(
            session_token, request_id=request_id, required_scope=required_scope, touch=True
        )

    def _authenticate_browser(
        self,
        session_token: str,
        *,
        request_id: str,
        required_scope: str | None,
        touch: bool,
    ) -> UserContext:
        now = self._now()
        session = self._repository.get_session(_token_hash(session_token))
        if session is None:
            raise unauthenticated()
        invalid = (
            session["revoked_at"] is not None
            or not cast(bool, session["enabled"])
            or now >= cast(datetime, session["idle_expires_at"])
            or now >= cast(datetime, session["absolute_expires_at"])
            or session["credential_version"] != session["current_credential_version"]
            or session["is_administrator"] != session["current_is_administrator"]
        )
        if invalid:
            raise unauthenticated()
        scopes = tuple(sorted(set(cast(list[str], session["scopes"]))))
        if required_scope is not None and required_scope not in scopes:
            raise AccessError(
                ErrorCode.INSUFFICIENT_SCOPE,
                "The authenticated identity lacks the required scope.",
                403,
            )
        next_idle_expiry = min(now + IDLE_TIMEOUT, cast(datetime, session["absolute_expires_at"]))
        if touch:
            self._repository.touch_session(
                cast(str, session["auth_session_id"]), now, next_idle_expiry
            )
        return UserContext(
            schema_version="v1",
            user_id=cast(str, session["user_id"]),
            auth_session_id=cast(str, session["auth_session_id"]),
            auth_method="browser_session",
            scopes=scopes,
            authenticated_at=cast(datetime, session["authenticated_at"]),
            expires_at=next_idle_expiry,
            request_id=request_id,
            credential_version=cast(int, session["credential_version"]),
            is_administrator=cast(bool, session["is_administrator"]),
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
        context = self._authenticate_browser(
            session_token,
            request_id=request_id,
            required_scope=required_scope,
            touch=False,
        )
        session = self._repository.get_session(_token_hash(session_token))
        assert session is not None  # authentication above established the same hashed token
        if origin is None or origin not in self._allowed_origins:
            raise AccessError(ErrorCode.INVALID_ORIGIN, "Request origin is not allowed.", 403)
        supplied_csrf_hash = _token_hash(csrf_token)
        if not secrets.compare_digest(supplied_csrf_hash, cast(bytes, session["csrf_hash"])):
            raise AccessError(ErrorCode.INVALID_CSRF, "CSRF validation failed.", 403)
        validated_at = self._now()
        authorization_token = secrets.token_urlsafe(32)
        expires_at = min(validated_at + WRITE_AUTHORIZATION_TIMEOUT, context.expires_at)
        self._repository.create_write_authorization(
            context,
            authorization_token,
            request_id,
            operation,
            expires_at,
            validated_at,
        )
        return BrowserWriteAuthorization(
            authorization_token=authorization_token,
            request_id=request_id,
            operation=operation,
            expires_at=expires_at,
        )

    def logout(self, authorization: BrowserWriteAuthorization) -> None:
        self._repository.revoke_authorized_session(authorization, self._now())

    def disable_user(self, user_id: str) -> None:
        """Administrator-only operation; disabling rotates credentials and sessions."""
        self._repository.disable_user(user_id, self._now())

    def change_password(self, authorization: BrowserWriteAuthorization, new_password: str) -> None:
        _validate_password(new_password)
        self._repository.change_authorized_password(
            authorization, self._password_hash.hash(new_password), self._now()
        )

    def create_broker_account(
        self,
        authorization: BrowserWriteAuthorization,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> BrokerAccountView:
        return self._repository.create_broker_account(
            authorization, payload, idempotency_key, self._cipher, self._now()
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
                self._now(),
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
                authorization, account_id, expected_version, idempotency_key, self._now()
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
                self._now(),
            )
        except AccessError as error:
            self._audit_write_denial(authorization, error)
            raise

    def _audit_opaque_denial(self, context: UserContext, error: AccessError) -> None:
        if error.code is ErrorCode.NOT_FOUND:
            self._repository.audit_not_found(context, self._now())

    def _audit_write_denial(
        self, authorization: BrowserWriteAuthorization, error: AccessError
    ) -> None:
        if error.code is ErrorCode.NOT_FOUND:
            self._repository.audit_authorized_not_found(authorization, self._now())

    def _require_current_context(self, context: UserContext) -> None:
        if not self._repository.context_is_current(context, self._now()):
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
