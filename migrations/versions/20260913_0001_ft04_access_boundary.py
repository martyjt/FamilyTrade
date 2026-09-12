"""Create the FT-04 private-user access boundary.

Revision ID: 20260913_0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260913_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_users",
        sa.Column("user_id", sa.String(100), primary_key=True),
        sa.Column("username_normalized", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("credential_version >= 1", name="ck_access_users_credential_version"),
    )
    op.create_table(
        "access_sessions",
        sa.Column("auth_session_id", sa.String(100), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(100),
            sa.ForeignKey("access_users.user_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("authenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "idle_expires_at > authenticated_at AND absolute_expires_at > authenticated_at",
            name="ck_access_sessions_expiry",
        ),
    )
    op.create_table(
        "access_credential_envelopes",
        sa.Column("credential_envelope_id", sa.String(100), primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.String(100),
            sa.ForeignKey("access_users.user_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("broker_account_id", sa.String(100), nullable=False, index=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("purpose", sa.String(100), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("wrap_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "purpose IN ('provider_account_ref', 'provider_credential')",
            name="ck_access_credential_purpose",
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_access_credential_status"),
    )
    op.create_table(
        "access_broker_accounts",
        sa.Column("broker_account_id", sa.String(100), primary_key=True),
        sa.Column("schema_version", sa.String(100), nullable=False),
        sa.Column(
            "owner_user_id",
            sa.String(100),
            sa.ForeignKey("access_users.user_id"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("provider_account_ref_ciphertext_id", sa.String(100), nullable=False),
        sa.Column("provider_account_ref_last_four", sa.String(100), nullable=False),
        sa.Column("environment", sa.String(100), nullable=False),
        sa.Column("status", sa.String(100), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("credential_envelope_id", sa.String(100), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("environment IN ('paper', 'live')", name="ck_access_broker_environment"),
        sa.CheckConstraint(
            "status IN ('pending_verification', 'active', 'reauth_required', 'disabled')",
            name="ck_access_broker_status",
        ),
        sa.CheckConstraint("record_version >= 1", name="ck_access_broker_record_version"),
    )
    op.create_table(
        "access_audit_events",
        sa.Column("audit_event_id", sa.String(100), primary_key=True),
        sa.Column(
            "actor_user_id",
            sa.String(100),
            sa.ForeignKey("access_users.user_id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("broker_account_id", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "access_idempotency_records",
        sa.Column(
            "owner_user_id",
            sa.String(100),
            sa.ForeignKey("access_users.user_id"),
            primary_key=True,
        ),
        sa.Column("operation", sa.String(100), primary_key=True),
        sa.Column("idempotency_key", sa.String(100), primary_key=True),
        sa.Column("request_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("access_idempotency_records")
    op.drop_table("access_audit_events")
    op.drop_table("access_broker_accounts")
    op.drop_table("access_credential_envelopes")
    op.drop_table("access_sessions")
    op.drop_table("access_users")
