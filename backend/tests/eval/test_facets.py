import json

import pytest

from tests.eval.test_gold import FACETS as GOLD
from tests.knowledge.fakes import FakeFacetReader, MemoryAnswerCache
from wobot.eval import gold
from wobot.eval.corpus import Corpus, Item
from wobot.eval.dataset import Case, Dataset, load_dataset
from wobot.eval.facets import (
    Product,
    compare_models,
    decide,
    labelled_products,
    need_checks,
)
from wobot.eval.metrics import FacetScore
from wobot.eval.report import facets_markdown, to_json, write_facets_report
from wobot.eval.runner import CaseResult, RunResult
from wobot.knowledge.extraction import Answer
from wobot.knowledge.facets import FACETS
from wobot.knowledge.records.text import key_text

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
    assert "| fake-facets | 0 | 0 | 0 of 0 | 0.50 | 0.50 | 0.50 | 1.00 | 1 | 0 |" in report
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


def test_the_reports_write_facet_counts_as_json(tmp_path):
    score = FacetScore()
    score.add("TM-1", {"functions": {"離床偵測": "離床提醒"}}, {"form": {"bed": "床墊下"}})
    result = CaseResult("d", "F-1", "dev", "facets", "scored", facets=score)
    run = RunResult(1, 3, [result], {})

    case = json.loads(json.dumps(to_json(run, {}), default=str))["cases"][0]

    assert case["facets"]["matched"] == {facet: 0 for facet in FACETS}
    assert case["facets"]["missed"]["functions"] == 1
    path = write_facets_report([], {"products": 0}, tmp_path, "r")
    assert json.loads(path.with_suffix(".json").read_text())["models"] == []


def test_on_the_labels_every_recommendation_need_is_decided_as_a_person_decided():
    """The labels, the dataset's queries and DEC-069's rules agree: no product a person
    said may never be shown passes, and none said to fit is filtered out."""
    checks = need_checks([load_dataset("recommendation-v2")])
    path = gold.GOLD_DIR / "product-facets.yaml"
    labels = {
        ref.value["name"]: ref.expected["facets"]
        for split in ("dev", "heldout")
        for ref in gold.product_facet_refs(path, split)
    }

    decisions = decide(checks, labels)

    assert len(checks) == 16  # every case but the one asking and the one routed to chat
    assert (decisions.never_passing, decisions.fits_filtered) == ([], [])


def test_a_need_leaves_out_what_was_shown_and_names_what_it_decides_wrong():
    checks = need_checks([load_dataset("recommendation-v2")])
    by_case = {check.case_id: check for check in checks}
    worn_bed = {"functions": {"離床偵測"}, "form": {"worn"}}
    facets = {
        "WhizPad 安心臥智慧床墊": {"functions": {"離床偵測"}, "form": {"bed"}},
        "家用緊急救援通報系統 HO-140": {"functions": {"離床偵測"}},  # worn unstated
        "iProték 智慧床墊": worn_bed,
    }

    decisions = decide([by_case["CASE-022"], by_case["REC-FX-10"]], facets)

    # WhizPad fits and was shown in REC-FX-10's history, where it is labelled never: left
    # out as the runtime guard leaves it out, so it is no miss.
    assert by_case["REC-FX-10"].shown == {key_text("WhizPad 安心臥智慧床墊")}
    assert decisions.never_passing == [
        "CASE-022: 家用緊急救援通報系統 HO-140",
        "REC-FX-10: 家用緊急救援通報系統 HO-140",
    ]
    assert decisions.fits_filtered == ["CASE-022: iProték 智慧床墊", "REC-FX-10: iProték 智慧床墊"]


def test_a_product_no_list_names_is_still_decided_against_its_labels():
    checks = need_checks([load_dataset("recommendation-v2")])
    [check] = [c for c in checks if c.case_id == "REC-FX-04"]
    labels = {"智慧照護解決方案": {"functions": {"離床偵測"}, "viewing": {"device"}}}
    facets = {"智慧照護解決方案": {"functions": {"離床偵測"}}}  # the device not read

    decisions = decide([check], facets, labels)

    # Neither of the case's lists names it, so only its labels show the miss.
    assert (decisions.never_passing, decisions.fits_filtered) == ([], [])
    assert decisions.unlike_labels == [
        "REC-FX-04: 智慧照護解決方案 passes; on its labels, filtered"
    ]
    assert decisions.compared == 1
