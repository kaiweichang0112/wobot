"""Account deletion: an account being deleted keeps its row, marked, until what it owned
outside the database is gone (spec 04).

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16

Only a nullable column is added: the API revision before this one never names it and
goes on as it did (expand first). wobot_api may already update and delete accounts
(0001's default privileges), so no grant changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Set once, when the owner asks: every request of the account is refused from then
    # on, and the row goes when nothing the account owned is left outside.
    op.add_column("accounts", sa.Column("deleted_at", sa.DateTime(timezone=True)), schema="app")


def downgrade() -> None:
    op.drop_column("accounts", "deleted_at", schema="app")
