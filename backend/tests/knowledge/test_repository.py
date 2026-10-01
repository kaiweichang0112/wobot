from sqlalchemy import select

from tests.knowledge.fakes import FakeEmbedder, fake_vector
from tests.knowledge.workbooks import catalog_row
from wobot.knowledge import repository
from wobot.knowledge.chunking.products import product_chunk
from wobot.knowledge.models import ChunkRecord
from wobot.knowledge.records.products import normalize_catalog

CONFIG = FakeEmbedder.config_id


def drafts_of(*overrides):
    return normalize_catalog(
        [catalog_row(o, row_number=n) for n, o in enumerate(overrides, start=2)]
    )


async def test_snapshots_of_the_same_bytes_are_one_row(ingest_db):
    snapshot = {
        "source_id": "test_catalog",
        "kind": "xlsx",
        "locator": "catalog.xlsx",
        "sha256": "a" * 64,
        "storage_key": "sha256/" + "a" * 64,
        "byte_size": 10,
    }
    async with ingest_db.begin() as conn:
        first = await repository.put_snapshot(conn, **snapshot)
        again = await repository.put_snapshot(conn, **snapshot)

    assert first == again


async def test_storing_the_same_products_twice_adds_nothing(ingest_db):
    drafts = drafts_of(None, {"產品名稱": "測試地墊 TM-2"})
    async with ingest_db.begin() as conn:
        first_ids, first_new = await repository.put_records(conn, drafts)
        again_ids, again_new = await repository.put_records(conn, drafts)

    assert (first_new, again_new) == (2, 0)
    assert first_ids == again_ids


async def test_a_contact_edit_links_the_same_chunk_to_the_new_revision(ingest_db):
    before = drafts_of(None)
    after = drafts_of({"連絡電話": "03-1111-1111"})
    async with ingest_db.begin() as conn:
        before_ids, _ = await repository.put_records(conn, before)
        chunk_ids, chunks_new = await repository.put_chunks(
            conn, [product_chunk(d) for d in before], before_ids
        )
        after_ids, records_new = await repository.put_records(conn, after)
        reused_ids, reused_new = await repository.put_chunks(
            conn, [product_chunk(d) for d in after], after_ids
        )
        linked = await conn.scalars(
            select(ChunkRecord.record_id).where(ChunkRecord.chunk_id.in_(list(chunk_ids.values())))
        )

    assert (chunks_new, records_new, reused_new) == (1, 1, 0)
    assert reused_ids == chunk_ids
    assert set(linked) == {*before_ids.values(), *after_ids.values()}


async def test_only_chunks_without_a_vector_are_missing(ingest_db):
    drafts = drafts_of(None, {"產品名稱": "測試地墊 TM-2"})
    async with ingest_db.begin() as conn:
        record_ids, _ = await repository.put_records(conn, drafts)
        chunk_ids, _ = await repository.put_chunks(
            conn, [product_chunk(d) for d in drafts], record_ids
        )
        missing = await repository.missing_embeddings(conn, chunk_ids.values(), CONFIG)
        first_id, first_text = missing[0]
        await repository.put_embeddings(conn, CONFIG, {first_id: fake_vector(first_text)})
        still_missing = await repository.missing_embeddings(conn, chunk_ids.values(), CONFIG)

    assert len(missing) == 2
    assert [chunk_id for chunk_id, _ in still_missing] == [missing[1][0]]
