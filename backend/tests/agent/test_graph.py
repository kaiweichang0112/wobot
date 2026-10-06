from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.agent.fakes import FakeRouter, fake_chat
from wobot.agent.graph import (
    MAX_LIST_ROUNDS,
    MAX_REC_ROUNDS,
    build_graph,
    list_should_continue,
    ok_or_failed,
    pass_or_retry,
    rec_should_continue,
    route_by_intent,
    search_or_ask,
)

EXPECTED_GRAPH = Path(__file__).with_name("expected_graph.mmd")


def graph(route="chat", *replies):
    return build_graph(FakeRouter(route), fake_chat(*replies))


def turn(text: str) -> dict:
    """A turn's input, as the CLI and the API will give it."""
    return {
        "messages": [HumanMessage(text)],
        "index_version": 1,
        "query_time": datetime(2026, 10, 6, 10, tzinfo=ZoneInfo("Asia/Taipei")),
        "chatbot_name": "Wobot",
    }


async def nodes_run(app, state: dict) -> list[str]:
    """The nodes a turn passes through, in order: each update names the node it came from."""
    return [node async for update in app.astream(state, stream_mode="updates") for node in update]


CALLS_A_TOOL = AIMessage("", tool_calls=[{"name": "list_lectures", "args": {}, "id": "c1"}])


def test_the_graph_is_drawn_as_planned():
    # The plan's drawing is the contract: a change to a node or an edge shows here first.
    drawn = graph().get_graph().draw_mermaid()

    assert drawn == EXPECTED_GRAPH.read_text()


def test_every_conditional_edge_is_labelled():
    # A label named like its target is left out of the drawing.
    edges = graph().get_graph().edges

    unlabelled = [(e.source, e.target) for e in edges if e.conditional and not e.data]
    assert unlabelled == []


@pytest.mark.parametrize("route", ["chat", "knowledge", "list", "recommend"])
def test_the_route_names_the_path(route):
    assert route_by_intent({"route": route}) == route


@pytest.mark.parametrize(("status", "label"), [("ok", "ok"), ("failed", "failed")])
def test_a_failed_lookup_leaves_the_path(status, label):
    assert ok_or_failed({"status": status}) == label


@pytest.mark.parametrize(
    ("requirements", "label"),
    [
        (None, "ask"),
        ({"goal": "", "must_have": []}, "ask"),  # "recommend a product", no more
        ({"goal": "caring for a bedridden parent", "must_have": []}, "ask"),
        ({"goal": "caring for a bedridden parent", "must_have": [{"id": "n1"}]}, "search"),
    ],
)
def test_a_search_waits_for_a_goal_and_a_need(requirements, label):
    assert search_or_ask({"requirements": requirements}) == label


@pytest.mark.parametrize(
    ("should_continue", "key", "limit"),
    [
        (list_should_continue, "list_messages", MAX_LIST_ROUNDS),
        (rec_should_continue, "rec_messages", MAX_REC_ROUNDS),
    ],
)
def test_a_loop_runs_tools_until_the_agent_stops_or_the_rounds_run_out(should_continue, key, limit):
    assert should_continue({key: [CALLS_A_TOOL], "tool_rounds": 0}) == "tools"
    assert should_continue({key: [CALLS_A_TOOL], "tool_rounds": limit}) == "done"
    assert should_continue({key: [AIMessage("These will do.")], "tool_rounds": 0}) == "done"
    assert should_continue({key: [HumanMessage("all talks in 2024")], "tool_rounds": 0}) == "done"
    assert should_continue({key: [], "tool_rounds": 0}) == "done"


@pytest.mark.parametrize(
    ("route", "path"),
    [
        ("chat", ["classify", "chat_reply"]),
        ("knowledge", ["classify", "rewrite_query", "retrieve", "answer"]),
        ("list", ["classify", "list_agent", "write_list"]),
        ("recommend", ["classify", "update_needs", "decide", "check"]),
    ],
)
async def test_the_route_picks_the_path(route, path):
    # The paths not built yet are stubs, so each runs straight to its end.
    assert await nodes_run(graph(route, "你好！"), turn("你好")) == path


async def test_a_chat_turn_adds_the_reply_to_the_conversation():
    router = FakeRouter("chat")
    app = build_graph(router, fake_chat("你好！今天想聊什麼？"))

    state = await app.ainvoke(turn("你好"))

    assert state["reply"] == {"text": "你好！今天想聊什麼？"}
    assert [m.text for m in state["messages"]] == ["你好", "你好！今天想聊什麼？"]
    assert router.calls[0] == ([HumanMessage("你好", id=state["messages"][0].id)], None)


async def test_a_turn_starts_without_the_last_turns_work():
    state = {**turn("你好"), "rec_messages": [AIMessage("old")], "status": "failed"}

    final = await graph("chat", "嗨").ainvoke(state)

    assert final["rec_messages"] == [] and final["status"] == "ok"


def test_a_decision_with_problems_goes_back_to_the_loop():
    assert pass_or_retry({"problems": ["n2 is unknown for p-1"]}) == "retry"
    assert pass_or_retry({"problems": []}) == "pass"
