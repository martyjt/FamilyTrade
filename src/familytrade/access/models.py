"""Public and persistence-neutral types for the access boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


class BrokerAccountCreate(BaseModel):
    """Strict public input: ownership and persistence fields are server-only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    provider_account_reference: str = Field(min_length=1, max_length=500)
    environment: BrokerEnvironment
    capabilities: tuple[str, ...] = ()
    credential: bytes = Field(min_length=1, max_length=65536, repr=False)


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


@dataclass(frozen=True, slots=True)
class BrowserWriteContext(UserContext):
    """A current browser identity after bound CSRF and Origin validation."""

    csrf_validated_at: datetime


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
