import pytest
from sqlalchemy import func, select

from tests.knowledge.fakes import FakeEmbedder, FakeLectureReader, fake_vector
from tests.knowledge.grc_site import grc_fetcher, grc_source
from tests.knowledge.workbooks import catalog_row, catalog_workbook, product_row
from wobot.knowledge import repository
from wobot.knowledge.blobs import LocalBlobStore
from wobot.knowledge.catalog import SOURCE_ID, CatalogFile, ProductCatalogSource
from wobot.knowledge.chunking import products as product_chunking
from wobot.knowledge.chunking.products import product_chunk
from wobot.knowledge.extraction import CachedReader, DbAnswerCache
from wobot.knowledge.grc import GrcWebsiteSource
from wobot.knowledge.models import IndexVersion, IndexVersionRecord, IngestionRun
from wobot.knowledge.pipeline import run_ingestion
from wobot.knowledge.records.products import normalize_product
from wobot.knowledge.search import search_chunks
from wobot.knowledge.sources.drive import DriveError
from wobot.knowledge.sources.xlsx import HEADERS, CatalogSchemaError

PRODUCTS = [
    product_row(),
    product_row({"產品名稱": "測試地墊 TM-2", "主要功能": "踩踏互動遊戲，訓練下肢肌力。"}),
    product_row({"產品名稱": "測試手環 TM-3", "主要功能": "量測心率與睡眠。"}),
]


def catalog(rows, **workbook_options):
    return CatalogFile(locator="catalog.xlsx", content=catalog_workbook(rows, **workbook_options))


@pytest.fixture
def ingest(ingest_db, tmp_path):
    """Run ingestion against the rolled-back database; returns the result and the embedder."""

    async def _ingest(catalog_file, **options):
        async def fetch():
            return catalog_file

        embedder = FakeEmbedder()
        result = await run_ingestion(
            ingest_db, LocalBlobStore(tmp_path), embedder, [ProductCatalogSource(fetch)], **options
        )
        return result, embedder

    return _ingest


async def active(ingest_db):
    async with ingest_db.begin() as conn:
        return await repository.read_active(conn)


async def members(ingest_db, version_id):
    async with ingest_db.begin() as conn:
        rows = await conn.execute(
            select(IndexVersionRecord.logical_key, IndexVersionRecord.record_id).where(
                IndexVersionRecord.index_version_id == version_id
            )
        )
        return dict(rows.all())


async def test_first_run_publishes_every_row(ingest, ingest_db):
    result, embedder = await ingest(catalog(PRODUCTS))

    assert result.status == "published"
    assert result.counts["embedding_tokens"] > 0
    assert {key: result.counts[key] for key in ("rows", "records_new", "embeddings_new")} == {
        "rows": 3,
        "records_new": 3,
        "embeddings_new": 3,
    }
    assert len(embedder.calls) == 1
    assert (await active(ingest_db)).index_version_id == result.index_version_id


async def test_the_same_file_again_changes_nothing(ingest, ingest_db):
    catalog_file = catalog(PRODUCTS)
    first, _ = await ingest(catalog_file)
    again, embedder = await ingest(catalog_file)

    assert again.status == "no_change"
    assert again.reason == "same file"
    assert again.index_version_id is None
    assert embedder.calls == []
    assert (await active(ingest_db)).index_version_id == first.index_version_id


async def test_reordered_rows_are_the_same_content(ingest, ingest_db):
    first, _ = await ingest(catalog(PRODUCTS))
    reordered, embedder = await ingest(catalog(PRODUCTS[::-1]))

    assert reordered.status == "no_change"
    assert reordered.reason == "same content"
    assert embedder.calls == []
    assert (await active(ingest_db)).index_version_id == first.index_version_id


async def test_editing_one_row_reuses_the_rest(ingest, ingest_db):
    first, _ = await ingest(catalog(PRODUCTS))
    edited_rows = [*PRODUCTS[:2], product_row({**PRODUCTS[2], "主要功能": "量測血氧。"})]
    second, embedder = await ingest(catalog(edited_rows))

    assert second.status == "published"
    assert (second.counts["records_new"], second.counts["embeddings_new"]) == (1, 1)
    assert len(embedder.calls[0]) == 1 and "量測血氧。" in embedder.calls[0][0]
    v1 = await members(ingest_db, first.index_version_id)
    v2 = await members(ingest_db, second.index_version_id)
    assert v1.keys() == v2.keys()  # every product in both, changed or not
    assert sum(v1[key] == v2[key] for key in v1) == 2  # two revisions shared
    assert (await active(ingest_db)).index_version_id == second.index_version_id


async def test_a_contact_edit_needs_no_new_embedding(ingest):
    await ingest(catalog(PRODUCTS))
    edited_rows = [product_row({**PRODUCTS[0], "連絡電話": "03-1111-1111"}), *PRODUCTS[1:]]
    result, embedder = await ingest(catalog(edited_rows))

    assert result.status == "published"
    assert (result.counts["records_new"], result.counts["chunks_new"]) == (1, 0)
    assert embedder.calls == []


async def test_a_blocking_row_stops_the_run_before_writing(ingest, ingest_db):
    before = await active(ingest_db)
    result, embedder = await ingest(catalog([*PRODUCTS, product_row({"產品名稱": None})]))

    assert result.status == "failed"
    assert result.index_version_id is None
    assert result.sources[SOURCE_ID]["report"]["blocking"] == ["row 5: product_name is empty"]
    assert embedder.calls == []
    assert (await active(ingest_db)).revision == before.revision


async def test_a_dry_run_builds_a_version_but_leaves_the_pointer(ingest, ingest_db):
    before = await active(ingest_db)
    result, _ = await ingest(catalog(PRODUCTS), policy="dry_run")

    assert result.status == "validated"
    assert result.index_version_id is not None
    assert (await active(ingest_db)).revision == before.revision


async def test_of_two_versions_built_on_one_revision_only_one_publishes(ingest, ingest_db):
    first, _ = await ingest(catalog(PRODUCTS), policy="dry_run")
    second, _ = await ingest(catalog(PRODUCTS[:2]), policy="dry_run")
    revision = (await active(ingest_db)).revision

    async with ingest_db.begin() as conn:
        first_won = await repository.publish(conn, first.index_version_id, revision)
        second_won = await repository.publish(conn, second.index_version_id, revision)

    assert (first_won, second_won) == (True, False)
    assert (await active(ingest_db)).index_version_id == first.index_version_id


async def test_a_changed_column_fails_the_run_and_records_it(ingest, ingest_db):
    async def failed_runs():
        async with ingest_db.begin() as conn:
            return await conn.scalar(select(func.count()).where(IngestionRun.status == "failed"))

    before = await failed_runs()
    with pytest.raises(CatalogSchemaError):
        await ingest(catalog(PRODUCTS, headers=[*HEADERS, "價格"]))

    assert await failed_runs() == before + 1


async def test_the_same_file_under_new_code_is_rebuilt(ingest, monkeypatch):
    catalog_file = catalog(PRODUCTS)
    first, _ = await ingest(catalog_file)
    monkeypatch.setattr(product_chunking, "STRATEGY_VERSION", 2)
    rebuilt, embedder = await ingest(catalog_file)

    assert rebuilt.status == "published"
    assert rebuilt.index_version_id != first.index_version_id
    assert rebuilt.counts["records_new"] == 0
    assert rebuilt.counts["embeddings_new"] == 3  # a new strategy builds new chunks


async def test_search_reads_only_the_active_version(ingest, ingest_db):
    def query_for(row):
        # The fake vector of a chunk's own text: distance 0 to that chunk, about 1 to others.
        return fake_vector(product_chunk(normalize_product(catalog_row(row))).embedding_input)

    await ingest(catalog(PRODUCTS[:2]))
    await ingest(catalog(PRODUCTS), policy="dry_run")  # holds a third product, unpublished

    async with ingest_db.begin() as conn:
        found = await search_chunks(conn, query_for(PRODUCTS[1]), FakeEmbedder.config_id, k=5)
        unpublished = await search_chunks(conn, query_for(PRODUCTS[2]), FakeEmbedder.config_id, k=5)

    assert found[0].distance == pytest.approx(0, abs=1e-6)
    assert found[0].body.startswith("產品名稱：測試地墊 TM-2")
    assert len(unpublished) == 2
    assert all("測試手環" not in hit.body for hit in unpublished)


async def test_a_source_that_cannot_be_read_is_a_recorded_failure(ingest_db, tmp_path):
    async def failed_runs():
        async with ingest_db.begin() as conn:
            return await conn.scalar(select(func.count()).where(IngestionRun.status == "failed"))

    async def fetch():
        raise DriveError("file not found: is it shared with the service account?")

    before = await failed_runs()
    with pytest.raises(DriveError):
        await run_ingestion(
            ingest_db, LocalBlobStore(tmp_path), FakeEmbedder(), [ProductCatalogSource(fetch)]
        )

    assert await failed_runs() == before + 1


def catalog_source(catalog_file):
    async def fetch():
        return catalog_file

    return ProductCatalogSource(fetch)


@pytest.fixture
def ingest_sources(ingest_db, tmp_path):
    async def _ingest(sources, **options):
        embedder = FakeEmbedder()
        result = await run_ingestion(
            ingest_db, LocalBlobStore(tmp_path), embedder, sources, **options
        )
        return result, embedder

    return _ingest


async def test_a_run_of_one_source_carries_the_others_over(ingest_sources, ingest_db):
    first, _ = await ingest_sources([catalog_source(catalog(PRODUCTS))])
    second, embedder = await ingest_sources([grc_source([])])

    assert second.status == "published"
    v1 = await members(ingest_db, first.index_version_id)
    v2 = await members(ingest_db, second.index_version_id)
    assert {key.split(":")[0] for key in v2} == {
        "product",
        "student",
        "project",
        "publication",
        "profile",
        "section",
        "lecture",
    }
    assert all(v2[key] == v1[key] for key in v1)  # the catalog, revision for revision
    assert not any("產品名稱" in text for call in embedder.calls for text in call)
    async with ingest_db.begin() as conn:
        strategies = await conn.scalar(
            select(IndexVersion.strategies).where(
                IndexVersion.index_version_id == second.index_version_id
            )
        )
    assert set(strategies) == {
        "product_row",
        "student_block",
        "project_block",
        "publication_block",
        "profile_section",
        "lecture_block",
    }


async def test_a_source_left_out_of_a_run_stays_as_published(ingest_sources, ingest_db):
    await ingest_sources([catalog_source(catalog(PRODUCTS)), grc_source([])])
    edited_rows = [*PRODUCTS[:2], product_row({**PRODUCTS[2], "主要功能": "量測血氧。"})]

    edited, embedder = await ingest_sources([catalog_source(catalog(edited_rows))])

    assert edited.status == "published"
    keys = await members(ingest_db, edited.index_version_id)
    assert any(key.startswith("student:") for key in keys)
    assert [len(call) for call in embedder.calls] == [1]


async def test_one_unreadable_source_stops_the_whole_run(ingest_sources, ingest_db):
    before = await active(ingest_db)

    result, embedder = await ingest_sources(
        [catalog_source(catalog(PRODUCTS)), grc_source([], project_amount="NTD")]
    )

    assert result.status == "failed"
    assert result.sources["product_catalog"]["report"]["passed"]
    assert not result.sources["grc_website"]["report"]["passed"]
    assert embedder.calls == []
    assert (await active(ingest_db)).revision == before.revision


async def test_a_model_reads_each_speech_once_across_runs(ingest_sources, ingest_db):
    def source(reader):
        lectures = CachedReader(reader, DbAnswerCache(ingest_db))
        return GrcWebsiteSource(grc_fetcher([]), lectures)

    first_reader, again_reader = FakeLectureReader(), FakeLectureReader()
    first, _ = await ingest_sources([source(first_reader)])
    again, _ = await ingest_sources([source(again_reader)])

    assert first.status == "published"
    assert len(first_reader.calls) == 2
    # The kept answers give the same fields, so the same records: nothing changed.
    assert again.status == "no_change"
    assert again_reader.calls == []
    counts = again.sources["grc_website"]["report"]["counts"]
    assert (counts["model_reads_cached"], counts["model_reads_calls"]) == (2, 0)
