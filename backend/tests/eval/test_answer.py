from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import HumanMessage

from tests.agent.fakes import fake_chat
from wobot.eval.answer import (
    AnswerCase,
    AnswerResult,
    Reply,
    answer_cases,
    compare,
    rule_pick,
    scored_cases,
)
from wobot.eval.candidates import Candidate
from wobot.eval.dataset import Case, load_dataset
from wobot.eval.report import answer_markdown
from wobot.eval.rewrite import Planned

EVIDENCE = {
    "passages": [{"header": "GRC › 首頁", "text": "Since 2003", "records": []}],
    "records": [],
}


def case(case_id: str, **check) -> Case:
    return Case(
        case_id=case_id,
        scenario="test",
        split="dev",
        user_input="GRC 是哪一年成立的？",
        expected_behavior="",
        reference=None,
        check={"kind": "knowledge", **check},
        pending=None,
    )


def item(case_id: str = "KN-1", mentions=("2003",), no_info=False) -> AnswerCase:
    return AnswerCase(case(case_id), "GRC 是哪一年成立的？", EVIDENCE, tuple(mentions), no_info)


def test_the_cases_are_those_whose_reply_is_checked():
    cases = scored_cases([load_dataset("knowledge-v1")], ["dev"])
    ids = {c.case_id for c in cases}

    assert {"KN-001", "KN-005", "KN-015"} <= ids  # by its texts, or read by a person
    assert all(c.split == "dev" for c in cases)


async def test_each_case_is_rewritten_and_searched_once_before_any_reply():
    searched = []

    async def rewriter(messages):
        assert messages == [HumanMessage("GRC 是哪一年成立的？")]
        return Planned("元智大學老人福祉科技研究中心是哪一年成立的？", ["q-zh", "q-en"], None)

    async def lookup(queries, name):
        searched.append((queries, name))
        return EVIDENCE

    [prepared] = await answer_cases([case("KN-1", mentions=["2003"])], rewriter, lookup)

    assert searched == [(["q-zh", "q-en"], None)]
    assert prepared.question == "元智大學老人福祉科技研究中心是哪一年成立的？"
    assert (prepared.evidence, prepared.mentions, prepared.scored) == (EVIDENCE, ("2003",), True)


async def test_a_reply_is_streamed_timed_and_checked_for_its_texts():
    lines: list[str] = []
    cases = [item("KN-1"), item("KN-2", mentions=[["455-5726", "4555726"]])]

    [result] = await compare(
        [(Candidate("gpt-6-luna", "none"), fake_chat("GRC 成立於 2003 年。", "電話不詳。"))],
        cases,
        runs=1,
        progress=lines.append,
    )

    right, wrong = result.replies
    assert right.text == "GRC 成立於 2003 年。" and not right.lacking
    assert 0 <= right.first_token_seconds <= right.seconds
    assert wrong.lacking == ("455-5726 / 4555726",)
    assert result.correct_per_run() == [1]
    assert lines[1].startswith("run 1/1 case 2/2 KN-2: gpt-6-luna:none MISS")


async def test_a_reply_the_sources_cannot_check_is_read_not_scored():
    [result] = await compare(
        [(Candidate("gpt-6-luna", "none"), fake_chat("資料沒有寫保固。"))],
        [item("KN-15", mentions=(), no_info=True)],
        runs=1,
    )

    assert result.correct_per_run() == [0] and result.failures() == {}


async def test_a_failed_reply_is_wrong_and_left_out_of_the_latencies():
    broken = GenericFakeChatModel(messages=iter([]))  # nothing to say: it raises

    [result] = await compare([(Candidate("gpt-6-luna", "none"), broken)], [item()], 1)

    assert result.errors == 1 and result.done == []
    assert result.correct_per_run() == [0] and "KN-1" in result.failures()


def result(model: str, right: list[int], seconds: float) -> AnswerResult:
    """A candidate whose first `right[run]` of 10 checked replies hold their texts."""
    r = AnswerResult(Candidate(model, "none"), len(right), frozenset(f"KN-{i}" for i in range(10)))
    for run, count in enumerate(right):
        for i in range(10):
            missing = () if i < count else ("2003",)
            r.replies.append(Reply(f"KN-{i}", run, "答", 0.5, seconds, 5000, 60, missing))
    return r


def test_the_rule_keeps_the_correct_then_takes_the_fastest():
    slow_best = result("gpt-4o", [10, 10], seconds=3.0)
    fast_close = result("gpt-6-luna", [9, 10], seconds=1.5)
    faster_worse = result("gpt-5.6-luna", [7, 7], seconds=1.0)

    assert rule_pick([slow_best, fast_close, faster_worse]) is fast_close


def test_the_report_shows_each_reply_and_what_it_lacked():
    luna = result("gpt-6-luna", [9], seconds=1.5)
    cases = [item(f"KN-{i}") for i in range(10)]

    markdown = answer_markdown([luna], cases, {"runs": 1})

    assert "| gpt-6-luna:none | 9.0 / 10 (9–9) |" in markdown
    assert "| KN-9 | 1× lacks 2003 |" in markdown
    assert "Read as: GRC 是哪一年成立的？ · 1 passages, 0 records · expected: 2003" in markdown
    assert "- **gpt-6-luna:none**: 答" in markdown


def test_history_is_shown_before_the_message():
    with_history = AnswerCase(
        Case("KN-1", "t", "dev", "那它能偵測離床嗎？", "", None, {"kind": "knowledge"}, None,
             history=[{"user": "WhizPad 是什麼？", "answer": "智慧床墊。"}]),
        "WhizPad 能偵測離床嗎？", EVIDENCE, ("離床",), False,
    )  # fmt: skip

    markdown = answer_markdown([result("gpt-6-luna", [0], 1.0)], [with_history], {})

    assert "> WhizPad 是什麼？\n>\n> — 智慧床墊。" in markdown
