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
    Record,
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


# Reciprocal rank fusion's constant: rank r scores 1 / (RRF_K + r). The usual 60 keeps the
# first few ranks of one list from outweighing agreement between lists.
RRF_K = 60


def fuse(rankings: Sequence[Sequence[SearchHit]], k: int) -> list[SearchHit]:
    """The top k chunks of several searches by reciprocal rank fusion.

    Ranks, not distances: searches in different languages sit at different distances from
    the same text, so their distances do not compare. A chunk scores the sum over the
    searches that returned it; ties keep the order in which chunks first appear.
    """
    scores: dict[uuid.UUID, float] = {}
    hits: dict[uuid.UUID, SearchHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1 / (RRF_K + rank)
            hits.setdefault(hit.chunk_id, hit)
    order = sorted(scores, key=lambda chunk_id: -scores[chunk_id])  # stable on ties
    return [hits[chunk_id] for chunk_id in order[:k]]


@dataclass(frozen=True)
class Member:
    """A record a chunk is built from."""

    record_id: uuid.UUID
    record_type: str
    logical_key: str


async def chunk_members(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID], version_id: int
) -> dict[uuid.UUID, list[Member]]:
    """The records each chunk is built from, as this version lists them.

    A reused chunk keeps links to older revisions too; only the version's own count.
    """
    rows = await conn.execute(
        select(
            ChunkRecord.chunk_id,
            IndexVersionRecord.record_id,
            Record.record_type,
            IndexVersionRecord.logical_key,
        )
        .join(IndexVersionRecord, IndexVersionRecord.record_id == ChunkRecord.record_id)
        .join(Record, Record.record_id == ChunkRecord.record_id)
        .where(
            ChunkRecord.chunk_id.in_(list(chunk_ids)),
            IndexVersionRecord.index_version_id == version_id,
        )
        .order_by(ChunkRecord.chunk_id, ChunkRecord.position)
    )
    members: dict[uuid.UUID, list[Member]] = {chunk_id: [] for chunk_id in chunk_ids}
    for row in rows:
        members[row.chunk_id].append(Member(row.record_id, row.record_type, row.logical_key))
    return members


async def chunk_record_keys(
    conn: AsyncConnection, chunk_ids: Sequence[uuid.UUID], version_id: int
) -> dict[uuid.UUID, list[str]]:
    """The logical keys of the records each chunk is built from, as this version lists them."""
    members = await chunk_members(conn, chunk_ids, version_id)
    return {chunk_id: [m.logical_key for m in found] for chunk_id, found in members.items()}
