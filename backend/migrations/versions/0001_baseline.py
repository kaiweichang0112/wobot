"""Baseline: accounts, email allowlist and role privileges.

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The bootstrap only created the schemas; their owner decides who may use them.
    op.execute("GRANT USAGE ON SCHEMA app TO wobot_api")
    op.execute("GRANT USAGE ON SCHEMA knowledge TO wobot_api, wobot_ingest")
    # Default privileges apply to tables the owner creates from now on.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO wobot_api"
    )
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT USAGE ON SEQUENCES TO wobot_api")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge GRANT SELECT ON TABLES TO wobot_api")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO wobot_ingest"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge GRANT USAGE ON SEQUENCES TO wobot_ingest"
    )

    op.create_table(
        "accounts",
        sa.Column("account_id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema="app",
    )
    op.create_table(
        "allowed_emails",
        sa.Column("email", sa.Text, primary_key=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("email = lower(email)", name="allowed_emails_email_lowercase"),
        schema="app",
    )
    # The API only reads the allowlist; maintainers edit it.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON app.allowed_emails FROM wobot_api")


def downgrade() -> None:
    op.drop_table("allowed_emails", schema="app")
    op.drop_table("accounts", schema="app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge REVOKE USAGE ON SEQUENCES FROM wobot_ingest"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM wobot_ingest"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge REVOKE SELECT ON TABLES FROM wobot_api"
    )
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE USAGE ON SEQUENCES FROM wobot_api")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA app "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM wobot_api"
    )
    op.execute("REVOKE USAGE ON SCHEMA knowledge FROM wobot_api, wobot_ingest")
    op.execute("REVOKE USAGE ON SCHEMA app FROM wobot_api")
