"""GRC website blocks → records: students, projects, publications, and the profile page.

The page structure decides how many records there are; nothing here guesses. Each parser
returns its drafts and the problems that must stop a publish, such as a project without an
amount, so a page that changed shape fails loudly instead of losing items.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import groupby
from typing import Any, Literal

from wobot.knowledge.records.drafts import RecordDraft, record_draft, separate_collisions
from wobot.knowledge.records.text import key_text
from wobot.knowledge.sources.wix import Block, Link

Degree = Literal["master", "phd"]

_CJK = re.compile(r"[㐀-鿿]")
_YEAR_ONLY = re.compile(r"^(\d{4})$")
_PROJECT_YEAR = re.compile(r"^(\d{4})\s*Research projects$", re.IGNORECASE)
# Doubled slashes are tolerated: the page has "2021/06/01~2023//03/31".
_DATE = r"\d{4}/+\d{1,2}/+\d{1,2}"
_PERIOD = re.compile(rf"(?P<start>{_DATE})\s*[~～]\s*(?P<end>{_DATE})")
_AMOUNT = re.compile(r"NTD\s*(?P<amount>\d{1,3}(?:,\d{3})*|\d+)")
# Funder text: Chinese first, then English, as in "科技部 Ministry of Science and Technology".
_FUNDER = re.compile(r"^(?P<zh>.*[㐀-鿿）」』])\s+(?P<en>[A-Za-z].*)$")
_ZH_TITLE = re.compile(r"^論文名稱\s*[:：]\s*")
_EN_TITLE = re.compile(r"^Thesis title\s*[:：]\s*", re.IGNORECASE)
_FULLTEXT = "電子全文"
_CITATION_YEAR = re.compile(r"\((\d{4})[),]")  # APA "(2026)." or "(1989, September)"
_DATE_YEAR = re.compile(r"\b(\d{4})[/-]\d{1,2}\b")  # patents and columns: "2025/05/01"
_ROC_YEAR = re.compile(r"(?<!\d)(\d{2,3})年")  # "109年第二十九期"
ROC_YEAR_OFFSET = 1911
_DOI = re.compile(r"https?://(?:dx\.)?doi\.org/(?P<doi>10\.\S+)", re.IGNORECASE)
# Anchor labels that trail an item's text and say nothing about it.
_LINK_LABELS = {"pdf", "link", "電子全文", "video", "影音", "全文"}

PUBLICATION_CATEGORIES = {
    "Journal papers": "journal",
    "Conference papers": "conference",
    "Books / book chapters": "book",
    "Patents": "patent",
    "General publications": "general",
}
PROFILE_SECTIONS = (
    "Education",
    "Current Positions",
    "Academic Experience",
    "Industrial Experience",
    "Individual Awards",
    "Research Interests",
)


@dataclass
class Parsed:
    drafts: list[RecordDraft] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # blocking


def link_json(link: Link) -> dict[str, Any]:
    return {"text": link.text, "url": link.url}


def _groups(blocks: Sequence[Block]) -> list[list[Block]]:
    """Paragraph runs separated by blank paragraphs: one run per student or project."""
    runs = [list(run) for blank, run in groupby(blocks, key=lambda b: b.kind == "blank")]
    return [run for run in runs if run[0].kind != "blank"]


def _lines(blocks: Sequence[Block]) -> list[str]:
    return [line for block in blocks for line in block.text.split("\n") if line]


# --- Students ---------------------------------------------------------------------------


def parse_students(blocks: Sequence[Block], degree: Degree) -> Parsed:
    """A Wix repeater: each item is a year beside its graduates, paired by item ID."""
    parsed = Parsed()
    years: dict[str, int] = {}
    entries: dict[str, list[Block]] = {}
    for block in blocks:
        item = block.repeater_item
        if item is None:
            continue
        if (match := _YEAR_ONLY.match(block.text)) and item not in years:
            years[item] = int(match.group(1))
        else:
            entries.setdefault(item, []).append(block)
    if not years:
        parsed.problems.append(f"{degree}: no year blocks found")
    for item, year in years.items():
        groups = _groups(entries.get(item, []))
        if not groups:
            parsed.problems.append(f"{degree} {year}: a year without students")
        for position, group in enumerate(groups, start=1):
            parsed.drafts.append(_student(group, degree, year, item, position, parsed))
    parsed.drafts = separate_collisions(parsed.drafts)
    return parsed


def _student(
    group: list[Block], degree: Degree, year: int, item: str, position: int, parsed: Parsed
) -> RecordDraft:
    lines = _lines(group)
    links = [link for block in group for link in block.links]
    name, title_zh, title_en, other = None, None, None, []
    for line in lines:
        if _ZH_TITLE.match(line):
            title_zh = _ZH_TITLE.sub("", line) or None
        elif _EN_TITLE.match(line):
            title_en = _EN_TITLE.sub("", line) or None
        elif line == _FULLTEXT:
            continue
        elif name is None:
            name = line
        else:
            other.append(line)
    where = f"{degree} {year} #{position}"
    if name is None or not (title_zh or title_en):
        parsed.problems.append(f"{where}: needs a name and a thesis title, read {lines!r}")
    fulltext = [link for link in links if link.text == _FULLTEXT]
    warnings = [f"unread line {line!r}" for line in other]
    if not fulltext:
        warnings.append("no full-text link")
    elif fulltext[0].url is None:
        warnings.append("full-text link leads nowhere on the source page")
    return record_draft(
        "student",
        f"student:{degree}:{key_text(name)}",
        raw={"lines": lines, "links": [link_json(link) for link in links]},
        fields={
            "name": name,
            "degree": degree,
            "graduation_year": year,
            "thesis_title_zh": title_zh,
            "thesis_title_en": title_en,
            "fulltext_url": fulltext[0].url if fulltext else None,
        },
        locator={"repeater_item": item, "position": position},
        warnings=[f"{where}: {warning}" for warning in warnings],
    )


# --- Projects ---------------------------------------------------------------------------


def parse_projects(blocks: Sequence[Block]) -> Parsed:
    """Year headings ("2024 Research projects"), each followed by its projects.

    Each project ends with the line holding funder, period and amount; the lines before it
    are its titles, Chinese first. Blank paragraphs usually separate projects but not
    always, so the funder line, not the gap, closes a project. Period and amount are read
    by pattern only, and a project without either stops the publish.
    """
    parsed = Parsed()
    year: int | None = None
    sections: dict[int, list[Block]] = {}
    for block in blocks:
        if match := _PROJECT_YEAR.match(block.text):
            year = int(match.group(1))
            sections.setdefault(year, [])
        elif year is not None and block.kind == "paragraph":
            sections[year].append(block)
    if not sections:
        parsed.problems.append("projects: no year headings found")
    for year, section in sections.items():
        titles: list[str] = []
        position = 0
        for line in _lines(section):
            if not _PERIOD.search(line):
                titles.append(line)
                continue
            position += 1
            if draft := _project(titles, line, year, position, parsed):
                parsed.drafts.append(draft)
            titles = []
        if position == 0:
            parsed.problems.append(f"projects {year}: a year without projects")
        if titles:
            parsed.problems.append(f"projects {year}: lines without a period: {titles!r}")
    parsed.drafts = separate_collisions(parsed.drafts)
    return parsed


def _project(
    titles: list[str], funder_line: str, year: int, position: int, parsed: Parsed
) -> RecordDraft | None:
    where = f"projects {year} #{position}"
    period, amount = _PERIOD.search(funder_line), _AMOUNT.search(funder_line)
    if amount is None:
        parsed.problems.append(f"{where}: no NTD amount in {funder_line!r}")
        return None
    if not titles:
        parsed.problems.append(f"{where}: no title before {funder_line!r}")
        return None
    # By position: an English title may hold a Chinese name ("Postdoctoral Research: 鄭智銘").
    if len(titles) == 1:
        title_zh, title_en = (titles[0], None) if _CJK.search(titles[0]) else (None, titles[0])
    else:
        title_zh, title_en = titles[0], titles[1]
    start, end = _date(period.group("start")), _date(period.group("end"))
    warnings = [f"unread line {line!r}" for line in titles[2:]]
    if start is None or end is None or start > end:
        # A typo on the page: keep what it says, and claim no dates it does not state.
        warnings.append(f"no valid period in {period.group(0)!r}; dates left empty")
        start = end = None
    elif "//" in period.group(0):
        warnings.append(f"period written as {period.group(0)!r}")
    funder_raw = funder_line[: period.start()].strip()
    funder_zh, funder_en = _split_funder(funder_raw)
    return record_draft(
        "project",
        f"project:{key_text(title_zh or title_en)}:{year}",
        raw={"lines": [*titles, funder_line]},
        fields={
            "title_zh": title_zh,
            "title_en": title_en,
            "funder_raw": funder_raw,
            "funder_zh": funder_zh,
            "funder_en": funder_en,
            "period_raw": period.group(0),
            "period_start": start,
            "period_end": end,
            "amount_ntd": int(amount.group("amount").replace(",", "")),
            "amount_raw": amount.group(0),
            "year": year,
        },
        locator={"year_block": year, "position": position},
        warnings=[f"{where}: {warning}" for warning in warnings],
    )


def _split_funder(funder: str) -> tuple[str | None, str | None]:
    """ "科技部 Ministry of Science and Technology" → its Chinese and English names."""
    if match := _FUNDER.match(funder):
        return match.group("zh").strip(), match.group("en").strip()
    return (funder, None) if _CJK.search(funder) else (None, funder)


def _date(text: str) -> date | None:
    year, month, day = (int(part) for part in text.split("/") if part)
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --- Publications -----------------------------------------------------------------------


def parse_publications(blocks: Sequence[Block]) -> Parsed:
    """One record per list item, filed under the category heading above it.

    The page writes some headings as paragraphs, so a block is a heading when its text
    names a known category, whatever its tag.
    """
    parsed = Parsed()
    category: str | None = None
    positions: dict[str, int] = {}
    for block in blocks:
        if block.kind in ("heading", "paragraph") and block.text in PUBLICATION_CATEGORIES:
            category = block.text
            continue
        if block.kind != "item":
            continue
        if category is None:
            parsed.problems.append(f"publications: an item before any category: {block.text!r}")
            continue
        positions[category] = positions.get(category, 0) + 1
        parsed.drafts.append(_publication(block, category, positions[category]))
    missing = [name for name in PUBLICATION_CATEGORIES if name not in positions]
    if missing:
        parsed.problems.append(f"publications: no items under {missing}")
    parsed.drafts = separate_collisions(parsed.drafts)
    return parsed


def _publication(block: Block, category: str, position: int) -> RecordDraft:
    text = _without_trailing_labels(block.text, block.links)
    year_match = _CITATION_YEAR.search(text) or _DATE_YEAR.search(text)
    year = int(year_match.group(1)) if year_match else None
    if year_match is None and (roc := _ROC_YEAR.search(text)):
        year_match, year = roc, int(roc.group(1)) + ROC_YEAR_OFFSET
    doi = _DOI.search(" ".join([text, *(link.url or "" for link in block.links)]))
    links = [{"kind": _link_kind(link), **link_json(link)} for link in block.links]
    identity = doi.group("doi").rstrip(".").lower() if doi else key_text(text)[:160]
    warnings = []
    if year_match is None:
        warnings.append("no year found")
    if any(link.url is None for link in block.links):
        warnings.append("a link leads nowhere on the source page")
    code = PUBLICATION_CATEGORIES[category]
    return record_draft(
        "list_item",
        f"publication:{code}:{identity}",
        raw={"text": block.text, "links": [link_json(link) for link in block.links]},
        fields={
            "list_kind": "publication",
            "category": category,
            "section_path": ["Publications", category],
            "year": year,
            "year_raw": year_match.group(0) if year_match else None,
            "item_text": text,
            "links": links,
        },
        locator={"category": category, "position": position},
        warnings=[f"publications {category} #{position}: {warning}" for warning in warnings],
    )


def _without_trailing_labels(text: str, links: Sequence[Link]) -> str:
    """The item without the "PDF" or "Link" anchors it ends with; they are kept as links."""
    labels = {link.text for link in links if link.text.casefold() in _LINK_LABELS}
    stripped = True
    while stripped:
        stripped = False
        for label in labels:
            if text.endswith(label):
                text, stripped = text[: -len(label)].rstrip(), True
    return text


def _link_kind(link: Link) -> str:
    if link.url and _DOI.match(link.url):
        return "doi"
    return "pdf" if link.text.casefold() == "pdf" else "link"


# --- Profile page -----------------------------------------------------------------------


def parse_profile(blocks: Sequence[Block], *, person: str, intro_heading: str) -> Parsed:
    """The profile's prose becomes section records; each listed line, a profile item.

    Blocks are this page's own, site chrome already removed. Lists sit under the known
    section names; a text element opening with "Biography" is the English biography; any
    other prose is the introduction.
    """
    parsed = Parsed()
    by_element: dict[str, list[Block]] = {}
    for block in blocks:
        by_element.setdefault(block.element_id, []).append(block)
    found_sections: set[str] = set()
    for element_blocks in by_element.values():
        if any(block.kind == "item" for block in element_blocks):
            _profile_lists(element_blocks, person, parsed, found_sections)
            continue
        texts = [block.text for block in element_blocks if block.kind == "paragraph"]
        if not texts:
            continue
        if texts[0] == "Biography":
            heading, texts = "Biography", texts[1:]
        else:
            heading = intro_heading
        if texts:
            parsed.drafts.append(_section(person, heading, texts))
    missing = [name for name in PROFILE_SECTIONS if name not in found_sections]
    if missing:
        parsed.problems.append(f"profile: no items under {missing}")
    parsed.drafts = separate_collisions(parsed.drafts)
    return parsed


def _profile_lists(blocks: list[Block], person: str, parsed: Parsed, found: set[str]) -> None:
    section: str | None = None
    position = 0
    for block in blocks:
        if block.kind in ("heading", "paragraph") and block.text in PROFILE_SECTIONS:
            section, position = block.text, 0
            continue
        if block.kind != "item":
            continue
        if section is None:
            parsed.problems.append(f"profile: an item before any section: {block.text!r}")
            continue
        found.add(section)
        position += 1
        parsed.drafts.append(
            record_draft(
                "list_item",
                f"profile:{key_text(person)}:{key_text(section)}:{key_text(block.text)[:160]}",
                raw={"text": block.text, "links": [link_json(link) for link in block.links]},
                fields={
                    "list_kind": "profile_item",
                    "category": section,
                    "section_path": [person, section],
                    "year": None,
                    "year_raw": None,
                    "item_text": block.text,
                    "links": [link_json(link) for link in block.links],
                },
                locator={"section": section, "position": position},
            )
        )


def _section(person: str, heading: str, paragraphs: list[str]) -> RecordDraft:
    return record_draft(
        "section",
        f"section:{key_text(person)}:{key_text(heading)}",
        raw={"heading_path": [person, heading], "paragraphs": paragraphs},
        fields={},
        locator={"heading": heading},
    )
