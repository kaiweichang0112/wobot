import hashlib
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from wobot.agent import list_agent
from wobot.agent.list_agent import (
    LIST_TOOLS,
    SHOWN_ITEMS,
    TOOLS_BY_NAME,
    list_agent_prompt,
    lists_will_do,
    result_content,
    run_list_call,
    turn_date,
)
from wobot.agent.retrieval import LookupFailed

TODAY = date(2026, 10, 6)


def call(name: str, **args) -> dict:
    return {"name": name, "args": args, "id": "call-1"}


def test_the_prompt_version_moves_with_the_prompt():
    tools = [convert_to_openai_tool(tool) for tool in LIST_TOOLS]
    prompt = json.dumps([list_agent.INSTRUCTIONS, tools], ensure_ascii=False)
    fingerprint = hashlib.sha256(prompt.encode()).hexdigest()[:12]

    assert (list_agent.PROMPT_VERSION, fingerprint) == (3, "52a9026aa86f")


def test_each_kind_of_list_has_a_tool_with_only_its_own_filters():
    filters = {
        name: set(convert_to_openai_tool(tool)["function"]["parameters"]["properties"])
        for name, tool in TOOLS_BY_NAME.items()
    }

    assert {name: tool.kind for name, tool in TOOLS_BY_NAME.items()} == {
        "list_lectures": "lecture",
        "list_students": "student",
        "list_projects": "project",
        "list_publications": "publication",
        "list_products": "product",
    }
    assert filters["list_products"] == {"category", "contains"}  # products have no year
    assert "degree" in filters["list_students"] and "degree" not in filters["list_lectures"]


def test_the_agent_reads_the_conversation_and_todays_date():
    messages = [
        HumanMessage("列出 2021 年的碩士畢業生"),
        AIMessage("共 5 位。"),
        HumanMessage("那 2022 年的呢？"),
    ]

    _, human = list_agent_prompt(messages, TODAY)

    assert json.loads(human.content) == {
        "earlier_messages": [
            {"role": "human", "content": "列出 2021 年的碩士畢業生"},
            {"role": "ai", "content": "共 5 位。"},
        ],
        "latest_message": "那 2022 年的呢？",
        "today": "2026-10-06",
    }


def test_today_is_the_query_times_date_in_taipei():
    late_in_utc = datetime(2026, 10, 5, 17, tzinfo=ZoneInfo("UTC"))  # 01:00 the 6th in Taipei

    assert turn_date(late_in_utc) == TODAY


async def test_a_call_finds_the_whole_list_and_shows_the_model_its_count(knowledge):
    answer, found = await run_list_call(
        knowledge.db, knowledge.version_id, TODAY, call("list_students", degree="master")
    )

    assert [item["key"] for item in found["items"]] == ["student:master:王小明"]
    assert found["filters"] == {"degree": "master"} and found["window"] is None
    shown = json.loads(answer.content)
    assert shown["result_id"] == found["result_id"] and shown["count"] == 1
    [student] = shown["items"]
    assert student["name"] == "王小明" and student["fulltext_url"] is True  # a flag, no link
    assert answer.tool_call_id == "call-1" and answer.status == "success"


async def test_the_same_query_gets_the_same_result_id(knowledge):
    once = await run_list_call(knowledge.db, knowledge.version_id, TODAY, call("list_products"))
    again = await run_list_call(knowledge.db, knowledge.version_id, TODAY, call("list_products"))

    assert once[1]["result_id"] == again[1]["result_id"]


async def test_the_last_years_are_counted_back_from_today(knowledge):
    _, found = await run_list_call(
        knowledge.db, knowledge.version_id, TODAY, call("list_lectures", last_years=2)
    )

    assert found["window"] == {"from": "2024-10-06", "to": "2026-10-06"}
    assert len(found["items"]) + len(found["uncertain"]) == 2  # the two talks of 2025


async def test_the_model_reads_at_most_the_items_shown():
    many = {
        "result_id": "q-1",
        "tool": "list_publications",
        "kind": "publication",
        "filters": {},
        "window": None,
        "items": [
            {"id": f"r-{n:08x}", "fields": {"year": 2024, "category": "Patents", "item_text": "x"}}
            for n in range(SHOWN_ITEMS + 5)
        ],
        "uncertain": [],
    }

    content = result_content(many)

    assert content["count"] == SHOWN_ITEMS + 5 and len(content["items"]) == SHOWN_ITEMS


@pytest.mark.parametrize(
    ("tool_call", "reason"),
    [
        (call("list_courses"), "there is no tool list_courses"),
        (call("list_students", degree="bachelor"), "degree"),
        (call("list_lectures", last_years=0), "last_years"),
        (call("list_lectures", last_years=5, year_from=2024), "either a window or years"),
        (call("list_students", contains=" "), "contains takes"),
    ],
)
async def test_a_refused_call_tells_the_model_why(knowledge, tool_call, reason):
    answer, found = await run_list_call(knowledge.db, knowledge.version_id, TODAY, tool_call)

    assert found is None and answer.status == "error"
    assert reason in answer.content and "call again" in answer.content


class Unreachable:
    """A database that cannot be reached."""

    @asynccontextmanager
    async def begin(self):
        raise TimeoutError("no answer")
        yield


async def test_an_unreachable_database_fails_the_lookup():
    with pytest.raises(LookupFailed):
        await run_list_call(Unreachable(), 1, TODAY, call("list_products"))


def answered(count: int, status: str = "success", uncertain: int | None = None) -> ToolMessage:
    content = {"count": count} | ({} if uncertain is None else {"uncertain_count": uncertain})
    return ToolMessage(json.dumps(content), tool_call_id="call-1", status=status)


@pytest.mark.parametrize(
    ("scratch", "will_do"),
    [
        ([AIMessage("", tool_calls=[call("list_products")]), answered(3)], True),
        ([AIMessage(""), answered(3), answered(2)], True),
        ([AIMessage(""), answered(0, uncertain=2)], True),  # only uncertain, still found
        ([AIMessage(""), answered(3), answered(0)], False),  # one list empty
        ([AIMessage(""), ToolMessage("Not run: ...", tool_call_id="c", status="error")], False),
        ([AIMessage("", tool_calls=[call("list_products")])], False),  # no tool has answered
        ([], False),
    ],
)
def test_the_agent_is_asked_again_only_when_a_round_left_something_to_fix(scratch, will_do):
    assert lists_will_do(scratch) is will_do
