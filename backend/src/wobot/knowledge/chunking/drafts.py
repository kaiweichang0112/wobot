"""What every chunk strategy produces, and the part they all share."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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
    embedding_body: str | None = None,
) -> ChunkDraft:
    """Derive what gets embedded, its size and its identity the same way for every strategy.

    `embedding_body` replaces the body in what is embedded, for a body that shows URLs: they
    carry no meaning a query could match, so they are cited from `links` instead.
    """
    # The header travels with the body, so a chunk still says where it belongs when it is
    # retrieved alone.
    embedding_input = f"{context_header}\n{body if embedding_body is None else embedding_body}"
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


# A list block's size target: one chunk stays about one topic, and long enough lists split.
MAX_BLOCK_TOKENS = 1000


@dataclass(frozen=True)
class BlockItem:
    """One item of a list block: shown with its links, embedded without them."""

    shown: str
    embedded: str
    record_revision: tuple[str, str]
    links: tuple[dict[str, str], ...] = field(default_factory=tuple)


def block_chunks(
    *,
    strategy: str,
    strategy_version: int,
    heading_path: Sequence[str],
    context_header: str,
    items: Sequence[BlockItem],
    max_tokens: int = MAX_BLOCK_TOKENS,
) -> list[ChunkDraft]:
    """One chunk for the block, or several split between items when it grows too long.

    An item is never cut in two; parts share the header, numbered "(1/2)", "(2/2)".
    """
    parts = _pack(count_tokens(context_header), items, max_tokens)
    chunks = []
    for number, part in enumerate(parts, start=1):
        header = context_header if len(parts) == 1 else f"{context_header} ({number}/{len(parts)})"
        chunks.append(
            build_chunk(
                strategy=strategy,
                strategy_version=strategy_version,
                heading_path=heading_path,
                context_header=header,
                body="\n\n".join(item.shown for item in part),
                embedding_body="\n\n".join(item.embedded for item in part),
                links=[link for item in part for link in item.links],
                # Paragraphs of one record share it; link the record once.
                record_revisions=list(dict.fromkeys(item.record_revision for item in part)),
            )
        )
    return chunks


def _pack(header_tokens: int, items: Sequence[BlockItem], max_tokens: int) -> list[list[BlockItem]]:
    parts: list[list[BlockItem]] = [[]]
    size = header_tokens
    for item in items:
        tokens = count_tokens(item.embedded) + 1  # and the blank line between items
        if parts[-1] and size + tokens > max_tokens:
            parts.append([])
            size = header_tokens
        parts[-1].append(item)
        size += tokens
    return parts
