from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from familytrade.access.repository import access_metadata


@pytest.fixture(scope="session")
def postgres_engine() -> Iterator[Engine]:
    database_url = os.environ.get("FAMILYTRADE_TEST_DATABASE_URL")
    if database_url is None:
        pytest.fail(
            "FAMILYTRADE_TEST_DATABASE_URL is required; access tests must exercise disposable PostgreSQL."
        )
    engine = create_engine(database_url, pool_pre_ping=True)
    repository_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    alembic_config = Config(os.path.join(repository_root, "alembic.ini"))
    alembic_config.set_main_option("script_location", os.path.join(repository_root, "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    try:
        yield engine
    finally:
        command.downgrade(alembic_config, "base")
        engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(postgres_engine: Engine) -> Iterator[None]:
    yield
    with postgres_engine.begin() as connection:
        for table in reversed(access_metadata.sorted_tables):
            connection.execute(table.delete())
