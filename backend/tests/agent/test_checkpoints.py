import operator
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, TypedDict

import asyncpg
import pytest
from langgraph.checkpoint.postgres import base
from langgraph.graph import END, START, StateGraph

from tests.database import API_USER, MIGRATOR_USER, connect, rolled_back
from wobot.agent.checkpoints import LIBRARY_MIGRATIONS, open_checkpointer
from wobot.config import get_settings

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


class Counted(TypedDict):
    said: Annotated[list[str], operator.add]


@asynccontextmanager
async def saved_thread():
    """A saver as the API's role, and a new thread that is deleted afterwards."""
    async with open_checkpointer(get_settings()) as saver:
        thread_id = f"test-{uuid.uuid4()}"
        try:
            yield saver, {"configurable": {"thread_id": thread_id}}
        finally:
            await saver.adelete_thread(thread_id)
    check = await connect(API_USER)
    try:
        left = await check.fetchval(
            "SELECT count(*) FROM app.checkpoints WHERE thread_id = $1", thread_id
        )
    finally:
        await check.close()
    assert left == 0


async def test_the_api_role_keeps_a_conversation_across_turns():
    graph = StateGraph(Counted)
    graph.add_node("echo", lambda state: {"said": ["ok"]})
    graph.add_edge(START, "echo")
    graph.add_edge("echo", END)

    async with saved_thread() as (saver, config):
        app = graph.compile(checkpointer=saver)
        await app.ainvoke({"said": ["hi"]}, config)
        state = await app.ainvoke({"said": ["again"]}, config)

    assert state["said"] == ["hi", "ok", "again", "ok"]


async def test_conversations_wait_for_cloud_sql_support():
    settings = get_settings().model_copy(update={"db_mode": "cloudsql"})

    with pytest.raises(RuntimeError, match="db_mode=local"):
        async with open_checkpointer(settings):
            pass
