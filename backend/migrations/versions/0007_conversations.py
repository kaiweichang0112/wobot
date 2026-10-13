"""Conversations: each account's chats, their turns, and what it calls the assistant.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13

These tables are the record the app reads; the graph keeps its own memory of a
conversation in the checkpoint tables (0005). A conversation points at one checkpoint,
the one its last completed turn left, and every turn starts from it (DEC-079): a turn
that fails or is stopped leaves the pointer where it was.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Privileges come from the default privileges set in 0001: the API reads and writes app
# tables. Nothing here needs more.

TURN_STATUSES = ("running", "completed", "cancelled", "failed")
# Longest name the assistant may be given, and longest message, in characters.
NAME_LIMIT = 40
TEXT_LIMIT = 2000


def _timestamp(name: str, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def upgrade() -> None:
    # One row per account that renamed the assistant; none means the default, Wobot.
    op.create_table(
        "account_settings",
        sa.Column(
            "account_id",
            sa.Text,
            sa.ForeignKey("app.accounts.account_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chatbot_name", sa.Text, nullable=False),
        # Compared on every update, so two devices cannot overwrite each other unseen.
        sa.Column("version", sa.Integer, nullable=False),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            f"char_length(chatbot_name) BETWEEN 1 AND {NAME_LIMIT}",
            name="account_settings_chatbot_name_length",
        ),
        sa.CheckConstraint("version > 0", name="account_settings_version_positive"),
        schema="app",
    )

    op.create_table(
        "conversations",
        sa.Column(
            "conversation_id",
            sa.Uuid,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_id",
            sa.Text,
            sa.ForeignKey("app.accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text),  # NULL until the first turn names it
        # The checkpoint the last completed turn left; NULL before the first one.
        sa.Column("head_checkpoint_id", sa.Text),
        _timestamp("created_at"),
        _timestamp("updated_at"),  # last activity: the history is listed by it
        schema="app",
    )
    # The history: one owner's conversations, most recent first, a page at a time.
    op.create_index(
        "conversations_owner_recent_idx",
        "conversations",
        ["owner_id", "updated_at", "conversation_id"],
        schema="app",
    )

    op.create_table(
        "turns",
        # Made by the app, so a request sent again finds its turn instead of adding one.
        sa.Column("turn_id", sa.Uuid, primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid,
            sa.ForeignKey("app.conversations.conversation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer, nullable=False),  # 1, 2, ... within the conversation
        sa.Column("user_text", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        # Counts the user's retries; every write that ends an attempt names it, so a late
        # one from an attempt already replaced changes nothing.
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("reply_text", sa.Text),  # what was shown: whole, or up to where it stopped
        sa.Column("reply", postgresql.JSONB),  # the lists and products, for the avatar later
        sa.Column("route", sa.Text),
        sa.Column("error_code", sa.Text),
        # What the turn ran on: the knowledge version, and the code (a git SHA), which
        # fixes every prompt and model setting.
        sa.Column("index_version", sa.Integer),
        sa.Column("app_version", sa.Text, nullable=False),
        _timestamp("created_at"),
        _timestamp("started_at"),
        _timestamp("finished_at", nullable=True),
        sa.UniqueConstraint("conversation_id", "seq", name="turns_conversation_seq"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in TURN_STATUSES) + ")",
            name="turns_status",
        ),
        sa.CheckConstraint("attempt > 0", name="turns_attempt_positive"),
        sa.CheckConstraint(
            f"char_length(user_text) BETWEEN 1 AND {TEXT_LIMIT}", name="turns_user_text_length"
        ),
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="turns_finished_unless_running"
        ),
        schema="app",
    )
    # At most one turn of a conversation generates at a time, however many devices or
    # instances try: the database refuses the second (AC-CHAT-07).
    op.create_index(
        "turns_one_running",
        "turns",
        ["conversation_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_table("turns", schema="app")
    op.drop_table("conversations", schema="app")
    op.drop_table("account_settings", schema="app")
