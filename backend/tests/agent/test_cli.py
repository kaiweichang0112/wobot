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
    searched = calls(("search_products", {"functions": ["離床預警"]}))
    recommended = calls(
        (
            "recommend_products",
            {
                "needs": [
                    {"text": "離床預警", "kind": "function"},
                    {"text": "居家使用", "kind": "constraint"},
                ],
                "products": [
                    {
                        "product_id": "r-1",
                        "checks": [
                            {"need": 1, "status": "supported"},
                            {"need": 2, "status": "unknown"},
                        ],
                    }
                ],
                "count": 1,
            },
        )
    )
    product = {"id": "r-1", "category": "產品目錄", "text": "產品名稱：WhizPad\n公司：世大智科"}
    found = ToolMessage(json.dumps({"products": [product]}), tool_call_id="call-1")
    shown = ToolMessage('{"shown": ["r-1"]}', tool_call_id="call-2")
    refused = ToolMessage('{"refused": "Not shown: r-1: function 1 is unknown."}', tool_call_id="c")
    asked = {"text": "請問是在家裡用嗎？\n還是在機構？", "action": "replied", "products": []}
    card = {"action": "recommend", "products": [{"name": "WhizPad"}]}

    assert describe("rec_agent", {"rec_messages": [searched]}) == (
        'search_products {"functions": ["離床預警"]}'
    )
    assert describe("rec_agent", {"rec_messages": [recommended]}) == (
        "recommend_products needs [1 離床預警 (f), 2 居家使用 (c)] count 1: "
        "r-1 1=supported 2=unknown"
    )
    assert describe("rec_agent", {"reply": asked}) == "replied: 請問是在家裡用嗎？"
    assert describe("rec_agent", {"status": "failed"}) == "failed"
    assert describe("rec_tools", {"rec_messages": [searched, found]}) == "1 products: WhizPad"
    assert describe("rec_tools", {"rec_messages": [recommended, refused]}) == (
        "Not shown: r-1: function 1 is unknown."
    )
    assert describe("rec_tools", {"rec_messages": [recommended, shown], "reply": card}) == (
        "shown r-1; recommend: WhizPad"
    )
