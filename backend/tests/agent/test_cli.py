import json

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
    assert describe("classify", {"route": "chat", "route_fallback": True}) == (
        "route chat (fallback)"
    )
    assert describe("classify", {"status": "failed"}) == "failed: no router answered"
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


def test_the_recommendation_nodes_are_described_too():
    found_call = calls(
        (
            "find_products",
            {
                "functions": ["離床偵測"],
                "must": {"places": ["home"], "form": []},
                "must_not": {},
                "other": [],
                "count": None,
            },
        )
    )
    product = {"id": "r-1", "name": "WhizPad", "company": "世大智科", "needs": {}}
    split = {"facet": "places", "values": {"home": 3, "institution": 2}, "unstated": 0}
    asking = {"matched": 5, "products": [product], "decisive": [split], "gaps": []}
    nothing = {
        "matched": 0,
        "products": [],
        "decisive": [],
        "gaps": [{"need": "places=home", "products": 2}],
    }
    found = ToolMessage(json.dumps(asking), tool_call_id="call-1")
    none = ToolMessage(json.dumps(nothing), tool_call_id="call-1")
    shown = ToolMessage('{"shown": ["r-1"]}', tool_call_id="call-1")
    refused = ToolMessage(
        "Not done: r-9 not among the products found.", tool_call_id="c", status="error"
    )
    asked = {"text": "請問是在家裡用嗎？\n還是在機構？", "action": "replied", "products": []}
    card = {"action": "recommend", "products": [{"name": "WhizPad"}]}

    assert describe("rec_agent", {"rec_messages": [found_call]}) == (
        'find_products {"functions": ["離床偵測"], "must": {"places": ["home"]}}'
    )
    assert describe("rec_agent", {"reply": asked}) == "replied: 請問是在家裡用嗎？"
    assert describe("rec_agent", {"status": "failed"}) == "failed"
    assert describe("rec_tools", {"rec_messages": [found_call, found]}) == (
        "5 fit: WhizPad, decisive places {'home': 3, 'institution': 2}"
    )
    assert describe("rec_tools", {"rec_messages": [found_call, none]}) == "0 fit, gap places=home 2"
    assert describe("rec_tools", {"rec_messages": [found_call, refused]}) == refused.text
    assert describe("rec_tools", {"rec_messages": [found_call, shown], "reply": card}) == (
        "shown r-1; recommend: WhizPad"
    )
