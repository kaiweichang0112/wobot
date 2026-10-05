import uuid
from datetime import UTC, date, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from tests.agent.fakes import ScriptedChatModel, answers, calls
from wobot.agent.answers import Answer, ListRef
from wobot.agent.build import MODEL_CALLS, TOOL_CALLS, build_agent
from wobot.agent.guard import UNPARSED_NOTE, problems, this_turn, turn_evidence
from wobot.agent.render import ReplyStatus, turn_reply
from wobot.agent.tools import (
    DetailsResult,
    Evidence,
    RecordsResult,
    SearchResult,
    ToolStatus,
    TurnContext,
    build_tools,
    chunk_handle,
    record_handle,
)
from wobot.knowledge.lists import Listed, RecordQuery
from wobot.knowledge.search import Member, SearchHit

PAGE = "https://www.grc.yzu.edu.tw/speeches"
CATALOG = "catalog.xlsx"


def listed(source_url=PAGE):
    fields = {"lecture_date": date(2024, 5, 10)}
    return Listed(uuid.uuid4(), f"lecture:{uuid.uuid4()}", fields, source_url, {})


def returned(artifact, status="success"):
    return ToolMessage("{}", tool_call_id="call-1", artifact=artifact, status=status)


def records(*items, uncertain=()):
    query = RecordQuery("lecture")
    return RecordsResult(ToolStatus.FOUND, "q-1", query, list(items), list(uncertain))


def passage(*members):
    hit = SearchHit(uuid.uuid4(), 0.1, "GRC", "text", [], 10)
    return Evidence(hit, list(members))


def answer(grounding="grounded", citations=(), lists=()):
    return Answer(
        answer="好。",
        grounding=grounding,
        citations=list(citations),
        lists=list(lists),
        recommendation=None,
    )


def test_the_turn_starts_after_its_own_question():
    messages = [HumanMessage("一"), AIMessage("好。"), HumanMessage("二"), AIMessage("嗯。")]

    assert this_turn(messages) == [messages[-1]]


def test_every_id_the_model_was_given_is_held_with_its_source():
    talk, product = listed(), listed(CATALOG)
    found = passage(Member(product.record_id, "product", "product:x", CATALOG))

    evidence = turn_evidence(
        [
            returned(records(talk)),
            returned(SearchResult(ToolStatus.FOUND, ["床墊"], [found])),
            returned(DetailsResult(ToolStatus.FOUND, [product])),
        ]
    )

    assert evidence.sources == {
        record_handle(talk.record_id): [PAGE],
        "q-1": [PAGE],
        chunk_handle(found.hit.chunk_id): [CATALOG],
        record_handle(product.record_id): [CATALOG],
    }
    assert list(evidence.results) == ["q-1"]
    assert (evidence.looked_up, evidence.failed) == (True, False)


def test_a_refused_call_holds_nothing_and_a_failed_one_marks_the_turn():
    refused = ToolMessage("bad", tool_call_id="call-1", status="error")

    evidence = turn_evidence([refused, returned(SearchResult(ToolStatus.FAILED, ["GRC"]))])

    assert (evidence.sources, evidence.looked_up, evidence.failed) == ({}, False, True)


def test_no_match_is_a_lookup_that_holds_no_ids():
    evidence = turn_evidence([returned(SearchResult(ToolStatus.NO_MATCH, ["GRC"]))])

    assert problems(answer("no_info"), evidence) == []
    assert problems(answer("grounded"), evidence) != []


@pytest.mark.parametrize("grounding", ["grounded", "no_info"])
def test_claiming_sources_without_a_lookup_is_held_back(grounding):
    (found,) = problems(answer(grounding), turn_evidence([]))

    assert "no tool returned anything" in found


def test_general_needs_no_lookup():
    assert problems(answer("general"), turn_evidence([])) == []


def test_only_ids_this_turn_returned_may_be_cited():
    talk = listed()
    evidence = turn_evidence([returned(records(talk))])
    held = record_handle(talk.record_id)

    assert problems(answer(citations=[held]), evidence) == []
    # A count rests on the whole result: its ID is cited as such.
    assert problems(answer(citations=["q-1"]), evidence) == []
    (found,) = problems(answer(citations=[held, "k-00000000", "q-9"]), evidence)
    assert "['k-00000000', 'q-9']" in found


def test_lists_name_only_this_turns_results_and_their_items():
    talk = listed()
    evidence = turn_evidence([returned(records(talk))])
    held = record_handle(talk.record_id)

    whole = ListRef(result_id="q-1", item_ids=None)
    picked = ListRef(result_id="q-1", item_ids=[held, "r-00000000"])
    other = ListRef(result_id="q-9", item_ids=None)

    assert problems(answer(lists=[whole]), evidence) == []
    assert problems(answer(lists=[picked, other]), evidence) == [
        "item_ids ['r-00000000'] are not items of q-1",
        "result_id q-9 is not one query_records returned this turn",
    ]


def test_a_grounded_answer_cites_something():
    evidence = turn_evidence([returned(records(listed()))])

    (found,) = problems(answer("grounded"), evidence)

    assert "nothing is cited" in found


LECTURES = {"record_type": "lecture"}


async def play(knowledge, script):
    model = ScriptedChatModel(script=script)
    agent = build_agent(model, build_tools(knowledge.db, knowledge.embedder))
    context = TurnContext("account-1", datetime(2026, 10, 3, tzinfo=UTC), knowledge.version_id)
    state = await agent.ainvoke({"messages": [HumanMessage("列出演講")]}, context=context)
    reply = turn_reply(
        this_turn(state["messages"]), state["structured_response"], context.artifacts
    )
    return model, state, reply


async def test_a_reply_that_does_not_parse_is_asked_again(knowledge):
    written = AIMessage('{"queries": ["GRC"]}')

    model, state, reply = await play(knowledge, [written, answers("你好！")])

    assert reply.status is ReplyStatus.ANSWERED
    # The second try is told why, after seeing what it wrote; neither is kept.
    *_, shown, note = model.requests[1]
    assert (shown, note) == (written, SystemMessage(UNPARSED_NOTE))
    assert [type(m) for m in state["messages"]] == [HumanMessage, AIMessage]
    assert [held["reason"] for held in state["retries"]] == ["unparsed"]


async def test_a_reply_that_never_parses_ends_the_turn_retryable(knowledge):
    model, state, reply = await play(knowledge, [AIMessage("嗯"), AIMessage("嗯")])

    assert reply.status is ReplyStatus.RETRYABLE
    assert len(model.requests) == 2
    assert [held["reason"] for held in state["retries"]] == ["unparsed", "unparsed"]


async def test_a_forged_citation_is_named_and_corrected(knowledge):
    script = [
        calls("query_records", LECTURES),
        answers("兩場。", grounding="grounded", citations=["r-00000000"]),
        answers("沒有找到相符的資料。", grounding="no_info"),
    ]

    model, state, reply = await play(knowledge, script)

    assert (reply.status, reply.grounding) == (ReplyStatus.ANSWERED, "no_info")
    assert "r-00000000" in model.requests[2][-1].content
    (held,) = state["retries"]
    assert held["reason"] == "problems" and "r-00000000" in held["detail"][0]


async def test_an_answer_claiming_sources_unread_is_sent_to_look(knowledge):
    script = [
        answers("GRC 成立於 2003 年。", grounding="grounded", citations=["k-12345678"]),
        calls("query_records", LECTURES),
        answers("兩場。", grounding="general"),
    ]

    model, _, reply = await play(knowledge, script)

    # The second try looked it up, and its answer was checked like any other.
    assert reply.status is ReplyStatus.ANSWERED
    assert len(model.requests) == 3


async def test_a_turn_stuck_calling_tools_ends_at_the_limit(knowledge):
    script = [calls("query_records", LECTURES, f"call-{n}") for n in range(MODEL_CALLS + 1)]

    model, _, reply = await play(knowledge, script)

    assert len(model.requests) == MODEL_CALLS
    assert reply.status is ReplyStatus.RETRYABLE


async def test_calls_past_the_tool_limit_are_refused_and_the_model_answers(knowledge):
    # Arguments refused before any query, so the parallel calls share no connection.
    refused = {"record_type": "lecture", "last_years": 0}
    many = AIMessage(
        "",
        tool_calls=[
            {"name": "query_records", "args": refused, "id": f"call-{n}"}
            for n in range(TOOL_CALLS + 2)
        ],
    )

    _, state, reply = await play(knowledge, [many, answers("好。")])

    results = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(results) == TOOL_CALLS + 2
    assert sum("limit exceeded" in m.content for m in results) == 2
    assert reply.status is ReplyStatus.ANSWERED
