"""Looking things up for the chat graph: passages by meaning, and records by name.

What is found becomes the turn's evidence: plain values, so the checkpointer stores it
as it is. Each passage and record gets a short handle (k- or r- and eight hex digits),
which the recommendation path will cite and code checks; the answer path cites nothing
(DV4). The search is phase B's: each query is embedded, ranked by cosine distance, and
the rankings fused by reciprocal rank.
"""

import logging
import uuid
from collections.abc import Sequence
from typing import Any, get_args

import openai
from sqlalchemy.exc import DBAPIError

from wobot.knowledge.embeddings import Embedder
from wobot.knowledge.lists import Listed, ListKind, QueryError, RecordQuery, find_records
from wobot.knowledge.repository import Database
from wobot.knowledge.search import chunk_members, fuse, search_chunks

logger = logging.getLogger(__name__)

# Passages one search returns. More than phase A's 5: the passage that answers often
# ranks just past fifth, among many alike, such as a year's talks or a product's pages.
SEARCHED_CHUNKS = 15
# How deep each wording's ranking goes before fusion. Deeper with 5 passages lost recall,
# letting wordings that agree on the wrong passages bury one only a single language
# finds; with 15, that one keeps a place.
FUSION_DEPTH = 10
# Records of one kind a name may match before it is too common to single one out.
NAMED_RECORDS = 10
# The database or the provider failed, not the code: worth trying again later. Anything
# else is a bug and is raised, never dressed up as an outage.
UNAVAILABLE = (DBAPIError, openai.APIError, TimeoutError)

# What the answer reads of a named record; a product shows its catalog in full.
SHOWN_FIELDS: dict[str, tuple[str, ...]] = {
    "lecture": ("date_raw", "category", "title", "event", "location"),
    "student": ("name", "degree", "graduation_year", "thesis_title_zh", "thesis_title_en"),
    "project": ("year", "title_zh", "title_en", "funder_raw", "period_raw", "amount_ntd"),
    "publication": ("year", "category", "item_text"),
    "product": (
        "product_name",
        "company_name",
        "company_address",
        "contact_phone",
        "product_url",
        "category_l2_label",
        "features_text",
        "usage_text",
        "summary_text",
    ),
}


class LookupFailed(Exception):
    """The database or the embedding provider could not be reached."""


def record_handle(record_id: uuid.UUID) -> str:
    """A short ID a model can copy without mistakes; code finds the record by its prefix."""
    return f"r-{record_id.hex[:8]}"


def chunk_handle(chunk_id: uuid.UUID) -> str:
    return f"k-{chunk_id.hex[:8]}"


async def search_knowledge(
    db: Database,
    embedder: Embedder,
    version_id: int,
    queries: Sequence[str],
    *,
    limit: int = SEARCHED_CHUNKS,
    record_type: str | None = None,
) -> list[dict[str, Any]]:
    """The `limit` passages closest in meaning to the queries, best first, with the
    records each is built from; with `record_type`, only passages built from such a
    record."""
    try:
        embedded = await embedder.embed(list(queries))
        async with db.begin() as conn:
            rankings = [
                await search_chunks(
                    conn,
                    vector,
                    embedder.config_id,
                    FUSION_DEPTH,
                    version_id=version_id,
                    record_type=record_type,
                )
                for vector in embedded.vectors
            ]
            hits = fuse(rankings, limit)
            members = await chunk_members(conn, [hit.chunk_id for hit in hits], version_id)
    except UNAVAILABLE as error:
        logger.exception("search failed", extra={"index_version": version_id})
        raise LookupFailed from error
    return [
        {
            "id": chunk_handle(hit.chunk_id),
            "header": hit.context_header,
            "text": hit.body,
            "records": [
                {
                    "id": record_handle(m.record_id),
                    "key": m.logical_key,
                    "type": m.record_type,
                    "source": m.source_url,
                }
                for m in members[hit.chunk_id]
            ],
        }
        for hit in hits
    ]


async def records_named(db: Database, version_id: int, name: str) -> list[dict[str, Any]]:
    """The records of every kind whose names or titles hold the name, as written.

    A search by meaning does not single out one of many people or talks listed together,
    so a name is looked up in the records too. A name too common to single one out of a
    kind finds none of that kind.
    """

    kinds: list[list[Listed]] = []
    try:
        async with db.begin() as conn:
            for kind in get_args(ListKind):
                query = RecordQuery(kind, contains=name)
                found = (await find_records(conn, version_id, query)).items
                kinds.append(found if len(found) <= NAMED_RECORDS else [])
    except QueryError:  # such as a "name" too long to be one
        return []
    except UNAVAILABLE as error:
        logger.exception("name lookup failed", extra={"index_version": version_id})
        raise LookupFailed from error
    return [
        {
            "id": record_handle(item.record_id),
            "key": item.logical_key,
            "type": kind,
            "source": item.source_url,
            "fields": {
                field: item.fields[field]
                for field in SHOWN_FIELDS[kind]
                if item.fields.get(field) is not None
            },
        }
        for kind, items in zip(get_args(ListKind), kinds, strict=True)
        for item in items
    ]


def evidence_keys(evidence: dict[str, Any]) -> list[str]:
    """The records the evidence holds, as logical keys in the order found: what
    evaluation compares with the labels."""
    keys = [r["key"] for passage in evidence.get("passages", []) for r in passage["records"]]
    keys += [record["key"] for record in evidence.get("records", [])]
    return list(dict.fromkeys(keys))
