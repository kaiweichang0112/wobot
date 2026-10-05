import json
import uuid
from datetime import UTC, date, datetime

from langchain_core.messages import HumanMessage, ToolMessage

from tests.agent.fakes import ScriptedChatModel, answers, calls
from tests.agent.test_tools import Down, call
from tests.knowledge.fakes import FakeEmbedder
from wobot.agent.answers import Answer, ListRef
from wobot.agent.build import build_agent
from wobot.agent.guard import TurnEvidence
from wobot.agent.render import (
    RETRYABLE_TEXT,
    UNVERIFIED_TEXT,
    ReplyStatus,
    render,
    turn_reply,
)
from wobot.agent.tools import (
    RecordsResult,
    ToolStatus,
    TurnContext,
    build_tools,
    record_handle,
    records_content,
    result_handle,
)
from wobot.knowledge.lists import Listed, RecordQuery, Window

PAGE = "https://www.grc.yzu.edu.tw/speeches"


def lecture(title, location=None, day=date(2024, 5, 10), pdf_url=None):
    fields = {
        "date_raw": day.strftime("%Y/%m/%d"),
        "lecture_date": day,
        "category": "invited",
        "title": title,
        "entry_text": title,
        "event": "A conference",
        "location": location,
        "pdf_url": pdf_url,
    }
    return Listed(uuid.uuid4(), f"lecture:{title}", fields, PAGE, {})


def publication(text, year):
    fields = {"year": year, "year_raw": str(year), "item_text": text}
    return Listed(uuid.uuid4(), f"publication:{text}", fields, PAGE, {})


def held(query, items, uncertain=()):
    """The evidence of a turn whose one query_records result is q-1."""
    result = RecordsResult(ToolStatus.FOUND, "q-1", query, list(items), list(uncertain))
    return TurnEvidence(results={"q-1": result}, looked_up=True)


def answer(*refs, text="以下是清單：", citations=()):
    return Answer(
        answer=text,
        grounding="grounded",
        citations=list(citations),
        lists=[ListRef(result_id=r, item_ids=ids) for r, ids in refs],
        recommendation=None,
    )


def test_every_item_is_shown_and_a_missing_location_is_left_out():
    talks = [lecture("Smart mattress", "Taipei"), lecture("Robots")]

    reply = render(answer(("q-1", None)), held(RecordQuery("lecture"), talks))

    assert reply.text.splitlines()[2:4] == [
        "1. 2024/05/10　Smart mattress，A conference，地點：Taipei",
        "2. 2024/05/10　Robots，A conference",
    ]
    assert "None" not in reply.text and reply.text.endswith(f"來源：{PAGE}")
    assert (reply.status, reply.grounding) == (ReplyStatus.ANSWERED, "grounded")


def test_the_model_is_told_a_pdf_exists_and_the_reply_links_it():
    slides = "https://drive.google.com/file/d/1/view"
    talks = [lecture("Robots", pdf_url=slides), lecture("Smart mattress")]
    query = RecordQuery("lecture")
    result = RecordsResult(ToolStatus.FOUND, "q-1", query, talks)

    shown = json.loads(records_content(result))["items"]
    reply = render(answer(("q-1", None)), held(query, talks))

    assert [item.get("pdf_url") for item in shown] == [True, None]
    assert "1. 2024/05/10　Robots，A conference，PDF：" + slides in reply.text.splitlines()


def test_item_ids_narrow_a_list():
    talks = [lecture("Smart mattress"), lecture("Robots")]

    reply = render(
        answer(("q-1", [record_handle(talks[0].record_id)])),
        held(RecordQuery("lecture"), talks),
    )

    assert "Smart mattress" in reply.text and "Robots" not in reply.text


def test_uncertain_items_are_listed_apart_with_why():
    window = Window(date(2021, 10, 5), date(2026, 10, 5))
    query = RecordQuery("publication", window=window)

    reply = render(
        answer(("q-1", None)), held(query, [publication("A", 2023)], [publication("B", 2026)])
    )

    lines = reply.text.splitlines()
    assert "1. 2023　A" in lines
    assert (
        "以下 1 筆的來源只記載年份或沒有日期，無法確定是否在 2021-10-05 至 2026-10-05 之間："
        in lines
    )
    assert "- 2026　B" in lines


def test_cited_sources_follow_the_answer_once():
    evidence = TurnEvidence(
        sources={"k-1": ["https://a.example", PAGE], "r-1": ["https://a.example"]},
        looked_up=True,
    )

    reply = render(answer(text="GRC 成立於 2003 年。", citations=["k-1", "r-1"]), evidence)

    assert reply.text == f"GRC 成立於 2003 年。\n\n來源：https://a.example、{PAGE}"


def test_a_source_a_list_names_is_not_cited_again():
    talks = [lecture("Robots")]
    handle = record_handle(talks[0].record_id)
    evidence = held(RecordQuery("lecture"), talks)
    evidence.sources[handle] = [PAGE]

    reply = render(answer(("q-1", None), citations=[handle]), evidence)

    assert reply.text.count(PAGE) == 1


def test_no_information_shows_no_sources():
    evidence = TurnEvidence(sources={"k-1": [PAGE]}, looked_up=True)
    searched = Answer(
        answer="來源沒有內部會議紀錄。",
        grounding="no_info",
        citations=["k-1"],
        lists=[],
        recommendation=None,
    )

    reply = render(searched, evidence)

    assert (reply.text, reply.citations) == ("來源沒有內部會議紀錄。", [])


def test_a_cited_lecture_links_its_pdf_once():
    slides = "https://drive.google.com/file/d/1/view"
    talk = lecture("Robots", pdf_url=slides)
    handle = record_handle(talk.record_id)
    evidence = held(RecordQuery("lecture"), [talk])
    evidence.sources[handle] = [PAGE]
    evidence.pdfs[handle] = slides

    cited = render(answer(text="有附 PDF。", citations=[handle]), evidence)
    listed = render(answer(("q-1", [handle]), citations=[handle]), evidence)

    assert cited.text == f"有附 PDF。\n\n來源：{PAGE}\nPDF：{slides}"
    assert listed.text.count(slides) == 1


async def ask(db, embedder, version_id, script):
    agent = build_agent(ScriptedChatModel(script=script), build_tools(db, embedder))
    context = TurnContext("account-1", datetime(2026, 10, 3, tzinfo=UTC), version_id)
    state = await agent.ainvoke({"messages": [HumanMessage("列出演講")]}, context=context)
    return turn_reply(state["messages"][1:], state["structured_response"], context.artifacts)


async def test_a_turn_shows_the_list_it_named(knowledge):
    result_id = result_handle(knowledge.version_id, RecordQuery("lecture"))
    script = [
        calls("query_records", {"record_type": "lecture"}),
        answers(
            "共兩場演講：",
            [{"result_id": result_id, "item_ids": None}],
            grounding="grounded",
        ),
    ]

    reply = await ask(knowledge.db, knowledge.embedder, knowledge.version_id, script)

    assert reply.text.startswith("共兩場演講：")
    assert [len(shown.items) for shown in reply.lists] == [2]


async def test_a_cited_passage_shows_where_it_was_read(knowledge):
    # The fake embedder is deterministic: the same search finds the same passages.
    tools = build_tools(knowledge.db, knowledge.embedder)
    found = await call(tools, "search_knowledge", {"queries": ["GRC"]}, knowledge.version_id)
    first = json.loads(found.content)["items"][0]["id"]
    script = [
        calls("search_knowledge", {"queries": ["GRC"]}),
        answers("GRC 在元智大學。", grounding="grounded", citations=[first]),
    ]

    reply = await ask(knowledge.db, knowledge.embedder, knowledge.version_id, script)

    sources = {m.source_url for e in found.artifact.evidence for m in e.members}
    assert reply.status is ReplyStatus.ANSWERED
    assert reply.text.startswith("GRC 在元智大學。\n\n來源：")
    assert any(url in reply.text for url in sources)


async def test_a_forged_citation_is_held_back(knowledge):
    forged = answers("兩場。", grounding="grounded", citations=["r-00000000"])
    script = [calls("query_records", {"record_type": "lecture"}), forged, forged]

    reply = await ask(knowledge.db, knowledge.embedder, knowledge.version_id, script)

    assert (reply.text, reply.status) == (UNVERIFIED_TEXT, ReplyStatus.UNVERIFIED)
    assert "r-00000000" in reply.problems[0]


async def test_a_failed_tool_is_never_shown_as_no_information():
    script = [
        calls("query_records", {"record_type": "lecture"}),
        answers("來源中沒有演講紀錄。", grounding="no_info"),
    ]

    reply = await ask(Down(), FakeEmbedder(), 1, script)

    assert (reply.text, reply.status) == (RETRYABLE_TEXT, ReplyStatus.RETRYABLE)


def test_a_turn_without_an_answer_shows_no_unchecked_text():
    message = ToolMessage("{}", tool_call_id="call-1", status="error")

    reply = turn_reply([message], None)

    assert (reply.text, reply.status) == (RETRYABLE_TEXT, ReplyStatus.RETRYABLE)
