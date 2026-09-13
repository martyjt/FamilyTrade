from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


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
        connection.execute(
            text(
                "TRUNCATE access_write_authorizations, access_idempotency_records, "
                "access_audit_events, access_broker_accounts, access_credential_envelopes, "
                "access_sessions, access_users CASCADE"
            )
        )
