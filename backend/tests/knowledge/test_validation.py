from tests.knowledge.fakes import FakeFacetReader
from tests.knowledge.workbooks import catalog_row
from wobot.knowledge.chunking.products import product_chunk
from wobot.knowledge.extraction import Answer
from wobot.knowledge.facets import FACETS
from wobot.knowledge.records.products import normalize_catalog, with_facets
from wobot.knowledge.validation import ValidationReport, check_products, check_version

READ = Answer({facet: [] for facet in FACETS}, None, "fake-facets", 0, 0)
UNREAD = Answer(None, "refused: no", "fake-facets", 0, 0)


def check(*overrides, answers=None):
    drafts = normalize_catalog(
        [catalog_row(o, row_number=n) for n, o in enumerate(overrides, start=2)]
    )
    question = FakeFacetReader.question
    drafts = [
        with_facets(draft, answer, question)
        for draft, answer in zip(drafts, answers or [READ] * len(drafts), strict=True)
    ]
    return check_products(drafts, [product_chunk(d) for d in drafts], current_year=2026)


def test_a_clean_catalog_passes():
    report = check(None, {"產品名稱": "測試地墊 TM-2"})

    assert report.passed
    assert report.to_json() == {
        "passed": True,
        "counts": {"rows": 2},
        "blocking": [],
        "warnings": [],
        "holds": [],
    }


def test_a_product_the_model_did_not_read_holds_the_version():
    report = check(None, {"產品名稱": "測試地墊 TM-2"}, answers=[READ, UNREAD])

    assert report.passed
    assert report.holds == ["1 products have no facets: rows [3]"]
    assert report.warnings == ["row 3: no facets: refused: no"]


def test_an_empty_catalog_blocks():
    assert check().blocking == ["the catalog has no products"]


def test_a_missing_name_blocks_and_names_the_row():
    report = check(None, {"公司名稱": "  "})

    assert report.blocking == ["row 3: company_name is empty"]


def test_a_dropped_roc_digit_blocks():
    assert check({"本計畫導入年分": "12"}).blocking == ["row 2: implausible adoption years [1923]"]


def test_a_category_filed_under_another_parent_only_warns():
    report = check({"產品第二層分類": "(2-1) 遠距生理量測"})

    assert report.passed
    assert report.warnings == ["row 2: category 2-1 is not under 1"]


def test_row_warnings_carry_their_row_number():
    report = check({"本計畫導入年分": "112年"})

    assert report.warnings == ["row 2: unparsed adoption years '112年'"]


def test_a_stored_version_missing_embeddings_blocks():
    report = ValidationReport()
    integrity = {
        "records": 2,
        "chunks": 2,
        "records_without_chunk": 0,
        "chunks_without_embedding": 1,
    }

    check_version(report, integrity, expected_records=2, expected_chunks=2)

    assert report.blocking == ["1 chunks have no embedding"]
    assert report.counts == integrity
