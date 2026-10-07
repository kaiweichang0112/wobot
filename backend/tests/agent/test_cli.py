from langchain_core.messages import AIMessage, ToolMessage

from tests.agent.fakes import calls, fake_models
from tests.agent.test_graph import turn
from wobot.agent.cli import describe, play_turn
from wobot.agent.graph import build_graph


def test_each_node_is_described_by_what_it_found():
    asked = calls(("list_students", {"degree": "master", "contains": None}))
    found = ToolMessage('{"list": "list_students", "count": 3}', tool_call_id="call-1")
    refused = ToolMessage("Not run: degree: bad", tool_call_id="call-2", status="error")

    assert describe("classify", {"route": "list", "route_confidence": 0.91}) == "route list (0.91)"
    assert describe("rewrite_query", {"question": "張維益的論文？", "name": "張維益"}) == (
        "張維益的論文？, name 張維益"
    )
    assert describe("retrieve", {"evidence": {"passages": [1, 2], "records": []}}) == (
        "2 passages, 0 records"
    )
    assert describe("list_agent", {"list_messages": [asked]}) == (
        'list_students {"degree": "master"}'
    )
    assert describe("list_agent", {"list_messages": [AIMessage("done")]}) == "done"
    assert describe("list_agent", {}) == "no model call: the lists will do"
    assert describe("list_tools", {"list_messages": [asked, found, refused]}) == (
        "list_students 3; Not run: degree: bad"
    )
    assert describe("retrieve", {"status": "failed"}) == "failed"
    assert describe("chat_reply", {"reply": {"text": "嗨"}}) == ""


async def test_a_turn_prints_its_path_then_the_reply(capsys):
    app = build_graph(fake_models("chat", chat=["你好！"]), db=None, embedder=None)

    await play_turn(app, turn("你好"), {})

    printed = capsys.readouterr().out
    assert "· classify" in printed and "route chat" in printed and "· chat_reply" in printed
    assert printed.endswith("\n你好！\n")
