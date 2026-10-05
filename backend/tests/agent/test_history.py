from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.checkpoint.memory import InMemorySaver

from tests.agent.fakes import ScriptedChatModel, answers, calls
from tests.agent.test_checkpoints import saved_thread
from wobot.agent.answers import Answer
from wobot.agent.build import build_agent
from wobot.agent.checkpoints import checkpoint_serde
from wobot.agent.history import CLEARED, ClearEarlierResults
from wobot.agent.tools import TurnContext, build_tools, result_handle
from wobot.knowledge.lists import RecordQuery

LECTURES = {"record_type": "lecture"}


def turn(number):
    """One turn that called a tool: the question, the call, its result and the answer."""
    call_id = f"call-{number}"
    return [
        HumanMessage(f"問題 {number}"),
        AIMessage("", tool_calls=[{"name": "query_records", "args": LECTURES, "id": call_id}]),
        ToolMessage(f"結果 {number}", tool_call_id=call_id),
        AIMessage(f"回答 {number}"),
    ]


def test_results_before_the_last_turn_are_cleared_and_calls_kept():
    messages = [*turn(1), *turn(2), *turn(3)]

    ClearEarlierResults().apply(messages, count_tokens=count_tokens_approximately)

    results = [m.content for m in messages if isinstance(m, ToolMessage)]
    assert results == [CLEARED, "結果 2", "結果 3"]
    assert messages[1].tool_calls[0]["args"] == LECTURES  # what was looked up stays


def test_a_short_conversation_is_left_alone():
    messages = [*turn(1), *turn(2)]

    ClearEarlierResults().apply(messages, count_tokens=count_tokens_approximately)

    assert [m.content for m in messages if isinstance(m, ToolMessage)] == ["結果 1", "結果 2"]


async def converse(knowledge, script, questions):
    model = ScriptedChatModel(script=script)
    tools = build_tools(knowledge.db, knowledge.embedder)
    agent = build_agent(model, tools, InMemorySaver(serde=checkpoint_serde()))
    config = {"configurable": {"thread_id": "t-1"}}
    states = []
    for question in questions:
        context = TurnContext("account-1", datetime(2026, 10, 3, tzinfo=UTC), knowledge.version_id)
        states.append(
            await agent.ainvoke({"messages": [HumanMessage(question)]}, config, context=context)
        )
    return model, states


async def test_the_model_is_sent_only_recent_results_while_the_thread_keeps_all(knowledge):
    script = []
    for number in (1, 2, 3):
        script += [calls("query_records", LECTURES, f"call-{number}"), answers(f"回答 {number}")]

    model, states = await converse(knowledge, script, ["一", "二", "三"])

    last_request = model.requests[-1]
    sent = [m.content for m in last_request if isinstance(m, ToolMessage)]
    assert sent[0] == CLEARED and CLEARED not in sent[1:]
    kept = [m.content for m in states[-1]["messages"] if isinstance(m, ToolMessage)]
    assert CLEARED not in kept


async def test_each_turn_counts_its_own_second_tries(knowledge):
    script = [AIMessage("not json"), answers("好。"), answers("不客氣。")]

    _, (first, second) = await converse(knowledge, script, ["你好", "謝謝"])

    assert [held["reason"] for held in first["retries"]] == ["unparsed"]
    assert second["retries"] == []


async def test_a_stored_turn_holds_only_what_the_strict_serializer_revives(knowledge, caplog):
    result_id = result_handle(knowledge.version_id, RecordQuery("lecture"))
    script = [
        calls("query_records", {"record_type": "lecture"}),
        answers("兩場：", [{"result_id": result_id, "item_ids": None}], grounding="grounded"),
        answers("不客氣。"),
    ]
    tools = build_tools(knowledge.db, knowledge.embedder)

    async with saved_thread() as (saver, config):
        agent = build_agent(ScriptedChatModel(script=script), tools, saver)
        for question in ("列出演講", "謝謝"):
            context = TurnContext(
                "account-1", datetime(2026, 10, 3, tzinfo=UTC), knowledge.version_id
            )
            await agent.ainvoke({"messages": [HumanMessage(question)]}, config, context=context)
        stored = await agent.aget_state(config)

    messages = stored.values["messages"]
    assert [type(m).__name__ for m in messages] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "HumanMessage",
        "AIMessage",
    ]
    assert messages[2].artifact is None  # the list stayed with its turn
    assert isinstance(stored.values["structured_response"], Answer)
    assert "Blocked deserialization" not in caplog.text
