"""Tables of the shared knowledge corpus and its ingestion runs; migrations define them."""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wobot.models import Base

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
EMBEDDING_DIMENSIONS = 1536


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "triggered_by IN ('manual', 'schedule')", name="ingestion_runs_triggered_by"
        ),
        CheckConstraint("policy IN ('publish', 'dry_run')", name="ingestion_runs_policy"),
        CheckConstraint(
            "status IN ('running', 'published', 'no_change', 'validated', 'held', 'failed', "
            "'skipped_concurrent')",
            name="ingestion_runs_status",
        ),
        {"schema": "ops"},
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    triggered_by: Mapped[str] = mapped_column(Text)
    policy: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    code_version: Mapped[str] = mapped_column(Text)
    source_results: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint("kind IN ('xlsx', 'website', 'pdf')", name="sources_kind"),
        {"schema": "knowledge"},
    )

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        UniqueConstraint("source_id", "locator", "content_sha256", name="source_snapshots_content"),
        {"schema": "knowledge"},
    )

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("knowledge.sources.source_id"))
    locator: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    http_status: Mapped[int | None] = mapped_column(SmallInteger)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (
        CheckConstraint(
            "record_type IN (" + ", ".join(f"'{t}'" for t in RECORD_TYPES) + ")",
            name="records_record_type",
        ),
        UniqueConstraint("logical_key", "content_hash", name="records_revision"),
        UniqueConstraint("record_id", "logical_key", name="records_id_logical_key"),
        {"schema": "knowledge"},
    )

    record_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    record_type: Mapped[str] = mapped_column(Text)
    logical_key: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductRecord(Base):
    __tablename__ = "product_records"
    __table_args__ = {"schema": "knowledge"}

    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge.records.record_id"), primary_key=True
    )
    product_name: Mapped[str] = mapped_column(Text)
    company_name: Mapped[str] = mapped_column(Text)
    company_address: Mapped[str | None] = mapped_column(Text)
    contact_phone: Mapped[str | None] = mapped_column(Text)
    product_url: Mapped[str | None] = mapped_column(Text)
    product_url_text: Mapped[str | None] = mapped_column(Text)
    features_text: Mapped[str | None] = mapped_column(Text)
    usage_text: Mapped[str | None] = mapped_column(Text)
    summary_text: Mapped[str | None] = mapped_column(Text)
    category_l1_code: Mapped[str | None] = mapped_column(Text)
    category_l1_label: Mapped[str | None] = mapped_column(Text)
    category_l2_code: Mapped[str | None] = mapped_column(Text)
    category_l2_label: Mapped[str | None] = mapped_column(Text)
    adoption_years: Mapped[list[int]] = mapped_column(ARRAY(Integer), server_default=text("'{}'"))
    adoption_years_raw: Mapped[str | None] = mapped_column(Text)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("content_hash", name="chunks_content_hash"),
        CheckConstraint("token_count > 0", name="chunks_token_count_positive"),
        {"schema": "knowledge"},
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    content_hash: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str] = mapped_column(Text)
    strategy_version: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    context_header: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    links: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    embedding_input: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChunkRecord(Base):
    __tablename__ = "chunk_records"
    __table_args__ = {"schema": "knowledge"}

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge.chunks.chunk_id"), primary_key=True
    )
    record_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge.records.record_id"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer)


class EmbeddingConfig(Base):
    __tablename__ = "embedding_configs"
    __table_args__ = (
        CheckConstraint(
            f"dimensions = {EMBEDDING_DIMENSIONS}", name="embedding_configs_dimensions"
        ),
        CheckConstraint("distance = 'cosine'", name="embedding_configs_distance"),
        {"schema": "knowledge"},
    )

    embedding_config_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int] = mapped_column(Integer)
    distance: Mapped[str] = mapped_column(Text)


class Embedding(Base):
    __tablename__ = "embeddings"
    __table_args__ = {"schema": "knowledge"}

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge.chunks.chunk_id"), primary_key=True
    )
    embedding_config_id: Mapped[str] = mapped_column(
        Text, ForeignKey("knowledge.embedding_configs.embedding_config_id"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexVersion(Base):
    __tablename__ = "index_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('building', 'validated', 'held', 'published', 'failed')",
            name="index_versions_status",
        ),
        {"schema": "knowledge"},
    )

    index_version_id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text)
    embedding_config_id: Mapped[str] = mapped_column(
        Text, ForeignKey("knowledge.embedding_configs.embedding_config_id")
    )
    strategies: Mapped[dict[str, Any]] = mapped_column(JSONB)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ops.ingestion_runs.run_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexVersionRecord(Base):
    __tablename__ = "index_version_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["record_id", "logical_key"],
            ["knowledge.records.record_id", "knowledge.records.logical_key"],
        ),
        UniqueConstraint(
            "index_version_id", "logical_key", name="index_version_records_one_revision"
        ),
        {"schema": "knowledge"},
    )

    index_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.index_versions.index_version_id"), primary_key=True
    )
    record_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    logical_key: Mapped[str] = mapped_column(Text)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge.source_snapshots.snapshot_id")
    )
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB)


class IndexVersionChunk(Base):
    __tablename__ = "index_version_chunks"
    __table_args__ = {"schema": "knowledge"}

    index_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge.index_versions.index_version_id"), primary_key=True
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge.chunks.chunk_id"), primary_key=True
    )


class ActiveKnowledge(Base):
    __tablename__ = "active_knowledge"
    __table_args__ = (
        CheckConstraint("singleton", name="active_knowledge_singleton"),
        {"schema": "knowledge"},
    )

    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, server_default=true())
    index_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge.index_versions.index_version_id")
    )
    revision: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
