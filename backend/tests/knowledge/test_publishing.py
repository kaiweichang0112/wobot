"""What stands between a run and the published version: the hold on lost records, and
the maintenance moves that publish or roll back by hand."""

import pytest
from sqlalchemy import select

from tests.knowledge.fakes import FakeEmbedder
from tests.knowledge.workbooks import catalog_workbook, product_row
from wobot.knowledge import repository
from wobot.knowledge.blobs import LocalBlobStore
from wobot.knowledge.catalog import CatalogFile, ProductCatalogSource
from wobot.knowledge.maintenance import MaintenanceError, accept, rollback
from wobot.knowledge.models import IngestionRun
from wobot.knowledge.pipeline import run_ingestion, skipped_run
from wobot.knowledge.validation import hold_reasons


def products(count, *, edition=""):
    return [product_row({"產品名稱": f"測試床墊 TM-{n}{edition}"}) for n in range(count)]


@pytest.fixture
def ingest(ingest_db, tmp_path):
    async def _ingest(rows, **options):
        content = catalog_workbook(rows)

        async def fetch():
            return CatalogFile(locator="catalog.xlsx", content=content)

        source = ProductCatalogSource(fetch)
        return await run_ingestion(
            ingest_db, LocalBlobStore(tmp_path), FakeEmbedder(), [source], **options
        )

    return _ingest


async def pointer(ingest_db):
    async with ingest_db.begin() as conn:
        return await repository.read_pointer(conn)


async def version(ingest_db, version_id):
    async with ingest_db.begin() as conn:
        return await repository.read_version(conn, version_id)


# --- The hold ---------------------------------------------------------------------------


def test_a_source_holds_when_it_empties_or_loses_more_than_the_limit():
    before = {"catalog": 10, "grc": 100, "docs": 0}

    assert hold_reasons(before, {"catalog": 8, "grc": 100}) == []  # 20% is the limit itself
    assert hold_reasons(before, {"catalog": 7, "grc": 0, "docs": 5, "new": 3}) == [
        "catalog: 7 records, down 30% from 10",
        "grc: no records, down from 100",
    ]
    assert hold_reasons(before, {"catalog": 9}, max_drop=0.05) == [
        "catalog: 9 records, down 10% from 10"
    ]


async def test_a_version_that_loses_records_waits_with_what_went_missing(ingest, ingest_db):
    first = await ingest(products(10))

    held = await ingest(products(7))

    assert (held.status, held.held) == ("held", ["product_catalog: 7 records, down 30% from 10"])
    assert (await pointer(ingest_db)).index_version_id == first.index_version_id
    report = (await version(ingest_db, held.index_version_id)).validation_report
    assert report["held"] == held.held
    assert len(report["changes"]["product_catalog"]["removed"]) == 3
    assert report["changes"]["product_catalog"]["added"] == []


async def test_a_small_loss_or_a_first_run_publishes(ingest):
    assert (await ingest(products(10))).status == "published"
    assert (await ingest(products(9))).status == "published"


# --- Accept and rollback ----------------------------------------------------------------


async def test_accepting_publishes_the_held_version_as_built(ingest, ingest_db):
    await ingest(products(10))
    held = await ingest(products(7))

    await accept(ingest_db, held.index_version_id)

    assert (await pointer(ingest_db)).index_version_id == held.index_version_id
    assert (await version(ingest_db, held.index_version_id)).status == "published"
    with pytest.raises(MaintenanceError, match="only a held or validated version"):
        await accept(ingest_db, held.index_version_id)


async def test_a_held_version_cannot_undo_a_later_publish(ingest, ingest_db):
    await ingest(products(10))
    held = await ingest(products(7))
    later = await ingest(products(10, edition="b"))  # all new names, none lost: publishes

    with pytest.raises(MaintenanceError, match="published after"):
        await accept(ingest_db, held.index_version_id)
    assert (await pointer(ingest_db)).index_version_id == later.index_version_id


async def test_rolling_back_points_at_an_older_version_and_keeps_its_history(ingest, ingest_db):
    first = await ingest(products(10))
    second = await ingest(products(10, edition="b"))
    published_at = (await version(ingest_db, first.index_version_id)).published_at

    replaced = await rollback(ingest_db, first.index_version_id)

    assert replaced == second.index_version_id
    assert (await pointer(ingest_db)).index_version_id == first.index_version_id
    assert (await version(ingest_db, first.index_version_id)).published_at == published_at
    with pytest.raises(MaintenanceError, match="already the active one"):
        await rollback(ingest_db, first.index_version_id)


async def test_only_a_version_published_before_can_be_rolled_back_to(ingest, ingest_db):
    await ingest(products(10))
    dry = await ingest(products(10, edition="b"), policy="dry_run")

    with pytest.raises(MaintenanceError, match="never published"):
        await rollback(ingest_db, dry.index_version_id)
    with pytest.raises(MaintenanceError, match="no version"):
        await rollback(ingest_db, 10**9)


# --- A run that finds another


async def test_a_run_that_finds_another_is_recorded_as_skipped(ingest_db):
    result = await skipped_run(
        ingest_db, triggered_by="schedule", policy="publish", code_version="test"
    )

    async with ingest_db.begin() as conn:
        row = (
            await conn.execute(
                select(IngestionRun.status, IngestionRun.triggered_by).where(
                    IngestionRun.run_id == result.run_id
                )
            )
        ).one()
    assert tuple(row) == ("skipped_concurrent", "schedule")
