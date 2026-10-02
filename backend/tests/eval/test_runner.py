"""The harness end to end: synthetic sources ingested, labels written, cases scored."""

import pytest
from sqlalchemy import select

from tests.knowledge.fakes import FakeEmbedder
from tests.knowledge.grc_site import grc_source
from tests.knowledge.gtech_site import documents_source
from tests.knowledge.workbooks import catalog_workbook, product_row
from wobot.eval.corpus import load_corpus
from wobot.eval.dataset import Case, Dataset
from wobot.eval.runner import run_datasets
from wobot.knowledge.blobs import LocalBlobStore
from wobot.knowledge.catalog import CatalogFile, ProductCatalogSource
from wobot.knowledge.models import Chunk, IndexVersionChunk
from wobot.knowledge.pipeline import run_ingestion

ZWSP = "​"
PRODUCTS = [
    product_row(),
    product_row({"產品名稱": "測試地墊 TM-2"}),
    product_row({"產品名稱": "測試手環 TM-3"}),
]


@pytest.fixture
async def ingested(ingest_db, tmp_path):
    async def fetch():
        return CatalogFile(locator="catalog.xlsx", content=catalog_workbook(PRODUCTS))

    result = await run_ingestion(
        ingest_db,
        LocalBlobStore(tmp_path / "blobs"),
        FakeEmbedder(),
        [ProductCatalogSource(fetch), grc_source([])],
    )
    async with ingest_db.begin() as conn:
        return await load_corpus(conn, result.index_version_id)


def case(case_id, check=None, user_input="問題", pending=None, split="dev"):
    return Case(case_id, "test", split, user_input, "", None, check, pending)


def dataset(*cases):
    return Dataset("test-v1", "2026-09-23T10:00:00+08:00", "Asia/Taipei", list(cases), "x")


async def score(ingest_db, corpus, gold_dir, *cases, embedder=None, k=3):
    async with ingest_db.begin() as conn:
        run = await run_datasets(conn, corpus, embedder, [dataset(*cases)], k=k, gold_dir=gold_dir)
    return {result.case_id: result for result in run.cases}


@pytest.fixture
def gold_dir(tmp_path):
    directory = tmp_path / "gold"
    directory.mkdir()
    return directory


async def test_a_complete_list_with_its_fields(ingest_db, ingested, gold_dir):
    (gold_dir / "masters.yaml").write_text(
        "2022:\n  - name: '王小明'\n    thesis_title_zh: '智慧床墊'\n    thesis_title_en: ''\n",
        encoding="utf-8",
    )
    check = {
        "kind": "list",
        "records": "student",
        "where": {"degree": "master", "graduation_year": 2022},
        "gold": {"file": "masters.yaml", "as": "student", "degree": "master", "years": [2022]},
        "fields": ["thesis_title_zh", "thesis_title_en"],
    }

    result = (await score(ingest_db, ingested, gold_dir, case("C1", check)))["C1"]

    assert result.status == "scored"
    assert (result.set.precision, result.set.recall) == (1, 1)
    assert result.fields.accuracy("thesis_title_zh") == 1
    assert result.fields.accuracy("thesis_title_en") == 1


async def test_talks_pasted_from_the_page_match_their_records(ingest_db, ingested, gold_dir):
    (gold_dir / "talks.txt").write_text(
        "# pasted\n"
        f"“Smart care in practice,” keynote speech, Example Symposium, Seoul, Korea, 202{ZWSP}"
        "5/12/05 PDF\n"
        '"長者運動遊戲設計，"範例大學護理系課程專題演講，2025/11/12 PDF\n',
        encoding="utf-8",
    )
    check = {
        "kind": "list",
        "records": "lecture",
        "where": {"year": 2025},
        "gold": {"file": "talks.txt", "as": "speech"},
    }

    result = (await score(ingest_db, ingested, gold_dir, case("C1", check)))["C1"]

    assert result.unresolved == []
    assert result.set.f1 == 1


async def test_a_label_that_matches_nothing_counts_as_missed(ingest_db, ingested, gold_dir):
    (gold_dir / "products.txt").write_text(
        "測試床墊 TM-1\t範例科技股份有限公司\n"
        "測試地墊 TM-2\t範例科技股份有限公司\n"
        "測試手環 TM-9\t範例科技股份有限公司\n",  # a typo in the label
        encoding="utf-8",
    )
    check = {
        "kind": "list",
        "records": "product",
        "where": {"category_l2_code": "1-3"},
        "gold": {"file": "products.txt", "as": "product"},
    }

    result = (await score(ingest_db, ingested, gold_dir, case("C1", check)))["C1"]

    assert (result.set.hits, result.set.expected, result.set.actual) == (2, 3, 3)
    assert result.unresolved == ["products.txt:3: product '測試手環 TM-9, 範例科技股份有限公司'"]
    assert result.set.unexpected == ["product:範例科技股份有限公司:測試手環 tm-3"]


async def test_speech_fields_against_what_the_model_read(ingest_db, ingested, gold_dir):
    # The stand-in model reads the quoted title only.
    (gold_dir / "fields.yaml").write_text(
        "- entry: |-\n"
        "    “Smart care in practice,” keynote speech, Example Symposium, Seoul, Korea, "
        "2025/12/05 PDF\n"
        "  title: 'Smart care in practice'\n"
        "  event: 'Example Symposium'\n"
        "  location: 'Seoul, Korea'\n",
        encoding="utf-8",
    )
    check = {
        "kind": "fields",
        "records": "lecture",
        "gold": {"file": "fields.yaml", "as": "speech_fields"},
        "fields": ["title", "event", "location"],
    }

    result = (await score(ingest_db, ingested, gold_dir, case("C1", check)))["C1"]

    assert result.fields.accuracy("title") == 1
    assert result.fields.accuracy("event") == 0
    assert [m[1:] for m in result.fields.mismatches] == [
        ("event", "Example Symposium", None),
        ("location", "Seoul, Korea", None),
    ]


async def test_retrieval_maps_chunks_back_to_their_records(ingest_db, ingested, gold_dir):
    (gold_dir / "retrieval.yaml").write_text(
        "C1:\n  relevant:\n    - student: '王小明'\n", encoding="utf-8"
    )
    async with ingest_db.begin() as conn:
        # This version's chunk only: the local database may hold real chunks alike.
        question = await conn.scalar(
            select(Chunk.embedding_input)
            .join(IndexVersionChunk, IndexVersionChunk.chunk_id == Chunk.chunk_id)
            .where(
                IndexVersionChunk.index_version_id == ingested.version_id,
                Chunk.context_header.like("%碩士畢業生%"),
            )
        )
    # Fake vectors match only equal texts: asking with the chunk's own text finds it first.
    check = {"kind": "retrieval", "gold": {"file": "retrieval.yaml"}}

    result = (
        await score(
            ingest_db,
            ingested,
            gold_dir,
            case("C1", check, user_input=question),
            embedder=FakeEmbedder(),
        )
    )["C1"]

    assert result.retrieval.first_relevant_rank == 1
    assert result.retrieval.recall == 1
    assert result.retrieval.context_tokens > 0


async def test_cases_that_cannot_run_say_why(ingest_db, ingested, gold_dir):
    (gold_dir / "empty.txt").write_text("# nothing pasted yet\n", encoding="utf-8")
    retrieval = {"kind": "retrieval", "gold": {"file": "empty.txt", "as": "speech"}}
    missing = {"kind": "list", "records": "lecture", "gold": {"file": "none.txt", "as": "speech"}}

    results = await score(
        ingest_db,
        ingested,
        gold_dir,
        case("C1", pending="needs phase B"),
        case("C2", retrieval),
        case("C3", missing),
        case("C4", pending="held out", split="heldout"),
    )

    assert (results["C1"].status, results["C1"].detail) == ("pending", "needs phase B")
    assert (results["C2"].status, results["C2"].detail) == ("pending", "not labelled yet")
    assert results["C3"].status == "error"
    assert results["C4"].split == "heldout"


async def test_a_vision_reading_against_a_person_s_transcription(ingest_db, tmp_path, gold_dir):
    result = await run_ingestion(
        ingest_db, LocalBlobStore(tmp_path / "blobs"), FakeEmbedder(), [documents_source()]
    )
    async with ingest_db.begin() as conn:
        corpus = await load_corpus(conn, result.index_version_id)
    (gold_dir / "t.yaml").write_text(
        "V1:\n  document: whiztoys_manual\n  page: 1\n  text: ['page 1', '封面標語']\n"
        "V2:\n  document: whiztoys_manual\n  page: 9\n  text: ['x']\n",
        encoding="utf-8",
    )
    check = {"kind": "transcription", "gold": {"file": "t.yaml", "as": "transcription"}}

    results = await score(ingest_db, corpus, gold_dir, case("V1", check), case("V2", check))

    t = results["V1"].transcription
    assert (results["V1"].status, t.lines_found, t.missing_lines) == ("scored", 1, ["封面標語"])
    assert (results["V2"].status, results["V2"].detail) == (
        "error",
        "the picture is not in this version",
    )
