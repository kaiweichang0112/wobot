import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.agent.fakes import FakeRouter
from wobot.agent.route import Routed
from wobot.eval.dataset import Case, load_dataset
from wobot.eval.report import route_markdown
from wobot.eval.route import (
    Candidate,
    CandidateResult,
    Played,
    case_messages,
    compare,
    parse_candidate,
    percentile,
    route_cases,
    rule_pick,
)


def case(case_id: str, expect: list[str], **extra) -> Case:
    return Case(
        case_id=case_id,
        scenario="test",
        split="dev",
        user_input=extra.pop("user_input", "你好"),
        expected_behavior="",
        reference=None,
        check={"kind": "route", "expect": expect},
        pending=None,
        **extra,
    )


def test_the_route_dataset_has_dev_cases_to_compare_on():
    cases = route_cases([load_dataset("route-v1")], ["dev"])

    assert len(cases) >= 20
    assert all(set(c.check["expect"]) <= {"chat", "knowledge", "list", "recommend"} for c in cases)


@pytest.mark.parametrize(
    ("spec", "candidate"),
    [
        ("gpt-6-luna:none", Candidate("gpt-6-luna", "none")),
        ("jev-latest", Candidate("jev-latest", "-")),
    ],
)
def test_a_candidate_is_a_model_and_its_effort(spec, candidate):
    assert parse_candidate(spec) == candidate


def test_an_openai_candidate_needs_its_effort():
    with pytest.raises(ValueError, match="effort"):
        parse_candidate("gpt-6-luna")


def test_a_case_plays_its_history_before_its_message():
    played = case(
        "RT-1",
        ["list"],
        user_input="那 2022 年的呢？",
        history=[{"user": "列出 2021 年的碩士畢業生。", "answer": "共有 5 位。"}],
    )

    assert case_messages(played) == [
        HumanMessage("列出 2021 年的碩士畢業生。"),
        AIMessage("共有 5 位。"),
        HumanMessage("那 2022 年的呢？"),
    ]


async def test_each_candidate_plays_each_case_in_every_run():
    cases = [case("RT-1", ["chat"]), case("RT-2", ["list"], pending_question="哪一年？")]
    chat, lists = FakeRouter("chat"), FakeRouter("list")

    lines: list[str] = []

    results = await compare(
        [(Candidate("a", "none"), chat), (Candidate("b", "none"), lists)],
        cases,
        runs=2,
        progress=lines.append,
    )

    assert [r.correct_per_run() for r in results] == [[1, 1], [1, 1]]
    assert len(lines) == 4  # one line per case and run, so a long comparison shows it moves
    assert (
        lines[0].startswith("run 1/2 case 1/2 RT-1: a:none ok") and "b:none MISS list" in lines[0]
    )
    assert len(chat.calls) == 4
    assert chat.calls[1][1] == "哪一年？"


class Failing:
    async def __call__(self, messages, pending_question) -> Routed:
        raise TimeoutError("no answer")


async def test_a_failed_call_is_a_miss_and_an_error():
    [result] = await compare([(Candidate("a", "none"), Failing())], [case("RT-1", ["chat"])], 1)

    assert result.correct_per_run() == [0] and result.errors == 1
    assert "TimeoutError" in result.misses()["RT-1"][0].error


def test_percentiles_are_by_nearest_rank():
    seconds = [0.1 * i for i in range(1, 21)]

    assert percentile(seconds, 0.5) == pytest.approx(1.0)
    assert percentile(seconds, 0.95) == pytest.approx(1.9)


def result(model: str, correct: list[int], seconds: float, tokens: int = 400) -> CandidateResult:
    """A candidate that got `correct[run]` of 10 cases right in each run."""
    r = CandidateResult(Candidate(model, "none"), runs=len(correct))
    for run, right in enumerate(correct):
        for i in range(10):
            got = "chat" if i < right else "list"
            r.plays.append(Played(f"RT-{i}", run, ("chat",), got, None, seconds, tokens, 5))
    return r


def test_the_rule_keeps_the_accurate_then_takes_the_fastest():
    slow_best = result("gpt-5.5", [10, 10], seconds=2.0)
    fast_close = result("gpt-6-luna", [9, 9], seconds=0.8)
    faster_worse = result("gpt-5.6-luna", [7, 8], seconds=0.5)

    assert rule_pick([slow_best, fast_close, faster_worse]) is fast_close


def test_among_the_fastest_the_rule_takes_the_cheapest():
    luna = result("gpt-6-luna", [10, 10], seconds=1.00)
    sol = result("gpt-6.1-sol", [10, 10], seconds=0.95)

    assert rule_pick([luna, sol]) is luna  # within a tenth of the fastest, and cheaper


def test_the_report_names_the_misses_by_case():
    markdown = route_markdown([result("gpt-6-luna", [9, 10], 0.8)], {"runs": 2})

    assert "| gpt-6-luna:none | 9.5 / 10 (9–10) | 0.950 |" in markdown
    assert "| RT-9 | chat | list |" in markdown
    assert "picks **gpt-6-luna:none**" in markdown
