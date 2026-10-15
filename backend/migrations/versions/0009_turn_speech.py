"""Turn speech: a turn asked in Avatar mode, and its reply spoken (DEC-089).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-15

Only columns are added, with a default for the rows there are: the API revision before
this one never names them and goes on as it did (expand first).

Speech is a layer over the reply, not part of how the turn ends: the text completes the
turn and moves the conversation's head, and the speech is kept beside it, with how far
the app played it and when it was stopped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column("mode", sa.Text, nullable=False, server_default=sa.text("'text'")),
        schema="app",
    )
    # The sentences spoken: their text and feeling, and the voice version that spoke them.
    op.add_column("turns", sa.Column("speech", JSONB), schema="app")
    # How many of them the app played to the end; it only moves forward.
    op.add_column("turns", sa.Column("heard_upto", sa.Integer), schema="app")
    # When the app stopped the speech (the user interrupted): no sentence is spoken after.
    op.add_column("turns", sa.Column("speech_stopped_at", sa.DateTime(timezone=True)), schema="app")
    op.create_check_constraint("turns_mode", "turns", "mode IN ('text', 'avatar')", schema="app")
    op.create_check_constraint(
        "turns_heard_upto_positive", "turns", "heard_upto >= 0", schema="app"
    )


def downgrade() -> None:
    op.drop_constraint("turns_heard_upto_positive", "turns", schema="app")
    op.drop_constraint("turns_mode", "turns", schema="app")
    for column in ("speech_stopped_at", "heard_upto", "speech", "mode"):
        op.drop_column("turns", column, schema="app")
