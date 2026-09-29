"""Knowledge core: sources, records, chunks, embeddings, index versions, ingestion runs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECORD_TYPES = (
    "product",
    "lecture",
    "student",
    "project",
    "list_item",
    "section",
    "image",
    "document_page",
)
KNOWLEDGE_TABLES = (
    "active_knowledge",
    "index_version_chunks",
    "index_version_records",
    "index_versions",
    "embeddings",
    "embedding_configs",
    "chunk_records",
    "chunks",
    "product_records",
    "records",
    "source_snapshots",
    "sources",
)


def _uuid_pk(name: str) -> sa.Column:
    return sa.Column(name, sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _timestamp(name: str, *, default_now: bool = False) -> sa.Column:
    if default_now:
        return sa.Column(
            name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        )
    return sa.Column(name, sa.DateTime(timezone=True))


def _jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB, server_default=sa.text(f"'{default}'"), nullable=False)


def upgrade() -> None:
    # Append-only by default: tables created from here on give the ingest role no UPDATE
    # or DELETE, so stored content and published versions cannot be rewritten.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge "
        "REVOKE UPDATE, DELETE ON TABLES FROM wobot_ingest"
    )

    op.create_table(
        "ingestion_runs",
        _uuid_pk("run_id"),
        sa.Column("triggered_by", sa.Text, nullable=False),
        sa.Column("policy", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("code_version", sa.Text, nullable=False),
        _jsonb("source_results", "{}"),
        _jsonb("counts", "{}"),
        _jsonb("errors", "[]"),
        _timestamp("started_at", default_now=True),
        _timestamp("finished_at"),
        sa.CheckConstraint(
            "triggered_by IN ('manual', 'schedule')", name="ingestion_runs_triggered_by"
        ),
        sa.CheckConstraint("policy IN ('publish', 'dry_run')", name="ingestion_runs_policy"),
        sa.CheckConstraint(
            "status IN ('running', 'published', 'no_change', 'validated', 'held', 'failed', "
            "'skipped_concurrent')",
            name="ingestion_runs_status",
        ),
        schema="ops",
    )
    # ops has no default privileges for the ingest role: later ops tables (deletion jobs)
    # are none of its business.
    op.execute("GRANT USAGE ON SCHEMA ops TO wobot_ingest")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ops.ingestion_runs TO wobot_ingest")

    op.create_table(
        "sources",
        sa.Column("source_id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        _timestamp("created_at", default_now=True),
        sa.CheckConstraint("kind IN ('xlsx', 'website', 'pdf')", name="sources_kind"),
        schema="knowledge",
    )
    op.create_table(
        "source_snapshots",
        _uuid_pk("snapshot_id"),
        sa.Column(
            "source_id", sa.Text, sa.ForeignKey("knowledge.sources.source_id"), nullable=False
        ),
        sa.Column("locator", sa.Text, nullable=False),
        sa.Column("content_sha256", sa.Text, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False),
        sa.Column("byte_size", sa.BigInteger, nullable=False),
        sa.Column("http_status", sa.SmallInteger),
        _jsonb("details", "{}"),
        _timestamp("fetched_at", default_now=True),
        # Identical bytes from the same place reuse one snapshot.
        sa.UniqueConstraint(
            "source_id", "locator", "content_sha256", name="source_snapshots_content"
        ),
        schema="knowledge",
    )

    op.create_table(
        "records",
        _uuid_pk("record_id"),
        sa.Column("record_type", sa.Text, nullable=False),
        sa.Column("logical_key", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("raw", postgresql.JSONB, nullable=False),
        _timestamp("created_at", default_now=True),
        sa.CheckConstraint(
            "record_type IN (" + ", ".join(f"'{t}'" for t in RECORD_TYPES) + ")",
            name="records_record_type",
        ),
        # One row per revision of an item; unchanged content is reused, not copied.
        sa.UniqueConstraint("logical_key", "content_hash", name="records_revision"),
        # Target of the composite foreign key from index_version_records.
        sa.UniqueConstraint("record_id", "logical_key", name="records_id_logical_key"),
        schema="knowledge",
    )
    op.create_table(
        "product_records",
        sa.Column(
            "record_id", sa.Uuid, sa.ForeignKey("knowledge.records.record_id"), primary_key=True
        ),
        sa.Column("product_name", sa.Text, nullable=False),
        sa.Column("company_name", sa.Text, nullable=False),
        sa.Column("company_address", sa.Text),
        sa.Column("contact_phone", sa.Text),
        sa.Column("product_url", sa.Text),
        sa.Column("product_url_text", sa.Text),
        sa.Column("features_text", sa.Text),
        sa.Column("usage_text", sa.Text),
        sa.Column("summary_text", sa.Text),
        sa.Column("category_l1_code", sa.Text),
        sa.Column("category_l1_label", sa.Text),
        sa.Column("category_l2_code", sa.Text),
        sa.Column("category_l2_label", sa.Text),
        sa.Column(
            "adoption_years",
            postgresql.ARRAY(sa.Integer),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        # NULL when the cell is empty: unknown is not the same as an empty list or 0.
        sa.Column("adoption_years_raw", sa.Text),
        schema="knowledge",
    )

    op.create_table(
        "chunks",
        _uuid_pk("chunk_id"),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("strategy", sa.Text, nullable=False),
        sa.Column("strategy_version", sa.Integer, nullable=False),
        sa.Column(
            "heading_path",
            postgresql.ARRAY(sa.Text),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("context_header", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        _jsonb("links", "[]"),
        # Exactly what was embedded, so a surprising search result can be traced.
        sa.Column("embedding_input", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False),
        _timestamp("created_at", default_now=True),
        sa.UniqueConstraint("content_hash", name="chunks_content_hash"),
        sa.CheckConstraint("token_count > 0", name="chunks_token_count_positive"),
        schema="knowledge",
    )
    op.create_table(
        "chunk_records",
        sa.Column(
            "chunk_id", sa.Uuid, sa.ForeignKey("knowledge.chunks.chunk_id"), primary_key=True
        ),
        sa.Column(
            "record_id", sa.Uuid, sa.ForeignKey("knowledge.records.record_id"), primary_key=True
        ),
        sa.Column("position", sa.Integer, nullable=False),
        schema="knowledge",
    )

    embedding_configs = op.create_table(
        "embedding_configs",
        sa.Column("embedding_config_id", sa.Text, primary_key=True),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("dimensions", sa.Integer, nullable=False),
        sa.Column("distance", sa.Text, nullable=False),
        # The embeddings column is vector(1536): other dimensions need a migration.
        sa.CheckConstraint("dimensions = 1536", name="embedding_configs_dimensions"),
        sa.CheckConstraint("distance = 'cosine'", name="embedding_configs_distance"),
        schema="knowledge",
    )
    op.bulk_insert(
        embedding_configs,
        [
            {
                "embedding_config_id": "openai/text-embedding-3-small/1536/cosine",
                "provider": "openai",
                "model": "text-embedding-3-small",
                "dimensions": 1536,
                "distance": "cosine",
            }
        ],
    )
    op.create_table(
        "embeddings",
        sa.Column(
            "chunk_id", sa.Uuid, sa.ForeignKey("knowledge.chunks.chunk_id"), primary_key=True
        ),
        sa.Column(
            "embedding_config_id",
            sa.Text,
            sa.ForeignKey("knowledge.embedding_configs.embedding_config_id"),
            primary_key=True,
        ),
        sa.Column("embedding", Vector(1536), nullable=False),
        _timestamp("created_at", default_now=True),
        schema="knowledge",
    )

    op.create_table(
        "index_versions",
        sa.Column("index_version_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column(
            "embedding_config_id",
            sa.Text,
            sa.ForeignKey("knowledge.embedding_configs.embedding_config_id"),
            nullable=False,
        ),
        sa.Column("strategies", postgresql.JSONB, nullable=False),
        sa.Column("validation_report", postgresql.JSONB),
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("ops.ingestion_runs.run_id"), nullable=False),
        _timestamp("created_at", default_now=True),
        _timestamp("validated_at"),
        _timestamp("published_at"),
        sa.CheckConstraint(
            "status IN ('building', 'validated', 'held', 'published', 'failed')",
            name="index_versions_status",
        ),
        schema="knowledge",
    )
    op.create_table(
        "index_version_records",
        sa.Column(
            "index_version_id",
            sa.BigInteger,
            sa.ForeignKey("knowledge.index_versions.index_version_id"),
            primary_key=True,
        ),
        sa.Column("record_id", sa.Uuid, primary_key=True),
        sa.Column("logical_key", sa.Text, nullable=False),
        sa.Column(
            "snapshot_id",
            sa.Uuid,
            sa.ForeignKey("knowledge.source_snapshots.snapshot_id"),
            nullable=False,
        ),
        # Where the record sits in that snapshot; rows move without the content changing.
        sa.Column("locator", postgresql.JSONB, nullable=False),
        # The copied logical_key lets the database allow one revision per item per version;
        # the composite foreign key keeps the copy equal to the record's own key.
        sa.ForeignKeyConstraint(
            ["record_id", "logical_key"],
            ["knowledge.records.record_id", "knowledge.records.logical_key"],
        ),
        sa.UniqueConstraint(
            "index_version_id", "logical_key", name="index_version_records_one_revision"
        ),
        schema="knowledge",
    )
    op.create_table(
        "index_version_chunks",
        sa.Column(
            "index_version_id",
            sa.BigInteger,
            sa.ForeignKey("knowledge.index_versions.index_version_id"),
            primary_key=True,
        ),
        sa.Column(
            "chunk_id", sa.Uuid, sa.ForeignKey("knowledge.chunks.chunk_id"), primary_key=True
        ),
        schema="knowledge",
    )

    active_knowledge = op.create_table(
        "active_knowledge",
        # Always true, so the table can never hold a second row.
        sa.Column("singleton", sa.Boolean, primary_key=True, server_default=sa.true()),
        sa.Column(
            "index_version_id",
            sa.BigInteger,
            sa.ForeignKey("knowledge.index_versions.index_version_id"),
        ),
        # Publishing is a compare-and-swap on this column.
        sa.Column("revision", sa.BigInteger, server_default=sa.text("0"), nullable=False),
        _timestamp("published_at"),
        sa.CheckConstraint("singleton", name="active_knowledge_singleton"),
        schema="knowledge",
    )
    op.bulk_insert(active_knowledge, [{"singleton": True, "revision": 0}])

    # Exceptions to append-only: version status changes and the active pointer moves.
    op.execute(
        "GRANT UPDATE ON knowledge.index_versions, knowledge.active_knowledge TO wobot_ingest"
    )
    # The pointer row and the embedding spaces come from migrations only.
    op.execute(
        "REVOKE INSERT ON knowledge.active_knowledge, knowledge.embedding_configs FROM wobot_ingest"
    )


def downgrade() -> None:
    for table in KNOWLEDGE_TABLES:
        op.drop_table(table, schema="knowledge")
    op.drop_table("ingestion_runs", schema="ops")
    op.execute("REVOKE USAGE ON SCHEMA ops FROM wobot_ingest")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA knowledge "
        "GRANT UPDATE, DELETE ON TABLES TO wobot_ingest"
    )
