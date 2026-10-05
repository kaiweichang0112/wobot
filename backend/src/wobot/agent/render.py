"""The reply a user reads: the model's words, then every listed item, written by code.

A list never passes through the model's words, so none of its items can be dropped,
merged or made up (DB6). The model only says which results to show, and which of their
items when it narrows one by meaning.

Only an answer the guard passes is shown. When a tool failed, or the model gave no answer
that passes, code says so in its own words: a failure must never read as having no
information, and an unchecked answer is never shown (DB5).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from langchain_core.messages import BaseMessage

from wobot.agent.answers import Answer, Grounding, Pick
from wobot.agent.guard import TurnEvidence, problems, turn_evidence
from wobot.agent.tools import RecordsResult, record_handle
from wobot.knowledge.lists import Listed

DEGREES = {"master": "碩士", "phd": "博士"}


def _lecture(f: Mapping[str, Any]) -> str:
    # A field the source does not give is left out, never shown empty (AC-RAG-04); the
    # location is named, so it is not taken for part of the event.
    location = f"地點：{f['location']}" if f["location"] else None
    pdf = f"PDF：{f['pdf_url']}" if f["pdf_url"] else None
    parts = [f["title"] or f["entry_text"], f["event"], location, pdf]
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


class ReplyStatus(StrEnum):
    ANSWERED = "answered"
    UNVERIFIED = "unverified"  # the answer failed the guard; code says it cannot confirm
    RETRYABLE = "retryable"  # a tool failed or no answer came; the user may try again


# What code says in place of the model; the app is Chinese first.
UNVERIFIED_TEXT = (
    "抱歉，我無法用資料來源確認這個回答，為避免提供錯誤資訊，這次先不回覆。可以換個說法再問一次。"
)
RETRYABLE_TEXT = "抱歉，這次查詢資料時發生問題，請稍後再試一次。"


@dataclass(frozen=True)
class ShownList:
    result_id: str
    items: list[Listed]  # shown as matches
    uncertain: list[Listed]  # shown apart: their dates cannot place them


@dataclass(frozen=True)
class Reply:
    text: str
    status: ReplyStatus
    grounding: Grounding | None = None  # the model's, when its answer is shown
    citations: list[str] = field(default_factory=list)
    lists: list[ShownList] = field(default_factory=list)
    # Why the guard held the answer back, in the words the model would be told.
    problems: list[str] = field(default_factory=list)
    action: str | None = None  # the recommendation's decision, when there was one
    products: list[Listed] = field(default_factory=list)  # the products recommended


def turn_reply(
    messages: Sequence[BaseMessage],
    answer: Answer | None,
    artifacts: Mapping[str, Any] | None = None,
    requirements: Mapping[str, Any] | None = None,
) -> Reply:
    """The reply to one turn, from its messages after the question, the parsed answer, the
    artifacts its context kept and the user's needs as its state holds them."""
    evidence = turn_evidence(messages, artifacts)
    if evidence.failed or answer is None:
        return Reply(RETRYABLE_TEXT, ReplyStatus.RETRYABLE)
    if found := problems(answer, evidence, requirements):
        return Reply(UNVERIFIED_TEXT, ReplyStatus.UNVERIFIED, problems=found)
    return render(answer, evidence, requirements)


def render(
    answer: Answer, evidence: TurnEvidence, requirements: Mapping[str, Any] | None = None
) -> Reply:
    """An answer the guard passed, with the sources it cites, the products it recommends
    and its lists in full."""
    # Nothing answered the question, so nothing is its source: sources under "not found"
    # would read as where the answer is.
    citations = [] if answer.grounding == "no_info" else answer.citations
    shown: list[ShownList] = []
    for ref in answer.lists:
        result = evidence.results[ref.result_id]
        items, uncertain = result.items, result.uncertain
        if ref.item_ids is not None:
            wanted = set(ref.item_ids)
            items = [i for i in items if record_handle(i.record_id) in wanted]
            uncertain = [i for i in uncertain if record_handle(i.record_id) in wanted]
        shown.append(ShownList(ref.result_id, items, uncertain))
    decision = answer.recommendation
    picks = decision.products if decision is not None else []
    # A source a list or a product card below already names is not named twice.
    listed = {i.source_url for s in shown for i in [*s.items, *s.uncertain]}
    listed |= {
        url
        for pick in picks
        for check in pick.checks
        for i in check.evidence_ids
        for url in evidence.sources[i]
    }
    cited = [
        url
        for url in dict.fromkeys(u for c in citations for u in evidence.sources[c])
        if url not in listed
    ]
    # A cited lecture's PDF, unless a list below shows the lecture with it.
    listed_ids = {record_handle(i.record_id) for s in shown for i in [*s.items, *s.uncertain]}
    pdfs = [
        evidence.pdfs[c]
        for c in dict.fromkeys(citations)
        if c in evidence.pdfs and c not in listed_ids
    ]
    text = answer.answer.strip()
    if cited:
        text += "\n\n來源：" + "、".join(cited)
    if pdfs:
        text += ("\n" if cited else "\n\n") + "PDF：" + "、".join(pdfs)
    cards = [_card(pick, evidence, requirements or {}) for pick in picks]
    blocks = [text, *cards, *(_block(s, evidence.results[s.result_id]) for s in shown)]
    return Reply(
        "\n\n".join(b for b in blocks if b),
        ReplyStatus.ANSWERED,
        answer.grounding,
        citations,
        shown,
        action=decision.action if decision is not None else None,
        products=[evidence.products[pick.product_id] for pick in picks],
    )


def _card(pick: Pick, evidence: TurnEvidence, requirements: Mapping[str, Any]) -> str:
    """A recommended product as code shows it: what it is, where each condition is stated,
    and how to reach its maker. A field the catalog does not give is left out."""
    f = evidence.products[pick.product_id].fields
    needs = {need["id"]: need["text"] for need in requirements.get("must_have", [])}
    lines = [f"推薦：{f['product_name']}（{f['company_name']}）"]
    for check in pick.checks:
        sources = dict.fromkeys(u for i in check.evidence_ids for u in evidence.sources[i])
        lines.append(f"- {needs[check.requirement_id]}：依據 " + "、".join(sources))
    if f["product_url"]:
        lines.append(f"產品網頁：{f['product_url']}")
    if f["contact_phone"]:
        lines.append(f"廠商電話：{f['contact_phone']}")
    return "\n".join(lines)


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
