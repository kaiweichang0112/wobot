import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from tests.agent.fakes import FakeStructuredModel
from wobot.agent.rewrite import SearchPlan
from wobot.eval import gold
from wobot.eval.candidates import Candidate, lacking
from wobot.eval.corpus import Corpus, Item
from wobot.eval.dataset import Case, load_dataset
from wobot.eval.report import rewrite_markdown
from wobot.eval.rewrite import (
    BASELINE,
    DATASETS,
    ModelRewriter,
    Planned,
    RewriteCase,
    RewriteResult,
    Searched,
    as_is,
    compare,
    play,
    rewrite_cases,
    rule_pick,
)

STUDENT = Item(
    "student:master:張維益",
    "student",
    {"name": "張維益", "degree": "master", "graduation_year": 2013},
)


def test_the_cases_are_those_with_something_a_rewrite_changes():
    corpus = Corpus(version_id=1, embedding_config_id="e", strategies={}, items=[STUDENT])
    datasets = [load_dataset(name) for name in DATASETS]

    cases, gold_files = rewrite_cases(datasets, ["dev"], corpus, gold.GOLD_DIR)
    by_id = {c.case.case_id: c for c in cases}

    assert {"CASE-001", "ITEM-01", "KN-001", "KN-012"} <= set(by_id)
    # Nothing answers these, or only the answer is checked: the rewrite is not scored.
    assert not {"KN-005", "KN-015"} & set(by_id)
    assert by_id["KN-012"].relevant == {"student:master:張維益"}
    assert by_id["KN-012"].name == "張維益"
    # A label no record matches stays, so the search cannot find it: recall counts it.
    assert all(key.startswith("unresolved") for key in by_id["KN-001"].relevant)
    assert {"retrieval.yaml", "knowledge.yaml"} <= set(gold_files)


def case(case_id: str = "KN-1", relevant=("student:master:張維益",), **check) -> RewriteCase:
    played = Case(
        case_id=case_id,
        scenario="test",
        split="dev",
        user_input="那張維益的呢？",
        expected_behavior="",
        reference=None,
        check={"kind": "knowledge", **check},
        pending=None,
        history=[{"user": "張凱維的論文題目是什麼", "answer": "「活動感知地墊…」"}],
    )
    return RewriteCase(
        "knowledge-v1",
        played,
        frozenset(relevant),
        tuple(check.get("question_mentions", ())),
        check.get("name"),
    )


def planned(question="張維益的論文題目是什麼？", name="張維益") -> Planned:
    return Planned(question, [question, "Wei-Yi Chang thesis"], name, 300, 40)


def rewriter(plan: Planned):
    calls: list[list[BaseMessage]] = []

    async def rewrite(messages):
        calls.append(list(messages))
        return plan

    rewrite.calls = calls
    return rewrite


def search_finding(*keys: str):
    async def search(queries, name):
        return list(keys) + ([f"student:master:{name}"] if name else [])

    return search


@pytest.mark.parametrize(
    ("plan", "passed", "problem"),
    [
        (planned(), True, None),
        (planned(name=None), False, "missed"),  # the record is found only by name
        (planned(question="他的論文題目是什麼？"), False, "lacks"),
        (planned(name="張維"), False, "name"),
    ],
)
async def test_a_case_passes_when_the_search_finds_all_and_the_plan_is_as_asked(
    plan, passed, problem
):
    item = case(question_mentions=["張維益"], name="張維益")

    searched = await play(rewriter(plan), search_finding(), item, run=0)

    assert searched.passed is passed
    if problem == "missed":
        assert searched.missing == ("student:master:張維益",) and searched.recall == 0
    if problem == "lacks":
        assert searched.lacking == ("張維益",) and searched.question_ok is False
    if problem == "name":
        assert searched.name_ok is False


async def test_any_item_of_an_any_of_answers_for_it():
    group = {"section:grc:home": "any of k.yaml:KN-1#1", "section:bio": "any of k.yaml:KN-1#1"}
    item = RewriteCase(
        "knowledge-v1", case().case, frozenset({"any of k.yaml:KN-1#1"}), (), None, group
    )

    searched = await play(rewriter(planned(name=None)), search_finding("section:bio"), item, 0)

    assert searched.passed and searched.recall == 1


async def test_the_rewrite_reads_the_earlier_turns():
    rewrite = rewriter(planned())

    await play(rewrite, search_finding(), case(), run=0)

    assert rewrite.calls[0] == [
        HumanMessage("張凱維的論文題目是什麼"),
        AIMessage("「活動感知地墊…」"),
        HumanMessage("那張維益的呢？"),
    ]


async def test_the_baseline_searches_the_message_as_it_is():
    plan = await as_is([HumanMessage("早安"), AIMessage("早！"), HumanMessage("GRC 哪年成立？")])

    assert (plan.queries, plan.name, plan.input_tokens) == (["GRC 哪年成立？"], None, 0)


async def test_a_failed_rewrite_fails_the_case_and_is_reported():
    async def failing(messages):
        raise TimeoutError("no answer")

    searched = await play(failing, search_finding(), case(), run=0)

    assert not searched.passed and "TimeoutError" in searched.error
    assert searched.recall == 0


async def test_a_model_rewriter_reads_the_plan_the_node_would():
    model = FakeStructuredModel(
        {
            "parsed": SearchPlan(question_zh=" WhizPad 能偵測離床嗎？ ", question_en="", name=" "),
            "raw": AIMessage(
                "", usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
            ),
            "parsing_error": None,
        }
    )

    plan = await ModelRewriter(model)([HumanMessage("WhizPad 能偵測離床嗎？")])

    # The node's own reading: trimmed, a blank language left out, a blank name none.
    assert plan == Planned("WhizPad 能偵測離床嗎？", ["WhizPad 能偵測離床嗎？"], None, 5, 2)


async def test_each_candidate_and_the_baseline_play_each_case_in_every_run():
    lines: list[str] = []

    results = await compare(
        [(BASELINE, as_is), (Candidate("gpt-6-luna", "none"), rewriter(planned()))],
        [case(name="張維益")],
        search_finding(),
        runs=2,
        progress=lines.append,
    )

    baseline, model = results
    assert baseline.passed_per_run() == [0, 0] and model.passed_per_run() == [1, 1]
    assert baseline.cost_per_thousand == 0.0
    assert lines[0] == "run 1/2 case 1/1 KN-1: as-is FAIL 0.0s; gpt-6-luna:none ok 0.0s"


def result(model: str, passed: list[int], seconds: float) -> RewriteResult:
    r = RewriteResult(Candidate(model, "none"), runs=len(passed))
    for run, right in enumerate(passed):
        for i in range(10):
            missing = () if i < right else ("product:x",)
            r.plays.append(
                Searched("seed-v1", f"CASE-{i}", run, 1, False, seconds, missing=missing)
            )
    return r


def test_the_rule_chooses_among_the_models_not_the_baseline():
    baseline = result("as-is", [10], seconds=0.0)
    baseline.candidate = BASELINE
    luna = result("gpt-6-luna", [9], seconds=1.0)

    assert rule_pick([baseline, luna]) is luna


def test_the_report_shows_recall_and_each_failure():
    luna = result("gpt-6-luna", [9, 10], seconds=1.0)

    markdown = rewrite_markdown([luna], [], {"runs": 2})

    assert "| gpt-6-luna:none | 9.5 / 10 (9–10) | 0.950 | — | 0 / 0 |" in markdown
    assert "| CASE-9 | 1× missed 1/1 |" in markdown


@pytest.mark.parametrize(
    ("text", "mentions", "lacks"),
    [
        ("電話是 03 4555726", ["455-5726"], []),
        ("研究 Gerontechnology", [["老人福祉科技", "gerontechnology"]], []),
        ("地址在中壢", ["遠東路135號", "455-5726"], ["遠東路135號", "455-5726"]),
        ("可以，有三階段離床提醒", [["離床預警", "離床提醒"]], []),
        ("沒有提到", [["離床預警", "離床提醒"]], ["離床預警 / 離床提醒"]),
    ],
)
def test_mentions_are_matched_ignoring_case_spaces_and_dashes(text, mentions, lacks):
    assert lacking(text, mentions) == lacks
