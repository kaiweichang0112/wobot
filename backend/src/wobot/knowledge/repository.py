"""Every statement ingestion runs against the database. Callers own the transactions."""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from typing import Any, Protocol

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
    LectureRecord,
    ListItemRecord,
    LlmExtraction,
    ProductRecord,
    ProjectRecord,
    Record,
    Source,
    SourceSnapshot,
    StudentRecord,
)
from wobot.knowledge.records.drafts import RecordDraft


class Database(Protocol):
    """Where each step's transaction comes from: an AsyncEngine, or a test's savepoints."""

    def begin(self) -> AbstractAsyncContextManager[AsyncConnection]: ...


# A record revision by its natural identity: (logical_key, content_hash).
RecordRevision = tuple[str, str]
# Each record type's own table; a section has none, its text lives in records.raw.
TYPED_TABLES = {
    "product": ProductRecord,
    "student": StudentRecord,
    "project": ProjectRecord,
    "list_item": ListItemRecord,
    "lecture": LectureRecord,
}


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


async def put_records(
    conn: AsyncConnection, drafts: Sequence[RecordDraft]
) -> tuple[dict[RecordRevision, uuid.UUID], int]:
    """Store the revisions not stored yet; return every draft's record ID and the new count."""
    inserted = await conn.execute(
        insert(Record)
        .on_conflict_do_nothing(constraint="records_revision")
        .returning(Record.record_id),
        [
            {
                "record_type": draft.record_type,
                "logical_key": draft.logical_key,
                "content_hash": draft.content_hash,
                "raw": draft.raw,
            }
            for draft in drafts
        ],
    )
    new = len(inserted.all())
    record_ids: dict[RecordRevision, uuid.UUID] = {}
    revisions = list({draft.revision for draft in drafts})
    for batch in _batches(revisions):
        rows = await conn.execute(
            select(Record.record_id, Record.logical_key, Record.content_hash).where(
                tuple_(Record.logical_key, Record.content_hash).in_(batch)
            )
        )
        record_ids |= {(row.logical_key, row.content_hash): row.record_id for row in rows}
    for record_type, table in TYPED_TABLES.items():
        typed = [draft for draft in drafts if draft.record_type == record_type]
        if typed:
            await conn.execute(
                insert(table).on_conflict_do_nothing(index_elements=[table.record_id]),
                [{"record_id": record_ids[draft.revision], **draft.fields} for draft in typed],
            )
    return record_ids, new


def _batches(values: list[Any], size: int = 500) -> list[list[Any]]:
    # Keeps each IN list well under the driver's 32,767 parameters.
    return [values[start : start + size] for start in range(0, len(values), size)] or [[]]


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


async def cached_answers(
    conn: AsyncConnection,
    *,
    purpose: str,
    model: str,
    prompt_version: int,
    input_hashes: Iterable[str],
) -> list[Any]:
    """The answers already paid for: these inputs, asked of this model with this prompt."""
    rows = await conn.execute(
        select(LlmExtraction.__table__).where(
            LlmExtraction.purpose == purpose,
            LlmExtraction.model == model,
            LlmExtraction.prompt_version == prompt_version,
            LlmExtraction.input_hash.in_(list(input_hashes)),
        )
    )
    return list(rows)


async def put_answer(conn: AsyncConnection, answer: Mapping[str, Any]) -> None:
    """Keep a model's answer; an answer already kept for the same question stays as it is."""
    await conn.execute(insert(LlmExtraction).values(**answer).on_conflict_do_nothing())


# --- Runs, versions and the active pointer ----------------------------------------------


@dataclass(frozen=True)
class Member:
    """One record in a version, and where it sat in the snapshot it came from."""

    record_id: uuid.UUID
    logical_key: str
    snapshot_id: uuid.UUID
    locator: dict[str, Any]


@dataclass(frozen=True)
class ActiveRecord:
    source_id: str
    member: Member
    content_hash: str
    snapshot_sha256: str


@dataclass(frozen=True)
class ActiveChunk:
    source_id: str
    chunk_id: uuid.UUID
    content_hash: str
    strategy: str
    strategy_version: int


@dataclass(frozen=True)
class ActiveState:
    """The active version, source by source: what a run compares with, and carries over.

    A run that reads only some sources keeps the others exactly as they are published.
    """

    index_version_id: int | None
    revision: int
    embedding_config_id: str | None
    records: tuple[ActiveRecord, ...] = ()
    chunks: tuple[ActiveChunk, ...] = ()

    def record_revisions(self, source_id: str) -> frozenset[RecordRevision]:
        return frozenset(
            (r.member.logical_key, r.content_hash) for r in self.records if r.source_id == source_id
        )

    def chunk_hashes(self, source_id: str) -> frozenset[str]:
        return frozenset(c.content_hash for c in self.chunks if c.source_id == source_id)

    def snapshot_sha256s(self, source_id: str) -> frozenset[str]:
        return frozenset(r.snapshot_sha256 for r in self.records if r.source_id == source_id)


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


async def read_active(conn: AsyncConnection) -> ActiveState:
    pointer = (
        await conn.execute(select(ActiveKnowledge.index_version_id, ActiveKnowledge.revision))
    ).one()
    version_id = pointer.index_version_id
    if version_id is None:
        return ActiveState(None, pointer.revision, None)
    config_id = await conn.scalar(
        select(IndexVersion.embedding_config_id).where(IndexVersion.index_version_id == version_id)
    )
    members = await conn.execute(
        select(
            SourceSnapshot.source_id,
            IndexVersionRecord.record_id,
            IndexVersionRecord.logical_key,
            IndexVersionRecord.snapshot_id,
            IndexVersionRecord.locator,
            Record.content_hash,
            SourceSnapshot.content_sha256,
        )
        .join(Record, Record.record_id == IndexVersionRecord.record_id)
        .join(SourceSnapshot, SourceSnapshot.snapshot_id == IndexVersionRecord.snapshot_id)
        .where(IndexVersionRecord.index_version_id == version_id)
    )
    records = tuple(
        ActiveRecord(
            source_id=row.source_id,
            member=Member(row.record_id, row.logical_key, row.snapshot_id, row.locator),
            content_hash=row.content_hash,
            snapshot_sha256=row.content_sha256,
        )
        for row in members
    )
    chunks = await conn.execute(_ACTIVE_CHUNKS, {"version": version_id})
    return ActiveState(
        index_version_id=version_id,
        revision=pointer.revision,
        embedding_config_id=config_id,
        records=records,
        chunks=tuple(ActiveChunk(**row._mapping) for row in chunks),
    )


# A chunk belongs to the source of the records it was built from, as the version lists them.
_ACTIVE_CHUNKS = text(
    """
    SELECT DISTINCT snapshot.source_id, chunk.chunk_id, chunk.content_hash,
           chunk.strategy, chunk.strategy_version
    FROM knowledge.index_version_chunks AS member_chunk
    JOIN knowledge.chunks AS chunk ON chunk.chunk_id = member_chunk.chunk_id
    JOIN knowledge.chunk_records AS link ON link.chunk_id = chunk.chunk_id
    JOIN knowledge.index_version_records AS member
      ON member.record_id = link.record_id
     AND member.index_version_id = member_chunk.index_version_id
    JOIN knowledge.source_snapshots AS snapshot ON snapshot.snapshot_id = member.snapshot_id
    WHERE member_chunk.index_version_id = :version
    """
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
    if not await point_to(conn, version_id, expected_revision):
        return False
    await conn.execute(
        update(IndexVersion)
        .where(IndexVersion.index_version_id == version_id)
        .values(status="published", published_at=func.now())
    )
    return True


async def point_to(conn: AsyncConnection, version_id: int, expected_revision: int) -> bool:
    """Move the pointer alone, by the same compare-and-swap: a rollback to a version
    published before keeps when it was first published."""
    moved = await conn.execute(
        update(ActiveKnowledge)
        .where(ActiveKnowledge.revision == expected_revision)
        .values(
            index_version_id=version_id,
            revision=ActiveKnowledge.revision + 1,
            published_at=func.now(),
        )
    )
    return moved.rowcount == 1


async def read_version(conn: AsyncConnection, version_id: int) -> Any:
    """One version's status, report and dates; None when there is no such version."""
    return (
        await conn.execute(
            select(
                IndexVersion.index_version_id,
                IndexVersion.status,
                IndexVersion.validation_report,
                IndexVersion.embedding_config_id,
                IndexVersion.created_at,
                IndexVersion.published_at,
            ).where(IndexVersion.index_version_id == version_id)
        )
    ).one_or_none()


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
