"""The chat agent's checkpoint tables, as langgraph-checkpoint-postgres 3.1.2 leaves them.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-05

The library creates its tables in ten migrations of its own, recorded in
checkpoint_migrations. This writes their final state at once, so its setup() would find
nothing to do and is never run. Three of its indexes are built CONCURRENTLY, which cannot
run in a transaction; on new, empty tables a plain index does the same.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Privileges come from the default privileges set in 0001: the API reads and writes app
# tables. It may only read which migrations ran, so it can never mark one as done.

LIBRARY_MIGRATIONS = 10  # langgraph-checkpoint-postgres 3.1.2


def _key(*columns: sa.Column) -> list[sa.Column]:
    """The thread and namespace every checkpoint table is keyed by, then its own columns."""
    return [
        sa.Column("thread_id", sa.Text, primary_key=True),
        sa.Column("checkpoint_ns", sa.Text, primary_key=True, server_default=sa.text("''")),
        *columns,
    ]


def upgrade() -> None:
    op.create_table(
        "checkpoint_migrations",
        sa.Column("v", sa.Integer, primary_key=True, autoincrement=False),
        schema="app",
    )
    op.create_table(
        "checkpoints",
        *_key(sa.Column("checkpoint_id", sa.Text, primary_key=True)),
        sa.Column("parent_checkpoint_id", sa.Text),
        sa.Column("type", sa.Text),
        sa.Column("checkpoint", postgresql.JSONB, nullable=False),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
        schema="app",
    )
    op.create_table(
        "checkpoint_blobs",
        *_key(
            sa.Column("channel", sa.Text, primary_key=True),
            sa.Column("version", sa.Text, primary_key=True),
        ),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("blob", sa.LargeBinary),
        schema="app",
    )
    op.create_table(
        "checkpoint_writes",
        *_key(
            sa.Column("checkpoint_id", sa.Text, primary_key=True),
            sa.Column("task_id", sa.Text, primary_key=True),
            sa.Column("idx", sa.Integer, primary_key=True),
        ),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("type", sa.Text),
        sa.Column("blob", sa.LargeBinary, nullable=False),
        sa.Column("task_path", sa.Text, nullable=False, server_default=sa.text("''")),
        schema="app",
    )
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        op.create_index(f"{table}_thread_id_idx", table, ["thread_id"], schema="app")

    op.execute(
        "INSERT INTO app.checkpoint_migrations (v) "
        f"SELECT generate_series(0, {LIBRARY_MIGRATIONS - 1})"
    )
    op.execute("REVOKE INSERT, UPDATE, DELETE ON app.checkpoint_migrations FROM wobot_api")


def downgrade() -> None:
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints", "checkpoint_migrations"):
        op.drop_table(table, schema="app")
