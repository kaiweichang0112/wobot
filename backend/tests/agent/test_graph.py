import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from tests.agent.fakes import calls, fake_models
from tests.agent.test_list_agent import Unreachable
from wobot.agent.graph import (
    FAILURE_TEXT,
    MAX_LIST_ROUNDS,
    MAX_REC_ROUNDS,
    after_rec_tools,
    build_graph,
    list_should_continue,
    ok_or_failed,
    rec_should_continue,
    route_by_intent,
)
from wobot.agent.list_agent import result_handle
from wobot.agent.retrieval import record_handle
from wobot.agent.rewrite import SearchPlan
from wobot.agent.write_list import ListIntro, ShownList
from wobot.knowledge.lists import RecordQuery, find_records

EXPECTED_GRAPH = Path(__file__).with_name("expected_graph.mmd")


def graph(route="chat", *replies):
    """A graph for paths that look nothing up."""
    return build_graph(fake_models(route, chat=replies or ("你好！",)), db=None, embedder=None)


def turn(text: str, version: int = 1) -> dict:
    """A turn's input, as the CLI and the API will give it."""
    return {
        "messages": [HumanMessage(text)],
        "index_version": version,
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
    assert route_by_intent({"route": route, "status": "ok"}) == route
    assert route_by_intent({"route": route, "status": "failed"}) == "failed"


@pytest.mark.parametrize(("status", "label"), [("ok", "ok"), ("failed", "failed")])
def test_a_failed_lookup_leaves_the_path(status, label):
    assert ok_or_failed({"status": status}) == label


def test_the_list_loop_runs_tools_until_the_agent_stops_or_the_rounds_run_out():
    def given(*messages, rounds=0):
        return list_should_continue({"list_messages": list(messages), "tool_rounds": rounds})

    assert given(CALLS_A_TOOL) == "tools"
    assert given(CALLS_A_TOOL, rounds=MAX_LIST_ROUNDS) == "done"
    assert given(AIMessage("These will do.")) == "done"
    assert given(HumanMessage("all talks in 2024")) == "done"
    assert given() == "done"


def test_the_recommendation_loop_runs_tools_until_there_is_a_reply():
    calling = {"rec_messages": [CALLS_A_TOOL], "reply": {}, "status": "ok"}

    assert rec_should_continue(calling) == "tools"
    assert rec_should_continue(calling | {"reply": {"text": "請問在哪裡用？"}}) == "answered"
    assert rec_should_continue(calling | {"status": "failed"}) == "failed"
    assert after_rec_tools({"reply": {}, "status": "ok"}) == "next"
    assert after_rec_tools({"reply": {"text": "推薦：…"}, "status": "ok"}) == "recommended"
    assert after_rec_tools({"reply": {}, "status": "failed"}) == "failed"


@pytest.mark.parametrize(
    ("route", "path"),
    [
        ("chat", ["classify", "chat_reply"]),
        ("knowledge", ["classify", "rewrite_query", "retrieve", "answer"]),
        ("list", ["classify", "list_agent", "write_list"]),
        ("recommend", ["classify", "rec_agent"]),
    ],
)
async def test_the_route_picks_the_path(knowledge, route, path):
    # The paths not built yet are stubs, so each runs straight to its end.
    app = build_graph(fake_models(route), knowledge.db, knowledge.embedder)

    assert await nodes_run(app, turn("你好", knowledge.version_id)) == path


async def test_a_chat_turn_adds_the_reply_to_the_conversation():
    models = fake_models("chat", chat=["你好！今天想聊什麼？"])
    router = models.router
    app = build_graph(models, db=None, embedder=None)

    state = await app.ainvoke(turn("你好"))

    assert state["reply"] == {"text": "你好！今天想聊什麼？"}
    assert [m.text for m in state["messages"]] == ["你好", "你好！今天想聊什麼？"]
    assert router.calls[0] == ([HumanMessage("你好", id=state["messages"][0].id)], None)


async def test_a_turn_starts_without_the_last_turns_work():
    state = {**turn("你好"), "rec_messages": [AIMessage("old")], "status": "failed"}

    final = await graph("chat", "嗨").ainvoke(state)

    assert final["rec_messages"] == [] and final["status"] == "ok"


# --- The knowledge path -----------------------------------------------------------------


async def test_a_fact_is_answered_from_what_this_turn_found(knowledge):
    plan = SearchPlan(
        question_zh="王小明的論文是什麼？", question_en="What is 王小明's thesis?", name="王小明"
    )
    models = fake_models("knowledge", plan=plan, answers=["王小明的論文是智慧床墊。"])
    app = build_graph(models, knowledge.db, knowledge.embedder)

    state = await app.ainvoke(turn("王小明的論文是什麼？", knowledge.version_id))

    assert state["question"] == "王小明的論文是什麼？"
    assert knowledge.embedder.calls[-1] == ["王小明的論文是什麼？", "What is 王小明's thesis?"]
    assert state["evidence"]["passages"]
    assert [r["key"] for r in state["evidence"]["records"]] == ["student:master:王小明"]
    assert state["reply"] == {"text": "王小明的論文是智慧床墊。"}
    assert state["messages"][-1].text == "王小明的論文是智慧床墊。"


class Broken:
    """A model, or an embedder, whose provider cannot be reached."""

    config_id = "broken"

    def with_structured_output(self, schema, **kwargs):
        return RunnableLambda(self._fail)

    def bind_tools(self, tools, **kwargs):
        return RunnableLambda(self._fail)

    async def _fail(self, *args):
        raise TimeoutError("no answer")

    async def ainvoke(self, *args, **kwargs):
        raise TimeoutError("no answer")

    async def embed(self, texts):
        raise TimeoutError("no answer")


async def test_a_failed_search_is_reported_as_retryable(knowledge):
    app = build_graph(fake_models("knowledge"), knowledge.db, Broken())

    state = await app.ainvoke(turn("GRC 是哪一年成立的？", knowledge.version_id))

    assert await nodes_run(app, turn("GRC 是哪一年成立的？", knowledge.version_id)) == [
        "classify",
        "rewrite_query",
        "retrieve",
        "report_failure",
    ]
    assert state["reply"] == {"text": FAILURE_TEXT["zh"], "retryable": True}


async def test_a_failed_rewrite_searches_the_message_as_it_is(knowledge):
    models = replace(fake_models("knowledge"), rewrite=Broken())
    app = build_graph(models, knowledge.db, knowledge.embedder)

    state = await app.ainvoke(turn("WhizPad 是什麼？", knowledge.version_id))

    assert state["queries"] == ["WhizPad 是什麼？"]
    assert state["reply"] == {"text": "答案。"}


async def test_a_failed_answer_is_reported_as_retryable(knowledge):
    models = replace(fake_models("knowledge"), answer=Broken())
    app = build_graph(models, knowledge.db, knowledge.embedder)

    state = await app.ainvoke(turn("What is WhizPad?", knowledge.version_id))

    assert state["reply"] == {"text": FAILURE_TEXT["en"], "retryable": True}


# --- The list path ----------------------------------------------------------------------


async def test_a_list_is_found_by_the_tools_and_shown_in_full_by_code(knowledge):
    masters = result_handle(knowledge.version_id, RecordQuery("student", degree="master"))
    models = fake_models(
        "list",
        list_calls=[calls(("list_students", {"degree": "master"}))],
        intro=ListIntro(
            intro="共 1 位碩士畢業生。", lists=[ShownList(result_id=masters, item_ids=None)]
        ),
    )
    app = build_graph(models, knowledge.db, knowledge.embedder)

    state = await app.ainvoke(turn("列出所有碩士畢業生", knowledge.version_id))

    # The first round must call a tool; with every list found, no second is asked for.
    assert [choice for choice, _ in models.list_agent.prompts] == ["required"]
    assert state["tool_rounds"] == 1 and list(state["list_results"]) == [masters]
    assert state["reply"]["lists"][0]["keys"] == ["student:master:王小明"]
    assert state["reply"]["text"].startswith("共 1 位碩士畢業生。\n\n1. 2022　碩士　王小明")
    assert state["messages"][-1].text == state["reply"]["text"]


async def test_an_empty_list_lets_the_agent_try_other_filters(knowledge):
    models = fake_models(
        "list",
        list_calls=[
            calls(("list_students", {"contains": "王曉明"})),  # spelled another way
            calls(("list_students", {"contains": "王小明"})),
        ],
    )
    app = build_graph(models, knowledge.db, knowledge.embedder)

    state = await app.ainvoke(turn("王曉明是哪一年畢業的碩士？", knowledge.version_id))

    assert [choice for choice, _ in models.list_agent.prompts] == ["required", None]
    assert state["tool_rounds"] == 2
    assert [len(r["items"]) for r in state["list_results"].values()] == [0, 1]
    # The list found empty before the change is not the writer's to read.
    _, human = models.write_list.prompts[-1]
    assert [r["count"] for r in json.loads(human.content)["lists"]] == [1]


async def test_a_list_loop_stops_when_its_rounds_run_out(knowledge):
    nothing = ("list_students", {"year_from": 1999, "year_to": 1999})
    asking = [calls(nothing) for _ in range(MAX_LIST_ROUNDS)]
    models = fake_models("list", list_calls=asking)
    app = build_graph(models, knowledge.db, knowledge.embedder)

    path = await nodes_run(app, turn("列出所有產品", knowledge.version_id))

    assert path == [
        "classify",
        *["list_agent", "list_tools"] * MAX_LIST_ROUNDS,
        "list_agent",
        "write_list",
    ]
    assert len(models.list_agent.prompts) == MAX_LIST_ROUNDS  # the last turn asks no model


async def test_a_failed_list_query_is_reported_as_retryable():
    # Two turns are played: each calls a tool once.
    models = fake_models("list", list_calls=[calls(("list_products", {}))] * 2)
    app = build_graph(models, Unreachable(), embedder=None)

    state = await app.ainvoke(turn("列出所有產品"))

    assert await nodes_run(app, turn("列出所有產品")) == [
        "classify",
        "list_agent",
        "list_tools",
        "report_failure",
    ]
    assert state["reply"] == {"text": FAILURE_TEXT["zh"], "retryable": True}


@pytest.mark.parametrize("broken", ["list_agent", "write_list"])
async def test_a_failed_list_model_is_reported_as_retryable(broken):
    models = replace(fake_models("list"), **{broken: Broken()})
    app = build_graph(models, db=None, embedder=None)

    state = await app.ainvoke(turn("List every product."))

    assert state["reply"] == {"text": FAILURE_TEXT["en"], "retryable": True}


# --- The recommendation path ------------------------------------------------------------


SEARCH = calls(("search_products", {"functions": ["離床偵測"]}))


async def bed_product(knowledge) -> str:
    """The handle of the fixture's bed, which states bed-exit detection."""
    async with knowledge.db.begin() as conn:
        beds = await find_records(
            conn, knowledge.version_id, RecordQuery("product", category="1-3")
        )
    return next(
        record_handle(item.record_id)
        for item in beds.items
        if item.fields["product_name"] == "測試床墊 TM-1"
    )


def recommending(product_id: str, *statuses: str) -> AIMessage:
    """The agent recommending the product, judged on a function and a constraint."""
    checks = [{"need": n, "status": status} for n, status in enumerate(statuses, start=1)]
    return calls(
        (
            "recommend_products",
            {
                "needs": [
                    {"text": "離床偵測", "kind": "function"},
                    {"text": "居家使用", "kind": "constraint"},
                ],
                "products": [
                    {"product_id": product_id, "checks": checks, "reason": "床墊下感測。"}
                ],
                "count": 1,
                "intro": "這項產品符合你的需求。",
            },
        )
    )


async def played(app, state: dict) -> tuple[list[str], dict]:
    """The nodes a turn passed through, in order, and the state it ended in."""
    path, final = [], {}
    async for mode, chunk in app.astream(state, stream_mode=["updates", "values"]):
        if mode == "updates":
            path += list(chunk)
        else:
            final = chunk
    return path, final


async def test_the_agent_asks_what_the_user_needs_without_searching():
    models = fake_models("recommend")
    app = build_graph(models, db=None, embedder=None)

    path, state = await played(app, turn("推薦一個智慧照護產品給我"))

    assert path == ["classify", "rec_agent"]
    assert state["reply"] == {
        "text": "想解決哪方面的照顧需求？",
        "action": "replied",
        "products": [],
    }
    assert state["pending_question"] == "想解決哪方面的照顧需求？"
    assert state["messages"][-1].text == "想解決哪方面的照顧需求？"
    choice, prompt = models.rec_agent.prompts[0]
    assert choice is None  # not made to call a tool
    assert prompt[-1].text == "推薦一個智慧照護產品給我"


async def test_a_recommendation_the_guards_pass_ends_the_turn_as_a_card(knowledge):
    bed = await bed_product(knowledge)
    rounds = [SEARCH, recommending(bed, "supported", "unknown")]
    models = fake_models("recommend", rec_calls=rounds)
    app = build_graph(models, knowledge.db, knowledge.embedder)
    answering = {
        **turn("在家裡用，要能偵測離床", knowledge.version_id),
        "pending_question": "請問在哪裡用？",
    }

    path, state = await played(app, answering)

    assert path == ["classify", "rec_agent", "rec_tools", "rec_agent", "rec_tools"]
    assert state["reply"]["action"] == "recommend"
    assert [p["id"] for p in state["reply"]["products"]] == [bed]
    assert state["reply"]["text"].startswith("這項產品符合你的需求。\n\n推薦：測試床墊 TM-1")
    assert "- 居家使用：目錄未註明" in state["reply"]["text"]
    assert state["messages"][-1].text == state["reply"]["text"]
    assert state["pending_question"] is None
    # The agent read the products found before it recommended.
    _, second = models.rec_agent.prompts[1]
    assert "測試床墊 TM-1" in second[-1].content


async def test_a_refused_recommendation_goes_back_to_the_agent(knowledge):
    bed = await bed_product(knowledge)
    rounds = [
        SEARCH,
        recommending(bed, "unknown", "supported"),
        AIMessage("目錄沒有寫到離床偵測。"),
    ]
    models = fake_models("recommend", rec_calls=rounds)
    app = build_graph(models, knowledge.db, knowledge.embedder)

    path, state = await played(app, turn("要能偵測離床", knowledge.version_id))

    assert path == ["classify", "rec_agent", "rec_tools", "rec_agent", "rec_tools", "rec_agent"]
    refused = json.loads(state["rec_messages"][-1].content)["refused"]
    assert "function 1 (離床偵測) is unknown" in refused
    assert state["reply"]["text"] == "目錄沒有寫到離床偵測。"


async def test_code_names_what_was_found_when_the_rounds_run_out(knowledge):
    models = fake_models("recommend", rec_calls=[SEARCH] * MAX_REC_ROUNDS)
    app = build_graph(models, knowledge.db, knowledge.embedder)

    path, state = await played(app, turn("要能偵測離床", knowledge.version_id))

    assert path.count("rec_tools") == MAX_REC_ROUNDS and path[-1] == "rec_agent"
    assert len(models.rec_agent.prompts) == MAX_REC_ROUNDS  # the last turn asks no model
    assert state["reply"]["action"] == "unverified"
    assert "- 測試床墊 TM-1（範例科技股份有限公司）" in state["reply"]["text"]


async def test_a_failed_product_search_is_reported_as_retryable(knowledge):
    models = fake_models("recommend", rec_calls=[SEARCH])
    app = build_graph(models, Unreachable(), knowledge.embedder)

    path, state = await played(app, turn("想找偵測離床的"))

    assert path[-1] == "report_failure"
    assert state["reply"] == {"text": FAILURE_TEXT["zh"], "retryable": True}


async def test_a_failed_agent_is_reported_as_retryable():
    models = replace(fake_models("recommend"), rec_agent=Broken())
    app = build_graph(models, db=None, embedder=None)

    state = await app.ainvoke(turn("想找偵測離床的"))

    assert state["reply"] == {"text": FAILURE_TEXT["zh"], "retryable": True}


async def test_the_last_question_is_read_by_the_route_then_cleared():
    models = fake_models("chat")
    app = build_graph(models, db=None, embedder=None)

    state = await app.ainvoke({**turn("你好"), "pending_question": "請問在哪裡用？"})

    assert models.router.calls[0][1] == "請問在哪裡用？"
    assert state["pending_question"] is None


async def test_a_turn_no_router_answers_is_reported_as_retryable():
    models = replace(fake_models("chat"), router=FailingRouter())
    app = build_graph(models, db=None, embedder=None)
    asked = {**turn("下肢"), "pending_question": "你想做哪一類復健呢？"}

    path, state = await played(app, asked)

    assert path == ["classify", "report_failure"]
    assert state["reply"] == {"text": FAILURE_TEXT["zh"], "retryable": True}
    assert state["pending_question"] == "你想做哪一類復健呢？"  # for the message sent again


class FailingRouter:
    async def __call__(self, messages, pending_question):
        raise TimeoutError("no answer")
