import asyncio
import json

import pytest

from tests.knowledge.openai_responses import openai_answering
from wobot.eval.dataset import Case, Dataset
from wobot.eval.judge import (
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    REASONING_EFFORT,
    RUBRIC,
    STATEMENTS,
    VERDICTS,
    Grade,
    JudgeInput,
    OpenAIJudge,
    Rubric,
    Statements,
    StatementVerdict,
    StatementVerdicts,
    Usage,
    by_candidate,
    cause_of,
    faithfulness_of,
    found_keys,
    judge_inputs,
    judge_one,
    judge_replies,
)
from wobot.eval.report import judge_markdown
from wobot.knowledge.hashing import content_hash

# Verdicts of another prompt answered another question. After changing a prompt, a
# schema or the request settings, bump PROMPT_VERSION and record the new fingerprint here.
PROMPT_FINGERPRINTS = {
    1: "575e341c11187fc48d374ca717c1b991428533e35f892d9c141f8dc93e2e6fa9",
    2: "ab9fa4a5bfa55ea5f2d319384378226474617d0cbc624061b2e4ffadb1febf15",
}

EVIDENCE = {
    "passages": [
        {
            "id": "k-1",
            "header": "WhizPad 型錄 › 第 4 頁",
            "text": "醫療級防水外布套，可直接以濕布擦拭。",
            "records": [{"id": "r-1", "key": "document_page:whizpad_catalog:4"}],
        }
    ],
    "records": [
        {
            "id": "r-2",
            "key": "student:master:吳竹右",
            "type": "student",
            "fields": {"name": "吳竹右", "graduation_year": 2018},
        }
    ],
}

ITEM = JudgeInput(
    candidate="model-a:none",
    case_id="KN-009",
    run=0,
    message="WhizPad 的布套可以拆下來洗嗎？",
    question="WhizPad 安心臥智慧床墊的布套可以拆下來洗嗎？",
    expected="Say the cover comes off and can be washed.",
    evidence=EVIDENCE,
    reply="可以拆下清潔。",
    labelled=frozenset({"document_page:whizpad_catalog:9"}),
    found=found_keys(EVIDENCE),
    mentions_passed=True,
)


def output_text(output):
    return {
        "type": "output_text",
        "text": json.dumps(output, ensure_ascii=False),
        "annotations": [],
    }


def grades(expected=True, supported=True, language=True):
    def grade(passed):
        return {"reason": "ok" if passed else "the reply says 可以拆", "passed": passed}

    return {
        "expected": grade(expected),
        "supported": grade(supported),
        "language": grade(language),
    }


def test_a_changed_prompt_comes_with_a_new_version():
    fingerprint = content_hash(
        {
            "rubric": RUBRIC,
            "statements": STATEMENTS,
            "verdicts": VERDICTS,
            "schemas": [
                Rubric.model_json_schema(),
                Statements.model_json_schema(),
                StatementVerdicts.model_json_schema(),
            ],
            "reasoning_effort": REASONING_EFFORT,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }
    )

    assert PROMPT_FINGERPRINTS.get(PROMPT_VERSION) == fingerprint


# --- The requests -------------------------------------------------------------------------


async def test_grades_the_reply_on_what_its_answer_read():
    requests = []
    client = openai_answering(output_text(grades(expected=False)), requests)
    usage = Usage()

    rubric = await OpenAIJudge(client, "judge-test").rubric(ITEM, usage)

    assert rubric.expected == Grade(reason="the reply says 可以拆", passed=False)
    assert usage == Usage(calls=1, input_tokens=420, output_tokens=31)
    (sent,) = requests
    assert sent["instructions"] == RUBRIC
    assert json.loads(sent["input"]) == {
        "message": "WhizPad 的布套可以拆下來洗嗎？",
        "question": "WhizPad 安心臥智慧床墊的布套可以拆下來洗嗎？",
        "expected": "Say the cover comes off and can be washed.",
        # As the answer saw them: no handles, no keys.
        "passages": [
            {"header": "WhizPad 型錄 › 第 4 頁", "text": "醫療級防水外布套，可直接以濕布擦拭。"}
        ],
        "records": [{"type": "student", "name": "吳竹右", "graduation_year": 2018}],
        "reply": "可以拆下清潔。",
    }
    assert sent["text"]["format"]["name"] == "Rubric"
    assert sent["text"]["format"]["strict"] is True
    assert sent["reasoning"] == {"effort": REASONING_EFFORT}
    assert sent["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert sent["store"] is False


async def test_breaks_the_reply_into_statements_then_judges_each():
    requests = []
    statements = {"statements": ["布套可以拆下清潔。"]}
    verdicts = {
        "statements": [
            {"statement": "布套可以拆下清潔。", "reason": "only wiping", "supported": False}
        ]
    }
    client = openai_answering([output_text(statements), output_text(verdicts)], requests)
    usage = Usage()

    judged = await OpenAIJudge(client, "judge-test").faithfulness(ITEM, usage)

    assert judged == [
        StatementVerdict(statement="布套可以拆下清潔。", reason="only wiping", supported=False)
    ]
    assert usage.calls == 2
    made, checked = requests
    assert made["instructions"] == STATEMENTS
    assert json.loads(made["input"]) == {
        "question": "WhizPad 安心臥智慧床墊的布套可以拆下來洗嗎？",
        "reply": "可以拆下清潔。",
    }
    assert checked["instructions"] == VERDICTS
    sent = json.loads(checked["input"])
    assert sent["statements"] == ["布套可以拆下清潔。"]
    assert set(sent) == {"passages", "records", "statements"}


async def test_a_reply_that_states_nothing_takes_one_request():
    requests = []
    client = openai_answering(output_text({"statements": []}), requests)

    judged = await OpenAIJudge(client, "judge-test").faithfulness(ITEM, Usage())

    assert judged == [] and len(requests) == 1


# --- What a verdict means -----------------------------------------------------------------


def verdict(supported):
    return StatementVerdict(statement="s", reason="r", supported=supported)


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([], (None, None)),
        ([verdict(True), verdict(True)], (1.0, True)),
        # One unsupported answer among statements about what the sources leave out.
        ([verdict(False), verdict(True), verdict(True), verdict(True)], (0.75, False)),
    ],
    ids=["nothing stated", "all supported", "one not"],
)
def test_faithfulness_is_the_share_supported_and_whether_all_are(verdicts, expected):
    assert faithfulness_of(verdicts) == expected


@pytest.mark.parametrize(
    ("passed", "labelled", "found", "cause"),
    [
        (True, {"a"}, set(), None),
        (False, {"a"}, {"b"}, "retrieval"),
        (False, {"a", "c"}, {"c"}, "answer"),
        (False, set(), {"b"}, "answer"),
    ],
    ids=["passed", "labelled item not found", "one found", "nothing labelled"],
)
def test_a_miss_is_put_down_to_retrieval_only_when_nothing_labelled_was_found(
    passed, labelled, found, cause
):
    assert cause_of(passed, frozenset(labelled), frozenset(found)) == cause


def test_found_keys_come_from_passages_and_records():
    assert found_keys(EVIDENCE) == {"document_page:whizpad_catalog:4", "student:master:吳竹右"}


# --- From a saved run ---------------------------------------------------------------------


def knowledge_case(case_id, expected):
    return Case(
        case_id=case_id,
        scenario="product_detail",
        split="dev",
        user_input=f"{case_id} message",
        expected_behavior=expected,
        reference=None,
        check={"kind": "knowledge", "mentions": ["x"]},
        pending=None,
    )


RUN = {
    "meta": {"dataset": "knowledge-v1 abc"},
    "cases": [
        {"case_id": "KN-1", "question": "q1", "mentions": ["x"], "evidence": EVIDENCE},
        {"case_id": "KN-2", "question": "q2", "mentions": [], "evidence": EVIDENCE},
    ],
    "candidates": [
        {
            "candidate": "model-a:none",
            "replies": [
                {"case_id": "KN-1", "run": 0, "text": "x", "lacking": [], "error": None},
                {"case_id": "KN-2", "run": 0, "text": "", "lacking": [], "error": "boom"},
                {"case_id": "KN-2", "run": 1, "text": "y", "lacking": [], "error": None},
            ],
        },
        {
            "candidate": "model-b:none",
            "replies": [
                {"case_id": "KN-1", "run": 0, "text": "z", "lacking": ["x"], "error": None}
            ],
        },
    ],
}
DATASET = Dataset(
    "knowledge-v1",
    "",
    "",
    [knowledge_case("KN-1", "Say x."), knowledge_case("KN-2", "Say the sources do not say.")],
    "abc",
)


def test_takes_every_reply_of_the_chosen_candidates_that_came():
    items = judge_inputs(RUN, DATASET, {"KN-1": frozenset({"k"})}, ["model-a:none"])

    assert [(i.case_id, i.run) for i in items] == [("KN-1", 0), ("KN-2", 1)]
    first, second = items
    assert first.message == "KN-1 message" and first.question == "q1"
    assert first.expected == "Say x." and first.labelled == {"k"}
    assert first.mentions_passed is True
    assert second.mentions_passed is None  # a case the text check did not score
    assert len(judge_inputs(RUN, DATASET, {})) == 3  # every candidate by default


class FakeJudge:
    label = "fake:low"

    def __init__(self, rubric=None, fail_on=()):
        self._rubric = rubric or grades()
        self._fail_on = fail_on

    async def rubric(self, item, usage):
        usage.calls += 1
        usage.input_tokens += 1000
        usage.output_tokens += 100
        if item.case_id in self._fail_on:
            raise RuntimeError("judge down")
        return Rubric.model_validate(self._rubric)

    async def faithfulness(self, item, usage):
        await asyncio.sleep(0)
        usage.calls += 2
        return [verdict(True), verdict(False)]


async def test_a_judged_reply_keeps_its_verdicts_and_cause():
    judged = await judge_one(FakeJudge(grades(expected=False)), ITEM)

    assert judged.criteria["expected"] == {"reason": "the reply says 可以拆", "passed": False}
    assert judged.cause == "retrieval"  # page 9 was labelled, page 4 found
    assert (judged.faithfulness, judged.grounded) == (0.5, False)
    assert judged.unlike_mentions  # the text check passed it
    assert judged.calls == 3 and judged.error is None


async def test_a_failed_judge_call_is_the_replys_error_not_a_verdict():
    items = judge_inputs(RUN, DATASET, {})

    judged = await judge_replies(FakeJudge(fail_on={"KN-2"}), items)

    (a, b) = by_candidate(judged)
    assert (a.candidate, b.candidate) == ("model-a:none", "model-b:none")
    assert a.errors == 1 and len(a.verdicts) == 1
    assert a.judged[1].error == "RuntimeError('judge down')" and a.judged[1].criteria == {}
    assert b.passes("expected") == 1 and b.grounded == 0 and b.stated == 1
    assert b.faithfulness == 0.5
    assert [j.case_id for j in b.unlike_mentions] == ["KN-1"]  # the text check failed it


async def test_the_report_counts_each_criterion_and_lists_the_disagreements():
    items = judge_inputs(RUN, DATASET, {"KN-1": frozenset({"k"})}, ["model-b:none"])
    results = by_candidate(await judge_replies(FakeJudge(grades(supported=False)), items))

    text = judge_markdown(results, {"judge": "gpt-6.1-sol:low", "run": "x-answer.json"})

    assert "| model-b:none | 1 / 1 | 0 / 0 | 0 / 1 | 1 / 1 | 0 / 1 | 0.50 | 1 |" in text
    assert "- **KN-1 model-b:none run 0**: the text check fails; expected passes: ok" in text
    assert "- supported FAILS: the reply says 可以拆" in text
    assert "- unsupported: s (r)" in text
