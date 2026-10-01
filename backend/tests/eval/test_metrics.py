import os
from datetime import date

from wobot.eval.metrics import field_score, retrieval_score, set_score


def test_ragas_reports_no_usage():
    assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"


async def test_set_score_names_what_is_missing_and_what_was_not_labelled():
    score = await set_score({"a", "b", "c"}, {"b", "c", "d"})

    assert (score.hits, score.precision, score.recall) == (2, 2 / 3, 2 / 3)
    assert score.f1 == 2 / 3
    assert (score.missing, score.unexpected) == (["a"], ["d"])


async def test_set_score_without_items_claims_no_precision():
    assert (await set_score({"a"}, set())).precision is None
    assert (await set_score({"a"}, set())).recall == 0
    assert (await set_score(set(), set())).f1 is None


async def test_field_score_compares_dates_and_spaces_as_written():
    expected = {"period_start": "2024-08-01", "funder_raw": "科技部  MOST", "amount_ntd": 100}
    actual = {"period_start": date(2024, 8, 1), "funder_raw": "科技部 MOST", "amount_ntd": 90}

    score = await field_score([("projects.yaml#1", expected, actual)], list(expected))

    assert score.accuracy("period_start") == 1
    assert score.accuracy("funder_raw") == 1
    assert score.mismatches == [("projects.yaml#1", "amount_ntd", "100", "90")]


async def test_field_score_ignores_a_line_break_inside_chinese_text():
    label = {"event": "學程「長期照護政策」課程"}
    record = {"event": "學程\n「長期照護政策」課程"}

    score = await field_score([("talks#1", label, record)], ["event"])

    assert score.accuracy("event") == 1


async def test_a_wider_value_holds_the_label_but_does_not_match_it():
    labels = [
        (
            "talks#1",
            {"location": "Hong Kong"},
            {"location": "the Hong Kong Polytechnic University"},
        ),
        ("talks#2", {"location": "合肥"}, {"location": None}),
        ("talks#3", {"location": ""}, {"location": "Taipei"}),
    ]

    score = await field_score(labels, ["location"])

    assert score.accuracy("location") == 0
    assert score.presence("location") == 1 / 3


async def test_field_score_counts_an_absent_value_both_sides_as_a_match():
    score = await field_score([("talks#1", {"location": ""}, {"location": None})], ["location"])

    assert score.accuracy("location") == 1


async def test_retrieval_counts_records_and_ranks_chunks():
    ranked = [(["x", "y"], 300), (["a"], 50), (["b", "a"], 400)]

    score = await retrieval_score({"a", "c"}, ranked, k=2)

    assert (score.found, score.records, score.recall) == (1, 3, 0.5)
    assert score.record_precision == 1 / 3
    assert score.first_relevant_rank == 2
    assert score.reciprocal_rank == 0.5
    assert score.chunk_precision == 0.5
    assert score.context_tokens == 350
    assert score.missing == ["c"]
