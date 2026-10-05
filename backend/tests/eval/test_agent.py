import json
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from tests.agent.fakes import ScriptedChatModel, answers, calls
from wobot.agent.build import build_agent
from wobot.agent.tools import SEARCHED_CHUNKS, TurnContext, build_tools, result_handle
from wobot.eval.agent import (
    AgentCaseResult,
    ToolUse,
    TurnRecord,
    called_tools,
    play,
    record_turn,
    run_agent_checks,
    tools_passed,
)
from wobot.eval.corpus import load_corpus
from wobot.eval.dataset import Case, Dataset
from wobot.eval.report import agent_markdown, agent_summary
from wobot.knowledge.lists import RecordQuery
from wobot.knowledge.models import Chunk, IndexVersionChunk

LECTURES = {"record_type": "lecture"}


def usage(input_tokens=100, output_tokens=10):
    total = input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total}


def reply(text, input_tokens=100, output_tokens=10, lists=()):
    """A final reply in the answer schema, with what it cost."""
    return answers(text, lists, usage_metadata=usage(input_tokens, output_tokens))


def phases(*texts):
    """Reply content as the Responses API returns it: text blocks, each with a phase."""
    return [{"type": "text", "text": text, "phase": phase} for phase, text in texts]


async def run(knowledge, script, *turns):
    model = ScriptedChatModel(script=script)
    agent = build_agent(model, build_tools(knowledge.db, knowledge.embedder), InMemorySaver())
    context = TurnContext("eval", datetime(2026, 10, 4, tzinfo=UTC), knowledge.version_id)
    return model, await play(agent, turns, context)


async def test_a_turn_keeps_its_tools_answer_and_cost(knowledge):
    script = [calls("query_records", LECTURES), reply("兩場。", input_tokens=300)]

    _, (record,) = await run(knowledge, script, "列出演講")

    assert record.tools == [ToolUse("query_records", LECTURES, "found")]
    assert record.answer == "兩場。"
    assert (record.model_calls, record.input_tokens, record.output_tokens) == (2, 300, 10)
    assert record.unsent_calls == []


async def test_a_refused_call_and_its_correction_are_both_kept(knowledge):
    refused = {"record_type": "product", "year_from": 2020}
    script = [
        calls("query_records", refused, "call-1"),
        calls("query_records", LECTURES, "call-2"),
        reply("好。"),
    ]

    _, (record,) = await run(knowledge, script, "列出演講")

    assert [tool.outcome for tool in record.tools] == ["error", "found"]


def test_a_call_written_as_text_is_unsent_and_hidden():
    written = '{"queries": ["GRC 成立"]}'
    unsent = AIMessage(phases(("commentary", written), ("final_answer", "來源中沒有查到。")))

    record = record_turn([unsent], 1.0)

    assert record.tools == []
    assert record.unsent_calls == [written]
    assert record.answer == "來源中沒有查到。"


async def test_a_call_written_as_text_breaks_the_structured_reply(knowledge):
    # The commentary and the answer reach the parser as one text, which is not JSON: the
    # turn fails rather than claiming a lookup it never made. B6 retries it once.
    written = '{"queries": ["GRC 成立"]}'
    answer = json.dumps({"answer": "來源中沒有查到。", "lists": []})
    script = [AIMessage(phases(("commentary", written), ("final_answer", answer)))]

    _, (result,) = await evaluate(knowledge, script, tools_case("T1", [["search_knowledge"]]))

    assert result.status == "error" and "StructuredOutputValidationError" in result.detail


async def test_commentary_before_a_real_call_is_not_unsent(knowledge):
    before = calls("query_records", LECTURES)
    before.content = phases(("commentary", "我來查一下。"))
    script = [before, reply("兩場。")]

    _, (record,) = await run(knowledge, script, "列出演講")

    assert record.unsent_calls == []
    assert record.answer == "兩場。"


async def test_each_turn_is_recorded_alone_and_later_turns_see_earlier_ones(knowledge):
    script = [calls("query_records", LECTURES), reply("兩場。"), reply("不客氣。")]

    model, (first, second) = await run(knowledge, script, "列出演講", "謝謝")

    assert [len(first.tools), len(second.tools)] == [1, 0]
    assert second.answer == "不客氣。"
    assert any("兩場。" in m.content for m in model.requests[-1])


def tools_case(case_id, expect, user_input="問題", history=(), chatbot_name=None):
    check = {"kind": "tools", "expect": expect}
    return Case(
        case_id, "test", "dev", user_input, "", None, check, None, list(history), chatbot_name
    )


def retrieval_case(case_id):
    check = {"kind": "retrieval", "gold": {"file": "retrieval.yaml"}}
    return Case(case_id, "test", "dev", "誰是碩士畢業生？", "", None, check, None)


async def evaluate(knowledge, script, *cases, gold_dir=None):
    model = ScriptedChatModel(script=script)
    tools = build_tools(knowledge.db, knowledge.embedder)
    agent = build_agent(model, tools, InMemorySaver())
    dataset = Dataset("test-v1", "2026-10-04T10:00:00+08:00", "Asia/Taipei", list(cases), "x")
    async with knowledge.db.begin() as conn:
        corpus = await load_corpus(conn, knowledge.version_id)
    options = {"gold_dir": gold_dir} if gold_dir else {}
    return model, await run_agent_checks(agent, tools, [dataset], corpus, **options)


async def passage(knowledge, header_part):
    """A chunk's own text: searching for it ranks that chunk first."""
    async with knowledge.db.begin() as conn:
        return await conn.scalar(
            select(Chunk.embedding_input)
            .join(IndexVersionChunk, IndexVersionChunk.chunk_id == Chunk.chunk_id)
            .where(
                IndexVersionChunk.index_version_id == knowledge.version_id,
                Chunk.context_header.contains(header_part),
            )
        )


def list_case(case_id):
    gold = {"file": "students.yaml", "as": "student", "degree": "master", "years": [2022]}
    check = {"kind": "list", "records": "student", "gold": gold}
    return Case(case_id, "test", "dev", "列出碩士畢業生", "", None, check, None)


@pytest.fixture
def gold_dir(tmp_path):
    (tmp_path / "students.yaml").write_text(
        "2022:\n  - name: '王小明'\n  - name: '李大華'\n", encoding="utf-8"
    )
    (tmp_path / "retrieval.yaml").write_text(
        "R1:\n  relevant:\n    - student: '王小明'\nR2:\n  relevant: []\n", encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize(
    ("called", "expect", "passed"),
    [
        ([], [[]], True),
        (["search_knowledge"], [[]], False),
        (["query_records"], [["search_knowledge"], ["query_records"]], True),
        (["search_knowledge", "query_records"], [["query_records", "search_knowledge"]], True),
        (["search_knowledge"], [["query_records", "search_knowledge"]], False),
    ],
)
def test_the_called_set_must_be_one_of_the_expected(called, expect, passed):
    assert tools_passed(called, expect) is passed


def test_a_refused_call_and_its_correction_are_one_choice():
    record = TurnRecord(
        "",
        [ToolUse("query_records", {}, "error"), ToolUse("query_records", {}, "found")],
        [],
        3,
        0,
        0,
        1.0,
    )

    assert called_tools(record) == ["query_records"]


async def test_cases_are_scored_and_unlabelled_ones_stay_pending(knowledge):
    script = [calls("query_records", LECTURES), reply("兩場。"), reply("你好！")]

    _, results = await evaluate(
        knowledge,
        script,
        tools_case("T1", [["query_records"]]),
        tools_case("T2", [["search_knowledge"]]),
        tools_case("T3", None),
    )

    assert [(r.case_id, r.status, r.passed) for r in results] == [
        ("T1", "scored", True),
        ("T2", "scored", False),
        ("T3", "pending", None),
    ]
    assert agent_summary(results)["tools dev"]["accuracy"] == 0.5


async def test_history_tools_run_for_real_before_the_turn(knowledge):
    history = [
        {
            "user": "列出演講",
            "tools": [{"name": "query_records", "args": LECTURES}],
            "answer": "兩場。",
        }
    ]

    model, (result,) = await evaluate(
        knowledge, [reply("第一場。")], tools_case("T1", [[]], "第一場是哪場？", history)
    )

    (scripted,) = [m for m in model.requests[0] if isinstance(m, ToolMessage)]
    assert json.loads(scripted.content)["count"] == 2
    assert result.passed and result.record.model_calls == 1


async def test_a_case_that_breaks_is_an_error_and_the_run_goes_on(knowledge):
    _, results = await evaluate(
        knowledge, [reply("好。")], tools_case("T1", [[]]), tools_case("T2", [[]])
    )

    assert [r.status for r in results] == ["scored", "error"]
    assert "more often than scripted" in results[1].detail


async def test_the_name_reaches_the_prompt(knowledge):
    model, _ = await evaluate(
        knowledge, [reply("好。")], tools_case("T1", [[]], chatbot_name="小幫手")
    )

    assert '"chatbot_name": "小幫手"' in model.requests[0][0].content


def test_the_report_shows_what_was_called_and_unsent():
    written = '{"queries": ["GRC"]}'
    record = record_turn(
        [AIMessage(phases(("commentary", written), ("final_answer", "沒有。")))], 1.0
    )
    result = AgentCaseResult("test-v1", "T1", "dev", "test", "問題", "tools", "scored")
    result.expect, result.passed, result.record = [["search_knowledge"]], False, record

    markdown = agent_markdown([result], {"model": "scripted"})

    assert "**fail**" in markdown and written in markdown
    assert agent_summary([result])["unsent_calls"] == 1


async def test_the_first_search_is_scored_alone_and_the_turn_as_read(knowledge, gold_dir):
    masters = await passage(knowledge, "碩士畢業生")
    products = await passage(knowledge, "產品目錄")
    script = [
        calls("search_knowledge", {"queries": [products]}, "call-1"),
        calls("search_knowledge", {"queries": [masters]}, "call-2"),
        reply("王小明。"),
    ]

    _, (result,) = await evaluate(knowledge, script, retrieval_case("R1"), gold_dir=gold_dir)

    assert (result.status, result.searches) == ("scored", 2)
    assert result.first_search.recall == 0
    assert result.turn_searches.recall == 1
    assert SEARCHED_CHUNKS < result.turn_searches.k <= 2 * SEARCHED_CHUNKS  # what was read


async def test_a_turn_without_a_search_finds_nothing(knowledge, gold_dir):
    _, (result,) = await evaluate(
        knowledge, [reply("不知道。")], retrieval_case("R1"), gold_dir=gold_dir
    )

    assert result.searches == 0
    assert (result.first_search.recall, result.turn_searches.recall) == (0, 0)
    assert agent_summary([result])["retrieval dev"]["no_search"] == 1


async def test_an_unlabelled_retrieval_case_is_not_played(knowledge, gold_dir):
    model, (result,) = await evaluate(knowledge, [], retrieval_case("R2"), gold_dir=gold_dir)

    assert (result.status, result.detail) == ("pending", "not labelled yet")
    assert model.requests == []


async def test_a_list_is_scored_by_the_records_the_reply_showed(knowledge, gold_dir):
    masters = {"record_type": "student", "degree": "master"}
    result_id = result_handle(knowledge.version_id, RecordQuery("student", degree="master"))
    script = [
        calls("query_records", masters),
        reply("共一位：", lists=[{"result_id": result_id, "item_ids": None}]),
    ]

    _, (result,) = await evaluate(knowledge, script, list_case("L1"), gold_dir=gold_dir)

    # 王小明 is shown; 李大華 is labelled but the version has no such record.
    assert (result.set.hits, result.set.expected, result.set.actual) == (1, 2, 1)
    assert result.set.precision == 1 and result.set.recall == 0.5
    assert agent_summary([result])["list dev"]["complete"] == 0


async def test_a_list_the_reply_did_not_attach_shows_nothing(knowledge, gold_dir):
    script = [calls("query_records", {"record_type": "student"}), reply("有兩位。")]

    _, (result,) = await evaluate(knowledge, script, list_case("L1"), gold_dir=gold_dir)

    assert (result.set.actual, result.set.recall) == (0, 0)
