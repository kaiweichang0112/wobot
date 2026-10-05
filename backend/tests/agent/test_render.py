import uuid
from datetime import UTC, date, datetime

from langchain_core.messages import HumanMessage

from tests.agent.fakes import ScriptedChatModel, answers, calls
from wobot.agent.answers import Answer, ListRef
from wobot.agent.build import build_agent
from wobot.agent.render import render, turn_reply
from wobot.agent.tools import (
    RecordsResult,
    ToolStatus,
    TurnContext,
    build_tools,
    record_handle,
    result_handle,
)
from wobot.knowledge.lists import Listed, RecordQuery, Window

PAGE = "https://www.grc.yzu.edu.tw/speeches"


def lecture(title, location=None, day=date(2024, 5, 10)):
    fields = {
        "date_raw": day.strftime("%Y/%m/%d"),
        "lecture_date": day,
        "title": title,
        "entry_text": title,
        "event": "A conference",
        "location": location,
    }
    return Listed(uuid.uuid4(), f"lecture:{title}", fields, PAGE, {})


def publication(text, year):
    fields = {"year": year, "year_raw": str(year), "item_text": text}
    return Listed(uuid.uuid4(), f"publication:{text}", fields, PAGE, {})


def result(query, items, uncertain=()):
    return RecordsResult(ToolStatus.FOUND, "q-1", query, list(items), list(uncertain))


def answer(*refs, text="以下是清單："):
    return Answer(answer=text, lists=[ListRef(result_id=r, item_ids=ids) for r, ids in refs])


def test_every_item_is_shown_and_a_missing_location_is_left_out():
    talks = [lecture("Smart mattress", "Taipei"), lecture("Robots")]

    reply = render(answer(("q-1", None)), {"q-1": result(RecordQuery("lecture"), talks)})

    assert reply.text.splitlines()[2:4] == [
        "1. 2024/05/10　Smart mattress，A conference，地點：Taipei",
        "2. 2024/05/10　Robots，A conference",
    ]
    assert "None" not in reply.text and reply.text.endswith(f"來源：{PAGE}")


def test_item_ids_narrow_a_list_and_unknown_ids_show_nothing():
    talks = [lecture("Smart mattress"), lecture("Robots")]
    wanted = [record_handle(talks[0].record_id), "r-00000000"]

    reply = render(
        answer(("q-1", wanted), ("q-9", None)), {"q-1": result(RecordQuery("lecture"), talks)}
    )

    assert "Smart mattress" in reply.text and "Robots" not in reply.text
    assert reply.unknown_ids == ["r-00000000", "q-9"]


def test_uncertain_items_are_listed_apart_with_why():
    window = Window(date(2021, 10, 5), date(2026, 10, 5))
    query = RecordQuery("publication", window=window)

    reply = render(
        answer(("q-1", None)),
        {"q-1": result(query, [publication("A", 2023)], [publication("B", 2026)])},
    )

    lines = reply.text.splitlines()
    assert "1. 2023　A" in lines
    assert (
        "以下 1 筆的來源只記載年份或沒有日期，無法確定是否在 2021-10-05 至 2026-10-05 之間："
        in lines
    )
    assert "- 2026　B" in lines


async def test_a_turn_shows_the_list_it_named(knowledge):
    result_id = result_handle(knowledge.version_id, RecordQuery("lecture"))
    model = ScriptedChatModel(
        script=[
            calls("query_records", {"record_type": "lecture"}),
            answers("共兩場演講：", [{"result_id": result_id, "item_ids": None}]),
        ]
    )
    agent = build_agent(model, build_tools(knowledge.db, knowledge.embedder))
    context = TurnContext("account-1", datetime(2026, 10, 3, tzinfo=UTC), knowledge.version_id)

    state = await agent.ainvoke({"messages": [HumanMessage("列出演講")]}, context=context)
    reply = turn_reply(state["messages"][1:], state["structured_response"])

    assert reply.text.startswith("共兩場演講：")
    assert [len(shown.items) for shown in reply.lists] == [2]
    assert reply.unknown_ids == []
