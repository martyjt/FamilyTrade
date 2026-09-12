"""Envelope encryption for write-only provider credentials."""

from __future__ import annotations

import base64
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from familytrade.access.models import AccessError, ErrorCode

KEK_ENVIRONMENT_VARIABLE = "FAMILYTRADE_CREDENTIAL_KEK_V1"
KEK_VERSION_ENVIRONMENT_VARIABLE = "FAMILYTRADE_CREDENTIAL_KEK_VERSION"


class KekProvider(Protocol):
    @property
    def version(self) -> str: ...

    def key(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class EnvironmentKekProvider:
    """Load the deployment KEK from process configuration, never persistence."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    @property
    def version(self) -> str:
        return self.environ.get(KEK_VERSION_ENVIRONMENT_VARIABLE, "v1")

    def key(self) -> bytes:
        encoded = self.environ.get(KEK_ENVIRONMENT_VARIABLE)
        if encoded is None:
            raise _key_unavailable()
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise _key_unavailable() from exc
        if len(key) != 32:
            raise _key_unavailable()
        return key


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)
    wrapped_dek: bytes = field(repr=False)
    wrap_nonce: bytes = field(repr=False)
    key_version: str


class EnvelopeCipher:
    """AES-GCM data envelopes bound to owner/account/provider/purpose."""

    def __init__(self, kek_provider: KekProvider) -> None:
        self._kek_provider = kek_provider

    @staticmethod
    def associated_data(owner_user_id: str, account_id: str, provider: str, purpose: str) -> bytes:
        return json.dumps(
            {
                "account_id": account_id,
                "owner_user_id": owner_user_id,
                "provider": provider,
                "purpose": purpose,
                "schema_version": "v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def encrypt(
        self,
        secret: bytes,
        *,
        owner_user_id: str,
        account_id: str,
        provider: str,
        purpose: str,
    ) -> CredentialEnvelope:
        if not secret:
            raise AccessError(ErrorCode.VALIDATION_ERROR, "Credential must not be empty.", 422)
        aad = self.associated_data(owner_user_id, account_id, provider, purpose)
        dek = AESGCM.generate_key(bit_length=256)
        nonce = secrets.token_bytes(12)
        wrap_nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(dek).encrypt(nonce, secret, aad)
        wrapped_dek = AESGCM(self._kek_provider.key()).encrypt(wrap_nonce, dek, aad)
        return CredentialEnvelope(
            ciphertext=ciphertext,
            nonce=nonce,
            wrapped_dek=wrapped_dek,
            wrap_nonce=wrap_nonce,
            key_version=self._kek_provider.version,
        )

    def _decrypt(
        self,
        envelope: CredentialEnvelope,
        *,
        owner_user_id: str,
        account_id: str,
        provider: str,
        purpose: str,
    ) -> bytes:
        if envelope.key_version != self._kek_provider.version:
            raise _key_unavailable()
        aad = self.associated_data(owner_user_id, account_id, provider, purpose)
        try:
            dek = AESGCM(self._kek_provider.key()).decrypt(
                envelope.wrap_nonce, envelope.wrapped_dek, aad
            )
            return AESGCM(dek).decrypt(envelope.nonce, envelope.ciphertext, aad)
        except (InvalidTag, ValueError) as exc:
            raise _key_unavailable() from exc

    def verify(self, envelope: CredentialEnvelope, expected: bytes, **binding: str) -> None:
        decrypted = self._decrypt(envelope, **binding)
        if not secrets.compare_digest(decrypted, expected):
            raise _key_unavailable()

    def verify_available(self, envelope: CredentialEnvelope, **binding: str) -> None:
        """Prove decryptability without exposing plaintext to a caller."""
        decrypted = self._decrypt(envelope, **binding)
        del decrypted


def mask_reference(value: str) -> str:
    suffix = value[-4:] if len(value) >= 4 else value
    return f"****{suffix}"


def _key_unavailable() -> AccessError:
    return AccessError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "Credential key material is unavailable.",
        503,
        retryable=False,
    )
