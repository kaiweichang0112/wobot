import json
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from tests.agent.fakes import ScriptedChatModel, calls
from wobot.agent.build import build_agent
from wobot.agent.tools import TurnContext, build_tools
from wobot.eval.agent import (
    ToolUse,
    TurnRecord,
    called_tools,
    play,
    run_tool_selection,
    tools_passed,
)
from wobot.eval.dataset import Case, Dataset
from wobot.eval.report import agent_markdown, agent_summary

LECTURES = {"record_type": "lecture"}


def reply(text, input_tokens=100, output_tokens=10, **kwargs):
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    return AIMessage(text, usage_metadata=usage, **kwargs)


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


async def test_a_call_written_as_text_is_unsent_and_hidden(knowledge):
    written = '{"query": "GRC 成立"}'
    script = [reply(phases(("commentary", written), ("final_answer", "來源中沒有查到。")))]

    _, (record,) = await run(knowledge, script, "GRC 是哪一年成立的？")

    assert record.tools == []
    assert record.unsent_calls == [written]
    assert record.answer == "來源中沒有查到。"


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
    assert "兩場。" in [m.content for m in model.requests[-1]]


def tools_case(case_id, expect, user_input="問題", history=(), chatbot_name=None):
    check = {"kind": "tools", "expect": expect}
    return Case(
        case_id, "test", "dev", user_input, "", None, check, None, list(history), chatbot_name
    )


async def select(knowledge, script, *cases):
    model = ScriptedChatModel(script=script)
    tools = build_tools(knowledge.db, knowledge.embedder)
    agent = build_agent(model, tools, InMemorySaver())
    dataset = Dataset("test-v1", "2026-10-04T10:00:00+08:00", "Asia/Taipei", list(cases), "x")
    return model, await run_tool_selection(agent, tools, [dataset], knowledge.version_id)


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

    _, results = await select(
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
    assert agent_summary(results)["accuracy dev"] == 0.5


async def test_history_tools_run_for_real_before_the_turn(knowledge):
    history = [
        {
            "user": "列出演講",
            "tools": [{"name": "query_records", "args": LECTURES}],
            "answer": "兩場。",
        }
    ]

    model, (result,) = await select(
        knowledge, [reply("第一場。")], tools_case("T1", [[]], "第一場是哪場？", history)
    )

    (scripted,) = [m for m in model.requests[0] if isinstance(m, ToolMessage)]
    assert json.loads(scripted.content)["count"] == 2
    assert result.passed and result.record.model_calls == 1


async def test_a_case_that_breaks_is_an_error_and_the_run_goes_on(knowledge):
    _, results = await select(
        knowledge, [reply("好。")], tools_case("T1", [[]]), tools_case("T2", [[]])
    )

    assert [r.status for r in results] == ["scored", "error"]
    assert "more often than scripted" in results[1].detail


async def test_the_name_reaches_the_prompt(knowledge):
    model, _ = await select(
        knowledge, [reply("好。")], tools_case("T1", [[]], chatbot_name="小幫手")
    )

    assert '"chatbot_name": "小幫手"' in model.requests[0][0].content


async def test_the_report_shows_what_was_called_and_unsent(knowledge):
    written = '{"query": "GRC"}'
    script = [reply(phases(("commentary", written), ("final_answer", "沒有。")))]

    _, results = await select(knowledge, script, tools_case("T1", [["search_knowledge"]]))

    markdown = agent_markdown(results, {"model": "scripted"})
    assert "**fail**" in markdown and written in markdown
    assert agent_summary(results)["unsent_calls"] == 1
