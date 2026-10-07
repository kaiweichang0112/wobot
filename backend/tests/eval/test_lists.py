from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage

from tests.agent.conftest import knowledge  # noqa: F401  # a small published version
from tests.agent.fakes import FakeStructuredModel, FakeToolModel, calls
from tests.agent.test_list_agent import Unreachable
from wobot.agent.list_agent import result_handle
from wobot.agent.write_list import ListIntro, ShownList
from wobot.eval import gold
from wobot.eval.candidates import Candidate
from wobot.eval.corpus import Corpus, Item
from wobot.eval.dataset import Case, load_dataset
from wobot.eval.lists import (
    AgentResult,
    ListCase,
    ListPlay,
    ModelWriter,
    agent_rule_pick,
    calls_right,
    compare_agents,
    compare_writers,
    counted_calls,
    expected_items,
    list_cases,
    list_f1,
    list_path,
    play,
    write_cases,
)
from wobot.eval.metrics import SetScore
from wobot.eval.report import list_agent_markdown, write_list_markdown
from wobot.knowledge.lists import RecordQuery

QUERY_TIME = datetime(2026, 10, 7, 10, tzinfo=ZoneInfo("Asia/Taipei"))
MASTERS = {"tool": "list_students", "degree": "master"}


def case(case_id: str = "LS-1", user_input: str = "列出所有碩士畢業生") -> Case:
    return Case(case_id, "test", "dev", user_input, "", None, {"kind": "list_path"}, None)


def item(case_id="LS-1", accepted=((MASTERS,),), gold_keys=None, mentions=(), no_lists=False):
    labelled = None if gold_keys is None else frozenset(gold_keys)
    return ListCase(case(case_id), QUERY_TIME, accepted, labelled, tuple(mentions), no_lists)


# --- Which calls are right --------------------------------------------------------------


YEARS = {"tool": "list_students", "degree": "master", "year_from": 2021, "year_to": 2021}
SPAN = {"tool": "list_students", "degree": "master", "year_from": 2021, "year_to": 2022}
NEXT = {**YEARS, "year_from": 2022, "year_to": 2022}


@pytest.mark.parametrize(
    ("made", "right"),
    [
        ([YEARS, NEXT], True),
        ([NEXT, YEARS, NEXT], True),  # in any order, each counted once
        ([SPAN], True),
        ([YEARS], False),  # one year of two
        ([YEARS, NEXT, SPAN], False),
        ([{**SPAN, "last_years": 2}], False),  # a filter the case does not name
        ([{**SPAN, "degree": "phd"}], False),
        ([], False),
    ],
)
def test_calls_are_right_when_they_are_one_accepted_set(made, right):
    assert calls_right(made, [[YEARS, NEXT], [SPAN]]) is right


def test_contains_may_be_any_spelling_the_case_accepts():
    accepted = [[{"tool": "list_students", "contains": ["地墊", "感測地墊"]}]]

    assert calls_right([{"tool": "list_students", "contains": "感測地墊"}], accepted)
    assert not calls_right([{"tool": "list_students", "contains": "感知地墊"}], accepted)


def found(tool: str, filters: dict, keys=()) -> dict:
    items = [{"id": f"r-{n}", "key": key, "fields": {}} for n, key in enumerate(keys)]
    return {"tool": tool, "kind": "student", "filters": filters, "items": items, "uncertain": []}


def test_the_calls_that_count_found_something_unless_none_did():
    empty = found("list_students", {"contains": "王曉明"})
    named = found("list_students", {"contains": "王小明"}, ["student:master:王小明"])

    assert counted_calls({"q-1": empty, "q-2": named}) == [
        {"tool": "list_students", "contains": "王小明"}
    ]
    assert counted_calls({"q-1": empty}) == [{"tool": "list_students", "contains": "王曉明"}]


def test_the_cases_are_lists_v1s_with_their_labels_matched():
    student = Item(
        "student:master:朱美憶",
        "student",
        {"name": "朱美憶", "degree": "master", "graduation_year": 2022},
    )
    corpus = Corpus(version_id=1, embedding_config_id="e", strategies={}, items=[student])

    cases, gold_files = list_cases([load_dataset("lists-v1")], ["dev"], corpus, gold.GOLD_DIR)
    by_id = {c.case.case_id: c for c in cases}

    assert len(cases) == 16 and "LS-101" not in by_id
    assert "student:master:朱美憶" in by_id["LS-002"].gold
    assert any(key.startswith("unresolved") for key in by_id["LS-002"].gold)  # not in corpus
    assert by_id["LS-010"].gold is None and by_id["LS-016"].no_lists
    assert by_id["LS-009"].mentions == ("32",)
    assert by_id["LS-012"].query_time.date().isoformat() == "2026-10-07"
    assert "students-master-2021-2022.yaml" in gold_files


# --- list_agent through the path ------------------------------------------------------


def intro_for(version_id: int, *queries) -> ListIntro:
    return ListIntro(
        intro="共 1 位。",
        lists=[ShownList(result_id=result_handle(version_id, q), item_ids=None) for q in queries],
    )


async def test_a_play_scores_the_calls_and_the_items_shown(knowledge):  # noqa: F811
    agent = FakeToolModel(
        AIMessage(
            "",
            tool_calls=[
                {"name": "list_students", "args": {"degree": "master", "contains": None}, "id": "c"}
            ],
            usage_metadata={"input_tokens": 900, "output_tokens": 20, "total_tokens": 920},
        )
    )
    writer = FakeStructuredModel(
        intro_for(knowledge.version_id, RecordQuery("student", degree="master"))
    )
    app = list_path(agent, writer, knowledge.db)

    played = await play(app, item(gold_keys=["student:master:王小明"]), knowledge.version_id, 0)

    assert played.error is None
    assert played.rounds == ((MASTERS,),)  # a null filter is no filter
    assert played.first_right and played.right and played.model_calls == 1
    assert played.lists.f1 == 1.0 and played.shows_lists
    assert (played.input_tokens, played.output_tokens) == (900, 20)
    assert played.agent_seconds <= played.seconds


async def test_a_play_that_fails_is_wrong_and_says_why():
    agent = FakeToolModel(calls(("list_products", {})))
    app = list_path(agent, FakeStructuredModel(ListIntro(intro="", lists=[])), Unreachable())

    played = await play(app, item(), 1, 0)

    assert played.error == "the path reported a failure" and not played.right


async def test_agents_take_turns_and_the_rule_weighs_the_calls(knowledge):  # noqa: F811
    lines: list[str] = []
    writer = FakeStructuredModel(ListIntro(intro="", lists=[]))
    agents = [
        (
            Candidate("gpt-6-luna", "none"),
            FakeToolModel(calls(("list_students", {"degree": "master"}))),
        ),
        (
            Candidate("gpt-4o", "default"),
            FakeToolModel(calls(("list_students", {"degree": "phd"}))),
        ),
    ]

    results = await compare_agents(
        agents, writer, [item()], knowledge.db, knowledge.version_id, 1, lines.append
    )

    assert [r.correct_per_run() for r in results] == [[1], [0]]
    assert (
        lines == [lines[0]]
        and "gpt-6-luna:none ok x1" in lines[0]
        and "gpt-4o:default MISS" in lines[0]
    )
    markdown = list_agent_markdown(results, [item()], {"runs": 1})
    assert "| LS-1 | list_students {degree: master} |" in markdown
    assert "1× list_students {degree: phd}" in markdown


def played(right: bool, seconds: float, run: int = 0, model_calls: int = 1) -> ListPlay:
    return ListPlay(
        "LS-1", run, (), (), right, right, model_calls, None, (), True, "", seconds, seconds, 100, 5
    )


def test_the_rule_keeps_the_right_then_takes_the_fastest_agent():
    def result(model: str, right: int, seconds: float) -> AgentResult:
        r = AgentResult(Candidate(model, "none"), 1)
        r.plays += [played(n < right, seconds) for n in range(5)]
        return r

    slow_best = result("gpt-4o", 5, seconds=3.0)
    fast_close = result("gpt-5.6-luna", 4, seconds=1.5)
    faster_worse = result("gpt-6-luna", 2, seconds=1.0)

    assert agent_rule_pick([slow_best, fast_close, faster_worse]) is fast_close


def test_asked_again_counts_the_plays_given_a_second_round():
    result = AgentResult(Candidate("gpt-6-luna", "none"), 1)
    result.plays += [
        played(True, 1.0),
        played(True, 2.0, model_calls=2),
        played(False, 2.0, model_calls=2),
    ]

    assert result.second_rounds() == (2, 1)
    assert result.mean_model_calls == pytest.approx(5 / 3)


# --- write_list on the same lists -------------------------------------------------------


def test_a_writer_should_show_the_labelled_items_when_the_lists_hold_them():
    lists = {"q-1": found("list_students", {"degree": "master"}, ["a", "b"])}

    assert expected_items(item(gold_keys=["a"]), lists) == {"a"}
    assert expected_items(item(gold_keys=["a", "c"]), lists) is None  # no writer could
    assert expected_items(item(), lists) == {"a", "b"}  # the list the case asks for
    assert expected_items(item(accepted=(({"tool": "list_projects"},),)), lists) is None
    assert expected_items(item(no_lists=True), {}) == frozenset()


def written(intro: ListIntro, tokens=(300, 40)) -> FakeStructuredModel:
    raw = AIMessage(
        "",
        usage_metadata={
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "total_tokens": sum(tokens),
        },
    )
    return FakeStructuredModel({"raw": raw, "parsed": intro, "parsing_error": None})


async def test_writers_read_the_same_lists_and_are_scored_by_what_they_show(knowledge):  # noqa: F811
    finder = list_path(
        FakeToolModel(calls(("list_students", {"degree": "master"}))),
        FakeStructuredModel(ListIntro(intro="", lists=[])),
        knowledge.db,
    )
    [prepared] = await write_cases([item(mentions=["1 位"])], finder, knowledge.version_id)
    [result_id] = prepared.results
    shows = ListIntro(intro="共 1 位碩士。", lists=[ShownList(result_id=result_id, item_ids=None)])
    hides = ListIntro(intro="沒有。", lists=[])

    results = await compare_writers(
        [
            (Candidate("gpt-6-luna", "none"), ModelWriter(written(shows))),
            (Candidate("gpt-4o", "default"), ModelWriter(written(hides))),
        ],
        [prepared],
        runs=1,
    )

    assert prepared.expected == {"student:master:王小明"}
    good, bad = results
    assert good.correct_per_run() == [1] and good.mean_tokens == (300, 40)
    assert bad.correct_per_run() == [0]
    assert bad.replies[0].missing == ("student:master:王小明",) and bad.replies[0].lacking == (
        "1 位",
    )
    markdown = write_list_markdown(results, [prepared], {"runs": 1})
    assert "1× misses 1, lacks 1 位" in markdown
    assert "- **gpt-6-luna:none**: 共 1 位碩士。" in markdown


def test_showing_none_of_the_labelled_items_scores_zero():
    nothing = SetScore(expected=3, actual=0, hits=0, precision=None, recall=0.0)
    rightly_nothing = SetScore(expected=0, actual=0, hits=0, precision=None, recall=None)
    some = SetScore(expected=2, actual=1, hits=1, precision=1.0, recall=0.5)

    assert list_f1(nothing) == 0.0 and list_f1(rightly_nothing) == 1.0
    assert list_f1(some) == pytest.approx(2 / 3) and list_f1(None) is None
