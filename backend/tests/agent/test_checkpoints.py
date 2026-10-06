import asyncpg
import pytest
from langgraph.checkpoint.postgres import base

from tests.database import API_USER, MIGRATOR_USER, rolled_back
from wobot.agent.checkpoints import LIBRARY_MIGRATIONS

TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")

COLUMNS = """
    SELECT table_name, column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = $1 AND table_name = ANY($2)
    ORDER BY table_name, column_name
"""
KEYS = """
    SELECT c.relname, array_agg(a.attname ORDER BY a.attname)
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
    WHERE n.nspname = $1 AND c.relname = ANY($2)
    GROUP BY c.relname, i.indexrelid, i.indisprimary
    ORDER BY 1, 2
"""


def test_the_tables_stand_for_every_migration_of_the_pinned_library():
    assert len(base.BasePostgresSaver.MIGRATIONS) == LIBRARY_MIGRATIONS


async def test_the_library_would_find_no_migration_left_to_run():
    async with rolled_back(API_USER) as conn:
        done = await conn.fetch("SELECT v FROM app.checkpoint_migrations ORDER BY v")

    assert [row["v"] for row in done] == list(range(LIBRARY_MIGRATIONS))


async def test_the_tables_match_what_the_library_creates():
    # The library's own migrations, run in ops by the schema owner and rolled back: no
    # role may make temporary tables. Its indexes are built CONCURRENTLY, which no
    # transaction allows, so they are built plainly here.
    async with rolled_back(MIGRATOR_USER) as conn:
        await conn.execute("SET LOCAL ROLE wobot_migrator")
        await conn.execute("SET LOCAL search_path = ops")
        for sql in base.BasePostgresSaver.MIGRATIONS:
            await conn.execute(sql.replace("CONCURRENTLY ", ""))

        theirs = [tuple(r) for r in await conn.fetch(COLUMNS, "ops", list(TABLES))]
        ours = [tuple(r) for r in await conn.fetch(COLUMNS, "app", list(TABLES))]
        their_keys = [tuple(r) for r in await conn.fetch(KEYS, "ops", list(TABLES))]
        our_keys = [tuple(r) for r in await conn.fetch(KEYS, "app", list(TABLES))]

    assert len(theirs) == 23  # all four tables were made
    assert ours == theirs
    assert our_keys == their_keys


async def test_the_api_role_cannot_mark_a_migration_done():
    async with rolled_back(API_USER) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                f"INSERT INTO app.checkpoint_migrations VALUES ({LIBRARY_MIGRATIONS})"
            )
