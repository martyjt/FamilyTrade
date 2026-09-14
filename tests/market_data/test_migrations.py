from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from familytrade.access.repository import access_metadata, users
from familytrade.market_data.catalog import market_data_metadata
from familytrade.strategies.repository import strategy_metadata


def test_alembic_env_combines_access_and_market_data_metadata_without_duplicate_keys_or_drift(
    postgres_engine,
) -> None:
    keys = [
        table.key
        for metadata in (access_metadata, market_data_metadata, strategy_metadata)
        for table in metadata.tables.values()
    ]
    assert len(keys) == len(set(keys))
    with postgres_engine.connect() as connection:
        drift = compare_metadata(
            MigrationContext.configure(connection),
            [access_metadata, market_data_metadata, strategy_metadata],
        )
        uuid_guarded_tables = set(
            connection.execute(
                text(
                    "SELECT event_object_table FROM information_schema.triggers "
                    "WHERE trigger_schema=current_schema() "
                    "AND trigger_name='md_uuidv7_guard'"
                )
            ).scalars()
        )
    assert drift == []
    assert uuid_guarded_tables == set(market_data_metadata.tables)


def test_market_data_user_foreign_keys_target_integrated_access_repository_users_column_object() -> (
    None
):
    owner_tables = {
        table.name: table
        for table in market_data_metadata.tables.values()
        if "owner_user_id" in table.c
    }
    assert len(owner_tables) == 27
    for name, table in owner_tables.items():
        direct_owner_fks = [
            constraint
            for constraint in table.foreign_key_constraints
            if len(constraint.elements) == 1
            and constraint.elements[0].parent is table.c.owner_user_id
        ]
        assert len(direct_owner_fks) == 1, name
        assert direct_owner_fks[0].elements[0].column is users.c.user_id, name


def test_alembic_metadata_constraints_and_ft04_rows_survive_upgrade_downgrade(
    postgres_engine,
) -> None:
    user_id = str(uuid7())
    now = datetime.now(UTC)
    with postgres_engine.begin() as c:
        c.execute(
            users.insert().values(
                user_id=user_id,
                username_normalized="migration-survivor@example.invalid",
                password_hash="fixture",
                scopes=["data:read"],
                is_administrator=False,
                enabled=True,
                credential_version=1,
                record_version=1,
                created_at=now,
                updated_at=now,
            )
        )
    root = Path(__file__).resolve().parents[2]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", os.environ["FAMILYTRADE_TEST_DATABASE_URL"])
    command.downgrade(config, "20260913_0001")
    downgraded = set(inspect(postgres_engine).get_table_names())
    assert set(access_metadata.tables) <= downgraded
    assert not set(market_data_metadata.tables) & downgraded
    with postgres_engine.connect() as c:
        assert c.execute(users.select().where(users.c.user_id == user_id)).first() is not None
    command.upgrade(config, "head")
    upgraded = set(inspect(postgres_engine).get_table_names())
    assert (
        set(access_metadata.tables)
        | set(market_data_metadata.tables)
        | set(strategy_metadata.tables)
        <= upgraded
    )
    with postgres_engine.connect() as c:
        assert c.execute(users.select().where(users.c.user_id == user_id)).first() is not None


import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid7

from alembic import command
from alembic.config import Config
