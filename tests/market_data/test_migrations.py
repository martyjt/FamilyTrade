from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from familytrade.access.repository import access_metadata, users
from familytrade.market_data.catalog import market_data_metadata


def test_alembic_env_combines_access_and_market_data_metadata_without_duplicate_keys_or_drift(
    postgres_engine,
) -> None:
    keys = [
        table.key
        for metadata in (access_metadata, market_data_metadata)
        for table in metadata.tables.values()
    ]
    assert len(keys) == len(set(keys))
    assert (
        compare_metadata(
            MigrationContext.configure(postgres_engine.connect()),
            [access_metadata, market_data_metadata],
        )
        == []
    )


def test_market_data_user_foreign_keys_target_integrated_access_repository_users_column_object() -> (
    None
):
    targets = [
        fk.column
        for table in market_data_metadata.tables.values()
        for fk in table.foreign_keys
        if fk.column.table is users
    ]
    assert targets and all(target is users.c.user_id for target in targets)


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
    assert set(access_metadata.tables) | set(market_data_metadata.tables) <= upgraded
    with postgres_engine.connect() as c:
        assert c.execute(users.select().where(users.c.user_id == user_id)).first() is not None


import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid7

from alembic import command
from alembic.config import Config
