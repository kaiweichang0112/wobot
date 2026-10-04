"""A small published version, built by the real pipeline from the synthetic sources."""

from dataclasses import dataclass

import pytest

from tests.knowledge.conftest import (
    SavepointDatabase,
    ingest_db,  # noqa: F401  # the rolled-back database
)
from tests.knowledge.fakes import FakeEmbedder
from tests.knowledge.grc_site import grc_source
from tests.knowledge.workbooks import catalog_workbook, product_row
from wobot.knowledge.blobs import LocalBlobStore
from wobot.knowledge.catalog import CatalogFile, ProductCatalogSource
from wobot.knowledge.pipeline import run_ingestion

PRODUCTS = [
    product_row(),
    product_row(
        {
            "產品名稱": "測試地墊 TM-2",
            "主要功能": "踩踏互動遊戲，訓練下肢肌力。",
            "產品第一層分類": "(4) 提升長者身體及認知能力科技產品",
            "產品第二層分類": "(4-3) 認知訓練/運動遊戲",
        }
    ),
    product_row({"產品名稱": "測試手環 TM-3", "主要功能": "量測心率與睡眠。"}),
]


@dataclass(frozen=True)
class Knowledge:
    db: SavepointDatabase
    embedder: FakeEmbedder
    version_id: int


@pytest.fixture
async def knowledge(ingest_db, tmp_path) -> Knowledge:  # noqa: F811
    """GRC's synthetic site and three products, published; rolled back after the test."""
    catalog = CatalogFile(locator="catalog.xlsx", content=catalog_workbook(PRODUCTS))

    async def fetch() -> CatalogFile:
        return catalog

    embedder = FakeEmbedder()
    result = await run_ingestion(
        ingest_db,
        LocalBlobStore(tmp_path),
        embedder,
        [ProductCatalogSource(fetch), grc_source([])],
    )
    assert result.status == "published", result
    return Knowledge(ingest_db, embedder, result.index_version_id)
