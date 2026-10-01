"""Every statement ingestion runs against the database. Callers own the transactions."""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import exists, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.knowledge.chunking.drafts import ChunkDraft
from wobot.knowledge.models import (
    ActiveKnowledge,
    Chunk,
    ChunkRecord,
    Embedding,
    EmbeddingConfig,
    IndexVersion,
    IndexVersionChunk,
    IndexVersionRecord,
    IngestionRun,
    ProductRecord,
    Record,
    Source,
    SourceSnapshot,
)
from wobot.knowledge.records.products import RECORD_TYPE, ProductDraft

# A record revision by its natural identity: (logical_key, content_hash).
RecordRevision = tuple[str, str]


# --- Content: append-only, and every write is safe to repeat ---------------------------


async def put_snapshot(
    conn: AsyncConnection,
    *,
    source_id: str,
    kind: str,
    locator: str,
    sha256: str,
    storage_key: str,
    byte_size: int,
    details: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    """The snapshot row for these bytes from this place, created on first sight."""
    await conn.execute(
        insert(Source).values(source_id=source_id, kind=kind).on_conflict_do_nothing()
    )
    await conn.execute(
        insert(SourceSnapshot)
        .values(
            source_id=source_id,
            locator=locator,
            content_sha256=sha256,
            storage_key=storage_key,
            byte_size=byte_size,
            details=dict(details or {}),
        )
        .on_conflict_do_nothing(constraint="source_snapshots_content")
    )
    # DO NOTHING returns no row for an existing snapshot, and the ingest role may not use
    # DO UPDATE to force one: read the ID back instead.
    return await conn.scalar(
        select(SourceSnapshot.snapshot_id).where(
            SourceSnapshot.source_id == source_id,
            SourceSnapshot.locator == locator,
            SourceSnapshot.content_sha256 == sha256,
        )
    )


async def put_products(
    conn: AsyncConnection, drafts: Sequence[ProductDraft]
) -> tuple[dict[RecordRevision, uuid.UUID], int]:
    """Store the revisions not stored yet; return every draft's record ID and the new count."""
    inserted = await conn.execute(
        insert(Record)
        .on_conflict_do_nothing(constraint="records_revision")
        .returning(Record.record_id),
        [
            {
                "record_type": RECORD_TYPE,
                "logical_key": draft.logical_key,
                "content_hash": draft.content_hash,
                "raw": draft.raw,
            }
            for draft in drafts
        ],
    )
    new = len(inserted.all())
    revisions = [(draft.logical_key, draft.content_hash) for draft in drafts]
    rows = await conn.execute(
        select(Record.record_id, Record.logical_key, Record.content_hash).where(
            tuple_(Record.logical_key, Record.content_hash).in_(revisions)
        )
    )
    record_ids = {(row.logical_key, row.content_hash): row.record_id for row in rows}
    await conn.execute(
        insert(ProductRecord).on_conflict_do_nothing(index_elements=[ProductRecord.record_id]),
        [
            {"record_id": record_ids[(draft.logical_key, draft.content_hash)], **draft.fields}
            for draft in drafts
        ],
    )
    return record_ids, new


async def put_chunks(
    conn: AsyncConnection,
    chunks: Sequence[ChunkDraft],
    record_ids: Mapping[RecordRevision, uuid.UUID],
) -> tuple[dict[str, uuid.UUID], int]:
    """Store the chunks not stored yet and link each to its records; return IDs by hash."""
    unique = list({chunk.content_hash: chunk for chunk in chunks}.values())
    inserted = await conn.execute(
        insert(Chunk)
        .on_conflict_do_nothing(constraint="chunks_content_hash")
        .returning(Chunk.chunk_id),
        [
            {
                "content_hash": chunk.content_hash,
                "strategy": chunk.strategy,
                "strategy_version": chunk.strategy_version,
                "heading_path": list(chunk.heading_path),
                "context_header": chunk.context_header,
                "body": chunk.body,
                "links": list(chunk.links),
                "embedding_input": chunk.embedding_input,
                "token_count": chunk.token_count,
            }
            for chunk in unique
        ],
    )
    new = len(inserted.all())
    rows = await conn.execute(
        select(Chunk.chunk_id, Chunk.content_hash).where(
            Chunk.content_hash.in_([chunk.content_hash for chunk in unique])
        )
    )
    chunk_ids = {row.content_hash: row.chunk_id for row in rows}
    # A reused chunk gains a link to the new record revision; its old links stay.
    await conn.execute(
        insert(ChunkRecord).on_conflict_do_nothing(),
        [
            {
                "chunk_id": chunk_ids[chunk.content_hash],
                "record_id": record_ids[revision],
                "position": position,
            }
            for chunk in chunks
            for position, revision in enumerate(chunk.record_revisions)
        ],
    )
    return chunk_ids, new


async def embedding_config_exists(conn: AsyncConnection, config_id: str) -> bool:
    return await conn.scalar(
        select(exists().where(EmbeddingConfig.embedding_config_id == config_id))
    )


async def missing_embeddings(
    conn: AsyncConnection, chunk_ids: Iterable[uuid.UUID], config_id: str
) -> list[tuple[uuid.UUID, str]]:
    """The chunks, with their text, that have no vector in this embedding space yet."""
    rows = await conn.execute(
        select(Chunk.chunk_id, Chunk.embedding_input)
        .where(
            Chunk.chunk_id.in_(list(chunk_ids)),
            ~exists().where(
                Embedding.chunk_id == Chunk.chunk_id,
                Embedding.embedding_config_id == config_id,
            ),
        )
        .order_by(Chunk.chunk_id)
    )
    return [(row.chunk_id, row.embedding_input) for row in rows]


async def put_embeddings(
    conn: AsyncConnection, config_id: str, vectors: Mapping[uuid.UUID, list[float]]
) -> None:
    await conn.execute(
        insert(Embedding).on_conflict_do_nothing(),
        [
            {"chunk_id": chunk_id, "embedding_config_id": config_id, "embedding": vector}
            for chunk_id, vector in vectors.items()
        ],
    )


# --- Runs, versions and the active pointer ----------------------------------------------


@dataclass(frozen=True)
class ActiveState:
    """What the active version holds for one source: the baseline a run compares against."""

    index_version_id: int | None
    revision: int
    embedding_config_id: str | None
    strategies: dict[str, int]
    snapshot_sha256s: frozenset[str]
    record_revisions: frozenset[RecordRevision]
    chunk_hashes: frozenset[str]


@dataclass(frozen=True)
class Member:
    """One record in a version, and where it sat in the snapshot it came from."""

    record_id: uuid.UUID
    logical_key: str
    snapshot_id: uuid.UUID
    locator: dict[str, Any]


async def start_run(
    conn: AsyncConnection, *, triggered_by: str, policy: str, code_version: str
) -> uuid.UUID:
    return await conn.scalar(
        insert(IngestionRun)
        .values(
            triggered_by=triggered_by, policy=policy, status="running", code_version=code_version
        )
        .returning(IngestionRun.run_id)
    )


async def finish_run(
    conn: AsyncConnection,
    run_id: uuid.UUID,
    *,
    status: str,
    source_results: Mapping[str, Any],
    counts: Mapping[str, int],
    errors: Sequence[str],
) -> None:
    await conn.execute(
        update(IngestionRun)
        .where(IngestionRun.run_id == run_id)
        .values(
            status=status,
            source_results=dict(source_results),
            counts=dict(counts),
            errors=list(errors),
            finished_at=func.now(),
        )
    )


async def read_active(conn: AsyncConnection, source_id: str) -> ActiveState:
    pointer = (
        await conn.execute(select(ActiveKnowledge.index_version_id, ActiveKnowledge.revision))
    ).one()
    version_id = pointer.index_version_id
    if version_id is None:
        return ActiveState(None, pointer.revision, None, {}, frozenset(), frozenset(), frozenset())
    version = (
        await conn.execute(
            select(IndexVersion.embedding_config_id, IndexVersion.strategies).where(
                IndexVersion.index_version_id == version_id
            )
        )
    ).one()
    members = (
        await conn.execute(
            select(Record.logical_key, Record.content_hash, SourceSnapshot.content_sha256)
            .join(IndexVersionRecord, IndexVersionRecord.record_id == Record.record_id)
            .join(SourceSnapshot, SourceSnapshot.snapshot_id == IndexVersionRecord.snapshot_id)
            .where(
                IndexVersionRecord.index_version_id == version_id,
                SourceSnapshot.source_id == source_id,
            )
        )
    ).all()
    # A2 has one source, so every chunk of the version is the catalog's; A4 narrows this.
    chunk_hashes = await conn.scalars(
        select(Chunk.content_hash)
        .join(IndexVersionChunk, IndexVersionChunk.chunk_id == Chunk.chunk_id)
        .where(IndexVersionChunk.index_version_id == version_id)
    )
    return ActiveState(
        index_version_id=version_id,
        revision=pointer.revision,
        embedding_config_id=version.embedding_config_id,
        strategies=version.strategies,
        snapshot_sha256s=frozenset(member.content_sha256 for member in members),
        record_revisions=frozenset((m.logical_key, m.content_hash) for m in members),
        chunk_hashes=frozenset(chunk_hashes),
    )


async def create_version(
    conn: AsyncConnection,
    *,
    run_id: uuid.UUID,
    config_id: str,
    strategies: Mapping[str, int],
    members: Sequence[Member],
    chunk_ids: Iterable[uuid.UUID],
) -> int:
    """A building version listing every record and chunk it holds, changed or not."""
    version_id = await conn.scalar(
        insert(IndexVersion)
        .values(
            status="building",
            embedding_config_id=config_id,
            strategies=dict(strategies),
            run_id=run_id,
        )
        .returning(IndexVersion.index_version_id)
    )
    await conn.execute(
        insert(IndexVersionRecord),
        [{"index_version_id": version_id, **asdict(member)} for member in members],
    )
    await conn.execute(
        insert(IndexVersionChunk),
        [{"index_version_id": version_id, "chunk_id": chunk_id} for chunk_id in chunk_ids],
    )
    return version_id


# Counts that must hold for any version, read from what was stored rather than trusted
# from memory.
_VERSION_INTEGRITY = text(
    """
    SELECT
      (SELECT count(*) FROM knowledge.index_version_records
        WHERE index_version_id = :version) AS records,
      (SELECT count(*) FROM knowledge.index_version_chunks
        WHERE index_version_id = :version) AS chunks,
      (SELECT count(*) FROM knowledge.index_version_records AS member
        WHERE member.index_version_id = :version
          AND NOT EXISTS (
            SELECT 1 FROM knowledge.chunk_records AS link
            JOIN knowledge.index_version_chunks AS chunk
              ON chunk.chunk_id = link.chunk_id AND chunk.index_version_id = :version
            WHERE link.record_id = member.record_id)) AS records_without_chunk,
      (SELECT count(*) FROM knowledge.index_version_chunks AS chunk
        WHERE chunk.index_version_id = :version
          AND NOT EXISTS (
            SELECT 1 FROM knowledge.embeddings AS embedding
            WHERE embedding.chunk_id = chunk.chunk_id
              AND embedding.embedding_config_id = :config)) AS chunks_without_embedding
    """
)


async def version_integrity(
    conn: AsyncConnection, version_id: int, config_id: str
) -> dict[str, int]:
    row = await conn.execute(_VERSION_INTEGRITY, {"version": version_id, "config": config_id})
    return dict(row.one()._mapping)


async def set_version_status(
    conn: AsyncConnection, version_id: int, status: str, report: Mapping[str, Any]
) -> None:
    await conn.execute(
        update(IndexVersion)
        .where(IndexVersion.index_version_id == version_id)
        .values(status=status, validation_report=dict(report), validated_at=func.now())
    )


async def publish(conn: AsyncConnection, version_id: int, expected_revision: int) -> bool:
    """Point active knowledge at the version, unless another run published first.

    Compare-and-swap: the update matches only while the revision is the one this run read
    at its start, so of two runs that read the same revision, one publishes and one fails.
    """
    moved = await conn.execute(
        update(ActiveKnowledge)
        .where(ActiveKnowledge.revision == expected_revision)
        .values(
            index_version_id=version_id,
            revision=ActiveKnowledge.revision + 1,
            published_at=func.now(),
        )
    )
    if moved.rowcount != 1:
        return False
    await conn.execute(
        update(IndexVersion)
        .where(IndexVersion.index_version_id == version_id)
        .values(status="published", published_at=func.now())
    )
    return True


# --- Status, for people -----------------------------------------------------------------


async def read_pointer(conn: AsyncConnection) -> Any:
    """The active version (None before the first publish), its revision and embedding space."""
    return (
        await conn.execute(
            select(
                ActiveKnowledge.index_version_id,
                ActiveKnowledge.revision,
                ActiveKnowledge.published_at,
                IndexVersion.embedding_config_id,
            ).outerjoin(
                IndexVersion, IndexVersion.index_version_id == ActiveKnowledge.index_version_id
            )
        )
    ).one()


async def recent_runs(conn: AsyncConnection, limit: int) -> list[Any]:
    rows = await conn.execute(
        select(
            IngestionRun.started_at,
            IngestionRun.status,
            IngestionRun.policy,
            IngestionRun.counts,
            IngestionRun.errors,
        )
        .order_by(IngestionRun.started_at.desc())
        .limit(limit)
    )
    return list(rows)
