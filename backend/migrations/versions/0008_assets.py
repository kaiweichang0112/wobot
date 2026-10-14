"""Assets: each account's cloned voice and generated avatar, as versions.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14

A version row is also the job that makes it (DEC-086): the worker claims it by moving
its status, and records the execution it runs in and a heartbeat. The rules that must
hold whichever instance or job writes are the database's: one active version per
account, and one being made, as partial unique indexes like `turns_one_running`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Made: queued -> running -> ready | failed. Used: ready -> active -> retired.
# Removed: deleted; cleaned_at records that the provider's copy and the files are gone.
STATUSES = ("queued", "running", "ready", "failed", "active", "retired", "deleted")
MAKING = ("queued", "running")
FRAME_STATUSES = ("pending", "running", "ready", "failed")
QUALITIES = ("low", "medium", "high")
# Frozen copy of wobot.assets.frames.FRAME_KEYS at expression schema version 1.
FRAME_KEYS = tuple(
    f"{expression}_{mouth}"
    for expression in ("neutral", "joy", "sadness", "anger", "fear", "surprise", "thinking")
    for mouth in ("closed", "half", "wide")
)
TABLES = ("voice_versions", "avatar_versions", "avatar_frames")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _timestamp(name: str, nullable: bool = False) -> sa.Column:
    default = None if nullable else sa.func.now()
    return sa.Column(name, sa.DateTime(timezone=True), server_default=default, nullable=nullable)


def _version_columns() -> list[sa.Column]:
    """What every version keeps about the job that made it."""
    return [
        sa.Column(
            "owner_id",
            sa.Text,
            sa.ForeignKey("app.accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("execution", sa.Text),  # the Cloud Run job execution working on it
        _timestamp("heartbeat_at", nullable=True),  # a stale one means the worker died
        sa.Column("error_code", sa.Text),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("started_at", nullable=True),
        _timestamp("finished_at", nullable=True),
        _timestamp("cleaned_at", nullable=True),
    ]


def _version_checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(_in("status", STATUSES), name=f"{table}_status"),
        sa.CheckConstraint(
            f"({_in('status', MAKING)}) = (finished_at IS NULL)",
            name=f"{table}_finished_unless_making",
        ),
        sa.CheckConstraint(
            "status <> 'running' OR started_at IS NOT NULL", name=f"{table}_started_when_running"
        ),
        sa.CheckConstraint(
            f"cleaned_at IS NULL OR {_in('status', ('failed', 'retired', 'deleted'))}",
            name=f"{table}_cleaned_when_unused",
        ),
    ]


def _version_indexes(table: str) -> None:
    # The account's versions, newest first: what /v1/me reports.
    op.create_index(f"{table}_owner_recent_idx", table, ["owner_id", "created_at"], schema="app")
    # Activation retires the old version before it activates the new one, in one
    # transaction: a unique index is checked row by row and cannot be deferred.
    op.create_index(
        f"{table}_one_active",
        table,
        ["owner_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("status = 'active'"),
    )
    # A second request to make one while another is being made is refused, whichever
    # device or instance sends it.
    op.create_index(
        f"{table}_one_making",
        table,
        ["owner_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text(_in("status", MAKING)),
    )


def upgrade() -> None:
    op.create_table(
        "voice_versions",
        # Made by the app, so a request sent again finds its version instead of adding one.
        sa.Column("voice_id", sa.Uuid, primary_key=True),
        *_version_columns(),
        # ElevenLabs' ID; the voice is named after voice_id, so a retry finds it there.
        sa.Column("provider_voice_id", sa.Text, unique=True),
        sa.Column("sample_seconds", sa.Float, nullable=False),
        *_version_checks("voice_versions"),
        sa.CheckConstraint(
            "status NOT IN ('ready', 'active') OR provider_voice_id IS NOT NULL",
            name="voice_versions_cloned_when_usable",
        ),
        sa.CheckConstraint("sample_seconds > 0", name="voice_versions_sample_seconds_positive"),
        schema="app",
    )
    _version_indexes("voice_versions")

    op.create_table(
        "avatar_versions",
        sa.Column("avatar_id", sa.Uuid, primary_key=True),
        *_version_columns(),
        # What made the frames: changing any of them makes a different avatar.
        sa.Column("expression_schema_version", sa.Integer, nullable=False),
        sa.Column("prompt_version", sa.Integer, nullable=False),
        sa.Column("prompt_hash", sa.Text, nullable=False),
        sa.Column("image_model", sa.Text, nullable=False),
        sa.Column("quality", sa.Text, nullable=False),
        *_version_checks("avatar_versions"),
        sa.CheckConstraint(
            "expression_schema_version > 0", name="avatar_versions_expression_schema_positive"
        ),
        sa.CheckConstraint("prompt_version > 0", name="avatar_versions_prompt_version_positive"),
        sa.CheckConstraint(_in("quality", QUALITIES), name="avatar_versions_quality"),
        schema="app",
    )
    _version_indexes("avatar_versions")

    # One row per frame, all 21 added with the version. Which frame each is made from
    # follows from its key (spec 21), so it is not stored.
    op.create_table(
        "avatar_frames",
        sa.Column(
            "avatar_id",
            sa.Uuid,
            sa.ForeignKey("app.avatar_versions.avatar_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("frame_key", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("storage_key", sa.Text),
        sa.Column("sha256", sa.Text),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        # From the image API's usage: what the frame cost.
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("error_code", sa.Text),
        _timestamp("updated_at"),
        sa.CheckConstraint(_in("frame_key", FRAME_KEYS), name="avatar_frames_frame_key"),
        sa.CheckConstraint(_in("status", FRAME_STATUSES), name="avatar_frames_status"),
        sa.CheckConstraint(
            "status <> 'ready' OR (storage_key IS NOT NULL AND sha256 IS NOT NULL)",
            name="avatar_frames_stored_when_ready",
        ),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="avatar_frames_sha256_hex"),
        sa.CheckConstraint("attempts >= 0", name="avatar_frames_attempts_not_negative"),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0", name="avatar_frames_tokens_not_negative"
        ),
        schema="app",
    )

    # The API gets the rest from the default privileges set in 0001. The asset worker may
    # only read and move the rows the API added: it cannot add or remove one, and reads
    # nothing else, the conversations least of all. Its role comes from the bootstrap.
    op.execute("GRANT USAGE ON SCHEMA app TO wobot_assets")
    op.execute(f"GRANT SELECT, UPDATE ON {', '.join(f'app.{t}' for t in TABLES)} TO wobot_assets")


def downgrade() -> None:
    op.drop_table("avatar_frames", schema="app")
    op.drop_table("avatar_versions", schema="app")
    op.drop_table("voice_versions", schema="app")
    op.execute("REVOKE USAGE ON SCHEMA app FROM wobot_assets")
