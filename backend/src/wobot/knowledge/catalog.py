"""The product catalog as an ingestion source: one workbook, one record and chunk per row."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from wobot.knowledge.chunking.products import product_chunk
from wobot.knowledge.records.products import normalize_catalog
from wobot.knowledge.source import Extraction, Snapshot
from wobot.knowledge.sources.xlsx import read_catalog
from wobot.knowledge.validation import check_products

SOURCE_ID = "product_catalog"


@dataclass(frozen=True)
class CatalogFile:
    locator: str  # where the bytes came from: a file name, or drive:<file ID>
    content: bytes
    details: Mapping[str, Any] = field(default_factory=dict)  # kept on the snapshot


# Called inside the run, so a catalog that cannot be read is a recorded failure.
FetchCatalog = Callable[[], Awaitable[CatalogFile]]


class ProductCatalogSource:
    source_id = SOURCE_ID

    def __init__(self, fetch: FetchCatalog) -> None:
        self._fetch = fetch

    async def extract(self) -> Extraction:
        catalog = await self._fetch()
        drafts = normalize_catalog(read_catalog(catalog.content))
        chunks = [product_chunk(draft) for draft in drafts]
        return Extraction(
            source_id=SOURCE_ID,
            kind="xlsx",
            pages=[(Snapshot(catalog.locator, catalog.content, catalog.details), drafts)],
            chunks=chunks,
            report=check_products(drafts, chunks, current_year=date.today().year),
        )
