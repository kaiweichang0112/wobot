import logging
import uuid
from contextlib import asynccontextmanager

import asyncpg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres import base

from tests.agent.fakes import calls, fake_models
from tests.agent.test_graph import turn
from tests.database import API_USER, MIGRATOR_USER, connect, rolled_back
from wobot.agent.checkpoints import LIBRARY_MIGRATIONS, open_checkpointer
from wobot.agent.graph import build_graph
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


# --- Conversations kept across turns ----------------------------------------------------


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
    models = fake_models("chat", chat=["你好！", "我記得。"])

    async with saved_thread() as (saver, config):
        app = build_graph(models, db=None, embedder=None, checkpointer=saver)
        await app.ainvoke(turn("你好"), config)
        state = await app.ainvoke(turn("你記得我說過什麼嗎？"), config)

    assert [m.text for m in state["messages"]] == [
        "你好",
        "你好！",
        "你記得我說過什麼嗎？",
        "我記得。",
    ]
    # The router read the first turn too: the conversation came back from the database.
    [_, (seen, _)] = models.router.calls
    assert [m.text for m in seen] == ["你好", "你好！", "你記得我說過什麼嗎？"]


async def test_a_thread_is_taken_up_again_by_a_new_process():
    async with saved_thread() as (saver, config):
        first = build_graph(fake_models("chat", chat=["你好！"]), None, None, saver)
        await first.ainvoke(turn("你好"), config)

        # A new saver on a new pool, as after a restart, and a graph built anew.
        async with open_checkpointer(get_settings()) as again:
            later = build_graph(fake_models("chat", chat=["還在。"]), None, None, again)
            state = await later.ainvoke(turn("還在嗎？"), config)

    assert [m.text for m in state["messages"]] == ["你好", "你好！", "還在嗎？", "還在。"]


async def test_a_turn_keeps_the_conversation_but_not_the_last_turns_work(knowledge):
    models = fake_models("list", list_calls=[calls(("list_students", {"degree": "master"}))])

    async with saved_thread() as (saver, config):
        app = build_graph(models, knowledge.db, knowledge.embedder, saver)
        listed = await app.ainvoke(turn("列出碩士畢業生", knowledge.version_id), config)
        models.router.route = "chat"
        state = await app.ainvoke(turn("謝謝", knowledge.version_id), config)

    assert listed["list_results"] and listed["tool_rounds"] == 1
    assert state["list_results"] == {} and state["list_messages"] == []
    assert state["tool_rounds"] == 0 and state["reply"] == {"text": "你好！"}
    assert [type(m).__name__ for m in state["messages"]] == [
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
        "AIMessage",
    ]


async def test_a_stored_turn_reads_back_as_safe_types_alone(knowledge, caplog):
    models = fake_models("list", list_calls=[calls(("list_students", {"degree": "master"}))])

    async with saved_thread() as (saver, config):
        app = build_graph(models, knowledge.db, knowledge.embedder, saver)
        await app.ainvoke(turn("列出碩士畢業生", knowledge.version_id), config)
        with caplog.at_level(logging.WARNING, logger="langgraph"):
            state = (await app.aget_state(config)).values

    assert isinstance(state["messages"][0], HumanMessage)
    assert state["query_time"].tzinfo is not None
    assert [r for r in caplog.records if r.name.startswith("langgraph")] == []


async def test_conversations_wait_for_cloud_sql_support():
    settings = get_settings().model_copy(update={"db_mode": "cloudsql"})

    with pytest.raises(RuntimeError, match="db_mode=local"):
        async with open_checkpointer(settings):
            pass
