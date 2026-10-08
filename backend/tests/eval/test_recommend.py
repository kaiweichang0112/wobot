from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage

from tests.agent.conftest import knowledge  # noqa: F401  # a small published version
from tests.agent.fakes import FakeToolModel, calls
from tests.agent.test_graph import bed_product, recommending
from wobot.eval.candidates import Candidate
from wobot.eval.dataset import Case, load_dataset
from wobot.eval.recommend import (
    RecCase,
    RecPlay,
    RecResult,
    compare,
    play,
    preferred,
    problems,
    rec_cases,
    rec_path,
    rule_pick,
)
from wobot.eval.report import recommend_markdown

TAIPEI = ZoneInfo("Asia/Taipei")
FIT, OTHER, NEVER = "WhizPad 安心臥智慧床墊", "iProték 智慧床墊", "Monibed 紅外線離床警示器"


def rec_case(**check) -> RecCase:
    case = Case(
        "RC-1", "s", "dev", "想找離床預警的產品", "x", None, {"kind": "recommendation"}, None
    )
    given = {
        "actions": ("recommend",),
        "recommends": frozenset({FIT, OTHER}),
        "never": frozenset({NEVER}),
        "prefers": None,
        "count": 1,
        "searches": None,
        "language": None,
    } | check
    return RecCase(case, datetime(2026, 10, 8, tzinfo=TAIPEI), **given)


def test_the_cases_are_recommendation_v2s_less_the_routers():
    cases = rec_cases([load_dataset("recommendation-v2")], ["dev", "heldout"])

    ids = [c.case.case_id for c in cases]
    assert "REC-FX-06" not in ids and len(ids) == 17  # the thanks checks the route
    case_022 = next(c for c in cases if c.case.case_id == "CASE-022")
    assert case_022.actions == ("recommend",) and case_022.prefers == FIT
    assert NEVER in case_022.never and FIT in case_022.recommends
    asking = next(c for c in cases if c.case.case_id == "CASE-021")
    assert asking.searches is False and asking.recommends is None


@pytest.mark.parametrize(
    ("check", "action", "shown", "searches", "text", "found"),
    [
        ({}, "recommend", [FIT], 1, "推薦", []),
        ({}, "replied", [], 1, "請問？", ["replied, not recommend"]),
        ({}, "recommend", [NEVER], 1, "推薦", [f"shows {NEVER}, which may never be shown"]),
        ({}, "recommend", ["其他產品"], 1, "推薦", ["shows 其他產品, not labelled to fit"]),
        ({}, "recommend", [FIT, OTHER], 1, "推薦", ["shows 2, not 1"]),
        ({"count": "all"}, "recommend", [FIT], 1, "推薦", ["shows 1, not all"]),
        ({"actions": ("replied",), "searches": False}, "replied", [], 1, "？", ["searched"]),
        ({"language": "en"}, "recommend", [FIT], 1, "這項產品\nRecommended", ["not in English"]),
        ({"language": "en"}, "recommend", [FIT], 1, "This fits.\n推薦：WhizPad", []),
        (
            {"actions": ("replied", "recommend")},
            "unverified",
            [],
            2,
            "",
            ["unverified, not replied or recommend"],
        ),
    ],
)
def test_a_turn_is_scored_by_what_it_did_and_showed(check, action, shown, searches, text, found):
    assert problems(rec_case(**check), action, shown, searches, text) == found


def test_the_preferred_product_is_judged_only_where_a_product_was_shown():
    prefers = rec_case(prefers=FIT)

    assert preferred(prefers, "recommend", [FIT]) is True
    assert preferred(prefers, "recommend", [OTHER]) is False
    assert preferred(prefers, "replied", []) is None
    assert preferred(rec_case(), "recommend", [FIT]) is None


SEARCH = calls(("search_products", {"functions": ["離床偵測"]}))


async def test_a_play_keeps_the_flow_and_scores_the_card(knowledge):  # noqa: F811
    bed = await bed_product(knowledge)
    agent = FakeToolModel(SEARCH, recommending(bed, "supported", "unknown"))
    app = rec_path(agent, knowledge.db, knowledge.embedder)
    item = rec_case(recommends=frozenset({"測試床墊 TM-1"}))

    played = await play(app, item, knowledge.version_id, run=0)

    assert played.right and played.action == "recommend" and played.shown == ("測試床墊 TM-1",)
    assert [step.node for step in played.flow] == ["rec_agent", "rec_tools"] * 2
    assert played.flow[0].note == 'search_products {"functions": ["離床偵測"]}'
    assert played.flow[-1].note.endswith("recommend: 測試床墊 TM-1")
    assert played.searches == 1 and set(played.nodes) == {"rec_agent", "rec_tools"}
    assert played.seconds == pytest.approx(sum(step.seconds for step in played.flow))


async def test_a_play_that_fails_is_wrong_and_says_why(knowledge):  # noqa: F811
    app = rec_path(FakeToolModel(), knowledge.db, knowledge.embedder)  # no reply to give

    played = await play(app, rec_case(), knowledge.version_id, run=0)

    assert not played.right and played.error and played.problems == ()


async def test_agents_take_turns_and_the_report_shows_each_flow(knowledge):  # noqa: F811
    asks = AIMessage("請問是在家裡用嗎？")
    agents = [
        (Candidate("asks", "none"), FakeToolModel(asks)),
        (Candidate("searches", "none"), FakeToolModel(SEARCH, asks)),
    ]
    item = rec_case(actions=("replied",), searches=False)

    results = await compare(
        agents, [item], knowledge.db, knowledge.embedder, knowledge.version_id, 1
    )

    assert [r.correct_per_run() for r in results] == [[1], [0]]
    assert results[1].failures()["RC-1"][0].problems == ("searched",)
    report = recommend_markdown(results, [item], {"runs": 1})
    assert "| 1 | rec_agent |" in report and "replied: 請問是在家裡用嗎？" in report
    assert "| RC-1 | replied · 2 fit · 1 never · count 1 · no search |" in report


def result(label: str, right: int, seconds: float) -> RecResult:
    plays = [
        RecPlay(
            f"C{n}",
            0,
            "recommend",
            (),
            0,
            () if n < right else ("x",),
            None,
            "",
            (),
            seconds,
            {},
            1,
            100,
            10,
        )
        for n in range(3)
    ]
    return RecResult(Candidate(label, "none"), 1, plays)


def test_the_rule_keeps_the_right_then_takes_the_fastest():
    slow_best, fast_close, fast_poor = result("a", 3, 9.0), result("b", 2, 4.0), result("c", 0, 1.0)

    assert rule_pick([slow_best, fast_close, fast_poor]) is fast_close
