"""Public and persistence-neutral types for the access boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "v1"


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    STALE_VERSION = "STALE_VERSION"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INVALID_CSRF = "INVALID_CSRF"
    INVALID_ORIGIN = "INVALID_ORIGIN"


class AccessError(Exception):
    """A safe operation error whose text/details may be returned to a caller."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        http_status: int,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.details = details or {}

    def envelope(self, request_id: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            },
        }


class BrokerEnvironment(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class BrokerAccountStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    REAUTH_REQUIRED = "reauth_required"
    DISABLED = "disabled"


class _BrokerAccountFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    provider: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    provider_account_reference: str = Field(min_length=1, max_length=500, repr=False)
    environment: BrokerEnvironment
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrokerAccountCreate:
    """Secret-safe strict input with no caller-supplied ownership fields."""

    provider: str
    provider_account_reference: str = field(repr=False)
    environment: BrokerEnvironment
    capabilities: tuple[str, ...]
    credential: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class UserContext:
    schema_version: Literal["v1"]
    user_id: str
    auth_session_id: str
    auth_method: Literal["browser_session", "mcp_oauth"]
    scopes: tuple[str, ...]
    authenticated_at: datetime
    expires_at: datetime
    request_id: str
    credential_version: int
    is_administrator: bool


@dataclass(frozen=True, slots=True)
class BrowserWriteAuthorization:
    """Opaque, single-use database-backed authorization for one browser mutation."""

    authorization_token: str = field(repr=False)
    request_id: str
    operation: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CookieSettings:
    secure: Literal[True] = True
    httponly: Literal[True] = True
    samesite: Literal["lax"] = "lax"
    path: Literal["/"] = "/"
    domain: None = None


@dataclass(frozen=True, slots=True)
class BrokerAccountView:
    schema_version: Literal["v1"]
    broker_account_id: str
    owner_user_id: str
    provider: str
    provider_account_reference: str
    environment: BrokerEnvironment
    status: BrokerAccountStatus
    capabilities: tuple[str, ...]
    credential_status: Literal["active", "revoked"]
    verified_at: datetime | None
    last_reconciled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    record_version: int


def not_found() -> AccessError:
    return AccessError(ErrorCode.NOT_FOUND, "Resource not found.", 404)


def unauthenticated() -> AccessError:
    return AccessError(ErrorCode.UNAUTHENTICATED, "Authentication is required.", 401)


def stale_version(expected: int, current: int) -> AccessError:
    return AccessError(
        ErrorCode.STALE_VERSION,
        "The resource version is stale.",
        409,
        details={"expected_version": expected, "current_version": current},
    )


def parse_broker_account_create(payload: dict[str, object]) -> BrokerAccountCreate:
    allowed_fields = {
        "provider",
        "provider_account_reference",
        "environment",
        "capabilities",
        "credential",
    }
    unknown_fields = sorted(set(payload) - allowed_fields)
    if unknown_fields:
        raise AccessError(
            ErrorCode.VALIDATION_ERROR,
            "Invalid broker account input.",
            422,
            details={"paths": [f"/{field}" for field in unknown_fields]},
        )
    credential = payload.get("credential")
    validate_secret_bytes(credential, path="/credential")
    public_fields = {key: value for key, value in payload.items() if key != "credential"}
    try:
        parsed = _BrokerAccountFields.model_validate(public_fields)
    except ValidationError as exc:
        paths = sorted("/" + "/".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise AccessError(
            ErrorCode.VALIDATION_ERROR,
            "Invalid broker account input.",
            422,
            details={"paths": paths},
        ) from None
    return BrokerAccountCreate(
        provider=parsed.provider,
        provider_account_reference=parsed.provider_account_reference,
        environment=parsed.environment,
        capabilities=parsed.capabilities,
        credential=cast(bytes, credential),
    )


def validate_secret_bytes(value: object, *, path: str) -> None:
    if not isinstance(value, bytes) or not 1 <= len(value) <= 65536:
        raise AccessError(
            ErrorCode.VALIDATION_ERROR,
            "Invalid credential input.",
            422,
            details={"paths": [path]},
        )
