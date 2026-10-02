"""Prose pages → section records: what sits under each heading, in page order.

The headings decide how many records a page holds. A Wix page's heading levels are
styling, so its headings are read as one level; a Docusaurus page's levels nest, and its
title (h1) names the page. Tables keep their cells, and buttons leading off the site or
to a file, such as a catalog, are kept as links of the section they sit in.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from wobot.knowledge.records.drafts import RecordDraft, record_draft, separate_collisions
from wobot.knowledge.records.grc import Parsed, link_json
from wobot.knowledge.records.text import key_text
from wobot.knowledge.sources.wix import Block, Link


@dataclass(frozen=True)
class SectionPage:
    url: str
    site: str  # as the reader sees it, first in every heading path
    site_key: str  # in logical keys
    title: str
    page_key: str  # the page in logical keys, such as "whizpad"


@dataclass
class _Section:
    path: list[str]
    paragraphs: list[str] = field(default_factory=list)
    # Each table with the number of paragraphs before it, so page order can be rebuilt.
    tables: list[dict[str, Any]] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    last_element: str | None = None  # the Wix element the last paragraph came from

    @property
    def empty(self) -> bool:
        return not (self.paragraphs or self.tables or self.links)


def section_records(blocks: Sequence[Block], page: SectionPage, *, nested: bool) -> Parsed:
    """`nested`: heading levels nest, and the first h1 is the page's title, not a section."""
    root = _Section([])
    sections = [root]
    open_headings: list[tuple[int, str]] = []
    current = root
    for block in blocks:
        if block.kind == "heading":
            level = (block.level or 1) if nested else 1
            if nested and level == 1 and not open_headings and current is root:
                continue  # the page title
            while open_headings and open_headings[-1][0] >= level:
                open_headings.pop()
            open_headings.append((level, block.text))
            path = [text for _, text in open_headings]
            if path != current.path:  # a heading repeated at once, as Wix pages do, goes on
                current = _Section(path)
                sections.append(current)
            current.links += [_absolute(link, page.url) for link in block.links]
        elif block.kind in ("paragraph", "item", "code"):
            text = f"- {block.text}" if block.kind == "item" else block.text
            if (
                block.kind == "paragraph"
                and block.element_id
                and block.element_id == current.last_element
            ):
                # Wix sets one text box as a <p> per line: the box is the paragraph.
                current.paragraphs[-1] += "\n" + text
            else:
                current.paragraphs.append(text)
            current.last_element = block.element_id if block.kind == "paragraph" else None
            current.links += [_absolute(link, page.url) for link in block.links]
        elif block.kind == "blank":
            current.last_element = None
        elif block.kind == "table":
            current.tables.append(
                {"after": len(current.paragraphs), "rows": [list(row) for row in block.rows]}
            )
            current.links += [_absolute(link, page.url) for link in block.links]
        elif block.kind == "button":
            current.links += [
                link
                for link in (_absolute(link, page.url) for link in block.links)
                if _leads_out(link, page.url)
            ]
    parsed = Parsed()
    for position, (section, following) in enumerate(
        zip(sections, [*sections[1:], None], strict=True)
    ):
        # A heading with nothing under it is content itself, such as a figure set as a
        # heading; one that only opens subsections is not a record of its own.
        opens_subsections = following is not None and following.path[: len(section.path)] == (
            section.path
        )
        if section.empty and (section is root or opens_subsections):
            continue
        parsed.drafts.append(_draft(page, section, position))
    parsed.drafts = separate_collisions(parsed.drafts)
    return parsed


def _draft(page: SectionPage, section: _Section, position: int) -> RecordDraft:
    links = [link_json(link) for link in dict.fromkeys(section.links)]
    key = ":".join(["section", page.site_key, page.page_key, *map(key_text, section.path)])
    return record_draft(
        "section",
        key[:240],
        raw={
            "heading_path": [page.site, page.title, *section.path],
            "paragraphs": section.paragraphs,
            "tables": section.tables,
            "links": links,
        },
        fields={},
        locator={"url": page.url, "position": position},
    )


def _absolute(link: Link, page_url: str) -> Link:
    return Link(link.text, urljoin(page_url, link.url) if link.url else None)


def _leads_out(link: Link, page_url: str) -> bool:
    """A button worth keeping: one to another site or to a file, not the site's menu."""
    if not link.url:
        return False
    target = urlsplit(link.url)
    if target.scheme not in ("http", "https"):
        return False
    return target.hostname != urlsplit(page_url).hostname or target.path.startswith("/_files/")
