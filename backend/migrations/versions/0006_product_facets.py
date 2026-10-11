"""Product facets: what the catalog states of each product, read by a model as spans.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Privileges come from the default privileges set in 0001 and 0002: the API reads, and
# ingestion inserts and reads but never updates or deletes.

# The facets a product may have (DEC-069). Their values live in code
# (wobot/knowledge/facets.py): a new value changes the prompt, not the schema.
FACETS = ("functions", "form", "places", "sensing", "viewing", "power", "users")
# The catalog text a span is read from.
SOURCE_FIELDS = ("features_text", "usage_text", "summary_text")

# A CHECK sees only its own row, and the text is in product_records: a trigger holds each
# span to the text it names, so the database itself rejects a value the catalog never
# stated, as lecture_records does with a CHECK.
SPAN_IN_TEXT = """
CREATE FUNCTION knowledge.product_facets_span_in_text() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE
    source text;
BEGIN
    SELECT CASE NEW.source_field
               WHEN 'features_text' THEN p.features_text
               WHEN 'usage_text' THEN p.usage_text
               WHEN 'summary_text' THEN p.summary_text
           END
      INTO source
      FROM knowledge.product_records AS p
     WHERE p.record_id = NEW.record_id;
    IF source IS NULL OR strpos(source, NEW.span) = 0 THEN
        RAISE EXCEPTION 'product facet % %: the span is not in %',
            NEW.facet, NEW.value, NEW.source_field
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    # Which model and prompt read the facets: NULL for a product read before facets, and
    # a new prompt is a new revision, as with lecture_records.
    op.add_column("product_records", sa.Column("facets_model", sa.Text), schema="knowledge")
    op.add_column(
        "product_records", sa.Column("facets_prompt_version", sa.Integer), schema="knowledge"
    )
    op.create_check_constraint(
        "product_records_facets_read",
        "product_records",
        "(facets_model IS NULL) = (facets_prompt_version IS NULL)",
        schema="knowledge",
    )

    # One row per value a product's text states; a facet it states nothing of has none.
    op.create_table(
        "product_facets",
        sa.Column(
            "record_id",
            sa.Uuid,
            sa.ForeignKey("knowledge.product_records.record_id"),
            primary_key=True,
        ),
        sa.Column("facet", sa.Text, primary_key=True),
        sa.Column("value", sa.Text, primary_key=True),
        # The piece of the text that states the value, and the field it is in.
        sa.Column("span", sa.Text, nullable=False),
        sa.Column("source_field", sa.Text, nullable=False),
        sa.CheckConstraint(
            "facet IN (" + ", ".join(f"'{f}'" for f in FACETS) + ")",
            name="product_facets_facet",
        ),
        sa.CheckConstraint(
            "source_field IN (" + ", ".join(f"'{f}'" for f in SOURCE_FIELDS) + ")",
            name="product_facets_source_field",
        ),
        sa.CheckConstraint("span <> ''", name="product_facets_span"),
        schema="knowledge",
    )
    # find_products asks which products have a value: 離床偵測, worn, home.
    op.create_index(
        "product_facets_facet_value", "product_facets", ["facet", "value"], schema="knowledge"
    )
    op.execute(SPAN_IN_TEXT)
    op.execute(
        "CREATE TRIGGER product_facets_span_in_text "
        "BEFORE INSERT OR UPDATE ON knowledge.product_facets "
        "FOR EACH ROW EXECUTE FUNCTION knowledge.product_facets_span_in_text()"
    )


def downgrade() -> None:
    op.drop_table("product_facets", schema="knowledge")  # drops its trigger
    op.execute("DROP FUNCTION knowledge.product_facets_span_in_text()")
    op.drop_constraint("product_records_facets_read", "product_records", schema="knowledge")
    op.drop_column("product_records", "facets_prompt_version", schema="knowledge")
    op.drop_column("product_records", "facets_model", schema="knowledge")
