"""Semantic search over the active version, the read path answers will build on."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.knowledge.models import ActiveKnowledge, Chunk, Embedding, IndexVersionChunk


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    distance: float  # cosine distance: 0 for the same direction, up to 2
    context_header: str
    body: str
    links: list[dict[str, Any]]


async def search_chunks(
    conn: AsyncConnection, query_vector: Sequence[float], config_id: str, k: int
) -> list[SearchHit]:
    """The k chunks of the active version closest to the query, by exact cosine distance.

    Membership narrows the candidates first, so a chunk that only an unpublished or older
    version holds is never returned. Without an index PostgreSQL computes every distance
    and sorts: exact, and fast enough for a few thousand chunks.
    """
    distance = Embedding.embedding.cosine_distance(list(query_vector)).label("distance")
    rows = await conn.execute(
        select(Chunk.chunk_id, distance, Chunk.context_header, Chunk.body, Chunk.links)
        .select_from(ActiveKnowledge)
        .join(
            IndexVersionChunk,
            IndexVersionChunk.index_version_id == ActiveKnowledge.index_version_id,
        )
        .join(Chunk, Chunk.chunk_id == IndexVersionChunk.chunk_id)
        .join(
            Embedding,
            and_(Embedding.chunk_id == Chunk.chunk_id, Embedding.embedding_config_id == config_id),
        )
        .order_by(distance)
        .limit(k)
    )
    return [SearchHit(**row._mapping) for row in rows]
