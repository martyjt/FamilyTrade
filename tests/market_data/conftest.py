from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid7

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from familytrade.access.models import UserContext
from familytrade.access.repository import users
from familytrade.market_data.archive import ArchiveStore
from familytrade.market_data.catalog import MarketDataCatalog


@pytest.fixture
def contract_case() -> Callable[[str], dict[str, object]]:
    path = Path(__file__).resolve().parents[2] / "docs" / "contracts-examples-v1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in document["cases"]}

    def load(case_id: str) -> dict[str, object]:
        return cases[case_id]

    return load


@pytest.fixture(scope="session")
def postgres_engine() -> Iterator[Engine]:
    url = os.environ.get("FAMILYTRADE_TEST_DATABASE_URL")
    if url is None:
        pytest.fail(
            "FAMILYTRADE_TEST_DATABASE_URL is required; market-data tests require PostgreSQL 16."
        )
    engine = create_engine(url, pool_pre_ping=True)
    root = Path(__file__).resolve().parents[2]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(postgres_engine: Engine) -> Iterator[None]:
    yield
    with postgres_engine.begin() as connection:
        connection.execute(text("TRUNCATE access_users CASCADE"))


def _context(
    user_id: str, *, scopes: tuple[str, ...] = ("data:read",), admin: bool = False
) -> UserContext:
    now = datetime(2026, 9, 13, 8, tzinfo=UTC)
    return UserContext(
        schema_version="v1",
        user_id=user_id,
        auth_session_id=str(uuid7()),
        auth_method="browser_session",
        scopes=scopes,
        authenticated_at=now,
        expires_at=now + timedelta(hours=1),
        request_id=str(uuid7()),
        credential_version=1,
        is_administrator=admin,
    )


@pytest.fixture
def contexts(postgres_engine: Engine) -> tuple[UserContext, UserContext]:
    a, b = _context(str(uuid7())), _context(str(uuid7()))
    now = datetime(2026, 9, 13, 8, tzinfo=UTC)
    with postgres_engine.begin() as c:
        for n, context in enumerate((a, b)):
            c.execute(
                users.insert().values(
                    user_id=context.user_id,
                    username_normalized=f"user{n}@example.invalid",
                    password_hash="fixture",
                    scopes=list(context.scopes),
                    is_administrator=False,
                    enabled=True,
                    credential_version=1,
                    record_version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
    return a, b


@pytest.fixture
def catalog(postgres_engine: Engine) -> MarketDataCatalog:
    return MarketDataCatalog(
        postgres_engine, context_is_current=lambda _: True, clock=lambda: datetime.now(UTC)
    )


@pytest.fixture
def archive_store(tmp_path: Path) -> ArchiveStore:
    return ArchiveStore(tmp_path / "archive")
