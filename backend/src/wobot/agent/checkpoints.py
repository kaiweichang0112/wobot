"""Where conversations are kept: LangGraph's checkpoint tables, in the app schema (DB9).

The tables are created by our own migration, never by the library's `setup()`, which the
API's role may not run: no role but the migrator holds DDL. They are written in the state
the pinned langgraph-checkpoint-postgres reaches after its migrations, and declared here
so `alembic check` compares them; a test pins how many migrations that state stands for.
"""

from sqlalchemy import Column, Index, Integer, LargeBinary, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB

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
