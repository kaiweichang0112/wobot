"""Lecture records, and the cache of answers a language model gave during ingestion.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Privileges come from the default privileges set in 0001 and 0002: the API reads, and
# ingestion inserts and reads but never updates or deletes.

# Fields a model reads from an entry. Each is a verbatim span of the entry or NULL, so the
# database itself rejects a value the source never stated.
MODEL_READ_FIELDS = ("title", "event", "location")


def upgrade() -> None:
    op.create_table(
        "lecture_records",
        sa.Column(
            "record_id", sa.Uuid, sa.ForeignKey("knowledge.records.record_id"), primary_key=True
        ),
        sa.Column("speaker", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        # The block the page lists the talk under, as written: "2024~2025".
        sa.Column("year_block", sa.Text, nullable=False),
        # The entry as shown, link labels removed: what the model read.
        sa.Column("entry_text", sa.Text, nullable=False),
        *(sa.Column(name, sa.Text) for name in MODEL_READ_FIELDS),
        sa.Column("lecture_date", sa.Date),
        sa.Column("date_precision", sa.Text),
        sa.Column("date_raw", sa.Text),
        # The talk's own year, from its date; the block's when the date is missing.
        sa.Column("year", sa.Integer),
        # NULL when there is no slide link, or the page's link leads nowhere.
        sa.Column("pdf_url", sa.Text),
        sa.Column("links", postgresql.JSONB, server_default=sa.text("'[]'"), nullable=False),
        # Which model and prompt read the fields: a new prompt is a new revision.
        sa.Column("extraction_model", sa.Text, nullable=False),
        sa.Column("extraction_prompt_version", sa.Integer, nullable=False),
        sa.CheckConstraint("category IN ('keynote', 'invited')", name="lecture_records_category"),
        sa.CheckConstraint(
            "date_precision IN ('day', 'month')", name="lecture_records_date_precision"
        ),
        sa.CheckConstraint(
            "(lecture_date IS NULL) = (date_precision IS NULL)",
            name="lecture_records_date",
        ),
        *(
            sa.CheckConstraint(
                f"{name} IS NULL OR ({name} <> '' AND strpos(entry_text, {name}) > 0)",
                name=f"lecture_records_{name}_in_entry",
            )
            for name in MODEL_READ_FIELDS
        ),
        schema="knowledge",
    )
    op.create_index(
        "lecture_records_category_year",
        "lecture_records",
        ["category", "year"],
        schema="knowledge",
    )

    # One answer per question: the same input, model and prompt version are never paid
    # for twice. A refusal is cached too, so a bad entry is not retried every week.
    op.create_table(
        "llm_extractions",
        sa.Column("purpose", sa.Text, primary_key=True),
        sa.Column("input_hash", sa.Text, primary_key=True),
        sa.Column("model", sa.Text, primary_key=True),
        sa.Column("prompt_version", sa.Integer, primary_key=True),
        sa.Column("input", postgresql.JSONB, nullable=False),
        # The structured output as the model gave it, before any check against the input.
        sa.Column("output", postgresql.JSONB),
        sa.Column("failure", sa.Text),
        # The snapshot that answered, which a model alias may move between.
        sa.Column("response_model", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("(output IS NULL) <> (failure IS NULL)", name="llm_extractions_outcome"),
        schema="knowledge",
    )


def downgrade() -> None:
    op.drop_table("llm_extractions", schema="knowledge")
    op.drop_table("lecture_records", schema="knowledge")
