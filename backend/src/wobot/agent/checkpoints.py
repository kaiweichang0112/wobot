"""Where conversations are kept: LangGraph's checkpoint tables, in the app schema (DB9).

The tables are created by our own migration, never by the library's `setup()`, which the
API's role may not run: no role but the migrator holds DDL. They are written in the state
the pinned langgraph-checkpoint-postgres reaches after its migrations, and declared here
so `alembic check` compares them; a test pins how many migrations that state stands for.

The graph saves its state after every node, under the conversation's thread ID, and reads
it back when that thread is called again: what the freeCodeCamp course's Memory_Agent
kept by hand in `conversation_history`.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import google.auth
import google.auth.transport.requests
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import Column, Index, Integer, LargeBinary, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

from wobot.config import Settings
from wobot.models import Base

# The library's migrations the tables already contain: setup() would apply none.
LIBRARY_MIGRATIONS = 10
# The scope Cloud SQL's IAM database login accepts.
SQL_LOGIN_SCOPE = "https://www.googleapis.com/auth/sqlservice.login"
# What the library expects of its connections.
CONNECTION = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}

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
    """Revives LangGraph's own safe types, such as messages and datetimes, and nothing else.

    The chat state holds no class of ours: what the tools found is kept as plain dicts.
    A type of ours that slipped in would be blocked, with a warning a test looks for,
    rather than revived as a class a stored row could name.
    """
    # None: LangGraph's safe types only. The default allows any type, with a warning.
    return JsonPlusSerializer(allowed_msgpack_modules=None)


class IamPassword:
    """An IAM access token to log in with, refreshed when it has expired (R1).

    Cloud SQL checks the token only when a connection logs in, so an hour's lifetime
    never ends a connection already open. The Python Connector does this for asyncpg;
    it has no async psycopg, so the checkpointer does it itself."""

    def __init__(self, credentials: Any = None) -> None:
        if credentials is None:
            credentials, _ = google.auth.default(scopes=[SQL_LOGIN_SCOPE])
        self._credentials = credentials

    async def __call__(self) -> str:
        if not self._credentials.valid:
            request = google.auth.transport.requests.Request()
            # google-auth is synchronous: refresh off the event loop.
            await asyncio.to_thread(self._credentials.refresh, request)
        return self._credentials.token


def connection_args(
    settings: Settings, password: Callable[[], Awaitable[str]] | None = None
) -> tuple[str, dict[str, Any] | Callable[[], Awaitable[dict[str, Any]]]]:
    """Where the pool connects and with what. Locally, the compose database's password.
    On Cloud Run, the socket its Cloud SQL connection opens, whose proxy encrypts the
    link, and a fresh IAM token for each new connection: the pool calls the kwargs
    function every time it connects."""
    common = {
        "dbname": settings.db_name,
        "user": settings.db_user,
        "connect_timeout": int(settings.db_connect_timeout_seconds),
        "options": "-c search_path=app",  # the library names its tables without a schema
    }
    if settings.db_mode == "local":
        conninfo = make_conninfo(
            host=settings.db_host, port=settings.db_port, password=settings.db_password, **common
        )
        return conninfo, CONNECTION
    socket = f"{settings.cloudsql_socket_dir}/{settings.instance_connection_name}"
    token = password or IamPassword()

    async def kwargs() -> dict[str, Any]:
        return {**CONNECTION, "password": await token()}

    return make_conninfo(host=socket, **common), kwargs


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """A saver on its own small psycopg pool, as the settings' database user, in `app`."""
    conninfo, kwargs = connection_args(settings)
    pool = AsyncConnectionPool(
        conninfo, min_size=1, max_size=settings.checkpoint_pool_size, open=False, kwargs=kwargs
    )
    async with pool:
        yield AsyncPostgresSaver(pool, serde=checkpoint_serde())
