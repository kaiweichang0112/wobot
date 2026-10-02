import os
from datetime import date

from wobot.eval.metrics import field_score, retrieval_score, set_score, transcription_score


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


async def test_a_transcription_counts_lines_present_and_each_value_once():
    reading = {
        "verbatim_text": ["體壓分佈測定", "42 36 30 24 mmHg", "２０年老人福祉科技學術重鎮"],
        "values": [
            {"label": "高壓", "value": "42", "unit": "mmHg"},
            {"label": "", "value": "36", "unit": "mmHg"},
            {"label": "", "value": "30", "unit": "mmhg"},  # misread unit
        ],
    }

    score = await transcription_score(
        ["體壓分佈測定", "20年老人福祉科技學術重鎮", "傳統彈簧床"],
        [("42", "mmHg"), ("36", "mmHg"), ("36", "mmHg"), ("30", "mmHg")],
        reading,
    )

    assert (score.lines_found, score.missing_lines) == (2, ["傳統彈簧床"])  # ２０ is 20
    assert (score.values_matched, score.values_read) == (2, 3)
    assert score.missing_values == ["36mmHg", "30mmHg"]
    assert score.extra_values == ["30mmhg"]
    assert (score.value_precision, score.value_recall) == (2 / 3, 0.5)


async def test_no_reading_reads_nothing_and_claims_no_precision():
    score = await transcription_score(["封面"], [("1", None)], None)

    assert (score.text_recall, score.value_recall, score.value_precision) == (0.0, 0.0, None)


async def test_marks_no_eye_tells_apart_count_as_the_same():
    reading = {
        "verbatim_text": ["(IMAGER-37溫感釋壓記憶泡綿床墊)", "世大福智科技股份有限公司SEDA G-Tech"],
        "values": [],
    }

    score = await transcription_score(
        ["(IMAGER–37溫感釋壓記憶泡綿床墊)", "世大福智科技股份有限公司 SEDA G-Tech"], [], reading
    )

    assert score.lines_found == 2


async def test_values_nobody_listed_are_not_scored_but_an_empty_list_is():
    reading = {"verbatim_text": [], "values": [{"label": "電話", "value": "02", "unit": None}]}

    unlisted = await transcription_score(["封面"], None, reading)
    none_there = await transcription_score(["封面"], [], reading)

    assert (unlisted.value_precision, unlisted.value_recall) == (None, None)
    assert (none_there.value_precision, none_there.extra_values) == (0.0, ["02"])
