"""The product catalog as an ingestion source: one workbook, one record and chunk per row,
each product with the facets a model read in its text (DEC-069)."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from wobot.knowledge.chunking.products import product_chunk
from wobot.knowledge.extraction import CachedReader
from wobot.knowledge.facet_extraction import ProductText
from wobot.knowledge.records.products import normalize_catalog, with_facets
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

    def __init__(self, fetch: FetchCatalog, facets: CachedReader[ProductText]) -> None:
        self._fetch = fetch
        # Reads each product's facets: answers kept from earlier runs, or a model.
        self._facets = facets

    async def extract(self) -> Extraction:
        catalog = await self._fetch()
        drafts = normalize_catalog(read_catalog(catalog.content))
        texts = [ProductText.of(draft.fields) for draft in drafts]
        answers, model_reads = await self._facets.read_all(texts)
        question = self._facets.question
        drafts = [
            with_facets(draft, answers[text], question)
            for draft, text in zip(drafts, texts, strict=True)
        ]
        chunks = [product_chunk(draft) for draft in drafts]
        report = check_products(drafts, chunks, current_year=date.today().year)
        report.counts |= model_reads.counts("model_reads")
        return Extraction(
            source_id=SOURCE_ID,
            kind="xlsx",
            pages=[(Snapshot(catalog.locator, catalog.content, catalog.details), drafts)],
            chunks=chunks,
            report=report,
        )
