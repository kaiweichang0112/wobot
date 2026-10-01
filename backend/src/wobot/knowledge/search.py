"""Semantic search over an index version, the read path answers will build on."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.knowledge.models import (
    ActiveKnowledge,
    Chunk,
    ChunkRecord,
    Embedding,
    IndexVersionChunk,
    IndexVersionRecord,
)


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    distance: float  # cosine distance: 0 for the same direction, up to 2
    context_header: str
    body: str
    links: list[dict[str, Any]]
    token_count: int


async def search_chunks(
    conn: AsyncConnection,
    query_vector: Sequence[float],
    config_id: str,
    k: int,
    *,
    version_id: int | None = None,
) -> list[SearchHit]:
    """The k chunks closest to the query, by exact cosine distance.

    Searches the active version, or `version_id`, such as an unpublished candidate under
    evaluation. Membership narrows the candidates first, so a chunk only another version
    holds is never returned. Without an index PostgreSQL computes every distance and
    sorts: exact, and fast enough for a few thousand chunks.
    """
    distance = Embedding.embedding.cosine_distance(list(query_vector)).label("distance")
    columns = (
        Chunk.chunk_id,
        distance,
        Chunk.context_header,
        Chunk.body,
        Chunk.links,
        Chunk.token_count,
    )
    if version_id is None:
        candidates = (
            select(*columns)
            .select_from(ActiveKnowledge)
            .join(
                IndexVersionChunk,
                IndexVersionChunk.index_version_id == ActiveKnowledge.index_version_id,
            )
        )
    else:
        candidates = (
            select(*columns)
            .select_from(IndexVersionChunk)
            .where(IndexVersionChunk.index_version_id == version_id)
        )
    rows = await conn.execute(
        candidates.join(Chunk, Chunk.chunk_id == IndexVersionChunk.chunk_id)
        .join(
            Embedding,
            and_(Embedding.chunk_id == Chunk.chunk_id, Embedding.embedding_config_id == config_id),
        )
        .order_by(distance)
        .limit(k)
    )
    return [SearchHit(**row._mapping) for row in rows]


async def chunk_record_keys(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID], version_id: int
) -> dict[uuid.UUID, list[str]]:
    """The logical keys of the records each chunk is built from, as this version lists them.

    A reused chunk keeps links to older revisions too; only the version's own count.
    """
    rows = await conn.execute(
        select(ChunkRecord.chunk_id, IndexVersionRecord.logical_key)
        .join(IndexVersionRecord, IndexVersionRecord.record_id == ChunkRecord.record_id)
        .where(
            ChunkRecord.chunk_id.in_(list(chunk_ids)),
            IndexVersionRecord.index_version_id == version_id,
        )
        .order_by(ChunkRecord.chunk_id, ChunkRecord.position)
    )
    keys: dict[uuid.UUID, list[str]] = {chunk_id: [] for chunk_id in chunk_ids}
    for row in rows:
        keys[row.chunk_id].append(row.logical_key)
    return keys
