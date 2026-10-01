from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.database import INGEST_USER
from wobot.config import Settings
from wobot.db import create_engine
from wobot.knowledge.models import ActiveKnowledge


class SavepointDatabase:
    """Hands out savepoints on one connection whose transaction the test rolls back.

    Code under test commits step by step as in production, yet no row outlives the test,
    even as the ingest role, which may not delete.
    """

    def __init__(self, connection: AsyncConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        async with self.connection.begin_nested():
            yield self.connection


@pytest.fixture
async def ingest_db() -> AsyncIterator[SavepointDatabase]:
    """The local database as the ingest role, rolled back when the test ends."""
    engine, _ = await create_engine(Settings(db_user=INGEST_USER), pooled=False)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            # Start from an empty corpus: a version the developer published locally would
            # otherwise be carried over into every version a test builds.
            await connection.execute(update(ActiveKnowledge).values(index_version_id=None))
            try:
                yield SavepointDatabase(connection)
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
