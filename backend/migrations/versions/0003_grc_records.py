"""Typed records for the GRC website: students, projects, and list items.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Privileges come from the default privileges set in 0001 and 0002: the API reads, and
# ingestion inserts and reads but never updates or deletes.


def _record_id() -> sa.Column:
    return sa.Column(
        "record_id", sa.Uuid, sa.ForeignKey("knowledge.records.record_id"), primary_key=True
    )


def upgrade() -> None:
    op.create_table(
        "student_records",
        _record_id(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("degree", sa.Text, nullable=False),
        sa.Column("graduation_year", sa.Integer, nullable=False),
        sa.Column("thesis_title_zh", sa.Text),
        sa.Column("thesis_title_en", sa.Text),
        # NULL when the page links nowhere: no link, or a link the site itself left broken.
        sa.Column("fulltext_url", sa.Text),
        sa.CheckConstraint("degree IN ('master', 'phd')", name="student_records_degree"),
        sa.CheckConstraint(
            "thesis_title_zh IS NOT NULL OR thesis_title_en IS NOT NULL",
            name="student_records_title",
        ),
        schema="knowledge",
    )
    op.create_index(
        "student_records_degree_year",
        "student_records",
        ["degree", "graduation_year"],
        schema="knowledge",
    )

    op.create_table(
        "project_records",
        _record_id(),
        sa.Column("title_zh", sa.Text),
        sa.Column("title_en", sa.Text),
        sa.Column("funder_raw", sa.Text, nullable=False),
        sa.Column("funder_zh", sa.Text),
        sa.Column("funder_en", sa.Text),
        # The period as written, always; the dates only when they form a real period. The
        # page has typos such as "2014/96/30", which no one may silently correct.
        sa.Column("period_raw", sa.Text, nullable=False),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        # Whole New Taiwan dollars, exact; the text as shown stays beside it.
        sa.Column("amount_ntd", sa.BigInteger, nullable=False),
        sa.Column("amount_raw", sa.Text, nullable=False),
        # The year the page lists the project under, which need not be the start year.
        sa.Column("year", sa.Integer, nullable=False),
        sa.CheckConstraint(
            "title_zh IS NOT NULL OR title_en IS NOT NULL", name="project_records_title"
        ),
        sa.CheckConstraint("period_start <= period_end", name="project_records_period"),
        sa.CheckConstraint("amount_ntd >= 0", name="project_records_amount"),
        schema="knowledge",
    )
    op.create_index("project_records_year", "project_records", ["year"], schema="knowledge")

    op.create_table(
        "list_item_records",
        _record_id(),
        sa.Column("list_kind", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("section_path", postgresql.ARRAY(sa.Text), nullable=False),
        # NULL when the item states no year; year_raw keeps what was read.
        sa.Column("year", sa.Integer),
        sa.Column("year_raw", sa.Text),
        sa.Column("item_text", sa.Text, nullable=False),
        sa.Column("links", postgresql.JSONB, server_default=sa.text("'[]'"), nullable=False),
        sa.CheckConstraint(
            "list_kind IN ('publication', 'profile_item')", name="list_item_records_kind"
        ),
        schema="knowledge",
    )
    op.create_index(
        "list_item_records_kind_category_year",
        "list_item_records",
        ["list_kind", "category", "year"],
        schema="knowledge",
    )


def downgrade() -> None:
    for table in ("list_item_records", "project_records", "student_records"):
        op.drop_table(table, schema="knowledge")
