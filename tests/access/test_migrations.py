from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from familytrade.access.repository import access_metadata


def test_migration_matches_metadata_and_installs_relationship_constraints(
    postgres_engine: Engine,
) -> None:
    access_table_names = frozenset(access_metadata.tables)

    def include_access_name(
        name: str | None, object_type: str, parent_names: dict[str, str | None]
    ) -> bool:
        if object_type == "table":
            return name in access_table_names
        table_name = parent_names.get("table_name")
        return table_name is None or table_name in access_table_names

    with postgres_engine.connect() as connection:
        migration_context = MigrationContext.configure(
            connection, opts={"include_name": include_access_name}
        )
        assert compare_metadata(migration_context, access_metadata) == []

    inspector = inspect(postgres_engine)
    envelope_foreign_keys = {
        item["name"] for item in inspector.get_foreign_keys("access_credential_envelopes")
    }
    account_foreign_keys = {
        item["name"] for item in inspector.get_foreign_keys("access_broker_accounts")
    }
    envelope_indexes = {
        item["name"] for item in inspector.get_indexes("access_credential_envelopes")
    }
    assert "fk_access_envelope_account_owner_provider" in envelope_foreign_keys
    assert {
        "fk_access_broker_reference_envelope",
        "fk_access_broker_credential_envelope",
    } <= account_foreign_keys
    assert "uq_access_active_envelope_per_purpose" in envelope_indexes
