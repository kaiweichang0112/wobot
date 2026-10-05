"""Where conversations are kept: LangGraph's checkpoint tables, in the app schema (DB9).

The tables are created by our own migration, never by the library's `setup()`, which the
API's role may not run: no role but the migrator holds DDL. They are written in the state
the pinned langgraph-checkpoint-postgres reaches after its migrations, and declared here
so `alembic check` compares them; a test pins how many migrations that state stands for.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import Column, Index, Integer, LargeBinary, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from wobot.agent.answers import Answer, ListRef
from wobot.config import Settings
from wobot.models import Base

# The library's migrations the tables already contain: setup() would apply none.
LIBRARY_MIGRATIONS = 10

checkpoint_migrations = Table(
    "checkpoint_migrations",
    Base.metadata,
    Column("v", Integer, primary_key=True, autoincrement=False),
    schema="app",
)

checkpoints = Table(
    "checkpoints",
    Base.metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True, server_default=text("''")),
    Column("checkpoint_id", Text, primary_key=True),
    Column("parent_checkpoint_id", Text),
    Column("type", Text),
    Column("checkpoint", JSONB, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'")),
    Index("checkpoints_thread_id_idx", "thread_id"),
    schema="app",
)

checkpoint_blobs = Table(
    "checkpoint_blobs",
    Base.metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True, server_default=text("''")),
    Column("channel", Text, primary_key=True),
    Column("version", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("blob", LargeBinary),
    Index("checkpoint_blobs_thread_id_idx", "thread_id"),
    schema="app",
)

checkpoint_writes = Table(
    "checkpoint_writes",
    Base.metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True, server_default=text("''")),
    Column("checkpoint_id", Text, primary_key=True),
    Column("task_id", Text, primary_key=True),
    Column("idx", Integer, primary_key=True),
    Column("channel", Text, nullable=False),
    Column("type", Text),
    Column("blob", LargeBinary, nullable=False),
    Column("task_path", Text, nullable=False, server_default=text("''")),
    Index("checkpoint_writes_thread_id_idx", "thread_id"),
    schema="app",
)


def checkpoint_serde() -> JsonPlusSerializer:
    """Revives LangGraph's own safe types and the answer's schema, and nothing else (D2).

    Tool artifacts stay out of the state (TurnArtifacts), so no other type of ours should
    be stored; one that is comes back as plain data, with a warning a test looks for,
    rather than as a class a stored row could name.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=[Answer, ListRef])


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """A saver on its own small psycopg pool, as the settings' database user, in `app`."""
    if settings.db_mode != "local":
        # Async psycopg through the Cloud SQL connector is not settled yet (R1).
        raise RuntimeError("conversations can be kept only with db_mode=local for now")
    conninfo = make_conninfo(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        connect_timeout=int(settings.db_connect_timeout_seconds),
        options="-c search_path=app",  # the library names its tables without a schema
    )
    pool = AsyncConnectionPool(
        conninfo,
        min_size=1,
        max_size=settings.checkpoint_pool_size,
        open=False,
        # What the library expects of its connections.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    async with pool:
        yield AsyncPostgresSaver(pool, serde=checkpoint_serde())
