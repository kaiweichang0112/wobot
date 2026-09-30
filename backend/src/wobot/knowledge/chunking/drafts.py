"""What every chunk strategy produces, and the part they all share."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from wobot.knowledge.hashing import content_hash
from wobot.knowledge.tokens import count_tokens


@dataclass(frozen=True)
class ChunkDraft:
    content_hash: str
    strategy: str
    strategy_version: int
    heading_path: tuple[str, ...]
    context_header: str
    body: str
    links: tuple[dict[str, str], ...]
    embedding_input: str
    token_count: int
    # The record revisions the chunk is built from, in order, as (logical_key, content_hash).
    # Record IDs exist only once the repository has stored the records.
    record_revisions: tuple[tuple[str, str], ...]


def build_chunk(
    *,
    strategy: str,
    strategy_version: int,
    heading_path: Sequence[str],
    context_header: str,
    body: str,
    links: Sequence[Mapping[str, str]],
    record_revisions: Sequence[tuple[str, str]],
) -> ChunkDraft:
    """Derive what gets embedded, its size and its identity the same way for every strategy."""
    # The header travels with the body, so a chunk still says where it belongs when it is
    # retrieved alone.
    embedding_input = f"{context_header}\n{body}"
    content = {
        "strategy": strategy,
        "strategy_version": strategy_version,
        "heading_path": list(heading_path),
        "context_header": context_header,
        "body": body,
        "links": [dict(link) for link in links],
        "embedding_input": embedding_input,
    }
    return ChunkDraft(
        # Records stay out of the hash: records that differ only in fields the chunk leaves
        # out share one chunk, and with it one embedding.
        content_hash=content_hash(content),
        strategy=strategy,
        strategy_version=strategy_version,
        heading_path=tuple(heading_path),
        context_header=context_header,
        body=body,
        links=tuple(dict(link) for link in links),
        embedding_input=embedding_input,
        token_count=count_tokens(embedding_input),
        record_revisions=tuple(record_revisions),
    )
