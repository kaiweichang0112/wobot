import pytest

from tests.eval.test_gold import FACETS as GOLD
from tests.knowledge.fakes import FakeFacetReader, MemoryAnswerCache
from wobot.eval.corpus import Corpus, Item
from wobot.eval.dataset import Case, Dataset
from wobot.eval.facets import Product, compare_models, labelled_products
from wobot.eval.metrics import FacetScore
from wobot.eval.report import facets_markdown
from wobot.knowledge.extraction import Answer
from wobot.knowledge.facets import FACETS

FIELDS = {
    "product_name": "TM-1",
    "company_name": "Co",
    "features_text": "提供離床提醒，透過手機 App 查看。",
    "usage_text": "感測墊鋪於床墊下。",
    "summary_text": None,
}


def test_values_are_counted_per_facet_with_what_differs():
    score = FacetScore()

    score.add(
        "TM-1",
        {"functions": {"離床偵測": "離床提醒"}, "viewing": {"app": "手機 App"}},
        {"functions": {"離床偵測": "離床提醒"}, "viewing": {"phone": "手機"}},
    )

    assert (score.precision(), score.recall(), score.f1()) == (0.5, 0.5, 0.5)
    assert score.precision("functions") == 1.0 and score.recall("viewing") == 0.0
    assert score.precision("places") is None  # nothing labelled, nothing given
    assert score.differences == [
        "TM-1: + viewing phone 「手機」",
        "TM-1: - viewing app 「手機 App」",
    ]


def product(**expected):
    return Product("TM-1", {facet: {} for facet in FACETS} | expected, FIELDS)


async def test_each_model_is_scored_on_what_code_keeps_of_its_answer():
    def reads(text):
        return {facet: [] for facet in FACETS} | {
            "functions": [{"value": "離床偵測", "span": "離床提醒"}],
            "viewing": [{"value": "phone", "span": "手機"}, {"value": "app", "span": "App 看"}],
        }

    def refuses(text):
        return Answer(None, "refused: no", "fake-facets", 10, 0)

    labelled = product(functions={"離床偵測": "離床提醒"}, viewing={"app": "手機 App"})
    first, second = FakeFacetReader(reads), FakeFacetReader(refuses)
    second.question = type(first.question)("product_facets", "other", 1)

    [read, refused] = await compare_models([first, second], [labelled], MemoryAnswerCache())

    assert (read.score.matched["functions"], read.score.extra["viewing"]) == (1, 1)
    assert read.score.missed["viewing"] == 1  # its app span is not the text's: dropped
    assert read.dropped == ["TM-1: facet viewing app: 'App 看' is not in the text"]
    assert (read.calls, read.input_tokens) == (1, 900)
    assert refused.failures == {"TM-1": "refused: no"} and refused.score.recall() == 0.0
    report = facets_markdown([read, refused], {"products": 1})
    assert "| fake-facets | 0.50 | 0.50 | 0.50 | 1.00 | 1 | 0 |" in report
    assert "- TM-1: + viewing phone 「手機」" in report


def test_labelled_products_are_the_split_s_matched_to_their_records(tmp_path):
    (tmp_path / "f.yaml").write_text(GOLD, encoding="utf-8")
    check = {"kind": "facets", "gold": {"file": "f.yaml", "as": "product_facets"}}
    dataset = Dataset(
        "f-v1",
        "2026-10-11T10:00:00+08:00",
        "Asia/Taipei",
        [Case("F-1", "s", "dev", "?", "", None, check, None)],
        "x",
    )
    corpus = Corpus(1, "c", {}, [Item("product:co:tm-1", "product", FIELDS)])

    [found] = labelled_products([dataset], corpus, "dev", gold_dir=tmp_path)

    assert found.name == "TM-1" and found.expected["functions"] == {"離床偵測": "離床提醒"}
    assert found.text.features == FIELDS["features_text"]
    with pytest.raises(Exception, match="matches no single product"):
        labelled_products([dataset], Corpus(1, "c", {}, []), "dev", gold_dir=tmp_path)
