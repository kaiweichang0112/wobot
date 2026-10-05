"""The reply a user reads: the model's words, then every listed item, written by code.

A list never passes through the model's words, so none of its items can be dropped,
merged or made up (DB6). The model only says which results to show, and which of their
items when it narrows one by meaning; an ID it did not get this turn shows nothing.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from wobot.agent.answers import Answer
from wobot.agent.messages import final_text
from wobot.agent.tools import RecordsResult, record_handle
from wobot.knowledge.lists import Listed

DEGREES = {"master": "碩士", "phd": "博士"}


def _lecture(f: Mapping[str, Any]) -> str:
    # A field the source does not give is left out, never shown empty (AC-RAG-04); the
    # location is named, so it is not taken for part of the event.
    location = f"地點：{f['location']}" if f["location"] else None
    parts = [f["title"] or f["entry_text"], f["event"], location]
    return f"{f['date_raw'] or f['lecture_date']}　" + "，".join(p for p in parts if p)


def _student(f: Mapping[str, Any]) -> str:
    titles = f"《{f['thesis_title_zh']}》" if f["thesis_title_zh"] else ""
    if f["thesis_title_en"]:
        titles += f"（{f['thesis_title_en']}）"
    degree = DEGREES.get(f["degree"], f["degree"])
    return f"{f['graduation_year']}　{degree}　{f['name']}" + (f"：{titles}" if titles else "")


def _project(f: Mapping[str, Any]) -> str:
    parts = [f["title_zh"] or f["title_en"], f["funder_raw"], f["period_raw"], f["amount_raw"]]
    return f"{f['year']}　" + "，".join(p for p in parts if p)


def _publication(f: Mapping[str, Any]) -> str:
    return f"{f['year'] or f['year_raw'] or ''}　{f['item_text']}".strip()


def _product(f: Mapping[str, Any]) -> str:
    parts = [f["product_name"], f["company_name"], f["category_l2_label"]]
    return "，".join(p for p in parts if p)


LINES: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "lecture": _lecture,
    "student": _student,
    "project": _project,
    "publication": _publication,
    "product": _product,
}


@dataclass(frozen=True)
class ShownList:
    result_id: str
    items: list[Listed]  # shown as matches
    uncertain: list[Listed]  # shown apart: their dates cannot place them


@dataclass(frozen=True)
class Reply:
    text: str
    lists: list[ShownList] = field(default_factory=list)
    # IDs the model gave that no result of this turn holds: nothing is shown for them,
    # and the guard of B6 will act on them.
    unknown_ids: list[str] = field(default_factory=list)


def turn_results(messages: Sequence[BaseMessage]) -> dict[str, RecordsResult]:
    """The query_records results among a turn's messages, by result_id."""
    return {
        m.artifact.result_id: m.artifact
        for m in messages
        if isinstance(m, ToolMessage) and isinstance(m.artifact, RecordsResult)
    }


def turn_reply(messages: Sequence[BaseMessage], answer: Answer | None) -> Reply:
    """The reply to one turn, from its messages after the question and the parsed answer."""
    if answer is None:  # no structured answer: what the model last wrote
        replies = [m for m in messages if isinstance(m, AIMessage)]
        return Reply(final_text(replies[-1]) if replies else "")
    return render(answer, turn_results(messages))


def render(answer: Answer, results: Mapping[str, RecordsResult]) -> Reply:
    shown: list[ShownList] = []
    unknown: list[str] = []
    for ref in answer.lists:
        result = results.get(ref.result_id)
        if result is None:
            unknown.append(ref.result_id)
            continue
        items, uncertain = result.items, result.uncertain
        if ref.item_ids is not None:
            wanted = set(ref.item_ids)
            held = {record_handle(i.record_id) for i in [*items, *uncertain]}
            unknown += [handle for handle in ref.item_ids if handle not in held]
            items = [i for i in items if record_handle(i.record_id) in wanted]
            uncertain = [i for i in uncertain if record_handle(i.record_id) in wanted]
        shown.append(ShownList(ref.result_id, items, uncertain))
    blocks = [answer.answer.strip(), *(_block(s, results[s.result_id]) for s in shown)]
    return Reply("\n\n".join(b for b in blocks if b), shown, unknown)


def _block(shown: ShownList, result: RecordsResult) -> str:
    line = LINES[result.query.kind]
    lines = [f"{n}. {line(item.fields)}" for n, item in enumerate(shown.items, start=1)]
    if shown.uncertain:
        window = result.query.window
        lines += [
            "",
            f"以下 {len(shown.uncertain)} 筆的來源只記載年份或沒有日期，無法確定是否在 "
            f"{window.start} 至 {window.end} 之間：",
            *(f"- {line(item.fields)}" for item in shown.uncertain),
        ]
    sources = dict.fromkeys(i.source_url for i in [*shown.items, *shown.uncertain])
    if sources:
        lines += ["", "來源：" + "、".join(sources)]
    return "\n".join(lines)
