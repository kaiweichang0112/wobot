"""One index version's records, with their typed fields, read once for a whole run."""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from wobot.knowledge.chunking.sections import table_rows
from wobot.knowledge.models import ActiveKnowledge, IndexVersion, IndexVersionRecord, Record
from wobot.knowledge.repository import TYPED_TABLES


@dataclass(frozen=True)
class Item:
    logical_key: str
    record_type: str
    # The typed table's columns; a section has none, so it gets its raw heading and text.
    fields: dict[str, Any]


@dataclass
class Corpus:
    version_id: int
    embedding_config_id: str
    strategies: dict[str, int]
    items: list[Item] = field(default_factory=list)

    def of(self, record_type: str) -> list[Item]:
        return self._by_type.get(record_type, [])

    def __post_init__(self) -> None:
        self._by_type: dict[str, list[Item]] = defaultdict(list)
        for item in self.items:
            self._by_type[item.record_type].append(item)


async def active_version(conn: AsyncConnection) -> int | None:
    return await conn.scalar(select(ActiveKnowledge.index_version_id))


async def load_corpus(conn: AsyncConnection, version_id: int) -> Corpus:
    version = (
        await conn.execute(
            select(IndexVersion.strategies, IndexVersion.embedding_config_id).where(
                IndexVersion.index_version_id == version_id
            )
        )
    ).one_or_none()
    if version is None:
        raise LookupError(f"no index version {version_id}")
    items = []
    for record_type, table in TYPED_TABLES.items():
        columns = [column for column in table.__table__.c if column.name != "record_id"]
        rows = await conn.execute(
            select(IndexVersionRecord.logical_key, *columns)
            .join(table, table.record_id == IndexVersionRecord.record_id)
            .where(IndexVersionRecord.index_version_id == version_id)
        )
        for row in rows:
            values = dict(row._mapping)
            items.append(Item(values.pop("logical_key"), record_type, values))
    untyped = await conn.execute(
        select(IndexVersionRecord.logical_key, Record.record_type, Record.raw)
        .join(Record, Record.record_id == IndexVersionRecord.record_id)
        .where(
            IndexVersionRecord.index_version_id == version_id,
            Record.record_type.in_(("section", "image", "document_page")),
        )
    )
    for row in untyped:
        items.append(Item(row.logical_key, row.record_type, _raw_fields(row.record_type, row.raw)))
    return Corpus(
        version_id=version_id,
        embedding_config_id=version.embedding_config_id,
        strategies=dict(version.strategies),
        items=items,
    )


def _raw_fields(record_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Fields of a record type without a table of its own, from what the reader saw."""
    if record_type == "section":
        heading_path = raw["heading_path"]
        rows = [line for table in raw.get("tables", []) for line in table_rows(table["rows"])]
        return {
            "heading": heading_path[-1],
            "path": heading_path,
            "text": [*raw["paragraphs"], *rows],
        }
    # An image or a PDF page: where it is, and what the model read in it.
    fields = {"path": raw["heading_path"], "reading": raw["reading"]}
    if record_type == "image":
        return fields | {"image_url": raw["image_url"], "alts": raw["alts"]}
    return fields | {"document_url": raw["document_url"], "page": raw["page"], "text": raw["text"]}
