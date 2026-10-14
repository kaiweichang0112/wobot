"""The asset worker's tests: its database role, an account to own versions, and media
in a temporary directory."""

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.database import ASSETS_USER, run_as_migrator
from wobot.assets.media import LocalMediaStore
from wobot.config import get_settings

OWNER = "uid-asset-worker-tests"


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker]:
    """Connections as the worker's role, which cannot add or remove rows."""
    settings = get_settings()
    url = URL.create(
        "postgresql+asyncpg",
        username=ASSETS_USER,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )
    engine = create_async_engine(url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def owner() -> Iterator[str]:
    asyncio.run(
        run_as_migrator(
            "INSERT INTO app.accounts (account_id, email) VALUES ($1, 'w@example.com') "
            "ON CONFLICT DO NOTHING",
            OWNER,
        )
    )
    yield OWNER
    asyncio.run(run_as_migrator("DELETE FROM app.accounts WHERE account_id = $1", OWNER))


@pytest.fixture
def media(tmp_path) -> LocalMediaStore:
    return LocalMediaStore(tmp_path)
