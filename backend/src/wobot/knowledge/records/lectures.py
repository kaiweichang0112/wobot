"""GRC speeches → lecture records: code splits the list, a model labels each entry's parts.

The page has two categories, each a Wix repeater of year blocks beside their lists. One
list item is one talk, so the page's <li>s decide how many records there are, whatever a
model answers. Dates and slide links are read by pattern. Title, event and location come
from a model (`extraction`), each kept only when the entry states it word for word.
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from wobot.knowledge.extraction import Answer, LectureFields, Question, grounded
from wobot.knowledge.records.drafts import RecordDraft, record_draft, separate_collisions
from wobot.knowledge.records.grc import Parsed, link_json, without_trailing_labels
from wobot.knowledge.records.text import clean_line, key_text
from wobot.knowledge.sources.wix import Block, Link

LECTURE_CATEGORIES = {
    "Keynote and plenary speeches at international conferences": "keynote",
    "Invited speeches and guest lectures": "invited",
}
_YEAR_BLOCK = re.compile(r"^(?P<first>\d{4})(?:\s*[~～-]\s*(?P<last>\d{4}))?$")
# "2024/09/21", or "2024/09" for a month. The entry's last date is the talk's own.
_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*/\s*(?P<month>\d{1,2})(?:\s*/\s*(?P<day>\d{1,2}))?(?!\d)"
)


@dataclass(frozen=True)
class LectureEntry:
    category: str
    year_block: str
    position: int  # within its year block
    text: str  # one line, link labels removed: what the model reads
    raw_text: str
    links: tuple[Link, ...]


@dataclass
class Lectures:
    entries: list[LectureEntry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # blocking


def split_lectures(blocks: Sequence[Block]) -> Lectures:
    """One entry per list item, filed under its category heading and its year block."""
    lectures = Lectures()
    category: str | None = None
    by_category: dict[str, list[Block]] = {}
    for block in blocks:
        if block.kind in ("heading", "paragraph") and block.text in LECTURE_CATEGORIES:
            category = LECTURE_CATEGORIES[block.text]
            by_category.setdefault(category, [])
        elif category is not None:
            by_category[category].append(block)
        elif block.kind == "item":
            lectures.problems.append(f"speeches: an item before any category: {block.text!r}")
    if missing := [name for name, code in LECTURE_CATEGORIES.items() if code not in by_category]:
        lectures.problems.append(f"speeches: no category {missing}")
    for code, category_blocks in by_category.items():
        _split_category(code, category_blocks, lectures)
    return lectures


def _split_category(category: str, blocks: Sequence[Block], lectures: Lectures) -> None:
    """A repeater: a year block and its list share an item ID.

    Both categories reuse the same item IDs, so pairing stays within one category.
    """
    year_blocks: dict[str, str] = {}
    for block in blocks:
        item = block.repeater_item
        if item is not None and block.kind == "paragraph" and _YEAR_BLOCK.match(block.text):
            year_blocks.setdefault(item, block.text)
    if not year_blocks:
        lectures.problems.append(f"speeches {category}: no year blocks found")
    counts = Counter[str]()
    for block in (b for b in blocks if b.kind == "item"):
        item = block.repeater_item or ""
        if item not in year_blocks:
            lectures.problems.append(
                f"speeches {category}: an item outside any year block: {block.text!r}"
            )
            continue
        text = clean_line(without_trailing_labels(block.text, block.links))
        if text is None:
            lectures.problems.append(f"speeches {category} {year_blocks[item]}: an empty item")
            continue
        counts[item] += 1
        lectures.entries.append(
            LectureEntry(category, year_blocks[item], counts[item], text, block.text, block.links)
        )
    for item, year_block in year_blocks.items():
        if not counts[item]:
            lectures.problems.append(f"speeches {category} {year_block}: a year without talks")


def lecture_records(
    lectures: Lectures, answers: Mapping[str, Answer], *, speaker: str, question: Question
) -> Parsed:
    """The entries as records, with the parts of each the model found in it.

    `answers` holds the model's answer for every entry's text.
    """
    parsed = Parsed(problems=list(lectures.problems))
    dead_links = Counter[tuple[str, str]]()
    for entry in lectures.entries:
        parsed.drafts.append(_lecture(entry, answers[entry.text], speaker, question))
        dead_links[entry.category, entry.year_block] += sum(
            link.url is None for link in entry.links
        )
    # One note per year block: a block's links tend to break together.
    parsed.notes += [
        f"speeches {category} {year_block}: {count} links lead nowhere on the source page"
        for (category, year_block), count in dead_links.items()
        if count
    ]
    parsed.drafts = separate_collisions(parsed.drafts)
    return parsed


def _lecture(entry: LectureEntry, answer: Answer, speaker: str, question: Question) -> RecordDraft:
    warnings: list[str] = []
    if answer.failure is not None:
        warnings.append(f"the model read nothing: {answer.failure}")
    read = answer.output or {}
    spans = {}
    for name in LectureFields.model_fields:
        spans[name] = grounded(read.get(name), entry.text)
        if read.get(name) is not None and spans[name] is None:
            warnings.append(f"{name} {read[name]!r} is not in the entry; left empty")
    if answer.output is not None and read.get("title") is None:
        warnings.append("the model found no title")
    lecture_date, precision, date_raw, year = _date(entry, warnings)
    links = [
        {"kind": "pdf" if link.text.casefold() == "pdf" else "link", **link_json(link)}
        for link in entry.links
    ]
    where = f"speeches {entry.category} {entry.year_block} #{entry.position}"
    return record_draft(
        "lecture",
        f"lecture:{entry.category}:{key_text(entry.text)[:160]}",
        raw={"text": entry.raw_text, "links": [link_json(link) for link in entry.links]},
        fields={
            "speaker": speaker,
            "category": entry.category,
            "year_block": entry.year_block,
            "entry_text": entry.text,
            **spans,
            "lecture_date": lecture_date,
            "date_precision": precision,
            "date_raw": date_raw,
            "year": year,
            "pdf_url": next((link["url"] for link in links if link["kind"] == "pdf"), None),
            "links": links,
            "extraction_model": question.model,
            "extraction_prompt_version": question.prompt_version,
        },
        locator={
            "category": entry.category,
            "year_block": entry.year_block,
            "position": entry.position,
        },
        warnings=[f"{where}: {warning}" for warning in warnings],
    )


def _date(
    entry: LectureEntry, warnings: list[str]
) -> tuple[date | None, str | None, str | None, int | None]:
    """The talk's date, its precision, the text it was read from, and the talk's year.

    Without a valid date the year comes from the block, when the block is a single year.
    """
    block = _YEAR_BLOCK.match(entry.year_block)
    assert block is not None  # split_lectures takes only blocks that match
    first, last = int(block["first"]), int(block["last"] or block["first"])
    block_year = first if first == last else None
    matches = list(_DATE.finditer(entry.text))
    if not matches:
        warnings.append("no date")
        return None, None, None, block_year
    match = matches[-1]
    day = match["day"]
    try:
        value = date(int(match["year"]), int(match["month"]), int(day) if day else 1)
    except ValueError:
        # A typo on the page: keep what it says, and claim no date it does not state.
        warnings.append(f"no valid date in {match.group(0)!r}; date left empty")
        return None, None, match.group(0), block_year
    if not first <= value.year <= last:
        warnings.append(f"dated {match.group(0)}, outside its year block {entry.year_block}")
    return value, "day" if day else "month", match.group(0), value.year
