"""One ingestion run at a time, wherever it starts: the job, a retry, a developer's laptop.

Publishing is a compare-and-swap, so two runs can never both publish; but both would pay
for the same model calls and embeddings, and one would fail at the end. A run first takes
a PostgreSQL advisory lock; one that cannot is recorded as skipped and does nothing.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

# Any number, as long as every run uses the same one.
RUN_LOCK = 7_301_920_461


@asynccontextmanager
async def exclusive_run(engine: AsyncEngine) -> AsyncIterator[bool]:
    """Whether this run holds the lock, kept until the block ends.

    The lock belongs to a session, so it is taken on a connection of its own, kept open
    and outside any transaction for the whole run. A crash closes the connection and
    frees the lock.
    """
    async with engine.connect() as connection:
        session = await connection.execution_options(isolation_level="AUTOCOMMIT")
        held = bool(
            await session.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": RUN_LOCK})
        )
        try:
            yield held
        finally:
            if held:
                await session.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": RUN_LOCK})
